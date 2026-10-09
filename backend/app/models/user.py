"""
User model — registered players.
No email or phone collected (Admin-assisted password reset only).
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models._types import utcnow


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)

    username: Mapped[str] = mapped_column(
        String(30), unique=True, nullable=False, index=True
    )
    ff_uid: Mapped[str] = mapped_column(
        String(20), unique=True, nullable=False, index=True
    )
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)

    referral_code: Mapped[str] = mapped_column(
        String(10), unique=True, nullable=False, index=True
    )
    referred_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    referral_rewarded: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    token_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    # Free Fire in-game display name — editable by the player via PATCH /api/users/me.
    # NULL means the player has not set it yet; callers fall back to `username` in that case.
    # The login identifier (`username`) is never changed — it is a separate, immutable field.
    ff_name: Mapped[str | None] = mapped_column(String(30), nullable=True, index=True)
    avatar_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    is_banned: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        server_default=text("now()"),
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )

    referred_by: Mapped["User | None"] = relationship(
        "User", remote_side="User.id", foreign_keys=[referred_by_id]
    )

    # Referral relationships — populated by ReferralRelationship model
    referrals_made: Mapped[list["ReferralRelationship"]] = relationship(  # type: ignore[name-defined]  # noqa: F821
        "ReferralRelationship",
        foreign_keys="ReferralRelationship.referrer_id",
        back_populates="referrer",
        lazy="select",
    )
    referral_received: Mapped["ReferralRelationship | None"] = relationship(  # type: ignore[name-defined]  # noqa: F821
        "ReferralRelationship",
        foreign_keys="ReferralRelationship.referred_user_id",
        back_populates="referred_user",
        uselist=False,
        lazy="select",
    )

    wallet: Mapped["Wallet"] = relationship(
        "Wallet", back_populates="user", uselist=False, lazy="select"
    )
    player_stats: Mapped["PlayerStats"] = relationship(
        "PlayerStats", back_populates="user", uselist=False, lazy="select"
    )
    refresh_tokens: Mapped[list["RefreshToken"]] = relationship(
        "RefreshToken",
        back_populates="user",
        foreign_keys="RefreshToken.user_id",
        lazy="select",
    )
    password_reset_tokens: Mapped[list["PasswordResetToken"]] = relationship(
        "PasswordResetToken", back_populates="user", lazy="select"
    )

    def __repr__(self) -> str:
        return f"<User id={self.id} username={self.username!r}>"
