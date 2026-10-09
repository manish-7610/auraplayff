"""
Agent performance tracking models — Phase 6.

Models
------
AgentDailyPerformance
    One row per (agent, date, entry_fee_tier).
    Derived from actual tournament records; never manually entered by agents.
    Unique constraint prevents duplicate daily records.

    Tracks:
    - required count for the tier on that day
    - completed count (tournaments that reached COMPLETED status)
    - shortfall / extra
    - extra bonus earned
    - grace/warning/deduction flags
    - daily status

AgentGrace
    An explicit grace grant for a missed target day.
    Issued by Super Admin only. Immutable after creation.
    Prevents a day's shortfall from counting as a deduction.

AgentWarning
    Second target miss creates a warning record.
    Stored explicitly — not derived dynamically.
    Visible to agent (own only) and Super Admin (all).

AgentSalaryRecord
    One row per (agent, year, month).
    Aggregates the month's performance: bonuses, deductions, adjustments.
    Computed lazily by salary_service; updated as new data arrives.
    status: "draft" | "confirmed" | "paid"

Business rules
--------------
- AgentDailyPerformance is the source of truth for tournament counts.
- Grace beats deduction: if a day has is_grace=True, deduction is 0.
- Warning is informational only — does NOT automatically deduct salary.
- First miss: grace opportunity (status = "grace_pending")
- Second miss: warning issued (status = "warned")
- Third+ miss: deduction applied (status = "deducted")
- All of the above must be explicitly actioned by Super Admin.
"""
from __future__ import annotations

import enum as pyenum
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import (
    Boolean, CheckConstraint, Date, DateTime, Enum, ForeignKey,
    Integer, JSON, Numeric, String, Text, UniqueConstraint, text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models._types import utcnow

_jsonb = JSONB().with_variant(JSON(), "sqlite")


# ── Status enums ──────────────────────────────────────────────────────────────

class DailyPerfStatus(str, pyenum.Enum):
    """Daily performance resolution status."""
    pending   = "pending"           # Day not yet ended or unreviewed
    met       = "met"               # Target met — no action needed
    grace     = "grace"             # Missed, grace granted
    warned    = "warned"            # Missed, warning issued
    deducted  = "deducted"          # Missed, salary deduction applied
    excused   = "excused"           # Admin explicitly excused this day
    exceeded  = "exceeded"          # Target exceeded (extra bonus earned)


class SalaryRecordStatus(str, pyenum.Enum):
    """Monthly salary record lifecycle."""
    draft     = "draft"             # Being accumulated during the month
    confirmed = "confirmed"         # Month ended, reviewed by Super Admin
    paid      = "paid"              # Salary paid (record kept for history)


# ── AgentDailyPerformance ─────────────────────────────────────────────────────

class AgentDailyPerformance(Base):
    """
    One row per (agent, date, entry_fee_tier).

    'completed_count' is derived from actual Tournament records where:
      - created_by_id == agent.id
      - entry_fee == this tier's entry_fee
      - status == 'completed'
      - completed_at::date == this date

    'required_count' is copied from AgentTargetConfig at record creation time
    so historical records are immutable even if config changes later.

    Performance snapshot is updated by performance_service.refresh_daily()
    which can be called on demand or by a future scheduler.
    """
    __tablename__ = "agent_daily_performances"
    __table_args__ = (
        UniqueConstraint(
            "agent_id", "perf_date", "entry_fee",
            name="uq_agent_daily_perf_agent_date_tier",
        ),
        CheckConstraint("completed_count >= 0", name="ck_adp_completed_count_nn"),
        CheckConstraint("required_count >= 0",  name="ck_adp_required_count_nn"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)

    agent_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("staff_users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    perf_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    entry_fee: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)

    # Required count snapshotted from config at record creation time
    required_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Actual completed tournaments for this tier on this date
    completed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Derived (kept denormalised for fast dashboard queries)
    shortfall: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    extra_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    extra_bonus: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, default=Decimal("0.00"), server_default=text("0")
    )
    deduction_amount: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, default=Decimal("0.00"), server_default=text("0")
    )

    # Status flags
    status: Mapped[DailyPerfStatus] = mapped_column(
        Enum(DailyPerfStatus, name="daily_perf_status_enum", native_enum=False),
        nullable=False,
        default=DailyPerfStatus.pending,
        index=True,
    )
    is_grace: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    is_warned: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    is_excused: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )

    # Optional note from Super Admin
    admin_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    reviewed_by_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("staff_users.id", ondelete="SET NULL"),
        nullable=True,
    )

    meta: Mapped[Optional[dict[str, Any]]] = mapped_column(_jsonb, nullable=True)

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

    # Relationships
    agent: Mapped["StaffUser"] = relationship(  # type: ignore[name-defined]
        "StaffUser", foreign_keys=[agent_id]
    )
    reviewed_by: Mapped[Optional["StaffUser"]] = relationship(  # type: ignore[name-defined]
        "StaffUser", foreign_keys=[reviewed_by_id]
    )

    def __repr__(self) -> str:
        return (
            f"<AgentDailyPerformance agent={self.agent_id} "
            f"date={self.perf_date} tier={self.entry_fee} "
            f"completed={self.completed_count}/{self.required_count} "
            f"status={self.status}>"
        )


# ── AgentGrace ────────────────────────────────────────────────────────────────

