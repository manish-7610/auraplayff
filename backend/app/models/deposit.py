"""
Deposit model — tracks every deposit attempt (online Razorpay + manual UPI).

State machine
-------------
Online (Razorpay):
  PENDING  →  PAID      (webhook/verify confirms capture)
  PENDING  →  FAILED    (webhook reports failure)
  PENDING  →  EXPIRED   (order TTL passed without payment)

Manual (UPI / QR):
  PENDING_MANUAL_VERIFICATION  →  APPROVED  (staff approves UTR)
  PENDING_MANUAL_VERIFICATION  →  REJECTED  (staff rejects UTR)

Rules
-----
- PAID / APPROVED must never transition to FAILED.
- Wallet credit happens ONCE inside the same DB transaction that sets status=PAID/APPROVED.
- wallet_tx_id links to the WalletTransaction that credited the balance.
- provider_payment_id + unique constraint prevents double-credit on webhooks.
- utr_normalized + partial unique constraint prevents duplicate UTR approvals.
"""
from __future__ import annotations

import enum as pyenum
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean, CheckConstraint, DateTime, Enum, ForeignKey,
    Index, JSON, Numeric, String, Text, UniqueConstraint, text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models._types import utcnow

_jsonb = JSONB().with_variant(JSON(), "sqlite")


class DepositMethod(str, pyenum.Enum):
    online = "online"   # Razorpay / payment gateway
    manual = "manual"   # Manual UPI / QR scan


class DepositStatus(str, pyenum.Enum):
    # Online deposit states
    pending = "pending"
    paid = "paid"
    failed = "failed"
    expired = "expired"
    # Manual deposit states
    pending_manual_verification = "pending_manual_verification"
    approved = "approved"
    rejected = "rejected"

    @classmethod
    def terminal_states(cls) -> frozenset["DepositStatus"]:
        """States from which no further transition is allowed."""
        return frozenset({cls.paid, cls.approved, cls.failed, cls.expired, cls.rejected})

    @classmethod
    def success_states(cls) -> frozenset["DepositStatus"]:
        """States that indicate a successful deposit (wallet already credited)."""
        return frozenset({cls.paid, cls.approved})


class Deposit(Base):
    """
    One deposit attempt. Immutable after reaching a terminal state.

    Columns
    -------
    method          : online | manual
    status          : see DepositStatus
    amount          : requested deposit amount (INR, 2 d.p.)
    currency        : always INR for now
    provider        : "razorpay" for online deposits, "" for manual
    provider_order_id    : Razorpay order ID (online only)
    provider_payment_id  : Razorpay payment ID set on capture (online only)
    provider_event_id    : Razorpay webhook event ID that triggered the update
    utr             : User-supplied UTR / transaction ID (manual only)
    utr_normalized  : Lowercase stripped UTR for uniqueness check (manual only)
    payer_info      : Optional JSON note from payer (manual only)
    admin_note      : Staff note on approval/rejection
    verified_by_id  : Staff user who approved/rejected (manual only)
    verified_at     : Timestamp of staff action
    wallet_tx_id    : FK to WalletTransaction created on credit
    meta            : Flexible JSONB for extra context
    """
    __tablename__ = "deposits"
    __table_args__ = (
        # Prevent crediting twice for the same provider payment
        UniqueConstraint(
            "provider_payment_id",
            name="uq_deposit_provider_payment_id",
        ),
        # Prevent same UTR being approved for multiple deposits
        # Partial index: only unique when status = 'approved'
        # Handled at DB level in the migration via a partial unique index.
        CheckConstraint("amount > 0", name="ck_deposit_amount_positive"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    method: Mapped[DepositMethod] = mapped_column(
        Enum(DepositMethod, name="deposit_method_enum", native_enum=False),
        nullable=False,
        index=True,
    )
    status: Mapped[DepositStatus] = mapped_column(
        Enum(DepositStatus, name="deposit_status_enum", native_enum=False),
        nullable=False,
        index=True,
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="INR")

    # ── Online payment fields ─────────────────────────────────────────────────
    provider: Mapped[str | None] = mapped_column(String(40), nullable=True)
    provider_order_id: Mapped[str | None] = mapped_column(
        String(100), nullable=True, index=True
    )
    provider_payment_id: Mapped[str | None] = mapped_column(
        String(100), nullable=True, index=True
    )
    provider_event_id: Mapped[str | None] = mapped_column(
        String(100), nullable=True
    )

    # ── Manual UPI fields ────────────────────────────────────────────────────
    utr: Mapped[str | None] = mapped_column(String(100), nullable=True)
    utr_normalized: Mapped[str | None] = mapped_column(
        String(100), nullable=True, index=True
    )
    payer_info: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ── Staff review fields ──────────────────────────────────────────────────
    admin_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    verified_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("staff_users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # ── Wallet linkage ───────────────────────────────────────────────────────
    wallet_tx_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("wallet_transactions.id", ondelete="RESTRICT"),
        nullable=True,
        unique=True,  # one deposit → at most one wallet credit
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
    verified_by: Mapped["StaffUser | None"] = relationship(  # type: ignore[name-defined]
        "StaffUser", foreign_keys=[verified_by_id]
    )

    def __repr__(self) -> str:
        return f"<Deposit id={self.id} user={self.user_id} amount={self.amount} status={self.status}>"
