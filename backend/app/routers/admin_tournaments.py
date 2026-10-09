"""
Admin/Agent tournament management routes.

  POST /api/admin/tournaments                      — create tournament
  GET  /api/admin/tournaments                      — list all tournaments
  GET  /api/admin/tournaments/{id}                 — full tournament detail (incl. room creds)
  POST /api/admin/tournaments/{id}/open            — DRAFT → OPEN
  POST /api/admin/tournaments/{id}/confirm         — OPEN → CONFIRMED
  POST /api/admin/tournaments/{id}/release-room    — CONFIRMED → ROOM_RELEASED
  POST /api/admin/tournaments/{id}/go-live         — ROOM_RELEASED → LIVE
  POST /api/admin/tournaments/{id}/enter-results   — LIVE → RESULT_ENTRY
  POST /api/admin/tournaments/{id}/complete        — RESULT_ENTRY → COMPLETED
  POST /api/admin/tournaments/{id}/cancel          — OPEN|CONFIRMED → CANCELLED
  GET  /api/admin/tournaments/{id}/entries         — enrolled players + sheet numbers

Permission model follows the same inline _require_permission pattern used
in admin_wallet.py — consistent with the project convention.

Creator is always taken from the authenticated staff token — never from the
request body.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError, PermissionDeniedError
from app.core.permissions import Permission
from app.database import get_db
from app.dependencies import get_current_staff
from app.models.permission import Permission as PermModel, StaffUserPermission
from app.models.staff_user import StaffRole, StaffUser
from app.models.tournament import EntryStatus, TournamentEntry, TournamentStatus
from app.models.user import User
from app.schemas.tournament import (
    AdminEntryListResponse,
    AdminEntryResponse,
    AdminTournamentDetail,
    AdminTournamentListResponse,
    AdminTournamentSummary,
    CancelTournamentRequest,
    CreateTournamentRequest,
    ReleaseRoomRequest,
    TournamentActionResponse,
)
from app.services import tournament_service

router = APIRouter(prefix="/api/admin/tournaments", tags=["Admin — Tournaments"])


# ── Inline permission helper (matches admin_wallet.py convention) ─────────────

async def _require_perm(
    staff: StaffUser,
    db: AsyncSession,
    perm: Permission,
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

    Super Admins bypass this check and may access any tournament.
    Agents may only access tournaments they created (created_by_id == staff.id).
    Raises PermissionDeniedError (403) so direct API requests to another
    agent's tournament_id are rejected the same way the UI would be.
    We use 403 (not 404) intentionally — this is an authorisation boundary,
    not a missing resource.  Leaking that the tournament *exists* is
    acceptable here since the tournament code is semi-public.
    """
    if staff.role == StaffRole.super_admin:
        return  # Super Admin is unrestricted
    if tournament.created_by_id != staff.id:
        raise PermissionDeniedError("tournament.owner")


# ── Create tournament ─────────────────────────────────────────────────────────

@router.post(
    "",
    response_model=AdminTournamentDetail,
    status_code=201,
    summary="Create a new tournament (Admin/Agent)",
)
async def create_tournament(
    body: CreateTournamentRequest,
    db: AsyncSession = Depends(get_db),
    current_staff: StaffUser = Depends(get_current_staff),
) -> AdminTournamentDetail:
    """
    Creates the tournament in DRAFT status.
    Creator is the authenticated staff member — not overridable from the body.
    """
    await _require_perm(current_staff, db, Permission.TOURNAMENT_CREATE)

    tournament = await tournament_service.create_tournament(
        db,
        title=body.title,
        entry_fee=body.entry_fee,
        scheduled_at=body.scheduled_at,
        created_by=current_staff,
        game=body.game,
        mode=body.mode,
        max_players=body.max_players,
        min_players=body.min_players,
    )
    await db.commit()
    tournament = await tournament_service.get_tournament(db, tournament.id)
    return AdminTournamentDetail.from_tournament(tournament)


# ── List tournaments ──────────────────────────────────────────────────────────

