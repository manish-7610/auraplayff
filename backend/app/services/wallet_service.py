"""
Wallet service — all money-affecting operations go through here.

Design principles:
  - Every balance mutation creates an immutable WalletTransaction ledger entry.
  - All mutations are atomic: wallet row + ledger row commit together.
  - Row-level locking (SELECT ... FOR UPDATE) prevents race conditions.
  - Idempotency keys (unique per logical operation) prevent double-credits/debits.
  - Decimal arithmetic throughout — no floats, ever.
  - The service never trusts amount values from request bodies. Callers must
    pass pre-validated Decimal amounts derived from server-side business logic.
  - Negative balances are impossible: DB CHECK constraints + service guards.

Entry-fee split logic (referral bonus usage rule):
  - At most 50 % of entry_fee may come from referral_balance.
  - The remaining must come from deposit_balance (or winning_balance if allowed).
  - A player cannot pay 100 % from referral_balance.
  - calculate_entry_fee_split() encapsulates this rule for routers/tournament service.
"""
from __future__ import annotations

import logging
import uuid
from decimal import ROUND_DOWN, Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    DuplicateTransactionError,
    InsufficientBalanceError,
    InvalidAmountError,
    ReferralBonusLimitExceededError,
    WalletFrozenError,
    WalletNotFoundError,
)
from app.models.wallet import (
    BalanceColumn,
    TransactionDirection,
    TransactionType,
    Wallet,
    WalletTransaction,
)

logger = logging.getLogger("auraplayff.wallet")

# Referral bonus may cover at most this fraction of an entry fee
REFERRAL_BONUS_MAX_FRACTION = Decimal("0.50")

# Monetary precision used throughout (2 decimal places, truncating)
_CENTS = Decimal("0.01")


# ── Internal helpers ──────────────────────────────────────────────────────────

def _quantize(amount: Decimal) -> Decimal:
    """Round down to 2 decimal places. Never round up financial amounts."""
    return amount.quantize(_CENTS, rounding=ROUND_DOWN)


def _get_balance_attr(wallet: Wallet, column: BalanceColumn) -> Decimal:
    mapping = {
        BalanceColumn.deposit: wallet.deposit_balance,
        BalanceColumn.winning: wallet.winning_balance,
        BalanceColumn.referral: wallet.referral_balance,
    }
    return mapping[column]


def _set_balance_attr(wallet: Wallet, column: BalanceColumn, value: Decimal) -> None:
    if column == BalanceColumn.deposit:
        wallet.deposit_balance = value
    elif column == BalanceColumn.winning:
        wallet.winning_balance = value
    elif column == BalanceColumn.referral:
        wallet.referral_balance = value


# ── Wallet fetch (with row lock) ──────────────────────────────────────────────

async def get_wallet_for_update(
    db: AsyncSession,
    user_id: uuid.UUID,
) -> Wallet:
    """
    Fetch the wallet row with a FOR UPDATE lock.
    Must be called inside an active transaction.
    Raises WalletNotFoundError if wallet does not exist.
    """
    result = await db.execute(
        select(Wallet)
        .where(Wallet.user_id == user_id)
        .with_for_update()
    )
    wallet = result.scalar_one_or_none()
    if wallet is None:
        raise WalletNotFoundError()
    return wallet


async def get_wallet(
    db: AsyncSession,
    user_id: uuid.UUID,
) -> Wallet:
    """
    Fetch the wallet row (read-only, no lock).
    Raises WalletNotFoundError if wallet does not exist.
    """
    result = await db.execute(
        select(Wallet).where(Wallet.user_id == user_id)
    )
    wallet = result.scalar_one_or_none()
    if wallet is None:
        raise WalletNotFoundError()
    return wallet


# ── Core ledger operation ─────────────────────────────────────────────────────

