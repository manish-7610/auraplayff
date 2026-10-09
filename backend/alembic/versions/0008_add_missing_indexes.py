"""Add missing FK indexes for RBAC and agent performance tables — Phase 10

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-16

Changes
-------
Adds missing indexes on foreign key columns that were absent in earlier migrations.
These are used by admin reporting queries and RBAC lookups.

Tables affected
---------------
  staff_user_permissions   — permission_id, granted_by_id
  agent_daily_performances — reviewed_by_id
  agent_graces             — granted_by_id
  agent_warnings           — issued_by_id
  agent_salary_records     — confirmed_by_id

No schema changes to existing data or constraints.
Safe to apply on a live database (CREATE INDEX CONCURRENTLY is not used here
for simplicity — run during a maintenance window or on a read replica first
if the tables are very large).
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── staff_user_permissions ────────────────────────────────────────────────
    op.create_index(
        "ix_staff_user_permissions_permission_id",
        "staff_user_permissions",
        ["permission_id"],
        unique=False,
    )
    op.create_index(
        "ix_staff_user_permissions_granted_by_id",
        "staff_user_permissions",
        ["granted_by_id"],
        unique=False,
    )

    # ── agent_daily_performances ──────────────────────────────────────────────
    op.create_index(
        "ix_agent_daily_performances_reviewed_by_id",
        "agent_daily_performances",
        ["reviewed_by_id"],
        unique=False,
    )

    # ── agent_graces ──────────────────────────────────────────────────────────
    op.create_index(
        "ix_agent_graces_granted_by_id",
        "agent_graces",
        ["granted_by_id"],
        unique=False,
    )

    # ── agent_warnings ────────────────────────────────────────────────────────
    op.create_index(
        "ix_agent_warnings_issued_by_id",
        "agent_warnings",
        ["issued_by_id"],
        unique=False,
    )

    # ── agent_salary_records ──────────────────────────────────────────────────
    op.create_index(
        "ix_agent_salary_records_confirmed_by_id",
        "agent_salary_records",
        ["confirmed_by_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_agent_salary_records_confirmed_by_id", table_name="agent_salary_records")
    op.drop_index("ix_agent_warnings_issued_by_id", table_name="agent_warnings")
    op.drop_index("ix_agent_graces_granted_by_id", table_name="agent_graces")
    op.drop_index("ix_agent_daily_performances_reviewed_by_id", table_name="agent_daily_performances")
    op.drop_index("ix_staff_user_permissions_granted_by_id", table_name="staff_user_permissions")
    op.drop_index("ix_staff_user_permissions_permission_id", table_name="staff_user_permissions")
