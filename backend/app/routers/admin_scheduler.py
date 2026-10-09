"""
Admin Scheduler / Job Monitoring API — Phase 7.

All endpoints are Super Admin only (role check, not permission check —
scheduler management is not delegated to agents).

Endpoints
---------
GET  /api/admin/jobs                       — job dashboard summary (one row per job)
GET  /api/admin/jobs/history               — paginated full execution history
GET  /api/admin/jobs/registered            — list triggerable job names
POST /api/admin/jobs/{job_name}/trigger    — manually trigger a registered job

Security
--------
- Only Super Admin (StaffRole.super_admin) can access these endpoints.
- Agents are explicitly rejected — not just by missing permission.
- Manual trigger fires the job in the background (run_job does its own
  session management).
- Error summaries from failed jobs are truncated to 2 000 chars in the
  service layer — no risk of leaking sensitive DB content.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppError
from app.database import get_db
from app.dependencies import get_current_staff
from app.models.staff_user import StaffRole, StaffUser
from app.schemas.scheduler import (
    JobExecutionListResponse,
    JobExecutionResponse,
    JobSummaryResponse,
    RegisteredJobsResponse,
    TriggerJobResponse,
)
from app.services.scheduler_service import (
    get_job_summary,
    list_job_executions,
    list_registered_jobs,
    trigger_job,
)

router = APIRouter(prefix="/api/admin/jobs", tags=["admin-scheduler"])


# ── Auth helper ───────────────────────────────────────────────────────────────

def _require_super_admin(staff: StaffUser) -> None:
    if staff.role != StaffRole.super_admin:
        raise AppError(
            "permission_denied",
            "Scheduler management is restricted to Super Admin.",
            403,
        )


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("", response_model=list[JobSummaryResponse])
async def job_dashboard(
    current_staff: StaffUser = Depends(get_current_staff),
    db: AsyncSession = Depends(get_db),
) -> list[JobSummaryResponse]:
    """
    Return the latest execution record for every registered job.
    Provides a quick health overview of all scheduled automation.
    """
    _require_super_admin(current_staff)

    rows = await get_job_summary(db)
    registered = set(list_registered_jobs())

    summaries: list[JobSummaryResponse] = []
    seen_jobs: set[str] = set()

    for row in rows:
        seen_jobs.add(row["job_name"])
        summaries.append(
            JobSummaryResponse(
                job_name=row["job_name"],
                last_run_at=row["started_at"],
                last_status=row["status"],
                last_duration_ms=row["duration_ms"],
                last_records_affected=row["records_affected"],
                last_error_summary=row["error_summary"],
                is_registered=row["job_name"] in registered,
            )
        )

    # Include registered jobs that have never run yet
    for job_name in sorted(registered - seen_jobs):
        summaries.append(
            JobSummaryResponse(
                job_name=job_name,
                last_run_at=None,
                last_status=None,
                last_duration_ms=None,
                last_records_affected=None,
                last_error_summary=None,
                is_registered=True,
            )
        )

    summaries.sort(key=lambda s: s.job_name)
    return summaries


@router.get("/history", response_model=JobExecutionListResponse)
async def job_execution_history(
    job_name: str | None = Query(None, description="Filter by job name"),
    status: str | None = Query(None, description="Filter by status: running/success/failed/skipped"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    current_staff: StaffUser = Depends(get_current_staff),
    db: AsyncSession = Depends(get_db),
) -> JobExecutionListResponse:
    """
    Paginated history of all job executions.
    Optionally filtered by job_name and/or status.
    Newest first.
    """
    _require_super_admin(current_staff)

    rows, total = await list_job_executions(
        db,
        job_name=job_name,
        status=status,
        limit=limit,
        offset=offset,
    )

    items = [JobExecutionResponse(**row) for row in rows]
    return JobExecutionListResponse(
        items=items,
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/registered", response_model=RegisteredJobsResponse)
async def list_triggerable_jobs(
    current_staff: StaffUser = Depends(get_current_staff),
) -> RegisteredJobsResponse:
    """Return all job names that can be manually triggered."""
    _require_super_admin(current_staff)
    return RegisteredJobsResponse(jobs=list_registered_jobs())


@router.post("/{job_name}/trigger", response_model=TriggerJobResponse)
async def manual_trigger_job(
    job_name: str,
    current_staff: StaffUser = Depends(get_current_staff),
) -> TriggerJobResponse:
    """
    Manually trigger a registered scheduled job.

    The job runs asynchronously — this endpoint returns immediately after
    dispatch.  Check /api/admin/jobs/history for the execution result.

    Raises 404 if the job_name is not registered.
    """
    _require_super_admin(current_staff)

    try:
        await trigger_job(job_name)
    except KeyError:
        raise HTTPException(
            status_code=404,
            detail=f"Job '{job_name}' is not registered. "
                   f"Available jobs: {list_registered_jobs()}",
        )

    return TriggerJobResponse(
        job_name=job_name,
        message=f"Job '{job_name}' has been triggered. Check /api/admin/jobs/history for the result.",
    )
