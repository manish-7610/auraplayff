"""
Admin user management routes — Phase 9.

Routes
------
GET  /api/admin/users                          — list / search players
GET  /api/admin/users/{user_id}                — player detail
POST /api/admin/users/{user_id}/ban            — ban player
POST /api/admin/users/{user_id}/unban          — unban player
POST /api/admin/users/{user_id}/issue-password-reset — one-time reset token

All routes require staff authentication.
Financial operations remain protected by RBAC.
Passwords, hashes, and reset token hashes are never returned.
"""
from __future__ import annotations

import uuid
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError, PermissionDeniedError
from app.core.permissions import Permission
from app.core.security import (
    generate_password_reset_token,
    password_reset_token_expiry,
)
from app.database import get_db
from app.dependencies import get_current_staff
from app.models.audit_log import ActorType
from app.models.password_reset_token import PasswordResetToken
from app.models.permission import Permission as PermModel, StaffUserPermission
from app.models.player_stats import PlayerStats
from app.models.staff_user import StaffRole, StaffUser
from app.models.user import User
from app.models.wallet import Wallet
from app.schemas.auth import IssuePasswordResetResponse
from app.schemas.user import AdminUserDetail, AdminUserListResponse, AdminUserSummary
from app.services import audit_service
from app.config import get_settings

router = APIRouter(prefix="/api/admin/users", tags=["Admin — Users"])


# ── Permission helper ─────────────────────────────────────────────────────────

async def _check_perm(staff: StaffUser, db: AsyncSession, perm: Permission) -> None:
    if staff.role == StaffRole.super_admin:
        return
    # BUG #7 FIX: joinedload replaces N separate db.get() calls
    from sqlalchemy.orm import joinedload
    result = await db.execute(
        select(StaffUserPermission)
        .options(joinedload(StaffUserPermission.permission))
        .where(StaffUserPermission.staff_user_id == staff.id)
    )
    grants = result.unique().scalars().all()
    granted: set[str] = {
        g.permission.code for g in grants if g.permission is not None
    }
    if perm.value not in granted:
        raise PermissionDeniedError(perm.value)


def _get_ip(request: Request) -> str | None:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


# ── List players ──────────────────────────────────────────────────────────────

