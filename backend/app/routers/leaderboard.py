"""
Leaderboard, player statistics, active-user metrics, and agent performance routes.

Public routes (player JWT required):
  GET /api/leaderboard/tournaments     — recent completed tournament winners
  GET /api/leaderboard/players         — top players by winnings / wins
  GET /api/leaderboard/players/{uid}   — individual player public stats

Admin routes (staff JWT required):
  GET /api/admin/metrics/active-users  — online / active today / active 24h
  GET /api/admin/metrics/agents        — agent tournament performance stats
  GET /api/admin/metrics/tournaments   — aggregate tournament stats

Time definitions:
  online today = last_seen_at within 3 minutes (UTC)
  active today = last_seen_at on current IST calendar day
  active 24h   = last_seen_at within rolling 24 hours

IST = UTC+05:30.  Computed server-side from last_seen_at.

Leaderboard windows:
  - Rolling 24-hour  — completed_at within the last 24 hours.
  - All-time         — all completed tournaments.
  - Both supported via ?window=24h or ?window=all.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_staff, get_current_user
from app.models.player_stats import PlayerStats
from app.models.result import ResultStatus, TournamentResult
from app.models.staff_user import StaffRole, StaffUser
from app.models.tournament import EntryStatus, Tournament, TournamentEntry, TournamentStatus
from app.models.user import User

public_router = APIRouter(prefix="/api/leaderboard", tags=["Leaderboard"])
admin_router = APIRouter(prefix="/api/admin/metrics", tags=["Admin — Metrics"])

# IST offset
_IST = timezone(timedelta(hours=5, minutes=30))
# "Online" threshold: active within last N minutes
_ONLINE_MINUTES = 3


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ══════════════════════════════════════════════════════════════════════════════
# TOURNAMENT LEADERBOARD
# ══════════════════════════════════════════════════════════════════════════════

@public_router.get(
    "/tournaments",
    summary="Recent completed tournament winners",
)
async def tournament_leaderboard(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    window: str = Query(
        default="24h",
        description="Time window: '24h' (rolling) or 'all' (all-time)",
    ),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> dict:
    """
    Returns completed tournaments with their 1st-place winner, entry fee,
    total payout, and creator information.

    Creator identity comes from the immutable tournament.created_by_display_name
    snapshot — accurate even if the staff account is later deleted.
    """
    now = _utcnow()
    base_q = select(Tournament).where(
        Tournament.status == TournamentStatus.completed
    )
    if window == "24h":
        since = now - timedelta(hours=24)
        base_q = base_q.where(Tournament.completed_at >= since)

    count_result = await db.execute(
        select(func.count()).select_from(base_q.subquery())
    )
    total = count_result.scalar_one()

    t_result = await db.execute(
        base_q.order_by(desc(Tournament.completed_at)).limit(limit).offset(offset)
    )
    tournaments = list(t_result.scalars().all())

    if not tournaments:
        return {"items": [], "total": total, "limit": limit, "offset": offset}

    t_ids = [t.id for t in tournaments]

    # Batch-load 1st place results for these tournaments (existing winner field)
    winner_q = await db.execute(
        select(TournamentResult).where(
            TournamentResult.tournament_id.in_(t_ids),
            TournamentResult.placement == 1,
            TournamentResult.result_status == ResultStatus.finalized,
        )
    )
    winners = {r.tournament_id: r for r in winner_q.scalars().all()}

    # Batch-load TOP 10 finalized results for every tournament in this page.
    # Ordered by (tournament_id, placement) so we can group them in Python.
    top10_q = await db.execute(
        select(TournamentResult).where(
            TournamentResult.tournament_id.in_(t_ids),
            TournamentResult.placement <= 10,
            TournamentResult.result_status == ResultStatus.finalized,
        ).order_by(TournamentResult.tournament_id, TournamentResult.placement)
    )
    top10_raw = top10_q.scalars().all()

    # Group top-10 rows by tournament
    from collections import defaultdict as _dd
    top10_by_t: dict[uuid.UUID, list[TournamentResult]] = _dd(list)
    for r in top10_raw:
        top10_by_t[r.tournament_id].append(r)

    # Collect all participant user IDs (winners + top-10) for a single username lookup
    all_user_ids: set[uuid.UUID] = set()
    for r in winners.values():
        all_user_ids.add(r.user_id)
    for r in top10_raw:
        all_user_ids.add(r.user_id)

    # Phase 11: select ff_name alongside username for display-name fallback
    user_q = await db.execute(
        select(User.id, User.username, User.ff_name).where(User.id.in_(all_user_ids))
    )
    # display_name_map: returns ff_name when set, falls back to username
    _user_rows = list(user_q)
    uname_map: dict[uuid.UUID, str] = {row.id: row.username for row in _user_rows}
    display_name_map: dict[uuid.UUID, str] = {
        row.id: (row.ff_name or row.username) for row in _user_rows
    }

    items = []
    for t in tournaments:
        winner = winners.get(t.id)

        # Serialize top-10 rows — values come directly from finalized DB records,
        # no frontend recalculation.
        top_results = [
            {
                "user_id": str(r.user_id),
                # username = login key (stable URL anchor); display_name = shown in UI
                "username": uname_map.get(r.user_id, "unknown"),
                "display_name": display_name_map.get(r.user_id, uname_map.get(r.user_id, "unknown")),
                "placement": r.placement,
                "kills": r.kills,
                "prize_amount": str(r.prize_amount),
                "kill_reward_amount": str(r.kill_reward_amount),
                "total_payout": str(r.total_payout),
                "kill_reward_breakdown": r.kill_reward_breakdown,
            }
            for r in top10_by_t.get(t.id, [])
        ]

        items.append({
            "tournament_id": str(t.id),
            "tournament_code": t.tournament_code,
            "title": t.title,
            "entry_fee": str(t.entry_fee),
            "mode": t.mode.value,
            "scheduled_at": t.scheduled_at.isoformat(),
            "completed_at": t.completed_at.isoformat() if t.completed_at else None,
            "player_count": t.player_count,
            "created_by": t.created_by_display_name,
            "created_by_role": t.created_by_role,
            # Existing field — unchanged for backward compatibility
            "winner": {
                "user_id": str(winner.user_id),
                "username": uname_map.get(winner.user_id, "unknown"),
                "display_name": display_name_map.get(winner.user_id, uname_map.get(winner.user_id, "unknown")),
                "prize": str(winner.prize_amount),
                "kill_reward": str(winner.kill_reward_amount),
                "kills": winner.kills,
                "total_payout": str(winner.total_payout),
            } if winner else None,
            # New field: top-10 ranked results for the Tournaments tab UI
            "top_results": top_results,
        })

    return {"items": items, "total": total, "limit": limit, "offset": offset}


# ══════════════════════════════════════════════════════════════════════════════
# PLAYER LEADERBOARD
# ══════════════════════════════════════════════════════════════════════════════

@public_router.get(
    "/players",
    summary="Top players by total winnings",
)
async def player_leaderboard(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    sort_by: str = Query(
        default="winnings",
        description="Sort by: 'winnings', 'wins', or 'kills'",
    ),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> dict:
    """
    Top players ranked by lifetime winnings, wins, or kills.
    Only players with at least one tournament participation are returned.
    """
    base_q = select(PlayerStats, User.username, User.ff_name).join(
        User, PlayerStats.user_id == User.id
    ).where(PlayerStats.total_tournaments > 0)

    order_col = {
        "winnings": PlayerStats.total_winnings,
        "wins":     PlayerStats.total_wins,
        "kills":    PlayerStats.total_kills,
    }.get(sort_by, PlayerStats.total_winnings)

    count_result = await db.execute(
        select(func.count()).select_from(base_q.subquery())
    )
    total = count_result.scalar_one()

    result = await db.execute(
        base_q.order_by(desc(order_col)).limit(limit).offset(offset)
    )
    rows = result.all()

    items = [
        {
            "rank": offset + i + 1,
            "user_id": str(ps.user_id),
            "username": username,                        # login key — used in profile URL
            "display_name": ff_name or username,         # shown in UI
            "total_tournaments": ps.total_tournaments,
            "total_wins": ps.total_wins,
            "total_kills": ps.total_kills,
            "total_winnings": str(ps.total_winnings),
        }
        for i, (ps, username, ff_name) in enumerate(rows)
    ]
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@public_router.get(
    "/players/{username}",
    summary="Individual player public statistics",
)
async def player_public_stats(
    username: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    """Public stats for a named player. Does NOT expose wallet or private data."""
    user_result = await db.execute(
        select(User).where(User.username == username, User.is_active == True)  # noqa: E712
    )
    target = user_result.scalar_one_or_none()
    if target is None:
        from app.core.exceptions import NotFoundError
        raise NotFoundError("Player")

    stats_result = await db.execute(
        select(PlayerStats).where(PlayerStats.user_id == target.id)
    )
    ps = stats_result.scalar_one_or_none()

    return {
        "user_id": str(target.id),
        "username": target.username,
        "display_name": target.ff_name or target.username,   # Phase 11
        "ff_name": target.ff_name,                           # Phase 11 — raw value
        "total_tournaments": ps.total_tournaments if ps else 0,
        "total_wins": ps.total_wins if ps else 0,
        "total_kills": ps.total_kills if ps else 0,
        "total_winnings": str(ps.total_winnings) if ps else "0.00",
        "member_since": target.created_at.isoformat(),
    }


@public_router.get(
    "/players/{username}/tournament-history",
    summary="Per-player tournament history with full result leaderboards",
)
async def player_tournament_history(
    username: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> dict:
    """
    Returns all completed tournaments in which the named player participated,
    ordered newest-first.  Each item includes:
      - Tournament metadata (code, title, entry_fee, completed_at, player_count)
      - The player's own result (placement, kills, payout — once finalized)
      - The full ranked result list for that tournament so the caller can
        display a complete leaderboard per event without a second request.

    Only finalized results have payout amounts populated.
    Results that are not yet finalized still show placement and kills.
    Does NOT expose other players' wallet balances or private data.
    """
    from app.core.exceptions import NotFoundError as _NotFoundError
    from app.models.result import ResultStatus as _RS

    # Resolve player
    user_q = await db.execute(
        select(User).where(User.username == username, User.is_active == True)  # noqa: E712
    )
    target = user_q.scalar_one_or_none()
    if target is None:
        raise _NotFoundError("Player")

    # Find all completed tournaments the player has an active entry in
    entry_subq = (
        select(TournamentEntry.tournament_id)
        .where(
            TournamentEntry.user_id == target.id,
            TournamentEntry.status == EntryStatus.active,
        )
    )

    count_q = await db.execute(
        select(func.count()).select_from(
            select(Tournament.id)
            .where(
                Tournament.status == TournamentStatus.completed,
                Tournament.id.in_(entry_subq),
            )
            .subquery()
        )
    )
    total = count_q.scalar_one()

    t_q = await db.execute(
        select(Tournament)
        .where(
            Tournament.status == TournamentStatus.completed,
            Tournament.id.in_(entry_subq),
        )
        .order_by(desc(Tournament.completed_at))
        .limit(limit)
        .offset(offset)
    )
    tournaments = list(t_q.scalars().all())

    if not tournaments:
        return {"items": [], "total": total, "limit": limit, "offset": offset}

    t_ids = [t.id for t in tournaments]

    # Batch-load ALL results for these tournaments (all participants)
    all_results_q = await db.execute(
        select(TournamentResult)
        .where(TournamentResult.tournament_id.in_(t_ids))
        .order_by(TournamentResult.tournament_id, TournamentResult.placement)
    )
    all_results = list(all_results_q.scalars().all())

    # Phase 11: fetch ff_name alongside username for display-name fallback
    participant_user_ids = list({r.user_id for r in all_results})
    if participant_user_ids:
        uname_q = await db.execute(
            select(User.id, User.username, User.ff_name).where(User.id.in_(participant_user_ids))
        )
        _urows = list(uname_q)
        uname_map: dict[uuid.UUID, str] = {row.id: row.username for row in _urows}
        display_name_map: dict[uuid.UUID, str] = {
            row.id: (row.ff_name or row.username) for row in _urows
        }
    else:
        uname_map = {}
        display_name_map = {}

    # Group results by tournament
    from collections import defaultdict as _dd
    results_by_tournament: dict[uuid.UUID, list[TournamentResult]] = _dd(list)
    for r in all_results:
        results_by_tournament[r.tournament_id].append(r)

    def _serialize_result(r: TournamentResult, is_self: bool) -> dict:
        finalized = r.payout_distributed
        return {
            "result_id": str(r.id),
            "user_id": str(r.user_id),
            "username": uname_map.get(r.user_id, "unknown"),
            "display_name": display_name_map.get(r.user_id, uname_map.get(r.user_id, "unknown")),
            "placement": r.placement,
            "kills": r.kills,
            "result_status": r.result_status.value,
            "is_self": is_self,
            # Payout fields — only populated once finalized
            "prize_amount": str(r.prize_amount) if finalized else None,
            "kill_reward_amount": str(r.kill_reward_amount) if finalized else None,
            "total_payout": str(r.total_payout) if finalized else None,
            "kill_reward_breakdown": r.kill_reward_breakdown if finalized else None,
            "kill_reward_eligible": r.kill_reward_eligible if finalized else None,
        }

    items = []
    for t in tournaments:
        t_results = results_by_tournament.get(t.id, [])
        # Find this player's own result
        own_result = next((r for r in t_results if r.user_id == target.id), None)
        serialized = [_serialize_result(r, r.user_id == target.id) for r in t_results]
        items.append({
            "tournament_id": str(t.id),
            "tournament_code": t.tournament_code,
            "title": t.title,
            "entry_fee": str(t.entry_fee),
            "mode": t.mode.value,
            "scheduled_at": t.scheduled_at.isoformat(),
            "completed_at": t.completed_at.isoformat() if t.completed_at else None,
            "player_count": t.player_count,
            # The player's own result summary (convenience field)
            "player_result": _serialize_result(own_result, True) if own_result else None,
            # Full ranked leaderboard for this tournament
            "results": serialized,
        })

    return {"items": items, "total": total, "limit": limit, "offset": offset}


# ══════════════════════════════════════════════════════════════════════════════
# ADMIN — ACTIVE USER METRICS
# ══════════════════════════════════════════════════════════════════════════════

@admin_router.get(
    "/active-users",
    summary="Active user counts (Admin)",
)
async def active_user_metrics(
    db: AsyncSession = Depends(get_db),
    current_staff: StaffUser = Depends(get_current_staff),
) -> dict:
    """
    Returns:
      online_now    — last_seen_at within last 3 minutes
      active_today  — last_seen_at on current IST calendar day
      active_24h    — last_seen_at within rolling 24 hours

    Uses existing User.last_seen_at field (updated by LastSeenMiddleware).
    Does NOT expose any private user information.
    """
    now = _utcnow()
    online_threshold = now - timedelta(minutes=_ONLINE_MINUTES)
    h24_threshold = now - timedelta(hours=24)

    # IST calendar day boundaries
    now_ist = now.astimezone(_IST)
    day_start_ist = now_ist.replace(hour=0, minute=0, second=0, microsecond=0)
    day_start_utc = day_start_ist.astimezone(timezone.utc)

    online_result = await db.execute(
        select(func.count()).select_from(User).where(
            User.last_seen_at >= online_threshold,
            User.is_active == True,  # noqa: E712
            User.is_banned == False,  # noqa: E712
        )
    )
    active_today_result = await db.execute(
        select(func.count()).select_from(User).where(
            User.last_seen_at >= day_start_utc,
            User.is_active == True,  # noqa: E712
            User.is_banned == False,  # noqa: E712
        )
    )
    active_24h_result = await db.execute(
        select(func.count()).select_from(User).where(
            User.last_seen_at >= h24_threshold,
            User.is_active == True,  # noqa: E712
            User.is_banned == False,  # noqa: E712
        )
    )
    total_registered_result = await db.execute(
        select(func.count()).select_from(User).where(
            User.is_active == True  # noqa: E712
        )
    )

    return {
        "online_now": online_result.scalar_one(),
        "active_today_ist": active_today_result.scalar_one(),
        "active_last_24h": active_24h_result.scalar_one(),
        "total_registered": total_registered_result.scalar_one(),
        "as_of": now.isoformat(),
    }


# ══════════════════════════════════════════════════════════════════════════════
# ADMIN — AGENT PERFORMANCE
# ══════════════════════════════════════════════════════════════════════════════

@admin_router.get(
    "/agents",
    summary="Agent tournament performance statistics (Admin)",
)
async def agent_stats(
    db: AsyncSession = Depends(get_db),
    current_staff: StaffUser = Depends(get_current_staff),
) -> dict:
    """
    Per-agent breakdown of tournament creation statistics.
    Returns data for all agents (and super_admins who created tournaments).
    Does NOT include salary/bonus calculations (deferred to Phase 5).
    """
    now = _utcnow()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    # Aggregate per creator
    stats_q = await db.execute(
        select(
            Tournament.created_by_id,
            Tournament.created_by_display_name,
            Tournament.created_by_role,
            func.count().label("total"),
            func.sum(
                func.cast(Tournament.status == TournamentStatus.completed, type_=db.get_bind().dialect.name == "postgresql" and "integer" or "INTEGER")
            ).label("completed"),
            func.sum(
                func.cast(Tournament.status == TournamentStatus.cancelled, type_=db.get_bind().dialect.name == "postgresql" and "integer" or "INTEGER")
            ).label("cancelled"),
        ).group_by(
            Tournament.created_by_id,
            Tournament.created_by_display_name,
            Tournament.created_by_role,
        )
    )
    # Use a simpler approach with raw counts
    all_t = await db.execute(select(Tournament))
    all_tournaments = list(all_t.scalars().all())

    # Aggregate in Python to avoid dialect-specific CAST issues
    from collections import defaultdict
    agg: dict[str, dict] = defaultdict(lambda: {
        "total": 0, "completed": 0, "cancelled": 0,
        "active": 0, "today": 0,
        "display_name": "", "role": "", "id": "",
    })

    today_start_naive = today_start.replace(tzinfo=None)

    for t in all_tournaments:
        key = str(t.created_by_id)
        a = agg[key]
        a["id"] = str(t.created_by_id)
        a["display_name"] = t.created_by_display_name
        a["role"] = t.created_by_role
        a["total"] += 1
        if t.status == TournamentStatus.completed:
            a["completed"] += 1
        if t.status == TournamentStatus.cancelled:
            a["cancelled"] += 1
        if t.status in (
            TournamentStatus.open, TournamentStatus.confirmed,
            TournamentStatus.room_released, TournamentStatus.live,
            TournamentStatus.result_entry,
        ):
            a["active"] += 1
        # Today count
        t_created = t.created_at
        if t_created.tzinfo is not None:
            t_created = t_created.replace(tzinfo=None)
        if t_created >= today_start_naive:
            a["today"] += 1

    items = sorted(agg.values(), key=lambda x: x["total"], reverse=True)
    return {"agents": items, "as_of": now.isoformat()}


# ══════════════════════════════════════════════════════════════════════════════
# ADMIN — TOURNAMENT AGGREGATE STATS
# ══════════════════════════════════════════════════════════════════════════════

@admin_router.get(
    "/tournaments",
    summary="Aggregate tournament statistics (Admin)",
)
async def tournament_aggregate_stats(
    db: AsyncSession = Depends(get_db),
    current_staff: StaffUser = Depends(get_current_staff),
) -> dict:
    """Platform-wide tournament statistics for the admin dashboard."""
    all_t = await db.execute(select(Tournament))
    tournaments = list(all_t.scalars().all())

    stats = {s.value: 0 for s in TournamentStatus}
    total_entry_fee_collected = 0.0
    total_players_across_tournaments = 0

    for t in tournaments:
        stats[t.status.value] += 1
        total_players_across_tournaments += t.player_count

    # Total prize distributed (sum of finalized result payouts)
    prize_q = await db.execute(
        select(func.sum(TournamentResult.prize_amount + TournamentResult.kill_reward_amount))
        .where(TournamentResult.payout_distributed == True)  # noqa: E712
    )
    total_distributed = prize_q.scalar_one() or 0

    return {
        "by_status": stats,
        "total_tournaments": len(tournaments),
        "total_players_enrolled": total_players_across_tournaments,
        "total_prize_distributed": str(total_distributed),
    }


# ══════════════════════════════════════════════════════════════════════════════
# ADMIN / AGENT — LEADERBOARD ENDPOINTS
# Gated by report.view permission.
# Super Admin bypasses the permission check automatically (via get_current_staff).
# Agents must have an explicit report.view grant.
# ══════════════════════════════════════════════════════════════════════════════

async def _require_report_view(
    staff: StaffUser = Depends(get_current_staff),
    db: AsyncSession = Depends(get_db),
) -> StaffUser:
    """Dependency: require report.view permission (super_admin always passes)."""
    from app.core.permissions import Permission as _Perm
    from app.core.exceptions import PermissionDeniedError as _PDError

    if staff.role == StaffRole.super_admin:
        return staff

    from sqlalchemy.orm import joinedload as _jl
    from app.models.permission import StaffUserPermission as _SUP
    result = await db.execute(
        select(_SUP)
        .options(_jl(_SUP.permission))
        .where(_SUP.staff_user_id == staff.id)
    )
    granted: set[str] = {
        g.permission.code for g in result.unique().scalars().all()
        if g.permission is not None
    }
    if _Perm.REPORT_VIEW.value not in granted:
        raise _PDError(_Perm.REPORT_VIEW.value)
    return staff


@admin_router.get(
    "/leaderboard/players",
    summary="Player leaderboard — top players by stats (Admin/Agent — report.view)",
)
async def admin_player_leaderboard(
    staff: StaffUser = Depends(_require_report_view),
    db: AsyncSession = Depends(get_db),
    sort_by: str = Query(
        default="winnings",
        description="Sort by: 'winnings', 'wins', 'kills', or 'tournaments'",
    ),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> dict:
    """
    Returns ranked player statistics from PlayerStats.
    Values come directly from the denormalised player_stats table —
    no prize/kill-reward/payout recalculation is performed here.

    Accessible to:
      - Super Admin  (always)
      - Agent        (requires report.view grant)
    """
    order_col = {
        "winnings":    PlayerStats.total_winnings,
        "wins":        PlayerStats.total_wins,
        "kills":       PlayerStats.total_kills,
        "tournaments": PlayerStats.total_tournaments,
    }.get(sort_by, PlayerStats.total_winnings)

    base_q = (
        select(PlayerStats, User.username, User.ff_name)
        .join(User, PlayerStats.user_id == User.id)
        .where(PlayerStats.total_tournaments > 0)
    )

    count_result = await db.execute(
        select(func.count()).select_from(base_q.subquery())
    )
    total = count_result.scalar_one()

    rows_result = await db.execute(
        base_q.order_by(desc(order_col)).limit(limit).offset(offset)
    )
    rows = rows_result.all()

    items = [
        {
            "rank": offset + i + 1,
            "user_id": str(ps.user_id),
            "username": username,
            "display_name": ff_name or username,
            "total_tournaments": ps.total_tournaments,
            "total_wins": ps.total_wins,
            "total_kills": ps.total_kills,
            "total_winnings": str(ps.total_winnings),
        }
        for i, (ps, username, ff_name) in enumerate(rows)
    ]
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@admin_router.get(
    "/leaderboard/tournaments",
    summary="Tournament leaderboard — completed tournaments with top-10 results (Admin/Agent — report.view)",
)
async def admin_tournament_leaderboard(
    staff: StaffUser = Depends(_require_report_view),
    db: AsyncSession = Depends(get_db),
    window: str = Query(
        default="24h",
        description="Time window: '24h' (rolling 24 hours) or 'all' (all-time)",
    ),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> dict:
    """
    Returns completed tournaments ordered by most-recently completed first,
    each with:
      - Tournament metadata (code, title, entry_fee, player_count, completed_at)
      - 1st-place winner summary (prize_amount, kill_reward_amount, total_payout)
      - Top-10 finalized results (placement, username, ff_uid_snapshot, kills,
        prize_amount, kill_reward_amount, total_payout)

    Payout values come directly from finalized TournamentResult rows —
    no financial recalculation is performed here.

    Accessible to:
      - Super Admin  (always)
      - Agent        (requires report.view grant)
    """
    now = _utcnow()
    base_q = select(Tournament).where(Tournament.status == TournamentStatus.completed)
    if window == "24h":
        since = now - timedelta(hours=24)
        base_q = base_q.where(Tournament.completed_at >= since)

    count_result = await db.execute(
        select(func.count()).select_from(base_q.subquery())
    )
    total = count_result.scalar_one()

    t_result = await db.execute(
        base_q.order_by(desc(Tournament.completed_at)).limit(limit).offset(offset)
    )
    tournaments = list(t_result.scalars().all())

    if not tournaments:
        return {"items": [], "total": total, "limit": limit, "offset": offset}

    t_ids = [t.id for t in tournaments]

    # Batch-load top-10 finalized results per tournament
    top10_q = await db.execute(
        select(TournamentResult).where(
            TournamentResult.tournament_id.in_(t_ids),
            TournamentResult.placement <= 10,
            TournamentResult.result_status == ResultStatus.finalized,
        ).order_by(TournamentResult.tournament_id, TournamentResult.placement)
    )
    top10_raw = list(top10_q.scalars().all())

    # Group by tournament
    from collections import defaultdict as _dd2
    top10_by_t: dict[uuid.UUID, list[TournamentResult]] = _dd2(list)
    for r in top10_raw:
        top10_by_t[r.tournament_id].append(r)

    # Single username + ff_name lookup for winner display names
    all_uids: set[uuid.UUID] = {r.user_id for r in top10_raw}
    uname_map: dict[uuid.UUID, str] = {}
    ffname_map: dict[uuid.UUID, str | None] = {}
    if all_uids:
        uq = await db.execute(
            select(User.id, User.username, User.ff_name).where(User.id.in_(all_uids))
        )
        for row in uq:
            uname_map[row.id] = row.username
            ffname_map[row.id] = row.ff_name

    items = []
    for t in tournaments:
        top10 = top10_by_t.get(t.id, [])
        winner = top10[0] if top10 else None  # placement 1 is first (ordered above)

        top_results = [
            {
                "placement": r.placement,
                "user_id": str(r.user_id),
                "username": uname_map.get(r.user_id, "unknown"),
                # ff_name_snapshot = name at tournament time; fall back to live ff_name or username
                "display_name": (
                    r.ff_name_snapshot
                    or ffname_map.get(r.user_id)
                    or uname_map.get(r.user_id, "unknown")
                ),
                "ff_uid_snapshot": r.ff_uid_snapshot,
                "kills": r.kills,
                "prize_amount": str(r.prize_amount),
                "kill_reward_amount": str(r.kill_reward_amount),
                "total_payout": str(r.total_payout),
                "kill_reward_breakdown": r.kill_reward_breakdown,
            }
            for r in top10
        ]

        items.append({
            "tournament_id": str(t.id),
            "tournament_code": t.tournament_code,
            "title": t.title,
            "entry_fee": str(t.entry_fee),
            "mode": t.mode.value,
            "completed_at": t.completed_at.isoformat() if t.completed_at else None,
            "player_count": t.player_count,
            "created_by": t.created_by_display_name,
            "winner": {
                "username": uname_map.get(winner.user_id, "unknown"),
                "display_name": (
                    winner.ff_name_snapshot
                    or ffname_map.get(winner.user_id)
                    or uname_map.get(winner.user_id, "unknown")
                ),
                "kills": winner.kills,
                "prize_amount": str(winner.prize_amount),
                "kill_reward_amount": str(winner.kill_reward_amount),
                "total_payout": str(winner.total_payout),
            } if winner else None,
            "top_results": top_results,
        })

    return {"items": items, "total": total, "limit": limit, "offset": offset}
