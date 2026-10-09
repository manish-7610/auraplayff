"""Initial schema — Phase 1 tables

Revision ID: 0001
Revises:
Create Date: 2026-09-11

Tables created:
  users, staff_users, permissions, staff_user_permissions,
  wallets, wallet_transactions, player_stats,
  password_reset_tokens, refresh_tokens, audit_logs
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers
revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── Custom ENUM types ─────────────────────────────────────────────────────
    staff_role_enum = postgresql.ENUM(
        "super_admin", "agent", name="staff_role_enum", create_type=False
    )
    staff_role_enum.create(op.get_bind(), checkfirst=True)

    transaction_type_enum = postgresql.ENUM(
        "deposit", "withdrawal", "prize", "kill_reward", "referral_bonus",
        "entry_fee", "refund", "penalty", "adjustment",
        name="transaction_type_enum", create_type=False
    )
    transaction_type_enum.create(op.get_bind(), checkfirst=True)

    balance_column_enum = postgresql.ENUM(
        "deposit", "winning", "referral",
        name="balance_column_enum", create_type=False
    )
    balance_column_enum.create(op.get_bind(), checkfirst=True)

    transaction_direction_enum = postgresql.ENUM(
        "credit", "debit",
        name="transaction_direction_enum", create_type=False
    )
    transaction_direction_enum.create(op.get_bind(), checkfirst=True)

    actor_type_enum = postgresql.ENUM(
        "user", "staff", "system",
        name="actor_type_enum", create_type=False
    )
    actor_type_enum.create(op.get_bind(), checkfirst=True)

    # ── users ─────────────────────────────────────────────────────────────────
    op.create_table(
        "users",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("username", sa.String(30), nullable=False),
        sa.Column("ff_uid", sa.String(20), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("referral_code", sa.String(10), nullable=False),
        sa.Column(
            "referred_by_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
        sa.Column(
            "referral_rewarded",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "token_version",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("avatar_url", sa.String(500), nullable=True),
        sa.Column(
            "is_active",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column(
            "is_banned",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["referred_by_id"], ["users.id"], ondelete="SET NULL"
        ),
        sa.UniqueConstraint("username", name="uq_users_username"),
        sa.UniqueConstraint("ff_uid", name="uq_users_ff_uid"),
        sa.UniqueConstraint("referral_code", name="uq_users_referral_code"),
    )
    op.create_index("ix_users_username", "users", ["username"])
    op.create_index("ix_users_ff_uid", "users", ["ff_uid"])
    op.create_index("ix_users_referral_code", "users", ["referral_code"])
    op.create_index("ix_users_referred_by_id", "users", ["referred_by_id"])
    op.create_index("ix_users_last_seen_at", "users", ["last_seen_at"])

    # ── staff_users ───────────────────────────────────────────────────────────
    op.create_table(
        "staff_users",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("agent_id", sa.String(20), nullable=False),
        sa.Column("display_name", sa.String(100), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column(
            "role",
            postgresql.ENUM("super_admin", "agent", name="staff_role_enum", create_type=False),
            nullable=False,
        ),
        sa.Column(
            "token_version",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "is_active",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column(
            "created_by_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["created_by_id"], ["staff_users.id"], ondelete="SET NULL"
        ),
        sa.UniqueConstraint("agent_id", name="uq_staff_users_agent_id"),
    )
    op.create_index("ix_staff_users_agent_id", "staff_users", ["agent_id"])

    # ── permissions ───────────────────────────────────────────────────────────
    op.create_table(
        "permissions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("code", sa.String(80), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code", name="uq_permissions_code"),
    )
    op.create_index("ix_permissions_code", "permissions", ["code"])

    # ── staff_user_permissions ────────────────────────────────────────────────
    op.create_table(
        "staff_user_permissions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("staff_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("permission_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("granted_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "granted_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["staff_user_id"], ["staff_users.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["permission_id"], ["permissions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["granted_by_id"], ["staff_users.id"], ondelete="SET NULL"
        ),
        sa.UniqueConstraint(
            "staff_user_id", "permission_id", name="uq_staff_permission"
        ),
    )
    op.create_index(
        "ix_staff_user_permissions_staff_user_id",
        "staff_user_permissions",
        ["staff_user_id"],
    )

    # ── wallets ───────────────────────────────────────────────────────────────
    op.create_table(
        "wallets",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "deposit_balance",
            sa.Numeric(12, 2),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "winning_balance",
            sa.Numeric(12, 2),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "referral_balance",
            sa.Numeric(12, 2),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "withdrawal_hold",
            sa.Numeric(12, 2),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("user_id", name="uq_wallets_user_id"),
        sa.CheckConstraint("deposit_balance >= 0", name="ck_wallet_deposit_balance"),
        sa.CheckConstraint("winning_balance >= 0", name="ck_wallet_winning_balance"),
        sa.CheckConstraint("referral_balance >= 0", name="ck_wallet_referral_balance"),
        sa.CheckConstraint("withdrawal_hold >= 0", name="ck_wallet_withdrawal_hold"),
    )
    op.create_index("ix_wallets_user_id", "wallets", ["user_id"])

    # ── wallet_transactions ───────────────────────────────────────────────────
    op.create_table(
        "wallet_transactions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("wallet_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "type",
            postgresql.ENUM(
                "deposit", "withdrawal", "prize", "kill_reward", "referral_bonus",
                "entry_fee", "refund", "penalty", "adjustment",
                name="transaction_type_enum",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "balance_column",
            postgresql.ENUM("deposit", "winning", "referral", name="balance_column_enum", create_type=False),
            nullable=False,
        ),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column(
            "direction",
            postgresql.ENUM("credit", "debit", name="transaction_direction_enum", create_type=False),
            nullable=False,
        ),
        sa.Column("balance_after", sa.Numeric(12, 2), nullable=False),
        sa.Column("reference_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("reference_type", sa.String(40), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["wallet_id"], ["wallets.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"], ["staff_users.id"], ondelete="SET NULL"
        ),
    )
    op.create_index(
        "ix_wallet_transactions_wallet_id", "wallet_transactions", ["wallet_id"]
    )
    op.create_index(
        "ix_wallet_transactions_user_id", "wallet_transactions", ["user_id"]
    )
    op.create_index(
        "ix_wallet_transactions_created_at",
        "wallet_transactions",
        ["created_at"],
        postgresql_ops={"created_at": "DESC"},
    )

    # ── player_stats ──────────────────────────────────────────────────────────
    op.create_table(
        "player_stats",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "total_tournaments",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "total_wins",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "total_kills",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "total_winnings",
            sa.Numeric(12, 2),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("user_id", name="uq_player_stats_user_id"),
    )
    op.create_index("ix_player_stats_user_id", "player_stats", ["user_id"])

    # ── password_reset_tokens ─────────────────────────────────────────────────
    op.create_table(
        "password_reset_tokens",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "is_active",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column("issued_by_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["issued_by_id"], ["staff_users.id"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint("token_hash", name="uq_password_reset_tokens_hash"),
    )
    op.create_index(
        "ix_password_reset_tokens_user_id", "password_reset_tokens", ["user_id"]
    )

    # ── refresh_tokens ────────────────────────────────────────────────────────
    op.create_table(
        "refresh_tokens",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("staff_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("token_version", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "is_revoked",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("user_agent", sa.String(500), nullable=True),
        sa.Column("ip_address", sa.String(45), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["staff_user_id"], ["staff_users.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint("token_hash", name="uq_refresh_tokens_hash"),
    )
    op.create_index("ix_refresh_tokens_user_id", "refresh_tokens", ["user_id"])
    op.create_index(
        "ix_refresh_tokens_staff_user_id", "refresh_tokens", ["staff_user_id"]
    )
    op.create_index(
        "ix_refresh_tokens_expires_at", "refresh_tokens", ["expires_at"]
    )

    # ── audit_logs ────────────────────────────────────────────────────────────
    op.create_table(
        "audit_logs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column(
            "actor_type",
            postgresql.ENUM("user", "staff", "system", name="actor_type_enum", create_type=False),
            nullable=False,
        ),
        sa.Column("actor_id", sa.String(80), nullable=False),
        sa.Column("action", sa.String(100), nullable=False),
        sa.Column("entity_type", sa.String(60), nullable=False),
        sa.Column("entity_id", sa.String(80), nullable=False),
        sa.Column("old_value", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("new_value", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("ip_address", sa.String(45), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audit_logs_actor_id", "audit_logs", ["actor_id"])
    op.create_index("ix_audit_logs_action", "audit_logs", ["action"])
    op.create_index("ix_audit_logs_entity_type", "audit_logs", ["entity_type"])
    op.create_index("ix_audit_logs_entity_id", "audit_logs", ["entity_id"])
    op.create_index(
        "ix_audit_logs_created_at",
        "audit_logs",
        ["created_at"],
        postgresql_ops={"created_at": "DESC"},
    )


def downgrade() -> None:
    # Drop tables in reverse FK order
    op.drop_table("audit_logs")
    op.drop_table("refresh_tokens")
    op.drop_table("password_reset_tokens")
    op.drop_table("player_stats")
    op.drop_table("wallet_transactions")
    op.drop_table("wallets")
    op.drop_table("staff_user_permissions")
    op.drop_table("permissions")
    op.drop_table("staff_users")
    op.drop_table("users")

    # Drop custom enum types
    op.execute("DROP TYPE IF EXISTS actor_type_enum")
    op.execute("DROP TYPE IF EXISTS transaction_direction_enum")
    op.execute("DROP TYPE IF EXISTS balance_column_enum")
    op.execute("DROP TYPE IF EXISTS transaction_type_enum")
    op.execute("DROP TYPE IF EXISTS staff_role_enum")
