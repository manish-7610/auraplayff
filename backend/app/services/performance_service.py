"""
Performance service — Phase 6.

Core responsibilities
--------------------
1. refresh_daily_performance()
   - Count completed tournaments per agent per tier for a given date.
   - Upsert AgentDailyPerformance rows.
   - Compute shortfall, extra_count, extra_bonus, deduction_amount.
   - Update status (met / exceeded / pending).

2. grant_grace()
   - Mark a daily performance record as excused (grace).
   - Create AgentGrace record.
   - Zero deduction_amount on the daily performance row.
   - Audit.

3. issue_warning()
   - Create AgentWarning record linked to a daily performance row.
   - Set is_warned=True on the performance row.
   - Increment warning_number (total lifetime warnings for that agent).
   - Audit.

4. excuse_day()
   - Set is_excused=True on a daily performance row.
   - Set status=DailyPerfStatus.excused.
   - Zero deduction_amount.
   - Audit.

5. get_today_performance()   — agent self-service
6. get_month_performance()   — monthly rollup (agent self-service + admin)
7. list_daily_performances() — admin paginated list

Financial safety rules
----------------------
- No wallet mutations happen here. Salary is an internal payroll record.
- completed_count is derived from Tournament records with:
    created_by_id == agent.id
    entry_fee == tier.entry_fee
    status == 'completed'
    completed_at::date == target date
  Never manually entered by agents.
- Deduction formula: shortfall × deduction_per_missing (or extra_bonus_per_tournament if null).
- Extra bonus formula: extra_count × extra_bonus_per_tournament.
- All amounts are Decimal(12,2). No floats.
"""
from __future__ import annotations

import logging
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_DOWN
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppError
from app.models._types import utcnow
from app.models.agent_performance import (
    AgentDailyPerformance,
    AgentGrace,
    AgentWarning,
    DailyPerfStatus,
)
from app.models.agent_target_config import AgentTargetConfig
from app.models.audit_log import ActorType
from app.models.staff_user import StaffRole, StaffUser
from app.models.tournament import Tournament, TournamentStatus
from app.services import audit_service

logger = logging.getLogger("auraplayff.performance")

_CENTS = Decimal("0.01")


def _q(v: Decimal) -> Decimal:
    return v.quantize(_CENTS, rounding=ROUND_DOWN)


# ── Custom exceptions ─────────────────────────────────────────────────────────

class DailyPerfNotFoundError(AppError):
    def __init__(self) -> None:
        super().__init__("daily_perf_not_found", "Daily performance record not found.", 404)


class GraceAlreadyGrantedError(AppError):
    def __init__(self) -> None:
        super().__init__(
            "grace_already_granted",
            "Grace has already been granted for this daily performance record.",
            409,
        )


class DayAlreadyExcusedError(AppError):
    def __init__(self) -> None:
        super().__init__(
            "day_already_excused",
            "This day is already excused.",
            409,
        )


# ── Target config helpers ─────────────────────────────────────────────────────

async def get_all_active_tier_configs(
    db: AsyncSession,
) -> list[AgentTargetConfig]:
    """Return all active tier configs (entry_fee IS NOT NULL, is_active=True)."""
    result = await db.execute(
        select(AgentTargetConfig)
        .where(
            AgentTargetConfig.entry_fee.is_not(None),
            AgentTargetConfig.is_active == True,  # noqa: E712
        )
        .order_by(AgentTargetConfig.entry_fee)
    )
    return list(result.scalars().all())


async def get_tier_config(
    db: AsyncSession, entry_fee: Decimal
) -> AgentTargetConfig | None:
    """Fetch config for a specific entry_fee tier. Returns None if not found."""
    result = await db.execute(
        select(AgentTargetConfig).where(
            AgentTargetConfig.entry_fee == entry_fee,
            AgentTargetConfig.is_active == True,  # noqa: E712
        )
    )
    return result.scalar_one_or_none()


async def get_base_salary(db: AsyncSession) -> Decimal:
    """Return base monthly salary from config. Falls back to ₹10,000."""
    result = await db.execute(
        select(AgentTargetConfig.base_monthly_salary).where(
            AgentTargetConfig.entry_fee.is_(None)
        )
    )
    row = result.scalar_one_or_none()
    if row and row > Decimal("0"):
        return _q(row)
    return Decimal("10000.00")