async def _record_transaction(
    db: AsyncSession,
    wallet: Wallet,
    tx_type: TransactionType,
    balance_col: BalanceColumn,
    direction: TransactionDirection,
    amount: Decimal,
    idempotency_key: str | None = None,
    reference_id: uuid.UUID | None = None,
    reference_type: str | None = None,
    description: str | None = None,
    meta: dict[str, Any] | None = None,
    created_by_id: uuid.UUID | None = None,
) -> WalletTransaction:
    """
    Apply a balance mutation to the wallet and record the ledger entry atomically.

    The caller is responsible for:
      - Holding the wallet row lock (get_wallet_for_update)
      - Being inside a DB transaction that it will commit

    This function:
      1. Validates amount > 0
      2. Checks wallet is not frozen
      3. Checks sufficient balance for debits
      4. Snapshots balance_before
      5. Mutates the wallet balance column
      6. Snapshots balance_after
      7. Inserts the WalletTransaction row
      8. Flushes (caller commits)

    Raises:
      InvalidAmountError          — amount <= 0
      WalletFrozenError           — wallet.is_frozen
      InsufficientBalanceError    — debit would create negative balance
      DuplicateTransactionError   — idempotency_key already exists (IntegrityError)
    """
    if amount <= Decimal("0"):
        raise InvalidAmountError()

    amount = _quantize(amount)

    if wallet.is_frozen:
        raise WalletFrozenError()

    current_balance = _get_balance_attr(wallet, balance_col)

    if direction == TransactionDirection.debit:
        if current_balance < amount:
            raise InsufficientBalanceError(balance_col.value)
        new_balance = _quantize(current_balance - amount)
    else:
        new_balance = _quantize(current_balance + amount)

    balance_before = current_balance
    balance_after = new_balance

    # Mutate wallet in-memory (session tracks this)
    _set_balance_attr(wallet, balance_col, new_balance)
    from app.models._types import utcnow
    wallet.updated_at = utcnow()

    tx = WalletTransaction(
        wallet_id=wallet.id,
        user_id=wallet.user_id,
        type=tx_type,
        balance_column=balance_col,
        direction=direction,
        amount=amount,
        balance_before=balance_before,
        balance_after=balance_after,
        idempotency_key=idempotency_key,
        reference_id=reference_id,
        reference_type=reference_type,
        description=description,
        meta=meta,
        created_by_id=created_by_id,
    )
    db.add(tx)

    try:
        await db.flush([wallet, tx])
    except IntegrityError as exc:
        # DO NOT call db.rollback() here.
        # get_db() is the single authoritative rollback owner.
        # Calling rollback() in service code causes a double-rollback on
        # PostgreSQL (asyncpg raises NoActiveSQLTransactionError on the
        # second rollback), which propagates as an unhandled 500.
        # Just re-raise — get_db()'s except block will roll back cleanly.
        if idempotency_key and (
            "uq_wallet_tx_idempotency_key" in str(exc.orig)
            or "wallet_transactions.idempotency_key" in str(exc.orig)
        ):
            raise DuplicateTransactionError() from exc
        raise

    return tx


# ── Public credit operations ──────────────────────────────────────────────────

async def credit_deposit_balance(
    db: AsyncSession,
    user_id: uuid.UUID,
    amount: Decimal,
    *,
    idempotency_key: str | None = None,
    reference_id: uuid.UUID | None = None,
    reference_type: str | None = None,
    description: str | None = None,
    meta: dict[str, Any] | None = None,
    created_by_id: uuid.UUID | None = None,
) -> WalletTransaction:
    """Credit real money to deposit_balance. Used for approved deposit payloads."""
    wallet = await get_wallet_for_update(db, user_id)
    return await _record_transaction(
        db, wallet,
        tx_type=TransactionType.deposit,
        balance_col=BalanceColumn.deposit,
        direction=TransactionDirection.credit,
        amount=amount,
        idempotency_key=idempotency_key,
        reference_id=reference_id,
        reference_type=reference_type,
        description=description or "Deposit credited",
        meta=meta,
        created_by_id=created_by_id,
    )


