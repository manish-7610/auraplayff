"""
Tournament auto-transition job — Phase 7.

Responsibilities
----------------
1. CONFIRMED → LIVE
   Any CONFIRMED tournament whose scheduled_at has passed AND whose
   player_count >= min_players is transitioned to LIVE automatically.
   If player_count < min_players the tournament is left alone so that
   Admin/Agent can decide to cancel it.

2. Room-release reminder (event: scheduler.room_release_due)
   CONFIRMED tournaments whose scheduled_at is within ROOM_RELEASE_WINDOW
   minutes from now emit a room_release_due notification to the creating
   agent.  This fires once per tournament (idempotency_key on notification).

Safety rules
------------
- Uses SELECT … FOR UPDATE SKIP LOCKED so concurrent workers never fight
  over the same row.
- Only transitions from the exact expected status — if the status has
  already been changed by Admin/Agent the lock check will see it and skip.
- No invalid transitions can occur: the VALID_TRANSITIONS guard in
  tournament_service is NOT bypassed — this job constructs the same check.
- All transitions are audited with ActorType.system.
- Every run is wrapped by the scheduler_service job-runner so execution
  is logged in job_execution_logs.

IST / timezone
--------------
All datetime comparisons are made in UTC.  The scheduler fires in UTC;
tournament.scheduled_at is stored timezone-aware (UTC).
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models._types import utcnow
from app.models.audit_log import ActorType
from app.models.tournament import Tournament, TournamentStatus, TournamentEntry, EntryStatus
from app.services import audit_service
from app.services import notification_service as ns

logger = logging.getLogger("auraplayff.jobs.tournament_transition")

# How many minutes before scheduled_at to emit the room-release reminder
ROOM_RELEASE_WINDOW_MINUTES: int = 10


async def run_tournament_auto_transition(db: AsyncSession) -> int:
    """
    Transition eligible tournaments to LIVE.

    Handles TWO cases per the documented tournament lifecycle:
      1. CONFIRMED → LIVE (scheduler moves directly when room was never released
         before scheduled_at, e.g. admin skipped room-release step)
      2. ROOM_RELEASED → LIVE (BUG #2 FIX: the scheduler previously never
         picked up ROOM_RELEASED tournaments, so they would stay stuck forever
         unless an admin manually clicked "Go Live")

    Both transitions require:
      - scheduled_at has passed
      - player_count >= min_players

    Idempotency: a tournament already in LIVE/COMPLETED/CANCELLED will
    never match the WHERE clause, so the job is safe to run multiple times.
    """
    now = utcnow()
    transitioned = 0

    # BUG #2 FIX: include BOTH CONFIRMED and ROOM_RELEASED statuses so that
    # a tournament whose room has already been released is also promoted to LIVE
    # automatically when scheduled_at passes.
    stmt = (
        select(Tournament)
        .where(
            Tournament.status.in_([
                TournamentStatus.confirmed,
                TournamentStatus.room_released,
            ]),
            Tournament.scheduled_at <= now,
        )
        .with_for_update(skip_locked=True)
    )
    result = await db.execute(stmt)
    tournaments: list[Tournament] = list(result.scalars().all())

    for t in tournaments:
        if t.player_count < t.min_players:
            logger.info(
                "Tournament %s (%s) has only %d/%d players — skipping auto-transition",
                t.tournament_code, t.id, t.player_count, t.min_players,
            )
            continue

        # Double-check status under lock — defensive guard for test/mock scenarios
        if t.status not in (TournamentStatus.confirmed, TournamentStatus.room_released):
            logger.warning(
                "Tournament %s status changed under lock to %s — skipping",
                t.tournament_code, t.status,
            )
            continue

        old_status = t.status
        t.status = TournamentStatus.live
        t.started_at = now
        t.updated_at = now

        await audit_service.log(
            db=db,
            actor_type=ActorType.system,
            actor_id="scheduler:tournament_auto_transition",
            action="tournament.auto_transitioned_to_live",
            entity_type="tournament",
            entity_id=str(t.id),
            old_value={"status": old_status.value},
            new_value={"status": TournamentStatus.live.value, "started_at": now.isoformat()},
        )

        # Notify all active participants that the tournament is LIVE
        await _notify_participants_live(db, t)

        transitioned += 1
        logger.info(
            "Tournament %s (%s) auto-transitioned %s → LIVE",
            t.tournament_code, t.id, old_status.value.upper(),
        )

    return transitioned


async def run_room_release_reminder(db: AsyncSession) -> int:
    """
    Emit room_release_due notifications for CONFIRMED tournaments whose
    scheduled_at is within ROOM_RELEASE_WINDOW_MINUTES from now.

    Each reminder is idempotent — only fires once per tournament (per
    creating agent) thanks to the idempotency_key on the notification.

    Returns the number of reminders emitted.
    """
    now = utcnow()
    window_end = now + timedelta(minutes=ROOM_RELEASE_WINDOW_MINUTES)

    stmt = select(Tournament).where(
        Tournament.status == TournamentStatus.confirmed,
        Tournament.scheduled_at > now,          # hasn't passed yet
        Tournament.scheduled_at <= window_end,  # but within window
        Tournament.room_released_at.is_(None),  # room not yet released
    )
    result = await db.execute(stmt)
    tournaments: list[Tournament] = list(result.scalars().all())

    reminded = 0
    for t in tournaments:
        if t.created_by_id is None:
            continue

        # Format scheduled_at in IST for the notification message
        ist_offset = timedelta(hours=5, minutes=30)
        scheduled_ist = (t.scheduled_at + ist_offset).strftime("%d %b %Y %I:%M %p")

        await ns.notify_agent_room_release_reminder(
            db,
            staff_user_id=t.created_by_id,
            tournament_id=t.id,
            tournament_title=t.title,
            scheduled_at_ist=scheduled_ist,
        )

        await audit_service.log(
            db=db,
            actor_type=ActorType.system,
            actor_id="scheduler:room_release_reminder",
            action="tournament.room_release_reminder_sent",
            entity_type="tournament",
            entity_id=str(t.id),
            new_value={"scheduled_at_ist": scheduled_ist},
        )

        reminded += 1
        logger.info(
            "Room-release reminder sent for tournament %s (%s) scheduled at %s IST",
            t.tournament_code, t.id, scheduled_ist,
        )

    return reminded


# ── Private helpers ───────────────────────────────────────────────────────────

async def _notify_participants_live(db: AsyncSession, t: Tournament) -> None:
    """
    Send a TOURNAMENT_STARTED notification to every active entry holder.
    Best-effort — a notification failure must not abort the transition.
    """
    try:
        entries_result = await db.execute(
            select(TournamentEntry).where(
                TournamentEntry.tournament_id == t.id,
                TournamentEntry.status == EntryStatus.active,
            )
        )
        entries = list(entries_result.scalars().all())
        for entry in entries:
            await ns.notify_player_tournament_started(
                db,
                user_id=entry.user_id,
                tournament_id=t.id,
                tournament_title=t.title,
            )
    except Exception:
        logger.exception(
            "Failed to notify participants for tournament %s going LIVE", t.id
        )
