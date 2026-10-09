"""
Scheduler service — Phase 7.

Provides:
  - run_job()            context-manager that wraps any async job function,
                         writes JobExecutionLog rows (running → success/failed/skipped),
                         and reports errors via admin notifications.

  - trigger_job()        programmatic manual trigger from Super Admin API.

  - get_job_status()     fetch latest execution records for the admin dashboard.

  - list_job_executions() paginated history for a single job name.

Job execution lifecycle
-----------------------
  1. INSERT log row with status=running.
  2. Await the wrapped coroutine — it returns an int (records_affected).
  3. UPDATE the row with status=success, duration_ms, records_affected.
  4. On exception: UPDATE with status=failed, error_summary. Then attempt
     to notify admins of the error (best-effort — never re-raises).

Idempotency
-----------
  execution_id is a UUID string generated fresh per run.  The UNIQUE
  constraint on job_execution_logs.execution_id prevents any collision.
  If the application is killed mid-run, the row stays in status=running
  indefinitely — a watchdog could flag stale "running" rows as failed.

Security
--------
  error_summary is truncated to 2 000 characters before storage.
  No secrets, tokens, or wallet values are written here.
"""
from __future__ import annotations

import logging
import time
import traceback
import uuid
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Awaitable, Callable

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session_factory
from app.models._types import utcnow
from app.models.job_execution_log import JobExecutionLog, JobStatus
from app.services import notification_service as ns

logger = logging.getLogger("auraplayff.scheduler_service")

# Max chars stored in error_summary
_ERROR_SUMMARY_MAX = 2_000


# ── Core job runner ───────────────────────────────────────────────────────────

@asynccontextmanager
async def _job_session() -> AsyncGenerator[AsyncSession, None]:
    """Yield a fresh DB session for a job run (separate from request scope)."""
    factory = get_session_factory()
    async with factory() as db:
        yield db


async def run_job(
    job_name: str,
    coro_fn: Callable[[AsyncSession], Awaitable[int]],
    *,
    meta: dict | None = None,
) -> None:
    """
    Execute *coro_fn* with its own DB session, wrapped in execution logging.

    Parameters
    ----------
    job_name : str
        Stable identifier, e.g. ``"tournament_auto_transition"``.
    coro_fn  : async (db: AsyncSession) -> int
        The job coroutine.  Must return the count of records affected.
    meta     : dict | None
        Optional extra data written to the log row.

    The entire job (including flush of the log row) is run inside the same
    session, committed in two phases:
      Phase A — commit the "running" row so it appears immediately.
      Phase B — commit the "success/failed" update.
    """
    execution_id = str(uuid.uuid4())
    start_ns = time.monotonic_ns()

    # ── Phase A: write "running" row ──────────────────────────────────────────
    log_row_id: uuid.UUID | None = None
    try:
        async with _job_session() as db:
            async with db.begin():
                log_row = JobExecutionLog(
                    job_name=job_name,
                    execution_id=execution_id,
                    status=JobStatus.running,
                    meta=meta,
                )
                db.add(log_row)
                await db.flush([log_row])
                log_row_id = log_row.id
    except Exception:
        logger.exception("Could not write running log row for job %s — proceeding anyway", job_name)

    # ── Phase B: execute the job ──────────────────────────────────────────────
    records_affected: int = 0
    status = JobStatus.success
    error_summary: str | None = None

    try:
        async with _job_session() as db:
            async with db.begin():
                records_affected = await coro_fn(db)
    except Exception as exc:
        status = JobStatus.failed
        error_summary = traceback.format_exc()[:_ERROR_SUMMARY_MAX]
        logger.exception("Job %s (exec=%s) FAILED: %s", job_name, execution_id, exc)

    duration_ms = (time.monotonic_ns() - start_ns) // 1_000_000

    # ── Phase C: update the log row ───────────────────────────────────────────
    try:
        async with _job_session() as db:
            async with db.begin():
                # Fetch by execution_id (unique) so we don't need the PK
                result = await db.execute(
                    select(JobExecutionLog).where(
                        JobExecutionLog.execution_id == execution_id
                    )
                )
                row = result.scalar_one_or_none()
                if row is not None:
                    row.status = status
                    row.completed_at = utcnow()
                    row.duration_ms = duration_ms
                    row.records_affected = records_affected
                    row.error_summary = error_summary
                else:
                    # Log row was never written — insert a complete record
                    db.add(
                        JobExecutionLog(
                            job_name=job_name,
                            execution_id=execution_id,
                            status=status,
                            completed_at=utcnow(),
                            duration_ms=duration_ms,
                            records_affected=records_affected,
                            error_summary=error_summary,
                            meta=meta,
                        )
                    )
    except Exception:
        logger.exception(
            "Could not update log row for job %s (exec=%s)", job_name, execution_id
        )

    # ── Phase D: notify admins on failure (best-effort) ───────────────────────
    if status == JobStatus.failed:
        try:
            async with _job_session() as db:
                async with db.begin():
                    await ns.notify_admin_system_error(
                        db,
                        job_name=job_name,
                        error_summary=(error_summary or "Unknown error")[:300],
                    )
        except Exception:
            logger.exception(
                "Could not send admin error notification for job %s", job_name
            )

    logger.info(
        "Job %s finished: status=%s records=%d duration=%dms exec=%s",
        job_name, status.value, records_affected, duration_ms, execution_id,
    )


