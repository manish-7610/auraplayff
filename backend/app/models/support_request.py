"""
SupportRequest model — player help & support tickets.

A player can submit a support request with:
  - An optional tournament reference (or "General / Other Issue")
  - A problem description / message
  - At least one contact method: WhatsApp number OR Instagram ID

Admin can update the status and add a response.
Players can only see their own requests; they cannot see other players' requests.

Status lifecycle:
  pending     — submitted, not yet reviewed by support
  in_progress — actively being handled by support staff
  resolved    — issue resolved / closed

Constraints:
  - message is required (non-empty).
  - At least one of whatsapp_number or instagram_id must be provided
    (enforced at the application layer in schemas / service).
  - tournament_id is nullable; NULL means "General / Other Issue".
  - resolved_at is set when status transitions to 'resolved'.
  - resolved_by references the staff_user who resolved the request.
"""
from __future__ import annotations

import enum as pyenum
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models._types import utcnow


class SupportStatus(str, pyenum.Enum):
    pending     = "pending"
    in_progress = "in_progress"
    resolved    = "resolved"


class SupportRequest(Base):
    __tablename__ = "support_requests"
    __table_args__ = (
        CheckConstraint(
            "length(trim(message)) > 0",
            name="ck_support_request_message_nonempty",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)

    # ── Player ────────────────────────────────────────────────────────────────
    # Nullable for recovery requests (submitted without authentication).
    # For normal support tickets this is always set to the authenticated player.
    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )

    # ── Tournament reference (nullable = General / Other Issue) ───────────────
    tournament_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("tournaments.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # ── Content ───────────────────────────────────────────────────────────────
    message: Mapped[str] = mapped_column(Text, nullable=False)

    # ── Contact (at least one required — enforced at application layer) ───────
    whatsapp_number: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    instagram_id: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)

    # ── Recovery flag ─────────────────────────────────────────────────────────
    # True when submitted via the PUBLIC /api/recovery/request endpoint.
    # The requester_username / requester_ff_uid are self-reported and NOT
    # verified — they exist only to help admin manually identify the account.
    is_recovery: Mapped[bool] = mapped_column(
        default=False, server_default=text("false"), nullable=False, index=True
    )
    requester_username: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    requester_ff_uid: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)

    # ── Status ────────────────────────────────────────────────────────────────
    status: Mapped[SupportStatus] = mapped_column(
        Enum(SupportStatus, name="support_status_enum", native_enum=False),
        nullable=False,
        default=SupportStatus.pending,
        server_default=text("'pending'"),
        index=True,
    )

    # ── Admin response ────────────────────────────────────────────────────────
    admin_response: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # ── Resolution tracking ───────────────────────────────────────────────────
    resolved_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    resolved_by_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("staff_users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # ── Timestamps ────────────────────────────────────────────────────────────
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        server_default=text("now()"),
        index=True,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        server_default=text("now()"),
        onupdate=utcnow,
    )

    # ── Relationships ─────────────────────────────────────────────────────────
    user: Mapped[Optional["User"]] = relationship(  # type: ignore[name-defined]
        "User", lazy="select"
    )
    tournament: Mapped[Optional["Tournament"]] = relationship(  # type: ignore[name-defined]
        "Tournament", lazy="select"
    )
    resolved_by: Mapped[Optional["StaffUser"]] = relationship(  # type: ignore[name-defined]
        "StaffUser", foreign_keys=[resolved_by_id], lazy="select"
    )

    def __repr__(self) -> str:
        return (
            f"<SupportRequest id={self.id} "
            f"user={self.user_id} "
            f"status={self.status.value}>"
        )
