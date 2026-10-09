"""
Admin agent management endpoints — Phase 6.

Routes
------
POST  /api/admin/agents                              — create agent
GET   /api/admin/agents                              — list agents
GET   /api/admin/agents/{id}                         — get agent
PATCH /api/admin/agents/{id}                         — update agent
POST  /api/admin/agents/{id}/activate                — activate
POST  /api/admin/agents/{id}/deactivate              — deactivate
POST  /api/admin/agents/{id}/reset-password          — reset password

GET   /api/admin/agents/{id}/performance/today       — agent today performance
GET   /api/admin/agents/{id}/performance/month/{yr}/{mo} — agent month performance
GET   /api/admin/agents/{id}/performance/daily       — paginated daily performance
POST  /api/admin/agents/{id}/performance/refresh     — refresh daily counts from tournaments

POST  /api/admin/agents/{id}/grace                   — grant grace
POST  /api/admin/agents/{id}/warning                 — issue warning
POST  /api/admin/agents/{id}/excuse-day              — excuse a day

GET   /api/admin/agents/{id}/salary                  — current month salary
GET   /api/admin/agents/{id}/salary/{yr}/{mo}        — specific month salary
POST  /api/admin/agents/{id}/salary/adjust           — manual salary adjustment
POST  /api/admin/agents/{id}/salary/confirm          — confirm monthly salary
GET   /api/admin/agents/{id}/graces                  — list graces
GET   /api/admin/agents/{id}/warnings                — list warnings

GET   /api/admin/targets                             — list all tier configs
PUT   /api/admin/targets/{entry_fee}                 — update a tier config

Permissions:
  AGENT_CREATE           — create agent
  AGENT_VIEW             — list/get agents and performance
  AGENT_MANAGE           — activate/deactivate/update
  AGENT_RESET_PASSWORD   — reset password
  AGENT_VIEW_PERFORMANCE — view performance data
  AGENT_MANAGE_PERFORMANCE — grace/warning/excuse
  AGENT_MANAGE_SALARY    — salary adjustment/confirm
  AGENT_MANAGE_TARGETS   — update target config

Security:
  - Super Admin bypasses all permission checks.
  - Agents cannot call any of these endpoints.
  - Every action is audited.
  - Passwords are never returned.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import joinedload
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppError, PermissionDeniedError
from app.core.permissions import Permission
from app.dependencies import get_current_staff, get_db, get_client_ip
from app.models.agent_target_config import AgentTargetConfig
from app.models.permission import Permission as PermModel, StaffUserPermission
from app.models.staff_user import StaffRole, StaffUser
from app.schemas.agent import (
    AdminAgentListResponse,
    AdminAgentResponse,
    AdminDailyPerfListResponse,
    AdminDailyPerfResponse,
    AdminGraceListResponse,
    AdminGraceResponse,
    AdminMonthPerfResponse,
    AdminSalaryListResponse,
    AdminSalaryResponse,
    AdminWarningListResponse,
    AdminWarningResponse,
    AgentPermissionItem,
    AgentPermissionsResponse,
    ConfirmSalaryRequest,
    CreateAgentRequest,
    ExcuseDayRequest,
    GrantGraceRequest,
    GrantPermissionRequest,
    IssueWarningRequest,
    ManualSalaryAdjustRequest,
    ResetAgentPasswordRequest,
    RevokePermissionResponse,
    TargetConfigListResponse,
    TargetConfigResponse,
    UpdateAgentRequest,
    UpdateTargetConfigRequest,
    AgentTodayPerformanceResponse,
    AgentMonthPerformanceResponse,
    TierPerformanceItem,
)
from app.services import agent_service, performance_service, salary_service
from app.models.agent_performance import DailyPerfStatus

router = APIRouter(prefix="/api/admin/agents", tags=["Admin — Agents"])
targets_router = APIRouter(prefix="/api/admin/targets", tags=["Admin — Target Config"])


# ── Permission helper (matches pattern used in admin_deposits.py) ─────────────

async def _check_perm(staff: StaffUser, db: AsyncSession, perm: Permission) -> None:
    if staff.role == StaffRole.super_admin:
        return
    # BUG #7 FIX: joinedload replaces N separate db.get() calls
    result = await db.execute(
        select(StaffUserPermission)
        .options(joinedload(StaffUserPermission.permission))
        .where(StaffUserPermission.staff_user_id == staff.id)
    )
    grants = result.unique().scalars().all()
    granted: set[str] = {
        g.permission.code for g in grants if g.permission is not None
    }
    if perm.value not in granted:
        raise PermissionDeniedError(perm.value)


def _ip(request: Request) -> str | None:
    return get_client_ip(request)


def _today():
    return datetime.now(timezone.utc).date()


# ── Create / List / Get ───────────────────────────────────────────────────────

@router.post("", response_model=AdminAgentResponse, status_code=201,
             summary="Create a new agent")
async def create_agent(
    body: CreateAgentRequest,
    request: Request,
    staff: StaffUser = Depends(get_current_staff),
    db: AsyncSession = Depends(get_db),
) -> AdminAgentResponse:
    """Create a new agent account. agent_id is server-generated (AGT-NNNN)."""
    await _check_perm(staff, db, Permission.AGENT_CREATE)
    agent = await agent_service.create_agent(
        db,
        display_name=body.display_name,
        password=body.password,
        created_by_id=staff.id,
        ip_address=_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    await db.commit()
    # BUG #15 FIX: db.commit() expires all ORM attributes. Refresh before
    # building the response DTO to avoid MissingGreenlet on attribute access.
    await db.refresh(agent)
    return AdminAgentResponse.from_staff_user(agent)


@router.get("", response_model=AdminAgentListResponse, summary="List all agents")
async def list_agents(
    is_active: Annotated[Optional[bool], Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    staff: StaffUser = Depends(get_current_staff),
    db: AsyncSession = Depends(get_db),
) -> AdminAgentListResponse:
    await _check_perm(staff, db, Permission.AGENT_VIEW)
    agents, total = await agent_service.list_agents(
        db, role=StaffRole.agent, is_active=is_active, limit=limit, offset=offset
    )
    return AdminAgentListResponse(
        items=[AdminAgentResponse.from_staff_user(a) for a in agents],
        total=total, limit=limit, offset=offset,
    )


@router.get("/{agent_id}", response_model=AdminAgentResponse, summary="Get agent details")
async def get_agent(
    agent_id: uuid.UUID,
    staff: StaffUser = Depends(get_current_staff),
    db: AsyncSession = Depends(get_db),
) -> AdminAgentResponse:
    await _check_perm(staff, db, Permission.AGENT_VIEW)
    agent = await agent_service.get_agent(db, agent_id)
    return AdminAgentResponse.from_staff_user(agent)


@router.patch("/{agent_id}", response_model=AdminAgentResponse, summary="Update agent")
async def update_agent(
    agent_id: uuid.UUID,
    body: UpdateAgentRequest,
    request: Request,
    staff: StaffUser = Depends(get_current_staff),
    db: AsyncSession = Depends(get_db),
) -> AdminAgentResponse:
    await _check_perm(staff, db, Permission.AGENT_MANAGE)
    agent = await agent_service.update_agent(
        db, agent_id,
        display_name=body.display_name,
        admin_id=staff.id,
        ip_address=_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    await db.commit()
    # BUG #15 FIX: refresh after commit to avoid MissingGreenlet
    await db.refresh(agent)
    return AdminAgentResponse.from_staff_user(agent)


@router.post("/{agent_id}/activate", response_model=AdminAgentResponse,
             summary="Activate agent")
async def activate_agent(
    agent_id: uuid.UUID,
    request: Request,
    staff: StaffUser = Depends(get_current_staff),
    db: AsyncSession = Depends(get_db),
) -> AdminAgentResponse:
    await _check_perm(staff, db, Permission.AGENT_MANAGE)
    agent = await agent_service.activate_agent(
        db, agent_id, admin_id=staff.id,
        ip_address=_ip(request), user_agent=request.headers.get("user-agent"),
    )
    await db.commit()
    # BUG #15 FIX: refresh after commit to avoid MissingGreenlet
    await db.refresh(agent)
    return AdminAgentResponse.from_staff_user(agent)


@router.post("/{agent_id}/deactivate", response_model=AdminAgentResponse,
             summary="Deactivate agent")
async def deactivate_agent(
    agent_id: uuid.UUID,
    request: Request,
    staff: StaffUser = Depends(get_current_staff),
    db: AsyncSession = Depends(get_db),
) -> AdminAgentResponse:
    await _check_perm(staff, db, Permission.AGENT_MANAGE)
    agent = await agent_service.deactivate_agent(
        db, agent_id, admin_id=staff.id,
        ip_address=_ip(request), user_agent=request.headers.get("user-agent"),
    )
    await db.commit()
    # BUG #15 FIX: refresh after commit to avoid MissingGreenlet
    await db.refresh(agent)
    return AdminAgentResponse.from_staff_user(agent)


@router.post("/{agent_id}/reset-password", response_model=AdminAgentResponse,
             summary="Reset agent password")
async def reset_password(
    agent_id: uuid.UUID,
    body: ResetAgentPasswordRequest,
    request: Request,
    staff: StaffUser = Depends(get_current_staff),
    db: AsyncSession = Depends(get_db),
) -> AdminAgentResponse:
    """Reset agent password. Invalidates all existing sessions via token_version bump."""
    await _check_perm(staff, db, Permission.AGENT_RESET_PASSWORD)
    agent = await agent_service.reset_agent_password(
        db, agent_id, body.new_password,
        admin_id=staff.id,
        ip_address=_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    await db.commit()
    # BUG #15 FIX: refresh after commit to avoid MissingGreenlet
    await db.refresh(agent)
    return AdminAgentResponse.from_staff_user(agent)


# ── Performance ───────────────────────────────────────────────────────────────

@router.post("/{agent_id}/performance/refresh", response_model=dict,
             summary="Refresh today's performance counts from tournament records")
async def refresh_performance(
    agent_id: uuid.UUID,
    request: Request,
    staff: StaffUser = Depends(get_current_staff),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Recompute daily performance counts by scanning actual Tournament records.
    Safe to call multiple times — idempotent by design.
    """
    await _check_perm(staff, db, Permission.AGENT_VIEW_PERFORMANCE)
    today = _today()
    rows = await performance_service.refresh_daily_performance(db, agent_id, today)
    await db.commit()
    return {"refreshed": len(rows), "date": str(today)}


