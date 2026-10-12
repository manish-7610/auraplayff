"""
Withdrawal service — orchestrates all withdrawal flows.

Responsibilities
----------------
1. create_withdrawal()
   - Validates amount (>= min_withdrawal, <= available winning balance).
   - Validates UPI ID format (already done in schema, re-checked here).
   - Enforces one-active-withdrawal rule.
   - Reserves winning_balance → withdrawal_hold atomically.
   - Creates Withdrawal record (status=PENDING).

2. admin_process_withdrawal()
   - Moves status PENDING → PROCESSING.
   - No wallet change at this step.

3. admin_mark_paid()
   - Moves status PROCESSING → PAID.
   - Calls complete_withdrawal() to finalise the hold debit.
   - Records payout UTR.

4. admin_reject_withdrawal()
   - Moves status PENDING|PROCESSING → REJECTED.
   - Releases the withdrawal hold back to winning_balance.

5. Listing/query helpers for both player and admin.

Balance accounting
------------------
  RESERVE  (create_withdrawal)    : winning_balance -= X, withdrawal_hold += X
  COMPLETE (admin_mark_paid)      : withdrawal_hold -= X  (funds leave platform)
  RELEASE  (admin_reject)         : withdrawal_hold -= X, winning_balance += X

Financial safety rules
----------------------
- Amount comparison uses available_winning = winning_balance - withdrawal_hold
  (not raw winning_balance) to prevent over-reservation.
- All mutations use SELECT FOR UPDATE via wallet_service helpers.
- Idempotency keys: "withdrawal:<id>:reserve:debit/credit", ":complete", ":release:*"
- One-active-withdrawal per user enforced to prevent simultaneous over-reservation.
- No float arithmetic anywhere.
- Status transitions are server-side only.
"""
from __future__ import annotations

import logging
import uuid
from decimal import Decimal

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.exceptions import AppError
from app.models._types import utcnow
from app.models.audit_log import ActorType
from app.models.platform_setting import (
    SETTING_MIN_WITHDRAWAL_AMOUNT,
    SETTING_DEFAULTS,
    PlatformSetting,
)
from app.models.withdrawal import Withdrawal, WithdrawalStatus
from app.services import audit_service
from app.services import wallet_service

logger = logging.getLogger("auraplayff.withdrawal")

# ── Custom exceptions ─────────────────────────────────────────────────────────

class WithdrawalNotFoundError(AppError):
    def __init__(self) -> None:
        # AppError.__init__(code, message, status_code) — code comes first
        super().__init__(
            "withdrawal_not_found",
            "Withdrawal not found.",
            404,
        )

class WithdrawalBelowMinimumError(AppError):
    def __init__(self, minimum: str) -> None:
        super().__init__(
            "withdrawal_below_minimum",
            f"Withdrawal amount is below the minimum of ₹{minimum}.",
            422,
        )

class WithdrawalExceedsBalanceError(AppError):
    def __init__(self) -> None:
        super().__init__(
            "withdrawal_exceeds_balance",
            "Withdrawal amount exceeds your available winning balance.",
            422,
        )

class ActiveWithdrawalExistsError(AppError):
    def __init__(self) -> None:
        super().__init__(
            "active_withdrawal_exists",
            "You already have a pending or processing withdrawal request. "
            "Please wait for it to be resolved before submitting a new one.",
            409,
        )

class WithdrawalNotActionableError(AppError):
    def __init__(self, status: str) -> None:
        super().__init__(
            "withdrawal_not_actionable",
            f"Withdrawal cannot be actioned in its current status: {status}.",
            409,
        )


# ── Platform setting helper ───────────────────────────────────────────────────

async def _get_min_withdrawal(db: AsyncSession) -> Decimal:
    result = await db.execute(
        select(PlatformSetting.value).where(
            PlatformSetting.key == SETTING_MIN_WITHDRAWAL_AMOUNT
        )
    )
    row = result.scalar_one_or_none()
    raw = row if row is not None else SETTING_DEFAULTS[SETTING_MIN_WITHDRAWAL_AMOUNT]
    try:
        return Decimal(raw)
    except Exception:
        return Decimal("100.00")


