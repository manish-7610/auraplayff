"""Permission and StaffUserPermission models — RBAC."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models._types import utcnow


class Permission(Base):
    __tablename__ = "permissions"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    code: Mapped[str] = mapped_column(
        String(80), unique=True, nullable=False, index=True
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    staff_grants: Mapped[list["StaffUserPermission"]] = relationship(
        "StaffUserPermission", back_populates="permission", lazy="select"
    )

    def __repr__(self) -> str:
        return f"<Permission code={self.code!r}>"


class StaffUserPermission(Base):
    __tablename__ = "staff_user_permissions"
    __table_args__ = (
        UniqueConstraint("staff_user_id", "permission_id", name="uq_staff_permission"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    staff_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("staff_users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    permission_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("permissions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    granted_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("staff_users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        server_default=text("now()"),
    )

    staff_user: Mapped["StaffUser"] = relationship(
        "StaffUser",
        back_populates="permissions",
        foreign_keys=[staff_user_id],
    )
    permission: Mapped[Permission] = relationship(
        "Permission", back_populates="staff_grants"
    )
    granted_by: Mapped["StaffUser | None"] = relationship(
        "StaffUser", foreign_keys=[granted_by_id]
    )

    def __repr__(self) -> str:
        return f"<StaffUserPermission staff={self.staff_user_id} perm={self.permission_id}>"