@router.get("/{agent_id}/performance/today",
            response_model=AgentTodayPerformanceResponse,
            summary="Agent today performance")
async def agent_today_perf(
    agent_id: uuid.UUID,
    staff: StaffUser = Depends(get_current_staff),
    db: AsyncSession = Depends(get_db),
) -> AgentTodayPerformanceResponse:
    await _check_perm(staff, db, Permission.AGENT_VIEW_PERFORMANCE)
    from decimal import Decimal
    today = _today()
    await performance_service.refresh_daily_performance(db, agent_id, today)
    rows = await performance_service.get_daily_performance_rows(db, agent_id, today)
    tiers = [TierPerformanceItem.from_perf(r) for r in rows]
    total_bonus = sum(r.extra_bonus for r in rows) if rows else Decimal("0")
    total_ded = sum(r.deduction_amount for r in rows) if rows else Decimal("0")
    return AgentTodayPerformanceResponse(
        date=today,
        tiers=tiers,
        total_completed=sum(r.completed_count for r in rows),
        total_extra_bonus=str(total_bonus),
        total_deductions=str(total_ded),
    )


@router.get("/{agent_id}/performance/month/{year}/{month}",
            response_model=AdminMonthPerfResponse,
            summary="Agent month performance")
async def agent_month_perf(
    agent_id: uuid.UUID,
    year: int,
    month: int,
    staff: StaffUser = Depends(get_current_staff),
    db: AsyncSession = Depends(get_db),
) -> AdminMonthPerfResponse:
    await _check_perm(staff, db, Permission.AGENT_VIEW_PERFORMANCE)
    if not (1 <= month <= 12):
        raise AppError("invalid_month", "Month must be 1–12.", 422)
    from decimal import Decimal

    rows = await performance_service.get_month_performance_rows(
        db, agent_id, year, month
    )
    record = await salary_service.get_or_create_salary_record(
        db, agent_id, year, month
    )

    total_bonus = sum(r.extra_bonus for r in rows) if rows else Decimal("0")
    total_ded = sum(r.deduction_amount for r in rows) if rows else Decimal("0")
    days_met = sum(1 for r in rows if r.status == DailyPerfStatus.met)
    days_exceeded = sum(1 for r in rows if r.status == DailyPerfStatus.exceeded)
    days_graced = sum(1 for r in rows if r.is_grace)
    days_warned = sum(1 for r in rows if r.is_warned)
    days_excused = sum(1 for r in rows if r.is_excused)
    days_missed = sum(
        1 for r in rows if r.shortfall > 0 and not r.is_grace and not r.is_excused
    )

    # Tier breakdown for admin detail view
    from collections import defaultdict
    tier_map: dict = defaultdict(lambda: {
        "entry_fee": "", "total_completed": 0, "total_required": 0,
        "total_bonus": Decimal("0"), "total_deduction": Decimal("0"),
    })
    for r in rows:
        key = str(r.entry_fee)
        tier_map[key]["entry_fee"] = str(r.entry_fee)
        tier_map[key]["total_completed"] += r.completed_count
        tier_map[key]["total_required"] += r.required_count
        tier_map[key]["total_bonus"] += r.extra_bonus
        tier_map[key]["total_deduction"] += r.deduction_amount

    tier_breakdown = [
        {
            "entry_fee": v["entry_fee"],
            "total_completed": v["total_completed"],
            "total_required": v["total_required"],
            "total_bonus": str(v["total_bonus"]),
            "total_deduction": str(v["total_deduction"]),
        }
        for v in tier_map.values()
    ]

    return AdminMonthPerfResponse(
        agent_id=agent_id,
        year=year,
        month=month,
        base_salary=str(record.base_salary),
        total_extra_bonus=str(total_bonus),
        total_deductions=str(total_ded),
        manual_adjustment=str(record.manual_adjustment),
        estimated_final_salary=str(record.final_salary),
        days_met=days_met,
        days_missed=days_missed,
        days_graced=days_graced,
        days_warned=days_warned,
        days_excused=days_excused,
        days_exceeded=days_exceeded,
        salary_status=record.status.value,
        tier_breakdown=tier_breakdown,
    )


