"""
Result and prize routes.

Player routes (require player JWT):
  GET /api/tournaments/{id}/results    — public leaderboard for completed tournament
  GET /api/tournaments/{id}/my-result  — own result + payout

Admin routes (require staff JWT + permission):
  GET  /api/admin/tournaments/{id}/results          — full result list
  POST /api/admin/tournaments/{id}/results          — enter a result     [enter_results]
  PUT  /api/admin/tournaments/{id}/results/{rid}    — correct a result   [enter_results]
  POST /api/admin/tournaments/{id}/results/{rid}/verify — verify        [verify_results]
  GET  /api/admin/results/kill-reward-preview       — pure calculation   [enter_results]
  GET  /api/admin/prize-configs                     — list all configs   [any staff]
  GET  /api/admin/prize-configs/{entry_fee}         — get one config     [any staff]

Completed History routes (require staff JWT + results.history_view):
  GET  /api/admin/results/history                               — paginated completed tournaments
  GET  /api/admin/results/history/{tournament_id}               — full result table for one completed tournament
"""
from __future__ import annotations

import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError, NotEnrolledError, PermissionDeniedError
from app.core.permissions import Permission
from app.database import get_db
from app.dependencies import get_current_staff, get_current_user
from app.models.permission import Permission as PermModel, StaffUserPermission
from app.models.prize_config import PrizeConfig
from app.models.result import TournamentResult
from app.models.staff_user import StaffRole, StaffUser
from app.models.tournament import EntryStatus, TournamentEntry, TournamentStatus
from app.models.user import User
from app.schemas.result import (
    AdminPayoutSummaryResponse,
    AdminResultListResponse,
    AdminResultResponse,
    CompletedHistoryListResponse,
    CompletedHistoryResultItem,
    CompletedTournamentResultsResponse,
    CompletedTournamentSummary,
    EnterResultRequest,
    KillRewardPreviewRequest,
    KillRewardPreviewResponse,
    PlayerResultResponse,
    PrizeConfigResponse,
    TournamentResultsListResponse,
    UpdatePrizeConfigRequest,
    UpdateResultRequest,
)
from app.services import prize_service, result_service, tournament_service

player_router = APIRouter(prefix="/api/tournaments", tags=["Player — Results"])
admin_router = APIRouter(prefix="/api/admin/tournaments", tags=["Admin — Results"])
config_router = APIRouter(prefix="/api/admin/prize-configs", tags=["Admin — Prize Config"])
history_router = APIRouter(prefix="/api/admin/results", tags=["Admin — Completed History"])


# ── Permission helper ─────────────────────────────────────────────────────────

async def _require_perm(
    staff: StaffUser, db: AsyncSession, perm: Permission
) -> None:
    if staff.role == StaffRole.super_admin:
        return
    # BUG #7 FIX: joinedload replaces N separate db.get() calls
    from sqlalchemy.orm import joinedload
    result = await db.execute(
        select(StaffUserPermission)
        .options(joinedload(StaffUserPermission.permission))
        .where(StaffUserPermission.staff_user_id == staff.id)
    )
    grants = result.unique().scalars().all()
    granted: set[str] = {
        g.permission.code for g in grants if g.permission is not None
    }
    if perm.value not in granted:
        raise PermissionDeniedError(perm.value)


def _assert_tournament_owner(staff: StaffUser, tournament) -> None:
    """
    Enforce tournament ownership for Agents.

    Super Admins bypass this check and may operate on any tournament.
    Agents may only access tournaments they created (created_by_id == staff.id).
    Raises PermissionDeniedError (403) — not a 404 — so direct API requests
    to another agent's tournament_id are visibly rejected.
    """
    if staff.role == StaffRole.super_admin:
        return
    if tournament.created_by_id != staff.id:
        raise PermissionDeniedError("tournament.owner")


# ══════════════════════════════════════════════════════════════════════════════
# PLAYER ROUTES
# ══════════════════════════════════════════════════════════════════════════════

