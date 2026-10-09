"""Agent Performance, Salary, Target Config — Phase 6

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-13

New tables
----------
  agent_target_configs       — configurable daily targets, bonus rates, base salary per tier
  agent_daily_performances   — per-agent, per-date, per-tier performance snapshot
  agent_graces               — explicit grace grants (immutable; one per daily_performance)
  agent_warnings             — warning records (second+ target miss)
  agent_salary_records       — monthly salary aggregation (draft → confirmed → paid)

No changes to existing Phase 1–5 tables.

Key constraints
---------------
  agent_target_configs.entry_fee                    UNIQUE (one config per tier)
  agent_daily_performances.(agent_id,perf_date,entry_fee)  UNIQUE
  agent_graces.daily_performance_id                 UNIQUE (one grace per perf row)
  agent_salary_records.(agent_id,year,month)        UNIQUE

Enum types stored as VARCHAR (native_enum=False) — no ALTER TYPE needed on additions.
All monetary columns use Numeric(12,2). No float columns.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── agent_target_configs ──────────────────────────────────────────────────
    op.create_table(
        "agent_target_configs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        # NULL entry_fee = special row for base_monthly_salary
        sa.Column("entry_fee", sa.Numeric(10, 2), nullable=True),
        sa.Column("label", sa.String(60), nullable=False, server_default=""),
        sa.Column("daily_required", sa.Integer, nullable=True),
        sa.Column("extra_bonus_per_tournament", sa.Numeric(10, 2), nullable=True),
        sa.Column("deduction_per_missing", sa.Numeric(10, 2), nullable=True),
        sa.Column("base_monthly_salary", sa.Numeric(12, 2), nullable=True),
        sa.Column(
            "is_active",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column("updated_by", sa.String(80), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_agent_target_configs"),
        sa.UniqueConstraint("entry_fee", name="uq_agent_target_config_entry_fee"),
        sa.CheckConstraint(
            "entry_fee IS NULL OR entry_fee > 0",
            name="ck_agent_target_config_entry_fee_positive",
        ),
        sa.CheckConstraint(
            "daily_required IS NULL OR daily_required > 0",
            name="ck_agent_target_config_daily_required_positive",
        ),
    )
    op.create_index("ix_agent_target_configs_entry_fee", "agent_target_configs", ["entry_fee"])

    # ── agent_daily_performances ──────────────────────────────────────────────
    op.create_table(
        "agent_daily_performances",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("perf_date", sa.Date, nullable=False),
        sa.Column("entry_fee", sa.Numeric(10, 2), nullable=False),
        sa.Column("required_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("completed_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("shortfall", sa.Integer, nullable=False, server_default="0"),
        sa.Column("extra_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("extra_bonus", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("deduction_amount", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column(
            "is_grace",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "is_warned",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "is_excused",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("admin_note", sa.Text, nullable=True),
        sa.Column("reviewed_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("meta", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name="pk_agent_daily_performances"),
        sa.UniqueConstraint(
            "agent_id", "perf_date", "entry_fee",
            name="uq_agent_daily_perf_agent_date_tier",
        ),
        sa.CheckConstraint(
            "completed_count >= 0", name="ck_adp_completed_count_nn"
        ),
        sa.CheckConstraint(
            "required_count >= 0", name="ck_adp_required_count_nn"
        ),
        sa.ForeignKeyConstraint(
            ["agent_id"], ["staff_users.id"], ondelete="RESTRICT",
            name="fk_adp_agent_id",
        ),
        sa.ForeignKeyConstraint(
            ["reviewed_by_id"], ["staff_users.id"], ondelete="SET NULL",
            name="fk_adp_reviewed_by_id",
        ),
    )
    op.create_index("ix_adp_agent_id", "agent_daily_performances", ["agent_id"])
    op.create_index("ix_adp_perf_date", "agent_daily_performances", ["perf_date"])
    op.create_index("ix_adp_status", "agent_daily_performances", ["status"])

    # ── agent_graces ──────────────────────────────────────────────────────────
    op.create_table(
        "agent_graces",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("daily_performance_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("grace_date", sa.Date, nullable=False),
        sa.Column("reason", sa.Text, nullable=False),
        sa.Column("granted_by_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_agent_graces"),
        sa.UniqueConstraint(
            "daily_performance_id",
            name="uq_agent_grace_daily_performance_id",
        ),
        sa.ForeignKeyConstraint(
            ["agent_id"], ["staff_users.id"], ondelete="RESTRICT",
            name="fk_agent_graces_agent_id",
        ),
        sa.ForeignKeyConstraint(
            ["daily_performance_id"],
            ["agent_daily_performances.id"],
            ondelete="RESTRICT",
            name="fk_agent_graces_daily_performance_id",
        ),
        sa.ForeignKeyConstraint(
            ["granted_by_id"], ["staff_users.id"], ondelete="RESTRICT",
            name="fk_agent_graces_granted_by_id",
        ),
    )
    op.create_index("ix_agent_graces_agent_id", "agent_graces", ["agent_id"])
    op.create_index("ix_agent_graces_grace_date", "agent_graces", ["grace_date"])
    op.create_index(
        "ix_agent_graces_daily_performance_id",
        "agent_graces",
        ["daily_performance_id"],
    )

    # ── agent_warnings ────────────────────────────────────────────────────────
    op.create_table(
        "agent_warnings",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("daily_performance_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("warning_date", sa.Date, nullable=False),
        sa.Column("warning_number", sa.Integer, nullable=False, server_default="1"),
        sa.Column("reason", sa.Text, nullable=False),
        sa.Column("issued_by_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_agent_warnings"),
        sa.ForeignKeyConstraint(
            ["agent_id"], ["staff_users.id"], ondelete="RESTRICT",
            name="fk_agent_warnings_agent_id",
        ),
        sa.ForeignKeyConstraint(
            ["daily_performance_id"],
            ["agent_daily_performances.id"],
            ondelete="SET NULL",
            name="fk_agent_warnings_daily_performance_id",
        ),
        sa.ForeignKeyConstraint(
            ["issued_by_id"], ["staff_users.id"], ondelete="RESTRICT",
            name="fk_agent_warnings_issued_by_id",
        ),
    )
    op.create_index("ix_agent_warnings_agent_id", "agent_warnings", ["agent_id"])
    op.create_index("ix_agent_warnings_warning_date", "agent_warnings", ["warning_date"])
    op.create_index(
        "ix_agent_warnings_daily_performance_id",
        "agent_warnings",
        ["daily_performance_id"],
    )

    # ── agent_salary_records ──────────────────────────────────────────────────
    op.create_table(
        "agent_salary_records",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("year", sa.Integer, nullable=False),
        sa.Column("month", sa.Integer, nullable=False),
        sa.Column("base_salary", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("total_extra_bonus", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("total_deductions", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("manual_adjustment", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("final_salary", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("manual_adjustment_reason", sa.Text, nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="draft"),
        sa.Column("notes", sa.Text, nullable=True),
        sa.Column("confirmed_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name="pk_agent_salary_records"),
        sa.UniqueConstraint(
            "agent_id", "year", "month",
            name="uq_agent_salary_record_agent_year_month",
        ),
        sa.CheckConstraint("year >= 2024", name="ck_agent_salary_year_sane"),
        sa.CheckConstraint(
            "month >= 1 AND month <= 12", name="ck_agent_salary_month_range"
        ),
        sa.ForeignKeyConstraint(
            ["agent_id"], ["staff_users.id"], ondelete="RESTRICT",
            name="fk_agent_salary_records_agent_id",
        ),
        sa.ForeignKeyConstraint(
            ["confirmed_by_id"], ["staff_users.id"], ondelete="SET NULL",
            name="fk_agent_salary_records_confirmed_by_id",
        ),
    )
    op.create_index(
        "ix_agent_salary_records_agent_id", "agent_salary_records", ["agent_id"]
    )
    op.create_index(
        "ix_agent_salary_records_status", "agent_salary_records", ["status"]
    )
    op.create_index(
        "ix_agent_salary_records_year_month",
        "agent_salary_records",
        ["year", "month"],
    )


def downgrade() -> None:
    # Drop in reverse dependency order
    op.drop_index("ix_agent_salary_records_year_month", table_name="agent_salary_records")
    op.drop_index("ix_agent_salary_records_status", table_name="agent_salary_records")
    op.drop_index("ix_agent_salary_records_agent_id", table_name="agent_salary_records")
    op.drop_table("agent_salary_records")

    op.drop_index("ix_agent_warnings_daily_performance_id", table_name="agent_warnings")
    op.drop_index("ix_agent_warnings_warning_date", table_name="agent_warnings")
    op.drop_index("ix_agent_warnings_agent_id", table_name="agent_warnings")
    op.drop_table("agent_warnings")

    op.drop_index("ix_agent_graces_daily_performance_id", table_name="agent_graces")
    op.drop_index("ix_agent_graces_grace_date", table_name="agent_graces")
    op.drop_index("ix_agent_graces_agent_id", table_name="agent_graces")
    op.drop_table("agent_graces")

    op.drop_index("ix_adp_status", table_name="agent_daily_performances")
    op.drop_index("ix_adp_perf_date", table_name="agent_daily_performances")
    op.drop_index("ix_adp_agent_id", table_name="agent_daily_performances")
    op.drop_table("agent_daily_performances")

    op.drop_index("ix_agent_target_configs_entry_fee", table_name="agent_target_configs")
    op.drop_table("agent_target_configs")