@router.get(
    "",
    response_model=AdminTournamentListResponse,
    summary="List all tournaments (Admin)",
)
async def list_tournaments(
    db: AsyncSession = Depends(get_db),
    current_staff: StaffUser = Depends(get_current_staff),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    status: str | None = Query(default=None, description="Filter by status value"),
    entry_fee: str | None = Query(default=None, description="Filter by entry fee (e.g. '50')"),
) -> AdminTournamentListResponse:
    statuses = None
    if status:
        try:
            statuses = [TournamentStatus(status)]
        except ValueError:
            statuses = None

    # BUG #5 FIX: parse and pass entry_fee filter so pagination totals and
    # page results reflect the filtered set rather than the full table.
    from decimal import Decimal, InvalidOperation
    fee_filter = None
    if entry_fee:
        try:
            fee_filter = Decimal(entry_fee)
        except InvalidOperation:
            fee_filter = None

    # Ownership scope: Agents may only see their own tournaments.
    # Super Admins see all tournaments regardless of creator.
    created_by_filter = current_staff.id if current_staff.role == StaffRole.agent else None

    tournaments, total = await tournament_service.list_tournaments(
        db, statuses=statuses, entry_fee=fee_filter, limit=limit, offset=offset,
        created_by_id=created_by_filter,
    )
    return AdminTournamentListResponse(
        items=[AdminTournamentSummary.from_tournament(t) for t in tournaments],
        total=total,
        limit=limit,
        offset=offset,
    )

# ── Get tournament detail ─────────────────────────────────────────────────────

@router.get(
    "/{tournament_id}",
    response_model=AdminTournamentDetail,
    summary="Get full tournament detail including room credentials (Admin)",
)
async def get_tournament(
    tournament_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_staff: StaffUser = Depends(get_current_staff),
) -> AdminTournamentDetail:
    tournament = await tournament_service.get_tournament(db, tournament_id)
    _assert_tournament_owner(current_staff, tournament)
    return AdminTournamentDetail.from_tournament(tournament)


# ── Open ──────────────────────────────────────────────────────────────────────

@router.post(
    "/{tournament_id}/open",
    response_model=TournamentActionResponse,
    summary="Open tournament for registration (DRAFT → OPEN)",
)
async def open_tournament(
    tournament_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_staff: StaffUser = Depends(get_current_staff),
) -> TournamentActionResponse:
    await _require_perm(current_staff, db, Permission.TOURNAMENT_CREATE)
    tournament = await tournament_service.get_tournament(db, tournament_id)
    _assert_tournament_owner(current_staff, tournament)
    tournament = await tournament_service.transition_status(
        db, tournament_id,
        target_status=TournamentStatus.open,
        actor=current_staff,
    )
    await db.commit()
    return TournamentActionResponse(
        message="Tournament is now open for registration.",
        tournament_id=tournament.id,
        tournament_code=tournament.tournament_code,
        status=tournament.status.value,
    )


# ── Confirm ───────────────────────────────────────────────────────────────────

@router.post(
    "/{tournament_id}/confirm",
    response_model=TournamentActionResponse,
    summary="Close registration and confirm (OPEN → CONFIRMED)",
)
async def confirm_tournament(
    tournament_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_staff: StaffUser = Depends(get_current_staff),
) -> TournamentActionResponse:
    await _require_perm(current_staff, db, Permission.TOURNAMENT_CREATE)
    tournament = await tournament_service.get_tournament(db, tournament_id)
    _assert_tournament_owner(current_staff, tournament)
    tournament = await tournament_service.transition_status(
        db, tournament_id,
        target_status=TournamentStatus.confirmed,
        actor=current_staff,
    )
    await db.commit()
    return TournamentActionResponse(
        message="Tournament confirmed. Registration is now closed.",
        tournament_id=tournament.id,
        tournament_code=tournament.tournament_code,
        status=tournament.status.value,
    )


# ── Release room ──────────────────────────────────────────────────────────────

