"""
Player-facing tournament routes.

  GET  /api/tournaments                        — list open/upcoming tournaments
  GET  /api/tournaments/{id}                   — tournament detail
  POST /api/tournaments/{id}/join              — join a tournament (financial op)
  GET  /api/tournaments/{id}/my-entry          — view my entry and payment breakdown
  GET  /api/tournaments/{id}/room              — get room credentials (enrolled only)

Security rules enforced here:
  - All endpoints require a valid player JWT.
  - Room credentials are never returned unless:
      a) the player is actively enrolled (ACTIVE entry)
      b) the room has been officially released (room_released_at is set)
      c) the tournament is not cancelled
  - Payment breakdown for other players is never exposed.
  - next_sheet_number, created_by_id, and internal IDs are never leaked.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user
from app.models.tournament import EntryStatus, Tournament, TournamentEntry, TournamentStatus
from app.models.user import User
from app.core.exceptions import FFNameRequiredError
from app.schemas.tournament import (
    JoinTournamentResponse,
    MyEntryResponse,
    RoomCredentialsResponse,
    TournamentDetailResponse,
    TournamentListItem,
    TournamentListResponse,
)
from app.services import tournament_service

router = APIRouter(prefix="/api/tournaments", tags=["Player — Tournaments"])


# ── List tournaments ──────────────────────────────────────────────────────────

@router.get(
    "",
    response_model=TournamentListResponse,
    summary="List open and upcoming tournaments",
)
async def list_tournaments(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    status: str | None = Query(
        default=None,
        description="Filter by status. Defaults to 'open'. Pass 'all' to see every status.",
    ),
) -> TournamentListResponse:
    """
    Returns tournaments visible to players.
    Defaults to OPEN tournaments. Pass ?status=all to see all non-cancelled ones.
    Room credentials are never included in list responses.
    """
    from app.models.tournament import TournamentStatus as TS

    if status == "all":
        filter_statuses = [
            TS.open, TS.confirmed, TS.room_released, TS.live,
            TS.result_entry, TS.completed,
        ]
    elif status and status != "open":
        try:
            filter_statuses = [TS(status)]
        except ValueError:
            filter_statuses = [TS.open]
    else:
        filter_statuses = [TS.open, TS.room_released]  # Both open and room_released are joinable pre-LIVE

    tournaments, total = await tournament_service.list_tournaments(
        db, statuses=filter_statuses, limit=limit, offset=offset
    )

    # Batch-check which tournaments this player has joined
    if tournaments:
        t_ids = [t.id for t in tournaments]
        result = await db.execute(
            select(TournamentEntry.tournament_id).where(
                TournamentEntry.user_id == current_user.id,
                TournamentEntry.tournament_id.in_(t_ids),
                TournamentEntry.status == EntryStatus.active,
            )
        )
        joined_ids: set[uuid.UUID] = {row[0] for row in result.fetchall()}
    else:
        joined_ids = set()

    items = [
        TournamentListItem.from_tournament(t, already_joined=t.id in joined_ids)
        for t in tournaments
    ]
    return TournamentListResponse(items=items, total=total, limit=limit, offset=offset)


# ── Tournament detail ─────────────────────────────────────────────────────────

@router.get(
    "/{tournament_id}",
    response_model=TournamentDetailResponse,
    summary="Get tournament details",
)
async def get_tournament(
    tournament_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TournamentDetailResponse:
    """
    Full tournament detail visible to any authenticated player.
    Includes placement prizes and kill reward tiers from the prize config.
    Room credentials are NOT included here — use /room endpoint instead.
    """
    from decimal import Decimal as _Dec
    from app.services import prize_service

    t = await tournament_service.get_tournament(db, tournament_id)
    entry = await tournament_service.get_entry(db, tournament_id, current_user.id)
    already_joined = entry is not None and entry.status == EntryStatus.active

    # Load prize config — non-fatal if not found (returns None)
    try:
        prize_cfg = await prize_service.get_prize_config(db, t.entry_fee)
    except Exception:
        prize_cfg = None

    return TournamentDetailResponse.from_tournament(
        t, already_joined=already_joined, prize_config=prize_cfg
    )


# ── Join tournament ───────────────────────────────────────────────────────────

@router.post(
    "/{tournament_id}/join",
    response_model=JoinTournamentResponse,
    status_code=201,
    summary="Join a tournament (deducts entry fee from wallet)",
)
async def join_tournament(
    tournament_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JoinTournamentResponse:
    """
    Enrol the authenticated player in the specified tournament.

    The server:
    - Determines entry fee from the tournament (never trusted from client).
    - Calculates the referral bonus / deposit split (50% referral cap).
    - Atomically assigns a sheet number and debits the wallet.
    - Returns the full payment breakdown.

    Idempotent at the DB level: a duplicate join attempt returns 409.
    """
    # ── FF Name guard — MUST run before any financial operation ──────────────
    # A player without a Free Fire name cannot join.  This check uses the
    # already-authenticated current_user object (no extra DB query needed).
    # Wallet is NOT touched and no entry is created if this guard fires.
    if not current_user.ff_name:
        raise FFNameRequiredError()

    entry = await tournament_service.join_tournament(
        db,
        tournament_id=tournament_id,
        user_id=current_user.id,
    )
    await db.commit()

    # Reload tournament for the code/title (entry doesn't hold these)
    tournament = await tournament_service.get_tournament(db, tournament_id)

    total = entry.deposit_amount_used + entry.referral_amount_used

    # Phase 7: notify player of successful registration
    from app.services import notification_service as ns
    try:
        async with db.begin():
            await ns.notify_player_tournament_joined(
                db,
                user_id=current_user.id,
                tournament_id=tournament_id,
                tournament_title=tournament.title,
                entry_fee=str(entry.entry_fee_snapshot),
                sheet_number=entry.sheet_number,
            )
    except Exception:
        pass  # notification failure must not affect the join response

    return JoinTournamentResponse(
        entry_id=entry.id,
        tournament_id=tournament_id,
        tournament_code=tournament.tournament_code,
        sheet_number=entry.sheet_number,
        entry_fee=str(entry.entry_fee_snapshot),
        deposit_used=str(entry.deposit_amount_used),
        referral_used=str(entry.referral_amount_used),
        total_paid=str(total),
    )


# ── My entry ──────────────────────────────────────────────────────────────────

@router.get(
    "/{tournament_id}/my-entry",
    response_model=MyEntryResponse,
    summary="View your entry and payment breakdown for a tournament",
)
async def get_my_entry(
    tournament_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MyEntryResponse:
    """
    Returns this player's own entry details including the exact payment breakdown.
    Returns 403 if the player is not enrolled.
    """
    entry = await tournament_service.require_entry(
        db, tournament_id, current_user.id
    )
    tournament = await tournament_service.get_tournament(db, tournament_id)
    return MyEntryResponse.from_entry(entry, tournament=tournament)


# ── Room credentials ──────────────────────────────────────────────────────────

@router.get(
    "/{tournament_id}/room",
    response_model=RoomCredentialsResponse,
    summary="Get room ID and password (enrolled players only, after release)",
)
async def get_room_credentials(
    tournament_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RoomCredentialsResponse:
    """
    Returns room ID and password.

    Guards:
    1. Player must have an ACTIVE entry in this tournament.
    2. Room must have been officially released by staff.
    3. Tournament must not be cancelled.

    Non-enrolled players receive 403 — NOT a 404 — to avoid leaking
    whether a tournament exists.
    """
    from app.core.exceptions import RoomNotReleasedError, NotEnrolledError, TournamentAlreadyCancelledError

    tournament = await tournament_service.get_tournament(db, tournament_id)

    # Guard: cancelled tournament
    if tournament.status == TournamentStatus.cancelled:
        raise TournamentAlreadyCancelledError()

    # Guard: player must be enrolled with an ACTIVE entry
    entry = await tournament_service.get_entry(db, tournament_id, current_user.id)
    if entry is None or entry.status != EntryStatus.active:
        raise NotEnrolledError()

    # Guard: room must be released
    if not tournament.room_is_released:
        raise RoomNotReleasedError()

    return RoomCredentialsResponse(
        tournament_id=tournament.id,
        tournament_code=tournament.tournament_code,
        tournament_title=tournament.title,
        sheet_number=entry.sheet_number,
        room_id=tournament.room_id,          # type: ignore[arg-type]
        room_password=tournament.room_password,  # type: ignore[arg-type]
        room_released_at=tournament.room_released_at,  # type: ignore[arg-type]
        scheduled_at=tournament.scheduled_at,
    )
