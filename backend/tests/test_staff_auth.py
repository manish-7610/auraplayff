"""
Tests for staff (Admin/Agent) authentication:
  - Login / logout / refresh
  - Audience separation
  - RBAC
"""
from __future__ import annotations

import pytest
from httpx import AsyncClient

from tests.conftest import login_staff, register_player

pytestmark = pytest.mark.asyncio


# ── Super Admin login ─────────────────────────────────────────────────────────

async def test_super_admin_login_success(client: AsyncClient):
    resp = await login_staff(client)
    assert resp.status_code == 200
    data = resp.json()
    assert "access_token" in data
    assert data["token_type"] == "bearer"


async def test_staff_wrong_password_rejected(client: AsyncClient):
    resp = await client.post(
        "/api/admin/auth/login",
        json={"agent_id": "TEST-ADMIN", "password": "WrongPassword@99"},
    )
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "INVALID_CREDENTIALS"


async def test_staff_wrong_agent_id_rejected(client: AsyncClient):
    resp = await client.post(
        "/api/admin/auth/login",
        json={"agent_id": "NOBODY", "password": "TestAdmin@123456"},
    )
    assert resp.status_code == 401


async def test_staff_token_has_staff_audience(client: AsyncClient):
    from jose import jwt as jose_jwt
    from app.config import get_settings

    resp = await login_staff(client)
    token = resp.json()["access_token"]
    settings = get_settings()
    payload = jose_jwt.decode(
        token, settings.jwt_secret_key,
        algorithms=[settings.jwt_algorithm],
        audience="staff",
    )
    assert payload["aud"] == "staff"
    assert payload["role"] == "super_admin"


# ── Audience separation ───────────────────────────────────────────────────────

async def test_player_token_rejected_on_staff_route(client: AsyncClient):
    """A player access token must be rejected on a staff-only endpoint."""
    await register_player(client, username="audiencetest", ff_uid="8880000001")
    login_resp = await client.post(
        "/api/auth/login",
        json={"username": "audiencetest", "password": "SecurePass@123"},
    )
    player_token = login_resp.json()["access_token"]

    # Issue password reset requires staff auth
    resp = await client.post(
        "/api/admin/users/00000000-0000-0000-0000-000000000001/issue-password-reset",
        headers={"Authorization": f"Bearer {player_token}"},
    )
    assert resp.status_code == 401


async def test_staff_token_rejected_on_player_audience_validation(client: AsyncClient):
    """Staff token with audience='staff' must fail decode_access_token(audience='player')."""
    from jose import JWTError
    from app.core.security import decode_access_token

    resp = await login_staff(client)
    staff_token = resp.json()["access_token"]

    with pytest.raises(JWTError):
        decode_access_token(staff_token, expected_audience="player")


# ── Staff refresh ─────────────────────────────────────────────────────────────

async def test_staff_refresh_works(client: AsyncClient):
    await login_staff(client)
    resp = await client.post("/api/admin/auth/refresh")
    assert resp.status_code == 200
    assert "access_token" in resp.json()


async def test_staff_refresh_without_cookie_fails(client: AsyncClient):
    resp = await client.post("/api/admin/auth/refresh")
    assert resp.status_code == 401


# ── Staff logout ──────────────────────────────────────────────────────────────

async def test_staff_logout_success(client: AsyncClient):
    await login_staff(client)
    resp = await client.post("/api/admin/auth/logout")
    assert resp.status_code == 200


async def test_staff_logout_invalidates_refresh(client: AsyncClient):
    await login_staff(client)
    await client.post("/api/admin/auth/logout")
    resp = await client.post("/api/admin/auth/refresh")
    assert resp.status_code == 401


# ── RBAC ──────────────────────────────────────────────────────────────────────

async def test_agent_no_permissions_by_default(db_session):
    """A freshly created agent has no permission grants."""
    from sqlalchemy import select
    from app.models.staff_user import StaffUser, StaffRole
    from app.models.permission import StaffUserPermission
    from app.core.security import hash_password

    agent = StaffUser(
        agent_id="AGT-0001",
        display_name="Test Agent",
        password_hash=hash_password("AgentPass@123"),
        role=StaffRole.agent,
    )
    db_session.add(agent)
    await db_session.flush([agent])

    result = await db_session.execute(
        select(StaffUserPermission).where(
            StaffUserPermission.staff_user_id == agent.id
        )
    )
    grants = result.scalars().all()
    assert len(grants) == 0


async def test_super_admin_bypasses_permission_check(client: AsyncClient):
    """Super admin can access the password reset endpoint without explicit grants."""
    # Register a player first
    await register_player(client, username="target_user", ff_uid="9990000001")

    # Get the user ID
    from tests.conftest import get_settings
    login_resp = await login_staff(client)
    staff_token = login_resp.json()["access_token"]

    # We need the user's UUID — get it from registration response
    reg_resp = await register_player(client, username="target_user2", ff_uid="9990000002")
    user_id = reg_resp.json()["id"]

    resp = await client.post(
        f"/api/admin/users/{user_id}/issue-password-reset",
        headers={"Authorization": f"Bearer {staff_token}"},
    )
    assert resp.status_code == 200
    assert "reset_token" in resp.json()


async def test_agent_denied_without_user_view_permission(client: AsyncClient, db_session):
    """An agent without user.view permission is denied the password reset endpoint."""
    from app.models.staff_user import StaffUser, StaffRole
    from app.core.security import create_access_token, hash_password

    agent = StaffUser(
        agent_id="AGT-NO-PERM",
        display_name="No Permission Agent",
        password_hash=hash_password("AgentPass@123"),
        role=StaffRole.agent,
    )
    db_session.add(agent)
    await db_session.flush([agent])
    await db_session.commit()

    agent_token = create_access_token(
        subject=agent.id,
        audience="staff",
        token_version=agent.token_version,
        role="agent",
    )

    resp = await client.post(
        "/api/admin/users/00000000-0000-0000-0000-000000000001/issue-password-reset",
        headers={"Authorization": f"Bearer {agent_token}"},
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "PERMISSION_DENIED"
