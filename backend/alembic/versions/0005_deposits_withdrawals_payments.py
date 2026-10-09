"""Deposits, Withdrawals, Provider Events, Platform Settings — Phase 5

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-13

New tables
----------
  deposits            — online (Razorpay) and manual (UPI) deposit records
  withdrawals         — player withdrawal requests with reservation accounting
  provider_events     — immutable webhook event log for idempotency
  platform_settings   — key/value operational configuration

No changes to existing Phase 1–4 tables.

Enum values are stored as VARCHAR (native_enum=False in ORM) so no
ALTER TYPE is needed when new values are added in future migrations.

Key constraints
---------------
  deposits.provider_payment_id              UNIQUE (prevents double webhook credit)
  deposits.wallet_tx_id                     UNIQUE (one wallet credit per deposit)
  deposits.utr_normalized + partial index   unique where status = 'approved'
  withdrawals.idempotency_key               UNIQUE
  withdrawals.wallet_reserve_tx_id          UNIQUE
  withdrawals.wallet_complete_tx_id         UNIQUE
  provider_events.(provider, event_id)      UNIQUE (idempotency guard)
  platform_settings.key                     UNIQUE

Notes
-----
  - withdrawal_hold column already exists on wallets table (added in 0001).
    No wallet table changes needed.
  - All Numeric columns use (12, 2) for INR amounts.
  - Partial unique index on utr_normalized is created with a WHERE clause
    so the same UTR can appear in multiple rejected/pending rows but only
    once in an approved row.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── deposits ──────────────────────────────────────────────────────────────
    op.create_table(
        "deposits",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("method", sa.String(20), nullable=False),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False, server_default="INR"),
        # Online (Razorpay) fields
        sa.Column("provider", sa.String(40), nullable=True),
        sa.Column("provider_order_id", sa.String(100), nullable=True),
        sa.Column("provider_payment_id", sa.String(100), nullable=True),
        sa.Column("provider_event_id", sa.String(100), nullable=True),
        # Manual UPI fields
        sa.Column("utr", sa.String(100), nullable=True),
        sa.Column("utr_normalized", sa.String(100), nullable=True),
        sa.Column("payer_info", sa.Text, nullable=True),
        # Staff review fields
        sa.Column("admin_note", sa.Text, nullable=True),
        sa.Column("verified_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        # Wallet linkage
        sa.Column("wallet_tx_id", postgresql.UUID(as_uuid=True), nullable=True),
        # Metadata
        sa.Column("meta", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        # Timestamps
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        # Primary key
        sa.PrimaryKeyConstraint("id", name="pk_deposits"),
        # Check constraints
        sa.CheckConstraint("amount > 0", name="ck_deposit_amount_positive"),
        # Unique constraints
        sa.UniqueConstraint(
            "provider_payment_id",
            name="uq_deposit_provider_payment_id",
        ),
        sa.UniqueConstraint(
            "wallet_tx_id",
            name="uq_deposit_wallet_tx_id",
        ),
        # Foreign keys
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], ondelete="RESTRICT",
            name="fk_deposits_user_id",
        ),
        sa.ForeignKeyConstraint(
            ["verified_by_id"], ["staff_users.id"], ondelete="SET NULL",
            name="fk_deposits_verified_by_id",
        ),
        sa.ForeignKeyConstraint(
            ["wallet_tx_id"], ["wallet_transactions.id"], ondelete="RESTRICT",
            name="fk_deposits_wallet_tx_id",
        ),
    )

    # Indexes on deposits
    op.create_index("ix_deposits_user_id", "deposits", ["user_id"])
    op.create_index("ix_deposits_status", "deposits", ["status"])
    op.create_index("ix_deposits_method", "deposits", ["method"])
    op.create_index("ix_deposits_provider_order_id", "deposits", ["provider_order_id"])
    op.create_index("ix_deposits_provider_payment_id", "deposits", ["provider_payment_id"])
    op.create_index("ix_deposits_utr_normalized", "deposits", ["utr_normalized"])
    op.create_index("ix_deposits_created_at", "deposits", ["created_at"])

    # Partial unique index: same UTR can appear in pending/rejected rows,
    # but only ONE approved deposit per UTR.
    op.execute(
        """
        CREATE UNIQUE INDEX uix_deposits_utr_approved
        ON deposits (utr_normalized)
        WHERE status = 'approved'
        """
    )

    # ── withdrawals ───────────────────────────────────────────────────────────
    op.create_table(
        "withdrawals",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False, server_default="INR"),
        # Player-supplied destination
        sa.Column("upi_id", sa.String(100), nullable=False),
        # Admin payout fields
        sa.Column("payout_utr", sa.String(100), nullable=True),
        sa.Column("payout_note", sa.Text, nullable=True),
        sa.Column("processed_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        # Wallet ledger linkage
        sa.Column("wallet_reserve_tx_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("wallet_complete_tx_id", postgresql.UUID(as_uuid=True), nullable=True),
        # Idempotency
        sa.Column("idempotency_key", sa.String(200), nullable=True),
        # Metadata
        sa.Column("meta", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        # Timestamps
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        # Primary key
        sa.PrimaryKeyConstraint("id", name="pk_withdrawals"),
        # Check constraints
        sa.CheckConstraint("amount > 0", name="ck_withdrawal_amount_positive"),
        # Unique constraints
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_withdrawal_idempotency_key",
        ),
        sa.UniqueConstraint(
            "wallet_reserve_tx_id",
            name="uq_withdrawal_wallet_reserve_tx_id",
        ),
        sa.UniqueConstraint(
            "wallet_complete_tx_id",
            name="uq_withdrawal_wallet_complete_tx_id",
        ),
        # Foreign keys
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], ondelete="RESTRICT",
            name="fk_withdrawals_user_id",
        ),
        sa.ForeignKeyConstraint(
            ["processed_by_id"], ["staff_users.id"], ondelete="SET NULL",
            name="fk_withdrawals_processed_by_id",
        ),
        sa.ForeignKeyConstraint(
            ["wallet_reserve_tx_id"], ["wallet_transactions.id"], ondelete="RESTRICT",
            name="fk_withdrawals_wallet_reserve_tx_id",
        ),
        sa.ForeignKeyConstraint(
            ["wallet_complete_tx_id"], ["wallet_transactions.id"], ondelete="RESTRICT",
            name="fk_withdrawals_wallet_complete_tx_id",
        ),
    )

    # Indexes on withdrawals
    op.create_index("ix_withdrawals_user_id", "withdrawals", ["user_id"])
    op.create_index("ix_withdrawals_status", "withdrawals", ["status"])
    op.create_index("ix_withdrawals_created_at", "withdrawals", ["created_at"])
    op.create_index("ix_withdrawals_processed_by_id", "withdrawals", ["processed_by_id"])
    op.create_index("ix_withdrawals_idempotency_key", "withdrawals", ["idempotency_key"])

    # ── provider_events ───────────────────────────────────────────────────────
    op.create_table(
        "provider_events",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("provider", sa.String(40), nullable=False),
        sa.Column("event_id", sa.String(200), nullable=False),
        sa.Column("event_type", sa.String(100), nullable=False),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("processing_status", sa.String(20), nullable=False),
        sa.Column("deposit_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("raw_event", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error_detail", sa.Text, nullable=True),
        # Primary key
        sa.PrimaryKeyConstraint("id", name="pk_provider_events"),
        # Unique: one record per provider+event_id pair
        sa.UniqueConstraint(
            "provider", "event_id",
            name="uq_provider_event_provider_event_id",
        ),
        # Foreign key
        sa.ForeignKeyConstraint(
            ["deposit_id"], ["deposits.id"], ondelete="SET NULL",
            name="fk_provider_events_deposit_id",
        ),
    )

    # Indexes on provider_events
    op.create_index("ix_provider_events_provider", "provider_events", ["provider"])
    op.create_index("ix_provider_events_event_id", "provider_events", ["event_id"])
    op.create_index("ix_provider_events_event_type", "provider_events", ["event_type"])
    op.create_index("ix_provider_events_received_at", "provider_events", ["received_at"])
    op.create_index("ix_provider_events_deposit_id", "provider_events", ["deposit_id"])

    # ── platform_settings ─────────────────────────────────────────────────────
    op.create_table(
        "platform_settings",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("key", sa.String(100), nullable=False),
        sa.Column("value", sa.Text, nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column(
            "is_public",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("updated_by", sa.String(80), nullable=True),
        # Primary key
        sa.PrimaryKeyConstraint("id", name="pk_platform_settings"),
        # Unique: one row per key
        sa.UniqueConstraint("key", name="uq_platform_settings_key"),
    )

    op.create_index("ix_platform_settings_key", "platform_settings", ["key"])


def downgrade() -> None:
    # Drop in reverse dependency order
    op.drop_table("platform_settings")

    op.drop_index("ix_provider_events_deposit_id", table_name="provider_events")
    op.drop_index("ix_provider_events_received_at", table_name="provider_events")
    op.drop_index("ix_provider_events_event_type", table_name="provider_events")
    op.drop_index("ix_provider_events_event_id", table_name="provider_events")
    op.drop_index("ix_provider_events_provider", table_name="provider_events")
    op.drop_table("provider_events")

    op.drop_index("ix_withdrawals_idempotency_key", table_name="withdrawals")
    op.drop_index("ix_withdrawals_processed_by_id", table_name="withdrawals")
    op.drop_index("ix_withdrawals_created_at", table_name="withdrawals")
    op.drop_index("ix_withdrawals_status", table_name="withdrawals")
    op.drop_index("ix_withdrawals_user_id", table_name="withdrawals")
    op.drop_table("withdrawals")

    op.execute("DROP INDEX IF EXISTS uix_deposits_utr_approved")
    op.drop_index("ix_deposits_created_at", table_name="deposits")
    op.drop_index("ix_deposits_utr_normalized", table_name="deposits")
    op.drop_index("ix_deposits_provider_payment_id", table_name="deposits")
    op.drop_index("ix_deposits_provider_order_id", table_name="deposits")
    op.drop_index("ix_deposits_method", table_name="deposits")
    op.drop_index("ix_deposits_status", table_name="deposits")
    op.drop_index("ix_deposits_user_id", table_name="deposits")
    op.drop_table("deposits")
