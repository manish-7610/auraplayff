"""
ProviderEvent — immutable record of every inbound payment provider webhook.

Purpose
-------
Provides strong idempotency for webhook delivery. Razorpay (and any future
provider) can deliver the same event multiple times. Before processing any
webhook, the service checks whether event_id already exists in this table.
If it does, it returns 200 OK immediately without re-processing.

Behaviour
---------
  INSERT on first delivery  →  process the event
  UNIQUE violation on retry →  already processed, skip safely

Columns
-------
provider          : "razorpay" | future providers
event_id          : Provider-assigned unique event ID (unique per provider)
event_type        : e.g. "payment.captured", "payment.failed"
received_at       : When we first received this event
processing_status : "received" | "processed" | "ignored" | "failed"
deposit_id        : FK to our internal Deposit record (nullable)
raw_event         : Safe subset of event payload (NO secrets)
error_detail      : Brief error note if processing failed
"""
from __future__ import annotations

import enum as pyenum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    DateTime, Enum, ForeignKey, JSON, String, Text,
    UniqueConstraint, text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models._types import utcnow

_jsonb = JSONB().with_variant(JSON(), "sqlite")


class ProviderEventStatus(str, pyenum.Enum):
    received = "received"     # Stored, processing in progress
    processed = "processed"   # Successfully processed, wallet updated
    ignored = "ignored"       # Valid event but no action needed (e.g. payment.authorized)
    failed = "failed"         # Processing failed (see error_detail)


class ProviderEvent(Base):
    """
    Immutable webhook event record.
    Never UPDATE or DELETE — only INSERT and status UPDATE on same transaction.
    """
    __tablename__ = "provider_events"
    __table_args__ = (
        # One record per provider+event_id pair — primary idempotency guard
        UniqueConstraint(
            "provider", "event_id",
            name="uq_provider_event_provider_event_id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)

    provider: Mapped[str] = mapped_column(
        String(40), nullable=False, index=True
    )
    event_id: Mapped[str] = mapped_column(
        String(200), nullable=False, index=True
    )
    event_type: Mapped[str] = mapped_column(
        String(100), nullable=False, index=True
    )
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        server_default=text("now()"),
        index=True,
    )
    processing_status: Mapped[ProviderEventStatus] = mapped_column(
        Enum(ProviderEventStatus, name="provider_event_status_enum", native_enum=False),
        nullable=False,
        default=ProviderEventStatus.received,
    )

    # Link to the internal deposit this event relates to (nullable — may not
    # be known until after signature check)
    deposit_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("deposits.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # Safe subset of the raw event — strip any payment secrets before storing
    raw_event: Mapped[dict[str, Any] | None] = mapped_column(
        _jsonb, nullable=True
    )

    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ── Relationships ─────────────────────────────────────────────────────────
    deposit: Mapped["Deposit | None"] = relationship(  # type: ignore[name-defined]
        "Deposit", foreign_keys=[deposit_id]
    )

    def __repr__(self) -> str:
        return (
            f"<ProviderEvent provider={self.provider!r} "
            f"event_id={self.event_id!r} type={self.event_type!r} "
            f"status={self.processing_status}>"
        )