# ── Manual trigger (Super Admin API) ─────────────────────────────────────────

# Registry of triggerable job functions keyed by job_name.
# Populated by app/scheduler.py at startup.
_JOB_REGISTRY: dict[str, Callable[[AsyncSession], Awaitable[int]]] = {}


def register_job(job_name: str, fn: Callable[[AsyncSession], Awaitable[int]]) -> None:
    """Register a job function so it can be triggered via the admin API."""
    _JOB_REGISTRY[job_name] = fn


def list_registered_jobs() -> list[str]:
    """Return the names of all registered jobs."""
    return sorted(_JOB_REGISTRY.keys())


async def trigger_job(job_name: str) -> None:
    """
    Manually trigger a registered job from the admin API.

    Raises KeyError if the job_name is not registered.
    The job runs asynchronously inside run_job() (same execution log pattern).
    """
    fn = _JOB_REGISTRY.get(job_name)
    if fn is None:
        raise KeyError(f"Unknown job: {job_name!r}")
    await run_job(job_name, fn, meta={"triggered_by": "admin_api"})


# ── Admin query helpers ───────────────────────────────────────────────────────

async def get_job_summary(db: AsyncSession) -> list[dict]:
    """
    Return the most recent execution record for every distinct job name.
    Used by the Super Admin dashboard.
    """
    # Latest execution_id per job_name (subquery)
    latest_subq = (
        select(
            JobExecutionLog.job_name,
            func.max(JobExecutionLog.started_at).label("max_started"),
        )
        .group_by(JobExecutionLog.job_name)
        .subquery()
    )
    stmt = select(JobExecutionLog).join(
        latest_subq,
        (JobExecutionLog.job_name == latest_subq.c.job_name)
        & (JobExecutionLog.started_at == latest_subq.c.max_started),
    ).order_by(JobExecutionLog.job_name)

    result = await db.execute(stmt)
    rows = list(result.scalars().all())

    return [_row_to_dict(r) for r in rows]


async def list_job_executions(
    db: AsyncSession,
    *,
    job_name: str | None = None,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict], int]:
    """
    Paginated history of job executions, newest first.
    Optionally filtered by job_name and/or status.
    """
    q = select(JobExecutionLog)
    if job_name:
        q = q.where(JobExecutionLog.job_name == job_name)
    if status:
        q = q.where(JobExecutionLog.status == status)

    count_result = await db.execute(select(func.count()).select_from(q.subquery()))
    total = count_result.scalar_one()

    rows_result = await db.execute(
        q.order_by(JobExecutionLog.started_at.desc()).limit(limit).offset(offset)
    )
    rows = list(rows_result.scalars().all())
    return [_row_to_dict(r) for r in rows], total


def _row_to_dict(row: JobExecutionLog) -> dict:
    return {
        "id": str(row.id),
        "job_name": row.job_name,
        "execution_id": row.execution_id,
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "completed_at": row.completed_at.isoformat() if row.completed_at else None,
        "duration_ms": row.duration_ms,
        "status": row.status.value if row.status else None,
        "records_affected": row.records_affected,
        "error_summary": row.error_summary,
        "meta": row.meta,
    }