@router.get(
    "",
    response_model=AdminUserListResponse,
    summary="List / search players",
)
async def list_users(
    search: Annotated[Optional[str], Query(max_length=60)] = None,
    is_active: Annotated[Optional[bool], Query()] = None,
    is_banned: Annotated[Optional[bool], Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    staff: StaffUser = Depends(get_current_staff),
    db: AsyncSession = Depends(get_db),
) -> AdminUserListResponse:
    """
    Paginated player list with optional search by username or ff_uid.
    Requires user.view permission (super_admin always passes).
    """
    await _check_perm(staff, db, Permission.USER_VIEW)

    q = select(User)
    if search:
        term = f"%{search.strip()}%"
        q = q.where(
            or_(
                User.username.ilike(term),
                User.ff_uid.ilike(term),
            )
        )
    if is_active is not None:
        q = q.where(User.is_active == is_active)
    if is_banned is not None:
        q = q.where(User.is_banned == is_banned)

    count_q = select(func.count()).select_from(q.subquery())
    total = (await db.execute(count_q)).scalar_one()

    rows = (
        await db.execute(
            q.order_by(User.created_at.desc()).limit(limit).offset(offset)
        )
    ).scalars().all()

    # Bulk-load stats
    user_ids = [u.id for u in rows]
    stats_rows = (
        await db.execute(
            select(PlayerStats).where(PlayerStats.user_id.in_(user_ids))
        )
    ).scalars().all()
    stats_map = {s.user_id: s for s in stats_rows}

    items: list[AdminUserSummary] = []
    for u in rows:
        s = stats_map.get(u.id)
        items.append(
            AdminUserSummary(
                id=str(u.id),
                username=u.username,
                ff_uid=u.ff_uid,
                referral_code=u.referral_code,
                is_active=u.is_active,
                is_banned=u.is_banned,
                created_at=u.created_at,
                last_seen_at=u.last_seen_at,
                total_tournaments=s.total_tournaments if s else 0,
                total_wins=s.total_wins if s else 0,
                total_kills=s.total_kills if s else 0,
                total_winnings=str(s.total_winnings) if s else "0.00",
            )
        )

    return AdminUserListResponse(items=items, total=total, limit=limit, offset=offset)


# ── Get player detail ─────────────────────────────────────────────────────────

@router.get(
    "/{user_id}",
    response_model=AdminUserDetail,
    summary="Full player profile (admin view)",
)
async def get_user(
    user_id: uuid.UUID,
    staff: StaffUser = Depends(get_current_staff),
    db: AsyncSession = Depends(get_db),
) -> AdminUserDetail:
    """
    Full player detail including wallet balances and stats.
    Never returns password_hash or reset tokens.
    """
    await _check_perm(staff, db, Permission.USER_VIEW)

    user = (
        await db.execute(select(User).where(User.id == user_id))
    ).scalar_one_or_none()
    if user is None:
        raise NotFoundError("User")

    stats = (
        await db.execute(select(PlayerStats).where(PlayerStats.user_id == user.id))
    ).scalar_one_or_none()

    wallet = (
        await db.execute(select(Wallet).where(Wallet.user_id == user.id))
    ).scalar_one_or_none()

    return AdminUserDetail(
        id=str(user.id),
        username=user.username,
        ff_uid=user.ff_uid,
        referral_code=user.referral_code,
        is_active=user.is_active,
        is_banned=user.is_banned,
        avatar_url=user.avatar_url,
        created_at=user.created_at,
        last_seen_at=user.last_seen_at,
        referred_by_id=str(user.referred_by_id) if user.referred_by_id else None,
        referral_rewarded=user.referral_rewarded,
        total_tournaments=stats.total_tournaments if stats else 0,
        total_wins=stats.total_wins if stats else 0,
        total_kills=stats.total_kills if stats else 0,
        total_winnings=str(stats.total_winnings) if stats else "0.00",
        deposit_balance=str(wallet.deposit_balance) if wallet else "0.00",
        winning_balance=str(wallet.winning_balance) if wallet else "0.00",
        referral_balance=str(wallet.referral_balance) if wallet else "0.00",
        withdrawal_hold=str(wallet.withdrawal_hold) if wallet else "0.00",
        is_frozen=wallet.is_frozen if wallet else False,
    )


# ── Ban / Unban ───────────────────────────────────────────────────────────────

@router.post(
    "/{user_id}/ban",
    summary="Ban a player account",
)
async def ban_user(
    user_id: uuid.UUID,
    request: Request,
    staff: StaffUser = Depends(get_current_staff),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Ban a player. Sets is_banned=True and is_active=False.
    Also bumps token_version to immediately invalidate all active sessions.
    Requires user.ban permission (super_admin bypasses).
    """
    await _check_perm(staff, db, Permission.USER_BAN)

    user = (
        await db.execute(select(User).where(User.id == user_id))
    ).scalar_one_or_none()
    if user is None:
        raise NotFoundError("User")

    old_state = {"is_banned": user.is_banned, "is_active": user.is_active}
    user.is_banned = True
    user.is_active = False
    user.token_version += 1  # invalidates all sessions
    db.add(user)

    await audit_service.log(
        db=db,
        actor_type=ActorType.staff,
        actor_id=str(staff.id),
        action="user.banned",
        entity_type="user",
        entity_id=str(user.id),
        old_value=old_state,
        new_value={"is_banned": True, "is_active": False},
        ip_address=_get_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    await db.commit()
    return {"message": f"Player {user.username} has been banned.", "user_id": str(user.id)}


@router.post(
    "/{user_id}/unban",
    summary="Unban a player account",
)
async def unban_user(
    user_id: uuid.UUID,
    request: Request,
    staff: StaffUser = Depends(get_current_staff),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Unban a player. Sets is_banned=False and is_active=True.
    Requires user.ban permission.
    """
    await _check_perm(staff, db, Permission.USER_BAN)

    user = (
        await db.execute(select(User).where(User.id == user_id))
    ).scalar_one_or_none()
    if user is None:
        raise NotFoundError("User")

    old_state = {"is_banned": user.is_banned, "is_active": user.is_active}
    user.is_banned = False
    user.is_active = True
    db.add(user)

    await audit_service.log(
        db=db,
        actor_type=ActorType.staff,
        actor_id=str(staff.id),
        action="user.unbanned",
        entity_type="user",
        entity_id=str(user.id),
        old_value=old_state,
        new_value={"is_banned": False, "is_active": True},
        ip_address=_get_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    await db.commit()
    return {"message": f"Player {user.username} has been unbanned.", "user_id": str(user.id)}


# ── Issue password reset ──────────────────────────────────────────────────────

@router.post(
    "/{user_id}/issue-password-reset",
    response_model=IssuePasswordResetResponse,
    summary="Issue a one-time password reset token for a player",
)
async def issue_password_reset(
    user_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_staff: StaffUser = Depends(get_current_staff),
) -> IssuePasswordResetResponse:
    """
    Super Admin only — issue a one-time password reset token for a player.

    Security notes:
    - Restricted to Super Admin. Agents are explicitly blocked regardless of
      any user.view permission grant, because password reset is a privileged
      account-takeover-capable action.
    - The raw token is returned once and never stored.
    - The hash is stored; the token cannot be reverse-engineered from the DB.
    - Any previous active reset tokens for this user are invalidated.
    - Successful reset increments token_version (invalidates all sessions).
    """
    # Password reset is Super Admin only — agents must never issue resets.
    if current_staff.role != StaffRole.super_admin:
        raise PermissionDeniedError("password_reset.super_admin_only")

    result = await db.execute(select(User).where(User.id == user_id))
    user: User | None = result.scalar_one_or_none()
    if user is None:
        raise NotFoundError("User")

    settings = get_settings()
    raw_token, token_hash = generate_password_reset_token()
    expires_at = password_reset_token_expiry()

    # Invalidate any existing active tokens for this user
    await db.execute(
        update(PasswordResetToken)
        .where(
            PasswordResetToken.user_id == user.id,
            PasswordResetToken.is_active == True,  # noqa: E712
        )
        .values(is_active=False)
    )

    prt = PasswordResetToken(
        user_id=user.id,
        token_hash=token_hash,
        expires_at=expires_at,
        issued_by_id=current_staff.id,
    )
    db.add(prt)

    await audit_service.log(
        db=db,
        actor_type=ActorType.staff,
        actor_id=str(current_staff.id),
        action="password_reset.issued",
        entity_type="user",
        entity_id=str(user.id),
        new_value={
            "issued_by": current_staff.agent_id,
            "expires_at": expires_at.isoformat(),
        },
        ip_address=_get_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    await db.commit()

    return IssuePasswordResetResponse(
        reset_token=raw_token,
        expires_in_minutes=settings.password_reset_token_expire_minutes,
    )


# ── Issue password reset from a recovery request ─────────────────────────────

@router.post(
    "/by-username/{username}/issue-password-reset",
    response_model=IssuePasswordResetResponse,
    summary="Issue reset token by username (Super Admin only — for recovery requests)",
)
async def issue_password_reset_by_username(
    username: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_staff: StaffUser = Depends(get_current_staff),
) -> IssuePasswordResetResponse:
    """
    Issue a one-time password reset token by username, for use after manually
    verifying a recovery request.

    Super Admin only — agents are explicitly blocked.
    The admin must have already verified account ownership before calling this.
    Returns the raw token once; it is never stored in plaintext.
    """
    if current_staff.role != StaffRole.super_admin:
        raise PermissionDeniedError("password_reset.super_admin_only")

    result = await db.execute(select(User).where(User.username == username.strip()))
    user: User | None = result.scalar_one_or_none()
    if user is None:
        raise NotFoundError("User")

    settings = get_settings()
    raw_token, token_hash = generate_password_reset_token()
    expires_at = password_reset_token_expiry()

    await db.execute(
        update(PasswordResetToken)
        .where(
            PasswordResetToken.user_id == user.id,
            PasswordResetToken.is_active == True,  # noqa: E712
        )
        .values(is_active=False)
    )

    prt = PasswordResetToken(
        user_id=user.id,
        token_hash=token_hash,
        expires_at=expires_at,
        issued_by_id=current_staff.id,
    )
    db.add(prt)

    await audit_service.log(
        db=db,
        actor_type=ActorType.staff,
        actor_id=str(current_staff.id),
        action="password_reset.issued_from_recovery",
        entity_type="user",
        entity_id=str(user.id),
        new_value={
            "issued_by": current_staff.agent_id,
            "expires_at": expires_at.isoformat(),
            "trigger": "recovery_request",
        },
        ip_address=_get_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    await db.commit()

    return IssuePasswordResetResponse(
        reset_token=raw_token,
        expires_in_minutes=settings.password_reset_token_expire_minutes,
    )
