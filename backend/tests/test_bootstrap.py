"""
Tests for Super Admin bootstrap and permission seeding.

These tests use the client fixture (which runs bootstrap via test_app)
so the tables are guaranteed to exist and bootstrap has already run.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


async def test_bootstrap_creates_super_admin(client: AsyncClient):
    """After test_app setup, super admin login must work (proving it was created)."""
    from tests.conftest import login_staff
    resp = await login_staff(client)
    assert resp.status_code == 200
    data = resp.json()
    assert "access_token" in data


async def test_bootstrap_is_idempotent(client: AsyncClient, db_session: AsyncSession):
    """Running bootstrap again must NOT create a duplicate super admin."""
    from app.models.staff_user import StaffUser, StaffRole
    from app.services.bootstrap_service import run_bootstrap

    # Run again on the same DB that already has a super admin
    await run_bootstrap(db_session)
    await db_session.commit()

    result = await db_session.execute(
        select(func.count()).select_from(StaffUser).where(
            StaffUser.role == StaffRole.super_admin
        )
    )
    count = result.scalar()
    assert count == 1


async def test_bootstrap_password_not_plaintext(client: AsyncClient, db_session: AsyncSession):
    """The stored password hash must not be the plaintext password."""
    import os
    from app.models.staff_user import StaffUser, StaffRole

    result = await db_session.execute(
        select(StaffUser).where(StaffUser.role == StaffRole.super_admin)
    )
    admin = result.scalar_one()
    raw_password = os.environ.get("SUPER_ADMIN_PASSWORD", "")
    assert admin.password_hash != raw_password
    assert admin.password_hash.startswith("$2b$")


async def test_permissions_seeded(client: AsyncClient, db_session: AsyncSession):
    """All permission codes must exist in the DB after bootstrap."""
    from app.models.permission import Permission as PermModel
    from app.core.permissions import ALL_PERMISSION_CODES

    result = await db_session.execute(select(PermModel.code))
    seeded = {row[0] for row in result.fetchall()}
    assert ALL_PERMISSION_CODES == seeded


async def test_permission_seeding_idempotent(client: AsyncClient, db_session: AsyncSession):
    """Running seed twice must not create duplicate permission rows."""
    from app.models.permission import Permission as PermModel
    from app.core.permissions import ALL_PERMISSION_CODES
    from app.services.bootstrap_service import run_bootstrap

    # Re-run bootstrap on a DB that already has permissions
    await run_bootstrap(db_session)
    await db_session.commit()

    result = await db_session.execute(select(func.count(PermModel.id)))
    count = result.scalar()
    assert count == len(ALL_PERMISSION_CODES)
