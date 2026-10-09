"""
Result service — tournament result entry, verification, completion guard.

Design principles:
  - Results are derived from TournamentEntry — user_id and ff_uid are
    never taken from client input.
  - Only ACTIVE (non-refunded, non-disqualified) entries can receive results.
  - Placement uniqueness within a tournament is enforced at both the DB
    level (UNIQUE constraint) and the service level (pre-check with clear error).
  - Results become immutable once payout_distributed=True.
  - player_stats (total_tournaments, total_wins, total_kills, total_winnings)
    are updated atomically when payout is distributed.
  - The completion guard (assert_results_complete) must be satisfied before
    tournament_service.transition_status(COMPLETED) proceeds.

Audit events:
  result.created, result.updated, result.verified, result.finalized
"""
from __future__ import annotations

import logging
import uuid
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    DuplicatePlacementError,
    InvalidPlacementError,
    NotFoundError,
    ResultAlreadyExistsError,
    ResultAlreadyFinalizedError,
    ResultNotFoundError,
    ResultsIncompleteError,
    TournamentNotFoundError,
)
from app.models._types import utcnow
from app.models.audit_log import ActorType
from app.models.player_stats import PlayerStats
from app.models.result import ResultStatus, TournamentResult
from app.models.staff_user import StaffUser
from app.models.tournament import EntryStatus, TournamentEntry, TournamentStatus
from app.services import audit_service

logger = logging.getLogger("auraplayff.result")


# ── Fetch helpers ─────────────────────────────────────────────────────────────

async def get_result(
    db: AsyncSession,
    result_id: uuid.UUID,
) -> TournamentResult:
    r = await db.execute(
        select(TournamentResult).where(TournamentResult.id == result_id)
    )
    result = r.scalar_one_or_none()
    if result is None:
        raise ResultNotFoundError()
    return result


async def get_result_by_entry(
    db: AsyncSession,
    entry_id: uuid.UUID,
) -> TournamentResult | None:
    r = await db.execute(
        select(TournamentResult).where(TournamentResult.entry_id == entry_id)
    )
    return r.scalar_one_or_none()


async def _load_active_entry(
    db: AsyncSession,
    entry_id: uuid.UUID,
) -> TournamentEntry:
    """Load a TournamentEntry, asserting it exists and is ACTIVE."""
    r = await db.execute(
        select(TournamentEntry).where(TournamentEntry.id == entry_id)
    )
    entry = r.scalar_one_or_none()
    if entry is None:
        raise NotFoundError("TournamentEntry")
    if entry.status != EntryStatus.active:
        from app.core.exceptions import ConflictError
        raise ConflictError(
            f"Cannot enter results for an entry with status '{entry.status.value}'. "
            f"Only ACTIVE entries can receive results."
        )
    return entry


# ── Enter result ──────────────────────────────────────────────────────────────

