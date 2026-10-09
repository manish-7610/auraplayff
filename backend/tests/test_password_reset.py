"""
Tests for Admin-assisted password reset flow.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import login_player, login_staff, register_player

pytestmark = pytest.mark.asyncio


async def _get_user_id(client: AsyncClient, username: str, ff_uid: str) -> str:
    resp = await register_player(client, username=username, ff_uid=ff_uid)
    assert resp.status_code == 201
    return resp.json()["id"]


async def test_admin_can_issue_reset_token(client: AsyncClient):
    user_id = await _get_user_id(client, "resetuser1", "1110000001")
    login_resp = await login_staff(client)
    staff_token = login_resp.json()["access_token"]

    resp = await client.post(
        f"/api/admin/users/{user_id}/issue-password-reset",
        headers={"Authorization": f"Bearer {staff_token}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "reset_token" in data
    assert data["expires_in_minutes"] == 30
    # Token should be a non-empty string
    assert len(data["reset_token"]) > 10


async def test_reset_token_is_stored_hashed(client: AsyncClient, db_session: AsyncSession):
    """The raw token must not appear in the DB — only its hash."""
    from app.models.password_reset_token import PasswordResetToken

    user_id = await _get_user_id(client, "resetuser2", "1110000002")
    login_resp = await login_staff(client)
    staff_token = login_resp.json()["access_token"]

    resp = await client.post(
        f"/api/admin/users/{user_id}/issue-password-reset",
        headers={"Authorization": f"Bearer {staff_token}"},
    )
    raw_token = resp.json()["reset_token"]

    result = await db_session.execute(select(PasswordResetToken))
    prt = result.scalar_one_or_none()
    assert prt is not None
    # Raw token must not be stored
    assert prt.token_hash != raw_token
    # Must be a 64-char SHA-256 hex digest
    assert len(prt.token_hash) == 64


async def test_reset_password_success(client: AsyncClient, db_session: AsyncSession):
    from sqlalchemy import select
    from app.models.user import User

    user_id = await _get_user_id(client, "resetuser3", "1110000003")
    login_resp = await login_staff(client)
    staff_token = login_resp.json()["access_token"]

    issue_resp = await client.post(
        f"/api/admin/users/{user_id}/issue-password-reset",
        headers={"Authorization": f"Bearer {staff_token}"},
    )
    raw_token = issue_resp.json()["reset_token"]

    reset_resp = await client.post(
        "/api/auth/reset-password",
        json={
            "token": raw_token,
            "new_password": "NewPassword@456",
            "confirm_password": "NewPassword@456",
        },
    )
    assert reset_resp.status_code == 200

    # Can now login with new password
    login_resp = await client.post(
        "/api/auth/login",
        json={"username": "resetuser3", "password": "NewPassword@456"},
    )
    assert login_resp.status_code == 200


async def test_reset_token_used_once_only(client: AsyncClient):
    user_id = await _get_user_id(client, "resetuser4", "1110000004")
    login_resp = await login_staff(client)
    staff_token = login_resp.json()["access_token"]

    issue_resp = await client.post(
        f"/api/admin/users/{user_id}/issue-password-reset",
        headers={"Authorization": f"Bearer {staff_token}"},
    )
    raw_token = issue_resp.json()["reset_token"]

    # Use it once
    await client.post(
        "/api/auth/reset-password",
        json={
            "token": raw_token,
            "new_password": "NewPassword@456",
            "confirm_password": "NewPassword@456",
        },
    )

    # Try to use it again — must fail
    resp2 = await client.post(
        "/api/auth/reset-password",
        json={
            "token": raw_token,
            "new_password": "AnotherNew@789",
            "confirm_password": "AnotherNew@789",
        },
    )
    assert resp2.status_code == 400
    assert resp2.json()["error"]["code"] == "RESET_TOKEN_INVALID"


async def test_reset_token_increments_token_version(client: AsyncClient, db_session: AsyncSession):
    from app.models.user import User

    user_id = await _get_user_id(client, "resetuser5", "1110000005")
    login_resp = await login_staff(client)
    staff_token = login_resp.json()["access_token"]

    # Get original token_version
    result = await db_session.execute(select(User).where(User.username == "resetuser5"))
    user = result.scalar_one()
    original_version = user.token_version

    issue_resp = await client.post(
        f"/api/admin/users/{user_id}/issue-password-reset",
        headers={"Authorization": f"Bearer {staff_token}"},
    )
    raw_token = issue_resp.json()["reset_token"]

    await client.post(
        "/api/auth/reset-password",
        json={
            "token": raw_token,
            "new_password": "NewPassword@456",
            "confirm_password": "NewPassword@456",
        },
    )

    # Refresh user from DB
    await db_session.refresh(user)
    assert user.token_version == original_version + 1


async def test_reset_invalidates_existing_sessions(client: AsyncClient, db_session: AsyncSession):
    """After password reset, old refresh tokens must be revoked."""
    import uuid as _uuid
    from app.models.refresh_token import RefreshToken

    reg = await register_player(client, username="resetuser6", ff_uid="1110000006")
    assert reg.status_code == 201
    user_id_str = reg.json()["id"]
    user_uuid = _uuid.UUID(user_id_str)

    # Login to create a refresh token
    await login_player(client, username="resetuser6")

    login_resp = await login_staff(client)
    staff_token = login_resp.json()["access_token"]

    issue_resp = await client.post(
        f"/api/admin/users/{user_id_str}/issue-password-reset",
        headers={"Authorization": f"Bearer {staff_token}"},
    )
    raw_token = issue_resp.json()["reset_token"]

    await client.post(
        "/api/auth/reset-password",
        json={
            "token": raw_token,
            "new_password": "NewPassword@456",
            "confirm_password": "NewPassword@456",
        },
    )

    # All refresh tokens for this user must now be revoked
    result = await db_session.execute(
        select(RefreshToken).where(
            RefreshToken.user_id == user_uuid,
            RefreshToken.is_revoked == False,  # noqa: E712
        )
    )
    active_tokens = result.scalars().all()
    assert len(active_tokens) == 0


async def test_reset_token_expiry(client: AsyncClient, db_session: AsyncSession):
    """An expired reset token must be rejected."""
    from app.models.password_reset_token import PasswordResetToken
    from app.core.security import generate_password_reset_token

    user_id = await _get_user_id(client, "resetuser7", "1110000007")
    login_resp = await login_staff(client)
    staff_token = login_resp.json()["access_token"]

    # Issue a token (legitimate)
    issue_resp = await client.post(
        f"/api/admin/users/{user_id}/issue-password-reset",
        headers={"Authorization": f"Bearer {staff_token}"},
    )
    raw_token = issue_resp.json()["reset_token"]

    # Manually expire the token in DB
    from app.core.security import hash_token
    token_hash = hash_token(raw_token)
    result = await db_session.execute(
        select(PasswordResetToken).where(PasswordResetToken.token_hash == token_hash)
    )
    prt = result.scalar_one()
    prt.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    await db_session.commit()

    resp = await client.post(
        "/api/auth/reset-password",
        json={
            "token": raw_token,
            "new_password": "NewPassword@456",
            "confirm_password": "NewPassword@456",
        },
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "RESET_TOKEN_EXPIRED"


async def test_superseded_reset_token_rejected(client: AsyncClient):
    """Issuing a second reset token must invalidate the first."""
    user_id = await _get_user_id(client, "resetuser8", "1110000008")
    login_resp = await login_staff(client)
    staff_token = login_resp.json()["access_token"]

    # Issue first token
    resp1 = await client.post(
        f"/api/admin/users/{user_id}/issue-password-reset",
        headers={"Authorization": f"Bearer {staff_token}"},
    )
    token1 = resp1.json()["reset_token"]

    # Issue second token (supersedes first)
    await client.post(
        f"/api/admin/users/{user_id}/issue-password-reset",
        headers={"Authorization": f"Bearer {staff_token}"},
    )

    # First token must now be rejected
    resp = await client.post(
        "/api/auth/reset-password",
        json={
            "token": token1,
            "new_password": "NewPassword@456",
            "confirm_password": "NewPassword@456",
        },
    )
    assert resp.status_code == 400


async def test_reset_creates_audit_log(client: AsyncClient, db_session: AsyncSession):
    from app.models.audit_log import AuditLog

    user_id = await _get_user_id(client, "resetuser9", "1110000009")
    login_resp = await login_staff(client)
    staff_token = login_resp.json()["access_token"]

    await client.post(
        f"/api/admin/users/{user_id}/issue-password-reset",
        headers={"Authorization": f"Bearer {staff_token}"},
    )

    result = await db_session.execute(
        select(AuditLog).where(AuditLog.action == "password_reset.issued")
    )
    log = result.scalar_one_or_none()
    assert log is not None
    assert log.entity_id == user_id
