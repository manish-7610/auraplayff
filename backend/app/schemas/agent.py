"""
Pydantic v2 schemas for Agent management, performance, salary, grace and warning.

Player-facing: none — agents are staff, not players.

Agent self-service schemas:
  AgentMeResponse             — GET /api/agent/me
  AgentTodayPerformanceResponse  — GET /api/agent/performance/today
  AgentMonthPerformanceResponse  — GET /api/agent/performance/month
  AgentTournamentSummaryResponse — GET /api/agent/tournaments
  AgentSalaryResponse            — GET /api/agent/salary

Admin schemas (request):
  CreateAgentRequest          — POST /api/admin/agents
  UpdateAgentRequest          — PATCH /api/admin/agents/{id}
  ResetAgentPasswordRequest   — POST /api/admin/agents/{id}/reset-password
  GrantGraceRequest           — POST /api/admin/agents/{id}/grace
  IssueWarningRequest         — POST /api/admin/agents/{id}/warning
  ExcuseDayRequest            — POST /api/admin/agents/{id}/excuse-day
  ManualSalaryAdjustRequest   — POST /api/admin/agents/{id}/salary-adjust
  ConfirmSalaryRequest        — POST /api/admin/agents/{id}/salary/confirm
  UpdateTargetConfigRequest   — PUT /api/admin/targets/{entry_fee}

Admin schemas (response):
  AdminAgentResponse          — single agent detail
  AdminAgentListResponse      — paginated list
  AdminDailyPerfResponse      — single daily performance record
  AdminMonthPerfResponse      — monthly rollup
  AdminGraceResponse          — grace record
  AdminWarningResponse        — warning record
  AdminSalaryResponse         — salary record
  TargetConfigResponse        — single tier config
  TargetConfigListResponse    — all tier configs

Security:
  - password_hash never appears in any response schema.
  - Amounts as strings to preserve Decimal precision.
  - Agents can only see their own data.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, Field, field_validator


# ── Shared validators ─────────────────────────────────────────────────────────

def _validate_password(v: str) -> str:
    v = v.strip()
    if len(v) < 8:
        raise ValueError("Password must be at least 8 characters.")
    if len(v) > 72:
        raise ValueError("Password must be at most 72 characters (bcrypt limit).")
    return v


# ── Admin — Create / Update Agent ─────────────────────────────────────────────

class CreateAgentRequest(BaseModel):
    """Super Admin creates a new agent account."""
    display_name: str = Field(..., min_length=2, max_length=100)
    password: str = Field(..., min_length=8, max_length=72)

    @field_validator("password")
    @classmethod
    def validate_password(cls, v: str) -> str:
        return _validate_password(v)

    @field_validator("display_name")
    @classmethod
    def strip_display_name(cls, v: str) -> str:
        return v.strip()


class UpdateAgentRequest(BaseModel):
    """Super Admin updates agent display name or active status."""
    display_name: Optional[str] = Field(None, min_length=2, max_length=100)
    is_active: Optional[bool] = None


class ResetAgentPasswordRequest(BaseModel):
    """Super Admin resets an agent's password."""
    new_password: str = Field(..., min_length=8, max_length=72)

    @field_validator("new_password")
    @classmethod
    def validate_password(cls, v: str) -> str:
        return _validate_password(v)


# ── Agent self-service responses ──────────────────────────────────────────────

class AgentMeResponse(BaseModel):
    """Agent's own profile — returned by GET /api/agent/me."""
    id: uuid.UUID
    agent_id: str
    display_name: str
    role: str
    is_active: bool
    created_at: datetime

    @classmethod
    def from_staff_user(cls, s: "StaffUser") -> "AgentMeResponse":  # type: ignore[name-defined]  # noqa
        return cls(
            id=s.id,
            agent_id=s.agent_id,
            display_name=s.display_name,
            role=s.role.value,
            is_active=s.is_active,
            created_at=s.created_at,
        )


