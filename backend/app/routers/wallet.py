"""
Player wallet routes — authenticated players only.

  GET  /api/wallet/me                     — wallet summary (all balances)
  GET  /api/wallet/me/transactions        — paginated ledger history
  GET  /api/wallet/me/referral            — referral code + incoming referral status
  GET  /api/wallet/me/referral/list       — paginated list of outgoing referrals
  GET  /api/wallet/me/referral/stats      — referral reward statistics

Security:
  - Every endpoint requires a valid player JWT (get_current_user).
  - A player can ONLY access their own wallet data.
  - No balance modification is possible via these routes.
  - Transaction history does not expose idempotency keys, balance_before,
    created_by_id, or internal metadata.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user
from app.models.referral import ReferralRelationship
from app.models.user import User
from app.models.wallet import WalletTransaction
from app.schemas.wallet import (
    ReferralInfoResponse,
    ReferralItemResponse,
    ReferralListResponse,
    WalletSummaryResponse,
    WalletTransactionListResponse,
    WalletTransactionResponse,
)
from app.services import referral_service, wallet_service
from app.services.referral_service import REFERRAL_REWARD_AMOUNT

router = APIRouter(prefix="/api/wallet", tags=["Player — Wallet"])


# ── Wallet summary ────────────────────────────────────────────────────────────

@router.get(
    "/me",
    response_model=WalletSummaryResponse,
    summary="Get your wallet balances",
)
async def get_my_wallet(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> WalletSummaryResponse:
    """
    Returns all balance columns for the authenticated player.
    deposit_balance  — withdrawable real-money balance.
    winning_balance  — tournament prizes and kill rewards.
    referral_balance — non-withdrawable referral bonuses.
    """
    wallet = await wallet_service.get_wallet(db, current_user.id)
    return WalletSummaryResponse.from_wallet(wallet)


# ── Transaction history ───────────────────────────────────────────────────────

@router.get(
    "/me/transactions",
    response_model=WalletTransactionListResponse,
    summary="Get your wallet transaction history",
)
async def get_my_transactions(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    tx_type: str | None = Query(
        default=None,
        description="Filter by transaction type (e.g. deposit, referral_bonus, entry_fee)",
    ),
    balance_column: str | None = Query(
        default=None,
        description="Filter by balance column: deposit | winning | referral",
    ),
) -> WalletTransactionListResponse:
    """
    Paginated immutable ledger of all wallet transactions for the current player.
    Ordered newest-first. Optional type/balance_column filters.
    """
    wallet = await wallet_service.get_wallet(db, current_user.id)

    base_q = select(WalletTransaction).where(
        WalletTransaction.wallet_id == wallet.id
    )

    if tx_type:
        base_q = base_q.where(WalletTransaction.type == tx_type)
    if balance_column:
        base_q = base_q.where(WalletTransaction.balance_column == balance_column)

    # Total count
    count_result = await db.execute(
        select(func.count()).select_from(base_q.subquery())
    )
    total = count_result.scalar_one()

    # Paginated rows
    result = await db.execute(
        base_q
        .order_by(WalletTransaction.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    txs = list(result.scalars().all())

    return WalletTransactionListResponse(
        items=[WalletTransactionResponse.from_tx(tx) for tx in txs],
        total=total,
        limit=limit,
        offset=offset,
    )


# ── Referral info ─────────────────────────────────────────────────────────────

@router.get(
    "/me/referral",
    response_model=ReferralInfoResponse,
    summary="Get your referral code and referral status",
)
async def get_my_referral_info(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ReferralInfoResponse:
    """
    Returns:
    - Your unique referral code to share with others.
    - Whether you yourself were referred (and the current reward status).
    - Summary counts of referrals you've made.
    """
    # Outgoing referral stats (referrals this user made)
    stats = await referral_service.get_referral_stats(db, current_user.id)

    # Incoming referral (was this user referred by someone?)
    incoming = await referral_service.get_referral_for_user(db, current_user.id)

    referred_by_username: str | None = None
    referral_status: str | None = None

    if incoming is not None:
        # Look up referrer username
        result = await db.execute(
            select(User.username).where(User.id == incoming.referrer_id)
        )
        referred_by_username = result.scalar_one_or_none()

        if incoming.referred_user_rewarded:
            referral_status = "rewarded"
        elif incoming.first_tournament_completed:
            referral_status = "completed"
        else:
            referral_status = "pending"

    return ReferralInfoResponse(
        my_referral_code=current_user.referral_code,
        referred_by_username=referred_by_username,
        referral_status=referral_status,
        reward_per_referral=str(REFERRAL_REWARD_AMOUNT),
        total_referrals_made=stats["total_referrals"],
        pending_referrals=stats["pending_referrals"],
        completed_referrals=stats["completed_referrals"],
        rewarded_referrals=stats["rewarded_referrals"],
        total_bonus_earned=stats["total_bonus_earned"],
    )


# ── Outgoing referral list ────────────────────────────────────────────────────

@router.get(
    "/me/referral/list",
    response_model=ReferralListResponse,
    summary="List players you have referred",
)
async def get_my_referral_list(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> ReferralListResponse:
    """
    Paginated list of users referred by the current player.
    Shows first-tournament status and reward status per referral.
    Does NOT expose the referred user's wallet details.
    """
    rels, total = await referral_service.get_referrals_made_by_user(
        db,
        referrer_id=current_user.id,
        limit=limit,
        offset=offset,
    )

    if not rels:
        return ReferralListResponse(items=[], total=0, limit=limit, offset=offset)

    # Batch-load referred usernames
    referred_ids = [r.referred_user_id for r in rels]
    result = await db.execute(
        select(User.id, User.username).where(User.id.in_(referred_ids))
    )
    username_map: dict[uuid.UUID, str] = {row.id: row.username for row in result}

    items = [
        ReferralItemResponse(
            referral_id=rel.id,
            referred_username=username_map.get(rel.referred_user_id, "unknown"),
            referral_code_used=rel.referral_code_used,
            created_at=rel.created_at,
            first_tournament_completed=rel.first_tournament_completed,
            first_tournament_completed_at=rel.first_tournament_completed_at,
            referrer_rewarded=rel.referrer_rewarded,
            referrer_rewarded_at=rel.referrer_rewarded_at,
        )
        for rel in rels
    ]

    return ReferralListResponse(
        items=items,
        total=total,
        limit=limit,
        offset=offset,
    )


# ── Referral stats summary ────────────────────────────────────────────────────

@router.get(
    "/me/referral/stats",
    summary="Get referral reward statistics",
)
async def get_my_referral_stats(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Returns aggregate referral statistics:
    - total_referrals, completed, rewarded, pending counts
    - total_bonus_earned (from referral rewards)
    - reward_per_referral (server-side constant)
    """
    return await referral_service.get_referral_stats(db, current_user.id)
