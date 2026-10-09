"""
Admin wallet management routes.

  GET  /api/admin/wallets/{user_id}                — view a player's wallet
  GET  /api/admin/wallets/{user_id}/transactions   — paginated ledger (full audit view)
  POST /api/admin/wallets/{user_id}/adjust         — controlled balance adjustment
  POST /api/admin/wallets/{user_id}/freeze         — freeze wallet
  POST /api/admin/wallets/{user_id}/unfreeze       — unfreeze wallet
  GET  /api/admin/referrals                        — paginated referral list (all users)
  GET  /api/admin/referrals/{referral_id}          — single referral detail
  GET  /api/admin/wallets/{user_id}/referral       — referral info for a specific user

Permission requirements:
  - VIEW endpoints:   USER_VIEW
  - ADJUST endpoint:  WALLET_MANUAL_ADJUST
  - FREEZE endpoints: WALLET_MANUAL_ADJUST
  - Super admin always passes all checks.

Security notes:
  - Admin cannot see other staff wallets (staff have no wallets).
  - Every manual adjustment creates both a WalletTransaction ledger entry
    AND an AuditLog record.
  - Adjustment amount is validated server-side; frontend values are never trusted.
  - idempotency_key for adjustments = "admin_adjust:<staff_id>:<audit_log_uuid>"
    Generated server-side; never supplied by the admin request body.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError, PermissionDeniedError
from app.core.permissions import Permission
from app.database import get_db
from app.dependencies import get_current_staff
from app.models.audit_log import ActorType
from app.models.permission import Permission as PermModel, StaffUserPermission
from app.models.referral import ReferralRelationship
from app.models.staff_user import StaffRole, StaffUser
from app.models.user import User
from app.models.wallet import BalanceColumn, TransactionDirection, WalletTransaction
from app.schemas.wallet import (
    AdminAdjustRequest,
    AdminAdjustResponse,
    AdminReferralItemResponse,
    AdminReferralListResponse,
    AdminTransactionListResponse,
    AdminWalletSummaryResponse,
    AdminWalletTransactionResponse,
    WalletFreezeResponse,
)
from app.services import audit_service, wallet_service

router = APIRouter(prefix="/api/admin/wallets", tags=["Admin — Wallets"])
referral_router = APIRouter(prefix="/api/admin/referrals", tags=["Admin — Referrals"])


# ── Permission helpers ────────────────────────────────────────────────────────

async def _require_permission(
    staff: StaffUser,
    db: AsyncSession,
    perm: Permission,
) -> None:
    """Inline permission check — raises PermissionDeniedError if not granted."""
    if staff.role == StaffRole.super_admin:
        return
    # BUG #7 FIX: joinedload replaces N separate db.get() calls
    from sqlalchemy.orm import joinedload
    result = await db.execute(
        select(StaffUserPermission)
        .options(joinedload(StaffUserPermission.permission))
        .where(StaffUserPermission.staff_user_id == staff.id)
    )
    grants = result.unique().scalars().all()
    granted_codes: set[str] = {
        g.permission.code for g in grants if g.permission is not None
    }
    if perm.value not in granted_codes:
        raise PermissionDeniedError(perm.value)


async def _load_user(db: AsyncSession, user_id: uuid.UUID) -> User:
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise NotFoundError("User")
    return user


def _get_ip(request: Request) -> str | None:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


# ── View wallet ───────────────────────────────────────────────────────────────

@router.get(
    "/{user_id}",
    response_model=AdminWalletSummaryResponse,
    summary="View a player's wallet (Admin)",
)
async def admin_get_wallet(
    user_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_staff: StaffUser = Depends(get_current_staff),
) -> AdminWalletSummaryResponse:
    """Full wallet summary including withdrawal_hold and frozen state."""
    await _require_permission(current_staff, db, Permission.USER_VIEW)
    user = await _load_user(db, user_id)
    wallet = await wallet_service.get_wallet(db, user_id)
    return AdminWalletSummaryResponse.from_wallet_user(wallet, user.username)


# ── View transactions ─────────────────────────────────────────────────────────

@router.get(
    "/{user_id}/transactions",
    response_model=AdminTransactionListResponse,
    summary="View a player's full transaction ledger (Admin)",
)
async def admin_get_transactions(
    user_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_staff: StaffUser = Depends(get_current_staff),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    tx_type: str | None = Query(default=None),
    balance_column: str | None = Query(default=None),
) -> AdminTransactionListResponse:
    """
    Full ledger including balance_before, idempotency_key, meta, and created_by_id.
    Used for support investigations and financial audits.
    """
    await _require_permission(current_staff, db, Permission.USER_VIEW)
    wallet = await wallet_service.get_wallet(db, user_id)

    base_q = select(WalletTransaction).where(
        WalletTransaction.wallet_id == wallet.id
    )
    if tx_type:
        base_q = base_q.where(WalletTransaction.type == tx_type)
    if balance_column:
        base_q = base_q.where(WalletTransaction.balance_column == balance_column)

    count_result = await db.execute(
        select(func.count()).select_from(base_q.subquery())
    )
    total = count_result.scalar_one()

    result = await db.execute(
        base_q
        .order_by(WalletTransaction.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    txs = list(result.scalars().all())

    return AdminTransactionListResponse(
        items=[AdminWalletTransactionResponse.from_tx(tx) for tx in txs],
        total=total,
        limit=limit,
        offset=offset,
    )


# ── Manual adjustment ─────────────────────────────────────────────────────────

@router.post(
    "/{user_id}/adjust",
    response_model=AdminAdjustResponse,
    summary="Manually adjust a player's wallet balance (Admin)",
)
async def admin_adjust_wallet(
    user_id: uuid.UUID,
    body: AdminAdjustRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_staff: StaffUser = Depends(get_current_staff),
) -> AdminAdjustResponse:
    """
    Controlled balance adjustment with mandatory reason.

    Rules enforced here:
    - Requires WALLET_MANUAL_ADJUST permission.
    - Amount is validated server-side (body.amount comes from Pydantic schema).
    - Idempotency key is generated server-side: "admin_adjust:<staff_id>:<new_uuid>"
    - Every adjustment creates both a WalletTransaction and an AuditLog record.
    - Cannot produce a negative balance (enforced by wallet_service).
    - Cannot adjust the withdrawal_hold column directly (operational lock-up only).
    """
    await _require_permission(current_staff, db, Permission.WALLET_MANUAL_ADJUST)

    try:
        col = BalanceColumn(body.balance_column)
    except ValueError:
        from app.core.exceptions import InvalidAmountError
        raise InvalidAmountError(f"Invalid balance_column: {body.balance_column}")

    try:
        direction = TransactionDirection(body.direction)
    except ValueError:
        from app.core.exceptions import InvalidAmountError
        raise InvalidAmountError(f"Invalid direction: {body.direction}")

    user = await _load_user(db, user_id)

    # Generate a server-side idempotency key (stable for this adjustment request)
    idempotency_key = f"admin_adjust:{current_staff.id}:{uuid.uuid4()}"

    async with db.begin_nested():
        # Snapshot balances before for audit
        wallet_before = await wallet_service.get_wallet(db, user_id)
        before_balance = {
            "deposit": str(wallet_before.deposit_balance),
            "winning": str(wallet_before.winning_balance),
            "referral": str(wallet_before.referral_balance),
        }

        tx = await wallet_service.admin_adjust(
            db,
            user_id=user_id,
            balance_col=col,
            direction=direction,
            amount=body.amount,
            idempotency_key=idempotency_key,
            created_by_id=current_staff.id,
            description=f"[Admin adjustment] {body.reason}",
            meta={
                "adjusted_by": current_staff.agent_id,
                "reason": body.reason,
            },
        )

        # Reload wallet for new balance snapshot
        wallet_after = await wallet_service.get_wallet(db, user_id)
        after_balance = {
            "deposit": str(wallet_after.deposit_balance),
            "winning": str(wallet_after.winning_balance),
            "referral": str(wallet_after.referral_balance),
        }

        await audit_service.log(
            db=db,
            actor_type=ActorType.staff,
            actor_id=str(current_staff.id),
            action="wallet.admin_adjustment",
            entity_type="wallet",
            entity_id=str(wallet_before.id),
            old_value={
                "balances": before_balance,
                "target_user": str(user_id),
                "username": user.username,
            },
            new_value={
                "balances": after_balance,
                "column": body.balance_column,
                "direction": body.direction,
                "amount": str(body.amount),
                "reason": body.reason,
                "transaction_id": str(tx.id),
            },
            ip_address=_get_ip(request),
            user_agent=request.headers.get("user-agent"),
        )

    await db.commit()

    # BUG #14 FIX: wallet_after was loaded inside the begin_nested() savepoint.
    # After db.commit() SQLAlchemy expires all ORM objects — accessing
    # wallet_after.deposit_balance etc. triggers implicit lazy loads which raise
    # MissingGreenlet in async context.
    # Fix: use the already-captured after_balance dict (built inside the savepoint)
    # and the already-captured tx.id (PK survives expiry, but use snapshot for safety).
    col_key = col.value  # e.g. "deposit", "winning", "referral"
    new_balance = after_balance.get(col_key, "0.00")
    tx_id = tx.id  # PK — cached by SQLAlchemy identity map, safe to read

    return AdminAdjustResponse(
        message="Wallet adjusted successfully.",
        transaction_id=tx_id,
        balance_column=body.balance_column,
        direction=body.direction,
        amount=str(body.amount),
        new_balance=new_balance,
    )


# ── Freeze / unfreeze ─────────────────────────────────────────────────────────

@router.post(
    "/{user_id}/freeze",
    response_model=WalletFreezeResponse,
    summary="Freeze a player's wallet (Admin)",
)
async def admin_freeze_wallet(
    user_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_staff: StaffUser = Depends(get_current_staff),
) -> WalletFreezeResponse:
    """
    Prevents all future transactions on this wallet.
    Used for fraud investigations. Requires WALLET_MANUAL_ADJUST.
    """
    await _require_permission(current_staff, db, Permission.WALLET_MANUAL_ADJUST)
    user = await _load_user(db, user_id)
    wallet = await wallet_service.freeze_wallet(db, user_id)

    await audit_service.log(
        db=db,
        actor_type=ActorType.staff,
        actor_id=str(current_staff.id),
        action="wallet.frozen",
        entity_type="wallet",
        entity_id=str(wallet.id),
        new_value={"target_user": str(user_id), "username": user.username},
        ip_address=_get_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    await db.commit()

    return WalletFreezeResponse(
        message="Wallet frozen successfully.",
        wallet_id=wallet.id,
        is_frozen=wallet.is_frozen,
    )


@router.post(
    "/{user_id}/unfreeze",
    response_model=WalletFreezeResponse,
    summary="Unfreeze a player's wallet (Admin)",
)
async def admin_unfreeze_wallet(
    user_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_staff: StaffUser = Depends(get_current_staff),
) -> WalletFreezeResponse:
    """Restores transaction capability. Requires WALLET_MANUAL_ADJUST."""
    await _require_permission(current_staff, db, Permission.WALLET_MANUAL_ADJUST)
    user = await _load_user(db, user_id)
    wallet = await wallet_service.unfreeze_wallet(db, user_id)

    await audit_service.log(
        db=db,
        actor_type=ActorType.staff,
        actor_id=str(current_staff.id),
        action="wallet.unfrozen",
        entity_type="wallet",
        entity_id=str(wallet.id),
        new_value={"target_user": str(user_id), "username": user.username},
        ip_address=_get_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    await db.commit()

    return WalletFreezeResponse(
        message="Wallet unfrozen successfully.",
        wallet_id=wallet.id,
        is_frozen=wallet.is_frozen,
    )


# ── Referral info for a specific user ─────────────────────────────────────────

@router.get(
    "/{user_id}/referral",
    response_model=AdminReferralItemResponse | dict,
    summary="View referral relationship for a specific user (Admin)",
)
async def admin_get_user_referral(
    user_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_staff: StaffUser = Depends(get_current_staff),
) -> AdminReferralItemResponse | dict:
    """
    Returns the ReferralRelationship where this user was the referred party.
    Returns {"message": "User was not referred."} if no referral exists.
    """
    await _require_permission(current_staff, db, Permission.USER_VIEW)

    from app.services import referral_service
    rel = await referral_service.get_referral_for_user(db, user_id)
    if rel is None:
        return {"message": "User was not referred."}

    # Load both usernames
    result = await db.execute(
        select(User.id, User.username).where(
            User.id.in_([rel.referrer_id, rel.referred_user_id])
        )
    )
    uname_map: dict[uuid.UUID, str] = {row.id: row.username for row in result}

    return AdminReferralItemResponse(
        referral_id=rel.id,
        referrer_id=rel.referrer_id,
        referrer_username=uname_map.get(rel.referrer_id, "unknown"),
        referred_user_id=rel.referred_user_id,
        referred_username=uname_map.get(rel.referred_user_id, "unknown"),
        referral_code_used=rel.referral_code_used,
        created_at=rel.created_at,
        first_tournament_completed=rel.first_tournament_completed,
        first_tournament_completed_at=rel.first_tournament_completed_at,
        referrer_rewarded=rel.referrer_rewarded,
        referrer_rewarded_at=rel.referrer_rewarded_at,
        referred_user_rewarded=rel.referred_user_rewarded,
        referred_user_rewarded_at=rel.referred_user_rewarded_at,
    )


# ── Global referral list ──────────────────────────────────────────────────────

@referral_router.get(
    "",
    response_model=AdminReferralListResponse,
    summary="List all referral relationships (Admin)",
)
async def admin_list_referrals(
    db: AsyncSession = Depends(get_db),
    current_staff: StaffUser = Depends(get_current_staff),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    rewarded_only: bool = Query(default=False),
    pending_only: bool = Query(default=False),
) -> AdminReferralListResponse:
    """
    Paginated list of all referral relationships across the platform.
    Filters: rewarded_only, pending_only (mutually exclusive; pending_only wins).
    """
    await _require_permission(current_staff, db, Permission.USER_VIEW)

    base_q = select(ReferralRelationship)
    if pending_only:
        base_q = base_q.where(
            ReferralRelationship.first_tournament_completed == False  # noqa: E712
        )
    elif rewarded_only:
        base_q = base_q.where(
            ReferralRelationship.referrer_rewarded == True  # noqa: E712
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
    rels = list(result.scalars().all())

    if not rels:
        return AdminReferralListResponse(items=[], total=total, limit=limit, offset=offset)

    # Batch-load all involved usernames
    all_user_ids = list(
        {r.referrer_id for r in rels} | {r.referred_user_id for r in rels}
    )
    uname_result = await db.execute(
        select(User.id, User.username).where(User.id.in_(all_user_ids))
    )
    uname_map: dict[uuid.UUID, str] = {row.id: row.username for row in uname_result}

    items = [
        AdminReferralItemResponse(
            referral_id=rel.id,
            referrer_id=rel.referrer_id,
            referrer_username=uname_map.get(rel.referrer_id, "unknown"),
            referred_user_id=rel.referred_user_id,
            referred_username=uname_map.get(rel.referred_user_id, "unknown"),
            referral_code_used=rel.referral_code_used,
            created_at=rel.created_at,
            first_tournament_completed=rel.first_tournament_completed,
            first_tournament_completed_at=rel.first_tournament_completed_at,
            referrer_rewarded=rel.referrer_rewarded,
            referrer_rewarded_at=rel.referrer_rewarded_at,
            referred_user_rewarded=rel.referred_user_rewarded,
            referred_user_rewarded_at=rel.referred_user_rewarded_at,
        )
        for rel in rels
    ]

    return AdminReferralListResponse(
        items=items,
        total=total,
        limit=limit,
        offset=offset,
    )


@referral_router.get(
    "/{referral_id}",
    response_model=AdminReferralItemResponse,
    summary="Get a single referral relationship by ID (Admin)",
)
async def admin_get_referral(
    referral_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_staff: StaffUser = Depends(get_current_staff),
) -> AdminReferralItemResponse:
    await _require_permission(current_staff, db, Permission.USER_VIEW)

    result = await db.execute(
        select(ReferralRelationship).where(ReferralRelationship.id == referral_id)
    )
    rel = result.scalar_one_or_none()
    if rel is None:
        raise NotFoundError("ReferralRelationship")

    uname_result = await db.execute(
        select(User.id, User.username).where(
            User.id.in_([rel.referrer_id, rel.referred_user_id])
        )
    )
    uname_map: dict[uuid.UUID, str] = {row.id: row.username for row in uname_result}

    return AdminReferralItemResponse(
        referral_id=rel.id,
        referrer_id=rel.referrer_id,
        referrer_username=uname_map.get(rel.referrer_id, "unknown"),
        referred_user_id=rel.referred_user_id,
        referred_username=uname_map.get(rel.referred_user_id, "unknown"),
        referral_code_used=rel.referral_code_used,
        created_at=rel.created_at,
        first_tournament_completed=rel.first_tournament_completed,
        first_tournament_completed_at=rel.first_tournament_completed_at,
        referrer_rewarded=rel.referrer_rewarded,
        referrer_rewarded_at=rel.referrer_rewarded_at,
        referred_user_rewarded=rel.referred_user_rewarded,
        referred_user_rewarded_at=rel.referred_user_rewarded_at,
    )
