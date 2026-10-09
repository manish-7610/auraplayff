"""
PrizeConfig model — configurable per-entry-fee prize structure.

One row per supported entry fee tier.  Only the ₹50 tier has confirmed
business values (Phase 4 deliverable).  All other tiers are left
unconfigured (is_active=False, prize amounts=0) until the business
finalises them.

A ₹50 row is seeded by the bootstrap service on first startup; all other
tiers are inserted with is_active=False so the system knows they exist but
will refuse to complete a tournament that uses them.

Kill-reward tiers are stored as JSON on this row so the prize_service can
compute progressive kill rewards without a separate table.

Confirmed kill-reward structure (₹50 tier, top-10 only):
  kills 1–3  → ₹5 each
  kills 4–6  → ₹7 each
  kills 7+   → ₹10 each

The JSON is an ordered list of threshold objects:
  [
    {"from_kill": 1, "to_kill": 3, "reward_per_kill": "5.00"},
    {"from_kill": 4, "to_kill": 6, "reward_per_kill": "7.00"},
    {"from_kill": 7, "to_kill": null, "reward_per_kill": "10.00"}
  ]
  to_kill=null means "all kills from from_kill onwards".

This structure is deliberately simple and forward-compatible.  It is
read and interpreted by prize_service.calculate_kill_reward().

Architecture note:
  - prize amounts are Numeric(12,2) — no floats anywhere.
  - 2nd / 3rd place may be 0 for unconfigured tiers; only is_active
    controls whether a tier is usable.
  - created_by_id references the staff user who last configured this tier
    for auditability.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    JSON,
    Numeric,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models._types import utcnow

# Cross-dialect JSONB/JSON
_jsonb = JSONB().with_variant(JSON(), "sqlite")


class PrizeConfig(Base):
    __tablename__ = "prize_configs"
    __table_args__ = (
        UniqueConstraint("entry_fee", name="uq_prize_config_entry_fee"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)

    # The entry fee this config applies to (e.g. 50.00)
    entry_fee: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False, index=True)

    # Whether this tier is active — unconfigured tiers remain False
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )

    # Placement prizes — ₹0 for unconfigured tiers
    first_place_prize: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, default=Decimal("0.00"), server_default=text("0")
    )
    second_place_prize: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, default=Decimal("0.00"), server_default=text("0")
    )
    third_place_prize: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, default=Decimal("0.00"), server_default=text("0")
    )

    # Top-N placements eligible for kill rewards
    kill_reward_top_n: Mapped[int] = mapped_column(
        Numeric(4, 0), nullable=False, default=10, server_default=text("10")
    )

    # Progressive kill-reward tiers — serialised as JSONB.
    # Format: [{"from_kill": 1, "to_kill": 3, "reward_per_kill": "5.00"}, ...]
    # Empty list means no kill rewards for this tier.
    kill_reward_tiers: Mapped[list[dict[str, Any]]] = mapped_column(
        _jsonb, nullable=False, default=list, server_default=text("'[]'")
    )

    # Audit fields
    created_by_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("staff_users.id", ondelete="SET NULL"), nullable=True
    )
    updated_by_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("staff_users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        server_default=text("now()"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        server_default=text("now()"),
    )
    notes: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)

    def __repr__(self) -> str:
        return (
            f"<PrizeConfig fee={self.entry_fee} "
            f"active={self.is_active} "
            f"1st={self.first_place_prize}>"
        )
