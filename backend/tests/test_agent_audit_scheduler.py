"""
T8 - Agent financial restrictions, audit logging completeness,
and scheduler API/idempotency testing.
"""
from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import login_player, login_staff, register_player

pytestmark = pytest.mark.asyncio


# ============================================================================
# Helper
# ============================================================================

async def _make_agent_with_permission(
    db_session: AsyncSession,
    agent_id_str: str,
    *permission_codes: str,
) -> str:
    """Create an agent with given permissions; return access token."""
    from app.models.staff_user import StaffUser, StaffRole
    from app.models.permission import Permission as PermModel, StaffUserPermission
    from app.core.security import create_access_token, hash_password
    import secrets

    unique_id = f"{agent_id_str}-{secrets.token_hex(3).upper()}"
    agent = StaffUser(
        agent_id=unique_id,
        display_name=f"Agent {unique_id}",
        password_hash=hash_password("AgentPass@123"),
        role=StaffRole.agent,
    )
    db_session.add(agent)
    await db_session.flush([agent])

    for code in permission_codes:
        perm = (
            await db_session.execute(
                select(PermModel).where(PermModel.code == code)
            )
        ).scalar_one_or_none()
        if perm:
            db_session.add(StaffUserPermission(
                staff_user_id=agent.id,
                permission_id=perm.id,
                granted_by_id=None,
            ))

    await db_session.commit()

    return create_access_token(
        subject=agent.id,
        audience="staff",
        token_version=agent.token_version,
        role="agent",
    )


# ============================================================================
# Agent financial restriction completeness
# ============================================================================

async def test_agent_with_deposit_view_cannot_approve(
    client: AsyncClient, db_session: AsyncSession
):
    """deposit.view does not grant deposit approval capability."""
    agent_token = await _make_agent_with_permission(
        db_session, "AGT-DEP-VIEW", "deposit.view"
    )
    fake_id = str(uuid.uuid4())
    resp = await client.post(
        f"/api/admin/deposits/{fake_id}/approve",
        json={"note": ""},
        headers={"Authorization": f"Bearer {agent_token}"},
    )
    assert resp.status_code == 403


async def test_agent_with_withdrawal_view_cannot_mark_paid(
    client: AsyncClient, db_session: AsyncSession
):
    """withdrawal.view does not grant mark-paid capability."""
    agent_token = await _make_agent_with_permission(
        db_session, "AGT-WD-VIEW", "withdrawal.view"
    )
    fake_id = str(uuid.uuid4())
    resp = await client.post(
        f"/api/admin/withdrawals/{fake_id}/mark-paid",
        json={"payout_utr": "UTR123", "note": ""},
        headers={"Authorization": f"Bearer {agent_token}"},
    )
    assert resp.status_code == 403


async def test_agent_cannot_adjust_wallet_balance(
    client: AsyncClient, db_session: AsyncSession
):
    """wallet.manual_adjust is an admin-only permission - agents must be blocked."""
    agent_token = await _make_agent_with_permission(db_session, "AGT-WALLET-TEST")
    fake_user_id = str(uuid.uuid4())

    resp = await client.post(
        f"/api/admin/wallets/{fake_user_id}/adjust",
        json={
            "balance_column": "deposit",
            "direction": "credit",
            "amount": "1000.00",
            "reason": "malicious adjustment attempt",
        },
        headers={"Authorization": f"Bearer {agent_token}"},
    )
    assert resp.status_code == 403


async def test_agent_with_tournament_create_can_create(
    client: AsyncClient, db_session: AsyncSession
):
    """Agents with tournament.create can create tournaments."""
    agent_token = await _make_agent_with_permission(
        db_session, "AGT-TOURN-CREATE", "tournament.create"
    )
    resp = await client.post(
        "/api/admin/tournaments",
        json={
            "title": "Agent Created Tournament",
            "entry_fee": 50,
            "scheduled_at": "2099-06-01T10:00:00+00:00",
            "max_players": 50,
            "min_players": 2,
            "game": "free_fire",
            "mode": "solo",
        },
        headers={"Authorization": f"Bearer {agent_token}"},
    )
    assert resp.status_code == 201, resp.text


