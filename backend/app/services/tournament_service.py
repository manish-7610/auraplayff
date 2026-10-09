"""
Tournament service — all tournament lifecycle operations.

Design principles:
  - Every operation that touches money calls wallet_service exclusively.
  - No business logic in route handlers — routers call service functions.
  - All mutating operations use SELECT FOR UPDATE on the Tournament row to
    prevent race conditions (duplicate joins, double-refunds, concurrent
    status changes).
  - Sheet numbers are assigned from Tournament.next_sheet_number under the
    same FOR UPDATE lock — no MAX()+1 pattern, no gaps on failure.
  - Idempotency keys for wallet transactions follow the convention:
      entry_fee debit deposit:  "entry_deposit:<entry_id>"
      entry_fee debit referral: "entry_referral:<entry_id>"
      refund deposit:           "refund_deposit:<entry_id>"
      refund referral:          "refund_referral:<entry_id>"
    The entry UUID is generated before the wallet call and stored on the row.
  - Services never commit — callers (routers) commit after the service returns.
  - Referral hook (mark_first_tournament_completed) is called inside the same
    transaction as the status → COMPLETED transition for atomicity.

Supported entry fees:
  SUPPORTED_ENTRY_FEES is the single source of truth. The validation in
  create_tournament() uses this set. Adding ₹200 later requires one line.

Concurrency:
  join_tournament:
    1. Tournament row locked FOR UPDATE.
    2. Duplicate-entry check (UNIQUE constraint is the DB-level guard too).
    3. Slot count check before sheet assignment.
    4. Wallet row locked inside wallet_service.debit_entry_fee (FOR UPDATE).
    5. All flushes happen before commit.
  cancel_tournament:
    1. Tournament row locked FOR UPDATE.
    2. All entries fetched; each wallet refunded individually inside the
       same session (still pre-commit).
    3. player_count reset to 0 after all refunds succeed.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    AlreadyJoinedTournamentError,
    ConflictError,
    FFNameRequiredError,
    InsufficientPlayersError,
    NotFoundError,
    TournamentAlreadyCancelledError,
    TournamentAlreadyCompletedError,
    TournamentFullError,
    TournamentInvalidTransitionError,
    TournamentNotFoundError,
    TournamentNotOpenError,
    TournamentScheduleError,
    InvalidEntryFeeError,
    RoomNotReleasedError,
    NotEnrolledError,
)
from app.models._types import utcnow
from app.models.audit_log import ActorType
from app.models.tournament import (
    CANCELLABLE_STATUSES,
    JOINABLE_STATUSES,
    VALID_TRANSITIONS,
    EntryStatus,
    Tournament,
    TournamentEntry,
    TournamentStatus,
)
from app.models.staff_user import StaffUser
from app.services import audit_service, wallet_service
from app.services.wallet_service import calculate_entry_fee_split

logger = logging.getLogger("auraplayff.tournament")

# ── Configuration ─────────────────────────────────────────────────────────────

# All valid entry fees (INR). Changing this set is the only change needed to
# add or remove a fee tier.
SUPPORTED_ENTRY_FEES: frozenset[Decimal] = frozenset(
    Decimal(str(fee)) for fee in [10, 20, 30, 40, 50, 100]
)

_TOURNAMENT_CODE_PREFIX = "T"
_MAX_PLAYERS = 50
_MIN_PLAYERS = 40


# ── Tournament code generation ────────────────────────────────────────────────

async def _generate_tournament_code(db: AsyncSession) -> str:
    """
    Generate the next sequential tournament code: T-0001, T-0002, …

    BUG #8 FIX: The previous COUNT(*)+1 approach had a race condition —
    two concurrent transactions could both read the same COUNT, generate
    the same code (e.g. T-0006), and then both attempt to INSERT, causing
    one to fail with an IntegrityError on the UNIQUE constraint.

    Fix: use MAX() on the numeric suffix extracted via CAST so that
    concurrent callers each see the highest committed code and produce a
    strictly larger candidate.  The UNIQUE constraint on tournament_code
    remains the final guard; create_tournament() retries once on collision.

    MAX() under REPEATABLE READ or READ COMMITTED both work correctly here:
    - Under READ COMMITTED: sees all committed rows → safe.
    - Under REPEATABLE READ: snapshot at tx start → same collision risk as
      COUNT, so the IntegrityError retry in create_tournament() is still needed.
    The UNIQUE constraint + retry is what makes this fully safe.
    """
    # Extract the numeric part of the code (e.g. "T-0042" → 42) and take MAX.
    # NULLIF handles the empty-table case (no rows yet → returns NULL → coalesce 0).
    from sqlalchemy import cast, Integer, text as sa_text
    result = await db.execute(
        sa_text(
            "SELECT COALESCE(MAX(CAST(SUBSTRING(tournament_code FROM '[0-9]+$') AS INTEGER)), 0)"
            " FROM tournaments"
        )
    )
    max_seq = result.scalar_one() or 0
    return f"{_TOURNAMENT_CODE_PREFIX}-{max_seq + 1:04d}"


# ── Status transition guard ───────────────────────────────────────────────────

def assert_valid_transition(
    tournament: Tournament,
    target: TournamentStatus,
) -> None:
    """
    Raise TournamentInvalidTransitionError if the transition is not allowed.
    Also raises specific errors for terminal states.
    """
    if tournament.status == TournamentStatus.cancelled:
        raise TournamentAlreadyCancelledError()
    if tournament.status == TournamentStatus.completed:
        raise TournamentAlreadyCompletedError()

    allowed = VALID_TRANSITIONS.get(tournament.status, set())
    if target not in allowed:
        raise TournamentInvalidTransitionError(
            from_status=tournament.status.value,
            to_status=target.value,
        )


# ── Fetch helpers ─────────────────────────────────────────────────────────────

async def get_tournament(
    db: AsyncSession,
    tournament_id: uuid.UUID,
) -> Tournament:
    """Read-only fetch. Raises TournamentNotFoundError if missing."""
    result = await db.execute(
        select(Tournament).where(Tournament.id == tournament_id)
    )
    t = result.scalar_one_or_none()
    if t is None:
        raise TournamentNotFoundError()
    return t


async def get_tournament_for_update(
    db: AsyncSession,
    tournament_id: uuid.UUID,
) -> Tournament:
    """Fetch tournament row with FOR UPDATE lock for all mutating operations."""
    result = await db.execute(
        select(Tournament)
        .where(Tournament.id == tournament_id)
        .with_for_update()
    )
    t = result.scalar_one_or_none()
    if t is None:
        raise TournamentNotFoundError()
    return t


async def get_entry(
    db: AsyncSession,
    tournament_id: uuid.UUID,
    user_id: uuid.UUID,
) -> TournamentEntry | None:
    """Return the entry for this player in this tournament, or None."""
    result = await db.execute(
        select(TournamentEntry).where(
            TournamentEntry.tournament_id == tournament_id,
            TournamentEntry.user_id == user_id,
        )
    )
    return result.scalar_one_or_none()


async def require_entry(
    db: AsyncSession,
    tournament_id: uuid.UUID,
    user_id: uuid.UUID,
) -> TournamentEntry:
    """Return entry or raise NotEnrolledError."""
    entry = await get_entry(db, tournament_id, user_id)
    if entry is None:
        raise NotEnrolledError()
    return entry


# ── Create tournament ─────────────────────────────────────────────────────────

async def create_tournament(
    db: AsyncSession,
    *,
    title: str,
    entry_fee: Decimal,
    scheduled_at: datetime,
    created_by: StaffUser,
    game: str = "free_fire",
    mode: str = "solo",
    max_players: int = _MAX_PLAYERS,
    min_players: int = _MIN_PLAYERS,
) -> Tournament:
    """
    Create a new tournament in DRAFT status.

    Validates:
      - entry_fee is in SUPPORTED_ENTRY_FEES
      - scheduled_at is in the future
      - max_players and min_players consistency

    Creator identity is taken from the authenticated StaffUser — never from
    the request body.
    """
    # Validate entry fee
    entry_fee = Decimal(str(entry_fee)).quantize(Decimal("0.01"))
    if entry_fee not in SUPPORTED_ENTRY_FEES:
        raise InvalidEntryFeeError(str(entry_fee))

    # Validate schedule
    now = utcnow()
    if scheduled_at.tzinfo is None:
        scheduled_at = scheduled_at.replace(tzinfo=timezone.utc)
    if scheduled_at <= now:
        raise TournamentScheduleError(
            "Scheduled time must be in the future."
        )

    # Validate player limits
    if min_players > max_players:
        raise TournamentScheduleError(
            "Minimum players cannot exceed maximum players."
        )

    code = await _generate_tournament_code(db)

    from app.models.tournament import TournamentGame, TournamentMode
    tournament = Tournament(
        tournament_code=code,
        title=title.strip(),
        game=TournamentGame(game),
        mode=TournamentMode(mode),
        entry_fee=entry_fee,
        max_players=max_players,
        min_players=min_players,
        player_count=0,
        next_sheet_number=1,
        status=TournamentStatus.draft,
        scheduled_at=scheduled_at,
        created_by_id=created_by.id,
        created_by_display_name=created_by.display_name,
        created_by_role=created_by.role.value,
    )
    db.add(tournament)

    try:
        await db.flush([tournament])
    except IntegrityError as exc:
        # BUG #8 FIX: on a tournament_code collision (two concurrent creates
        # produced the same code), regenerate with an incremented sequence and
        # retry once.  A second collision is astronomically unlikely.
        if "tournament_code" not in str(exc.orig):
            raise
        await db.rollback()
        code = await _generate_tournament_code(db)
        tournament.tournament_code = code
        db.add(tournament)
        await db.flush([tournament])

    await audit_service.log(
        db=db,
        actor_type=ActorType.staff,
        actor_id=str(created_by.id),
        action="tournament.created",
        entity_type="tournament",
        entity_id=str(tournament.id),
        new_value={
            "code": code,
            "title": title,
            "entry_fee": str(entry_fee),
            "scheduled_at": scheduled_at.isoformat(),
            "max_players": max_players,
            "min_players": min_players,
        },
    )

    logger.info(
        "Tournament %s created by %s (entry_fee=%s, scheduled=%s)",
        code, created_by.agent_id, entry_fee, scheduled_at.isoformat(),
    )
    return tournament


# ── Status transition ─────────────────────────────────────────────────────────

async def transition_status(
    db: AsyncSession,
    tournament_id: uuid.UUID,
    target_status: TournamentStatus,
    *,
    actor: StaffUser,
    reason: str | None = None,
) -> Tournament:
    """
    Move a tournament to the target status.

    Validates the transition against VALID_TRANSITIONS.
    Sets lifecycle timestamps and logs an audit entry.
    Caller commits.
    """
    tournament = await get_tournament_for_update(db, tournament_id)
    assert_valid_transition(tournament, target_status)

    old_status = tournament.status.value
    now = utcnow()
    tournament.status = target_status
    tournament.updated_at = now

    # Set appropriate timestamp for the transition
    if target_status == TournamentStatus.live:
        tournament.started_at = now
    elif target_status == TournamentStatus.completed:
        # ── Phase 4: results must be complete before COMPLETED ────────────────
        from app.services import result_service, prize_service
        await result_service.assert_results_complete(db, tournament_id)

        tournament.completed_at = now
        tournament.completed_by_id = actor.id

        # ── Payout distribution ───────────────────────────────────────────────
        # Load prize config for this tournament's entry fee (raises if unconfigured)
        config = await prize_service.get_prize_config(db, tournament.entry_fee)
        await prize_service.distribute_all_payouts(
            db, tournament_id, config, actor_id=str(actor.id)
        )
        # Update player lifetime stats atomically in same transaction
        await result_service.update_player_stats_after_payout(db, tournament_id)

        # ── Referral hook ─────────────────────────────────────────────────────
        await _trigger_referral_completions(db, tournament_id)
    elif target_status == TournamentStatus.cancelled:
        tournament.cancelled_at = now
        tournament.cancelled_by_id = actor.id
        if reason:
            tournament.cancellation_reason = reason

    await db.flush([tournament])

    await audit_service.log(
        db=db,
        actor_type=ActorType.staff,
        actor_id=str(actor.id),
        action=f"tournament.status_changed",
        entity_type="tournament",
        entity_id=str(tournament.id),
        old_value={"status": old_status},
        new_value={"status": target_status.value, "reason": reason},
    )

    logger.info(
        "Tournament %s transitioned %s → %s by %s",
        tournament.tournament_code, old_status, target_status.value, actor.agent_id,
    )
    return tournament


async def _trigger_referral_completions(
    db: AsyncSession,
    tournament_id: uuid.UUID,
) -> None:
    """
    For every ACTIVE entry in this tournament, call the referral hook.
    mark_first_tournament_completed is idempotent — safe to call for all users.
    """
    from app.services import referral_service
    result = await db.execute(
        select(TournamentEntry.user_id).where(
            TournamentEntry.tournament_id == tournament_id,
            TournamentEntry.status == EntryStatus.active,
        )
    )
    user_ids = [row[0] for row in result.fetchall()]
    for user_id in user_ids:
        try:
            await referral_service.mark_first_tournament_completed(
                db,
                referred_user_id=user_id,
                triggered_by="tournament_completion",
            )
        except Exception:
            # Referral failure must never abort the main tournament transition
            logger.exception(
                "Referral hook failed for user %s on tournament %s — continuing",
                user_id, tournament_id,
            )


# ── Join tournament ───────────────────────────────────────────────────────────

async def join_tournament(
    db: AsyncSession,
    tournament_id: uuid.UUID,
    user_id: uuid.UUID,
) -> TournamentEntry:
    """
    Enrol a player in a tournament.

    Full concurrency-safe join flow:
      1.  Lock tournament row FOR UPDATE.
      2.  Assert tournament is OPEN.
      3.  Check player not already enrolled (app + UNIQUE constraint guard).
      4.  Check slot availability.
      5.  Calculate entry fee split (referral cap 50%).
      6.  Verify wallet balances (inside wallet_service with FOR UPDATE).
      7.  Assign next sheet number from Tournament.next_sheet_number (atomic).
      8.  Increment tournament.player_count.
      9.  Increment tournament.next_sheet_number.
      10. Create TournamentEntry with all payment snapshots.
      11. Debit wallet (wallet row locked inside wallet_service).
      12. Flush everything. Caller commits.

    Idempotency keys use the pre-generated entry UUID so they are stable
    across retries even if the route is called twice.

    Returns the created TournamentEntry.
    """
    # ── 1. Lock tournament ────────────────────────────────────────────────────
    tournament = await get_tournament_for_update(db, tournament_id)

    # ── 2. Assert joinable ────────────────────────────────────────────────────
    if tournament.status not in JOINABLE_STATUSES:
        raise TournamentNotOpenError(tournament.status.value)

    # ── 3. Duplicate check ────────────────────────────────────────────────────
    existing = await get_entry(db, tournament_id, user_id)
    if existing is not None:
        raise AlreadyJoinedTournamentError()

    # ── 4. Slot check ─────────────────────────────────────────────────────────
    if tournament.player_count >= tournament.max_players:
        raise TournamentFullError()

    # ── 5. Entry fee split ────────────────────────────────────────────────────
    # Load wallet read-only first to calculate split; wallet_service will
    # re-lock it with FOR UPDATE during the actual debit.
    wallet = await wallet_service.get_wallet(db, user_id)
    split = calculate_entry_fee_split(
        entry_fee=tournament.entry_fee,
        deposit_balance=wallet.deposit_balance,
        referral_balance=wallet.referral_balance,
    )

    # ── 7–9. Sheet number assignment (under the tournament lock) ──────────────
    sheet_number = tournament.next_sheet_number

    # ── 10. Build TournamentEntry (flush after wallet to get final state) ─────
    entry_id = uuid.uuid4()
    entry = TournamentEntry(
        id=entry_id,
        tournament_id=tournament_id,
        user_id=user_id,
        sheet_number=sheet_number,
        entry_fee_snapshot=tournament.entry_fee,
        deposit_amount_used=split.deposit_amount,
        referral_amount_used=split.referral_amount,
        status=EntryStatus.active,
    )
    db.add(entry)

    # ── 8–9. Update tournament counters ──────────────────────────────────────
    tournament.player_count += 1
    tournament.next_sheet_number += 1
    tournament.updated_at = utcnow()

    # Flush entry + tournament to get IDs and detect UNIQUE constraint violations
    # before touching the wallet (so a duplicate join never debits money).
    try:
        await db.flush([tournament, entry])
    except IntegrityError:
        await db.rollback()
        raise AlreadyJoinedTournamentError()

    # ── 11. Debit wallet ──────────────────────────────────────────────────────
    meta: dict[str, Any] = {
        "tournament_id": str(tournament_id),
        "tournament_code": tournament.tournament_code,
        "tournament_title": tournament.title,
        "entry_id": str(entry_id),
        "sheet_number": sheet_number,
    }

    await wallet_service.debit_entry_fee(
        db,
        user_id=user_id,
        deposit_amount=split.deposit_amount,
        referral_amount=split.referral_amount,
        idempotency_key_deposit=f"entry_deposit:{entry_id}",
        idempotency_key_referral=f"entry_referral:{entry_id}" if split.referral_amount > Decimal("0") else None,
        reference_id=entry_id,
        description=(
            f"Tournament entry: {tournament.title} "
            f"(Sheet #{sheet_number})"
        ),
        meta=meta,
    )

    await audit_service.log(
        db=db,
        actor_type=ActorType.user,
        actor_id=str(user_id),
        action="tournament.joined",
        entity_type="tournament_entry",
        entity_id=str(entry_id),
        new_value={
            "tournament_id": str(tournament_id),
            "tournament_code": tournament.tournament_code,
            "sheet_number": sheet_number,
            "entry_fee": str(tournament.entry_fee),
            "deposit_used": str(split.deposit_amount),
            "referral_used": str(split.referral_amount),
        },
    )

    logger.info(
        "User %s joined tournament %s — sheet=%d deposit=%s referral=%s",
        user_id, tournament.tournament_code, sheet_number,
        split.deposit_amount, split.referral_amount,
    )
    return entry


# ── Cancel tournament ─────────────────────────────────────────────────────────

async def cancel_tournament(
    db: AsyncSession,
    tournament_id: uuid.UUID,
    *,
    actor: StaffUser,
    reason: str,
) -> Tournament:
    """
    Cancel a tournament and atomically refund all ACTIVE entries.

    Steps:
      1. Lock tournament row FOR UPDATE.
      2. Validate cancellable status.
      3. For each ACTIVE entry: refund deposit + referral amounts.
         Each refund uses a deterministic idempotency key so if the operation
         is retried, wallets are not double-credited.
      4. Mark all entries as REFUNDED.
      5. Update tournament status, timestamps, counters.
      6. Audit log.
      7. Caller commits entire batch.

    All refunds occur within the same transaction as the status change —
    no partial cancellations are possible.
    """
    tournament = await get_tournament_for_update(db, tournament_id)

    if tournament.status == TournamentStatus.cancelled:
        raise TournamentAlreadyCancelledError()
    if tournament.status == TournamentStatus.completed:
        raise TournamentAlreadyCompletedError()
    if tournament.status not in CANCELLABLE_STATUSES:
        raise TournamentInvalidTransitionError(
            from_status=tournament.status.value,
            to_status=TournamentStatus.cancelled.value,
        )

    # Fetch all active entries
    result = await db.execute(
        select(TournamentEntry).where(
            TournamentEntry.tournament_id == tournament_id,
            TournamentEntry.status == EntryStatus.active,
        )
    )
    entries = list(result.scalars().all())

    now = utcnow()
    refund_meta: dict[str, Any] = {
        "tournament_id": str(tournament_id),
        "tournament_code": tournament.tournament_code,
        "reason": reason,
    }

    for entry in entries:
        deposit_tx, referral_tx = await wallet_service.refund_entry_fee(
            db,
            user_id=entry.user_id,
            deposit_amount=entry.deposit_amount_used,
            referral_amount=entry.referral_amount_used,
            idempotency_key_deposit=f"refund_deposit:{entry.id}",
            idempotency_key_referral=(
                f"refund_referral:{entry.id}"
                if entry.referral_amount_used > Decimal("0")
                else None
            ),
            reference_id=entry.id,
            description=(
                f"Refund: {tournament.title} cancelled. {reason}"
            ),
            meta=refund_meta,
        )

        # Stamp refund info onto entry
        entry.status = EntryStatus.refunded
        entry.refunded_at = now
        entry.refund_deposit_tx_id = deposit_tx.id
        entry.refund_referral_tx_id = (
            referral_tx.id if referral_tx is not None else None
        )
        await db.flush([entry])

    # Update tournament
    old_status = tournament.status.value
    tournament.status = TournamentStatus.cancelled
    tournament.cancelled_at = now
    tournament.cancelled_by_id = actor.id
    tournament.cancellation_reason = reason
    tournament.player_count = 0
    tournament.updated_at = now
    await db.flush([tournament])

    await audit_service.log(
        db=db,
        actor_type=ActorType.staff,
        actor_id=str(actor.id),
        action="tournament.cancelled",
        entity_type="tournament",
        entity_id=str(tournament.id),
        old_value={"status": old_status, "player_count": len(entries)},
        new_value={
            "status": "cancelled",
            "reason": reason,
            "refunded_entries": len(entries),
        },
    )

    logger.info(
        "Tournament %s cancelled by %s — %d entries refunded. Reason: %s",
        tournament.tournament_code, actor.agent_id, len(entries), reason,
    )
    return tournament


# ── Release room credentials ──────────────────────────────────────────────────

async def release_room(
    db: AsyncSession,
    tournament_id: uuid.UUID,
    *,
    room_id: str,
    room_password: str,
    actor: StaffUser,
) -> Tournament:
    """
    Publish room credentials for enrolled players.

    Can only be done from CONFIRMED status.
    Transitions the tournament to ROOM_RELEASED.
    """
    tournament = await get_tournament_for_update(db, tournament_id)
    assert_valid_transition(tournament, TournamentStatus.room_released)

    now = utcnow()
    tournament.room_id = room_id.strip()
    tournament.room_password = room_password.strip()
    tournament.room_released_at = now
    tournament.room_released_by_id = actor.id
    tournament.status = TournamentStatus.room_released
    tournament.updated_at = now
    await db.flush([tournament])

    await audit_service.log(
        db=db,
        actor_type=ActorType.staff,
        actor_id=str(actor.id),
        action="tournament.room_released",
        entity_type="tournament",
        entity_id=str(tournament.id),
        new_value={
            "room_id": room_id,
            "released_at": now.isoformat(),
            "status": TournamentStatus.room_released.value,
        },
    )

    logger.info(
        "Room released for tournament %s by %s",
        tournament.tournament_code, actor.agent_id,
    )
    return tournament


# ── Scheduled transition (CONFIRMED → LIVE) ───────────────────────────────────

async def attempt_scheduled_transition(
    db: AsyncSession,
    tournament_id: uuid.UUID,
) -> Tournament:
    """
    Automatically transition a tournament from ROOM_RELEASED → LIVE if:
      - Status is ROOM_RELEASED
      - scheduled_at has passed
      - player_count >= min_players

    Called by the scheduler/background job; safe to call repeatedly (idempotent).
    Raises InsufficientPlayersError if below min threshold.
    Does NOT raise for already-LIVE tournaments (idempotent no-op).
    """
    tournament = await get_tournament_for_update(db, tournament_id)

    if tournament.status == TournamentStatus.live:
        return tournament  # Already live — idempotent

    if tournament.status != TournamentStatus.room_released:
        raise TournamentInvalidTransitionError(
            from_status=tournament.status.value,
            to_status=TournamentStatus.live.value,
        )

    now = utcnow()
    scheduled = tournament.scheduled_at
    if scheduled.tzinfo is None:
        scheduled = scheduled.replace(tzinfo=timezone.utc)
    if now < scheduled:
        raise TournamentScheduleError(
            "Scheduled time has not arrived yet."
        )

    if tournament.player_count < tournament.min_players:
        raise InsufficientPlayersError(
            current=tournament.player_count,
            minimum=tournament.min_players,
        )

    tournament.status = TournamentStatus.live
    tournament.started_at = now
    tournament.updated_at = now
    await db.flush([tournament])

    await audit_service.log(
        db=db,
        actor_type=ActorType.system,
        actor_id="scheduler",
        action="tournament.auto_live",
        entity_type="tournament",
        entity_id=str(tournament.id),
        new_value={
            "player_count": tournament.player_count,
            "started_at": now.isoformat(),
        },
    )
    return tournament


# ── List tournaments ──────────────────────────────────────────────────────────

async def list_tournaments(
    db: AsyncSession,
    *,
    statuses: list[TournamentStatus] | None = None,
    entry_fee: Decimal | None = None,
    limit: int = 20,
    offset: int = 0,
    created_by_id: uuid.UUID | None = None,
) -> tuple[list[Tournament], int]:
    """
    Return (tournaments, total_count) ordered by scheduled_at ASC.
    Optionally filter by a list of statuses, entry_fee, and/or created_by_id.

    created_by_id — when supplied, restricts results to tournaments created by
    that staff user.  Pass None (default) for Super Admin (no restriction).
    """
    base_q = select(Tournament)
    if statuses:
        base_q = base_q.where(Tournament.status.in_([s.value for s in statuses]))
    # BUG #5 FIX: apply entry_fee filter so that pagination totals and page
    # contents correctly reflect the filtered result set.
    if entry_fee is not None:
        base_q = base_q.where(Tournament.entry_fee == entry_fee)
    # Ownership filter: restricts to tournaments created by a specific agent.
    # Caller passes None to disable (Super Admin path).
    if created_by_id is not None:
        base_q = base_q.where(Tournament.created_by_id == created_by_id)

    count_result = await db.execute(
        select(func.count()).select_from(base_q.subquery())
    )
    total = count_result.scalar_one()

    result = await db.execute(
        base_q.order_by(Tournament.scheduled_at.asc()).limit(limit).offset(offset)
    )
    return list(result.scalars().all()), total


# ── List entries ──────────────────────────────────────────────────────────────

async def list_tournament_entries(
    db: AsyncSession,
    tournament_id: uuid.UUID,
    *,
    active_only: bool = True,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[TournamentEntry], int]:
    """Return paginated entries for a tournament, ordered by sheet_number."""
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
    return list(result.scalars().all()), total