class TierPerformanceItem(BaseModel):
    """Performance for a single entry-fee tier on one day."""
    entry_fee: str
    required_count: int
    completed_count: int
    shortfall: int
    extra_count: int
    extra_bonus: str
    deduction_amount: str
    status: str
    is_grace: bool
    is_warned: bool
    is_excused: bool

    @classmethod
    def from_perf(cls, p: "AgentDailyPerformance") -> "TierPerformanceItem":  # type: ignore[name-defined]  # noqa
        return cls(
            entry_fee=str(p.entry_fee),
            required_count=p.required_count,
            completed_count=p.completed_count,
            shortfall=p.shortfall,
            extra_count=p.extra_count,
            extra_bonus=str(p.extra_bonus),
            deduction_amount=str(p.deduction_amount),
            status=p.status.value,
            is_grace=p.is_grace,
            is_warned=p.is_warned,
            is_excused=p.is_excused,
        )


class AgentTodayPerformanceResponse(BaseModel):
    """Today's performance summary — GET /api/agent/performance/today."""
    date: date
    tiers: list[TierPerformanceItem]
    total_completed: int
    total_extra_bonus: str
    total_deductions: str


class AgentMonthPerformanceResponse(BaseModel):
    """Monthly performance overview — GET /api/agent/performance/month."""
    year: int
    month: int
    base_salary: str
    total_extra_bonus: str
    total_deductions: str
    manual_adjustment: str
    estimated_final_salary: str
    days_met: int
    days_missed: int
    days_graced: int
    days_warned: int
    days_excused: int
    days_exceeded: int
    salary_status: str


class AgentTournamentSummaryItem(BaseModel):
    """One tournament in the agent's tournament list."""
    tournament_id: uuid.UUID
    tournament_code: str
    title: str
    entry_fee: str
    status: str
    player_count: int
    scheduled_at: datetime
    completed_at: Optional[datetime] = None
    cancelled_at: Optional[datetime] = None


class AgentTournamentSummaryResponse(BaseModel):
    items: list[AgentTournamentSummaryItem]
    total: int
    limit: int
    offset: int


class AgentSalaryResponse(BaseModel):
    """Agent's own salary record — GET /api/agent/salary."""
    year: int
    month: int
    base_salary: str
    total_extra_bonus: str
    total_deductions: str
    manual_adjustment: str
    final_salary: str
    status: str

    @classmethod
    def from_record(cls, r: "AgentSalaryRecord") -> "AgentSalaryResponse":  # type: ignore[name-defined]  # noqa
        return cls(
            year=r.year,
            month=r.month,
            base_salary=str(r.base_salary),
            total_extra_bonus=str(r.total_extra_bonus),
            total_deductions=str(r.total_deductions),
            manual_adjustment=str(r.manual_adjustment),
            final_salary=str(r.final_salary),
            status=r.status.value,
        )


# ── Admin — Agent responses ────────────────────────────────────────────────────

class AdminAgentResponse(BaseModel):
    """Full agent record for admin panel."""
    id: uuid.UUID
    agent_id: str
    display_name: str
    role: str
    is_active: bool
    created_by_id: Optional[uuid.UUID] = None
    created_at: datetime
    last_seen_at: Optional[datetime] = None

    @classmethod
    def from_staff_user(cls, s: "StaffUser") -> "AdminAgentResponse":  # type: ignore[name-defined]  # noqa
        return cls(
            id=s.id,
            agent_id=s.agent_id,
            display_name=s.display_name,
            role=s.role.value,
            is_active=s.is_active,
            created_by_id=s.created_by_id,
            created_at=s.created_at,
            last_seen_at=s.last_seen_at,
        )


class AdminAgentListResponse(BaseModel):
    items: list[AdminAgentResponse]
    total: int
    limit: int
    offset: int


# ── Admin — Performance responses ─────────────────────────────────────────────

class AdminDailyPerfResponse(BaseModel):
    """Full daily performance record for admin panel."""
    id: uuid.UUID
    agent_id: uuid.UUID
    perf_date: date
    entry_fee: str
    required_count: int
    completed_count: int
    shortfall: int
    extra_count: int
    extra_bonus: str
    deduction_amount: str
    status: str
    is_grace: bool
    is_warned: bool
    is_excused: bool
    admin_note: Optional[str] = None
    reviewed_by_id: Optional[uuid.UUID] = None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_perf(cls, p: "AgentDailyPerformance") -> "AdminDailyPerfResponse":  # type: ignore[name-defined]  # noqa
        return cls(
            id=p.id,
            agent_id=p.agent_id,
            perf_date=p.perf_date,
            entry_fee=str(p.entry_fee),
            required_count=p.required_count,
            completed_count=p.completed_count,
            shortfall=p.shortfall,
            extra_count=p.extra_count,
            extra_bonus=str(p.extra_bonus),
            deduction_amount=str(p.deduction_amount),
            status=p.status.value,
            is_grace=p.is_grace,
            is_warned=p.is_warned,
            is_excused=p.is_excused,
            admin_note=p.admin_note,
            reviewed_by_id=p.reviewed_by_id,
            created_at=p.created_at,
            updated_at=p.updated_at,
        )