async def test_agent_without_tournament_permission_blocked(
    client: AsyncClient, db_session: AsyncSession
):
    """Agent without tournament.create is blocked from tournament creation."""
    agent_token = await _make_agent_with_permission(db_session, "AGT-NO-TOURN")
    resp = await client.post(
        "/api/admin/tournaments",
        json={
            "title": "Blocked Tournament",
            "entry_fee": 50,
            "scheduled_at": "2099-06-01T10:00:00+00:00",
            "max_players": 50,
            "min_players": 2,
            "game": "free_fire",
            "mode": "solo",
        },
        headers={"Authorization": f"Bearer {agent_token}"},
    )
    assert resp.status_code == 403


# ============================================================================
# Audit logging completeness
# ============================================================================

async def test_agent_creation_creates_audit_log(
    client: AsyncClient, db_session: AsyncSession
):
    """Creating an agent must create an audit log entry."""
    from app.models.audit_log import AuditLog

    staff_resp = await login_staff(client)
    staff_token = staff_resp.json()["access_token"]

    resp = await client.post(
        "/api/admin/agents",
        json={"display_name": "Audit Test Agent", "password": "AgentPass@123"},
        headers={"Authorization": f"Bearer {staff_token}"},
    )
    assert resp.status_code == 201, resp.text

    result = await db_session.execute(
        select(AuditLog).where(AuditLog.action == "agent.created")
    )
    log = result.scalar_one_or_none()
    assert log is not None
    assert log.actor_type.value == "staff"


async def test_staff_login_creates_audit_log(
    client: AsyncClient, db_session: AsyncSession
):
    """Staff login must always be audit-logged."""
    from app.models.audit_log import AuditLog

    await login_staff(client)

    result = await db_session.execute(
        select(AuditLog).where(AuditLog.action == "staff.login")
    )
    log = result.scalar_one_or_none()
    assert log is not None


async def test_password_reset_issue_creates_audit_log(
    client: AsyncClient, db_session: AsyncSession
):
    """Issuing a password reset token must be audit-logged."""
    from app.models.audit_log import AuditLog

    reg = await register_player(client, username="audit_reset", ff_uid="4440000001")
    user_id = reg.json()["id"]

    staff_resp = await login_staff(client)
    staff_token = staff_resp.json()["access_token"]

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


async def test_player_registration_creates_audit_log(
    client: AsyncClient, db_session: AsyncSession
):
    """Player registration must create a user.registered audit log."""
    from app.models.audit_log import AuditLog

    await register_player(client, username="auditregplayer", ff_uid="4440000002")

    result = await db_session.execute(
        select(AuditLog).where(AuditLog.action == "user.registered")
    )
    log = result.scalar_one_or_none()
    assert log is not None


async def test_audit_log_does_not_contain_passwords(
    client: AsyncClient, db_session: AsyncSession
):
    """Audit log entries must never contain password hashes."""
    from app.models.audit_log import AuditLog
    import json

    await register_player(client, username="auditpwcheck", ff_uid="4440000003")

    result = await db_session.execute(select(AuditLog))
    logs = result.scalars().all()

    for log in logs:
        for field in [log.new_value, log.old_value]:
            if field is None:
                continue
            serialised = json.dumps(field)
            # bcrypt hashes start with $2b$ - must never appear in audit logs
            assert "$2b$" not in serialised, (
                f"Audit log action '{log.action}' contains a bcrypt hash!"
            )
            # 'password_hash' key should never appear
            assert "password_hash" not in serialised, (
                f"Audit log action '{log.action}' leaks password_hash field"
            )


async def test_audit_log_entries_are_immutable(
    client: AsyncClient, db_session: AsyncSession
):
    """Audit logs have no UPDATE path - verify no updated_at column."""
    from app.models.audit_log import AuditLog
    import sqlalchemy

    mapper = sqlalchemy.inspect(AuditLog)
    column_names = {c.key for c in mapper.column_attrs}
    assert "updated_at" not in column_names, (
        "AuditLog must not have updated_at - records must be immutable"
    )


