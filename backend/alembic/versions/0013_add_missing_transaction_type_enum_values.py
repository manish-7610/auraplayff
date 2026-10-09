"""Add missing values to transaction_type_enum -- fixes withdrawal 500

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-08

Root Cause
----------
Migration 0001 created transaction_type_enum as a native PostgreSQL ENUM
with 9 values:

    deposit, withdrawal, prize, kill_reward, referral_bonus,
    entry_fee, refund, penalty, adjustment

Migration 0002 expanded the Python TransactionType model to include:

    withdrawal_request, withdrawal_completed, withdrawal_rejected,
    withdrawal_refund, entry_fee_refund, admin_adjustment

But 0002 never ran ALTER TYPE to add them.  Its comment incorrectly claimed
native_enum=False in the ORM bypasses PostgreSQL's native enum constraint.
It does not.  Every INSERT with a new type value raises:

    asyncpg.exceptions.InvalidTextRepresentationError:
    invalid input value for enum transaction_type_enum: "withdrawal_request"

This migration adds all 6 missing values.

ALTER TYPE ADD VALUE and autocommit
------------------------------------
ALTER TYPE ADD VALUE must run OUTSIDE a transaction block.

Alembic's env.py calls context.begin_transaction() before upgrade() runs,
so op.get_bind() already has an open transaction.  We cannot change the
isolation level of that connection while a transaction is open.

Solution: open a SEPARATE asyncpg connection directly from upgrade() and
execute the ALTER TYPE statements through it.  asyncpg raw execute() calls
outside of an explicit conn.transaction() block run in autocommit mode
by default.  The Alembic connection continues unaffected and only updates
the alembic_version row at the end.

IF NOT EXISTS makes each statement idempotent (safe to re-run).
"""
from __future__ import annotations

import asyncio
import os
from typing import Sequence, Union

from alembic import op

revision: str = "0013"
down_revision: Union[str, None] = "0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_MISSING_VALUES = [
    "withdrawal_request",
    "withdrawal_completed",
    "withdrawal_rejected",
    "withdrawal_refund",
    "entry_fee_refund",
    "admin_adjustment",
]


async def _apply_via_asyncpg(database_url: str) -> None:
    """
    Open a fresh asyncpg connection and execute each ALTER TYPE ADD VALUE.

    asyncpg connections are in autocommit mode by default (each execute()
    is its own implicit transaction unless conn.transaction() is used).
    This is exactly what ALTER TYPE ADD VALUE requires.
    """
    import asyncpg

    # Strip SQLAlchemy driver prefix; asyncpg needs a raw postgresql:// URL
    raw_url = database_url.replace("postgresql+asyncpg://", "postgresql://")
    # Remove any query-string parameters; pass ssl as a keyword argument
    if "?" in raw_url:
        raw_url = raw_url.split("?")[0]

    conn = await asyncpg.connect(raw_url, ssl="require")
    try:
        for value in _MISSING_VALUES:
            await conn.execute(
                f"ALTER TYPE transaction_type_enum "
                f"ADD VALUE IF NOT EXISTS '{value}'"
            )
    finally:
        await conn.close()


def upgrade() -> None:
    """
    Add all 6 missing TransactionType values to the PostgreSQL native enum.

    The upgrade() body runs inside SQLAlchemy's greenlet_spawn/run_sync
    callback, which executes on a greenlet that is bound to the running
    asyncio event loop.  Neither asyncio.run() nor loop.run_until_complete()
    can be called from this context.

    Solution: run the asyncpg work on a brand-new OS thread (via
    concurrent.futures.ThreadPoolExecutor).  That thread has no event loop,
    so asyncio.run() works normally inside it.
    """
    import concurrent.futures

    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url or "placeholder" in database_url:
        raise RuntimeError(
            "DATABASE_URL is not set to a real database. "
            "Set DATABASE_URL before running this migration."
        )

    def _run_in_thread():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(_apply_via_asyncpg(database_url))
        finally:
            loop.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_run_in_thread)
        future.result()  # re-raises any exception from the thread


def downgrade() -> None:
    # PostgreSQL does not allow removing enum values without recreating the type.
    # No-op downgrade is intentional and safe.
    pass
