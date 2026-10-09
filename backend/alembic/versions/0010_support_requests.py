"""Add support_requests table — Help & Support feature

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-01

Changes
-------
Creates the support_requests table for the player Help & Support system.

Fields:
  id              UUID PK (gen_random_uuid)
  user_id         FK → users.id  RESTRICT
  tournament_id   FK → tournaments.id  SET NULL  (nullable — General/Other issue)
  message         Text  NOT NULL
  whatsapp_number VARCHAR(30) nullable
  instagram_id    VARCHAR(80) nullable
  status          VARCHAR enum: pending | in_progress | resolved  default 'pending'
  admin_response  Text nullable
  resolved_at     timestamptz nullable
  resolved_by_id  FK → staff_users.id  SET NULL  nullable
  created_at      timestamptz  NOT NULL  default now()
  updated_at      timestamptz  NOT NULL  default now()

Indexes:
  ix_support_requests_user_id
  ix_support_requests_tournament_id
  ix_support_requests_status
  ix_support_requests_created_at
  ix_support_requests_resolved_by_id

The screenshot_proofs table is intentionally left in place (unused).
Old data is preserved; no data is deleted by this migration.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "support_requests",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tournament_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("whatsapp_number", sa.String(30), nullable=True),
        sa.Column("instagram_id", sa.String(80), nullable=True),
        sa.Column(
            "status",
            sa.String(20),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column("admin_response", sa.Text(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_by_id", postgresql.UUID(as_uuid=True), nullable=True),
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
        sa.CheckConstraint(
            "length(trim(message)) > 0",
            name="ck_support_request_message_nonempty",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["tournament_id"], ["tournaments.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["resolved_by_id"], ["staff_users.id"], ondelete="SET NULL"
        ),
    )

    op.create_index(
        "ix_support_requests_user_id", "support_requests", ["user_id"]
    )
    op.create_index(
        "ix_support_requests_tournament_id", "support_requests", ["tournament_id"]
    )
    op.create_index(
        "ix_support_requests_status", "support_requests", ["status"]
    )
    op.create_index(
        "ix_support_requests_created_at", "support_requests", ["created_at"]
    )
    op.create_index(
        "ix_support_requests_resolved_by_id", "support_requests", ["resolved_by_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_support_requests_resolved_by_id", table_name="support_requests")
    op.drop_index("ix_support_requests_created_at", table_name="support_requests")
    op.drop_index("ix_support_requests_status", table_name="support_requests")
    op.drop_index("ix_support_requests_tournament_id", table_name="support_requests")
    op.drop_index("ix_support_requests_user_id", table_name="support_requests")
    op.drop_table("support_requests")