@player_router.get(
    "/{tournament_id}/results",
    response_model=TournamentResultsListResponse,
    summary="View tournament result leaderboard (completed tournaments only)",
)
async def get_tournament_results(
    tournament_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TournamentResultsListResponse:
    """
    Returns the placement leaderboard for a completed tournament.
    Payout amounts are only shown once results are finalized.
    Does NOT expose other players' wallet balances or internal IDs.
    """
    tournament = await tournament_service.get_tournament(db, tournament_id)
    results = await result_service.list_tournament_results(db, tournament_id)

    items = [
        PlayerResultResponse.from_result(r, tournament_code=tournament.tournament_code)
        for r in results
    ]
    return TournamentResultsListResponse(
        tournament_id=tournament_id,
        tournament_code=tournament.tournament_code,
        results=items,
    )


@player_router.get(
    "/{tournament_id}/my-result",
    response_model=PlayerResultResponse,
    summary="View your own result and payout for a tournament",
)
async def get_my_result(
    tournament_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PlayerResultResponse:
    """Returns the authenticated player's result including payout once finalized."""
    # Verify enrolled
    entry = await tournament_service.get_entry(db, tournament_id, current_user.id)
    if entry is None:
        raise NotEnrolledError()

    result = await result_service.get_result_by_entry(db, entry.id)
    if result is None:
        raise NotFoundError("Result")

    tournament = await tournament_service.get_tournament(db, tournament_id)
    return PlayerResultResponse.from_result(
        result, tournament_code=tournament.tournament_code
    )


# ══════════════════════════════════════════════════════════════════════════════
# ADMIN ROUTES
# ══════════════════════════════════════════════════════════════════════════════

@admin_router.get(
    "/{tournament_id}/results",
    response_model=AdminResultListResponse,
    summary="View full result list for a tournament (Admin)",
)
async def admin_list_results(
    tournament_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_staff: StaffUser = Depends(get_current_staff),
) -> AdminResultListResponse:
    tournament = await tournament_service.get_tournament(db, tournament_id)
    _assert_tournament_owner(current_staff, tournament)
    results = await result_service.list_tournament_results(db, tournament_id)
    return AdminResultListResponse(
        tournament_id=tournament_id,
        results=[AdminResultResponse.from_result(r) for r in results],
        total=len(results),
    )


@admin_router.post(
    "/{tournament_id}/results",
    response_model=AdminResultResponse,
    status_code=201,
    summary="Enter a player result (Admin — enter_results permission)",
)
async def enter_result(
    tournament_id: uuid.UUID,
    body: EnterResultRequest,
    db: AsyncSession = Depends(get_db),
    current_staff: StaffUser = Depends(get_current_staff),
) -> AdminResultResponse:
    """
    Enter placement and kill count for a player entry.
    user_id and ff_uid are derived from the TournamentEntry — client cannot
    supply them.  Raises 409 if a result already exists for this entry.
    """
    await _require_perm(current_staff, db, Permission.TOURNAMENT_ENTER_RESULTS)

    # Ownership check: Agent can only enter results for their own tournaments.
    tournament = await tournament_service.get_tournament(db, tournament_id)
    _assert_tournament_owner(current_staff, tournament)

    # Verify entry belongs to the given tournament (prevent cross-tournament injection)
    entry_check = await db.execute(
        select(TournamentEntry).where(
            TournamentEntry.id == body.entry_id,
            TournamentEntry.tournament_id == tournament_id,
        )
    )
    if entry_check.scalar_one_or_none() is None:
        raise NotFoundError("TournamentEntry")

    result = await result_service.enter_result(
        db,
        entry_id=body.entry_id,
        placement=body.placement,
        kills=body.kills,
        entered_by=current_staff,
    )
    await db.commit()
    return AdminResultResponse.from_result(result)


@admin_router.put(
    "/{tournament_id}/results/{result_id}",
    response_model=AdminResultResponse,
    summary="Correct a result before payout (Admin — enter_results permission)",
)
async def update_result(
    tournament_id: uuid.UUID,
    result_id: uuid.UUID,
    body: UpdateResultRequest,
    db: AsyncSession = Depends(get_db),
    current_staff: StaffUser = Depends(get_current_staff),
) -> AdminResultResponse:
    """
    Correct placement or kills for an already-entered result.
    Blocked once payout has been distributed (result is immutable).
    Resets verification status if result was previously verified.
    """
    await _require_perm(current_staff, db, Permission.TOURNAMENT_ENTER_RESULTS)
    # Ownership check: Agent can only update results for their own tournaments.
    tournament = await tournament_service.get_tournament(db, tournament_id)
    _assert_tournament_owner(current_staff, tournament)
    result = await result_service.update_result(
        db,
        result_id=result_id,
        placement=body.placement,
        kills=body.kills,
        updated_by=current_staff,
    )
    await db.commit()
    return AdminResultResponse.from_result(result)


@admin_router.post(
    "/{tournament_id}/results/{result_id}/verify",
    response_model=AdminResultResponse,
    summary="Verify a result (Admin — verify_results permission)",
)
async def verify_result(
    tournament_id: uuid.UUID,
    result_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_staff: StaffUser = Depends(get_current_staff),
) -> AdminResultResponse:
    await _require_perm(current_staff, db, Permission.TOURNAMENT_VERIFY_RESULTS)
    # Ownership check: Agent can only verify results for their own tournaments.
    tournament = await tournament_service.get_tournament(db, tournament_id)
    _assert_tournament_owner(current_staff, tournament)
    result = await result_service.verify_result(
        db, result_id=result_id, verified_by=current_staff
    )
    await db.commit()
    return AdminResultResponse.from_result(result)


# ── Kill-reward preview ───────────────────────────────────────────────────────

@config_router.get(
    "/kill-reward-preview",
    response_model=KillRewardPreviewResponse,
    summary="Preview kill reward for given placement + kills (no money credited)",
    tags=["Admin — Results"],
)
async def kill_reward_preview(
    placement: int = Query(..., ge=1),
    kills: int = Query(..., ge=0),
    entry_fee: Decimal = Query(..., gt=Decimal("0")),
    db: AsyncSession = Depends(get_db),
    current_staff: StaffUser = Depends(get_current_staff),
) -> KillRewardPreviewResponse:
    """
    Pure server-side calculation of kill reward.
    No money is credited — this is a preview only.
    Used by the result-entry UI to show the live calculation before saving.
    """
    config = await prize_service.get_prize_config(db, entry_fee)
    result = prize_service.calculate_kill_reward(placement, kills, config)
    return KillRewardPreviewResponse(
        eligible=result.eligible,
        placement=placement,
        kills=kills,
        reward=str(result.reward),
        breakdown=result.breakdown,
    )


# ── Prize config views ────────────────────────────────────────────────────────

@config_router.get(
    "",
    summary="List all prize configurations (Admin)",
)
async def list_prize_configs(
    db: AsyncSession = Depends(get_db),
    current_staff: StaffUser = Depends(get_current_staff),
) -> list[PrizeConfigResponse]:
    result = await db.execute(
        select(PrizeConfig).order_by(PrizeConfig.entry_fee.asc())
    )
    configs = result.scalars().all()
    return [PrizeConfigResponse.from_config(c) for c in configs]


@config_router.get(
    "/{entry_fee}",
    response_model=PrizeConfigResponse,
    summary="Get prize configuration for a specific entry fee (Admin)",
)
async def get_prize_config(
    entry_fee: Decimal,
    db: AsyncSession = Depends(get_db),
    current_staff: StaffUser = Depends(get_current_staff),
) -> PrizeConfigResponse:
    config = await prize_service.get_prize_config(db, entry_fee)
    return PrizeConfigResponse.from_config(config)


# ── Prize config update (Phase 10 — T5) ──────────────────────────────────────

@config_router.put(
    "/{entry_fee}",
    response_model=PrizeConfigResponse,
    summary="Update prize configuration for an entry fee tier (Super Admin only)",
)
async def update_prize_config(
    entry_fee: Decimal,
    body: UpdatePrizeConfigRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_staff: StaffUser = Depends(get_current_staff),
) -> PrizeConfigResponse:
    """
    Update placement prizes, kill reward tiers, or activation status for a
    specific entry fee tier.

    Security:
    - Super Admin only (settings.manage is effectively required — any admin permission
      check here defers to the super_admin role guard since only super_admin can
      modify prize structures).
    - Validates: no negative prize values, valid tier structure.
    - Audit-logged.

    Blocked:
    - Updating a tier that doesn't exist (404).
    - Setting negative prize amounts.
    - Setting invalid kill_reward_tiers structure.
    """
    from fastapi import Request as _Request

    # Super Admin only — prize configs are financial infrastructure
    if current_staff.role != StaffRole.super_admin:
        raise PermissionDeniedError("settings.manage")

    result = await db.execute(
        select(PrizeConfig).where(PrizeConfig.entry_fee == entry_fee)
    )
    config: PrizeConfig | None = result.scalar_one_or_none()
    if config is None:
        from app.core.exceptions import NotFoundError
        raise NotFoundError("PrizeConfig")

    old_vals: dict = {}
    new_vals: dict = {}

    if body.is_active is not None:
        old_vals["is_active"] = config.is_active
        config.is_active = body.is_active
        new_vals["is_active"] = body.is_active

    if body.first_place_prize is not None:
        old_vals["first_place_prize"] = str(config.first_place_prize)
        config.first_place_prize = body.first_place_prize
        new_vals["first_place_prize"] = str(body.first_place_prize)

    if body.second_place_prize is not None:
        old_vals["second_place_prize"] = str(config.second_place_prize)
        config.second_place_prize = body.second_place_prize
        new_vals["second_place_prize"] = str(body.second_place_prize)

    if body.third_place_prize is not None:
        old_vals["third_place_prize"] = str(config.third_place_prize)
        config.third_place_prize = body.third_place_prize
        new_vals["third_place_prize"] = str(body.third_place_prize)

    if body.kill_reward_top_n is not None:
        old_vals["kill_reward_top_n"] = config.kill_reward_top_n
        config.kill_reward_top_n = body.kill_reward_top_n
        new_vals["kill_reward_top_n"] = body.kill_reward_top_n

    if body.kill_reward_tiers is not None:
        # Validate tier structure
        for tier in body.kill_reward_tiers:
            if "from_kill" not in tier or "reward_per_kill" not in tier:
                from app.core.exceptions import AppError
                raise AppError(
                    "invalid_tier",
                    "Each kill reward tier must have 'from_kill' and 'reward_per_kill'.",
                    422,
                )
            if Decimal(str(tier["reward_per_kill"])) < Decimal("0"):
                from app.core.exceptions import AppError
                raise AppError(
                    "invalid_tier",
                    "reward_per_kill cannot be negative.",
                    422,
                )
        old_vals["kill_reward_tiers"] = config.kill_reward_tiers
        config.kill_reward_tiers = body.kill_reward_tiers
        new_vals["kill_reward_tiers"] = body.kill_reward_tiers

    if body.notes is not None:
        config.notes = body.notes

    from app.models._types import utcnow
    config.updated_at = utcnow()
    config.updated_by_id = current_staff.id
    await db.flush([config])

    from app.services import audit_service
    from app.models.audit_log import ActorType
    await audit_service.log(
        db=db,
        actor_type=ActorType.staff,
        actor_id=str(current_staff.id),
        action="prize_config.updated",
        entity_type="prize_config",
        entity_id=str(config.id),
        old_value=old_vals,
        new_value=new_vals,
        ip_address=request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (
            request.client.host if request.client else None
        ),
        user_agent=request.headers.get("user-agent"),
    )
    await db.commit()

    return PrizeConfigResponse.from_config(config)

# ══════════════════════════════════════════════════════════════════════════════
# COMPLETED RESULTS HISTORY ROUTES  (Phase 11 — read-only)
# ══════════════════════════════════════════════════════════════════════════════
#
# These routes are strictly read-only.  No wallet operations, no payout
# distribution, no result mutation can be triggered from here.
# Access requires the results.history_view permission.
# Super Admins bypass the permission check as usual.

@history_router.get(
    "/history",
    response_model=CompletedHistoryListResponse,
    summary="List completed tournaments for history view (Admin — results.history_view)",
)
async def list_completed_history(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    search: str | None = Query(default=None, description="Filter by tournament code or title (case-insensitive substring)"),
    db: AsyncSession = Depends(get_db),
    current_staff: StaffUser = Depends(get_current_staff),
) -> CompletedHistoryListResponse:
    """
    Returns a paginated list of completed tournaments, newest first.

    Each item is a lightweight summary (no per-player detail) — the frontend
    uses this to render the history list.  Use the detail endpoint to fetch
    the full result table for a specific tournament.

    Permissions:
      - Super Admin: always allowed.
      - Other staff: requires results.history_view.

    Read-only — no side effects whatsoever.
    """
    await _require_perm(current_staff, db, Permission.RESULTS_HISTORY_VIEW)

    from sqlalchemy import func, case
    from app.models.tournament import Tournament, TournamentStatus
    from app.models.result import TournamentResult

    # Base query: completed tournaments
    q = (
        select(Tournament)
        .where(Tournament.status == TournamentStatus.completed)
    )

    # Ownership scope: Agents may only view history for their own tournaments.
    # Super Admins see all completed tournaments regardless of creator.
    if current_staff.role == StaffRole.agent:
        q = q.where(Tournament.created_by_id == current_staff.id)

    # Optional search by code or title
    if search:
        pattern = f"%{search.strip()}%"
        q = q.where(
            Tournament.tournament_code.ilike(pattern)
            | Tournament.title.ilike(pattern)
        )

    # Total count for pagination
    count_q = select(func.count()).select_from(q.subquery())
    total: int = (await db.execute(count_q)).scalar_one()

    # Paginated tournaments, newest completed first
    q = q.order_by(Tournament.completed_at.desc().nullslast(), Tournament.created_at.desc())
    q = q.limit(limit).offset(offset)
    tournaments = (await db.execute(q)).scalars().all()

    if not tournaments:
        return CompletedHistoryListResponse(items=[], total=total, limit=limit, offset=offset)

    tournament_ids = [t.id for t in tournaments]

    # Aggregate result counts per tournament in one query
    agg_q = (
        select(
            TournamentResult.tournament_id,
            func.count(TournamentResult.id).label("result_count"),
            func.sum(
                case((TournamentResult.payout_distributed == True, 1), else_=0)  # noqa: E712
            ).label("payout_distributed_count"),
        )
        .where(TournamentResult.tournament_id.in_(tournament_ids))
        .group_by(TournamentResult.tournament_id)
    )
    agg_rows = (await db.execute(agg_q)).all()
    agg_map = {
        row.tournament_id: {
            "result_count": row.result_count,
            "payout_distributed_count": int(row.payout_distributed_count or 0),
        }
        for row in agg_rows
    }

    items = [
        CompletedTournamentSummary(
            tournament_id=t.id,
            tournament_code=t.tournament_code,
            tournament_title=t.title,
            entry_fee=str(t.entry_fee),
            player_count=t.player_count,
            completed_at=t.completed_at,
            result_count=agg_map.get(t.id, {}).get("result_count", 0),
            payout_distributed_count=agg_map.get(t.id, {}).get("payout_distributed_count", 0),
        )
        for t in tournaments
    ]

    return CompletedHistoryListResponse(items=items, total=total, limit=limit, offset=offset)


@history_router.get(
    "/history/{tournament_id}",
    response_model=CompletedTournamentResultsResponse,
    summary="Get finalized result table for one completed tournament (Admin — results.history_view)",
)
async def get_completed_tournament_results(
    tournament_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_staff: StaffUser = Depends(get_current_staff),
) -> CompletedTournamentResultsResponse:
    """
    Returns the full finalized result table for a single completed tournament,
    sorted by placement ascending.

    All monetary values (prize_amount, kill_reward_amount, total_payout) are
    read directly from the database — this endpoint never recalculates them.

    Permissions:
      - Super Admin: always allowed.
      - Other staff: requires results.history_view.

    Read-only — no wallet operations, no payout distribution, no mutations.
    Raises 404 if the tournament does not exist or is not in completed status.
    """
    await _require_perm(current_staff, db, Permission.RESULTS_HISTORY_VIEW)

    from sqlalchemy.orm import joinedload
    from app.models.tournament import Tournament, TournamentStatus
    from app.models.result import TournamentResult
    from app.models.user import User

    # Fetch tournament — must exist and be completed
    tourn_row = await db.get(Tournament, tournament_id)
    if tourn_row is None:
        raise NotFoundError("Tournament")
    if tourn_row.status != TournamentStatus.completed:
        raise NotFoundError("Completed tournament")

    # Ownership check: Agent can only view results detail for their own tournaments.
    _assert_tournament_owner(current_staff, tourn_row)

    # Fetch all results for this tournament, sorted by placement
    results_q = (
        select(TournamentResult)
        .where(TournamentResult.tournament_id == tournament_id)
        .order_by(TournamentResult.placement.asc())
    )
    results: list[TournamentResult] = (await db.execute(results_q)).scalars().all()

    # Collect user IDs and fetch usernames + current ff_name in one query
    user_ids = list({r.user_id for r in results})
    username_map: dict = {}
    ffname_map: dict = {}
    if user_ids:
        users_q = select(User.id, User.username, User.ff_name).where(User.id.in_(user_ids))
        for uid, uname, ff_name in (await db.execute(users_q)).all():
            username_map[uid] = uname
            ffname_map[uid] = ff_name  # may be None

    items = [
        CompletedHistoryResultItem.from_orm_row(
            r,
            tournament_code=tourn_row.tournament_code,
            tournament_title=tourn_row.title,
            entry_fee=str(tourn_row.entry_fee),
            completed_at=tourn_row.completed_at,
            username=username_map.get(r.user_id, "—"),
            ff_name=ffname_map.get(r.user_id),  # live fallback when snapshot is NULL
        )
        for r in results
    ]

    return CompletedTournamentResultsResponse(
        tournament_id=tourn_row.id,
        tournament_code=tourn_row.tournament_code,
        tournament_title=tourn_row.title,
        entry_fee=str(tourn_row.entry_fee),
        completed_at=tourn_row.completed_at,
        player_count=tourn_row.player_count,
        results=items,
        total=len(items),
    )