class AdminDailyPerfListResponse(BaseModel):
    items: list[AdminDailyPerfResponse]
    total: int
    limit: int
    offset: int


class AdminMonthPerfResponse(BaseModel):
    """Monthly performance rollup per agent."""
    agent_id: uuid.UUID
    year: int
    month: int
    base_salary: str
    total_extra_bonus: str
    total_deductions: str
    manual_adjustment: str
    estimated_final_salary: str
    days_met: int
    days_missed: int
    days_graced: int
    days_warned: int
    days_excused: int
    days_exceeded: int
    salary_status: str
    tier_breakdown: list[dict]  # [{entry_fee, completed, required, bonus, deduction}]


# ── Admin — Grace / Warning responses ─────────────────────────────────────────

class GrantGraceRequest(BaseModel):
    """Grant grace for a missed target day."""
    daily_performance_id: uuid.UUID
    reason: str = Field(..., min_length=5, max_length=500)


class AdminGraceResponse(BaseModel):
    id: uuid.UUID
    agent_id: uuid.UUID
    grace_date: date
    reason: str
    granted_by_id: uuid.UUID
    daily_performance_id: uuid.UUID
    created_at: datetime

    @classmethod
    def from_grace(cls, g: "AgentGrace") -> "AdminGraceResponse":  # type: ignore[name-defined]  # noqa
        return cls(
            id=g.id,
            agent_id=g.agent_id,
            grace_date=g.grace_date,
            reason=g.reason,
            granted_by_id=g.granted_by_id,
            daily_performance_id=g.daily_performance_id,
            created_at=g.created_at,
        )


class AdminGraceListResponse(BaseModel):
    items: list[AdminGraceResponse]
    total: int


class IssueWarningRequest(BaseModel):
    """Issue a warning for a missed target."""
    daily_performance_id: Optional[uuid.UUID] = None
    reason: str = Field(..., min_length=5, max_length=500)


class AdminWarningResponse(BaseModel):
    id: uuid.UUID
    agent_id: uuid.UUID
    warning_date: date
    warning_number: int
    reason: str
    issued_by_id: uuid.UUID
    daily_performance_id: Optional[uuid.UUID] = None
    created_at: datetime

    @classmethod
    def from_warning(cls, w: "AgentWarning") -> "AdminWarningResponse":  # type: ignore[name-defined]  # noqa
        return cls(
            id=w.id,
            agent_id=w.agent_id,
            warning_date=w.warning_date,
            warning_number=w.warning_number,
            reason=w.reason,
            issued_by_id=w.issued_by_id,
            daily_performance_id=w.daily_performance_id,
            created_at=w.created_at,
        )


class AdminWarningListResponse(BaseModel):
    items: list[AdminWarningResponse]
    total: int


class ExcuseDayRequest(BaseModel):
    """Admin excuses a specific daily performance record."""
    daily_performance_id: uuid.UUID
    note: str = Field(..., min_length=3, max_length=500)


# ── Admin — Salary schemas ─────────────────────────────────────────────────────

class ManualSalaryAdjustRequest(BaseModel):
    """Admin applies a manual salary adjustment for the month."""
    year: int = Field(..., ge=2024, le=2100)
    month: int = Field(..., ge=1, le=12)
    adjustment: Decimal = Field(
        ...,
        description="Positive = bonus, negative = deduction. No float.",
    )
    reason: str = Field(..., min_length=5, max_length=500)

    @field_validator("adjustment")
    @classmethod
    def validate_adjustment(cls, v: Decimal) -> Decimal:
        if v == 0:
            raise ValueError("Adjustment amount cannot be zero.")
        if abs(v) > Decimal("100000"):
            raise ValueError("Adjustment exceeds maximum allowed value.")
        return v


