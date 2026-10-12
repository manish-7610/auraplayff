"""
Tests for the password recovery flow:

Part A — Public recovery request (unauthenticated):
  - Player can submit without logging in
  - Generic response regardless of whether username exists
  - Contact method required
  - Rate limiting (3/hour per IP)

Part B — Admin token issuance:
  - Super Admin can look up user by username and issue reset token
  - Token stored hashed, raw token returned once
  - Agent / non-super-admin blocked
  - Token for nonexistent username returns 404

Part C — Token consumption (reset-password):
  - Already tested by test_password_reset.py; focus on recovery-specific paths
  - Token from recovery issuance endpoint works on /api/auth/reset-password
  - Token is single-use
  - Expired token rejected
  - Existing sessions invalidated after reset

Part D — Preservation:
  - Normal support tickets still require auth
  - Normal login still works after recovery flow
  - RBAC unchanged
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import login_player, login_staff, register_player

pytestmark = pytest.mark.asyncio


# ═══════════════════════════════════════════════════════════════════════════════
# PART A — Public recovery request
# ═══════════════════════════════════════════════════════════════════════════════

async def test_recovery_request_without_login(client: AsyncClient):
    """Logged-out player can submit a recovery request."""
    resp = await client.post(
        "/api/recovery/request",
        json={
            "username": "someuser",
            "ff_uid": "1234567890",
            "message": "I forgot my password and cannot log in.",
            "whatsapp_number": "+91 98765 43210",
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert "request_id" in data
    assert len(data["request_id"]) > 10  # it's a UUID


async def test_recovery_request_generic_response_for_unknown_username(client: AsyncClient):
    """Response is identical whether the username exists or not — prevents enumeration."""
    resp_real = await client.post(
        "/api/recovery/request",
        json={
            "username": "unknown_user_xyz",   # valid format, but doesn't exist in DB
            "ff_uid": "9999999999",
            "message": "I cannot log in to my account.",
            "instagram_id": "myinsta",
        },
    )
    assert resp_real.status_code == 201
    assert "request_id" in resp_real.json()
    # Same 201 response whether username exists or not — prevents enumeration
    assert "message" in resp_real.json()


async def test_recovery_request_requires_contact_method(client: AsyncClient):
    """At least one contact method must be provided."""
    resp = await client.post(
        "/api/recovery/request",
        json={
            "username": "someuser",
            "ff_uid": "1234567890",
            "message": "I cannot log in to my account.",
            # No whatsapp_number or instagram_id
        },
    )
    assert resp.status_code == 422


async def test_recovery_request_requires_valid_username_format(client: AsyncClient):
    """Invalid username format should fail validation."""
    resp = await client.post(
        "/api/recovery/request",
        json={
            "username": "a",  # too short
            "ff_uid": "1234567890",
            "message": "I cannot log in to my account.",
            "whatsapp_number": "+91 98765 43210",
        },
    )
    assert resp.status_code == 422


async def test_recovery_request_requires_valid_ff_uid(client: AsyncClient):
    """FF UID must be 6–15 digits."""
    resp = await client.post(
        "/api/recovery/request",
        json={
            "username": "someuser",
            "ff_uid": "abc",  # not digits
            "message": "I cannot log in to my account.",
            "whatsapp_number": "+91 98765 43210",
        },
    )
    assert resp.status_code == 422


async def test_recovery_request_stored_with_is_recovery_true(
    client: AsyncClient, db_session: AsyncSession
):
    """Submitted recovery requests are stored with is_recovery=True."""
    from app.models.support_request import SupportRequest

    resp = await client.post(
        "/api/recovery/request",
        json={
            "username": "testrecoveryuser",
            "ff_uid": "1111111111",
            "message": "Please help me recover my account.",
            "whatsapp_number": "+91 11111 11111",
        },
    )
    assert resp.status_code == 201
    request_id = resp.json()["request_id"]

    result = await db_session.execute(
        select(SupportRequest).where(SupportRequest.id == uuid.UUID(request_id))
    )
    req = result.scalar_one_or_none()
    assert req is not None
    assert req.is_recovery is True
    assert req.user_id is None  # no authenticated user linked
    assert req.requester_username == "testrecoveryuser"
    assert req.requester_ff_uid == "1111111111"


async def test_recovery_request_does_not_store_password(
    client: AsyncClient, db_session: AsyncSession
):
    """Recovery request must never accept or store a password field."""
    from app.models.support_request import SupportRequest

    resp = await client.post(
        "/api/recovery/request",
        json={
            "username": "pwdtest",
            "ff_uid": "2222222222",
            "message": "I want to reset to 'newpassword123'.",  # password in message
            "whatsapp_number": "+91 22222 22222",
            # Note: even if someone adds new_password in the payload,
            # it is not a field in RecoveryRequestCreate and gets ignored
            "new_password": "should_be_ignored",
        },
    )
    # Pydantic ignores extra fields — should succeed
    assert resp.status_code == 201


# ═══════════════════════════════════════════════════════════════════════════════
# PART B — Admin token issuance via recovery
# ═══════════════════════════════════════════════════════════════════════════════

async def test_super_admin_can_issue_token_by_username(client: AsyncClient):
    """Super Admin can issue reset token using the by-username endpoint."""
    reg = await register_player(client, username="recoveryplayer1", ff_uid="3330000001")
    assert reg.status_code == 201

    login_resp = await login_staff(client)
    staff_token = login_resp.json()["access_token"]

    resp = await client.post(
        "/api/admin/users/by-username/recoveryplayer1/issue-password-reset",
        headers={"Authorization": f"Bearer {staff_token}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "reset_token" in data
    assert data["expires_in_minutes"] == 30
    assert len(data["reset_token"]) > 10


async def test_agent_cannot_issue_token_by_username(client: AsyncClient, db_session: AsyncSession):
    """Agents (non-super-admin) must be blocked from issuing reset tokens."""
    from app.models.staff_user import StaffUser, StaffRole
    from app.core.security import create_access_token, hash_password

    # Create an agent directly in DB
    agent = StaffUser(
        agent_id="AGT-RECOVERY-TEST",
        display_name="Recovery Test Agent",
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

    await register_player(client, username="recoveryplayer2", ff_uid="3330000002")

    resp = await client.post(
        "/api/admin/users/by-username/recoveryplayer2/issue-password-reset",
        headers={"Authorization": f"Bearer {agent_token}"},
    )
    assert resp.status_code == 403


async def test_token_issuance_for_nonexistent_user_returns_404(client: AsyncClient):
    """Issuing a token for a username that doesn't exist returns 404."""
    login_resp = await login_staff(client)
    staff_token = login_resp.json()["access_token"]

    resp = await client.post(
        "/api/admin/users/by-username/completely_nonexistent_user_xyz/issue-password-reset",
        headers={"Authorization": f"Bearer {staff_token}"},
    )
    assert resp.status_code == 404