# ============================================================================
# Scheduler API tests (via HTTP endpoint, not direct service call)
# Note: scheduler_service.run_job() creates its own DB connection pool
# targeting the real PostgreSQL URL, so we test via the admin API which
# uses the test DB session correctly.
# ============================================================================

async def test_scheduler_dashboard_accessible_to_super_admin(
    client: AsyncClient, db_session: AsyncSession
):
    """Scheduler dashboard is accessible to super admin and returns a valid list."""
    staff_resp = await login_staff(client)
    staff_token = staff_resp.json()["access_token"]

    resp = await client.get(
        "/api/admin/jobs",
        headers={"Authorization": f"Bearer {staff_token}"},
    )
    assert resp.status_code == 200, resp.text
    jobs = resp.json()
    # The response must be a list (may be empty in test env since scheduler
    # job registry is only populated when scheduler.py starts, which is
    # disabled in tests via SCHEDULER_ENABLED=false)
    assert isinstance(jobs, list)


async def test_scheduler_dashboard_blocked_for_agents(
    client: AsyncClient, db_session: AsyncSession
):
    """Scheduler dashboard must be blocked for agents."""
    agent_token = await _make_agent_with_permission(db_session, "AGT-SCHED-TEST")
    resp = await client.get(
        "/api/admin/jobs",
        headers={"Authorization": f"Bearer {agent_token}"},
    )
    assert resp.status_code == 403


async def test_scheduler_history_accessible_to_super_admin(
    client: AsyncClient, db_session: AsyncSession
):
    """Scheduler execution history returns paginated data."""
    staff_resp = await login_staff(client)
    staff_token = staff_resp.json()["access_token"]

    resp = await client.get(
        "/api/admin/jobs/history",
        headers={"Authorization": f"Bearer {staff_token}"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "items" in data
    assert "total" in data
    assert isinstance(data["items"], list)


async def test_scheduler_idempotency_service_unit():
    """
    Scheduler auto-transition job handles empty tournament table without error.
    Tests job logic directly (not via scheduler_service.run_job which needs real DB).
    """
    import asyncio
    from unittest.mock import AsyncMock, MagicMock, patch

    # Mock DB session
    mock_db = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = []
    mock_db.execute.return_value = mock_result

    from app.jobs.tournament_transition import run_tournament_auto_transition
    count = await run_tournament_auto_transition(mock_db)
    assert count == 0


async def test_performance_refresh_idempotency_unit():
    """
    Daily performance refresh job handles no-agents scenario without error.
    """
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    mock_db = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = []
    mock_db.execute.return_value = mock_result

    from app.jobs.agent_performance import run_daily_performance_refresh
    count = await run_daily_performance_refresh(mock_db)
    assert count == 0


# ============================================================================
# users/me endpoint
# ============================================================================

async def test_get_users_me_returns_safe_fields(client: AsyncClient):
    """GET /api/users/me must return profile without sensitive fields."""
    await register_player(client, username="metest", ff_uid="3330000001")
    login_resp = await client.post(
        "/api/auth/login",
        json={"username": "metest", "password": "SecurePass@123"},
    )
    token = login_resp.json()["access_token"]

    resp = await client.get(
        "/api/users/me",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert "id" in data
    assert "username" in data
    assert data["username"] == "metest"
    assert "ff_uid" in data
    assert "referral_code" in data
    assert "role" in data
    assert data["role"] == "player"

    # Sensitive fields must be absent
    assert "password_hash" not in data
    assert "password" not in data
    assert "token_version" not in data


async def test_get_users_me_requires_auth(client: AsyncClient):
    """GET /api/users/me must return 401 without auth."""
    resp = await client.get("/api/users/me")
    assert resp.status_code == 401


async def test_staff_token_rejected_on_users_me(client: AsyncClient):
    """Staff tokens (audience=staff) must not work on the player /me endpoint."""
    staff_resp = await login_staff(client)
    staff_token = staff_resp.json()["access_token"]

    resp = await client.get(
        "/api/users/me",
        headers={"Authorization": f"Bearer {staff_token}"},
    )
    assert resp.status_code == 401
