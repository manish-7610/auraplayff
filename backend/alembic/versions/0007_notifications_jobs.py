"""Notifications and Job Execution Logs — Phase 7

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-13

New tables
----------
  notifications         — in-app notifications for players, agents, and admins
  job_execution_logs    — lightweight audit trail for every scheduled job run

Design notes
------------
  notifications.idempotency_key  UNIQUE — prevents duplicate notification events
  job_execution_logs.execution_id UNIQUE — allows the scheduler to patch the row
                                           from running → success/failed/skipped

  All enum-like status columns are stored as VARCHAR (native_enum=False) so
  new values can be added without ALTER TYPE migrations.

  No changes to existing Phase 1–6 tables.

  Monetary values are NOT stored in these tables — they are read-only
  informational records. Player wallets are never touched here.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── notifications ─────────────────────────────────────────────────────────
    op.create_table(
        "notifications",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        # Recipient — exactly one of user_id / staff_user_id is non-null
        sa.Column("recipient_type", sa.String(20), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("staff_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        # Content
        sa.Column("notification_type", sa.String(100), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("message", sa.Text, nullable=False),
        # Related entity (for deep-linking)
        sa.Column("related_entity_type", sa.String(60), nullable=True),
        sa.Column("related_entity_id", sa.String(80), nullable=True),
        # Read state
        sa.Column(
            "is_read",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        # Idempotency key (unique when provided)
        sa.Column("idempotency_key", sa.String(300), nullable=True),
        # Extra metadata (no secrets)
        sa.Column("meta", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        # Timestamps
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        # Constraints
        sa.PrimaryKeyConstraint("id", name="pk_notifications"),
        sa.UniqueConstraint(
            "idempotency_key", name="uq_notifications_idempotency_key"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_notifications_user_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["staff_user_id"],
            ["staff_users.id"],
            name="fk_notifications_staff_user_id",
            ondelete="CASCADE",
        ),
    )

    # Indexes for common access patterns
    op.create_index(
        "ix_notifications_user_id_created",
        "notifications",
        ["user_id", "created_at"],
    )
    op.create_index(
        "ix_notifications_staff_user_id_created",
        "notifications",
        ["staff_user_id", "created_at"],
    )
    op.create_index(
        "ix_notifications_is_read",
        "notifications",
        ["is_read"],
    )
    op.create_index(
        "ix_notifications_recipient_type",
        "notifications",
        ["recipient_type"],
    )
    op.create_index(
        "ix_notifications_notification_type",
        "notifications",
        ["notification_type"],
    )
    op.create_index(
        "ix_notifications_created_at",
        "notifications",
        ["created_at"],
    )
    op.create_index(
        "ix_notifications_idempotency_key",
        "notifications",
        ["idempotency_key"],
    )

    # ── job_execution_logs ────────────────────────────────────────────────────
    op.create_table(
        "job_execution_logs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("job_name", sa.String(100), nullable=False),
        # Unique per run — used to patch running → completed on finish
        sa.Column("execution_id", sa.String(80), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.Integer, nullable=True),
        # running | success | failed | skipped
        sa.Column(
            "status",
            sa.String(20),
            nullable=False,
            server_default=sa.text("'running'"),
        ),
        sa.Column("records_affected", sa.Integer, nullable=True),
        # Truncated exception detail (no secrets)
        sa.Column("error_summary", sa.Text, nullable=True),
        sa.Column("meta", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        # Constraints
        sa.PrimaryKeyConstraint("id", name="pk_job_execution_logs"),
        sa.UniqueConstraint("execution_id", name="uq_job_execution_logs_execution_id"),
    )

    op.create_index(
        "ix_job_execution_logs_job_name",
        "job_execution_logs",
        ["job_name"],
    )
    op.create_index(
        "ix_job_execution_logs_execution_id",
        "job_execution_logs",
        ["execution_id"],
    )
    op.create_index(
        "ix_job_execution_logs_started_at",
        "job_execution_logs",
        ["started_at"],
    )
    op.create_index(
        "ix_job_execution_logs_status",
        "job_execution_logs",
        ["status"],
    )
    op.create_index(
        "ix_job_execution_logs_job_name_started",
        "job_execution_logs",
        ["job_name", "started_at"],
    )


def downgrade() -> None:
    # Drop in reverse order
    op.drop_index(
        "ix_job_execution_logs_job_name_started", table_name="job_execution_logs"
    )
    op.drop_index("ix_job_execution_logs_status", table_name="job_execution_logs")
    op.drop_index(
        "ix_job_execution_logs_started_at", table_name="job_execution_logs"
    )
    op.drop_index(
        "ix_job_execution_logs_execution_id", table_name="job_execution_logs"
    )
    op.drop_index("ix_job_execution_logs_job_name", table_name="job_execution_logs")
    op.drop_table("job_execution_logs")

    op.drop_index("ix_notifications_idempotency_key", table_name="notifications")
    op.drop_index("ix_notifications_created_at", table_name="notifications")
    op.drop_index(
        "ix_notifications_notification_type", table_name="notifications"
    )
    op.drop_index("ix_notifications_recipient_type", table_name="notifications")
    op.drop_index("ix_notifications_is_read", table_name="notifications")
    op.drop_index(
        "ix_notifications_staff_user_id_created", table_name="notifications"
    )
    op.drop_index(
        "ix_notifications_user_id_created", table_name="notifications"
    )
    op.drop_table("notifications")