async def credit_referral_bonus(
    db: AsyncSession,
    user_id: uuid.UUID,
    amount: Decimal,
    *,
    idempotency_key: str,
    reference_id: uuid.UUID | None = None,
    description: str | None = None,
    meta: dict[str, Any] | None = None,
) -> WalletTransaction:
    """
    Credit referral bonus to referral_balance (non-withdrawable).
    idempotency_key is REQUIRED to prevent double-credits.
    Callers must use a stable key like "referral_bonus:<referral_relationship_id>:<role>"
    where role is "referrer" or "referred".
    """
    wallet = await get_wallet_for_update(db, user_id)
    return await _record_transaction(
        db, wallet,
        tx_type=TransactionType.referral_bonus,
        balance_col=BalanceColumn.referral,
        direction=TransactionDirection.credit,
        amount=amount,
        idempotency_key=idempotency_key,
        reference_id=reference_id,
        reference_type="referral_relationship",
        description=description or "Referral bonus credited",
        meta=meta,
    )


async def credit_prize(
    db: AsyncSession,
    user_id: uuid.UUID,
    amount: Decimal,
    *,
    idempotency_key: str,
    reference_id: uuid.UUID | None = None,
    reference_type: str = "tournament",
    description: str | None = None,
    meta: dict[str, Any] | None = None,
) -> WalletTransaction:
    """Credit tournament prize winnings to winning_balance."""
    wallet = await get_wallet_for_update(db, user_id)
    return await _record_transaction(
        db, wallet,
        tx_type=TransactionType.prize,
        balance_col=BalanceColumn.winning,
        direction=TransactionDirection.credit,
        amount=amount,
        idempotency_key=idempotency_key,
        reference_id=reference_id,
        reference_type=reference_type,
        description=description or "Tournament prize credited",
        meta=meta,
    )


async def credit_kill_reward(
    db: AsyncSession,
    user_id: uuid.UUID,
    amount: Decimal,
    *,
    idempotency_key: str,
    reference_id: uuid.UUID | None = None,
    description: str | None = None,
    meta: dict[str, Any] | None = None,
) -> WalletTransaction:
    """Credit per-kill reward to winning_balance."""
    wallet = await get_wallet_for_update(db, user_id)
    return await _record_transaction(
        db, wallet,
        tx_type=TransactionType.kill_reward,
        balance_col=BalanceColumn.winning,
        direction=TransactionDirection.credit,
        amount=amount,
        idempotency_key=idempotency_key,
        reference_id=reference_id,
        reference_type="tournament",
        description=description or "Kill reward credited",
        meta=meta,
    )


# ── Public debit operations ───────────────────────────────────────────────────

async def debit_entry_fee(
    db: AsyncSession,
    user_id: uuid.UUID,
    deposit_amount: Decimal,
    referral_amount: Decimal,
    *,
    idempotency_key_deposit: str,
    idempotency_key_referral: str | None = None,
    reference_id: uuid.UUID | None = None,
    description: str | None = None,
    meta: dict[str, Any] | None = None,
) -> tuple[WalletTransaction, WalletTransaction | None]:
    """
    Debit tournament entry fee — may split between deposit_balance and referral_balance.

    Returns (deposit_tx, referral_tx).
    referral_tx is None when referral_amount == 0.

    Callers must call calculate_entry_fee_split() first and pass the result here.
    Both debits share the same reference_id (tournament slot / join record).
    """
    wallet = await get_wallet_for_update(db, user_id)

    deposit_tx = await _record_transaction(
        db, wallet,
        tx_type=TransactionType.entry_fee,
        balance_col=BalanceColumn.deposit,
        direction=TransactionDirection.debit,
        amount=deposit_amount,
        idempotency_key=idempotency_key_deposit,
        reference_id=reference_id,
        reference_type="tournament",
        description=description or "Tournament entry fee (deposit)",
        meta=meta,
    )

    referral_tx: WalletTransaction | None = None
    if referral_amount > Decimal("0"):
        if idempotency_key_referral is None:
            raise ValueError("idempotency_key_referral required when referral_amount > 0")
        referral_tx = await _record_transaction(
            db, wallet,
            tx_type=TransactionType.entry_fee,
            balance_col=BalanceColumn.referral,
            direction=TransactionDirection.debit,
            amount=referral_amount,
            idempotency_key=idempotency_key_referral,
            reference_id=reference_id,
            reference_type="tournament",
            description=description or "Tournament entry fee (referral bonus)",
            meta=meta,
        )

    return deposit_tx, referral_tx


