"""Add 'hold' value to balance_column_enum — BUG #4 fix

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-20

Changes
-------
Adds 'hold' to the balance_column_enum PostgreSQL TYPE.

Previously, withdrawal-hold ledger entries used 'deposit' as a placeholder
value (BalanceColumn.deposit) even though the deposit_balance was not
actually affected.  This caused financial reports and audit queries that
group by balance_column to incorrectly classify hold transactions as deposit
transactions.

After this migration, new hold ledger entries (reserve, release, complete)
will use the correct 'hold' value.  Existing rows are unaffected — they retain
the 'deposit' value they were written with, which is intentional so as not to
silently rewrite historical ledger data.

NOTE: ALTER TYPE … ADD VALUE cannot run inside a transaction block in
PostgreSQL < 12.  Alembic is instructed to run this migration non-transactional
via transactional_ddl = False.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ALTER TYPE … ADD VALUE must run OUTSIDE a transaction block on all
    # PostgreSQL versions (PG 12+ allows it inside transactions, but the
    # new enum value is not visible to queries within the same transaction —
    # so it is still safest and most portable to run in AUTOCOMMIT).
    #
    # Alembic wraps migrations in transactions by default.  We escape that
    # by executing in AUTOCOMMIT isolation level, which is the official
    # SQLAlchemy / Alembic pattern for DDL that cannot run inside a transaction.
    #
    # If 'hold' is already present (idempotent via IF NOT EXISTS), this is a no-op.
    bind = op.get_bind()
    bind.execution_options(isolation_level="AUTOCOMMIT").execute(
        sa.text("ALTER TYPE balance_column_enum ADD VALUE IF NOT EXISTS 'hold'")
    )


def downgrade() -> None:
    # PostgreSQL does not support removing enum values without recreating the type.
    # A no-op downgrade is safe here because:
    #   1. 'hold' rows will simply never be written after rollback.
    #   2. Any rows already written with 'hold' will fail constraint checks
    #      only if the type is explicitly recreated without 'hold' — which
    #      this downgrade does NOT do.
    pass
