"""Add ff_name (Free Fire display name) to users table

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-03

Changes
-------
Adds an optional `ff_name` column to the `users` table.

Design decision
--------------
`username` remains the immutable login identifier (never changes, never shown as
the "Free Fire name"). `ff_name` is the player-editable public display name that
appears on leaderboards, results, and the profile hero — matching the name the
player uses inside Free Fire.

Column spec:
  ff_name  VARCHAR(30)  NULLABLE  (NULL means "not set yet; fall back to username")

No uniqueness constraint is enforced at the DB level because Free Fire itself
allows duplicate in-game names. Uniqueness is enforced in application logic only
if the product decides to require it (currently: NOT enforced — two players can
share the same ff_name). This can always be tightened with a later migration.

No backfill: existing rows remain NULL; the application falls back to username
when ff_name IS NULL. Players set their ff_name on first profile edit.

Rollback: simply drops the column (no data loss risk since it is nullable and
newly added).
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: Union[str, None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "ff_name",
            sa.String(30),
            nullable=True,
        ),
    )
    # Partial index — only index rows where ff_name IS NOT NULL,
    # keeping the index lean for existing rows.
    op.create_index(
        "ix_users_ff_name",
        "users",
        ["ff_name"],
        postgresql_where=sa.text("ff_name IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_users_ff_name", table_name="users")
    op.drop_column("users", "ff_name")