async def refund_entry_fee(
    db: AsyncSession,
    user_id: uuid.UUID,
    deposit_amount: Decimal,
    referral_amount: Decimal,
    *,
    idempotency_key_deposit: str,
    idempotency_key_referral: str | None = None,
    reference_id: uuid.UUID | None = None,
    description: str | None = None,
    meta: dict[str, Any] | None = None,
) -> tuple[WalletTransaction, WalletTransaction | None]:
    """
    Refund a tournament entry fee (e.g. tournament cancelled).
    Reverses deposit and referral portions back to their original columns.
    """
    wallet = await get_wallet_for_update(db, user_id)

    deposit_tx = await _record_transaction(
        db, wallet,
        tx_type=TransactionType.entry_fee_refund,
        balance_col=BalanceColumn.deposit,
        direction=TransactionDirection.credit,
        amount=deposit_amount,
        idempotency_key=idempotency_key_deposit,
        reference_id=reference_id,
        reference_type="tournament",
        description=description or "Tournament entry fee refund (deposit)",
        meta=meta,
    )

    referral_tx: WalletTransaction | None = None
    if referral_amount > Decimal("0"):
        if idempotency_key_referral is None:
            raise ValueError("idempotency_key_referral required when referral_amount > 0")
        referral_tx = await _record_transaction(
            db, wallet,
            tx_type=TransactionType.entry_fee_refund,
            balance_col=BalanceColumn.referral,
            direction=TransactionDirection.credit,
            amount=referral_amount,
            idempotency_key=idempotency_key_referral,
            reference_id=reference_id,
            reference_type="tournament",
            description=description or "Tournament entry fee refund (referral bonus)",
            meta=meta,
        )

    return deposit_tx, referral_tx


# ── Admin adjustment ──────────────────────────────────────────────────────────

async def admin_adjust(
    db: AsyncSession,
    user_id: uuid.UUID,
    balance_col: BalanceColumn,
    direction: TransactionDirection,
    amount: Decimal,
    *,
    idempotency_key: str,
    created_by_id: uuid.UUID,
    description: str,
    meta: dict[str, Any] | None = None,
) -> WalletTransaction:
    """
    Controlled manual wallet adjustment by an authorised staff member.
    Requires WALLET_MANUAL_ADJUST permission (enforced at router level).
    idempotency_key is mandatory to prevent duplicate adjustments.
    description is mandatory for audit trail.
    """
    wallet = await get_wallet_for_update(db, user_id)
    return await _record_transaction(
        db, wallet,
        tx_type=TransactionType.admin_adjustment,
        balance_col=balance_col,
        direction=direction,
        amount=amount,
        idempotency_key=idempotency_key,
        reference_type="admin_adjustment",
        description=description,
        meta=meta,
        created_by_id=created_by_id,
    )


# ── Business rule helpers ─────────────────────────────────────────────────────

class EntryFeeSplit:
    """Result of calculate_entry_fee_split()."""
    __slots__ = ("deposit_amount", "referral_amount", "total")

    def __init__(self, deposit_amount: Decimal, referral_amount: Decimal) -> None:
        self.deposit_amount = deposit_amount
        self.referral_amount = referral_amount
        self.total = deposit_amount + referral_amount


def calculate_entry_fee_split(
    entry_fee: Decimal,
    deposit_balance: Decimal,
    referral_balance: Decimal,
    use_referral: bool = True,
) -> EntryFeeSplit:
    """
    Determine how much to debit from deposit_balance vs referral_balance for
    a tournament entry fee, respecting the 50% referral usage cap.

    Rule:
      max_referral = floor(entry_fee * 50%)
      actual_referral = min(referral_balance, max_referral)
      deposit_required = entry_fee - actual_referral

    Raises:
      InvalidAmountError          — entry_fee <= 0
      InsufficientBalanceError    — deposit_balance < deposit_required
      ReferralBonusLimitExceededError — (never raised here; kept in router for explicit UX)
    """
    if entry_fee <= Decimal("0"):
        raise InvalidAmountError("Entry fee must be greater than zero.")

    entry_fee = _quantize(entry_fee)

    if not use_referral or referral_balance <= Decimal("0"):
        # No referral balance to use
        referral_amount = Decimal("0")
    else:
        max_referral = _quantize(entry_fee * REFERRAL_BONUS_MAX_FRACTION)
        referral_amount = min(referral_balance, max_referral)
        referral_amount = _quantize(referral_amount)

    deposit_required = _quantize(entry_fee - referral_amount)

    if deposit_balance < deposit_required:
        raise InsufficientBalanceError("deposit balance")

    return EntryFeeSplit(
        deposit_amount=deposit_required,
        referral_amount=referral_amount,
    )


