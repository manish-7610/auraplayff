"""
Agent self-service endpoints.

Routes
------
GET  /api/agent/me                      — own profile
GET  /api/agent/performance/today       — today's performance across all tiers
GET  /api/agent/performance/month       — monthly performance rollup
GET  /api/agent/performance/month/{year}/{month}  — specific month
GET  /api/agent/tournaments             — own tournament history
GET  /api/agent/salary                  — own current-month salary estimate
GET  /api/agent/salary/{year}/{month}   — specific month salary
GET  /api/agent/warnings                — own warnings
GET  /api/agent/graces                  — own graces

Security:
  - All routes require staff authentication (audience="staff", role=agent).
  - Super Admin may also call these (they have all permissions).
  - Agents CANNOT see other agents' data — identity is always from JWT.
  - No performance data can be modified by agents.
  - Tournament counts are derived from DB records — never from agent input.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppError
from app.dependencies import get_current_staff, get_db
from app.models.staff_user import StaffUser
from app.models.tournament import Tournament
from app.schemas.agent import (
    AgentMeResponse,
    AgentMonthPerformanceResponse,
    AgentSalaryResponse,
    AgentTodayPerformanceResponse,
    AgentTournamentSummaryItem,
    AgentTournamentSummaryResponse,
    AdminGraceListResponse,
    AdminGraceResponse,
    AdminWarningListResponse,
    AdminWarningResponse,
    TierPerformanceItem,
)
from app.services import performance_service, salary_service

router = APIRouter(prefix="/api/agent", tags=["Agent — Self-Service"])


# ── Helper — current date in IST (or UTC as fallback) ─────────────────────────
def _today() -> "date":  # noqa: F821
    from datetime import date
    return datetime.now(timezone.utc).date()


# ── Profile ───────────────────────────────────────────────────────────────────

@router.get("/me", response_model=AgentMeResponse, summary="Agent own profile")
async def get_me(
    staff: StaffUser = Depends(get_current_staff),
) -> AgentMeResponse:
    """Return the authenticated agent's own profile. No sensitive fields."""
    return AgentMeResponse.from_staff_user(staff)


# ── Performance ───────────────────────────────────────────────────────────────

@router.get(
    "/performance/today",
    response_model=AgentTodayPerformanceResponse,
    summary="Today's performance across all tiers",
)
async def get_performance_today(
    staff: StaffUser = Depends(get_current_staff),
    db: AsyncSession = Depends(get_db),
) -> AgentTodayPerformanceResponse:
    """
    Return today's tournament performance for the authenticated agent.
    Refreshes counts from Tournament records on every call so it is always current.
    """
    from decimal import Decimal
    today = _today()

    async with db.begin():
        # Refresh counts from actual tournament data
        await performance_service.refresh_daily_performance(db, staff.id, today)

    async with db.begin():
        rows = await performance_service.get_daily_performance_rows(db, staff.id, today)

    tiers = [TierPerformanceItem.from_perf(r) for r in rows]
    total_completed = sum(r.completed_count for r in rows)
    total_bonus = sum(r.extra_bonus for r in rows)
    total_ded = sum(r.deduction_amount for r in rows)

    return AgentTodayPerformanceResponse(
        date=today,
        tiers=tiers,
        total_completed=total_completed,
        total_extra_bonus=str(total_bonus),
        total_deductions=str(total_ded),
    )


@router.get(
    "/performance/month",
    response_model=AgentMonthPerformanceResponse,
    summary="Current month performance rollup",
)
async def get_performance_month_current(
    staff: StaffUser = Depends(get_current_staff),
    db: AsyncSession = Depends(get_db),
) -> AgentMonthPerformanceResponse:
    """Return the current month's performance rollup for the authenticated agent."""
    today = _today()
    return await _build_month_response(db, staff.id, today.year, today.month)


@router.get(
    "/performance/month/{year}/{month}",
    response_model=AgentMonthPerformanceResponse,
    summary="Specific month performance rollup",
)
async def get_performance_month(
    year: int,
    month: int,
    staff: StaffUser = Depends(get_current_staff),
    db: AsyncSession = Depends(get_db),
) -> AgentMonthPerformanceResponse:
    """Return a specific month's performance rollup for the authenticated agent."""
    if not (1 <= month <= 12):
        raise AppError("invalid_month", "Month must be 1–12.", 422)
    return await _build_month_response(db, staff.id, year, month)


# ── Tournaments ───────────────────────────────────────────────────────────────

