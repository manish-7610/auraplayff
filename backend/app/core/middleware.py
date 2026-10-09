"""
Security and operational middleware:
  - SecurityHeadersMiddleware   — HSTS, CSP, X-Content-Type-Options, etc.
  - RequestIDMiddleware         — unique X-Request-ID on every request/response
  - LastSeenMiddleware          — updates users.last_seen_at on authenticated requests

Rate limiting is applied per-router via slowapi (see rate_limiter.py).
Global exception handlers are registered in main.py.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from fastapi import Request, Response
from sqlalchemy import update
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger("auraplayff.middleware")


# ── Security headers ──────────────────────────────────────────────────────────

class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """
    Adds security HTTP response headers to every response.
    Values are conservative defaults suitable for an SPA + API backend.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)

        # Prevent browsers from sniffing content-type
        response.headers["X-Content-Type-Options"] = "nosniff"

        # Block clickjacking
        response.headers["X-Frame-Options"] = "DENY"

        # Basic XSS protection for older browsers
        response.headers["X-XSS-Protection"] = "1; mode=block"

        # Enforce HTTPS (only useful when served over HTTPS)
        response.headers["Strict-Transport-Security"] = (
            "max-age=31536000; includeSubDomains"
        )

        # Referrer policy
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"

        # Permissions policy — restrict sensitive browser APIs
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=()"
        )

        # Content-Security-Policy — tightened for API-only responses
        # Frontend SPA will need to adjust this for its own assets.
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; "
            "frame-ancestors 'none';"
        )

        return response


# ── Request ID ───────────────────────────────────────────────────────────────

class RequestIDMiddleware(BaseHTTPMiddleware):
    """
    Attaches a unique X-Request-ID to every request and response.
    Enables log correlation across distributed traces.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
        # Make available to downstream handlers via request.state
        request.state.request_id = request_id

        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response


# ── Last-seen update ──────────────────────────────────────────────────────────

class LastSeenMiddleware(BaseHTTPMiddleware):
    """
    Updates users.last_seen_at (or staff_users.last_seen_at) after every
    authenticated request.

    Strategy:
      - Decode the Authorization Bearer token header lightly (no full validation)
        to determine actor type and ID.
      - Issue a fire-and-forget UPDATE — failure is silently logged, never raised.
      - Uses its own short-lived DB session to avoid interfering with the
        request's session lifecycle.

    "Online" definition: last_seen_at within the last 3 minutes.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)

        auth_header = request.headers.get("authorization", "")
        if not auth_header.startswith("Bearer "):
            return response

        token = auth_header.removeprefix("Bearer ").strip()
        if not token:
            return response

        # Fire-and-forget last_seen update
        try:
            await _update_last_seen(token, request)
        except Exception:
            # Never let a last_seen failure affect the response
            logger.debug("last_seen update skipped", exc_info=True)

        return response


async def _update_last_seen(token: str, request: Request) -> None:
    """Decode token (no signature check for performance) and update last_seen_at."""
    import jose.jwt as _jwt

    from app.config import get_settings
    from app.database import get_session_factory
    from app.models.staff_user import StaffUser
    from app.models.user import User

    settings = get_settings()
    try:
        # options={"verify_signature": False} — we only want claims, not validation
        # Real validation already happened in the route dependency.
        unverified = _jwt.decode(
            token,
            settings.jwt_secret_key,
            algorithms=[settings.jwt_algorithm],
            options={
                "verify_signature": True,   # still verify — cheap and safe
                "verify_exp": False,        # don't fail on expiry for last_seen
                "verify_aud": False,        # audience already validated upstream
            },
        )
    except Exception:
        return  # invalid token — skip

    audience = unverified.get("aud")
    subject = unverified.get("sub")
    if not subject:
        return

    # Parse UUID — subject is stored as string in JWT
    try:
        import uuid as _uuid
        subject_uuid = _uuid.UUID(subject)
    except (ValueError, AttributeError):
        return

    now = datetime.now(timezone.utc)
    factory = get_session_factory()

    async with factory() as db:
        try:
            if audience == "player":
                await db.execute(
                    update(User)
                    .where(User.id == subject_uuid)
                    .values(last_seen_at=now)
                )
            elif audience == "staff":
                await db.execute(
                    update(StaffUser)
                    .where(StaffUser.id == subject_uuid)
                    .values(last_seen_at=now)
                )
            await db.commit()
        except Exception:
            await db.rollback()
            logger.debug("last_seen DB update failed", exc_info=True)
