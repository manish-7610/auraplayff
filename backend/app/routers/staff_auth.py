"""
Staff (Admin/Agent) authentication routes:
  POST /api/admin/auth/login
  POST /api/admin/auth/refresh
  POST /api/admin/auth/logout

Completely separate from player auth.
Staff tokens have audience="staff".
Player tokens are rejected by all staff routes.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Body, Cookie, Depends, Request, Response
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.exceptions import (
    AccountInactiveError,
    InvalidCredentialsError,
    TokenInvalidError,
)
from app.core.rate_limiter import limiter
from app.core.security import (
    create_access_token,
    generate_refresh_token,
    hash_token,
    refresh_token_expiry,
    verify_password,
)
from app.database import get_db
from app.models.audit_log import ActorType
from app.models.refresh_token import RefreshToken
from app.models.staff_user import StaffUser
from app.schemas.auth import (
    MessageResponse,
    RefreshResponse,
    StaffLoginRequest,
    TokenResponse,
)
from app.services import audit_service

router = APIRouter(prefix="/api/admin/auth", tags=["Staff Auth"])

_REFRESH_COOKIE = "staff_refresh_token"
_COOKIE_MAX_AGE = 60 * 60 * 24 * 7  # 7 days


def _set_refresh_cookie(response: Response, raw_token: str) -> None:
    settings = get_settings()
    # In production: frontend (Netlify) and backend (Render) are on different
    # origins, so the browser will only send the cookie on cross-origin requests
    # when SameSite=None; Secure=True is set (RFC 6265bis).
    # In local development: Secure=False + SameSite omitted (None) so the
    # httpOnly cookie is sent by the browser on cross-port fetch() calls
    # (e.g. localhost:5500 → localhost:8000 / 127.0.0.1:5500 → 127.0.0.1:8000).
    response.set_cookie(
        key=_REFRESH_COOKIE,
        value=raw_token,
        httponly=True,
        secure=settings.is_production,
        samesite="none" if settings.is_production else None,
        max_age=_COOKIE_MAX_AGE,
        path="/api/admin/auth",
    )


def _clear_refresh_cookie(response: Response) -> None:
    response.delete_cookie(key=_REFRESH_COOKIE, path="/api/admin/auth")


# ── Login ─────────────────────────────────────────────────────────────────────

@router.post(
    "/login",
    response_model=TokenResponse,
    summary="Staff login — agent_id + password",
)
@limiter.limit("5/minute")
async def staff_login(
    request: Request,
    response: Response,
    body: StaffLoginRequest = Body(...),
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    result = await db.execute(
        select(StaffUser).where(StaffUser.agent_id == body.agent_id)
    )
    staff: StaffUser | None = result.scalar_one_or_none()

    dummy_hash = "$2b$12$aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    stored_hash = staff.password_hash if staff else dummy_hash
    password_ok = verify_password(body.password, stored_hash)

    if not staff or not password_ok:
        raise InvalidCredentialsError()
    if not staff.is_active:
        raise AccountInactiveError()

    # BUG #9 FIX: pass display_name and agent_id so they appear in JWT payload,
    # enabling restoreAdminSession() to rebuild the staff profile after page reload.
    access_token = create_access_token(
        subject=staff.id,
        audience="staff",
        token_version=staff.token_version,
        role=staff.role.value,
        display_name=staff.display_name,
        agent_id=staff.agent_id,
    )
    raw_refresh, refresh_hash = generate_refresh_token()

    rt = RefreshToken(
        staff_user_id=staff.id,
        token_hash=refresh_hash,
        token_version=staff.token_version,
        expires_at=refresh_token_expiry(),
        user_agent=request.headers.get("user-agent"),
        ip_address=_get_ip(request),
    )
    db.add(rt)

    await audit_service.log(
        db=db,
        actor_type=ActorType.staff,
        actor_id=str(staff.id),
        action="staff.login",
        entity_type="staff_user",
        entity_id=str(staff.id),
        ip_address=_get_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    await db.commit()

    _set_refresh_cookie(response, raw_refresh)
    return TokenResponse(access_token=access_token)


# ── Refresh ───────────────────────────────────────────────────────────────────

@router.post(
    "/refresh",
    response_model=RefreshResponse,
    summary="Silently refresh staff access token",
)
async def staff_refresh(
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
    refresh_token: str | None = Cookie(default=None, alias=_REFRESH_COOKIE),
) -> RefreshResponse:
    if not refresh_token:
        raise TokenInvalidError("No refresh token provided.")

    token_hash = hash_token(refresh_token)
    now = datetime.now(timezone.utc)

    result = await db.execute(
        select(RefreshToken).where(RefreshToken.token_hash == token_hash)
    )
    rt: RefreshToken | None = result.scalar_one_or_none()

    if rt is None or rt.is_revoked:
        raise TokenInvalidError("Refresh token is invalid.")
    # BUG #1 FIX: use astimezone() instead of replace() — replace() would
    # silently override an already-timezone-aware datetime returned by PostgreSQL
    # (TIMESTAMPTZ), causing incorrect expiry comparisons and spurious 401s.
    rt_expires = rt.expires_at if rt.expires_at.tzinfo is not None else rt.expires_at.replace(tzinfo=timezone.utc)
    if rt_expires.astimezone(timezone.utc) < now:
        raise TokenInvalidError("Refresh token has expired.")
    if rt.staff_user_id is None:
        raise TokenInvalidError("Token type mismatch.")

    result = await db.execute(
        select(StaffUser).where(StaffUser.id == rt.staff_user_id)
    )
    staff: StaffUser | None = result.scalar_one_or_none()

    if staff is None or not staff.is_active:
        raise TokenInvalidError()
    if staff.token_version != rt.token_version:
        raise TokenInvalidError("Session has been invalidated.")

    new_raw, new_hash = generate_refresh_token()

    rt.is_revoked = True
    rt.revoked_at = now
    db.add(rt)

    new_rt = RefreshToken(
        staff_user_id=staff.id,
        token_hash=new_hash,
        token_version=staff.token_version,
        expires_at=refresh_token_expiry(),
        user_agent=request.headers.get("user-agent"),
        ip_address=_get_ip(request),
    )
    db.add(new_rt)
    await db.commit()

    # BUG #9 FIX: include display_name and agent_id in refreshed token too
    access_token = create_access_token(
        subject=staff.id,
        audience="staff",
        token_version=staff.token_version,
        role=staff.role.value,
        display_name=staff.display_name,
        agent_id=staff.agent_id,
    )
    _set_refresh_cookie(response, new_raw)
    return RefreshResponse(access_token=access_token)


# ── Logout ────────────────────────────────────────────────────────────────────

@router.post(
    "/logout",
    response_model=MessageResponse,
    summary="Staff logout — revokes refresh token",
)
async def staff_logout(
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
    refresh_token: str | None = Cookie(default=None, alias=_REFRESH_COOKIE),
) -> MessageResponse:
    if refresh_token:
        token_hash = hash_token(refresh_token)
        result = await db.execute(
            select(RefreshToken).where(RefreshToken.token_hash == token_hash)
        )
        rt: RefreshToken | None = result.scalar_one_or_none()
        if rt and not rt.is_revoked:
            rt.is_revoked = True
            rt.revoked_at = datetime.now(timezone.utc)
            db.add(rt)
            await audit_service.log(
                db=db,
                actor_type=ActorType.staff,
                actor_id=str(rt.staff_user_id),
                action="staff.logout",
                entity_type="staff_user",
                entity_id=str(rt.staff_user_id),
                ip_address=_get_ip(request),
                user_agent=request.headers.get("user-agent"),
            )
            await db.commit()

    _clear_refresh_cookie(response)
    return MessageResponse(message="Logged out successfully.")


def _get_ip(request: Request) -> str | None:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None
