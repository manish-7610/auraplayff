"""
Agent performance refresh + month-end salary snapshot jobs — Phase 7.

Job 1 — run_daily_performance_refresh()
    Called daily (IST midnight + buffer).
    Calls performance_service.refresh_daily_performance() for every active
    agent for the current IST calendar date.
    Does NOT trust manually entered counts — derives from Tournament records.
    Idempotent: refresh_daily_performance() uses upsert logic; running twice
    for the same date produces the same result.

    After refreshing, emits per-agent notifications:
      - Target met / exceeded → AGENT_TARGET_COMPLETED / AGENT_EXTRA_BONUS_EARNED
      - Target missed (shortfall > 0) → AGENT_DAILY_TARGET_STATUS

Job 2 — run_month_end_salary_snapshot()
    Called on the 1st of every month (IST).
    Processes the *previous* calendar month for all active agents.
    Calls salary_service.recalculate_salary_record() which is idempotent —
    confirmed/paid records are skipped automatically.
    Does NOT automatically confirm or pay salary.
    Emits AGENT_SALARY_CALCULATED notifications.

IST / timezone
--------------
Business day is determined in IST (UTC+5:30).
All timestamps stored in DB are UTC.
"""
from __future__ import annotations

import logging
import uuid
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_performance import AgentDailyPerformance, DailyPerfStatus
from app.models.staff_user import StaffRole, StaffUser
from app.services import notification_service as ns
from app.services.performance_service import refresh_daily_performance
from app.services.salary_service import recalculate_salary_record, get_or_create_salary_record

logger = logging.getLogger("auraplayff.jobs.agent_performance")

# IST = UTC + 5h30m
_IST = timezone(timedelta(hours=5, minutes=30))


def _today_ist() -> date:
    """Return the current calendar date in IST."""
    return datetime.now(_IST).date()


def _prev_month_ist() -> tuple[int, int]:
    """Return (year, month) for the previous IST calendar month."""
    today = datetime.now(_IST).date()
    first_of_this_month = today.replace(day=1)
    last_of_prev_month = first_of_this_month - timedelta(days=1)
    return last_of_prev_month.year, last_of_prev_month.month


async def _get_active_agents(db: AsyncSession) -> list[StaffUser]:
    """Return all active agents (excluding Super Admins)."""
    result = await db.execute(
        select(StaffUser).where(
            StaffUser.role == StaffRole.agent,
            StaffUser.is_active == True,  # noqa: E712
        )
    )
    return list(result.scalars().all())


# ── Job 1: Daily performance refresh ─────────────────────────────────────────

async def run_daily_performance_refresh(db: AsyncSession) -> int:
    """
    Refresh AgentDailyPerformance for every active agent for today (IST).

    Returns the number of agents processed.

    Safety:
    - refresh_daily_performance() is idempotent — safe to run multiple times.
    - Admin-set grace/warning/excused flags are never overwritten.
    - completed_count is always derived from Tournament records, never
      from manually entered values.
    """
    target_date = _today_ist()
    agents = await _get_active_agents(db)

    if not agents:
        logger.info("No active agents found — daily performance refresh skipped.")
        return 0

    processed = 0
    for agent in agents:
        # Capture scalar values before flush-heavy operations that may expire
        # the StaffUser instance inside the same session transaction.
        agent_id_val = agent.id
        agent_agent_id_val = agent.agent_id
        try:
            perf_rows = await refresh_daily_performance(
                db, agent_id=agent_id_val, target_date=target_date
            )
            await _emit_performance_notifications(db, agent_id_val, perf_rows, target_date)
            processed += 1
        except Exception:
            logger.exception(
                "Performance refresh failed for agent %s (%s) on %s",
                agent_agent_id_val, agent_id_val, target_date,
            )

    logger.info(
        "Daily performance refresh complete: %d/%d agents processed for %s",
        processed, len(agents), target_date,
    )
    return processed


async def _emit_performance_notifications(
    db: AsyncSession,
    agent_id: uuid.UUID,
    perf_rows: list[AgentDailyPerformance],
    target_date: date,
) -> None:
    """
    Emit notifications based on today's performance rows.
    Best-effort — never raises.

    Accepts agent_id as a plain UUID (not the ORM instance) to avoid
    triggering a lazy reload on a potentially-expired StaffUser after flush.
    """
    try:
        date_str = target_date.isoformat()
        for row in perf_rows:
            if row.status in (DailyPerfStatus.exceeded,):
                # Extra bonus earned
                if row.extra_count > 0 and row.extra_bonus > 0:
                    await ns.notify_agent_extra_bonus(
                        db,
                        staff_user_id=agent_id,
                        perf_date=date_str,
                        extra_count=row.extra_count,
                        extra_bonus=str(row.extra_bonus),
                    )
            elif row.status == DailyPerfStatus.met:
                await ns.notify_agent_daily_target_status(
                    db,
                    staff_user_id=agent_id,
                    perf_date=date_str,
                    completed=row.completed_count,
                    required=row.required_count,
                    shortfall=0,
                )
            elif row.status == DailyPerfStatus.pending and row.shortfall > 0:
                # Target not yet met — informational
                await ns.notify_agent_daily_target_status(
                    db,
                    staff_user_id=agent_id,
                    perf_date=date_str,
                    completed=row.completed_count,
                    required=row.required_count,
                    shortfall=row.shortfall,
                )
    except Exception:
        logger.exception(
            "Failed to emit performance notifications for agent %s on %s",
            agent_id, target_date,
        )


# ── Job 2: Month-end salary snapshot ─────────────────────────────────────────

async def run_month_end_salary_snapshot(db: AsyncSession) -> int:
    """
    Recalculate salary records for the previous IST calendar month for
    every active agent.

    Runs on the 1st of each month.  Idempotent:
    - recalculate_salary_record() skips confirmed/paid records.
    - get_or_create_salary_record() uses UNIQUE (agent_id, year, month)
      so there are never duplicate records.

    Does NOT:
    - automatically confirm salary
    - pay salary
    - touch player wallets

    Returns the number of agents processed.
    """
    year, month = _prev_month_ist()
    agents = await _get_active_agents(db)

    if not agents:
        logger.info("No active agents — month-end salary snapshot skipped.")
        return 0

    processed = 0
    for agent in agents:
        # Pre-capture scalar values before flush-heavy service calls that may
        # expire the StaffUser ORM instance within the same session transaction.
        # Accessing agent.agent_id AFTER recalculate_salary_record() (which does
        # internal flushes) can trigger a MissingGreenlet / DetachedInstanceError.
        agent_id_val       = agent.id
        agent_agent_id_val = agent.agent_id
        try:
            record = await recalculate_salary_record(
                db, agent_id=agent_id_val, year=year, month=month
            )
            # Notify agent that their salary has been calculated
            await ns.notify_agent_salary_calculated(
                db,
                staff_user_id=agent_id_val,
                salary_record_id=record.id,
                year=year,
                month=month,
                final_salary=str(record.final_salary),
            )
            processed += 1
            logger.info(
                "Salary snapshot: agent=%s year=%d month=%d final=%.2f",
                agent_agent_id_val, year, month, record.final_salary,
            )
        except Exception:
            logger.exception(
                "Salary snapshot failed for agent %s year=%d month=%d",
                agent_agent_id_val, year, month,
            )

    logger.info(
        "Month-end salary snapshot complete: %d/%d agents for %d/%02d",
        processed, len(agents), year, month,
    )
    return processed