@router.get(
    "/tournaments",
    response_model=AgentTournamentSummaryResponse,
    summary="Own tournament history",
)
async def get_my_tournaments(
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    status: Optional[str] = Query(default=None),
    staff: StaffUser = Depends(get_current_staff),
    db: AsyncSession = Depends(get_db),
) -> AgentTournamentSummaryResponse:
    """
    Return the agent's own tournament history.
    Agent can only see tournaments they created (created_by_id == agent.id).
    """
    from sqlalchemy import func

    async with db.begin():
        q = select(Tournament).where(Tournament.created_by_id == staff.id)
        if status:
            q = q.where(Tournament.status == status)

        count_result = await db.execute(select(func.count()).select_from(q.subquery()))
        total = count_result.scalar_one()

        rows = await db.execute(
            q.order_by(Tournament.scheduled_at.desc()).limit(limit).offset(offset)
        )
        tournaments = list(rows.scalars().all())

    items = [
        AgentTournamentSummaryItem(
            tournament_id=t.id,
            tournament_code=t.tournament_code,
            title=t.title,
            entry_fee=str(t.entry_fee),
            status=t.status.value,
            player_count=t.player_count,
            scheduled_at=t.scheduled_at,
            completed_at=t.completed_at,
            cancelled_at=t.cancelled_at,
        )
        for t in tournaments
    ]
    return AgentTournamentSummaryResponse(
        items=items, total=total, limit=limit, offset=offset
    )


# ── Salary ────────────────────────────────────────────────────────────────────

@router.get(
    "/salary",
    response_model=AgentSalaryResponse,
    summary="Current month salary estimate",
)
async def get_salary_current(
    staff: StaffUser = Depends(get_current_staff),
    db: AsyncSession = Depends(get_db),
) -> AgentSalaryResponse:
    """Return the current month's salary estimate for the authenticated agent."""
    today = _today()
    async with db.begin():
        record = await salary_service.recalculate_salary_record(
            db, staff.id, today.year, today.month
        )
    return AgentSalaryResponse.from_record(record)


@router.get(
    "/salary/{year}/{month}",
    response_model=AgentSalaryResponse,
    summary="Specific month salary",
)
async def get_salary_month(
    year: int,
    month: int,
    staff: StaffUser = Depends(get_current_staff),
    db: AsyncSession = Depends(get_db),
) -> AgentSalaryResponse:
    """Return a specific month's salary record for the authenticated agent."""
    if not (1 <= month <= 12):
        raise AppError("invalid_month", "Month must be 1–12.", 422)
    async with db.begin():
        record = await salary_service.get_or_create_salary_record(
            db, staff.id, year, month
        )
    return AgentSalaryResponse.from_record(record)


# ── Warnings / Graces ─────────────────────────────────────────────────────────

@router.get(
    "/warnings",
    response_model=AdminWarningListResponse,
    summary="Own warnings",
)
async def get_my_warnings(
    staff: StaffUser = Depends(get_current_staff),
    db: AsyncSession = Depends(get_db),
) -> AdminWarningListResponse:
    """Return the authenticated agent's own warning history."""
    async with db.begin():
        warnings, total = await performance_service.list_warnings(db, agent_id=staff.id)
    return AdminWarningListResponse(
        items=[AdminWarningResponse.from_warning(w) for w in warnings],
        total=total,
    )


@router.get(
    "/graces",
    response_model=AdminGraceListResponse,
    summary="Own grace records",
)
async def get_my_graces(
    staff: StaffUser = Depends(get_current_staff),
    db: AsyncSession = Depends(get_db),
) -> AdminGraceListResponse:
    """Return the authenticated agent's own grace history."""
    async with db.begin():
        graces, total = await performance_service.list_graces(db, agent_id=staff.id)
    return AdminGraceListResponse(
        items=[AdminGraceResponse.from_grace(g) for g in graces],
        total=total,
    )


# ── Private helpers ───────────────────────────────────────────────────────────

async def _build_month_response(
    db: AsyncSession,
    agent_id: uuid.UUID,
    year: int,
    month: int,
) -> AgentMonthPerformanceResponse:
    from decimal import Decimal

    async with db.begin():
        rows = await performance_service.get_month_performance_rows(
            db, agent_id, year, month
        )
        record = await salary_service.get_or_create_salary_record(
            db, agent_id, year, month
        )

    total_bonus = sum(r.extra_bonus for r in rows) if rows else Decimal("0")
    total_ded = sum(r.deduction_amount for r in rows) if rows else Decimal("0")

    from app.models.agent_performance import DailyPerfStatus
    days_met = sum(1 for r in rows if r.status == DailyPerfStatus.met)
    days_exceeded = sum(1 for r in rows if r.status == DailyPerfStatus.exceeded)
    days_graced = sum(1 for r in rows if r.is_grace)
    days_warned = sum(1 for r in rows if r.is_warned)
    days_excused = sum(1 for r in rows if r.is_excused)
    days_missed = sum(
        1 for r in rows
        if r.shortfall > 0 and not r.is_grace and not r.is_excused
    )

    estimated = record.final_salary

    return AgentMonthPerformanceResponse(
        year=year,
        month=month,
        base_salary=str(record.base_salary),
        total_extra_bonus=str(total_bonus),
        total_deductions=str(total_ded),
        manual_adjustment=str(record.manual_adjustment),
        estimated_final_salary=str(estimated),
        days_met=days_met,
        days_missed=days_missed,
        days_graced=days_graced,
        days_warned=days_warned,
        days_excused=days_excused,
        days_exceeded=days_exceeded,
        salary_status=record.status.value,
    )
