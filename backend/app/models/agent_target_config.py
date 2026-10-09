"""
AgentTargetConfig — configurable daily tournament targets per entry-fee tier.

Confirmed targets (from Phase 6 spec):
  ₹10  → 8/day,   extra bonus ₹25/tournament
  ₹20  → 7/day,   extra bonus ₹40/tournament
  ₹30  → 6/day,   extra bonus ₹60/tournament
  ₹40  → 5/day,   extra bonus ₹80/tournament
  ₹50  → 4/day,   extra bonus ₹100/tournament
  ₹100 → 3/day,   extra bonus ₹200/tournament

Base monthly salary: ₹10,000 (stored in a separate config row)

All values are stored in this table so Super Admin can change them at runtime
without a code deploy. The salary_service reads from here.

Design
------
One row per entry-fee tier. The table is small (6 active rows).
A separate row with entry_fee=NULL (or a special key) stores base_salary.
We use a separate model row with key="base_monthly_salary" and entry_fee=NULL
for the salary value to keep the schema clean.

Rules
-----
- Do NOT scatter default constants through business logic.
- salary_service and performance_service always read from this table.
- Defaults are seeded on bootstrap; Super Admin can update via settings API.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    Boolean, CheckConstraint, DateTime, Numeric, String,
    Text, UniqueConstraint, text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models._types import utcnow

# ── Seed data constants (defaults only — runtime values are in DB) ─────────────

DEFAULT_BASE_MONTHLY_SALARY = Decimal("10000.00")

# (entry_fee_inr, daily_required, extra_bonus_per_tournament)
DEFAULT_TIER_CONFIGS: list[tuple[str, int, str]] = [
    ("10.00",  8,  "25.00"),
    ("20.00",  7,  "40.00"),
    ("30.00",  6,  "60.00"),
    ("40.00",  5,  "80.00"),
    ("50.00",  4, "100.00"),
    ("100.00", 3, "200.00"),
]

# Default deduction formula: missing_count × extra_bonus_for_that_tier
# This is the canonical deduction formula key stored per tier.
DEDUCTION_FORMULA_SHORTFALL_X_BONUS = "shortfall_x_bonus"


class AgentTargetConfig(Base):
    """
    One row per active entry-fee tier.
    Stores the daily required count, extra bonus per extra tournament,
    and deduction formula for shortfall.

    Also stores base_monthly_salary in a special row where entry_fee IS NULL
    and config_key = 'base_monthly_salary'.
    """
    __tablename__ = "agent_target_configs"
    __table_args__ = (
        # Only one config row per entry fee tier
        UniqueConstraint("entry_fee", name="uq_agent_target_config_entry_fee"),
        CheckConstraint(
            "entry_fee IS NULL OR entry_fee > 0",
            name="ck_agent_target_config_entry_fee_positive",
        ),
        CheckConstraint(
            "daily_required IS NULL OR daily_required > 0",
            name="ck_agent_target_config_daily_required_positive",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)

    # Entry fee in INR. NULL for special config rows (e.g. base salary).
    entry_fee: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(10, 2), nullable=True, index=True
    )
    # Human label e.g. "₹50 tier" — informational only
    label: Mapped[str] = mapped_column(String(60), nullable=False, default="")

    # Daily minimum tournaments an agent must complete for this tier
    daily_required: Mapped[Optional[int]] = mapped_column(nullable=True)

    # Bonus per extra tournament beyond daily_required (INR)
    extra_bonus_per_tournament: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(10, 2), nullable=True
    )

    # Deduction per missing tournament (defaults to extra_bonus if NULL)
    deduction_per_missing: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(10, 2), nullable=True,
        comment="If NULL, deduction defaults to extra_bonus_per_tournament"
    )

    # Base monthly salary (stored in the row where entry_fee IS NULL)
    base_monthly_salary: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(12, 2), nullable=True
    )

    # Whether this tier config is active
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )

    # Audit trail
    updated_by: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        server_default=text("now()"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        server_default=text("now()"),
    )

    def __repr__(self) -> str:
        if self.entry_fee is None:
            return f"<AgentTargetConfig base_salary={self.base_monthly_salary}>"
        return (
            f"<AgentTargetConfig entry_fee={self.entry_fee} "
            f"daily_required={self.daily_required} "
            f"extra_bonus={self.extra_bonus_per_tournament}>"
        )
