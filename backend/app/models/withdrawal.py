"""
Withdrawal model — tracks every withdrawal request.

State machine
-------------
  PENDING     →  PROCESSING  (admin begins processing)
  PROCESSING  →  PAID        (admin marks paid, enters payout UTR)
  PENDING     →  REJECTED    (admin rejects before processing)
  PROCESSING  →  REJECTED    (admin rejects during processing)

Balance accounting
------------------
On REQUEST  : winning_balance -= amount  |  withdrawal_hold += amount
On PAID     : withdrawal_hold -= amount  (funds leave platform)
On REJECTED : withdrawal_hold -= amount  |  winning_balance += amount  (refund)

These ledger entries are created in wallet_service via:
  withdrawal_request   → debit winning, credit hold
  withdrawal_completed → debit hold
  withdrawal_rejected  → debit hold, credit winning (refund)

Rules
-----
- Minimum withdrawal = ₹100 (enforced by service, also configurable in PlatformSetting).
- Only winning_balance is withdrawable.
- A player may only have one PENDING or PROCESSING withdrawal at a time
  (enforced at service layer to prevent over-reservation).
- Status can never be set by frontend.
- payout_utr is recorded by admin when marking PAID.
- Every state transition is audited.
"""
from __future__ import annotations

import enum as pyenum
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CheckConstraint, DateTime, Enum, ForeignKey,
    JSON, Numeric, String, Text, UniqueConstraint, text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models._types import utcnow

_jsonb = JSONB().with_variant(JSON(), "sqlite")


class WithdrawalStatus(str, pyenum.Enum):
    pending = "pending"
    processing = "processing"
    paid = "paid"
    rejected = "rejected"

    @classmethod
    def active_states(cls) -> frozenset["WithdrawalStatus"]:
        """States where the withdrawal hold is still active."""
        return frozenset({cls.pending, cls.processing})

    @classmethod
    def terminal_states(cls) -> frozenset["WithdrawalStatus"]:
        return frozenset({cls.paid, cls.rejected})


class Withdrawal(Base):
    """
    One withdrawal request.

    Columns
    -------
    status              : see WithdrawalStatus
    amount              : requested amount (INR, 2 d.p.)
    upi_id              : player-supplied UPI destination (e.g. "player@upi")
    payout_utr          : UTR / reference entered by admin when paid
    payout_note         : Staff note at any stage
    processed_by_id     : Staff user who processed/rejected
    processed_at        : Timestamp of staff action
    wallet_reserve_tx_id   : WalletTransaction that debited winning + credited hold
    wallet_complete_tx_id  : WalletTransaction that finalized debit (paid) or refunded (rejected)
    idempotency_key     : Prevents duplicate withdrawal submissions
    meta                : Flexible JSONB
    """
    __tablename__ = "withdrawals"
    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_withdrawal_amount_positive"),
        UniqueConstraint("idempotency_key", name="uq_withdrawal_idempotency_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    status: Mapped[WithdrawalStatus] = mapped_column(
        Enum(WithdrawalStatus, name="withdrawal_status_enum", native_enum=False),
        nullable=False,
        index=True,
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="INR")

    # ── Player-supplied payment destination ───────────────────────────────────
    upi_id: Mapped[str] = mapped_column(String(100), nullable=False)

    # ── Admin payout fields ───────────────────────────────────────────────────
    payout_utr: Mapped[str | None] = mapped_column(String(100), nullable=True)
    payout_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    processed_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("staff_users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    processed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # ── Wallet ledger linkage ────────────────────────────────────────────────
    # TX that moved funds from winning_balance to withdrawal_hold
    wallet_reserve_tx_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("wallet_transactions.id", ondelete="RESTRICT"),
        nullable=True,
        unique=True,
    )
    # TX that finalized the withdrawal (debit hold on paid / refund hold on rejected)
    wallet_complete_tx_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("wallet_transactions.id", ondelete="RESTRICT"),
        nullable=True,
        unique=True,
    )

    # ── Idempotency ──────────────────────────────────────────────────────────
    idempotency_key: Mapped[str | None] = mapped_column(
        String(200), nullable=True, index=True
    )

    # ── Metadata ─────────────────────────────────────────────────────────────
    meta: Mapped[dict[str, Any] | None] = mapped_column(_jsonb, nullable=True)

    # ── Timestamps ───────────────────────────────────────────────────────────
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        server_default=text("now()"),
        index=True,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        server_default=text("now()"),
    )

    # ── Relationships ─────────────────────────────────────────────────────────
    user: Mapped["User"] = relationship("User")  # type: ignore[name-defined]
    processed_by: Mapped["StaffUser | None"] = relationship(  # type: ignore[name-defined]
        "StaffUser", foreign_keys=[processed_by_id]
    )

    def __repr__(self) -> str:
        return (
            f"<Withdrawal id={self.id} user={self.user_id} "
            f"amount={self.amount} status={self.status}>"
        )