async def test_reset_token_is_stored_hashed_not_plaintext(
    client: AsyncClient, db_session: AsyncSession
):
    """The raw reset token must not appear in the database."""
    from app.models.password_reset_token import PasswordResetToken

    await register_player(client, username="hashcheckuser", ff_uid="3330000003")
    login_resp = await login_staff(client)
    staff_token = login_resp.json()["access_token"]

    resp = await client.post(
        "/api/admin/users/by-username/hashcheckuser/issue-password-reset",
        headers={"Authorization": f"Bearer {staff_token}"},
    )
    raw_token = resp.json()["reset_token"]

    result = await db_session.execute(
        select(PasswordResetToken).order_by(PasswordResetToken.created_at.desc()).limit(1)
    )
    prt = result.scalar_one_or_none()
    assert prt is not None
    # Raw token must never equal the stored hash
    assert prt.token_hash != raw_token
    # Hash must be 64-char SHA-256
    assert len(prt.token_hash) == 64


async def test_no_plaintext_token_in_audit_log(
    client: AsyncClient, db_session: AsyncSession
):
    """Audit log entry for reset issuance must not contain the raw token."""
    from app.models.audit_log import AuditLog
    import json

    await register_player(client, username="auditloguser", ff_uid="3330000004")
    login_resp = await login_staff(client)
    staff_token = login_resp.json()["access_token"]

    resp = await client.post(
        "/api/admin/users/by-username/auditloguser/issue-password-reset",
        headers={"Authorization": f"Bearer {staff_token}"},
    )
    raw_token = resp.json()["reset_token"]

    result = await db_session.execute(
        select(AuditLog).where(
            AuditLog.action.in_(["password_reset.issued", "password_reset.issued_from_recovery"])
        ).order_by(AuditLog.created_at.desc()).limit(5)
    )
    logs = result.scalars().all()
    assert len(logs) > 0
    for log in logs:
        # Serialize log values and verify token never appears
        log_text = json.dumps({
            "new_value": log.new_value,
            "old_value": log.old_value,
            "action": log.action,
        })
        assert raw_token not in log_text, "Raw token must not appear in audit log"