def validate_entry_fee_split_request(
    entry_fee: Decimal,
    requested_referral_amount: Decimal,
    deposit_balance: Decimal,
    referral_balance: Decimal,
) -> EntryFeeSplit:
    """
    Validate a caller-specified referral split (e.g. from a future API parameter).

    Enforces:
      1. requested_referral_amount <= floor(entry_fee * 50%)
      2. requested_referral_amount <= referral_balance
      3. deposit_required <= deposit_balance

    Raises ReferralBonusLimitExceededError if the cap is exceeded.
    """
    entry_fee = _quantize(entry_fee)
    requested_referral_amount = _quantize(requested_referral_amount)

    max_referral = _quantize(entry_fee * REFERRAL_BONUS_MAX_FRACTION)

    if requested_referral_amount > max_referral:
        raise ReferralBonusLimitExceededError(
            max_referral=str(max_referral),
            entry_fee=str(entry_fee),
        )

    if requested_referral_amount > referral_balance:
        raise InsufficientBalanceError("referral balance")

    deposit_required = _quantize(entry_fee - requested_referral_amount)
    if deposit_balance < deposit_required:
        raise InsufficientBalanceError("deposit balance")

    return EntryFeeSplit(
        deposit_amount=deposit_required,
        referral_amount=requested_referral_amount,
    )


# ── Wallet freeze (admin) ─────────────────────────────────────────────────────

async def freeze_wallet(
    db: AsyncSession,
    user_id: uuid.UUID,
) -> Wallet:
    """Freeze a player's wallet. All subsequent transactions will be blocked."""
    wallet = await get_wallet_for_update(db, user_id)
    wallet.is_frozen = True
    from app.models._types import utcnow
    wallet.updated_at = utcnow()
    await db.flush([wallet])
    return wallet


async def unfreeze_wallet(
    db: AsyncSession,
    user_id: uuid.UUID,
) -> Wallet:
    """Unfreeze a player's wallet."""
    wallet = await get_wallet_for_update(db, user_id)
    wallet.is_frozen = False
    from app.models._types import utcnow
    wallet.updated_at = utcnow()
    await db.flush([wallet])
    return wallet


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 5 — Withdrawal reservation / completion / release
# ═══════════════════════════════════════════════════════════════════════════════
#
# Accounting model:
#   RESERVE  : debit winning_balance, credit withdrawal_hold
#   RELEASE  : debit withdrawal_hold, credit winning_balance  (on rejection)
#   COMPLETE : debit withdrawal_hold                          (on payment)
#
# winning_balance and withdrawal_hold are both Numeric(12,2) with CHECK >= 0.
# All operations use SELECT FOR UPDATE + flush in the same DB transaction.
# Idempotency keys follow: "withdrawal:<withdrawal_id>:<action>"
# ═══════════════════════════════════════════════════════════════════════════════