class AgentGrace(Base):
    """
    Explicit grace grant for a missed target day.
    Issued by Super Admin only.
    Links to the AgentDailyPerformance it excuses.

    A grace record:
    - Sets agentdailyperformance.is_grace = True
    - Sets agentdailyperformance.status = "grace"
    - Zeroes out deduction_amount for that record
    - Is IMMUTABLE after creation

    One grace per daily performance record.
    """
    __tablename__ = "agent_graces"
    __table_args__ = (
        UniqueConstraint(
            "daily_performance_id",
            name="uq_agent_grace_daily_performance_id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)

    agent_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("staff_users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    daily_performance_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agent_daily_performances.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    grace_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    granted_by_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("staff_users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        server_default=text("now()"),
    )

    # Relationships
    agent: Mapped["StaffUser"] = relationship(  # type: ignore[name-defined]
        "StaffUser", foreign_keys=[agent_id]
    )
    daily_performance: Mapped["AgentDailyPerformance"] = relationship(
        "AgentDailyPerformance", foreign_keys=[daily_performance_id]
    )
    granted_by: Mapped["StaffUser"] = relationship(  # type: ignore[name-defined]
        "StaffUser", foreign_keys=[granted_by_id]
    )

    def __repr__(self) -> str:
        return (
            f"<AgentGrace agent={self.agent_id} "
            f"date={self.grace_date} granted_by={self.granted_by_id}>"
        )


# ── AgentWarning ──────────────────────────────────────────────────────────────

class AgentWarning(Base):
    """
    Second target miss (and beyond) creates an explicit warning record.
    Warnings are informational only — they do NOT automatically deduct salary.
    Salary deduction is handled by AgentDailyPerformance.deduction_amount.

    warning_number tracks cumulative warnings for this agent.
    """
    __tablename__ = "agent_warnings"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)

    agent_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("staff_users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    daily_performance_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("agent_daily_performances.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    warning_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    warning_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    issued_by_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("staff_users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        server_default=text("now()"),
    )

    # Relationships
    agent: Mapped["StaffUser"] = relationship(  # type: ignore[name-defined]
        "StaffUser", foreign_keys=[agent_id]
    )
    daily_performance: Mapped[Optional["AgentDailyPerformance"]] = relationship(
        "AgentDailyPerformance", foreign_keys=[daily_performance_id]
    )
    issued_by: Mapped["StaffUser"] = relationship(  # type: ignore[name-defined]
        "StaffUser", foreign_keys=[issued_by_id]
    )

    def __repr__(self) -> str:
        return (
            f"<AgentWarning agent={self.agent_id} "
            f"date={self.warning_date} warning_number={self.warning_number}>"
        )


# ── AgentSalaryRecord ─────────────────────────────────────────────────────────

class AgentSalaryRecord(Base):
    """
    Monthly salary record for an agent.

    One row per (agent, year, month).
    Accumulated by salary_service throughout the month.
    Confirmed by Super Admin at month end.

    This is an INTERNAL payroll record — it does NOT transfer money
    through the wallet system. Agent salary is paid externally.

    Columns
    -------
    base_salary          : Copy of base_monthly_salary at record creation time
    total_extra_bonus    : Sum of all daily extra_bonus values for the month
    total_deductions     : Sum of all daily deduction_amount values
    manual_adjustment    : Admin-entered manual +/- adjustment with reason
    final_salary         : base_salary + total_extra_bonus - total_deductions + manual_adjustment
    status               : draft | confirmed | paid
    notes                : Admin notes
    confirmed_by_id      : Staff who confirmed the record
    confirmed_at         : When confirmed
    """
    __tablename__ = "agent_salary_records"
    __table_args__ = (
        UniqueConstraint(
            "agent_id", "year", "month",
            name="uq_agent_salary_record_agent_year_month",
        ),
        CheckConstraint("year >= 2024", name="ck_agent_salary_year_sane"),
        CheckConstraint("month >= 1 AND month <= 12", name="ck_agent_salary_month_range"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)

    agent_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("staff_users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    month: Mapped[int] = mapped_column(Integer, nullable=False)   # 1–12

    # Financial fields (all INR Decimal(12,2))
    base_salary: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, default=Decimal("0.00"), server_default=text("0")
    )
    total_extra_bonus: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, default=Decimal("0.00"), server_default=text("0")
    )
    total_deductions: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, default=Decimal("0.00"), server_default=text("0")
    )
    manual_adjustment: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, default=Decimal("0.00"), server_default=text("0"),
        comment="Positive = bonus, negative = additional deduction"
    )
    final_salary: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, default=Decimal("0.00"), server_default=text("0"),
        comment="Computed: base + bonus - deductions + manual_adjustment"
    )
    manual_adjustment_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    status: Mapped[SalaryRecordStatus] = mapped_column(
        Enum(SalaryRecordStatus, name="salary_record_status_enum", native_enum=False),
        nullable=False,
        default=SalaryRecordStatus.draft,
        index=True,
    )
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    confirmed_by_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("staff_users.id", ondelete="SET NULL"),
        nullable=True,
    )
    confirmed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
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

    # Relationships
    agent: Mapped["StaffUser"] = relationship(  # type: ignore[name-defined]
        "StaffUser", foreign_keys=[agent_id]
    )
    confirmed_by: Mapped[Optional["StaffUser"]] = relationship(  # type: ignore[name-defined]
        "StaffUser", foreign_keys=[confirmed_by_id]
    )

    def __repr__(self) -> str:
        return (
            f"<AgentSalaryRecord agent={self.agent_id} "
            f"{self.year}-{self.month:02d} final={self.final_salary} "
            f"status={self.status}>"
        )