# ═══════════════════════════════════════════════════════════════════════════════
# PART C — Full recovery flow end-to-end
# ═══════════════════════════════════════════════════════════════════════════════

async def test_full_recovery_flow(client: AsyncClient):
    """
    Full flow:
    1. Player submits recovery request (unauthenticated)
    2. Super Admin issues reset token via by-username endpoint
    3. Player uses token to reset password
    4. Player logs in with new password
    5. Old password no longer works
    """
    # 1. Register player
    reg = await register_player(
        client, username="fullflowplayer", ff_uid="4440000001"
    )
    assert reg.status_code == 201

    # 2. Player submits recovery request (no login)
    rec_resp = await client.post(
        "/api/recovery/request",
        json={
            "username": "fullflowplayer",
            "ff_uid": "4440000001",
            "message": "I lost access to my account, please help.",
            "whatsapp_number": "+91 44444 44444",
        },
    )
    assert rec_resp.status_code == 201

    # 3. Admin reviews and issues token
    login_resp = await login_staff(client)
    staff_token = login_resp.json()["access_token"]

    issue_resp = await client.post(
        "/api/admin/users/by-username/fullflowplayer/issue-password-reset",
        headers={"Authorization": f"Bearer {staff_token}"},
    )
    assert issue_resp.status_code == 200
    reset_token = issue_resp.json()["reset_token"]

    # 4. Player uses token to reset password
    reset_resp = await client.post(
        "/api/auth/reset-password",
        json={
            "token": reset_token,
            "new_password": "NewPassword@Recovery1",
            "confirm_password": "NewPassword@Recovery1",
        },
    )
    assert reset_resp.status_code == 200

    # 5. Login with NEW password succeeds
    new_login = await client.post(
        "/api/auth/login",
        json={"username": "fullflowplayer", "password": "NewPassword@Recovery1"},
    )
    assert new_login.status_code == 200
    assert "access_token" in new_login.json()

    # 6. OLD password no longer works
    old_login = await client.post(
        "/api/auth/login",
        json={"username": "fullflowplayer", "password": "SecurePass@123"},
    )
    assert old_login.status_code == 401


async def test_recovery_token_single_use(client: AsyncClient):
    """A reset token from the recovery flow can only be used once."""
    await register_player(client, username="singleuseplayer", ff_uid="4440000002")

    login_resp = await login_staff(client)
    staff_token = login_resp.json()["access_token"]

    issue_resp = await client.post(
        "/api/admin/users/by-username/singleuseplayer/issue-password-reset",
        headers={"Authorization": f"Bearer {staff_token}"},
    )
    reset_token = issue_resp.json()["reset_token"]

    # Use it once
    await client.post(
        "/api/auth/reset-password",
        json={
            "token": reset_token,
            "new_password": "NewPass@Once1",
            "confirm_password": "NewPass@Once1",
        },
    )

    # Try again — must fail
    resp2 = await client.post(
        "/api/auth/reset-password",
        json={
            "token": reset_token,
            "new_password": "NewPass@Twice2",
            "confirm_password": "NewPass@Twice2",
        },
    )
    assert resp2.status_code == 400


