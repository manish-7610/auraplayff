"""
ReferralRelationship model.

Tracks the full lifecycle of a referral:
  - Who referred whom (immutable once created)
  - Which referral code was used
  - When the referral was established
  - Whether the first-tournament condition has been satisfied
  - Whether the referrer's ₹20 bonus was credited
  - Whether the referred user's ₹20 bonus was credited

Business rules enforced here (plus DB constraints):
  - One ReferralRelationship per referred_user_id (UNIQUE)
  - referrer_id != referred_user_id (CHECK — belt-and-suspenders over app logic)
  - Rewards are credited exactly once (Boolean flags + idempotency in service layer)
  - Relationship is immutable once inserted (application-level, no cascade deletes)
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models._types import utcnow


class ReferralRelationship(Base):
    __tablename__ = "referral_relationships"
    __table_args__ = (
        UniqueConstraint("referred_user_id", name="uq_referral_referred_user"),
        CheckConstraint(
            "referrer_id != referred_user_id",
            name="ck_referral_no_self_referral",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)

    # Who did the referring (the code owner)
    referrer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    # Who was referred (the new registrant — unique so only one referral per user)
    referred_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    # The exact code used at registration (immutable record)
    referral_code_used: Mapped[str] = mapped_column(String(10), nullable=False)

    # When the referral relationship was established (at registration)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        server_default=text("now()"),
        index=True,
    )

    # ── Condition tracking ────────────────────────────────────────────────────

    # Set to True + timestamp when referred user completes their first tournament
    first_tournament_completed: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
    )
    first_tournament_completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # ── Reward tracking ───────────────────────────────────────────────────────

    # Referrer (User A) reward — ₹20 referral_balance credit
    referrer_rewarded: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
    )
    referrer_rewarded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Referred user (User B) reward — ₹20 referral_balance credit
    referred_user_rewarded: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
    )
    referred_user_rewarded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # ── Relationships ─────────────────────────────────────────────────────────

    referrer: Mapped["User"] = relationship(  # type: ignore[name-defined]  # noqa: F821
        "User",
        foreign_keys=[referrer_id],
        back_populates="referrals_made",
    )
    referred_user: Mapped["User"] = relationship(  # type: ignore[name-defined]  # noqa: F821
        "User",
        foreign_keys=[referred_user_id],
        back_populates="referral_received",
    )

    def __repr__(self) -> str:
        return (
            f"<ReferralRelationship referrer={self.referrer_id} "
            f"referred={self.referred_user_id} "
            f"rewarded_referrer={self.referrer_rewarded} "
            f"rewarded_referred={self.referred_user_rewarded}>"
        )
