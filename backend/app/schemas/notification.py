"""
Notification schemas — Phase 7.

Player and Staff (agent/admin) views of notifications.
All schemas use Pydantic v2.

Security rules enforced at the router layer:
  - Players only see their own notifications (user_id == current_user.id).
  - Agents only see their own notifications (staff_user_id == current_staff.id).
  - Super Admin sees all staff notifications; NOT player notifications unless
    through the admin users endpoint.
  - read_at / is_read are set server-side; clients cannot spoof them.
  - related_entity_id is returned for deep-linking only; never used as a
    security gate.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class NotificationResponse(BaseModel):
    """Single notification record — shared between player and staff views."""
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    notification_type: str
    title: str
    message: str
    related_entity_type: Optional[str] = None
    related_entity_id: Optional[str] = None
    is_read: bool
    read_at: Optional[datetime] = None
    created_at: datetime
    expires_at: Optional[datetime] = None


class NotificationListResponse(BaseModel):
    """Paginated list of notifications."""
    items: list[NotificationResponse]
    total: int
    limit: int
    offset: int
    unread_count: int


class UnreadCountResponse(BaseModel):
    """Quick unread count — low-cost poll response."""
    unread_count: int


class MarkReadResponse(BaseModel):
    """Confirmation after marking notification(s) read."""
    marked_read: int
