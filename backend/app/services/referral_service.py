"""
Referral service — manages the full referral lifecycle.

Responsibilities:
  1. create_referral_relationship()
       Called at registration when a referral code is provided.
       Records the immutable referral link. No rewards issued yet.

  2. activate_referral_rewards()
       Called by the tournament system when a referred user completes
       their FIRST tournament participation.
       - Marks first_tournament_completed on the ReferralRelationship.
       - Credits ₹20 referral_bonus to BOTH referrer and referred user.
       - Uses idempotency keys to guarantee exactly-once reward delivery.
       - Records audit log entries for both credits.

  3. get_referral_for_user()
       Returns the ReferralRelationship where the given user was the referred party.

  4. get_referrals_made_by_user()
       Returns all ReferralRelationships where the given user was the referrer,
       with pagination.

Safety guarantees:
  - Self-referral is blocked at the model level (CHECK constraint) and here.
  - Rewards are idempotent: calling activate_referral_rewards() twice for the
    same relationship raises ReferralAlreadyRewardedError on the second call.
  - Reward amount is a server-side constant (REFERRAL_REWARD_AMOUNT). The
    caller never supplies an amount.
  - All DB mutations happen inside the caller's transaction; the caller commits.
"""
from __future__ import annotations

import logging
import uuid
from datetime import timezone
from decimal import Decimal

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    ReferralAlreadyRewardedError,
    ReferralConditionNotMetError,
)
from app.models.audit_log import ActorType
from app.models.referral import ReferralRelationship
from app.models.user import User
from app.models._types import utcnow
from app.services import audit_service
from app.services import wallet_service

logger = logging.getLogger("auraplayff.referral")

# Both referrer and referred user each receive this fixed bonus (INR)
REFERRAL_REWARD_AMOUNT = Decimal("20.00")


# ── Creation ──────────────────────────────────────────────────────────────────

async def create_referral_relationship(
    db: AsyncSession,
    *,
    referrer: User,
    referred_user: User,
    referral_code_used: str,
) -> ReferralRelationship:
    """
    Persist the referral link immediately after a successful registration.

    Must be called inside an active DB transaction (caller commits).
    Self-referral is prevented by the DB CHECK constraint; the application
    guard in the auth router fires first for a friendlier error message.
    """
    rel = ReferralRelationship(
        referrer_id=referrer.id,
        referred_user_id=referred_user.id,
        referral_code_used=referral_code_used,
    )
    db.add(rel)
    await db.flush([rel])
    logger.info(
        "Referral relationship created: referrer=%s referred=%s",
        referrer.id,
        referred_user.id,
    )
    return rel


# ── Reward activation ─────────────────────────────────────────────────────────

async def activate_referral_rewards(
    db: AsyncSession,
    *,
    referral_id: uuid.UUID,
    triggered_by: str = "system",
) -> tuple[ReferralRelationship, "WalletTransaction", "WalletTransaction"]:  # type: ignore[name-defined]  # noqa: F821
    """
    Credit ₹20 referral bonus to both referrer and referred user.

    Preconditions (enforced here):
      - ReferralRelationship must exist.
      - first_tournament_completed must be True.
      - Neither reward must have been credited already (full idempotency).

    The caller (tournament service) is responsible for:
      1. Setting referral.first_tournament_completed = True before calling this.
      2. Being inside an active DB transaction and committing after.

    Idempotency keys:
      referrer  → "referral_bonus:<referral_id>:referrer"
      referred  → "referral_bonus:<referral_id>:referred"

    Returns (relationship, referrer_tx, referred_tx).
    Raises ReferralAlreadyRewardedError if both rewards already credited.
    Raises ReferralConditionNotMetError if first_tournament_completed is False.
    """
    # Load relationship with FOR UPDATE to prevent concurrent double-credit
    result = await db.execute(
        select(ReferralRelationship)
        .where(ReferralRelationship.id == referral_id)
        .with_for_update()
    )
    rel: ReferralRelationship | None = result.scalar_one_or_none()
    if rel is None:
        from app.core.exceptions import NotFoundError
        raise NotFoundError("ReferralRelationship")

    # Guard: both rewards already issued
    if rel.referrer_rewarded and rel.referred_user_rewarded:
        raise ReferralAlreadyRewardedError()

    # Guard: condition not yet satisfied
    if not rel.first_tournament_completed:
        raise ReferralConditionNotMetError()

    now = utcnow()

    # ── Credit referrer ───────────────────────────────────────────────────────
    referrer_tx = None
    if not rel.referrer_rewarded:
        referrer_tx = await wallet_service.credit_referral_bonus(
            db,
            user_id=rel.referrer_id,
            amount=REFERRAL_REWARD_AMOUNT,
            idempotency_key=f"referral_bonus:{rel.id}:referrer",
            reference_id=rel.id,
            description=(
                f"Referral bonus — your referral completed their first tournament."
            ),
            meta={"referral_id": str(rel.id), "role": "referrer"},
        )
        rel.referrer_rewarded = True
        rel.referrer_rewarded_at = now

        await audit_service.log(
            db=db,
            actor_type=ActorType.system,
            actor_id=triggered_by,
            action="referral.referrer_bonus_credited",
            entity_type="referral_relationship",
            entity_id=str(rel.id),
            new_value={
                "referrer_id": str(rel.referrer_id),
                "amount": str(REFERRAL_REWARD_AMOUNT),
                "wallet_tx_id": str(referrer_tx.id),
            },
        )

    # ── Credit referred user ──────────────────────────────────────────────────
    referred_tx = None
    if not rel.referred_user_rewarded:
        referred_tx = await wallet_service.credit_referral_bonus(
            db,
            user_id=rel.referred_user_id,
            amount=REFERRAL_REWARD_AMOUNT,
            idempotency_key=f"referral_bonus:{rel.id}:referred",
            reference_id=rel.id,
            description=(
                f"Referral bonus — you completed your first tournament."
            ),
            meta={"referral_id": str(rel.id), "role": "referred"},
        )
        rel.referred_user_rewarded = True
        rel.referred_user_rewarded_at = now

        await audit_service.log(
            db=db,
            actor_type=ActorType.system,
            actor_id=triggered_by,
            action="referral.referred_bonus_credited",
            entity_type="referral_relationship",
            entity_id=str(rel.id),
            new_value={
                "referred_user_id": str(rel.referred_user_id),
                "amount": str(REFERRAL_REWARD_AMOUNT),
                "wallet_tx_id": str(referred_tx.id),
            },
        )

    await db.flush([rel])
    return rel, referrer_tx, referred_tx


