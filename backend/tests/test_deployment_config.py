"""
Tests for deployment configuration fixes:
  - Cookie SameSite=None in production for both player and staff auth
  - Cookie Secure=True in production
  - Cookie SameSite=None only when is_production=True
  - Dev mode still uses Secure=False and SameSite=None (omitted/falsy)
"""
from __future__ import annotations

import pytest
from unittest.mock import patch, MagicMock
from fastapi import Response

pytestmark = pytest.mark.asyncio


# ── Helper: import the private cookie helpers ─────────────────────────────────

def _get_player_set_cookie():
    from app.routers.auth import _set_refresh_cookie
    return _set_refresh_cookie


def _get_staff_set_cookie():
    from app.routers.staff_auth import _set_refresh_cookie as _set_staff_cookie
    return _set_staff_cookie


# ── Production cookie — player auth ──────────────────────────────────────────

def test_player_cookie_samesite_none_in_production():
    """In production, player refresh cookie must use SameSite=None (cross-origin Netlify→Render)."""
    mock_settings = MagicMock()
    mock_settings.is_production = True

    captured = {}

    def fake_set_cookie(key, value, **kwargs):
        captured.update(kwargs)
        captured["key"] = key

    response = MagicMock(spec=Response)
    response.set_cookie.side_effect = fake_set_cookie

    with patch("app.routers.auth.get_settings", return_value=mock_settings):
        _set_cookie = _get_player_set_cookie()
        _set_cookie(response, "dummy-token")

    assert captured.get("samesite") == "none", (
        f"Expected samesite='none' in production, got {captured.get('samesite')!r}"
    )
    assert captured.get("secure") is True, (
        f"Expected secure=True in production, got {captured.get('secure')!r}"
    )
    assert captured.get("httponly") is True


def test_player_cookie_samesite_none_in_dev():
    """In development, player refresh cookie must NOT set secure=True (HTTP)."""
    mock_settings = MagicMock()
    mock_settings.is_production = False

    captured = {}

    def fake_set_cookie(key, value, **kwargs):
        captured.update(kwargs)

    response = MagicMock(spec=Response)
    response.set_cookie.side_effect = fake_set_cookie

    with patch("app.routers.auth.get_settings", return_value=mock_settings):
        _set_cookie = _get_player_set_cookie()
        _set_cookie(response, "dummy-token")

    assert captured.get("secure") is False, (
        f"Expected secure=False in development, got {captured.get('secure')!r}"
    )
    # In dev, samesite is None (omitted) — not the string "none"
    assert captured.get("samesite") is None, (
        f"Expected samesite=None in development, got {captured.get('samesite')!r}"
    )


# ── Production cookie — staff auth ────────────────────────────────────────────

def test_staff_cookie_samesite_none_in_production():
    """In production, staff refresh cookie must use SameSite=None."""
    mock_settings = MagicMock()
    mock_settings.is_production = True

    captured = {}

    def fake_set_cookie(key, value, **kwargs):
        captured.update(kwargs)

    response = MagicMock(spec=Response)
    response.set_cookie.side_effect = fake_set_cookie

    with patch("app.routers.staff_auth.get_settings", return_value=mock_settings):
        _set_cookie = _get_staff_set_cookie()
        _set_cookie(response, "dummy-token")

    assert captured.get("samesite") == "none", (
        f"Expected samesite='none' in production, got {captured.get('samesite')!r}"
    )
    assert captured.get("secure") is True, (
        f"Expected secure=True in production, got {captured.get('secure')!r}"
    )
    assert captured.get("httponly") is True


def test_staff_cookie_samesite_none_in_dev():
    """In development, staff refresh cookie must NOT set secure=True."""
    mock_settings = MagicMock()
    mock_settings.is_production = False

    captured = {}

    def fake_set_cookie(key, value, **kwargs):
        captured.update(kwargs)

    response = MagicMock(spec=Response)
    response.set_cookie.side_effect = fake_set_cookie

    with patch("app.routers.staff_auth.get_settings", return_value=mock_settings):
        _set_cookie = _get_staff_set_cookie()
        _set_cookie(response, "dummy-token")

    assert captured.get("secure") is False
    assert captured.get("samesite") is None


# ── Integration: login sets correct cookie header in testing env ──────────────
# The test environment uses APP_ENV=testing (is_production=False), so the cookie
# will be Secure=False, SameSite omitted — matching development behaviour.

