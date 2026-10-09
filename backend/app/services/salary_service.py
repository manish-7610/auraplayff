"""
Salary service — deterministic monthly salary calculation — Phase 6.

Formula
-------
  final_salary = base_salary
               + total_extra_bonus     (sum of all daily extra_bonus for the month)
               - total_deductions      (sum of all daily deduction_amount for the month)
               + manual_adjustment     (admin-entered +/-)

  final_salary is floored at ₹0 (never negative).

Rules
-----
- Salary records are INTERNAL payroll. No wallet mutations.
- Amounts are all Decimal(12,2). No floats anywhere.
- The record is recomputed on demand (recalculate_salary_record) whenever
  new daily performance data is refreshed.
- Manual adjustments persist across recalculations.
- Confirming a salary record locks final_salary and status.

Public API
----------
get_or_create_salary_record()     — fetch or initialise a monthly record
recalculate_salary_record()       — recompute totals from daily perf rows
apply_manual_adjustment()         — admin +/- adjustment
confirm_salary_record()           — lock the record (Super Admin only)
get_salary_record()               — fetch existing record
list_salary_records()             — paginated admin list
"""
from __future__ import annotations

import logging
import uuid
from decimal import Decimal, ROUND_DOWN

from sqlalchemy import extract, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppError
from app.models._types import utcnow
from app.models.agent_performance import AgentDailyPerformance, AgentSalaryRecord, SalaryRecordStatus
from app.models.audit_log import ActorType
from app.services import audit_service
from app.services.performance_service import get_base_salary

logger = logging.getLogger("auraplayff.salary")

_CENTS = Decimal("0.01")


def _q(v: Decimal) -> Decimal:
    return v.quantize(_CENTS, rounding=ROUND_DOWN)


def _floor_zero(v: Decimal) -> Decimal:
    return max(v, Decimal("0.00"))


# ── Custom exceptions ─────────────────────────────────────────────────────────

class SalaryRecordNotFoundError(AppError):
    def __init__(self) -> None:
        super().__init__("salary_record_not_found", "Salary record not found.", 404)


class SalaryAlreadyConfirmedError(AppError):
    def __init__(self) -> None:
        super().__init__(
            "salary_already_confirmed",
            "This salary record is already confirmed and cannot be modified.",
            409,
        )


# ── Core functions ────────────────────────────────────────────────────────────

async def get_or_create_salary_record(
    db: AsyncSession,
    agent_id: uuid.UUID,
    year: int,
    month: int,
) -> AgentSalaryRecord:
    """
    Fetch the salary record for (agent, year, month).
    Creates a new draft record if one doesn't exist yet.
    """
    result = await db.execute(
        select(AgentSalaryRecord).where(
            AgentSalaryRecord.agent_id == agent_id,
            AgentSalaryRecord.year == year,
            AgentSalaryRecord.month == month,
        )
    )
    record = result.scalar_one_or_none()

    if record is None:
        base_salary = await get_base_salary(db)
        record = AgentSalaryRecord(
            agent_id=agent_id,
            year=year,
            month=month,
            base_salary=base_salary,
            status=SalaryRecordStatus.draft,
        )
        db.add(record)
        await db.flush([record])

    return record


async def recalculate_salary_record(
    db: AsyncSession,
    agent_id: uuid.UUID,
    year: int,
    month: int,
) -> AgentSalaryRecord:
    """
    Recompute the salary record by summing all daily performance rows
    for the given (agent, year, month).

    Does NOT overwrite manual_adjustment — that persists across recalculations.
    Does NOT modify a confirmed record (status != draft).

    Returns the updated record.
    """
    record = await get_or_create_salary_record(db, agent_id, year, month)

    if record.status != SalaryRecordStatus.draft:
        # Already confirmed or paid — do not recalculate
        logger.debug(
            "Skipping recalculation for confirmed salary record: "
            "agent=%s year=%d month=%d", agent_id, year, month
        )
        return record

    # Sum daily bonus and deduction from AgentDailyPerformance rows
    agg = await db.execute(
        select(
            func.coalesce(func.sum(AgentDailyPerformance.extra_bonus), 0).label("total_bonus"),
            func.coalesce(func.sum(AgentDailyPerformance.deduction_amount), 0).label("total_ded"),
        ).where(
            AgentDailyPerformance.agent_id == agent_id,
            extract("year", AgentDailyPerformance.perf_date) == year,
            extract("month", AgentDailyPerformance.perf_date) == month,
        )
    )
    row = agg.one()
    total_bonus = _q(Decimal(str(row.total_bonus)))
    total_deductions = _q(Decimal(str(row.total_ded)))

    # Recompute base salary from config each time (in case config changed)
    base_salary = await get_base_salary(db)

    final = _floor_zero(
        _q(base_salary + total_bonus - total_deductions + record.manual_adjustment)
    )

    record.base_salary = base_salary
    record.total_extra_bonus = total_bonus
    record.total_deductions = total_deductions
    record.final_salary = final
    record.updated_at = utcnow()
    await db.flush([record])

    return record


