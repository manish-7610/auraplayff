"""
Leaderboard cache refresh + active user metrics job — Phase 7.

Design philosophy
-----------------
The public leaderboard is query-driven by default.  This job pre-computes
the two most expensive leaderboard queries and stores the results in the
`leaderboard_cache` in-memory dict on the FastAPI app state so that the
API endpoints can serve cached results without hitting the DB on every
request.

Why not a separate cache table?
  A lightweight in-process dict is sufficient for this single-instance
  deployment.  If the project moves to multiple replicas a Redis-backed
  cache can replace this without changing the API surface — only the
  _read_cache / _write_cache helpers need updating.

Historical data is NEVER deleted.  This job only refreshes a read-through
cache — it never mutates tournament, player_stats, or any other source table.

Active user metrics
-------------------
run_active_user_metrics() pre-aggregates the three activity windows
(online / active_today_ist / active_last_24h) and stores them in the same
cache dict.  The admin /metrics/active-users endpoint can read from here
on cache-hit, or fall through to the live query on cache-miss.

IST calendar date
-----------------
active_today_ist uses the IST calendar day boundary.
All other windows use rolling UTC deltas.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.player_stats import PlayerStats
from app.models.tournament import Tournament, TournamentStatus, TournamentEntry, EntryStatus
from app.models.user import User

logger = logging.getLogger("auraplayff.jobs.leaderboard_refresh")

# IST offset
_IST = timezone(timedelta(hours=5, minutes=30))

# Module-level in-process cache dict
# Keys follow the convention used by the leaderboard router
_CACHE: dict[str, Any] = {}

# Cache TTL for leaderboard data (5 minutes default)
LEADERBOARD_CACHE_TTL_SECONDS: int = 300


# ── Cache helpers ─────────────────────────────────────────────────────────────

def get_cache(key: str) -> Any | None:
    """Return cached value if present and not expired, else None."""
    entry = _CACHE.get(key)
    if entry is None:
        return None
    value, expires_at = entry
    if datetime.now(timezone.utc) >= expires_at:
        del _CACHE[key]
        return None
    return value


def set_cache(key: str, value: Any, ttl_seconds: int = LEADERBOARD_CACHE_TTL_SECONDS) -> None:
    """Store a value with an expiry time."""
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
    _CACHE[key] = (value, expires_at)


def invalidate_cache(key: str) -> None:
    """Explicitly remove a cache entry."""
    _CACHE.pop(key, None)


def invalidate_all() -> None:
    """Flush the entire cache (used on app shutdown or test teardown)."""
    _CACHE.clear()


# ── Job: Leaderboard refresh ──────────────────────────────────────────────────

async def run_leaderboard_refresh(db: AsyncSession) -> int:
    """
    Pre-compute and cache the leaderboard datasets.

    Refreshes:
      - "leaderboard:players:winnings"  — top players by total winnings
      - "leaderboard:players:wins"      — top players by tournament wins
      - "leaderboard:players:kills"     — top players by total kills
      - "leaderboard:tournaments:24h"   — completed tournaments in last 24h
      - "leaderboard:tournaments:all"   — all completed tournaments (recent 50)

    Returns 1 on success, 0 if an error prevented a full refresh.
    """
    try:
        await _cache_player_leaderboards(db)
        await _cache_tournament_leaderboards(db)
        logger.info("Leaderboard cache refreshed successfully.")
        return 1
    except Exception:
        logger.exception("Leaderboard refresh job failed.")
        return 0


async def _cache_player_leaderboards(db: AsyncSession) -> None:
    """Cache top-50 player stats for each sort dimension."""
    for sort_col, key_suffix in [
        (PlayerStats.total_winnings, "winnings"),
        # BUG #10 FIX: PlayerStats.tournament_wins does not exist — the correct
        # column name on the model is total_wins.  Using the wrong attribute
        # caused AttributeError crashing the entire leaderboard refresh job.
        (PlayerStats.total_wins, "wins"),
        (PlayerStats.total_kills, "kills"),
    ]:
        # BUG #10 FIX: JOIN User to include username in cached data.
        # The previous query only stored user_id (UUID), so the leaderboard
        # API/frontend had no way to display a human-readable player name
        # without making N additional requests.
        result = await db.execute(
            select(PlayerStats, User.username)
            .join(User, User.id == PlayerStats.user_id)
            .order_by(sort_col.desc())
            .limit(50)
        )
        rows = result.all()  # list of (PlayerStats, username) tuples
        # Convert to serialisable dicts (no ORM objects in cache)
        data = [
            {
                "user_id":          str(r.PlayerStats.user_id),
                "username":         r.username,
                "total_winnings":   str(r.PlayerStats.total_winnings),
                # BUG #10 FIX: correct attribute name total_wins (was tournament_wins)
                "total_wins":       r.PlayerStats.total_wins,
                "total_kills":      r.PlayerStats.total_kills,
                # BUG #10 FIX: correct attribute name total_tournaments (was tournaments_played)
                "total_tournaments": r.PlayerStats.total_tournaments,
            }
            for r in rows
        ]
        set_cache(f"leaderboard:players:{key_suffix}", data)


async def _cache_tournament_leaderboards(db: AsyncSession) -> None:
    """Cache recent completed tournaments for both 24h and all-time windows."""
    now = datetime.now(timezone.utc)
    cutoff_24h = now - timedelta(hours=24)

    # All-time: last 50 completed tournaments
    result_all = await db.execute(
        select(Tournament)
        .where(Tournament.status == TournamentStatus.completed)
        .order_by(Tournament.completed_at.desc())
        .limit(50)
    )
    all_tournaments = list(result_all.scalars().all())

    # 24h window: subset of the above
    tournaments_24h = [
        t for t in all_tournaments
        if t.completed_at is not None and t.completed_at >= cutoff_24h
    ]

    for key, subset in [
        ("leaderboard:tournaments:all", all_tournaments),
        ("leaderboard:tournaments:24h", tournaments_24h),
    ]:
        data = [
            {
                "id": str(t.id),
                "tournament_code": t.tournament_code,
                "title": t.title,
                "entry_fee": str(t.entry_fee),
                "player_count": t.player_count,
                "completed_at": t.completed_at.isoformat() if t.completed_at else None,
            }
            for t in subset
        ]
        set_cache(key, data)


# ── Job: Active user metrics ──────────────────────────────────────────────────

async def run_active_user_metrics(db: AsyncSession) -> int:
    """
    Aggregate user activity windows and store in cache.

    Windows:
      online         — last_seen_at within last 3 minutes
      active_today   — last_seen_at on today's IST calendar date
      active_24h     — last_seen_at within the rolling 24 hours

    Stores result under cache key "metrics:active_users".

    Returns 1 on success, 0 on error.
    """
    try:
        now = datetime.now(timezone.utc)

        # Thresholds
        online_cutoff = now - timedelta(minutes=3)
        h24_cutoff = now - timedelta(hours=24)

        # IST today boundaries
        now_ist = datetime.now(_IST)
        ist_today_start = now_ist.replace(hour=0, minute=0, second=0, microsecond=0)
        # Convert back to UTC for the DB query
        ist_today_start_utc = ist_today_start.astimezone(timezone.utc)

        online_result = await db.execute(
            select(func.count(User.id)).where(
                User.last_seen_at >= online_cutoff,
                User.is_active == True,  # noqa: E712
            )
        )
        online = online_result.scalar_one() or 0

        today_result = await db.execute(
            select(func.count(User.id)).where(
                User.last_seen_at >= ist_today_start_utc,
                User.is_active == True,  # noqa: E712
            )
        )
        active_today = today_result.scalar_one() or 0

        h24_result = await db.execute(
            select(func.count(User.id)).where(
                User.last_seen_at >= h24_cutoff,
                User.is_active == True,  # noqa: E712
            )
        )
        active_24h = h24_result.scalar_one() or 0

        total_result = await db.execute(
            select(func.count(User.id)).where(User.is_active == True)  # noqa: E712
        )
        total_registered = total_result.scalar_one() or 0

        metrics = {
            "online": online,
            "active_today_ist": active_today,
            "active_last_24h": active_24h,
            "total_registered": total_registered,
            "computed_at": now.isoformat(),
        }
        # Cache for 2 minutes (short TTL — this is near-realtime data)
        set_cache("metrics:active_users", metrics, ttl_seconds=120)

        logger.info(
            "Active user metrics: online=%d today=%d 24h=%d total=%d",
            online, active_today, active_24h, total_registered,
        )
        return 1
    except Exception:
        logger.exception("Active user metrics job failed.")
        return 0