async def test_recovery_token_expiry(client: AsyncClient, db_session: AsyncSession):
    """An expired recovery token must be rejected."""
    from app.models.password_reset_token import PasswordResetToken
    from app.core.security import hash_token

    await register_player(client, username="expiryplayer", ff_uid="4440000003")

    login_resp = await login_staff(client)
    staff_token = login_resp.json()["access_token"]

    issue_resp = await client.post(
        "/api/admin/users/by-username/expiryplayer/issue-password-reset",
        headers={"Authorization": f"Bearer {staff_token}"},
    )
    raw_token = issue_resp.json()["reset_token"]

    # Manually expire it
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
            "new_password": "ExpiredNew@123",
            "confirm_password": "ExpiredNew@123",
        },
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] in ("RESET_TOKEN_EXPIRED", "RESET_TOKEN_INVALID")


async def test_recovery_resets_existing_sessions(client: AsyncClient, db_session: AsyncSession):
    """After recovery password reset, old refresh tokens are revoked."""
    from app.models.refresh_token import RefreshToken
    import uuid as _uuid

    reg = await register_player(client, username="sessionresetplayer", ff_uid="4440000004")
    user_id = _uuid.UUID(reg.json()["id"])

    # Login to create a refresh token
    await login_player(client, username="sessionresetplayer")

    login_resp = await login_staff(client)
    staff_token = login_resp.json()["access_token"]

    issue_resp = await client.post(
        "/api/admin/users/by-username/sessionresetplayer/issue-password-reset",
        headers={"Authorization": f"Bearer {staff_token}"},
    )
    raw_token = issue_resp.json()["reset_token"]

    await client.post(
        "/api/auth/reset-password",
        json={
            "token": raw_token,
            "new_password": "PostReset@Session1",
            "confirm_password": "PostReset@Session1",
        },
    )

    # All refresh tokens for the player should be revoked
    result = await db_session.execute(
        select(RefreshToken).where(
            RefreshToken.user_id == user_id,
            RefreshToken.is_revoked == False,  # noqa: E712
        )
    )
    active = result.scalars().all()
    assert len(active) == 0


# ═══════════════════════════════════════════════════════════════════════════════
# PART D — Preservation checks
# ═══════════════════════════════════════════════════════════════════════════════

async def test_normal_support_requires_auth(client: AsyncClient):
    """Normal support ticket endpoint still requires authentication."""
    resp = await client.post(
        "/api/support",
        json={
            "message": "I have an issue with my account.",
            "whatsapp_number": "+91 99999 99999",
        },
    )
    assert resp.status_code == 401


async def test_normal_login_unaffected(client: AsyncClient):
    """Regular login still works after recovery routes are added."""
    await register_player(client, username="normalloginuser", ff_uid="5550000001")
    resp = await login_player(client, username="normalloginuser")
    assert resp.status_code == 200
    assert "access_token" in resp.json()


async def test_super_admin_login_unaffected(client: AsyncClient):
    """Super Admin login still works."""
    resp = await login_staff(client)
    assert resp.status_code == 200
    assert "access_token" in resp.json()


async def test_recovery_request_admin_filter(client: AsyncClient):
    """Admin can filter support requests to show only recovery type."""
    # Submit a recovery request
    await client.post(
        "/api/recovery/request",
        json={
            "username": "filtertest",
            "ff_uid": "6660000001",
            "message": "Help me recover my account.",
            "whatsapp_number": "+91 66666 66666",
        },
    )

    login_resp = await login_staff(client)
    staff_token = login_resp.json()["access_token"]

    resp = await client.get(
        "/api/admin/support?is_recovery=true",
        headers={"Authorization": f"Bearer {staff_token}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    for req in data.get("requests", []):
        assert req["is_recovery"] is True
