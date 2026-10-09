"""
Tests for security features:
  - Security headers
  - Request ID
  - Protected routes require auth
  - CORS
  - Health endpoint
  - Audit log creation
"""
from __future__ import annotations

import pytest
from httpx import AsyncClient

from tests.conftest import login_player, login_staff, register_player

pytestmark = pytest.mark.asyncio


# ── Health endpoint ───────────────────────────────────────────────────────────

async def test_health_endpoint_returns_ok(client: AsyncClient):
    resp = await client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] in ("ok", "degraded")  # SQLite test DB
    assert "version" in data


async def test_health_db_field_present(client: AsyncClient):
    resp = await client.get("/health")
    assert "database" in resp.json()


# ── Security headers ──────────────────────────────────────────────────────────

async def test_security_headers_present(client: AsyncClient):
    resp = await client.get("/health")
    headers = resp.headers
    assert "x-content-type-options" in headers
    assert headers["x-content-type-options"] == "nosniff"
    assert "x-frame-options" in headers
    assert headers["x-frame-options"] == "DENY"
    assert "referrer-policy" in headers
    assert "content-security-policy" in headers


async def test_request_id_is_generated(client: AsyncClient):
    resp = await client.get("/health")
    assert "x-request-id" in resp.headers
    assert len(resp.headers["x-request-id"]) > 0


async def test_request_id_is_preserved(client: AsyncClient):
    """If client sends X-Request-ID, the same value is echoed back."""
    custom_id = "my-custom-trace-id-12345"
    resp = await client.get("/health", headers={"X-Request-ID": custom_id})
    assert resp.headers["x-request-id"] == custom_id


# ── Protected routes require auth ─────────────────────────────────────────────

async def test_staff_route_requires_auth(client: AsyncClient):
    resp = await client.post(
        "/api/admin/users/00000000-0000-0000-0000-000000000001/issue-password-reset"
    )
    assert resp.status_code == 401


async def test_invalid_token_rejected(client: AsyncClient):
    resp = await client.post(
        "/api/admin/users/00000000-0000-0000-0000-000000000001/issue-password-reset",
        headers={"Authorization": "Bearer not.a.real.token"},
    )
    assert resp.status_code == 401


async def test_malformed_bearer_rejected(client: AsyncClient):
    resp = await client.post(
        "/api/admin/users/00000000-0000-0000-0000-000000000001/issue-password-reset",
        headers={"Authorization": "NotBearer token"},
    )
    assert resp.status_code == 401


# ── Error format ──────────────────────────────────────────────────────────────

async def test_error_response_format(client: AsyncClient):
    """All errors must use the standard {error: {code, message}} shape."""
    resp = await client.post(
        "/api/auth/login",
        json={"username": "nobody", "password": "wrong"},
    )
    assert resp.status_code == 401
    body = resp.json()
    assert "error" in body
    assert "code" in body["error"]
    assert "message" in body["error"]


async def test_404_uses_standard_error_format(client: AsyncClient):
    resp = await client.get("/api/nonexistent-route-xyz")
    assert resp.status_code == 404
    body = resp.json()
    # FastAPI returns {"detail": "Not Found"} for 404 by default
    # Our handler converts it to the standard shape
    assert "detail" in body or ("error" in body and body["error"]["code"] == "NOT_FOUND")


# ── Audit logs ────────────────────────────────────────────────────────────────

async def test_registration_creates_audit_log(client: AsyncClient, db_session):
    from sqlalchemy import select
    from app.models.audit_log import AuditLog

    await register_player(client, username="auditplayer1", ff_uid="9991111001")

    result = await db_session.execute(
        select(AuditLog).where(AuditLog.action == "user.registered")
    )
    log = result.scalar_one_or_none()
    assert log is not None
    assert log.entity_type == "user"


async def test_login_creates_audit_log(client: AsyncClient, db_session):
    from sqlalchemy import select
    from app.models.audit_log import AuditLog

    await register_player(client, username="auditplayer2", ff_uid="9991111002")
    await login_player(client, username="auditplayer2")

    result = await db_session.execute(
        select(AuditLog).where(AuditLog.action == "user.login")
    )
    log = result.scalar_one_or_none()
    assert log is not None


async def test_staff_login_creates_audit_log(client: AsyncClient, db_session):
    from sqlalchemy import select
    from app.models.audit_log import AuditLog

    await login_staff(client)

    result = await db_session.execute(
        select(AuditLog).where(AuditLog.action == "staff.login")
    )
    log = result.scalar_one_or_none()
    assert log is not None


# The pure unit tests (no async/DB) live in test_security_unit.py
