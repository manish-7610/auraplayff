"""
Notification APIs — Phase 7.

Player endpoints (JWT audience="player"):
  GET  /api/notifications                 — paginated list (own only)
  GET  /api/notifications/unread-count    — quick poll
  POST /api/notifications/{id}/read       — mark one as read
  POST /api/notifications/read-all        — mark all as read

Staff endpoints (JWT audience="staff"):
  GET  /api/staff/notifications                — paginated list (own only)
  GET  /api/staff/notifications/unread-count   — quick poll
  POST /api/staff/notifications/{id}/read      — mark one as read
  POST /api/staff/notifications/read-all       — mark all as read

Security
--------
- Players only see notifications where user_id == current_user.id.
- Staff only see notifications where staff_user_id == current_staff.id.
  (Super Admin sees all *staff* notifications — not player notifications.)
- The recipient column is never derived from client input.
- Marking another user's notification as read raises 404 (row not found
  under the ownership filter — no information leakage about existence).
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppError
from app.database import get_db
from app.dependencies import get_current_staff, get_current_user
from app.models._types import utcnow
from app.models.notification import Notification, RecipientType
from app.models.staff_user import StaffRole, StaffUser
from app.models.user import User
from app.schemas.notification import (
    MarkReadResponse,
    NotificationListResponse,
    NotificationResponse,
    UnreadCountResponse,
)

# ── Routers ───────────────────────────────────────────────────────────────────

player_router = APIRouter(prefix="/api/notifications", tags=["notifications-player"])
staff_router = APIRouter(prefix="/api/staff/notifications", tags=["notifications-staff"])


# ── Custom exceptions ─────────────────────────────────────────────────────────

class NotificationNotFoundError(AppError):
    def __init__(self) -> None:
        super().__init__("notification_not_found", "Notification not found.", 404)


# ── Shared query helpers ──────────────────────────────────────────────────────

def _player_base_query(user_id: uuid.UUID):
    """Base filter: player sees only own notifications."""
    return select(Notification).where(Notification.user_id == user_id)


def _staff_base_query(staff: StaffUser):
    """
    Base filter for staff:
      - Super Admin sees ALL staff notifications (both agent and admin types).
      - Agent sees only their own notifications.
    """
    if staff.role == StaffRole.super_admin:
        # All staff/admin notifications — NOT player notifications
        return select(Notification).where(
            Notification.staff_user_id.is_not(None)
        )
    return select(Notification).where(Notification.staff_user_id == staff.id)


def _notification_to_response(n: Notification) -> NotificationResponse:
    return NotificationResponse(
        id=n.id,
        notification_type=n.notification_type,
        title=n.title,
        message=n.message,
        related_entity_type=n.related_entity_type,
        related_entity_id=n.related_entity_id,
        is_read=n.is_read,
        read_at=n.read_at,
        created_at=n.created_at,
        expires_at=n.expires_at,
    )


# ── Player endpoints ──────────────────────────────────────────────────────────

@player_router.get("", response_model=NotificationListResponse)
async def player_list_notifications(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    unread_only: bool = Query(False),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> NotificationListResponse:
    """
    Return paginated notifications for the authenticated player.
    Newest first.  Optionally filtered to unread only.
    """
    base_q = _player_base_query(current_user.id)
    if unread_only:
        base_q = base_q.where(Notification.is_read == False)  # noqa: E712

    count_result = await db.execute(select(func.count()).select_from(base_q.subquery()))
    total = count_result.scalar_one()

    unread_q = _player_base_query(current_user.id).where(
        Notification.is_read == False  # noqa: E712
    )
    unread_result = await db.execute(select(func.count()).select_from(unread_q.subquery()))
    unread_count = unread_result.scalar_one()

    rows_result = await db.execute(
        base_q.order_by(Notification.created_at.desc()).limit(limit).offset(offset)
    )
    items = [_notification_to_response(n) for n in rows_result.scalars().all()]

    return NotificationListResponse(
        items=items,
        total=total,
        limit=limit,
        offset=offset,
        unread_count=unread_count,
    )


@player_router.get("/unread-count", response_model=UnreadCountResponse)
async def player_unread_count(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> UnreadCountResponse:
    """Quick poll: return only the unread notification count for the player."""
    result = await db.execute(
        select(func.count(Notification.id)).where(
            Notification.user_id == current_user.id,
            Notification.is_read == False,  # noqa: E712
        )
    )
    return UnreadCountResponse(unread_count=result.scalar_one() or 0)


@player_router.post("/{notification_id}/read", response_model=MarkReadResponse)
async def player_mark_one_read(
    notification_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MarkReadResponse:
    """
    Mark a single notification as read.
    Only succeeds if the notification belongs to the authenticated player.
    """
    result = await db.execute(
        select(Notification).where(
            Notification.id == notification_id,
            Notification.user_id == current_user.id,
        )
    )
    notif: Notification | None = result.scalar_one_or_none()
    if notif is None:
        raise NotificationNotFoundError()

    if not notif.is_read:
        now = utcnow()
        notif.is_read = True
        notif.read_at = now
        async with db.begin_nested():
            pass  # flush happens on commit

    await db.commit()
    return MarkReadResponse(marked_read=1)


@player_router.post("/read-all", response_model=MarkReadResponse)
async def player_mark_all_read(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MarkReadResponse:
    """Mark all unread notifications as read for the authenticated player."""
    result = await db.execute(
        select(Notification).where(
            Notification.user_id == current_user.id,
            Notification.is_read == False,  # noqa: E712
        )
    )
    unread = list(result.scalars().all())
    now = utcnow()
    for n in unread:
        n.is_read = True
        n.read_at = now

    await db.commit()
    return MarkReadResponse(marked_read=len(unread))


# ── Staff endpoints ───────────────────────────────────────────────────────────

@staff_router.get("", response_model=NotificationListResponse)
async def staff_list_notifications(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    unread_only: bool = Query(False),
    current_staff: StaffUser = Depends(get_current_staff),
    db: AsyncSession = Depends(get_db),
) -> NotificationListResponse:
    """
    Return paginated notifications for the authenticated staff member.
    Super Admin sees all staff notifications.
    Agents see only their own.
    """
    base_q = _staff_base_query(current_staff)
    if unread_only:
        base_q = base_q.where(Notification.is_read == False)  # noqa: E712

    count_result = await db.execute(select(func.count()).select_from(base_q.subquery()))
    total = count_result.scalar_one()

    unread_base = _staff_base_query(current_staff).where(
        Notification.is_read == False  # noqa: E712
    )
    unread_result = await db.execute(select(func.count()).select_from(unread_base.subquery()))
    unread_count = unread_result.scalar_one()

    rows_result = await db.execute(
        base_q.order_by(Notification.created_at.desc()).limit(limit).offset(offset)
    )
    items = [_notification_to_response(n) for n in rows_result.scalars().all()]

    return NotificationListResponse(
        items=items,
        total=total,
        limit=limit,
        offset=offset,
        unread_count=unread_count,
    )


@staff_router.get("/unread-count", response_model=UnreadCountResponse)
async def staff_unread_count(
    current_staff: StaffUser = Depends(get_current_staff),
    db: AsyncSession = Depends(get_db),
) -> UnreadCountResponse:
    """Quick poll: unread notification count for the authenticated staff member."""
    base_q = _staff_base_query(current_staff).where(
        Notification.is_read == False  # noqa: E712
    )
    result = await db.execute(select(func.count()).select_from(base_q.subquery()))
    return UnreadCountResponse(unread_count=result.scalar_one() or 0)


@staff_router.post("/{notification_id}/read", response_model=MarkReadResponse)
async def staff_mark_one_read(
    notification_id: uuid.UUID,
    current_staff: StaffUser = Depends(get_current_staff),
    db: AsyncSession = Depends(get_db),
) -> MarkReadResponse:
    """
    Mark a single notification as read.

    Super Admin can mark any staff notification.
    Agents can only mark their own.
    """
    # Build the ownership filter
    if current_staff.role == StaffRole.super_admin:
        ownership_filter = Notification.staff_user_id.is_not(None)
    else:
        ownership_filter = Notification.staff_user_id == current_staff.id

    result = await db.execute(
        select(Notification).where(
            Notification.id == notification_id,
            ownership_filter,
        )
    )
    notif: Notification | None = result.scalar_one_or_none()
    if notif is None:
        raise NotificationNotFoundError()

    if not notif.is_read:
        notif.is_read = True
        notif.read_at = utcnow()

    await db.commit()
    return MarkReadResponse(marked_read=1)


@staff_router.post("/read-all", response_model=MarkReadResponse)
async def staff_mark_all_read(
    current_staff: StaffUser = Depends(get_current_staff),
    db: AsyncSession = Depends(get_db),
) -> MarkReadResponse:
    """
    Mark all unread notifications as read.

    Super Admin clears all unread staff notifications.
    Agents only clear their own.
    """
    base_q = _staff_base_query(current_staff).where(
        Notification.is_read == False  # noqa: E712
    )
    result = await db.execute(base_q)
    unread = list(result.scalars().all())
    now = utcnow()
    for n in unread:
        n.is_read = True
        n.read_at = now

    await db.commit()
    return MarkReadResponse(marked_read=len(unread))