async def enter_result(
    db: AsyncSession,
    *,
    entry_id: uuid.UUID,
    placement: int,
    kills: int,
    entered_by: StaffUser,
) -> TournamentResult:
    """
    Record a player's placement and kill count for a tournament.

    The user_id and ff_uid are derived from the TournamentEntry — never
    supplied by the client.

    Validates:
      - Entry exists and is ACTIVE.
      - No existing result for this entry.
      - Placement is positive.
      - Kills are non-negative.
      - Placement is unique within the tournament (pre-check + DB guard).

    The result is created with status=entered. Caller commits.
    """
    if placement < 1:
        raise InvalidPlacementError()
    if kills < 0:
        raise InvalidPlacementError("Kills cannot be negative.")

    entry = await _load_active_entry(db, entry_id)

    # Check no result already exists for this entry
    existing = await get_result_by_entry(db, entry_id)
    if existing is not None:
        raise ResultAlreadyExistsError()

    # Pre-check placement uniqueness (friendly error before DB fires)
    dup = await db.execute(
        select(TournamentResult.id).where(
            TournamentResult.tournament_id == entry.tournament_id,
            TournamentResult.placement == placement,
        )
    )
    if dup.scalar_one_or_none() is not None:
        raise DuplicatePlacementError(placement)

    # Load user's FF UID and FF display-name snapshots
    from app.models.user import User
    user_row = await db.execute(
        select(User.ff_uid, User.ff_name).where(User.id == entry.user_id)
    )
    user_data = user_row.one_or_none()
    ff_uid   = (user_data.ff_uid   if user_data else None) or ""
    ff_name  = (user_data.ff_name  if user_data else None)  # None is fine — nullable

    now = utcnow()
    result = TournamentResult(
        tournament_id=entry.tournament_id,
        entry_id=entry_id,
        user_id=entry.user_id,
        ff_uid_snapshot=ff_uid,
        ff_name_snapshot=ff_name,   # snapshot at entry time for historical accuracy
        placement=placement,
        kills=kills,
        result_status=ResultStatus.entered,
        entered_by_id=entered_by.id,
        entered_at=now,
    )
    db.add(result)

    try:
        await db.flush([result])
    except IntegrityError:
        await db.rollback()
        raise DuplicatePlacementError(placement)

    await audit_service.log(
        db=db,
        actor_type=ActorType.staff,
        actor_id=str(entered_by.id),
        action="result.created",
        entity_type="tournament_result",
        entity_id=str(result.id),
        new_value={
            "tournament_id": str(entry.tournament_id),
            "user_id": str(entry.user_id),
            "placement": placement,
            "kills": kills,
        },
    )

    logger.info(
        "Result entered: tournament=%s entry=%s placement=%d kills=%d by=%s",
        entry.tournament_id, entry_id, placement, kills, entered_by.agent_id,
    )
    return result


# ── Update result ─────────────────────────────────────────────────────────────

async def update_result(
    db: AsyncSession,
    *,
    result_id: uuid.UUID,
    placement: int | None = None,
    kills: int | None = None,
    updated_by: StaffUser,
) -> TournamentResult:
    """
    Correct an already-entered result before payout is distributed.

    Immutability: raises ResultAlreadyFinalizedError once payout_distributed=True.
    Placement uniqueness re-checked if placement changes.
    Caller commits.
    """
    result = await get_result(db, result_id)

    if result.is_immutable:
        raise ResultAlreadyFinalizedError()

    old_values = {
        "placement": result.placement,
        "kills": result.kills,
        "status": result.result_status.value,
    }

    if placement is not None:
        if placement < 1:
            raise InvalidPlacementError()
        if placement != result.placement:
            dup = await db.execute(
                select(TournamentResult.id).where(
                    TournamentResult.tournament_id == result.tournament_id,
                    TournamentResult.placement == placement,
                    TournamentResult.id != result_id,
                )
            )
            if dup.scalar_one_or_none() is not None:
                raise DuplicatePlacementError(placement)
            result.placement = placement

    if kills is not None:
        if kills < 0:
            raise InvalidPlacementError("Kills cannot be negative.")
        result.kills = kills

    result.updated_at = utcnow()
    # Reset to entered so verifier must re-verify
    if result.result_status == ResultStatus.verified:
        result.result_status = ResultStatus.entered
        result.verified_by_id = None
        result.verified_at = None

    try:
        await db.flush([result])
    except IntegrityError:
        await db.rollback()
        raise DuplicatePlacementError(result.placement)

    await audit_service.log(
        db=db,
        actor_type=ActorType.staff,
        actor_id=str(updated_by.id),
        action="result.updated",
        entity_type="tournament_result",
        entity_id=str(result_id),
        old_value=old_values,
        new_value={
            "placement": result.placement,
            "kills": result.kills,
            "status": result.result_status.value,
        },
    )
    return result


# ── Verify result ─────────────────────────────────────────────────────────────

