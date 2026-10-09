"""
Wallet and WalletTransaction models.
WalletTransactions are IMMUTABLE (INSERT only).

Idempotency:
  WalletTransaction.idempotency_key is a caller-supplied unique string that
  prevents double-credits/debits caused by retried requests.  The service
  layer checks for an existing transaction with the same key before inserting.
  Convention:  "<context>:<reference_id>"
  Examples:    "referral_bonus:referral_relationships/<uuid>"
               "tournament_entry:<tournament_id>:<user_id>"
               "admin_adjust:<audit_log_id>"

Metadata:
  WalletTransaction.meta (JSONB) stores extra context that does not fit in the
  fixed columns — e.g. tournament name, kill count, payment gateway order id.
  Kept nullable so existing records are not affected.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

import enum as pyenum

from sqlalchemy import (
    Boolean, CheckConstraint, DateTime, Enum, ForeignKey,
    JSON, Numeric, String, Text, text, UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from typing import Any

from app.database import Base
from app.models._types import utcnow

# Cross-dialect: JSONB on PostgreSQL, JSON on SQLite (tests)
_jsonb = JSONB().with_variant(JSON(), "sqlite")


class TransactionType(str, pyenum.Enum):
    # ── Money in ──────────────────────────────────────────────────────────────
    deposit = "deposit"                     # User deposits real money
    referral_bonus = "referral_bonus"       # ₹20 referral reward (non-withdrawable)
    prize = "prize"                         # Tournament prize payout
    kill_reward = "kill_reward"             # Per-kill reward payout
    # ── Money out ────────────────────────────────────────────────────────────
    entry_fee = "entry_fee"                 # Tournament entry deduction
    withdrawal_request = "withdrawal_request"   # Funds moved to withdrawal hold
    withdrawal_completed = "withdrawal_completed"  # Hold released, paid out
    withdrawal_rejected = "withdrawal_rejected"    # Hold returned to deposit balance
    withdrawal_refund = "withdrawal_refund"        # Manual reversal of withdrawal hold
    # ── Reversals ────────────────────────────────────────────────────────────
    entry_fee_refund = "entry_fee_refund"   # Refund when tournament cancelled
    # ── Admin ─────────────────────────────────────────────────────────────────
    admin_adjustment = "admin_adjustment"   # Controlled manual adjustment by staff
    penalty = "penalty"                     # Punitive deduction by staff


class BalanceColumn(str, pyenum.Enum):
    deposit = "deposit"    # Withdrawable real-money balance
    winning = "winning"    # Tournament prize / kill-reward (also withdrawable)
    referral = "referral"  # Non-withdrawable referral bonus balance
    # BUG #4 FIX: withdrawal_hold is a distinct balance tracked on the wallet
    # but was previously recorded in ledger entries as BalanceColumn.deposit
    # (placeholder).  It now has its own enum value so ledger queries and
    # financial reports correctly distinguish hold entries from deposit entries.
    hold = "hold"          # Withdrawal hold — reserved from winning_balance


class TransactionDirection(str, pyenum.Enum):
    credit = "credit"
    debit = "debit"


class Wallet(Base):
    __tablename__ = "wallets"
    __table_args__ = (
        CheckConstraint("deposit_balance >= 0", name="ck_wallet_deposit_balance"),
        CheckConstraint("winning_balance >= 0", name="ck_wallet_winning_balance"),
        CheckConstraint("referral_balance >= 0", name="ck_wallet_referral_balance"),
        CheckConstraint("withdrawal_hold >= 0", name="ck_wallet_withdrawal_hold"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        unique=True, nullable=False, index=True,
    )
    deposit_balance: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, default=Decimal("0.00"), server_default=text("0")
    )
    winning_balance: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, default=Decimal("0.00"), server_default=text("0")
    )
    referral_balance: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, default=Decimal("0.00"), server_default=text("0")
    )
    withdrawal_hold: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, default=Decimal("0.00"), server_default=text("0")
    )
    # Freeze flag — set by admin to block all transactions (e.g. fraud investigation)
    is_frozen: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        server_default=text("now()"),
    )

    user: Mapped["User"] = relationship("User", back_populates="wallet")
    transactions: Mapped[list["WalletTransaction"]] = relationship(
        "WalletTransaction", back_populates="wallet", lazy="select"
    )


class WalletTransaction(Base):
    """
    Immutable ledger record — never UPDATE or DELETE.

    idempotency_key:
        Unique string supplied by the caller to prevent double-writes on retry.
        Convention: "<context>:<reference_id>"
        The UNIQUE constraint on this column is the final DB-level guard.
        Callers must catch IntegrityError and treat it as "already processed".
    """
    __tablename__ = "wallet_transactions"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_wallet_tx_idempotency_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    wallet_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("wallets.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    type: Mapped[TransactionType] = mapped_column(
        Enum(TransactionType, name="transaction_type_enum", native_enum=False),
        nullable=False,
        index=True,
    )
    balance_column: Mapped[BalanceColumn] = mapped_column(
        Enum(BalanceColumn, name="balance_column_enum", native_enum=False),
        nullable=False,
    )
    direction: Mapped[TransactionDirection] = mapped_column(
        Enum(TransactionDirection, name="transaction_direction_enum", native_enum=False),
        nullable=False,
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    # Snapshot of the balance immediately before this transaction
    balance_before: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    # Snapshot of the balance immediately after this transaction
    balance_after: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    # Prevents duplicate transactions from retried/replayed requests
    idempotency_key: Mapped[str | None] = mapped_column(
        String(200), nullable=True, index=True
    )
    reference_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True, index=True)
    reference_type: Mapped[str | None] = mapped_column(String(60), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Flexible metadata — tournament name, gateway order id, kill count, etc.
    meta: Mapped[dict[str, Any] | None] = mapped_column(_jsonb, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        server_default=text("now()"),
        index=True,
    )
    # Staff user who triggered this transaction (None for system/player actions)
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("staff_users.id", ondelete="SET NULL"), nullable=True
    )

    wallet: Mapped[Wallet] = relationship("Wallet", back_populates="transactions")