# ── Public API ────────────────────────────────────────────────────────────────

async def create_withdrawal(
    db: AsyncSession,
    user_id: uuid.UUID,
    amount: Decimal,
    upi_id: str,
    *,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> Withdrawal:
    """
    Player submits a withdrawal request.

    Steps:
    1. Validate min amount.
    2. Load wallet and check available winning balance.
    3. Enforce one-active-withdrawal rule.
    4. Reserve winning_balance → withdrawal_hold.
    5. Create Withdrawal row (status=PENDING).
    6. Audit.

    Raises:
      WithdrawalBelowMinimumError    — amount < min_withdrawal
      WithdrawalExceedsBalanceError  — amount > available winning balance
      ActiveWithdrawalExistsError    — another pending/processing withdrawal exists
      WalletFrozenError              — wallet frozen
    """
    min_withdrawal = await _get_min_withdrawal(db)
    if amount < min_withdrawal:
        raise WithdrawalBelowMinimumError(str(min_withdrawal))

    # Check available winning balance BEFORE creating the record
    available = await wallet_service.get_available_winning_balance(db, user_id)
    if amount > available:
        raise WithdrawalExceedsBalanceError()

    # One-active-withdrawal rule
    active = await db.execute(
        select(func.count()).where(
            Withdrawal.user_id == user_id,
            Withdrawal.status.in_(list(WithdrawalStatus.active_states())),
        )
    )
    if (active.scalar_one() or 0) > 0:
        raise ActiveWithdrawalExistsError()

    # Create withdrawal record first to get the ID for idempotency keys
    withdrawal = Withdrawal(
        user_id=user_id,
        status=WithdrawalStatus.pending,
        amount=amount,
        currency="INR",
        upi_id=upi_id,
        idempotency_key=f"withdrawal:create:{user_id}:{amount}:{upi_id}",
    )
    db.add(withdrawal)
    await db.flush([withdrawal])

    # Reserve winning balance (wallet mutation + ledger entries).
    # DO NOT wrap in try/except with db.rollback() here.
    # get_db() is the single authoritative rollback owner — calling
    # db.rollback() in service code causes a double-rollback on PostgreSQL
    # (asyncpg raises NoActiveSQLTransactionError on the second call),
    # which propagates as an unhandled 500. Any exception here propagates
    # cleanly to get_db()'s except block, which rolls back once, correctly.
    reserve_tx, hold_tx = await wallet_service.reserve_withdrawal(
        db,
        user_id=user_id,
        amount=amount,
        withdrawal_id=withdrawal.id,
        description=f"Withdrawal request Rs{amount} to {upi_id}",
        meta={"withdrawal_id": str(withdrawal.id)},
    )

    # Link reservation transaction to withdrawal
    withdrawal.wallet_reserve_tx_id = reserve_tx.id
    withdrawal.updated_at = utcnow()
    await db.flush([withdrawal])

    await audit_service.log(
        db=db,
        actor_type=ActorType.user,
        actor_id=str(user_id),
        action="withdrawal.submitted",
        entity_type="withdrawal",
        entity_id=str(withdrawal.id),
        new_value={
            "amount": str(amount),
            "upi_id": upi_id,
            "status": WithdrawalStatus.pending.value,
        },
        ip_address=ip_address,
        user_agent=user_agent,
    )

    logger.info(
        "Withdrawal created: id=%s user=%s amount=%s upi=%s",
        withdrawal.id, user_id, amount, upi_id,
    )
    return withdrawal


async def admin_process_withdrawal(
    db: AsyncSession,
    withdrawal_id: uuid.UUID,
    staff_id: uuid.UUID,
    *,
    note: str | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> Withdrawal:
    """
    Admin marks withdrawal as PROCESSING (started).
    No wallet mutation at this step — hold already reserved at creation.
    """
    withdrawal = await _get_withdrawal(db, withdrawal_id)

    if withdrawal.status == WithdrawalStatus.processing:
        return withdrawal

    if withdrawal.status != WithdrawalStatus.pending:
        raise WithdrawalNotActionableError(withdrawal.status.value)

    old_status = withdrawal.status.value
    withdrawal.status = WithdrawalStatus.processing
    withdrawal.processed_by_id = staff_id
    withdrawal.processed_at = utcnow()
    withdrawal.payout_note = note
    withdrawal.updated_at = utcnow()
    await db.flush([withdrawal])

    await audit_service.log(
        db=db,
        actor_type=ActorType.staff,
        actor_id=str(staff_id),
        action="withdrawal.processing",
        entity_type="withdrawal",
        entity_id=str(withdrawal_id),
        old_value={"status": old_status},
        new_value={"status": "processing"},
        ip_address=ip_address,
        user_agent=user_agent,
    )
    return withdrawal


async def admin_mark_paid(
    db: AsyncSession,
    withdrawal_id: uuid.UUID,
    staff_id: uuid.UUID,
    *,
    payout_utr: str,
    note: str | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> Withdrawal:
    """
    Admin marks withdrawal as PAID.

    Atomically:
    1. Validates status is PROCESSING.
    2. Calls complete_withdrawal() → debit withdrawal_hold.
    3. Sets status=PAID, payout_utr, completion TX.
    4. Audits.

    Idempotency: if already PAID, returns without re-debiting hold.
    """
    withdrawal = await _get_withdrawal(db, withdrawal_id)

    if withdrawal.status == WithdrawalStatus.paid:
        return withdrawal

    if withdrawal.status != WithdrawalStatus.processing:
        raise WithdrawalNotActionableError(withdrawal.status.value)

    # Finalise hold debit
    complete_tx = await wallet_service.complete_withdrawal(
        db,
        user_id=withdrawal.user_id,
        amount=withdrawal.amount,
        withdrawal_id=withdrawal.id,
        description=f"Withdrawal paid — UTR: {payout_utr}",
        meta={"withdrawal_id": str(withdrawal_id), "payout_utr": payout_utr},
        created_by_id=staff_id,
    )

    withdrawal.status = WithdrawalStatus.paid
    withdrawal.payout_utr = payout_utr
    withdrawal.payout_note = note
    withdrawal.processed_by_id = staff_id
    withdrawal.processed_at = utcnow()
    withdrawal.wallet_complete_tx_id = complete_tx.id
    withdrawal.updated_at = utcnow()
    await db.flush([withdrawal])

    await audit_service.log(
        db=db,
        actor_type=ActorType.staff,
        actor_id=str(staff_id),
        action="withdrawal.paid",
        entity_type="withdrawal",
        entity_id=str(withdrawal_id),
        old_value={"status": "processing"},
        new_value={
            "status": "paid",
            "payout_utr": payout_utr,
            "amount": str(withdrawal.amount),
            "wallet_complete_tx_id": str(complete_tx.id),
        },
        ip_address=ip_address,
        user_agent=user_agent,
    )

    logger.info(
        "Withdrawal paid: id=%s user=%s amount=%s utr=%s",
        withdrawal.id, withdrawal.user_id, withdrawal.amount, payout_utr,
    )
    return withdrawal


async def admin_reject_withdrawal(
    db: AsyncSession,
    withdrawal_id: uuid.UUID,
    staff_id: uuid.UUID,
    *,
    reason: str,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> Withdrawal:
    """
    Admin rejects a PENDING or PROCESSING withdrawal.

    Atomically:
    1. Releases withdrawal_hold → credits winning_balance back.
    2. Sets status=REJECTED.
    3. Records reason in payout_note.
    4. Audits.

    Idempotency: if already REJECTED, returns without re-releasing hold.
    """
    withdrawal = await _get_withdrawal(db, withdrawal_id)

    if withdrawal.status == WithdrawalStatus.rejected:
        return withdrawal

    if withdrawal.status not in WithdrawalStatus.active_states():
        raise WithdrawalNotActionableError(withdrawal.status.value)

    old_status = withdrawal.status.value

    # Release hold back to winning_balance
    hold_tx, refund_tx = await wallet_service.release_withdrawal_hold(
        db,
        user_id=withdrawal.user_id,
        amount=withdrawal.amount,
        withdrawal_id=withdrawal.id,
        description=f"Withdrawal rejected — {reason}",
        meta={"withdrawal_id": str(withdrawal_id), "reason": reason},
    )

    withdrawal.status = WithdrawalStatus.rejected
    withdrawal.payout_note = reason
    withdrawal.processed_by_id = staff_id
    withdrawal.processed_at = utcnow()
    withdrawal.wallet_complete_tx_id = refund_tx.id
    withdrawal.updated_at = utcnow()
    await db.flush([withdrawal])

    await audit_service.log(
        db=db,
        actor_type=ActorType.staff,
        actor_id=str(staff_id),
        action="withdrawal.rejected",
        entity_type="withdrawal",
        entity_id=str(withdrawal_id),
        old_value={"status": old_status},
        new_value={
            "status": "rejected",
            "reason": reason,
            "amount_released": str(withdrawal.amount),
        },
        ip_address=ip_address,
        user_agent=user_agent,
    )

    logger.info(
        "Withdrawal rejected: id=%s user=%s amount=%s reason=%r",
        withdrawal.id, withdrawal.user_id, withdrawal.amount, reason,
    )
    return withdrawal


# ── Query helpers ─────────────────────────────────────────────────────────────

async def get_withdrawal_for_user(
    db: AsyncSession,
    withdrawal_id: uuid.UUID,
    user_id: uuid.UUID,
) -> Withdrawal:
    """Fetch a withdrawal, enforcing ownership."""
    result = await db.execute(
        select(Withdrawal).where(
            Withdrawal.id == withdrawal_id,
            Withdrawal.user_id == user_id,
        )
    )
    w = result.scalar_one_or_none()
    if w is None:
        raise WithdrawalNotFoundError()
    return w


async def list_withdrawals_for_user(
    db: AsyncSession,
    user_id: uuid.UUID,
    *,
    limit: int = 20,
    offset: int = 0,
) -> tuple[list[Withdrawal], int]:
    """Return paginated withdrawals for a player (own only)."""
    q = select(Withdrawal).where(Withdrawal.user_id == user_id)
    count_result = await db.execute(
        select(func.count()).select_from(q.subquery())
    )
    total = count_result.scalar_one()

    rows = await db.execute(
        q.order_by(Withdrawal.created_at.desc()).limit(limit).offset(offset)
    )
    return list(rows.scalars().all()), total


async def admin_list_withdrawals(
    db: AsyncSession,
    *,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[Withdrawal], int]:
    """Admin: list withdrawals with optional status filter."""
    q = select(Withdrawal)
    if status:
        q = q.where(Withdrawal.status == status)

    count_result = await db.execute(
        select(func.count()).select_from(q.subquery())
    )
    total = count_result.scalar_one()

    rows = await db.execute(
        q.options(selectinload(Withdrawal.user))
        .order_by(Withdrawal.created_at.desc()).limit(limit).offset(offset)
    )
    return list(rows.scalars().all()), total


async def admin_get_withdrawal(
    db: AsyncSession,
    withdrawal_id: uuid.UUID,
) -> Withdrawal:
    """Admin: fetch any withdrawal by ID."""
    return await _get_withdrawal(db, withdrawal_id)


# ── Private helpers ───────────────────────────────────────────────────────────

async def _get_withdrawal(db: AsyncSession, withdrawal_id: uuid.UUID) -> Withdrawal:
    result = await db.execute(
        select(Withdrawal)
        .options(selectinload(Withdrawal.user))
        .where(Withdrawal.id == withdrawal_id)
    )
    w = result.scalar_one_or_none()
    if w is None:
        raise WithdrawalNotFoundError()
    return w