async def reserve_withdrawal(
    db: AsyncSession,
    user_id: uuid.UUID,
    amount: Decimal,
    *,
    withdrawal_id: uuid.UUID,
    description: str | None = None,
    meta: dict[str, Any] | None = None,
) -> tuple[WalletTransaction, WalletTransaction]:
    """
    Reserve withdrawal amount from winning_balance into withdrawal_hold.

    Creates TWO ledger entries atomically:
      1. Debit winning_balance  (type=withdrawal_request, direction=debit)
      2. Credit withdrawal_hold (type=withdrawal_request, direction=credit)

    Idempotency keys:
      "withdrawal:<withdrawal_id>:reserve:debit"
      "withdrawal:<withdrawal_id>:reserve:credit"

    Raises:
      InsufficientBalanceError — if winning_balance < amount
      WalletFrozenError        — if wallet is frozen
      DuplicateTransactionError — if already reserved (same idempotency key)
    """
    if amount <= Decimal("0"):
        raise InvalidAmountError()

    wallet = await get_wallet_for_update(db, user_id)

    # Check sufficient winning balance BEFORE any mutation
    available_winning = wallet.winning_balance - wallet.withdrawal_hold
    if available_winning < amount:
        raise InsufficientBalanceError("winning balance")

    ik_debit = f"withdrawal:{withdrawal_id}:reserve:debit"
    ik_credit = f"withdrawal:{withdrawal_id}:reserve:credit"

    # 1. Debit winning_balance
    debit_tx = await _record_transaction(
        db, wallet,
        tx_type=TransactionType.withdrawal_request,
        balance_col=BalanceColumn.winning,
        direction=TransactionDirection.debit,
        amount=amount,
        idempotency_key=ik_debit,
        reference_id=withdrawal_id,
        reference_type="withdrawal",
        description=description or "Withdrawal reserve — winning balance hold",
        meta=meta,
    )

    # 2. Credit withdrawal_hold  (withdrawal_hold is tracked in wallet.withdrawal_hold,
    #    NOT in a named BalanceColumn. We manipulate it directly here.)
    #    We record this as a "credit" on a synthetic balance_column so the
    #    ledger remains complete and auditable.
    amount_q = _quantize(amount)
    hold_before = wallet.withdrawal_hold
    wallet.withdrawal_hold = _quantize(wallet.withdrawal_hold + amount_q)
    from app.models._types import utcnow as _utcnow
    wallet.updated_at = _utcnow()

    hold_tx = WalletTransaction(
        wallet_id=wallet.id,
        user_id=wallet.user_id,
        type=TransactionType.withdrawal_request,
        balance_column=BalanceColumn.hold,   # BUG #4 FIX: correct column for hold ledger
        direction=TransactionDirection.credit,
        amount=amount_q,
        balance_before=hold_before,
        balance_after=wallet.withdrawal_hold,
        idempotency_key=ik_credit,
        reference_id=withdrawal_id,
        reference_type="withdrawal",
        description="Withdrawal hold — reserved from winning balance",
        meta={**(meta or {}), "hold_column": "withdrawal_hold"},
        created_by_id=None,
    )
    db.add(hold_tx)

    try:
        await db.flush([wallet, hold_tx])
    except IntegrityError as exc:
        # DO NOT call db.rollback() here — get_db() owns the rollback.
        if (
            "uq_wallet_tx_idempotency_key" in str(exc.orig)
            or "wallet_transactions.idempotency_key" in str(exc.orig)
        ):
            raise DuplicateTransactionError() from exc
        raise

    return debit_tx, hold_tx


