"""
StaffUser model — Super Admins and Agents.
Completely separate from the player User model.
"""
from __future__ import annotations

import uuid
from datetime import datetime

import enum as pyenum

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models._types import utcnow


class StaffRole(str, pyenum.Enum):
    super_admin = "super_admin"
    agent = "agent"


class StaffUser(Base):
    __tablename__ = "staff_users"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4,
    )

    agent_id: Mapped[str] = mapped_column(
        String(20), unique=True, nullable=False, index=True
    )
    display_name: Mapped[str] = mapped_column(String(100), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[StaffRole] = mapped_column(
        Enum(StaffRole, name="staff_role_enum", native_enum=False),
        nullable=False,
    )

    token_version: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("staff_users.id", ondelete="SET NULL"),
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        server_default=text("now()"),
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_by: Mapped["StaffUser | None"] = relationship(
        "StaffUser", remote_side="StaffUser.id", foreign_keys=[created_by_id]
    )
    permissions: Mapped[list["StaffUserPermission"]] = relationship(
        "StaffUserPermission",
        back_populates="staff_user",
        foreign_keys="StaffUserPermission.staff_user_id",
        lazy="select",
    )
    refresh_tokens: Mapped[list["RefreshToken"]] = relationship(
        "RefreshToken",
        back_populates="staff_user",
        foreign_keys="RefreshToken.staff_user_id",
        lazy="select",
    )

    @property
    def is_super_admin(self) -> bool:
        return self.role == StaffRole.super_admin

    def __repr__(self) -> str:
        return f"<StaffUser agent_id={self.agent_id!r} role={self.role}>"
