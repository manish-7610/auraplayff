"""Add is_recovery flag to support_requests

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-11

Changes
-------
Adds is_recovery boolean column to support_requests.
When True the row was submitted via the public /api/recovery/request endpoint
(unauthenticated) and should be treated as a password recovery request.

Also adds:
  - requester_username  VARCHAR(30) nullable — submitted username (not verified)
  - requester_ff_uid    VARCHAR(20) nullable — submitted UID (not verified)

These are stored for manual admin verification only.
They are explicitly NOT trusted as proof of ownership.

NOTE: No DB migration is required to run this application in development
mode — aiosqlite will create tables from ORM metadata on every test run.
This migration is for the production PostgreSQL database.
DO NOT apply to production without explicit approval.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: Union[str, None] = "0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Make user_id nullable to support unauthenticated recovery requests
    op.alter_column(
        "support_requests",
        "user_id",
        existing_type=sa.dialects.postgresql.UUID(as_uuid=True) if hasattr(sa, 'dialects') else sa.UUID(),
        nullable=True,
    )
    op.add_column(
        "support_requests",
        sa.Column(
            "is_recovery",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "support_requests",
        sa.Column("requester_username", sa.String(30), nullable=True),
    )
    op.add_column(
        "support_requests",
        sa.Column("requester_ff_uid", sa.String(20), nullable=True),
    )
    op.create_index(
        "ix_support_requests_is_recovery",
        "support_requests",
        ["is_recovery"],
    )


def downgrade() -> None:
    op.drop_index("ix_support_requests_is_recovery", table_name="support_requests")
    op.drop_column("support_requests", "requester_ff_uid")
    op.drop_column("support_requests", "requester_username")
    op.drop_column("support_requests", "is_recovery")
    op.alter_column(
        "support_requests",
        "user_id",
        existing_type=sa.dialects.postgresql.UUID(as_uuid=True) if hasattr(sa, 'dialects') else sa.UUID(),
        nullable=False,
    )