async def verify_result(
    db: AsyncSession,
    *,
    result_id: uuid.UUID,
    verified_by: StaffUser,
) -> TournamentResult:
    """
    Mark a result as verified.  Requires result_status == entered.
    Caller commits.
    """
    result = await get_result(db, result_id)

    if result.is_immutable:
        raise ResultAlreadyFinalizedError()
    if result.result_status != ResultStatus.entered:
        from app.core.exceptions import ConflictError
        raise ConflictError(
            f"Result must be in 'entered' status to verify. "
            f"Current status: {result.result_status.value}"
        )

    now = utcnow()
    result.result_status = ResultStatus.verified
    result.verified_by_id = verified_by.id
    result.verified_at = now
    result.updated_at = now
    await db.flush([result])

    await audit_service.log(
        db=db,
        actor_type=ActorType.staff,
        actor_id=str(verified_by.id),
        action="result.verified",
        entity_type="tournament_result",
        entity_id=str(result_id),
        new_value={"status": "verified"},
    )
    return result


# ── Completion guard ──────────────────────────────────────────────────────────

async def assert_results_complete(
    db: AsyncSession,
    tournament_id: uuid.UUID,
) -> None:
    """
    Raise ResultsIncompleteError if any ACTIVE entry lacks a result.
    Called by tournament_service before allowing RESULT_ENTRY → COMPLETED.
    """
    # Count ACTIVE entries
    active_count_result = await db.execute(
        select(func.count()).select_from(TournamentEntry).where(
            TournamentEntry.tournament_id == tournament_id,
            TournamentEntry.status == EntryStatus.active,
        )
    )
    active_count = active_count_result.scalar_one()

    # Count entered/verified/finalized results
    result_count_result = await db.execute(
        select(func.count()).select_from(TournamentResult).where(
            TournamentResult.tournament_id == tournament_id,
            TournamentResult.result_status.in_([
                ResultStatus.entered.value,
                ResultStatus.verified.value,
                ResultStatus.finalized.value,
            ]),
        )
    )
    result_count = result_count_result.scalar_one()

    if result_count < active_count:
        raise ResultsIncompleteError(
            entered=result_count,
            expected=active_count,
        )


# ── Player stats update ───────────────────────────────────────────────────────

async def update_player_stats_after_payout(
    db: AsyncSession,
    tournament_id: uuid.UUID,
) -> None:
    """
    Update PlayerStats for all participants after payout is distributed.
    Increments:
      - total_tournaments for all ACTIVE entries
      - total_wins for 1st-place result
      - total_kills with result kill count
      - total_winnings with total_payout

    Uses SELECT FOR UPDATE per-player to avoid races if called concurrently.
    Caller commits.
    """
    # Load all finalized results for this tournament
    results_query = await db.execute(
        select(TournamentResult).where(
            TournamentResult.tournament_id == tournament_id,
            TournamentResult.result_status == ResultStatus.finalized,
        )
    )
    results = list(results_query.scalars().all())

    for r in results:
        ps_result = await db.execute(
            select(PlayerStats)
            .where(PlayerStats.user_id == r.user_id)
            .with_for_update()
        )
        ps = ps_result.scalar_one_or_none()
        if ps is None:
            continue

        ps.total_tournaments += 1
        ps.total_kills += r.kills
        ps.total_winnings += r.prize_amount + r.kill_reward_amount
        if r.placement == 1:
            ps.total_wins += 1
        ps.updated_at = utcnow()
        await db.flush([ps])

    logger.info(
        "Updated player_stats for %d participants in tournament %s",
        len(results), tournament_id,
    )


# ── List helpers ──────────────────────────────────────────────────────────────

async def list_tournament_results(
    db: AsyncSession,
    tournament_id: uuid.UUID,
) -> list[TournamentResult]:
    """Return all results for a tournament ordered by placement ASC."""
    r = await db.execute(
        select(TournamentResult)
        .where(TournamentResult.tournament_id == tournament_id)
        .order_by(TournamentResult.placement.asc())
    )
    return list(r.scalars().all())