async def release_withdrawal_hold(
    db: AsyncSession,
    user_id: uuid.UUID,
    amount: Decimal,
    *,
    withdrawal_id: uuid.UUID,
    description: str | None = None,
    meta: dict[str, Any] | None = None,
) -> tuple[WalletTransaction, WalletTransaction]:
    """
    Release a withdrawal hold back to winning_balance (rejection path).

    Creates TWO ledger entries:
      1. Debit withdrawal_hold
      2. Credit winning_balance  (type=withdrawal_rejected)

    Idempotency keys:
      "withdrawal:<withdrawal_id>:release:debit"
      "withdrawal:<withdrawal_id>:release:credit"
    """
    if amount <= Decimal("0"):
        raise InvalidAmountError()

    wallet = await get_wallet_for_update(db, user_id)

    amount_q = _quantize(amount)

    if wallet.withdrawal_hold < amount_q:
        raise InsufficientBalanceError("withdrawal hold")

    ik_debit = f"withdrawal:{withdrawal_id}:release:debit"
    ik_credit = f"withdrawal:{withdrawal_id}:release:credit"

    # 1. Debit withdrawal_hold directly
    hold_before = wallet.withdrawal_hold
    wallet.withdrawal_hold = _quantize(wallet.withdrawal_hold - amount_q)
    from app.models._types import utcnow as _utcnow2
    wallet.updated_at = _utcnow2()

    hold_tx = WalletTransaction(
        wallet_id=wallet.id,
        user_id=wallet.user_id,
        type=TransactionType.withdrawal_rejected,
        balance_column=BalanceColumn.hold,  # BUG #4 FIX: correct column for hold ledger
        direction=TransactionDirection.debit,
        amount=amount_q,
        balance_before=hold_before,
        balance_after=wallet.withdrawal_hold,
        idempotency_key=ik_debit,
        reference_id=withdrawal_id,
        reference_type="withdrawal",
        description="Withdrawal hold released — rejection",
        meta={**(meta or {}), "hold_column": "withdrawal_hold"},
        created_by_id=None,
    )
    db.add(hold_tx)

    try:
        await db.flush([wallet, hold_tx])
    except IntegrityError as exc:
        # DO NOT call db.rollback() here — get_db() owns the rollback.
        if (
            "uq_wallet_tx_idempotency_key" in str(exc.orig)
            or "wallet_transactions.idempotency_key" in str(exc.orig)
        ):
            raise DuplicateTransactionError() from exc
        raise

    # 2. Credit winning_balance (refund)
    credit_tx = await _record_transaction(
        db, wallet,
        tx_type=TransactionType.withdrawal_rejected,
        balance_col=BalanceColumn.winning,
        direction=TransactionDirection.credit,
        amount=amount_q,
        idempotency_key=ik_credit,
        reference_id=withdrawal_id,
        reference_type="withdrawal",
        description=description or "Withdrawal rejected — winning balance restored",
        meta=meta,
    )

    return hold_tx, credit_tx


async def complete_withdrawal(
    db: AsyncSession,
    user_id: uuid.UUID,
    amount: Decimal,
    *,
    withdrawal_id: uuid.UUID,
    description: str | None = None,
    meta: dict[str, Any] | None = None,
    created_by_id: uuid.UUID | None = None,
) -> WalletTransaction:
    """
    Finalise a paid withdrawal — debit withdrawal_hold only.

    The winning_balance was already debited at reserve time.
    This entry records the final outflow from the platform.

    Idempotency key: "withdrawal:<withdrawal_id>:complete"
    """
    if amount <= Decimal("0"):
        raise InvalidAmountError()

    wallet = await get_wallet_for_update(db, user_id)
    amount_q = _quantize(amount)

    if wallet.withdrawal_hold < amount_q:
        raise InsufficientBalanceError("withdrawal hold")

    ik = f"withdrawal:{withdrawal_id}:complete"

    hold_before = wallet.withdrawal_hold
    wallet.withdrawal_hold = _quantize(wallet.withdrawal_hold - amount_q)
    from app.models._types import utcnow as _utcnow3
    wallet.updated_at = _utcnow3()

    tx = WalletTransaction(
        wallet_id=wallet.id,
        user_id=wallet.user_id,
        type=TransactionType.withdrawal_completed,
        balance_column=BalanceColumn.hold,  # BUG #4 FIX: correct column for hold ledger
        direction=TransactionDirection.debit,
        amount=amount_q,
        balance_before=hold_before,
        balance_after=wallet.withdrawal_hold,
        idempotency_key=ik,
        reference_id=withdrawal_id,
        reference_type="withdrawal",
        description=description or "Withdrawal completed — funds paid out",
        meta=meta,
        created_by_id=created_by_id,
    )
    db.add(tx)

    try:
        await db.flush([wallet, tx])
    except IntegrityError as exc:
        # DO NOT call db.rollback() here — get_db() owns the rollback.
        if (
            "uq_wallet_tx_idempotency_key" in str(exc.orig)
            or "wallet_transactions.idempotency_key" in str(exc.orig)
        ):
            raise DuplicateTransactionError() from exc
        raise

    return tx


async def get_available_winning_balance(
    db: AsyncSession,
    user_id: uuid.UUID,
) -> Decimal:
    """
    Return the portion of winning_balance not currently held for withdrawals.
    available = winning_balance - withdrawal_hold
    """
    wallet = await get_wallet(db, user_id)
    available = wallet.winning_balance - wallet.withdrawal_hold
    return _quantize(max(available, Decimal("0")))
