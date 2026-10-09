"""Tournament Engine — Phase 3

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-12

New tables:
  tournaments          — tournament lifecycle, configuration, creator tracking
  tournament_entries   — player enrollments with full payment breakdown snapshot

New ENUM values stored as VARCHAR (native_enum=False in ORM):
  tournament_status_enum : draft, open, confirmed, room_released, live,
                           result_entry, completed, cancelled
  tournament_mode_enum   : solo
  tournament_game_enum   : free_fire
  entry_status_enum      : active, refunded, disqualified

No changes to existing tables.
Previous migrations (0001, 0002) are unaffected.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── tournaments ───────────────────────────────────────────────────────────
    op.create_table(
        "tournaments",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("tournament_code", sa.String(20), nullable=False),
        sa.Column("title", sa.String(120), nullable=False),
        sa.Column(
            "game",
            sa.String(20),
            nullable=False,
            server_default=sa.text("'free_fire'"),
        ),
        sa.Column(
            "mode",
            sa.String(20),
            nullable=False,
            server_default=sa.text("'solo'"),
        ),
        sa.Column("entry_fee", sa.Numeric(10, 2), nullable=False),
        sa.Column(
            "max_players",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("50"),
        ),
        sa.Column(
            "min_players",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("40"),
        ),
        # Running counter of ACTIVE entries — updated atomically on join/refund
        sa.Column(
            "player_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        # Next sheet number to assign — starts at 1, incremented under FOR UPDATE
        sa.Column(
            "next_sheet_number",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column(
            "status",
            sa.String(20),
            nullable=False,
            server_default=sa.text("'draft'"),
        ),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False),
        # Room credentials — nullable until released
        sa.Column("room_id", sa.String(50), nullable=True),
        sa.Column("room_password", sa.String(50), nullable=True),
        sa.Column("room_released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "room_released_by_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
        # Lifecycle timestamps
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancellation_reason", sa.Text(), nullable=True),
        # Creator tracking — immutable snapshot at creation time
        sa.Column(
            "created_by_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("created_by_display_name", sa.String(100), nullable=False),
        sa.Column("created_by_role", sa.String(20), nullable=False),
        # Completion / cancellation actor references
        sa.Column(
            "completed_by_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
        sa.Column(
            "cancelled_by_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
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
        # ── Constraints ───────────────────────────────────────────────────────
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tournament_code", name="uq_tournament_code"),
        sa.ForeignKeyConstraint(
            ["created_by_id"], ["staff_users.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["room_released_by_id"], ["staff_users.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["completed_by_id"], ["staff_users.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["cancelled_by_id"], ["staff_users.id"], ondelete="SET NULL"
        ),
        sa.CheckConstraint("entry_fee > 0", name="ck_tournament_entry_fee_positive"),
        sa.CheckConstraint(
            "max_players > 0", name="ck_tournament_max_players_positive"
        ),
        sa.CheckConstraint(
            "min_players <= max_players", name="ck_tournament_min_le_max"
        ),
        sa.CheckConstraint(
            "player_count >= 0", name="ck_tournament_player_count_non_negative"
        ),
        sa.CheckConstraint(
            "next_sheet_number >= 1", name="ck_tournament_next_sheet_positive"
        ),
    )
    op.create_index("ix_tournaments_tournament_code", "tournaments", ["tournament_code"])
    op.create_index("ix_tournaments_status", "tournaments", ["status"])
    op.create_index("ix_tournaments_scheduled_at", "tournaments", ["scheduled_at"])
    op.create_index("ix_tournaments_created_by_id", "tournaments", ["created_by_id"])
    op.create_index(
        "ix_tournaments_created_at",
        "tournaments",
        ["created_at"],
        postgresql_ops={"created_at": "DESC"},
    )

    # ── tournament_entries ────────────────────────────────────────────────────
    op.create_table(
        "tournament_entries",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column(
            "tournament_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sheet_number", sa.Integer(), nullable=False),
        # Payment snapshot — immutable after join
        sa.Column("entry_fee_snapshot", sa.Numeric(10, 2), nullable=False),
        sa.Column(
            "deposit_amount_used",
            sa.Numeric(10, 2),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "referral_amount_used",
            sa.Numeric(10, 2),
            nullable=False,
            server_default=sa.text("0"),
        ),
        # Status
        sa.Column(
            "status",
            sa.String(20),
            nullable=False,
            server_default=sa.text("'active'"),
        ),
        # Refund tracking
        sa.Column("refunded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "refund_deposit_tx_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
        sa.Column(
            "refund_referral_tx_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
        # Timestamps
        sa.Column(
            "joined_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        # ── Constraints ───────────────────────────────────────────────────────
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tournament_id", "user_id", name="uq_tournament_entry_user"
        ),
        sa.UniqueConstraint(
            "tournament_id", "sheet_number", name="uq_tournament_entry_sheet"
        ),
        sa.ForeignKeyConstraint(
            ["tournament_id"], ["tournaments.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], ondelete="RESTRICT"
        ),
        sa.CheckConstraint(
            "sheet_number >= 1", name="ck_entry_sheet_number_positive"
        ),
        sa.CheckConstraint(
            "entry_fee_snapshot > 0", name="ck_entry_fee_snapshot_positive"
        ),
        sa.CheckConstraint(
            "deposit_amount_used >= 0", name="ck_entry_deposit_non_negative"
        ),
        sa.CheckConstraint(
            "referral_amount_used >= 0", name="ck_entry_referral_non_negative"
        ),
    )
    op.create_index(
        "ix_tournament_entries_tournament_id",
        "tournament_entries",
        ["tournament_id"],
    )
    op.create_index(
        "ix_tournament_entries_user_id", "tournament_entries", ["user_id"]
    )
    op.create_index(
        "ix_tournament_entries_status", "tournament_entries", ["status"]
    )
    op.create_index(
        "ix_tournament_entries_joined_at",
        "tournament_entries",
        ["joined_at"],
        postgresql_ops={"joined_at": "DESC"},
    )
    # Composite index for "has this user joined this tournament?" lookup
    op.create_index(
        "ix_tournament_entries_tournament_user",
        "tournament_entries",
        ["tournament_id", "user_id"],
    )


def downgrade() -> None:
    # Drop entries first (FK dependency)
    op.drop_index("ix_tournament_entries_tournament_user", "tournament_entries")
    op.drop_index("ix_tournament_entries_joined_at", "tournament_entries")
    op.drop_index("ix_tournament_entries_status", "tournament_entries")
    op.drop_index("ix_tournament_entries_user_id", "tournament_entries")
    op.drop_index("ix_tournament_entries_tournament_id", "tournament_entries")
    op.drop_table("tournament_entries")

    op.drop_index("ix_tournaments_created_at", "tournaments")
    op.drop_index("ix_tournaments_created_by_id", "tournaments")
    op.drop_index("ix_tournaments_scheduled_at", "tournaments")
    op.drop_index("ix_tournaments_status", "tournaments")
    op.drop_index("ix_tournaments_tournament_code", "tournaments")
    op.drop_table("tournaments")