async def mark_first_tournament_completed(
    db: AsyncSession,
    *,
    referred_user_id: uuid.UUID,
    triggered_by: str = "system",
) -> ReferralRelationship | None:
    """
    Called by the tournament service when a referred user finishes their first
    tournament participation.

    Steps:
      1. Look up the ReferralRelationship for this user.
      2. If none exists (user was not referred), return None — no-op.
      3. If already marked complete, return the relationship — no-op.
      4. Otherwise mark it and call activate_referral_rewards().

    This is the single entry-point the tournament phase will call. The
    tournament service does NOT need to know about the internal reward logic.

    The caller is inside a transaction and will commit.
    """
    result = await db.execute(
        select(ReferralRelationship)
        .where(ReferralRelationship.referred_user_id == referred_user_id)
        .with_for_update()
    )
    rel: ReferralRelationship | None = result.scalar_one_or_none()

    if rel is None:
        # User was not referred — nothing to do
        return None

    if rel.first_tournament_completed:
        # Already triggered — idempotent no-op
        logger.debug(
            "first_tournament_completed already set for referral %s — skipping",
            rel.id,
        )
        return rel

    # Mark the condition as satisfied
    rel.first_tournament_completed = True
    rel.first_tournament_completed_at = utcnow()
    await db.flush([rel])

    # Credit both rewards
    await activate_referral_rewards(
        db,
        referral_id=rel.id,
        triggered_by=triggered_by,
    )

    logger.info(
        "Referral rewards activated for referral %s (referrer=%s, referred=%s)",
        rel.id,
        rel.referrer_id,
        rel.referred_user_id,
    )
    return rel


# ── Read helpers ──────────────────────────────────────────────────────────────

async def get_referral_for_user(
    db: AsyncSession,
    user_id: uuid.UUID,
) -> ReferralRelationship | None:
    """
    Return the ReferralRelationship where `user_id` was the referred party.
    Returns None if the user registered without a referral code.
    """
    result = await db.execute(
        select(ReferralRelationship)
        .where(ReferralRelationship.referred_user_id == user_id)
    )
    return result.scalar_one_or_none()


async def get_referrals_made_by_user(
    db: AsyncSession,
    referrer_id: uuid.UUID,
    *,
    limit: int = 20,
    offset: int = 0,
) -> tuple[list[ReferralRelationship], int]:
    """
    Return (relationships, total_count) for all referrals made by `referrer_id`.
    Ordered by created_at DESC (most recent first).
    """
    base_q = (
        select(ReferralRelationship)
        .where(ReferralRelationship.referrer_id == referrer_id)
    )
    count_result = await db.execute(
        select(func.count()).select_from(base_q.subquery())
    )
    total = count_result.scalar_one()

    result = await db.execute(
        base_q
        .order_by(ReferralRelationship.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    rows = list(result.scalars().all())
    return rows, total


async def get_referral_stats(
    db: AsyncSession,
    referrer_id: uuid.UUID,
) -> dict:
    """
    Return summary referral statistics for a user's own referral dashboard.
    """
    result = await db.execute(
        select(
            func.count().label("total_referrals"),
            func.sum(
                # 1 if first tournament completed, else 0
                func.cast(
                    ReferralRelationship.first_tournament_completed,
                    type_=__import__("sqlalchemy").Integer,
                )
            ).label("completed_referrals"),
            func.sum(
                func.cast(
                    ReferralRelationship.referrer_rewarded,
                    type_=__import__("sqlalchemy").Integer,
                )
            ).label("rewarded_count"),
        )
        .where(ReferralRelationship.referrer_id == referrer_id)
    )
    row = result.one()
    total = row.total_referrals or 0
    completed = int(row.completed_referrals or 0)
    rewarded = int(row.rewarded_count or 0)
    pending = total - completed

    return {
        "total_referrals": total,
        "completed_referrals": completed,
        "rewarded_referrals": rewarded,
        "pending_referrals": pending,
        "total_bonus_earned": str(REFERRAL_REWARD_AMOUNT * rewarded),
        "reward_per_referral": str(REFERRAL_REWARD_AMOUNT),
    }
