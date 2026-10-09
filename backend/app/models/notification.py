"""
Notification model — Phase 7.

Supports player, agent, and admin notifications.
Notifications are generated only when actual backend events occur.
Secrets are never stored here.

Recipient resolution:
  - player notifications: user_id IS NOT NULL, staff_user_id IS NULL
  - agent/admin notifications: staff_user_id IS NOT NULL, user_id IS NULL

The recipient is always derived from authenticated identity or system logic —
never from client input.

NotificationType values are kept as plain strings (VARCHAR) so new event
types can be added without a migration.
"""
from __future__ import annotations

import enum as pyenum
import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models._types import utcnow

_jsonb = JSONB().with_variant(JSON(), "sqlite")


# ── Recipient type ────────────────────────────────────────────────────────────

class RecipientType(str, pyenum.Enum):
    player = "player"
    agent  = "agent"
    admin  = "admin"


# ── Notification type constants ───────────────────────────────────────────────
# Kept as plain string constants (not an Enum) so we never need ALTER TYPE.

class NotificationType:
    # ── Account ───────────────────────────────────────────────────────────────
    REGISTRATION_SUCCESS        = "account.registration_success"
    PASSWORD_RESET_REQUESTED    = "account.password_reset_requested"
    PASSWORD_CHANGED            = "account.password_changed"

    # ── Tournament (player) ───────────────────────────────────────────────────
    TOURNAMENT_JOINED           = "tournament.joined"
    TOURNAMENT_CANCELLED        = "tournament.cancelled"
    TOURNAMENT_ROOM_RELEASED    = "tournament.room_released"
    TOURNAMENT_STARTED          = "tournament.started"
    TOURNAMENT_COMPLETED        = "tournament.completed"
    RESULT_FINALIZED            = "tournament.result_finalized"
    PRIZE_CREDITED              = "tournament.prize_credited"
    KILL_REWARD_CREDITED        = "tournament.kill_reward_credited"

    # ── Deposit ───────────────────────────────────────────────────────────────
    DEPOSIT_INITIATED           = "deposit.initiated"
    DEPOSIT_ONLINE_SUCCESS      = "deposit.online_success"
    DEPOSIT_MANUAL_SUBMITTED    = "deposit.manual_submitted"
    DEPOSIT_APPROVED            = "deposit.approved"
    DEPOSIT_REJECTED            = "deposit.rejected"

    # ── Withdrawal ────────────────────────────────────────────────────────────
    WITHDRAWAL_SUBMITTED        = "withdrawal.submitted"
    WITHDRAWAL_PROCESSING       = "withdrawal.processing"
    WITHDRAWAL_PAID             = "withdrawal.paid"
    WITHDRAWAL_REJECTED         = "withdrawal.rejected"

    # ── Referral ──────────────────────────────────────────────────────────────
    REFERRAL_REWARD_CREDITED    = "referral.reward_credited"

    # ── Agent notifications ───────────────────────────────────────────────────
    AGENT_TOURNAMENT_CREATED    = "agent.tournament_created"
    AGENT_TOURNAMENT_CANCELLED  = "agent.tournament_cancelled"
    AGENT_ROOM_RELEASE_REMINDER = "agent.room_release_reminder"
    AGENT_DAILY_TARGET_STATUS   = "agent.daily_target_status"
    AGENT_TARGET_COMPLETED      = "agent.target_completed"
    AGENT_EXTRA_BONUS_EARNED    = "agent.extra_bonus_earned"
    AGENT_WARNING_ISSUED        = "agent.warning_issued"
    AGENT_GRACE_GRANTED         = "agent.grace_granted"
    AGENT_SALARY_CALCULATED     = "agent.salary_calculated"
    AGENT_SALARY_ADJUSTED       = "agent.salary_adjusted"

    # ── Admin / system notifications ──────────────────────────────────────────
    ADMIN_DEPOSIT_PENDING       = "admin.deposit_pending"
    ADMIN_WITHDRAWAL_PENDING    = "admin.withdrawal_pending"
    ADMIN_TOURNAMENT_CANCELLED  = "admin.tournament_cancelled"
    ADMIN_RESULT_PENDING_VERIFY = "admin.result_pending_verification"
    ADMIN_SCREENSHOT_SUBMITTED  = "admin.screenshot_submitted"
    ADMIN_AGENT_TARGET_ISSUE    = "admin.agent_target_issue"
    ADMIN_AGENT_WARNING_ACTION  = "admin.agent_warning_action"
    ADMIN_AGENT_GRACE_ACTION    = "admin.agent_grace_action"
    ADMIN_SYSTEM_ERROR          = "admin.system_error"

    # ── Scheduler / automation ────────────────────────────────────────────────
    SCHEDULER_ROOM_RELEASE_DUE  = "scheduler.room_release_due"


# ── Notification model ────────────────────────────────────────────────────────

class Notification(Base):
    """
    Persistent in-app notification.

    One row per notification event per recipient.
    External email/SMS delivery is NOT handled here — only in-app.

    Idempotency:
        idempotency_key (unique) prevents duplicate notifications for the
        same event. Format convention:  "<type>:<entity_type>:<entity_id>:<recipient_id>"
        Callers that cannot guarantee uniqueness should omit the key and
        rely on application-level deduplication instead.
    """
    __tablename__ = "notifications"
    __table_args__ = (
        Index("ix_notifications_user_id_created", "user_id", "created_at"),
        Index("ix_notifications_staff_user_id_created", "staff_user_id", "created_at"),
        Index("ix_notifications_is_read", "is_read"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)

    # ── Recipient — exactly one of user_id / staff_user_id is non-null ───────
    recipient_type: Mapped[RecipientType] = mapped_column(
        Enum(RecipientType, name="recipient_type_enum", native_enum=False),
        nullable=False,
        index=True,
    )
    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    staff_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("staff_users.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    # ── Content ───────────────────────────────────────────────────────────────
    notification_type: Mapped[str] = mapped_column(
        String(100), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)

    # ── Related entity (for deep-linking / context) ───────────────────────────
    related_entity_type: Mapped[Optional[str]] = mapped_column(
        String(60), nullable=True
    )
    related_entity_id: Mapped[Optional[str]] = mapped_column(
        String(80), nullable=True
    )

    # ── Read state ────────────────────────────────────────────────────────────
    is_read: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    read_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # ── Idempotency ───────────────────────────────────────────────────────────
    idempotency_key: Mapped[Optional[str]] = mapped_column(
        String(300), nullable=True, unique=True, index=True
    )

    # ── Extra structured data (never secrets) ────────────────────────────────
    meta: Mapped[Optional[dict[str, Any]]] = mapped_column(_jsonb, nullable=True)

    # ── Timestamps ────────────────────────────────────────────────────────────
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        server_default=text("now()"),
        index=True,
    )
    expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # ── Relationships ─────────────────────────────────────────────────────────
    user: Mapped[Optional["User"]] = relationship(  # type: ignore[name-defined]
        "User", foreign_keys=[user_id], lazy="select"
    )
    staff_user: Mapped[Optional["StaffUser"]] = relationship(  # type: ignore[name-defined]
        "StaffUser", foreign_keys=[staff_user_id], lazy="select"
    )

    def __repr__(self) -> str:
        recipient = self.user_id or self.staff_user_id
        return (
            f"<Notification type={self.notification_type!r} "
            f"recipient={recipient} read={self.is_read}>"
        )
