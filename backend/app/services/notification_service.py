"""
Notification service — Phase 7.

Centralised notification creation.  All other services/routers/jobs call
these helpers — they must NEVER insert into notifications directly.

Public API
----------
create_notification()       — low-level insert (idempotent via key)
notify_player()             — create a notification for a player
notify_agent()              — create a notification for an agent
notify_admin()              — broadcast to every active Super Admin

The service silently swallows duplicate-key conflicts so callers that fire
the same event twice (e.g. retried jobs) do not crash.

Security rules
--------------
- Recipient is always derived from server-side identity, never from client.
- Secrets (room passwords, tokens, wallet keys) must never appear in title/message/meta.
- is_read starts False; only the owning recipient can flip it.
- Super Admin visibility of staff notifications is handled in the router layer.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.notification import Notification, NotificationType, RecipientType
from app.models.staff_user import StaffRole, StaffUser

logger = logging.getLogger("auraplayff.notifications")


# ── Core create ───────────────────────────────────────────────────────────────

async def create_notification(
    db: AsyncSession,
    *,
    recipient_type: RecipientType,
    user_id: uuid.UUID | None = None,
    staff_user_id: uuid.UUID | None = None,
    notification_type: str,
    title: str,
    message: str,
    related_entity_type: str | None = None,
    related_entity_id: str | None = None,
    idempotency_key: str | None = None,
    meta: dict[str, Any] | None = None,
    expires_at: datetime | None = None,
) -> Notification | None:
    """
    Insert one notification row.

    Returns the Notification instance on success, or None if the
    idempotency_key already exists (duplicate — silently skipped).

    The caller is responsible for flushing/committing the session.
    """
    # Sanity: exactly one recipient column must be set
    if recipient_type == RecipientType.player and user_id is None:
        logger.error(
            "create_notification called for player recipient but user_id is None — skipping"
        )
        return None
    if recipient_type in (RecipientType.agent, RecipientType.admin) and staff_user_id is None:
        logger.error(
            "create_notification called for %s recipient but staff_user_id is None — skipping",
            recipient_type,
        )
        return None

    notif = Notification(
        recipient_type=recipient_type,
        user_id=user_id,
        staff_user_id=staff_user_id,
        notification_type=notification_type,
        title=title,
        message=message,
        related_entity_type=related_entity_type,
        related_entity_id=related_entity_id,
        idempotency_key=idempotency_key,
        meta=meta,
        expires_at=expires_at,
    )

    try:
        # BUG #12 FIX: wrap in a savepoint so that a duplicate-idempotency_key
        # IntegrityError rolls back ONLY this insert, not the caller's entire
        # transaction.  The previous code called await db.rollback() on the shared
        # session, which silently discarded all pending writes from the caller
        # (salary adjustments, wallet transactions, etc.).
        async with db.begin_nested():
            db.add(notif)
            await db.flush([notif])
        return notif
    except IntegrityError:
        # Duplicate idempotency_key — this is expected for retried events.
        # The savepoint above already rolled back only the notification insert.
        logger.debug(
            "Duplicate notification skipped: key=%s type=%s",
            idempotency_key,
            notification_type,
        )
        return None
    except Exception:
        logger.exception(
            "Failed to create notification type=%s recipient=%s/%s",
            notification_type,
            user_id,
            staff_user_id,
        )
        return None


# ── Recipient-specific helpers ────────────────────────────────────────────────

async def notify_player(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    notification_type: str,
    title: str,
    message: str,
    related_entity_type: str | None = None,
    related_entity_id: str | None = None,
    idempotency_key: str | None = None,
    meta: dict[str, Any] | None = None,
    expires_at: datetime | None = None,
) -> Notification | None:
    """Create a notification for a single player."""
    # Build default idempotency key when caller omits one
    if idempotency_key is None and related_entity_id is not None:
        idempotency_key = (
            f"{notification_type}:{related_entity_type or 'entity'}:"
            f"{related_entity_id}:{user_id}"
        )
    return await create_notification(
        db,
        recipient_type=RecipientType.player,
        user_id=user_id,
        notification_type=notification_type,
        title=title,
        message=message,
        related_entity_type=related_entity_type,
        related_entity_id=related_entity_id,
        idempotency_key=idempotency_key,
        meta=meta,
        expires_at=expires_at,
    )


async def notify_agent(
    db: AsyncSession,
    *,
    staff_user_id: uuid.UUID,
    notification_type: str,
    title: str,
    message: str,
    related_entity_type: str | None = None,
    related_entity_id: str | None = None,
    idempotency_key: str | None = None,
    meta: dict[str, Any] | None = None,
    expires_at: datetime | None = None,
) -> Notification | None:
    """Create a notification for a single Agent."""
    if idempotency_key is None and related_entity_id is not None:
        idempotency_key = (
            f"{notification_type}:{related_entity_type or 'entity'}:"
            f"{related_entity_id}:{staff_user_id}"
        )
    return await create_notification(
        db,
        recipient_type=RecipientType.agent,
        staff_user_id=staff_user_id,
        notification_type=notification_type,
        title=title,
        message=message,
        related_entity_type=related_entity_type,
        related_entity_id=related_entity_id,
        idempotency_key=idempotency_key,
        meta=meta,
        expires_at=expires_at,
    )


async def notify_admin(
    db: AsyncSession,
    *,
    notification_type: str,
    title: str,
    message: str,
    related_entity_type: str | None = None,
    related_entity_id: str | None = None,
    meta: dict[str, Any] | None = None,
    expires_at: datetime | None = None,
) -> list[Notification]:
    """
    Broadcast a notification to every active Super Admin.

    Idempotency key is derived per-admin so each gets exactly one copy.
    Failures for individual admins are logged but do not abort the broadcast.
    """
    result = await db.execute(
        select(StaffUser).where(
            StaffUser.role == StaffRole.super_admin,
            StaffUser.is_active == True,  # noqa: E712
        )
    )
    admins = list(result.scalars().all())

    created: list[Notification] = []
    for admin in admins:
        idem_key = None
        if related_entity_id is not None:
            idem_key = (
                f"{notification_type}:{related_entity_type or 'entity'}:"
                f"{related_entity_id}:{admin.id}"
            )
        notif = await create_notification(
            db,
            recipient_type=RecipientType.admin,
            staff_user_id=admin.id,
            notification_type=notification_type,
            title=title,
            message=message,
            related_entity_type=related_entity_type,
            related_entity_id=related_entity_id,
            idempotency_key=idem_key,
            meta=meta,
            expires_at=expires_at,
        )
        if notif is not None:
            created.append(notif)

    return created


# ── Domain-level event helpers ────────────────────────────────────────────────
# These wrap the recipient helpers with pre-formatted titles/messages so
# individual routers/jobs need only one import and one call.

# ── Player: Account ───────────────────────────────────────────────────────────

async def notify_player_registered(
    db: AsyncSession, *, user_id: uuid.UUID, username: str
) -> None:
    await notify_player(
        db,
        user_id=user_id,
        notification_type=NotificationType.REGISTRATION_SUCCESS,
        title="Welcome to AuraPlayFF!",
        message=f"Your account '{username}' is ready. Join a tournament and start competing!",
        idempotency_key=f"{NotificationType.REGISTRATION_SUCCESS}:user:{user_id}",
    )


async def notify_player_password_reset_requested(
    db: AsyncSession, *, user_id: uuid.UUID
) -> None:
    await notify_player(
        db,
        user_id=user_id,
        notification_type=NotificationType.PASSWORD_RESET_REQUESTED,
        title="Password Reset Requested",
        message="A password reset was requested for your account. Contact support if this wasn't you.",
        idempotency_key=None,  # allow repeats
    )


async def notify_player_password_changed(
    db: AsyncSession, *, user_id: uuid.UUID
) -> None:
    await notify_player(
        db,
        user_id=user_id,
        notification_type=NotificationType.PASSWORD_CHANGED,
        title="Password Changed",
        message="Your account password was successfully updated.",
    )


# ── Player: Tournament ────────────────────────────────────────────────────────

async def notify_player_tournament_joined(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    tournament_id: uuid.UUID,
    tournament_title: str,
    entry_fee: str,
    sheet_number: int,
) -> None:
    await notify_player(
        db,
        user_id=user_id,
        notification_type=NotificationType.TOURNAMENT_JOINED,
        title="Tournament Registration Confirmed",
        message=(
            f"You've joined '{tournament_title}'. "
            f"Your sheet number is #{sheet_number}. Entry fee: ₹{entry_fee}."
        ),
        related_entity_type="tournament",
        related_entity_id=str(tournament_id),
        meta={"sheet_number": sheet_number, "entry_fee": entry_fee},
    )


async def notify_player_tournament_cancelled(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    tournament_id: uuid.UUID,
    tournament_title: str,
    entry_fee: str,
) -> None:
    await notify_player(
        db,
        user_id=user_id,
        notification_type=NotificationType.TOURNAMENT_CANCELLED,
        title="Tournament Cancelled — Refund Initiated",
        message=(
            f"'{tournament_title}' has been cancelled. "
            f"Your entry fee of ₹{entry_fee} will be refunded to your wallet."
        ),
        related_entity_type="tournament",
        related_entity_id=str(tournament_id),
    )


async def notify_player_room_released(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    tournament_id: uuid.UUID,
    tournament_title: str,
) -> None:
    await notify_player(
        db,
        user_id=user_id,
        notification_type=NotificationType.TOURNAMENT_ROOM_RELEASED,
        title="Room Credentials Released",
        message=(
            f"Room ID and password for '{tournament_title}' are now available. "
            "Check the tournament page to join."
        ),
        related_entity_type="tournament",
        related_entity_id=str(tournament_id),
    )


async def notify_player_tournament_started(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    tournament_id: uuid.UUID,
    tournament_title: str,
) -> None:
    await notify_player(
        db,
        user_id=user_id,
        notification_type=NotificationType.TOURNAMENT_STARTED,
        title="Tournament is LIVE!",
        message=f"'{tournament_title}' has started. Good luck!",
        related_entity_type="tournament",
        related_entity_id=str(tournament_id),
    )


async def notify_player_tournament_completed(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    tournament_id: uuid.UUID,
    tournament_title: str,
) -> None:
    await notify_player(
        db,
        user_id=user_id,
        notification_type=NotificationType.TOURNAMENT_COMPLETED,
        title="Tournament Completed",
        message=f"'{tournament_title}' has ended. Results will be published shortly.",
        related_entity_type="tournament",
        related_entity_id=str(tournament_id),
    )


async def notify_player_result_finalized(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    tournament_id: uuid.UUID,
    tournament_title: str,
    placement: int,
) -> None:
    await notify_player(
        db,
        user_id=user_id,
        notification_type=NotificationType.RESULT_FINALIZED,
        title="Your Result is Finalised",
        message=(
            f"Results for '{tournament_title}' are confirmed. "
            f"You finished at position #{placement}."
        ),
        related_entity_type="tournament",
        related_entity_id=str(tournament_id),
        meta={"placement": placement},
    )


async def notify_player_prize_credited(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    tournament_id: uuid.UUID,
    tournament_title: str,
    amount: str,
) -> None:
    await notify_player(
        db,
        user_id=user_id,
        notification_type=NotificationType.PRIZE_CREDITED,
        title="Prize Credited to Your Wallet",
        message=f"₹{amount} prize from '{tournament_title}' has been added to your wallet.",
        related_entity_type="tournament",
        related_entity_id=str(tournament_id),
        meta={"amount": amount},
    )


async def notify_player_kill_reward_credited(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    tournament_id: uuid.UUID,
    tournament_title: str,
    amount: str,
) -> None:
    await notify_player(
        db,
        user_id=user_id,
        notification_type=NotificationType.KILL_REWARD_CREDITED,
        title="Kill Reward Credited",
        message=f"₹{amount} kill reward from '{tournament_title}' has been added to your wallet.",
        related_entity_type="tournament",
        related_entity_id=str(tournament_id),
        meta={"amount": amount},
    )


# ── Player: Deposit ───────────────────────────────────────────────────────────

async def notify_player_deposit_initiated(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    deposit_id: uuid.UUID,
    amount: str,
    method: str,
) -> None:
    await notify_player(
        db,
        user_id=user_id,
        notification_type=NotificationType.DEPOSIT_INITIATED,
        title="Deposit Initiated",
        message=f"A deposit of ₹{amount} via {method} has been initiated.",
        related_entity_type="deposit",
        related_entity_id=str(deposit_id),
        meta={"amount": amount, "method": method},
    )


async def notify_player_deposit_online_success(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    deposit_id: uuid.UUID,
    amount: str,
) -> None:
    await notify_player(
        db,
        user_id=user_id,
        notification_type=NotificationType.DEPOSIT_ONLINE_SUCCESS,
        title="Payment Successful",
        message=f"₹{amount} has been added to your wallet.",
        related_entity_type="deposit",
        related_entity_id=str(deposit_id),
        meta={"amount": amount},
    )


async def notify_player_deposit_manual_submitted(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    deposit_id: uuid.UUID,
    amount: str,
) -> None:
    await notify_player(
        db,
        user_id=user_id,
        notification_type=NotificationType.DEPOSIT_MANUAL_SUBMITTED,
        title="Manual Deposit Under Review",
        message=(
            f"Your manual UPI deposit of ₹{amount} has been submitted for review. "
            "It will be approved within 24 hours."
        ),
        related_entity_type="deposit",
        related_entity_id=str(deposit_id),
        meta={"amount": amount},
    )


async def notify_player_deposit_approved(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    deposit_id: uuid.UUID,
    amount: str,
) -> None:
    await notify_player(
        db,
        user_id=user_id,
        notification_type=NotificationType.DEPOSIT_APPROVED,
        title="Deposit Approved",
        message=f"Your deposit of ₹{amount} has been approved and credited to your wallet.",
        related_entity_type="deposit",
        related_entity_id=str(deposit_id),
        meta={"amount": amount},
    )


async def notify_player_deposit_rejected(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    deposit_id: uuid.UUID,
    amount: str,
    reason: str | None = None,
) -> None:
    msg = f"Your deposit of ₹{amount} was rejected."
    if reason:
        msg += f" Reason: {reason}"
    await notify_player(
        db,
        user_id=user_id,
        notification_type=NotificationType.DEPOSIT_REJECTED,
        title="Deposit Rejected",
        message=msg,
        related_entity_type="deposit",
        related_entity_id=str(deposit_id),
    )


# ── Player: Withdrawal ────────────────────────────────────────────────────────

async def notify_player_withdrawal_submitted(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    withdrawal_id: uuid.UUID,
    amount: str,
) -> None:
    await notify_player(
        db,
        user_id=user_id,
        notification_type=NotificationType.WITHDRAWAL_SUBMITTED,
        title="Withdrawal Request Submitted",
        message=(
            f"Your withdrawal request of ₹{amount} has been submitted and is under review."
        ),
        related_entity_type="withdrawal",
        related_entity_id=str(withdrawal_id),
        meta={"amount": amount},
    )


async def notify_player_withdrawal_processing(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    withdrawal_id: uuid.UUID,
    amount: str,
) -> None:
    await notify_player(
        db,
        user_id=user_id,
        notification_type=NotificationType.WITHDRAWAL_PROCESSING,
        title="Withdrawal Processing",
        message=f"Your withdrawal of ₹{amount} is being processed.",
        related_entity_type="withdrawal",
        related_entity_id=str(withdrawal_id),
        meta={"amount": amount},
    )


async def notify_player_withdrawal_paid(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    withdrawal_id: uuid.UUID,
    amount: str,
) -> None:
    await notify_player(
        db,
        user_id=user_id,
        notification_type=NotificationType.WITHDRAWAL_PAID,
        title="Withdrawal Paid",
        message=f"₹{amount} has been sent to your UPI account.",
        related_entity_type="withdrawal",
        related_entity_id=str(withdrawal_id),
        meta={"amount": amount},
    )


async def notify_player_withdrawal_rejected(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    withdrawal_id: uuid.UUID,
    amount: str,
    reason: str | None = None,
) -> None:
    msg = f"Your withdrawal of ₹{amount} was rejected and the amount has been returned to your wallet."
    if reason:
        msg += f" Reason: {reason}"
    await notify_player(
        db,
        user_id=user_id,
        notification_type=NotificationType.WITHDRAWAL_REJECTED,
        title="Withdrawal Rejected",
        message=msg,
        related_entity_type="withdrawal",
        related_entity_id=str(withdrawal_id),
    )


# ── Player: Referral ──────────────────────────────────────────────────────────

async def notify_player_referral_reward(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    referral_id: uuid.UUID,
    amount: str,
    referred_username: str,
) -> None:
    await notify_player(
        db,
        user_id=user_id,
        notification_type=NotificationType.REFERRAL_REWARD_CREDITED,
        title="Referral Reward Credited",
        message=(
            f"₹{amount} referral bonus added to your wallet because "
            f"'{referred_username}' completed their first tournament!"
        ),
        related_entity_type="referral",
        related_entity_id=str(referral_id),
        meta={"amount": amount, "referred_username": referred_username},
    )


# ── Agent notifications ───────────────────────────────────────────────────────

async def notify_agent_tournament_created(
    db: AsyncSession,
    *,
    staff_user_id: uuid.UUID,
    tournament_id: uuid.UUID,
    tournament_title: str,
    tournament_code: str,
) -> None:
    await notify_agent(
        db,
        staff_user_id=staff_user_id,
        notification_type=NotificationType.AGENT_TOURNAMENT_CREATED,
        title="Tournament Created",
        message=f"'{tournament_title}' ({tournament_code}) has been created successfully.",
        related_entity_type="tournament",
        related_entity_id=str(tournament_id),
        meta={"tournament_code": tournament_code},
    )


async def notify_agent_tournament_cancelled(
    db: AsyncSession,
    *,
    staff_user_id: uuid.UUID,
    tournament_id: uuid.UUID,
    tournament_title: str,
) -> None:
    await notify_agent(
        db,
        staff_user_id=staff_user_id,
        notification_type=NotificationType.AGENT_TOURNAMENT_CANCELLED,
        title="Tournament Cancelled",
        message=f"'{tournament_title}' has been cancelled.",
        related_entity_type="tournament",
        related_entity_id=str(tournament_id),
    )


async def notify_agent_room_release_reminder(
    db: AsyncSession,
    *,
    staff_user_id: uuid.UUID,
    tournament_id: uuid.UUID,
    tournament_title: str,
    scheduled_at_ist: str,
) -> None:
    """
    Fires approximately 10 minutes before scheduled tournament time.
    Does NOT reveal room credentials.  Just a reminder to release.
    """
    await notify_agent(
        db,
        staff_user_id=staff_user_id,
        notification_type=NotificationType.AGENT_ROOM_RELEASE_REMINDER,
        title="⏰ Room Release Reminder",
        message=(
            f"'{tournament_title}' is scheduled at {scheduled_at_ist} IST. "
            "Please release the room credentials now."
        ),
        related_entity_type="tournament",
        related_entity_id=str(tournament_id),
        # Idempotency: one reminder per tournament
        idempotency_key=(
            f"{NotificationType.AGENT_ROOM_RELEASE_REMINDER}:tournament:{tournament_id}:{staff_user_id}"
        ),
    )


async def notify_agent_daily_target_status(
    db: AsyncSession,
    *,
    staff_user_id: uuid.UUID,
    perf_date: str,
    completed: int,
    required: int,
    shortfall: int,
) -> None:
    if shortfall == 0:
        msg = f"Great work! You met your daily target of {required} tournaments on {perf_date}."
        title = "Daily Target Met"
        ntype = NotificationType.AGENT_TARGET_COMPLETED
    else:
        msg = (
            f"You completed {completed}/{required} tournaments on {perf_date}. "
            f"Shortfall: {shortfall}."
        )
        title = "Daily Target Status"
        ntype = NotificationType.AGENT_DAILY_TARGET_STATUS
    await notify_agent(
        db,
        staff_user_id=staff_user_id,
        notification_type=ntype,
        title=title,
        message=msg,
        meta={
            "completed": completed,
            "required": required,
            "shortfall": shortfall,
            "perf_date": perf_date,
        },
        idempotency_key=f"{ntype}:perf:{perf_date}:{staff_user_id}",
    )


async def notify_agent_extra_bonus(
    db: AsyncSession,
    *,
    staff_user_id: uuid.UUID,
    perf_date: str,
    extra_count: int,
    extra_bonus: str,
) -> None:
    await notify_agent(
        db,
        staff_user_id=staff_user_id,
        notification_type=NotificationType.AGENT_EXTRA_BONUS_EARNED,
        title="Extra Bonus Earned!",
        message=(
            f"You ran {extra_count} extra tournament(s) on {perf_date}. "
            f"Bonus ₹{extra_bonus} added."
        ),
        meta={"extra_count": extra_count, "extra_bonus": extra_bonus, "perf_date": perf_date},
        idempotency_key=(
            f"{NotificationType.AGENT_EXTRA_BONUS_EARNED}:perf:{perf_date}:{staff_user_id}"
        ),
    )


async def notify_agent_warning_issued(
    db: AsyncSession,
    *,
    staff_user_id: uuid.UUID,
    warning_id: uuid.UUID,
    perf_date: str,
    warning_number: int,
    reason: str,
) -> None:
    await notify_agent(
        db,
        staff_user_id=staff_user_id,
        notification_type=NotificationType.AGENT_WARNING_ISSUED,
        title=f"Warning Issued (Warning #{warning_number})",
        message=f"A performance warning was issued for {perf_date}. Reason: {reason}",
        related_entity_type="agent_warning",
        related_entity_id=str(warning_id),
        meta={"warning_number": warning_number, "perf_date": perf_date},
    )


async def notify_agent_grace_granted(
    db: AsyncSession,
    *,
    staff_user_id: uuid.UUID,
    grace_id: uuid.UUID,
    perf_date: str,
    reason: str,
) -> None:
    await notify_agent(
        db,
        staff_user_id=staff_user_id,
        notification_type=NotificationType.AGENT_GRACE_GRANTED,
        title="Grace Granted",
        message=f"Grace was granted for your missed target on {perf_date}. Reason: {reason}",
        related_entity_type="agent_grace",
        related_entity_id=str(grace_id),
        meta={"perf_date": perf_date},
    )


async def notify_agent_salary_calculated(
    db: AsyncSession,
    *,
    staff_user_id: uuid.UUID,
    salary_record_id: uuid.UUID,
    year: int,
    month: int,
    final_salary: str,
) -> None:
    await notify_agent(
        db,
        staff_user_id=staff_user_id,
        notification_type=NotificationType.AGENT_SALARY_CALCULATED,
        title="Monthly Salary Calculated",
        message=(
            f"Your salary for {month:02d}/{year} has been calculated: ₹{final_salary}. "
            "Pending Super Admin confirmation."
        ),
        related_entity_type="agent_salary_record",
        related_entity_id=str(salary_record_id),
        meta={"year": year, "month": month, "final_salary": final_salary},
        idempotency_key=(
            f"{NotificationType.AGENT_SALARY_CALCULATED}:salary:{salary_record_id}"
        ),
    )


async def notify_agent_salary_adjusted(
    db: AsyncSession,
    *,
    staff_user_id: uuid.UUID,
    salary_record_id: uuid.UUID,
    year: int,
    month: int,
    final_salary: str,
) -> None:
    await notify_agent(
        db,
        staff_user_id=staff_user_id,
        notification_type=NotificationType.AGENT_SALARY_ADJUSTED,
        title="Salary Adjusted",
        message=(
            f"Your salary for {month:02d}/{year} was adjusted. "
            f"Updated amount: ₹{final_salary}."
        ),
        related_entity_type="agent_salary_record",
        related_entity_id=str(salary_record_id),
        meta={"year": year, "month": month, "final_salary": final_salary},
    )


# ── Admin notifications ───────────────────────────────────────────────────────

async def notify_admin_deposit_pending(
    db: AsyncSession,
    *,
    deposit_id: uuid.UUID,
    user_id: uuid.UUID,
    amount: str,
) -> None:
    await notify_admin(
        db,
        notification_type=NotificationType.ADMIN_DEPOSIT_PENDING,
        title="New Deposit Awaiting Approval",
        message=f"A manual deposit of ₹{amount} is pending review.",
        related_entity_type="deposit",
        related_entity_id=str(deposit_id),
        meta={"amount": amount, "user_id": str(user_id)},
    )


async def notify_admin_withdrawal_pending(
    db: AsyncSession,
    *,
    withdrawal_id: uuid.UUID,
    user_id: uuid.UUID,
    amount: str,
) -> None:
    await notify_admin(
        db,
        notification_type=NotificationType.ADMIN_WITHDRAWAL_PENDING,
        title="New Withdrawal Request",
        message=f"A withdrawal request of ₹{amount} is awaiting processing.",
        related_entity_type="withdrawal",
        related_entity_id=str(withdrawal_id),
        meta={"amount": amount, "user_id": str(user_id)},
    )


async def notify_admin_tournament_cancelled(
    db: AsyncSession,
    *,
    tournament_id: uuid.UUID,
    tournament_title: str,
    cancelled_by: str,
) -> None:
    await notify_admin(
        db,
        notification_type=NotificationType.ADMIN_TOURNAMENT_CANCELLED,
        title="Tournament Cancelled",
        message=f"'{tournament_title}' was cancelled by {cancelled_by}.",
        related_entity_type="tournament",
        related_entity_id=str(tournament_id),
    )


async def notify_admin_result_pending_verification(
    db: AsyncSession,
    *,
    tournament_id: uuid.UUID,
    tournament_title: str,
) -> None:
    await notify_admin(
        db,
        notification_type=NotificationType.ADMIN_RESULT_PENDING_VERIFY,
        title="Results Require Verification",
        message=f"Results for '{tournament_title}' have been entered and are awaiting verification.",
        related_entity_type="tournament",
        related_entity_id=str(tournament_id),
        meta={"tournament_title": tournament_title},
    )


async def notify_admin_screenshot_submitted(
    db: AsyncSession,
    *,
    tournament_id: uuid.UUID,
    tournament_title: str,
    screenshot_id: uuid.UUID,
) -> None:
    await notify_admin(
        db,
        notification_type=NotificationType.ADMIN_SCREENSHOT_SUBMITTED,
        title="Screenshot Proof Submitted",
        message=f"A screenshot was uploaded for '{tournament_title}'.",
        related_entity_type="screenshot",
        related_entity_id=str(screenshot_id),
    )


async def notify_admin_system_error(
    db: AsyncSession,
    *,
    error_summary: str,
    job_name: str | None = None,
) -> None:
    msg = f"A system error occurred."
    if job_name:
        msg = f"Scheduler job '{job_name}' encountered an error: {error_summary[:300]}"
    await notify_admin(
        db,
        notification_type=NotificationType.ADMIN_SYSTEM_ERROR,
        title="System Error",
        message=msg,
    )