async def apply_manual_adjustment(
    db: AsyncSession,
    agent_id: uuid.UUID,
    year: int,
    month: int,
    adjustment: Decimal,
    reason: str,
    *,
    admin_id: uuid.UUID,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> AgentSalaryRecord:
    """
    Apply a manual salary adjustment (positive = bonus, negative = deduction).
    Then recalculates final_salary.
    Raises SalaryAlreadyConfirmedError if the record is confirmed/paid.
    """
    record = await get_or_create_salary_record(db, agent_id, year, month)

    if record.status != SalaryRecordStatus.draft:
        raise SalaryAlreadyConfirmedError()

    old_adjustment = record.manual_adjustment

    record.manual_adjustment = _q(adjustment)
    record.manual_adjustment_reason = reason
    record.updated_at = utcnow()
    await db.flush([record])

    # Recalculate with new adjustment
    record = await recalculate_salary_record(db, agent_id, year, month)

    await audit_service.log(
        db=db,
        actor_type=ActorType.staff,
        actor_id=str(admin_id),
        action="agent.salary_adjusted",
        entity_type="agent_salary_record",
        entity_id=str(record.id),
        old_value={"manual_adjustment": str(old_adjustment)},
        new_value={
            "manual_adjustment": str(adjustment),
            "reason": reason,
            "final_salary": str(record.final_salary),
        },
        ip_address=ip_address,
        user_agent=user_agent,
    )
    return record


async def confirm_salary_record(
    db: AsyncSession,
    agent_id: uuid.UUID,
    year: int,
    month: int,
    *,
    admin_id: uuid.UUID,
    notes: str | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> AgentSalaryRecord:
    """
    Confirm a monthly salary record.
    - Recalculates final_salary one last time.
    - Sets status = confirmed.
    - Records confirmed_by_id and confirmed_at.
    - Audits.

    Raises SalaryAlreadyConfirmedError if already confirmed/paid.
    """
    record = await get_or_create_salary_record(db, agent_id, year, month)

    if record.status != SalaryRecordStatus.draft:
        raise SalaryAlreadyConfirmedError()

    # Final recalculation before confirming
    record = await recalculate_salary_record(db, agent_id, year, month)

    record.status = SalaryRecordStatus.confirmed
    record.confirmed_by_id = admin_id
    record.confirmed_at = utcnow()
    record.notes = notes
    record.updated_at = utcnow()
    await db.flush([record])

    await audit_service.log(
        db=db,
        actor_type=ActorType.staff,
        actor_id=str(admin_id),
        action="agent.salary_confirmed",
        entity_type="agent_salary_record",
        entity_id=str(record.id),
        new_value={
            "year": year,
            "month": month,
            "agent_id": str(agent_id),
            "final_salary": str(record.final_salary),
            "status": "confirmed",
        },
        ip_address=ip_address,
        user_agent=user_agent,
    )
    logger.info(
        "Salary confirmed: agent=%s year=%d month=%d final=%.2f",
        agent_id, year, month, record.final_salary,
    )
    return record


# ── Query helpers ─────────────────────────────────────────────────────────────

async def get_salary_record(
    db: AsyncSession,
    agent_id: uuid.UUID,
    year: int,
    month: int,
) -> AgentSalaryRecord | None:
    """Fetch a salary record without creating it."""
    result = await db.execute(
        select(AgentSalaryRecord).where(
            AgentSalaryRecord.agent_id == agent_id,
            AgentSalaryRecord.year == year,
            AgentSalaryRecord.month == month,
        )
    )
    return result.scalar_one_or_none()


async def list_salary_records(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID | None = None,
    year: int | None = None,
    month: int | None = None,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[AgentSalaryRecord], int]:
    """Admin: list salary records with optional filters."""
    q = select(AgentSalaryRecord)
    if agent_id is not None:
        q = q.where(AgentSalaryRecord.agent_id == agent_id)
    if year is not None:
        q = q.where(AgentSalaryRecord.year == year)
    if month is not None:
        q = q.where(AgentSalaryRecord.month == month)
    if status is not None:
        q = q.where(AgentSalaryRecord.status == status)

    count_result = await db.execute(select(func.count()).select_from(q.subquery()))
    total = count_result.scalar_one()
    rows = await db.execute(
        q.order_by(
            AgentSalaryRecord.year.desc(),
            AgentSalaryRecord.month.desc(),
        ).limit(limit).offset(offset)
    )
    return list(rows.scalars().all()), total