async def test_player_login_sets_refresh_cookie(client):
    """Player login response must include a Set-Cookie for refresh_token."""
    from tests.conftest import register_player, login_player

    await register_player(client)
    resp = await login_player(client)
    assert resp.status_code == 200

    set_cookie = resp.headers.get("set-cookie", "")
    assert "refresh_token" in set_cookie
    assert "HttpOnly" in set_cookie
    # In test/dev mode: Secure flag should NOT be present
    assert "Secure" not in set_cookie


async def test_staff_login_sets_refresh_cookie(client):
    """Staff login response must include a Set-Cookie for staff_refresh_token."""
    from tests.conftest import login_staff

    resp = await login_staff(client)
    assert resp.status_code == 200

    set_cookie = resp.headers.get("set-cookie", "")
    assert "staff_refresh_token" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "Secure" not in set_cookie


# ── config.is_production property ─────────────────────────────────────────────

def test_is_production_true_when_app_env_production():
    """Settings.is_production returns True only when APP_ENV=production."""
    import os
    from unittest.mock import patch as _patch

    with _patch.dict(os.environ, {
        "APP_ENV": "production",
        "APP_SECRET_KEY": "x" * 32,
        "DATABASE_URL": "postgresql+asyncpg://u:p@h:5432/db",
        "JWT_SECRET_KEY": "x" * 32,
        "SUPER_ADMIN_AGENT_ID": "SA",
        "SUPER_ADMIN_PASSWORD": "Pass@123456",
    }, clear=False):
        from app.config import Settings
        s = Settings()
        assert s.is_production is True


def test_is_production_false_when_app_env_testing():
    """Settings.is_production returns False for testing env (used by tests)."""
    from app.config import get_settings
    settings = get_settings()
    assert settings.is_production is False


# ── Regression: 422 "loc: query body" bug ────────────────────────────────────
# FastAPI with from __future__ import annotations + @limiter.limit decorator
# used to treat Pydantic body params as query params, returning:
#   {"detail":[{"type":"missing","loc":["query","body"],"msg":"Field required"}]}
# Fix: annotate all rate-limited route body params with Body(...).

async def test_register_no_query_body_422(client):
    """
    POST /api/auth/register with the exact frontend payload must NOT return
    loc=['query','body'] 422.  It must either succeed (201) or return a
    domain validation error (409/400/422 with loc starting with 'body.').
    """
    payload = {
        "username": "regtest_user",
        "ff_uid": "123456789",
        "password": "SecurePass@123",
        "confirm_password": "SecurePass@123",
    }
    resp = await client.post("/api/auth/register", json=payload)

    # Must not be the query-param 422
    if resp.status_code == 422:
        detail = resp.json().get("detail", [])
        for err in detail:
            loc = err.get("loc", [])
            assert loc != ["query", "body"], (
                f"Got the 'loc=[query,body]' 422 bug — Body(...) annotation missing. "
                f"Full detail: {detail}"
            )
    else:
        # 201 created, or a legitimate domain error (409 duplicate, 400, etc.)
        assert resp.status_code in (201, 400, 409), (
            f"Unexpected status {resp.status_code}: {resp.text}"
        )


async def test_login_no_query_body_422(client):
    """
    POST /api/auth/login must not return loc=['query','body'] 422.
    """
    from tests.conftest import register_player
    await register_player(client, username="logintest_qb", ff_uid="111222333")

    resp = await client.post(
        "/api/auth/login",
        json={"username": "logintest_qb", "password": "SecurePass@123"},
    )
    if resp.status_code == 422:
        detail = resp.json().get("detail", [])
        for err in detail:
            assert err.get("loc") != ["query", "body"], (
                f"Got the 'loc=[query,body]' 422 bug on /login: {detail}"
            )
    else:
        assert resp.status_code == 200, f"Unexpected status {resp.status_code}: {resp.text}"


async def test_staff_login_no_query_body_422(client):
    """
    POST /api/admin/auth/login must not return loc=['query','body'] 422.
    """
    from tests.conftest import login_staff
    resp = await login_staff(client)

    if resp.status_code == 422:
        detail = resp.json().get("detail", [])
        for err in detail:
            assert err.get("loc") != ["query", "body"], (
                f"Got the 'loc=[query,body]' 422 bug on /admin/auth/login: {detail}"
            )
    else:
        assert resp.status_code == 200, f"Unexpected status {resp.status_code}: {resp.text}"
