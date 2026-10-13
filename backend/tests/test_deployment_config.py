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
