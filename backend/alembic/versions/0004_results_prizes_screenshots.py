"""Results, Prizes, Screenshots — Phase 4

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-12

New tables:
  prize_configs          — configurable prize structure per entry-fee tier
  tournament_results     — per-player result with placement, kills, payout tracking
  screenshot_proofs      — tournament match evidence with Supabase storage metadata

No changes to existing Phase 1–3 tables.
Previous migrations (0001–0003) are unaffected.

Notes:
  - All enum values are stored as VARCHAR (native_enum=False in ORM models)
    so no ALTER TYPE is required when adding new values in future.
  - result_status_enum:    pending | entered | verified | finalized
  - proof_review_status_enum: pending | approved | rejected
  - kill_reward_top_n on prize_configs is stored as NUMERIC(4,0) to match
    the ORM mapped_column type; it behaves as a small integer.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── prize_configs ─────────────────────────────────────────────────────────
    op.create_table(
        "prize_configs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("entry_fee", sa.Numeric(10, 2), nullable=False),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "first_place_prize",
            sa.Numeric(12, 2),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "second_place_prize",
            sa.Numeric(12, 2),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "third_place_prize",
            sa.Numeric(12, 2),
            nullable=False,
            server_default=sa.text("0"),
        ),
        # kill_reward_top_n: small integer stored as NUMERIC(4,0)
        sa.Column(
            "kill_reward_top_n",
            sa.Numeric(4, 0),
            nullable=False,
            server_default=sa.text("10"),
        ),
        # kill_reward_tiers: JSON array of tier objects
        sa.Column(
            "kill_reward_tiers",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
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
        sa.Column("notes", sa.String(500), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("entry_fee", name="uq_prize_config_entry_fee"),
        sa.ForeignKeyConstraint(
            ["created_by_id"], ["staff_users.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"], ["staff_users.id"], ondelete="SET NULL"
        ),
    )
    op.create_index("ix_prize_configs_entry_fee", "prize_configs", ["entry_fee"])

    # ── tournament_results ────────────────────────────────────────────────────
    op.create_table(
        "tournament_results",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("tournament_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("entry_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ff_uid_snapshot", sa.String(20), nullable=False),
        # Result data
        sa.Column("placement", sa.Integer(), nullable=False),
        sa.Column("kills", sa.Integer(), nullable=False, server_default=sa.text("0")),
        # Payout snapshot
        sa.Column(
            "prize_amount",
            sa.Numeric(12, 2),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "kill_reward_amount",
            sa.Numeric(12, 2),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "kill_reward_eligible",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("kill_reward_breakdown", sa.String(200), nullable=True),
        # Payout tracking
        sa.Column(
            "payout_distributed",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("payout_distributed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "payout_tx_prize_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
        sa.Column(
            "payout_tx_kill_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
        # Workflow
        sa.Column(
            "result_status",
            sa.String(20),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column("entered_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("entered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verified_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.UniqueConstraint("entry_id", name="uq_result_entry"),
        sa.UniqueConstraint(
            "tournament_id", "placement", name="uq_result_tournament_placement"
        ),
        sa.ForeignKeyConstraint(
            ["tournament_id"], ["tournaments.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["entry_id"], ["tournament_entries.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["entered_by_id"], ["staff_users.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["verified_by_id"], ["staff_users.id"], ondelete="SET NULL"
        ),
        sa.CheckConstraint("placement >= 1", name="ck_result_placement_positive"),
        sa.CheckConstraint("kills >= 0", name="ck_result_kills_non_negative"),
        sa.CheckConstraint(
            "prize_amount >= 0", name="ck_result_prize_non_negative"
        ),
        sa.CheckConstraint(
            "kill_reward_amount >= 0", name="ck_result_kill_reward_non_negative"
        ),
    )
    op.create_index(
        "ix_tournament_results_tournament_id",
        "tournament_results",
        ["tournament_id"],
    )
    op.create_index(
        "ix_tournament_results_entry_id", "tournament_results", ["entry_id"]
    )
    op.create_index(
        "ix_tournament_results_user_id", "tournament_results", ["user_id"]
    )
    op.create_index(
        "ix_tournament_results_result_status",
        "tournament_results",
        ["result_status"],
    )
    op.create_index(
        "ix_tournament_results_payout_distributed",
        "tournament_results",
        ["payout_distributed"],
    )

    # ── screenshot_proofs ─────────────────────────────────────────────────────
    op.create_table(
        "screenshot_proofs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("tournament_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("entry_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        # Storage metadata
        sa.Column("storage_path", sa.String(500), nullable=False),
        sa.Column("original_filename", sa.String(255), nullable=False),
        sa.Column("content_type", sa.String(50), nullable=False),
        sa.Column("file_size_bytes", sa.Integer(), nullable=False),
        # Window timestamps
        sa.Column(
            "uploaded_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("upload_deadline", sa.DateTime(timezone=True), nullable=False),
        sa.Column("review_deadline", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        # Review
        sa.Column(
            "review_status",
            sa.String(20),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column("reviewed_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_notes", sa.String(500), nullable=True),
        # ── Constraints ───────────────────────────────────────────────────────
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["tournament_id"], ["tournaments.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["entry_id"], ["tournament_entries.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["reviewed_by_id"], ["staff_users.id"], ondelete="SET NULL"
        ),
        sa.CheckConstraint(
            "file_size_bytes > 0", name="ck_proof_file_size_positive"
        ),
    )
    op.create_index(
        "ix_screenshot_proofs_tournament_id",
        "screenshot_proofs",
        ["tournament_id"],
    )
    op.create_index(
        "ix_screenshot_proofs_entry_id", "screenshot_proofs", ["entry_id"]
    )
    op.create_index(
        "ix_screenshot_proofs_user_id", "screenshot_proofs", ["user_id"]
    )
    op.create_index(
        "ix_screenshot_proofs_uploaded_at",
        "screenshot_proofs",
        ["uploaded_at"],
        postgresql_ops={"uploaded_at": "DESC"},
    )
    op.create_index(
        "ix_screenshot_proofs_review_status",
        "screenshot_proofs",
        ["review_status"],
    )
    # Index for cleanup job: find expired proofs efficiently
    op.create_index(
        "ix_screenshot_proofs_expires_at",
        "screenshot_proofs",
        ["expires_at"],
    )


def downgrade() -> None:
    # Drop in reverse FK dependency order
    op.drop_index("ix_screenshot_proofs_expires_at", "screenshot_proofs")
    op.drop_index("ix_screenshot_proofs_review_status", "screenshot_proofs")
    op.drop_index("ix_screenshot_proofs_uploaded_at", "screenshot_proofs")
    op.drop_index("ix_screenshot_proofs_user_id", "screenshot_proofs")
    op.drop_index("ix_screenshot_proofs_entry_id", "screenshot_proofs")
    op.drop_index("ix_screenshot_proofs_tournament_id", "screenshot_proofs")
    op.drop_table("screenshot_proofs")

    op.drop_index("ix_tournament_results_payout_distributed", "tournament_results")
    op.drop_index("ix_tournament_results_result_status", "tournament_results")
    op.drop_index("ix_tournament_results_user_id", "tournament_results")
    op.drop_index("ix_tournament_results_entry_id", "tournament_results")
    op.drop_index("ix_tournament_results_tournament_id", "tournament_results")
    op.drop_table("tournament_results")

    op.drop_index("ix_prize_configs_entry_fee", "prize_configs")
    op.drop_table("prize_configs")
