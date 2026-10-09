"""
Shared FastAPI dependencies.

  get_db             — async DB session (re-exported from database.py)
  get_current_user   — authenticated player (audience="player")
  get_current_staff  — authenticated staff user (audience="staff")
  require_permission — factory that returns a dependency enforcing a specific permission

Token flow:
  Player:  Authorization: Bearer <access_token>  (audience "player")
  Staff:   Authorization: Bearer <access_token>  (audience "staff")

  Refresh tokens arrive via httpOnly cookie "refresh_token".
  Access tokens in the Authorization header are never sent as cookies.
"""
from __future__ import annotations

import uuid
from typing import Callable

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    AccountBannedError,
    AccountInactiveError,
    PermissionDeniedError,
    SessionInvalidatedError,
    TokenInvalidError,
)
from app.core.security import (
    decode_access_token,
    get_subject_from_payload,
    get_token_version_from_payload,
)
from app.database import get_db
from app.models.permission import StaffUserPermission
from app.models.staff_user import StaffRole, StaffUser
from app.models.user import User

# Re-export get_db so callers can import from one place
__all__ = [
    "get_db",
    "get_current_user",
    "get_current_staff",
    "require_permission",
]

_bearer = HTTPBearer(auto_error=False)


# ── Player dependency ─────────────────────────────────────────────────────────

async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: AsyncSession = Depends(get_db),
) -> User:
    """
    Extract and validate a player JWT from the Authorization header.
    Returns the authenticated User ORM object.
    Raises 401/403 on any failure.
    """
    if not credentials:
        raise TokenInvalidError("Authentication credentials were not provided.")

    try:
        payload = decode_access_token(credentials.credentials, expected_audience="player")
    except JWTError as exc:
        _raise_jwt_error(exc)

    subject = get_subject_from_payload(payload)
    token_version = get_token_version_from_payload(payload)

    try:
        user_id = uuid.UUID(subject)
    except ValueError:
        raise TokenInvalidError()

    result = await db.execute(select(User).where(User.id == user_id))
    user: User | None = result.scalar_one_or_none()

    if user is None:
        raise TokenInvalidError()
    if user.token_version != token_version:
        raise SessionInvalidatedError()
    if not user.is_active:
        raise AccountInactiveError()
    if user.is_banned:
        raise AccountBannedError()

    return user


# ── Staff dependency ──────────────────────────────────────────────────────────

async def get_current_staff(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: AsyncSession = Depends(get_db),
) -> StaffUser:
    """
    Extract and validate a staff JWT from the Authorization header.
    Returns the authenticated StaffUser ORM object.
    Raises 401/403 on any failure.
    A player token passed here will fail audience validation.
    """
    if not credentials:
        raise TokenInvalidError("Authentication credentials were not provided.")

    try:
        payload = decode_access_token(credentials.credentials, expected_audience="staff")
    except JWTError as exc:
        _raise_jwt_error(exc)

    subject = get_subject_from_payload(payload)
    token_version = get_token_version_from_payload(payload)

    try:
        staff_id = uuid.UUID(subject)
    except ValueError:
        raise TokenInvalidError()

    result = await db.execute(select(StaffUser).where(StaffUser.id == staff_id))
    staff: StaffUser | None = result.scalar_one_or_none()

    if staff is None:
        raise TokenInvalidError()
    if staff.token_version != token_version:
        raise SessionInvalidatedError()
    if not staff.is_active:
        raise AccountInactiveError()

    return staff


# ── RBAC — require_permission factory ────────────────────────────────────────

def require_permission(permission_code: str) -> Callable:
    """
    Dependency factory.  Usage:

        @router.post("/tournaments")
        async def create(staff = Depends(require_permission("tournament.create"))):
            ...

    Super Admin bypasses the DB check.
    Agents must have an explicit grant in staff_user_permissions.
    """

    async def _check(
        staff: StaffUser = Depends(get_current_staff),
        db: AsyncSession = Depends(get_db),
    ) -> StaffUser:
        if staff.role == StaffRole.super_admin:
            return staff  # super admin always passes

        # BUG #7 FIX: use joinedload to fetch all permission codes in a single
        # query instead of N separate db.get() calls (one per permission row).
        from sqlalchemy.orm import joinedload
        from app.models.permission import Permission as PermModel
        result = await db.execute(
            select(StaffUserPermission)
            .options(joinedload(StaffUserPermission.permission))
            .where(StaffUserPermission.staff_user_id == staff.id)
        )
        grants = result.unique().scalars().all()

        granted_codes: set[str] = {
            grant.permission.code
            for grant in grants
            if grant.permission is not None
        }

        if permission_code not in granted_codes:
            raise PermissionDeniedError(permission_code)

        return staff

    return Depends(_check)


# ── Helper ────────────────────────────────────────────────────────────────────

def _raise_jwt_error(exc: JWTError) -> None:
    """Convert jose JWTError to our AppError hierarchy."""
    msg = str(exc).lower()
    if "expired" in msg:
        from app.core.exceptions import TokenExpiredError
        raise TokenExpiredError()
    raise TokenInvalidError()


def get_client_ip(request: Request) -> str | None:
    """Extract real client IP, respecting X-Forwarded-For behind a proxy."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client:
        return request.client.host
    return None