@router.get("/{agent_id}/performance/daily",
            response_model=AdminDailyPerfListResponse,
            summary="Paginated daily performance records")
async def agent_daily_perf_list(
    agent_id: uuid.UUID,
    perf_date: Annotated[Optional[str], Query()] = None,
    status: Annotated[Optional[str], Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    staff: StaffUser = Depends(get_current_staff),
    db: AsyncSession = Depends(get_db),
) -> AdminDailyPerfListResponse:
    await _check_perm(staff, db, Permission.AGENT_VIEW_PERFORMANCE)
    from datetime import date as _date
    parsed_date: _date | None = None
    if perf_date:
        try:
            parsed_date = _date.fromisoformat(perf_date)
        except ValueError:
            raise AppError("invalid_date", "perf_date must be YYYY-MM-DD.", 422)

    rows, total = await performance_service.list_daily_performances(
        db, agent_id=agent_id, perf_date=parsed_date,
        status=status, limit=limit, offset=offset,
    )
    return AdminDailyPerfListResponse(
        items=[AdminDailyPerfResponse.from_perf(r) for r in rows],
        total=total, limit=limit, offset=offset,
    )


# ── Grace / Warning / Excuse ──────────────────────────────────────────────────

@router.post("/{agent_id}/grace", response_model=AdminGraceResponse,
             summary="Grant grace for a missed target day")
async def grant_grace(
    agent_id: uuid.UUID,
    body: GrantGraceRequest,
    request: Request,
    staff: StaffUser = Depends(get_current_staff),
    db: AsyncSession = Depends(get_db),
) -> AdminGraceResponse:
    """
    Grant grace for a specific daily performance record.
    Zeroes deduction_amount for that day.
    Only one grace per performance record.
    """
    await _check_perm(staff, db, Permission.AGENT_MANAGE_PERFORMANCE)
    grace = await performance_service.grant_grace(
        db,
        daily_performance_id=body.daily_performance_id,
        reason=body.reason,
        granted_by_id=staff.id,
        ip_address=_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    await db.commit()
    # BUG #16 FIX: snapshot scalar values before accessing them post-commit.
    # db.commit() expires all ORM attributes; accessing them without a refresh
    # triggers implicit lazy loads that raise MissingGreenlet in async context.
    # Also wrap the notification in async with db.begin() — without an explicit
    # transaction the notify_agent call's db.flush() runs without a transaction,
    # which either raises or silently does nothing (depends on autobegin setting).
    grace_id_val   = grace.id
    grace_date_val = grace.grace_date.isoformat() if grace.grace_date is not None else ""
    await db.refresh(grace)
    from app.services import notification_service as ns
    try:
        async with db.begin():
            await ns.notify_agent_grace_granted(
                db,
                staff_user_id=agent_id,
                grace_id=grace_id_val,
                perf_date=grace_date_val,
                reason=body.reason,
            )
    except Exception:
        pass
    return AdminGraceResponse.from_grace(grace)


@router.post("/{agent_id}/warning", response_model=AdminWarningResponse,
             summary="Issue a warning to an agent")
async def issue_warning(
    agent_id: uuid.UUID,
    body: IssueWarningRequest,
    request: Request,
    staff: StaffUser = Depends(get_current_staff),
    db: AsyncSession = Depends(get_db),
) -> AdminWarningResponse:
    """Issue a warning. Informational only — does not deduct salary automatically."""
    await _check_perm(staff, db, Permission.AGENT_MANAGE_PERFORMANCE)
    warning = await performance_service.issue_warning(
        db,
        agent_id=agent_id,
        reason=body.reason,
        issued_by_id=staff.id,
        daily_performance_id=body.daily_performance_id,
        ip_address=_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    await db.commit()
    # BUG #17 FIX: snapshot scalars before post-commit access, wrap notification
    # in async with db.begin(), and refresh before building the response DTO.
    warning_id_val     = warning.id
    warning_date_val   = warning.warning_date.isoformat() if warning.warning_date is not None else ""
    warning_number_val = warning.warning_number
    await db.refresh(warning)
    from app.services import notification_service as ns
    try:
        async with db.begin():
            await ns.notify_agent_warning_issued(
                db,
                staff_user_id=agent_id,
                warning_id=warning_id_val,
                perf_date=warning_date_val,
                warning_number=warning_number_val,
                reason=body.reason,
            )
    except Exception:
        pass
    return AdminWarningResponse.from_warning(warning)


@router.post("/{agent_id}/excuse-day", response_model=AdminDailyPerfResponse,
             summary="Excuse a missed day")
async def excuse_day(
    agent_id: uuid.UUID,
    body: ExcuseDayRequest,
    request: Request,
    staff: StaffUser = Depends(get_current_staff),
    db: AsyncSession = Depends(get_db),
) -> AdminDailyPerfResponse:
    """Mark a day as excused — zeroes deduction without creating a grace record."""
    await _check_perm(staff, db, Permission.AGENT_MANAGE_PERFORMANCE)
    perf = await performance_service.excuse_day(
        db,
        daily_performance_id=body.daily_performance_id,
        note=body.note,
        admin_id=staff.id,
        ip_address=_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    await db.commit()
    # BUG #18 FIX (same pattern): refresh after commit before building DTO
    await db.refresh(perf)
    return AdminDailyPerfResponse.from_perf(perf)


@router.get("/{agent_id}/graces", response_model=AdminGraceListResponse,
            summary="List agent grace records")
async def list_graces(
    agent_id: uuid.UUID,
    staff: StaffUser = Depends(get_current_staff),
    db: AsyncSession = Depends(get_db),
) -> AdminGraceListResponse:
    await _check_perm(staff, db, Permission.AGENT_VIEW_PERFORMANCE)
    graces, total = await performance_service.list_graces(db, agent_id=agent_id)
    return AdminGraceListResponse(
        items=[AdminGraceResponse.from_grace(g) for g in graces], total=total
    )


@router.get("/{agent_id}/warnings", response_model=AdminWarningListResponse,
            summary="List agent warnings")
async def list_warnings(
    agent_id: uuid.UUID,
    staff: StaffUser = Depends(get_current_staff),
    db: AsyncSession = Depends(get_db),
) -> AdminWarningListResponse:
    await _check_perm(staff, db, Permission.AGENT_VIEW_PERFORMANCE)
    warnings, total = await performance_service.list_warnings(
        db, agent_id=agent_id
    )
    return AdminWarningListResponse(
        items=[AdminWarningResponse.from_warning(w) for w in warnings], total=total
    )


# ── Salary ────────────────────────────────────────────────────────────────────

@router.get("/{agent_id}/salary", response_model=AdminSalaryResponse,
            summary="Current month salary")
async def get_salary_current(
    agent_id: uuid.UUID,
    staff: StaffUser = Depends(get_current_staff),
    db: AsyncSession = Depends(get_db),
) -> AdminSalaryResponse:
    await _check_perm(staff, db, Permission.AGENT_VIEW_PERFORMANCE)
    today = _today()
    record = await salary_service.recalculate_salary_record(
        db, agent_id, today.year, today.month
    )
    return AdminSalaryResponse.from_record(record)


@router.get("/{agent_id}/salary/{year}/{month}", response_model=AdminSalaryResponse,
            summary="Specific month salary")
async def get_salary_month(
    agent_id: uuid.UUID, year: int, month: int,
    staff: StaffUser = Depends(get_current_staff),
    db: AsyncSession = Depends(get_db),
) -> AdminSalaryResponse:
    await _check_perm(staff, db, Permission.AGENT_VIEW_PERFORMANCE)
    if not (1 <= month <= 12):
        raise AppError("invalid_month", "Month must be 1–12.", 422)
    record = await salary_service.get_or_create_salary_record(
        db, agent_id, year, month
    )
    return AdminSalaryResponse.from_record(record)


@router.post("/{agent_id}/salary/adjust", response_model=AdminSalaryResponse,
             summary="Apply manual salary adjustment")
async def salary_adjust(
    agent_id: uuid.UUID,
    body: ManualSalaryAdjustRequest,
    request: Request,
    staff: StaffUser = Depends(get_current_staff),
    db: AsyncSession = Depends(get_db),
) -> AdminSalaryResponse:
    """Apply a manual +/- salary adjustment for the specified month."""
    await _check_perm(staff, db, Permission.AGENT_MANAGE_SALARY)
    record = await salary_service.apply_manual_adjustment(
        db,
        agent_id=agent_id,
        year=body.year,
        month=body.month,
        adjustment=body.adjustment,
        reason=body.reason,
        admin_id=staff.id,
        ip_address=_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    await db.commit()
    # BUG #18 FIX: snapshot scalars for the notification before post-commit
    # attribute access, wrap in async with db.begin(), and refresh record
    # before building the response DTO.
    record_id_val    = record.id
    record_year_val  = record.year
    record_month_val = record.month
    record_sal_val   = str(record.final_salary)
    await db.refresh(record)
    from app.services import notification_service as ns
    try:
        async with db.begin():
            await ns.notify_agent_salary_adjusted(
                db,
                staff_user_id=agent_id,
                salary_record_id=record_id_val,
                year=record_year_val,
                month=record_month_val,
                final_salary=record_sal_val,
            )
    except Exception:
        pass
    return AdminSalaryResponse.from_record(record)


@router.post("/{agent_id}/salary/confirm", response_model=AdminSalaryResponse,
             summary="Confirm monthly salary record")
async def salary_confirm(
    agent_id: uuid.UUID,
    body: ConfirmSalaryRequest,
    request: Request,
    staff: StaffUser = Depends(get_current_staff),
    db: AsyncSession = Depends(get_db),
) -> AdminSalaryResponse:
    """Lock a monthly salary record. Recalculates final_salary one last time."""
    await _check_perm(staff, db, Permission.AGENT_MANAGE_SALARY)
    record = await salary_service.confirm_salary_record(
        db,
        agent_id=agent_id,
        year=body.year,
        month=body.month,
        admin_id=staff.id,
        notes=body.notes,
        ip_address=_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    await db.commit()
    # BUG #18 FIX (same pattern): refresh after commit before building DTO
    await db.refresh(record)
    return AdminSalaryResponse.from_record(record)


@router.get("/{agent_id}/salary/history", response_model=AdminSalaryListResponse,
            summary="Agent salary history")
async def salary_history(
    agent_id: uuid.UUID,
    limit: Annotated[int, Query(ge=1, le=100)] = 24,
    offset: Annotated[int, Query(ge=0)] = 0,
    staff: StaffUser = Depends(get_current_staff),
    db: AsyncSession = Depends(get_db),
) -> AdminSalaryListResponse:
    await _check_perm(staff, db, Permission.AGENT_VIEW_PERFORMANCE)
    records, total = await salary_service.list_salary_records(
        db, agent_id=agent_id, limit=limit, offset=offset
    )
    return AdminSalaryListResponse(
        items=[AdminSalaryResponse.from_record(r) for r in records],
        total=total, limit=limit, offset=offset,
    )


# ── Target configuration ──────────────────────────────────────────────────────

@targets_router.get("", response_model=TargetConfigListResponse,
                    summary="List all tier target configs")
async def list_targets(
    staff: StaffUser = Depends(get_current_staff),
    db: AsyncSession = Depends(get_db),
) -> TargetConfigListResponse:
    await _check_perm(staff, db, Permission.AGENT_VIEW)
    configs = await performance_service.get_all_active_tier_configs(db)
    # Also include base salary row
    base_result = await db.execute(
        select(AgentTargetConfig).where(AgentTargetConfig.entry_fee.is_(None))
    )
    base_row = base_result.scalar_one_or_none()
    all_configs = list(configs)
    if base_row:
        all_configs.insert(0, base_row)
    return TargetConfigListResponse(
        items=[TargetConfigResponse.from_config(c) for c in all_configs]
    )


@targets_router.put("/{entry_fee_str}", response_model=TargetConfigResponse,
                    summary="Update a tier's target configuration")
async def update_target(
    entry_fee_str: str,
    body: UpdateTargetConfigRequest,
    request: Request,
    staff: StaffUser = Depends(get_current_staff),
    db: AsyncSession = Depends(get_db),
) -> TargetConfigResponse:
    """
    Update daily_required, extra_bonus, deduction, or base_salary for a tier.
    Use entry_fee_str = 'base' for the base salary config row.
    """
    await _check_perm(staff, db, Permission.AGENT_MANAGE_TARGETS)

    if entry_fee_str.lower() == "base":
        result = await db.execute(
            select(AgentTargetConfig).where(AgentTargetConfig.entry_fee.is_(None))
        )
    else:
        try:
            entry_fee_decimal = Decimal(entry_fee_str)
        except Exception:
            raise AppError("invalid_entry_fee", "entry_fee must be a decimal number or 'base'.", 422)
        result = await db.execute(
            select(AgentTargetConfig).where(
                AgentTargetConfig.entry_fee == entry_fee_decimal
            )
        )

    cfg: AgentTargetConfig | None = result.scalar_one_or_none()
    if cfg is None:
        raise AppError("target_config_not_found", "Target config not found.", 404)

    old_vals: dict = {}
    new_vals: dict = {}

    if body.daily_required is not None:
        old_vals["daily_required"] = cfg.daily_required
        cfg.daily_required = body.daily_required
        new_vals["daily_required"] = body.daily_required

    if body.extra_bonus_per_tournament is not None:
        old_vals["extra_bonus"] = str(cfg.extra_bonus_per_tournament)
        cfg.extra_bonus_per_tournament = body.extra_bonus_per_tournament
        new_vals["extra_bonus"] = str(body.extra_bonus_per_tournament)

    if body.deduction_per_missing is not None:
        old_vals["deduction_per_missing"] = str(cfg.deduction_per_missing)
        cfg.deduction_per_missing = body.deduction_per_missing
        new_vals["deduction_per_missing"] = str(body.deduction_per_missing)

    if body.base_monthly_salary is not None:
        old_vals["base_monthly_salary"] = str(cfg.base_monthly_salary)
        cfg.base_monthly_salary = body.base_monthly_salary
        new_vals["base_monthly_salary"] = str(body.base_monthly_salary)

    if body.is_active is not None:
        old_vals["is_active"] = cfg.is_active
        cfg.is_active = body.is_active
        new_vals["is_active"] = body.is_active

    if body.label is not None:
        cfg.label = body.label

    from app.models._types import utcnow
    cfg.updated_by = str(staff.id)
    cfg.updated_at = utcnow()
    await db.flush([cfg])

    from app.services import audit_service
    from app.models.audit_log import ActorType
    await audit_service.log(
        db=db,
        actor_type=ActorType.staff,
        actor_id=str(staff.id),
        action="target_config.updated",
        entity_type="agent_target_config",
        entity_id=str(cfg.id),
        old_value=old_vals,
        new_value=new_vals,
        ip_address=_ip(request),
        user_agent=request.headers.get("user-agent"),
    )

    return TargetConfigResponse.from_config(cfg)


# ── Agent permission management (Phase 10 — T4) ──────────────────────────────
# Super Admin only. Agents cannot manage their own permissions or other agents'.

@router.get(
    "/{agent_id}/permissions",
    response_model=AgentPermissionsResponse,
    summary="List an agent's granted permissions (Super Admin only)",
)
async def list_agent_permissions(
    agent_id: uuid.UUID,
    staff: StaffUser = Depends(get_current_staff),
    db: AsyncSession = Depends(get_db),
) -> AgentPermissionsResponse:
    """
    Returns all permissions currently granted to an agent.
    Super Admin only — prevents agents from enumerating each other's grants.
    """
    if staff.role != StaffRole.super_admin:
        raise AppError("permission_denied", "Only Super Admin can manage permissions.", 403)

    # Verify agent exists
    agent = await db.execute(
        select(StaffUser).where(StaffUser.id == agent_id)
    )
    agent_obj: StaffUser | None = agent.scalar_one_or_none()
    if agent_obj is None:
        raise AppError("not_found", "Agent not found.", 404)

    grants_result = await db.execute(
        select(StaffUserPermission)
        .options(joinedload(StaffUserPermission.permission))
        .where(StaffUserPermission.staff_user_id == agent_id)
    )
    grants = grants_result.unique().scalars().all()

    items: list[AgentPermissionItem] = []
    for g in grants:
        if g.permission is not None:
            items.append(AgentPermissionItem(
                permission_id=g.permission_id,
                code=g.permission.code,
                description=g.permission.description,
                granted_at=g.granted_at,
                granted_by_id=g.granted_by_id,
            ))

    return AgentPermissionsResponse(
        agent_id=agent_obj.agent_id,
        permissions=items,
    )


@router.post(
    "/{agent_id}/permissions",
    response_model=AgentPermissionsResponse,
    status_code=201,
    summary="Grant a permission to an agent (Super Admin only)",
)
async def grant_agent_permission(
    agent_id: uuid.UUID,
    body: GrantPermissionRequest,
    request: Request,
    staff: StaffUser = Depends(get_current_staff),
    db: AsyncSession = Depends(get_db),
) -> AgentPermissionsResponse:
    """
    Grant a permission to an agent.

    Security rules:
    - Super Admin only.
    - Agents cannot grant permissions to themselves or others.
    - Permissions in ADMIN_ONLY_PERMISSIONS can NEVER be granted to agents.
      This includes all financial permissions, audit.view, settings.manage,
      scheduler.trigger/view, agent.create/manage/manage_performance, etc.
    - The operation is idempotent: granting an already-held permission is safe.
    """
    from app.core.permissions import ADMIN_ONLY_PERMISSIONS

    if staff.role != StaffRole.super_admin:
        raise AppError("permission_denied", "Only Super Admin can grant permissions.", 403)

    # Verify agent exists and is an agent (not super_admin)
    agent_result = await db.execute(
        select(StaffUser).where(StaffUser.id == agent_id)
    )
    agent_obj: StaffUser | None = agent_result.scalar_one_or_none()
    if agent_obj is None:
        raise AppError("not_found", "Agent not found.", 404)

    if agent_obj.role == StaffRole.super_admin:
        raise AppError(
            "invalid_operation",
            "Cannot explicitly grant permissions to Super Admin — they have all permissions.",
            422,
        )

    # Block granting admin-only permissions to agents
    perm_code = body.permission_code.strip().lower()
    if perm_code in ADMIN_ONLY_PERMISSIONS:
        raise AppError(
            "forbidden_permission",
            f"Permission '{perm_code}' is restricted to Super Admin and cannot be granted to agents.",
            403,
        )

    # Verify the permission code exists in DB
    perm_row_result = await db.execute(
        select(PermModel).where(PermModel.code == perm_code)
    )
    perm_row: PermModel | None = perm_row_result.scalar_one_or_none()
    if perm_row is None:
        raise AppError("not_found", f"Permission code '{perm_code}' does not exist.", 404)

    # Check if already granted (idempotent)
    existing_result = await db.execute(
        select(StaffUserPermission).where(
            StaffUserPermission.staff_user_id == agent_id,
            StaffUserPermission.permission_id == perm_row.id,
        )
    )
    existing = existing_result.scalar_one_or_none()

    if existing is None:
        grant = StaffUserPermission(
            staff_user_id=agent_id,
            permission_id=perm_row.id,
            granted_by_id=staff.id,
        )
        db.add(grant)
        await db.flush([grant])

        from app.services import audit_service as _audit
        from app.models.audit_log import ActorType
        await _audit.log(
            db=db,
            actor_type=ActorType.staff,
            actor_id=str(staff.id),
            action="agent.permission_granted",
            entity_type="staff_user",
            entity_id=str(agent_id),
            new_value={"permission_code": perm_code, "granted_by": staff.agent_id},
            ip_address=_ip(request),
            user_agent=request.headers.get("user-agent"),
        )
        await db.commit()

    # Return full updated permission list
    return await list_agent_permissions(agent_id=agent_id, staff=staff, db=db)


@router.delete(
    "/{agent_id}/permissions/{permission_code}",
    response_model=RevokePermissionResponse,
    summary="Revoke a permission from an agent (Super Admin only)",
)
async def revoke_agent_permission(
    agent_id: uuid.UUID,
    permission_code: str,
    request: Request,
    staff: StaffUser = Depends(get_current_staff),
    db: AsyncSession = Depends(get_db),
) -> RevokePermissionResponse:
    """
    Revoke a specific permission from an agent.
    Idempotent — revoking a permission the agent does not have is a no-op (200).
    Super Admin only.
    """
    if staff.role != StaffRole.super_admin:
        raise AppError("permission_denied", "Only Super Admin can revoke permissions.", 403)

    # Verify agent exists
    agent_result = await db.execute(
        select(StaffUser).where(StaffUser.id == agent_id)
    )
    agent_obj: StaffUser | None = agent_result.scalar_one_or_none()
    if agent_obj is None:
        raise AppError("not_found", "Agent not found.", 404)

    perm_code = permission_code.strip().lower()
    perm_row_result = await db.execute(
        select(PermModel).where(PermModel.code == perm_code)
    )
    perm_row: PermModel | None = perm_row_result.scalar_one_or_none()
    if perm_row is None:
        raise AppError("not_found", f"Permission code '{perm_code}' does not exist.", 404)

    # Delete grant if it exists
    existing_result = await db.execute(
        select(StaffUserPermission).where(
            StaffUserPermission.staff_user_id == agent_id,
            StaffUserPermission.permission_id == perm_row.id,
        )
    )
    existing = existing_result.scalar_one_or_none()

    if existing is not None:
        await db.delete(existing)

        from app.services import audit_service as _audit
        from app.models.audit_log import ActorType
        await _audit.log(
            db=db,
            actor_type=ActorType.staff,
            actor_id=str(staff.id),
            action="agent.permission_revoked",
            entity_type="staff_user",
            entity_id=str(agent_id),
            old_value={"permission_code": perm_code},
            new_value={"revoked_by": staff.agent_id},
            ip_address=_ip(request),
            user_agent=request.headers.get("user-agent"),
        )
        await db.commit()

    return RevokePermissionResponse(
        message=f"Permission '{perm_code}' revoked." if existing else f"Agent did not have permission '{perm_code}'.",
        agent_id=agent_obj.agent_id,
        permission_code=perm_code,
    )


# ── Agent Tournament History (Super Admin only) ───────────────────────────────

@router.get(
    "/{agent_id}/tournaments",
    summary="List tournaments created by a specific agent (Super Admin only)",
)
async def get_agent_tournament_history(
    agent_id: uuid.UUID,
    staff: StaffUser = Depends(get_current_staff),
    db: AsyncSession = Depends(get_db),
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    status: Annotated[Optional[str], Query(description="Filter by tournament status")] = None,
    search: Annotated[Optional[str], Query(description="Search by tournament code or title (case-insensitive substring)")] = None,
) -> dict:
    """
    Return a paginated list of tournaments created by the specified agent,
    scoped strictly to tournament.created_by_id == agent_id.

    Security:
    - Super Admin only. Agents cannot call this endpoint (403).
    - An agent cannot use this to inspect another agent's tournaments —
      the backend enforces the Super Admin role check unconditionally.
    - Uses existing Tournament.created_by_id ownership field (no new fields).

    Returns the same shape used throughout the admin tournament API:
      { items: [...], total: N, limit: N, offset: N }
    """
    # Super Admin only — agents must never enumerate another agent's tournaments
    if staff.role != StaffRole.super_admin:
        raise PermissionDeniedError("super_admin_only")

    # Verify target agent exists
    agent_row = await db.execute(select(StaffUser).where(StaffUser.id == agent_id))
    agent_obj: StaffUser | None = agent_row.scalar_one_or_none()
    if agent_obj is None:
        raise AppError("not_found", "Agent not found.", 404)

    from sqlalchemy import func
    from app.models.tournament import Tournament, TournamentStatus

    # Base query scoped to this agent's created tournaments
    q = select(Tournament).where(Tournament.created_by_id == agent_id)

    # Optional status filter
    if status:
        try:
            q = q.where(Tournament.status == TournamentStatus(status))
        except ValueError:
            pass  # Ignore unknown status values — return all

    # Optional search by code or title
    if search:
        pattern = f"%{search.strip()}%"
        q = q.where(
            Tournament.tournament_code.ilike(pattern)
            | Tournament.title.ilike(pattern)
        )

    # Total count
    count_q = select(func.count()).select_from(q.subquery())
    total: int = (await db.execute(count_q)).scalar_one()

    # Paginated results — newest scheduled first
    q = q.order_by(Tournament.scheduled_at.desc()).limit(limit).offset(offset)
    tournaments = (await db.execute(q)).scalars().all()

    items = [
        {
            "id": str(t.id),
            "tournament_code": t.tournament_code,
            "title": t.title,
            "entry_fee": str(t.entry_fee),
            "status": t.status.value,
            "player_count": t.player_count,
            "max_players": t.max_players,
            "scheduled_at": t.scheduled_at.isoformat(),
            "completed_at": t.completed_at.isoformat() if t.completed_at else None,
            "cancelled_at": t.cancelled_at.isoformat() if t.cancelled_at else None,
            "created_by_display_name": t.created_by_display_name,
            "created_by_role": t.created_by_role,
        }
        for t in tournaments
    ]

    return {"items": items, "total": total, "limit": limit, "offset": offset}
