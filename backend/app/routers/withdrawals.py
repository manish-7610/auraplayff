"""
Player-facing withdrawal endpoints.

Routes
------
POST  /api/withdrawals                 — submit withdrawal request
GET   /api/withdrawals/{id}            — get single withdrawal (own)
GET   /api/withdrawals                 — list own withdrawals
GET   /api/withdrawals/balance-summary — wallet balance breakdown with hold info

Transaction pattern
-------------------
Do NOT use `async with db.begin()` in any endpoint here.

The `get_current_user` dependency executes a SELECT on the shared session
(to look up the User row), which causes SQLAlchemy to autobegin a transaction
automatically.  Calling `db.begin()` again on a session that already has an
active transaction raises:
    sqlalchemy.exc.InvalidRequestError: A transaction is already begun on this Session.

This exception propagates through Starlette's middleware stack uncaught, aborts
the HTTP response, and causes `fetch()` on the frontend to throw a TypeError —
which the API client converts to "Network error — check your connection."

Correct pattern (matches deposits.py, admin_deposits.py, admin_payment_config.py):
  - Read-only routes:  just query; session rolls back automatically on scope exit.
  - Mutating routes:   call `await db.commit()` after the work is done.
                       On exception, the `get_db` dependency rolls back automatically.

Security:
  - All routes require authenticated player (get_current_user).
  - Ownership enforced in service: player can only see/act on own withdrawals.
  - Status transitions are never accepted from players.
  - Balance reservation happens server-side at creation time.
"""
from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_current_user, get_db, get_client_ip
from app.models.user import User
from app.schemas.withdrawal import (
    CreateWithdrawalRequest,
    WalletWithdrawalSummary,
    WithdrawalListResponse,
    WithdrawalResponse,
)
from app.services import withdrawal_service
from app.services import wallet_service

router = APIRouter(prefix="/api/withdrawals", tags=["Withdrawals — Player"])


# ── Balance summary ───────────────────────────────────────────────────────────

@router.get(
    "/balance-summary",
    response_model=WalletWithdrawalSummary,
    summary="Wallet balance breakdown with withdrawal reservation",
)
async def get_balance_summary(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> WalletWithdrawalSummary:
    """
    Returns full wallet balance including:
    - deposit_balance, winning_balance, referral_balance
    - withdrawal_hold (amount reserved for pending withdrawals)
    - available_winning_balance (winning_balance - withdrawal_hold)

    Read-only — no db.begin() / db.commit() needed.
    The session's implicit transaction (autobegun by get_current_user's SELECT)
    is rolled back automatically when the session scope exits.
    """
    wallet = await wallet_service.get_wallet(db, current_user.id)
    return WalletWithdrawalSummary.from_wallet(wallet)


# ── Create withdrawal ─────────────────────────────────────────────────────────

@router.post(
    "",
    response_model=WithdrawalResponse,
    status_code=201,
    summary="Submit a withdrawal request",
)
async def create_withdrawal(
    body: CreateWithdrawalRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> WithdrawalResponse:
    """
    Submit a withdrawal of Winning Balance to a UPI ID.

    Minimum: ₹100 (configurable platform setting).
    Only Winning Balance is withdrawable (Deposit Balance and Referral Balance are not).

    The amount is immediately reserved from winning_balance into withdrawal_hold
    so it cannot be spent or withdrawn again while pending.

    Admin will process and pay manually. Player is notified on status change.
    """
    withdrawal = await withdrawal_service.create_withdrawal(
        db,
        user_id=current_user.id,
        amount=body.amount,
        upi_id=body.upi_id,
        ip_address=get_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    # Commit the withdrawal + wallet reservation atomically.
    # get_db rolls back automatically if commit raises.
    await db.commit()

    # Snapshot scalars before possible session expiry
    _withdrawal_id = withdrawal.id
    _amount_str    = str(withdrawal.amount)

    # Phase 7: notify player and admins (best-effort — never fail the request)
    from app.services import notification_service as ns
    try:
        await ns.notify_player_withdrawal_submitted(
            db, user_id=current_user.id,
            withdrawal_id=_withdrawal_id, amount=_amount_str,
        )
        await ns.notify_admin_withdrawal_pending(
            db, withdrawal_id=_withdrawal_id,
            user_id=current_user.id, amount=_amount_str,
        )
        await db.commit()
    except Exception:
        pass

    return WithdrawalResponse.from_withdrawal(withdrawal)


# ── Query endpoints ───────────────────────────────────────────────────────────

@router.get(
    "/{withdrawal_id}",
    response_model=WithdrawalResponse,
    summary="Get a specific withdrawal",
)
async def get_withdrawal(
    withdrawal_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> WithdrawalResponse:
    """Fetch a withdrawal by ID. Player can only view their own withdrawals."""
    withdrawal = await withdrawal_service.get_withdrawal_for_user(
        db, withdrawal_id, current_user.id
    )
    return WithdrawalResponse.from_withdrawal(withdrawal)


@router.get(
    "",
    response_model=WithdrawalListResponse,
    summary="List own withdrawals",
)
async def list_withdrawals(
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> WithdrawalListResponse:
    """Return paginated list of the authenticated player's withdrawal requests."""
    withdrawals, total = await withdrawal_service.list_withdrawals_for_user(
        db,
        user_id=current_user.id,
        limit=limit,
        offset=offset,
    )
    return WithdrawalListResponse(
        items=[WithdrawalResponse.from_withdrawal(w) for w in withdrawals],
        total=total,
        limit=limit,
        offset=offset,
    )
