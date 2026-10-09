"""
APScheduler configuration — Phase 7.

Architecture
------------
Uses APScheduler's AsyncIOScheduler with an in-process job store.
Each job calls run_job() from scheduler_service, which:
  - opens its own DB session (independent of request scope)
  - writes JobExecutionLog rows
  - handles errors without crashing the scheduler

Multi-instance safety
---------------------
This configuration uses the default in-process MemoryJobStore.  In a
single-instance deployment (the current target) this is correct and safe.

If the project moves to multiple replicas:
  - Replace MemoryJobStore with SQLAlchemyJobStore (backed by the same DB)
  - Set coalesce=True and max_instances=1 (already set below) so only one
    instance runs a given job at a time.
  - Alternatively use a distributed lock (Redis / Postgres advisory lock).

All jobs are designed to be idempotent so a double-execution in the
transition period is harmless.

Timezone
--------
All cron schedules are expressed in IST (Asia/Kolkata).
APScheduler converts them internally to UTC before scheduling.

Job schedule
------------
  tournament_auto_transition  — every 1 minute
  room_release_reminder       — every 2 minutes
  daily_performance_refresh   — 00:05 IST daily
  month_end_salary_snapshot   — 00:30 IST on 1st of every month
  screenshot_cleanup          — 02:00 IST daily
  leaderboard_refresh         — every 5 minutes
  active_user_metrics         — every 2 minutes
"""
from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.jobs.agent_performance import (
    run_daily_performance_refresh,
    run_month_end_salary_snapshot,
)
from app.jobs.leaderboard_refresh import run_active_user_metrics, run_leaderboard_refresh
from app.jobs.screenshot_cleanup import run_screenshot_cleanup
from app.jobs.tournament_transition import (
    run_room_release_reminder,
    run_tournament_auto_transition,
)
from app.services.scheduler_service import register_job, run_job

logger = logging.getLogger("auraplayff.scheduler")

# Business timezone for cron scheduling
_IST_TZ = "Asia/Kolkata"


# ── Job wrapper callables ─────────────────────────────────────────────────────
# APScheduler calls plain callables.  We wrap each in an async lambda that
# delegates to run_job() so execution logging happens automatically.

async def _job_tournament_transition() -> None:
    await run_job("tournament_auto_transition", run_tournament_auto_transition)


async def _job_room_release_reminder() -> None:
    await run_job("room_release_reminder", run_room_release_reminder)


async def _job_daily_performance_refresh() -> None:
    await run_job("daily_performance_refresh", run_daily_performance_refresh)


async def _job_month_end_salary_snapshot() -> None:
    await run_job("month_end_salary_snapshot", run_month_end_salary_snapshot)


async def _job_screenshot_cleanup() -> None:
    await run_job("screenshot_cleanup", run_screenshot_cleanup)


async def _job_leaderboard_refresh() -> None:
    await run_job("leaderboard_refresh", run_leaderboard_refresh)


async def _job_active_user_metrics() -> None:
    await run_job("active_user_metrics", run_active_user_metrics)


# ── Scheduler factory ─────────────────────────────────────────────────────────

def create_scheduler() -> AsyncIOScheduler:
    """
    Build and configure the APScheduler instance.

    Call scheduler.start() in the FastAPI lifespan (before yield).
    Call scheduler.shutdown(wait=False) in the shutdown block (after yield).
    """
    scheduler = AsyncIOScheduler(
        job_defaults={
            "coalesce": True,       # merge missed executions into one
            "max_instances": 1,     # never run the same job twice concurrently
            "misfire_grace_time": 120,  # allow 2 min late start before skipping
        }
    )

    # ── Tournament auto-transition: every 1 minute ────────────────────────────
    scheduler.add_job(
        _job_tournament_transition,
        trigger=IntervalTrigger(minutes=1),
        id="tournament_auto_transition",
        name="Tournament Auto-Transition (CONFIRMED → LIVE)",
        replace_existing=True,
    )

    # ── Room release reminder: every 2 minutes ────────────────────────────────
    scheduler.add_job(
        _job_room_release_reminder,
        trigger=IntervalTrigger(minutes=2),
        id="room_release_reminder",
        name="Room Release Reminder (10 min pre-start)",
        replace_existing=True,
    )

    # ── Daily performance refresh: 00:05 IST ─────────────────────────────────
    scheduler.add_job(
        _job_daily_performance_refresh,
        trigger=CronTrigger(hour=0, minute=5, timezone=_IST_TZ),
        id="daily_performance_refresh",
        name="Agent Daily Performance Refresh",
        replace_existing=True,
    )

    # ── Month-end salary snapshot: 1st of every month 00:30 IST ──────────────
    scheduler.add_job(
        _job_month_end_salary_snapshot,
        trigger=CronTrigger(day=1, hour=0, minute=30, timezone=_IST_TZ),
        id="month_end_salary_snapshot",
        name="Month-End Salary Snapshot",
        replace_existing=True,
    )

    # ── Screenshot cleanup: 02:00 IST daily ──────────────────────────────────
    scheduler.add_job(
        _job_screenshot_cleanup,
        trigger=CronTrigger(hour=2, minute=0, timezone=_IST_TZ),
        id="screenshot_cleanup",
        name="Screenshot Retention Cleanup",
        replace_existing=True,
    )

    # ── Leaderboard cache refresh: every 5 minutes ───────────────────────────
    scheduler.add_job(
        _job_leaderboard_refresh,
        trigger=IntervalTrigger(minutes=5),
        id="leaderboard_refresh",
        name="Leaderboard Cache Refresh",
        replace_existing=True,
    )

    # ── Active user metrics: every 2 minutes ─────────────────────────────────
    scheduler.add_job(
        _job_active_user_metrics,
        trigger=IntervalTrigger(minutes=2),
        id="active_user_metrics",
        name="Active User Metrics Refresh",
        replace_existing=True,
    )

    logger.info("Scheduler configured with %d jobs.", len(scheduler.get_jobs()))
    return scheduler


def _register_all_jobs() -> None:
    """
    Register all job functions in the scheduler_service registry so they
    can be manually triggered via the Super Admin API.
    """
    register_job("tournament_auto_transition", run_tournament_auto_transition)
    register_job("room_release_reminder", run_room_release_reminder)
    register_job("daily_performance_refresh", run_daily_performance_refresh)
    register_job("month_end_salary_snapshot", run_month_end_salary_snapshot)
    register_job("screenshot_cleanup", run_screenshot_cleanup)
    register_job("leaderboard_refresh", run_leaderboard_refresh)
    register_job("active_user_metrics", run_active_user_metrics)


# Called once during lifespan startup
def setup_scheduler() -> AsyncIOScheduler:
    """Create the scheduler, register all jobs, and return it ready to start."""
    _register_all_jobs()
    return create_scheduler()
