"""
AuditLog model — immutable record of every sensitive action.
Never UPDATE or DELETE audit log records.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import enum as pyenum

from sqlalchemy import DateTime, Enum, JSON, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models._types import utcnow

# Cross-dialect: JSONB on PostgreSQL, JSON on SQLite (tests)
_jsonb = JSONB().with_variant(JSON(), "sqlite")


class ActorType(str, pyenum.Enum):
    user = "user"
    staff = "staff"
    system = "system"


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)

    actor_type: Mapped[ActorType] = mapped_column(
        Enum(ActorType, name="actor_type_enum", native_enum=False),
        nullable=False,
    )
    actor_id: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    entity_type: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    entity_id: Mapped[str] = mapped_column(String(80), nullable=False, index=True)

    old_value: Mapped[dict[str, Any] | None] = mapped_column(_jsonb, nullable=True)
    new_value: Mapped[dict[str, Any] | None] = mapped_column(_jsonb, nullable=True)

    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        server_default=text("now()"),
        index=True,
    )

    def __repr__(self) -> str:
        return f"<AuditLog action={self.action!r} entity={self.entity_type}/{self.entity_id}>"
