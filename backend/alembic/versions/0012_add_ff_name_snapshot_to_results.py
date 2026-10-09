"""Add ff_name_snapshot to tournament_results

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-03

Changes
-------
Adds `ff_name_snapshot VARCHAR(30) NULLABLE` to `tournament_results`.

Purpose
-------
When a player's result is entered, the backend snapshots their current
Free Fire display name (ff_name) alongside the existing ff_uid_snapshot.
This ensures that historical result records always show the name the player
used *at tournament time*, even if they later change their Free Fire name.

Existing rows receive NULL (no snapshot available for past results — the
application falls back to the current user.ff_name or user.username for display).

Rollback: simply drops the column.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: Union[str, None] = "0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "tournament_results",
        sa.Column("ff_name_snapshot", sa.String(30), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("tournament_results", "ff_name_snapshot")