@router.post(
    "/{tournament_id}/release-room",
    response_model=TournamentActionResponse,
    summary="Release room ID and password to enrolled players (CONFIRMED → ROOM_RELEASED)",
)
async def release_room(
    tournament_id: uuid.UUID,
    body: ReleaseRoomRequest,
    db: AsyncSession = Depends(get_db),
    current_staff: StaffUser = Depends(get_current_staff),
) -> TournamentActionResponse:
    await _require_perm(current_staff, db, Permission.TOURNAMENT_RELEASE_ROOM)
    pre = await tournament_service.get_tournament(db, tournament_id)
    _assert_tournament_owner(current_staff, pre)
    tournament = await tournament_service.release_room(
        db, tournament_id,
        room_id=body.room_id,
        room_password=body.room_password,
        actor=current_staff,
    )
    await db.commit()
    return TournamentActionResponse(
        message="Room credentials released to enrolled players.",
        tournament_id=tournament.id,
        tournament_code=tournament.tournament_code,
        status=tournament.status.value,
    )


# ── Go live ───────────────────────────────────────────────────────────────────

@router.post(
    "/{tournament_id}/go-live",
    response_model=TournamentActionResponse,
    summary="Mark tournament as LIVE (ROOM_RELEASED → LIVE)",
)
async def go_live(
    tournament_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_staff: StaffUser = Depends(get_current_staff),
) -> TournamentActionResponse:
    """
    Enforces the minimum player requirement before going LIVE.
    Uses attempt_scheduled_transition() which validates player count >= min_players.
    Can also be triggered by the scheduler without going through this endpoint.
    """
    await _require_perm(current_staff, db, Permission.TOURNAMENT_MANAGE_PLAYERS)
    pre = await tournament_service.get_tournament(db, tournament_id)
    _assert_tournament_owner(current_staff, pre)
    tournament = await tournament_service.attempt_scheduled_transition(
        db, tournament_id
    )
    await db.commit()
    return TournamentActionResponse(
        message="Tournament is now LIVE.",
        tournament_id=tournament.id,
        tournament_code=tournament.tournament_code,
        status=tournament.status.value,
    )


# ── Enter results ─────────────────────────────────────────────────────────────

@router.post(
    "/{tournament_id}/enter-results",
    response_model=TournamentActionResponse,
    summary="Move to result entry phase (LIVE → RESULT_ENTRY)",
)
async def enter_results_phase(
    tournament_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_staff: StaffUser = Depends(get_current_staff),
) -> TournamentActionResponse:
    await _require_perm(current_staff, db, Permission.TOURNAMENT_ENTER_RESULTS)
    pre = await tournament_service.get_tournament(db, tournament_id)
    _assert_tournament_owner(current_staff, pre)
    tournament = await tournament_service.transition_status(
        db, tournament_id,
        target_status=TournamentStatus.result_entry,
        actor=current_staff,
    )
    await db.commit()
    return TournamentActionResponse(
        message="Tournament moved to result entry phase.",
        tournament_id=tournament.id,
        tournament_code=tournament.tournament_code,
        status=tournament.status.value,
    )


# ── Complete ──────────────────────────────────────────────────────────────────

@router.post(
    "/{tournament_id}/complete",
    response_model=TournamentActionResponse,
    summary="Complete tournament — triggers referral rewards (RESULT_ENTRY → COMPLETED)",
)
async def complete_tournament(
    tournament_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_staff: StaffUser = Depends(get_current_staff),
) -> TournamentActionResponse:
    """
    Marks COMPLETED. Side effects in the same transaction:
      - Sets completed_at / completed_by_id.
      - Fires mark_first_tournament_completed() for every active entrant —
        credits ₹20 referral bonus to referred users and referrers atomically.
    """
    await _require_perm(current_staff, db, Permission.TOURNAMENT_VERIFY_RESULTS)
    pre = await tournament_service.get_tournament(db, tournament_id)
    _assert_tournament_owner(current_staff, pre)
    tournament = await tournament_service.transition_status(
        db, tournament_id,
        target_status=TournamentStatus.completed,
        actor=current_staff,
    )
    await db.commit()
    return TournamentActionResponse(
        message="Tournament completed. Results finalised.",
        tournament_id=tournament.id,
        tournament_code=tournament.tournament_code,
        status=tournament.status.value,
    )