# ── Tournament count helpers ──────────────────────────────────────────────────

async def count_completed_tournaments(
    db: AsyncSession,
    agent_id: uuid.UUID,
    entry_fee: Decimal,
    target_date: date,
) -> int:
    """
    Count tournaments created by this agent, with this entry_fee,
    that reached COMPLETED status and whose completed_at date = target_date.

    This is the authoritative source for completed_count.
    Agents cannot manipulate this — it derives from Tournament records.
    """
    result = await db.execute(
        select(func.count(Tournament.id)).where(
            Tournament.created_by_id == agent_id,
            Tournament.entry_fee == entry_fee,
            Tournament.status == TournamentStatus.completed,
            func.date(Tournament.completed_at) == target_date,
        )
    )
    return result.scalar_one() or 0


# ── Core refresh ──────────────────────────────────────────────────────────────

async def refresh_daily_performance(
    db: AsyncSession,
    agent_id: uuid.UUID,
    target_date: date,
) -> list[AgentDailyPerformance]:
    """
    Compute and upsert AgentDailyPerformance rows for all active tiers
    for this agent on the given date.

    - Creates rows that don't exist yet.
    - Updates completed_count and derived fields on existing rows
      UNLESS the row is already in a terminal state (excused, grace, deducted, warned).
    - Does NOT overwrite admin-set status/grace/warning flags.

    Returns the updated list of performance rows.
    """
    tier_configs = await get_all_active_tier_configs(db)
    if not tier_configs:
        logger.warning("No active tier configs found — no performance rows created.")
        return []

    updated_rows: list[AgentDailyPerformance] = []

    for cfg in tier_configs:
        if cfg.entry_fee is None or cfg.daily_required is None:
            continue

        completed = await count_completed_tournaments(
            db, agent_id, cfg.entry_fee, target_date
        )
        required = cfg.daily_required
        shortfall = max(0, required - completed)
        extra = max(0, completed - required)

        # Bonus: extra_count × extra_bonus_per_tournament
        extra_bonus_rate = cfg.extra_bonus_per_tournament or Decimal("0")
        extra_bonus = _q(Decimal(extra) * extra_bonus_rate)

        # Deduction: shortfall × deduction_per_missing (or extra_bonus if null)
        deduction_rate = cfg.deduction_per_missing if cfg.deduction_per_missing is not None else extra_bonus_rate
        deduction = _q(Decimal(shortfall) * deduction_rate)

        # Determine status
        if completed >= required and extra > 0:
            status = DailyPerfStatus.exceeded
        elif completed >= required:
            status = DailyPerfStatus.met
        else:
            status = DailyPerfStatus.pending

        # Upsert: check if row exists
        existing = await db.execute(
            select(AgentDailyPerformance).where(
                AgentDailyPerformance.agent_id == agent_id,
                AgentDailyPerformance.perf_date == target_date,
                AgentDailyPerformance.entry_fee == cfg.entry_fee,
            )
        )
        perf: AgentDailyPerformance | None = existing.scalar_one_or_none()

        if perf is None:
            perf = AgentDailyPerformance(
                agent_id=agent_id,
                perf_date=target_date,
                entry_fee=cfg.entry_fee,
                required_count=required,
                completed_count=completed,
                shortfall=shortfall,
                extra_count=extra,
                extra_bonus=extra_bonus,
                deduction_amount=deduction,
                status=status,
            )
            db.add(perf)
        else:
            # Only refresh counts & derived fields; respect admin-set flags
            perf.completed_count = completed
            perf.shortfall = shortfall
            perf.extra_count = extra
            perf.extra_bonus = extra_bonus

            # Only update deduction if not already resolved by admin
            if perf.status not in (
                DailyPerfStatus.grace,
                DailyPerfStatus.excused,
            ):
                perf.deduction_amount = deduction
                perf.status = status

            perf.updated_at = utcnow()

        await db.flush([perf])
        updated_rows.append(perf)

    return updated_rows


