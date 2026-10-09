"""Wallet + Referral Phase 2

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-12

Changes from Phase 1:
  wallets
    + is_frozen BOOLEAN NOT NULL DEFAULT false

  wallet_transactions
    + balance_before  NUMERIC(12,2) NOT NULL
    + idempotency_key VARCHAR(200)  UNIQUE nullable
    + meta            JSONB         nullable
    + index on type
    + index on reference_id
    (reference_type width widened from 40 → 60)

  TransactionType enum values added:
    withdrawal_request, withdrawal_completed, withdrawal_rejected,
    withdrawal_refund, entry_fee_refund, admin_adjustment
    (old values: deposit, withdrawal, prize, kill_reward,
     referral_bonus, entry_fee, refund, penalty, adjustment kept)

  NEW TABLE: referral_relationships

NOTE on enum migration:
  PostgreSQL native enums cannot be altered with ALTER TYPE inside a
  transaction in all versions.  We use native_enum=False (VARCHAR storage)
  in the ORM models, so the enum values are stored as plain strings —
  no ALTER TYPE needed; the new type values simply start being written.
  The existing enum TYPE objects created in 0001 are only used for the
  initial column declarations; adding new string values is transparent.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── wallets: add is_frozen ────────────────────────────────────────────────
    op.add_column(
        "wallets",
        sa.Column(
            "is_frozen",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )

    # ── wallet_transactions: add balance_before ───────────────────────────────
    # Existing rows get 0 as a safe placeholder; real data will have correct
    # values from this migration onwards.
    op.add_column(
        "wallet_transactions",
        sa.Column(
            "balance_before",
            sa.Numeric(12, 2),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )

    # ── wallet_transactions: add idempotency_key ──────────────────────────────
    op.add_column(
        "wallet_transactions",
        sa.Column("idempotency_key", sa.String(200), nullable=True),
    )
    op.create_unique_constraint(
        "uq_wallet_tx_idempotency_key",
        "wallet_transactions",
        ["idempotency_key"],
    )
    op.create_index(
        "ix_wallet_transactions_idempotency_key",
        "wallet_transactions",
        ["idempotency_key"],
    )

    # ── wallet_transactions: add meta (JSONB) ─────────────────────────────────
    op.add_column(
        "wallet_transactions",
        sa.Column(
            "meta",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )

    # ── wallet_transactions: widen reference_type 40 → 60 ────────────────────
    op.alter_column(
        "wallet_transactions",
        "reference_type",
        type_=sa.String(60),
        existing_nullable=True,
    )

    # ── wallet_transactions: add index on type ────────────────────────────────
    op.create_index(
        "ix_wallet_transactions_type",
        "wallet_transactions",
        ["type"],
    )

    # ── wallet_transactions: add index on reference_id ────────────────────────
    op.create_index(
        "ix_wallet_transactions_reference_id",
        "wallet_transactions",
        ["reference_id"],
    )

    # ── referral_relationships ────────────────────────────────────────────────
    op.create_table(
        "referral_relationships",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("referrer_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("referred_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("referral_code_used", sa.String(10), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        # First-tournament condition
        sa.Column(
            "first_tournament_completed",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "first_tournament_completed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        # Referrer reward tracking
        sa.Column(
            "referrer_rewarded",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "referrer_rewarded_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        # Referred user reward tracking
        sa.Column(
            "referred_user_rewarded",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "referred_user_rewarded_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        # Constraints
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["referrer_id"], ["users.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["referred_user_id"], ["users.id"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint(
            "referred_user_id", name="uq_referral_referred_user"
        ),
        sa.CheckConstraint(
            "referrer_id != referred_user_id",
            name="ck_referral_no_self_referral",
        ),
    )
    op.create_index(
        "ix_referral_relationships_referrer_id",
        "referral_relationships",
        ["referrer_id"],
    )
    op.create_index(
        "ix_referral_relationships_referred_user_id",
        "referral_relationships",
        ["referred_user_id"],
    )
    op.create_index(
        "ix_referral_relationships_created_at",
        "referral_relationships",
        ["created_at"],
        postgresql_ops={"created_at": "DESC"},
    )


def downgrade() -> None:
    # Drop referral_relationships
    op.drop_index("ix_referral_relationships_created_at", "referral_relationships")
    op.drop_index("ix_referral_relationships_referred_user_id", "referral_relationships")
    op.drop_index("ix_referral_relationships_referrer_id", "referral_relationships")
    op.drop_table("referral_relationships")

    # Revert wallet_transactions additions
    op.drop_index("ix_wallet_transactions_reference_id", "wallet_transactions")
    op.drop_index("ix_wallet_transactions_type", "wallet_transactions")
    op.drop_index("ix_wallet_transactions_idempotency_key", "wallet_transactions")
    op.drop_constraint(
        "uq_wallet_tx_idempotency_key", "wallet_transactions", type_="unique"
    )
    op.drop_column("wallet_transactions", "meta")
    op.drop_column("wallet_transactions", "idempotency_key")
    op.drop_column("wallet_transactions", "balance_before")
    op.alter_column(
        "wallet_transactions",
        "reference_type",
        type_=sa.String(40),
        existing_nullable=True,
    )

    # Revert wallets addition
    op.drop_column("wallets", "is_frozen")
