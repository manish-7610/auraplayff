"""
Scheduler / job monitoring schemas — Phase 7.

Used by the Super Admin job monitoring endpoints.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class JobExecutionResponse(BaseModel):
    """A single job execution log entry."""
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    job_name: str
    execution_id: str
    started_at: datetime
    completed_at: Optional[datetime] = None
    duration_ms: Optional[int] = None
    status: str
    records_affected: Optional[int] = None
    error_summary: Optional[str] = None
    meta: Optional[dict] = None


class JobExecutionListResponse(BaseModel):
    """Paginated job execution history."""
    items: list[JobExecutionResponse]
    total: int
    limit: int
    offset: int


class JobSummaryResponse(BaseModel):
    """Latest execution record per job name — for the dashboard overview."""
    job_name: str
    last_run_at: Optional[datetime] = None
    last_status: Optional[str] = None
    last_duration_ms: Optional[int] = None
    last_records_affected: Optional[int] = None
    last_error_summary: Optional[str] = None
    is_registered: bool = True


class TriggerJobResponse(BaseModel):
    """Confirmation that a manual job trigger was accepted."""
    job_name: str
    message: str


class RegisteredJobsResponse(BaseModel):
    """List of all job names that can be manually triggered."""
    jobs: list[str]