# ── Grace ─────────────────────────────────────────────────────────────────────

async def grant_grace(
    db: AsyncSession,
    daily_performance_id: uuid.UUID,
    reason: str,
    *,
    granted_by_id: uuid.UUID,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> AgentGrace:
    """
    Grant grace for a missed target day.

    - Validates the performance row exists.
    - Checks grace not already granted.
    - Sets is_grace=True, status=DailyPerfStatus.grace, deduction_amount=0.
    - Creates AgentGrace record.
    - Audits.
    """
    perf = await _get_daily_perf(db, daily_performance_id)

    if perf.is_grace:
        raise GraceAlreadyGrantedError()

    # Zero out deduction — grace means no penalty
    perf.is_grace = True
    perf.status = DailyPerfStatus.grace
    perf.deduction_amount = Decimal("0.00")
    perf.reviewed_by_id = granted_by_id
    perf.updated_at = utcnow()

    grace = AgentGrace(
        agent_id=perf.agent_id,
        daily_performance_id=daily_performance_id,
        grace_date=perf.perf_date,
        reason=reason,
        granted_by_id=granted_by_id,
    )
    db.add(grace)
    await db.flush([perf, grace])

    await audit_service.log(
        db=db,
        actor_type=ActorType.staff,
        actor_id=str(granted_by_id),
        action="agent.grace_granted",
        entity_type="agent_daily_performance",
        entity_id=str(daily_performance_id),
        new_value={
            "agent_id": str(perf.agent_id),
            "perf_date": str(perf.perf_date),
            "entry_fee": str(perf.entry_fee),
            "reason": reason,
        },
        ip_address=ip_address,
        user_agent=user_agent,
    )
    return grace


# ── Warning ───────────────────────────────────────────────────────────────────

async def issue_warning(
    db: AsyncSession,
    agent_id: uuid.UUID,
    reason: str,
    *,
    issued_by_id: uuid.UUID,
    daily_performance_id: uuid.UUID | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> AgentWarning:
    """
    Issue a warning to an agent.

    - Counts existing warnings for this agent to set warning_number.
    - If daily_performance_id is provided, sets is_warned=True on that row.
    - Creates AgentWarning record.
    - Audits.

    Warnings are informational only — they do NOT deduct salary automatically.
    """
    # Count existing warnings
    count_result = await db.execute(
        select(func.count(AgentWarning.id)).where(
            AgentWarning.agent_id == agent_id
        )
    )
    existing_count: int = count_result.scalar_one() or 0
    warning_number = existing_count + 1

    # Link to daily performance if provided
    if daily_performance_id is not None:
        perf = await _get_daily_perf(db, daily_performance_id)
        perf.is_warned = True
        perf.updated_at = utcnow()
        await db.flush([perf])
        warning_date = perf.perf_date
    else:
        warning_date = datetime.now(timezone.utc).date()

    warning = AgentWarning(
        agent_id=agent_id,
        daily_performance_id=daily_performance_id,
        warning_date=warning_date,
        warning_number=warning_number,
        reason=reason,
        issued_by_id=issued_by_id,
    )
    db.add(warning)
    await db.flush([warning])

    await audit_service.log(
        db=db,
        actor_type=ActorType.staff,
        actor_id=str(issued_by_id),
        action="agent.warning_issued",
        entity_type="staff_user",
        entity_id=str(agent_id),
        new_value={
            "warning_number": warning_number,
            "warning_date": str(warning_date),
            "reason": reason,
        },
        ip_address=ip_address,
        user_agent=user_agent,
    )
    return warning


# ── Excuse day ────────────────────────────────────────────────────────────────

async def excuse_day(
    db: AsyncSession,
    daily_performance_id: uuid.UUID,
    note: str,
    *,
    admin_id: uuid.UUID,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> AgentDailyPerformance:
    """
    Mark a daily performance day as excused.
    Zeroes deduction_amount. Does not create a grace record.
    """
    perf = await _get_daily_perf(db, daily_performance_id)

    if perf.is_excused:
        raise DayAlreadyExcusedError()

    perf.is_excused = True
    perf.status = DailyPerfStatus.excused
    perf.deduction_amount = Decimal("0.00")
    perf.admin_note = note
    perf.reviewed_by_id = admin_id
    perf.updated_at = utcnow()
    await db.flush([perf])

    await audit_service.log(
        db=db,
        actor_type=ActorType.staff,
        actor_id=str(admin_id),
        action="agent.day_excused",
        entity_type="agent_daily_performance",
        entity_id=str(daily_performance_id),
        new_value={
            "agent_id": str(perf.agent_id),
            "perf_date": str(perf.perf_date),
            "note": note,
        },
        ip_address=ip_address,
        user_agent=user_agent,
    )
    return perf


# ── Query helpers ─────────────────────────────────────────────────────────────

async def get_daily_performance_rows(
    db: AsyncSession,
    agent_id: uuid.UUID,
    target_date: date,
) -> list[AgentDailyPerformance]:
    """Return all daily performance rows for an agent on a given date."""
    result = await db.execute(
        select(AgentDailyPerformance).where(
            AgentDailyPerformance.agent_id == agent_id,
            AgentDailyPerformance.perf_date == target_date,
        ).order_by(AgentDailyPerformance.entry_fee)
    )
    return list(result.scalars().all())


async def get_month_performance_rows(
    db: AsyncSession,
    agent_id: uuid.UUID,
    year: int,
    month: int,
) -> list[AgentDailyPerformance]:
    """Return all daily performance rows for an agent within a given month."""
    from sqlalchemy import extract
    result = await db.execute(
        select(AgentDailyPerformance).where(
            AgentDailyPerformance.agent_id == agent_id,
            extract("year", AgentDailyPerformance.perf_date) == year,
            extract("month", AgentDailyPerformance.perf_date) == month,
        ).order_by(AgentDailyPerformance.perf_date, AgentDailyPerformance.entry_fee)
    )
    return list(result.scalars().all())


async def list_daily_performances(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID | None = None,
    perf_date: date | None = None,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[AgentDailyPerformance], int]:
    """Admin: list daily performance records with optional filters."""
    q = select(AgentDailyPerformance)
    if agent_id is not None:
        q = q.where(AgentDailyPerformance.agent_id == agent_id)
    if perf_date is not None:
        q = q.where(AgentDailyPerformance.perf_date == perf_date)
    if status is not None:
        q = q.where(AgentDailyPerformance.status == status)

    count_result = await db.execute(select(func.count()).select_from(q.subquery()))
    total = count_result.scalar_one()

    rows = await db.execute(
        q.order_by(
            AgentDailyPerformance.perf_date.desc(),
            AgentDailyPerformance.entry_fee,
        ).limit(limit).offset(offset)
    )
    return list(rows.scalars().all()), total


async def list_graces(
    db: AsyncSession,
    agent_id: uuid.UUID | None = None,
) -> tuple[list[AgentGrace], int]:
    q = select(AgentGrace)
    if agent_id is not None:
        q = q.where(AgentGrace.agent_id == agent_id)
    count_result = await db.execute(select(func.count()).select_from(q.subquery()))
    total = count_result.scalar_one()
    rows = await db.execute(q.order_by(AgentGrace.grace_date.desc()))
    return list(rows.scalars().all()), total


async def list_warnings(
    db: AsyncSession,
    agent_id: uuid.UUID | None = None,
) -> tuple[list[AgentWarning], int]:
    q = select(AgentWarning)
    if agent_id is not None:
        q = q.where(AgentWarning.agent_id == agent_id)
    count_result = await db.execute(select(func.count()).select_from(q.subquery()))
    total = count_result.scalar_one()
    rows = await db.execute(q.order_by(AgentWarning.warning_date.desc()))
    return list(rows.scalars().all()), total


# ── Private helpers ───────────────────────────────────────────────────────────

async def _get_daily_perf(
    db: AsyncSession,
    daily_performance_id: uuid.UUID,
) -> AgentDailyPerformance:
    result = await db.execute(
        select(AgentDailyPerformance).where(
            AgentDailyPerformance.id == daily_performance_id
        )
    )
    perf = result.scalar_one_or_none()
    if perf is None:
        raise DailyPerfNotFoundError()
    return perf