class ConfirmSalaryRequest(BaseModel):
    """Admin confirms a monthly salary record."""
    year: int = Field(..., ge=2024, le=2100)
    month: int = Field(..., ge=1, le=12)
    notes: Optional[str] = Field(None, max_length=1000)


class AdminSalaryResponse(BaseModel):
    """Full salary record for admin panel."""
    id: uuid.UUID
    agent_id: uuid.UUID
    year: int
    month: int
    base_salary: str
    total_extra_bonus: str
    total_deductions: str
    manual_adjustment: str
    manual_adjustment_reason: Optional[str] = None
    final_salary: str
    status: str
    notes: Optional[str] = None
    confirmed_by_id: Optional[uuid.UUID] = None
    confirmed_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_record(cls, r: "AgentSalaryRecord") -> "AdminSalaryResponse":  # type: ignore[name-defined]  # noqa
        return cls(
            id=r.id,
            agent_id=r.agent_id,
            year=r.year,
            month=r.month,
            base_salary=str(r.base_salary),
            total_extra_bonus=str(r.total_extra_bonus),
            total_deductions=str(r.total_deductions),
            manual_adjustment=str(r.manual_adjustment),
            manual_adjustment_reason=r.manual_adjustment_reason,
            final_salary=str(r.final_salary),
            status=r.status.value,
            notes=r.notes,
            confirmed_by_id=r.confirmed_by_id,
            confirmed_at=r.confirmed_at,
            created_at=r.created_at,
            updated_at=r.updated_at,
        )


class AdminSalaryListResponse(BaseModel):
    items: list[AdminSalaryResponse]
    total: int
    limit: int
    offset: int


# ── Admin — Target config schemas ──────────────────────────────────────────────

class UpdateTargetConfigRequest(BaseModel):
    """Super Admin updates a tier's daily target or bonus."""
    daily_required: Optional[int] = Field(None, ge=1, le=50)
    extra_bonus_per_tournament: Optional[Decimal] = Field(None, gt=Decimal("0"))
    deduction_per_missing: Optional[Decimal] = Field(None, ge=Decimal("0"))
    base_monthly_salary: Optional[Decimal] = Field(None, gt=Decimal("0"))
    is_active: Optional[bool] = None
    label: Optional[str] = Field(None, max_length=60)


class TargetConfigResponse(BaseModel):
    id: uuid.UUID
    entry_fee: Optional[str] = None
    label: str
    daily_required: Optional[int] = None
    extra_bonus_per_tournament: Optional[str] = None
    deduction_per_missing: Optional[str] = None
    base_monthly_salary: Optional[str] = None
    is_active: bool
    updated_at: datetime

    @classmethod
    def from_config(cls, c: "AgentTargetConfig") -> "TargetConfigResponse":  # type: ignore[name-defined]  # noqa
        return cls(
            id=c.id,
            entry_fee=str(c.entry_fee) if c.entry_fee is not None else None,
            label=c.label,
            daily_required=c.daily_required,
            extra_bonus_per_tournament=(
                str(c.extra_bonus_per_tournament)
                if c.extra_bonus_per_tournament is not None else None
            ),
            deduction_per_missing=(
                str(c.deduction_per_missing)
                if c.deduction_per_missing is not None else None
            ),
            base_monthly_salary=(
                str(c.base_monthly_salary)
                if c.base_monthly_salary is not None else None
            ),
            is_active=c.is_active,
            updated_at=c.updated_at,
        )


class TargetConfigListResponse(BaseModel):
    items: list[TargetConfigResponse]


# ── Agent permission management (Phase 10) ───────────────────────────────────

class AgentPermissionItem(BaseModel):
    """Single permission grant shown on an agent profile."""
    permission_id: uuid.UUID
    code: str
    description: Optional[str]
    granted_at: datetime
    granted_by_id: Optional[uuid.UUID]


class AgentPermissionsResponse(BaseModel):
    agent_id: str
    permissions: list[AgentPermissionItem]


class GrantPermissionRequest(BaseModel):
    """Super Admin grants a permission to an agent."""
    permission_code: str = Field(..., min_length=3, max_length=80)


class RevokePermissionResponse(BaseModel):
    message: str
    agent_id: str
    permission_code: str