# ── Cancel ────────────────────────────────────────────────────────────────────

@router.post(
    "/{tournament_id}/cancel",
    response_model=TournamentActionResponse,
    summary="Cancel tournament and atomically refund all entries (OPEN|CONFIRMED|ROOM_RELEASED → CANCELLED)",
)
async def cancel_tournament(
    tournament_id: uuid.UUID,
    body: CancelTournamentRequest,
    db: AsyncSession = Depends(get_db),
    current_staff: StaffUser = Depends(get_current_staff),
) -> TournamentActionResponse:
    """
    Cancels the tournament and atomically refunds every active entry.
    Permitted from OPEN, CONFIRMED, or ROOM_RELEASED status (pre-LIVE only).
    Each player's deposit_amount_used → deposit_balance.
    Each player's referral_amount_used → referral_balance.
    All refunds use deterministic idempotency keys; safe to retry.
    """
    await _require_perm(current_staff, db, Permission.TOURNAMENT_CANCEL)
    pre = await tournament_service.get_tournament(db, tournament_id)
    _assert_tournament_owner(current_staff, pre)
    tournament = await tournament_service.cancel_tournament(
        db, tournament_id,
        actor=current_staff,
        reason=body.reason,
    )
    await db.commit()
    return TournamentActionResponse(
        message="Tournament cancelled. All entries have been refunded.",
        tournament_id=tournament.id,
        tournament_code=tournament.tournament_code,
        status=tournament.status.value,
    )


# ── Enrolled players ──────────────────────────────────────────────────────────

@router.get(
    "/{tournament_id}/entries",
    response_model=AdminEntryListResponse,
    summary="View all enrolled players and payment details (Admin)",
)
async def list_entries(
    tournament_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_staff: StaffUser = Depends(get_current_staff),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    active_only: bool = Query(
        default=False,
        description="Return only ACTIVE entries when true. Default returns all.",
    ),
) -> AdminEntryListResponse:
    await _require_perm(current_staff, db, Permission.USER_VIEW)
    tournament = await tournament_service.get_tournament(db, tournament_id)
    _assert_tournament_owner(current_staff, tournament)

    base_q = select(TournamentEntry).where(
        TournamentEntry.tournament_id == tournament_id
    )
    if active_only:
        base_q = base_q.where(TournamentEntry.status == EntryStatus.active)

    count_result = await db.execute(
        select(func.count()).select_from(base_q.subquery())
    )
    total = count_result.scalar_one()

    result = await db.execute(
        base_q.order_by(TournamentEntry.sheet_number.asc())
        .limit(limit)
        .offset(offset)
    )
    entries = list(result.scalars().all())

    if not entries:
        return AdminEntryListResponse(
            items=[], total=total, limit=limit, offset=offset
        )

    user_ids = [e.user_id for e in entries]
    users_result = await db.execute(
        select(User.id, User.username, User.ff_uid, User.ff_name).where(User.id.in_(user_ids))
    )
    user_map: dict[uuid.UUID, tuple[str, str, str | None]] = {
        row.id: (row.username, row.ff_uid, row.ff_name) for row in users_result
    }

    items = [
        AdminEntryResponse(
            entry_id=entry.id,
            user_id=entry.user_id,
            username=user_map.get(entry.user_id, ("unknown", "", None))[0],
            ff_uid=user_map.get(entry.user_id, ("", "unknown", None))[1],
            ff_name=user_map.get(entry.user_id, ("", "", None))[2],
            sheet_number=entry.sheet_number,
            entry_fee=str(entry.entry_fee_snapshot),
            deposit_used=str(entry.deposit_amount_used),
            referral_used=str(entry.referral_amount_used),
            total_paid=str(
                entry.deposit_amount_used + entry.referral_amount_used
            ),
            status=entry.status.value,
            joined_at=entry.joined_at,
            refunded_at=entry.refunded_at,
        )
        for entry in entries
    ]

    return AdminEntryListResponse(
        items=items, total=total, limit=limit, offset=offset
    )
