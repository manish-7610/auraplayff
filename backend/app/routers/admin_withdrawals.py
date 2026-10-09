"""
Admin withdrawal management endpoints.

Routes
------
GET   /api/admin/withdrawals                      — list withdrawals (with filters)
GET   /api/admin/withdrawals/pending              — list pending withdrawals
GET   /api/admin/withdrawals/{id}                 — get withdrawal details
POST  /api/admin/withdrawals/{id}/process         — mark as processing
POST  /api/admin/withdrawals/{id}/mark-paid       — mark as paid + enter UTR
POST  /api/admin/withdrawals/{id}/reject          — reject withdrawal

Transaction pattern
-------------------
Do NOT use `async with db.begin()` in any endpoint here.

The `get_current_staff` dependency executes a SELECT on the shared session
(to look up the StaffUser row), which causes SQLAlchemy to autobegin a
transaction automatically.  Calling `db.begin()` again raises:
    sqlalchemy.exc.InvalidRequestError: A transaction is already begun on this Session.

Correct pattern (matches deposits.py, admin_deposits.py, admin_payment_config.py):
  - Read-only routes:  just query; session rolls back on scope exit.
  - Mutating routes:   call `await db.commit()` after the work.
                       `get_db` rolls back automatically on exception.

Permissions:
  withdrawal.view      — view/list withdrawals (read-only)
  withdrawal.process   — PENDING → PROCESSING
  withdrawal.mark_paid — PROCESSING → PAID (enter UTR)
  withdrawal.reject    — reject + release balance hold
  (Super Admin has all automatically)

Security:
  - Agents do NOT get financial permissions by default.
  - Status transitions are server-side only — never from frontend.
  - Balance release on rejection is atomic with status change.
  - Every action creates an audit log entry.
"""
from __future__ import annotations

import uuid
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import PermissionDeniedError
from app.core.permissions import Permission
from app.dependencies import get_current_staff, get_db, get_client_ip
from app.models.permission import Permission as PermModel, StaffUserPermission
from app.models.staff_user import StaffRole, StaffUser
from app.schemas.withdrawal import (
    AdminMarkPaidRequest,
    AdminProcessWithdrawalRequest,
    AdminRejectWithdrawalRequest,
    AdminWithdrawalListResponse,
    AdminWithdrawalResponse,
)
from app.services import withdrawal_service

router = APIRouter(prefix="/api/admin/withdrawals", tags=["Admin — Withdrawals"])


def _ip(request: Request) -> str | None:
    return get_client_ip(request)


async def _check_permission(
    staff: StaffUser, db: AsyncSession, perm: Permission
) -> None:
    """Raise PermissionDeniedError if staff does not have the permission."""
    if staff.role == StaffRole.super_admin:
        return
    from sqlalchemy.orm import joinedload
    result = await db.execute(
        select(StaffUserPermission)
        .options(joinedload(StaffUserPermission.permission))
        .where(StaffUserPermission.staff_user_id == staff.id)
    )
    grants = result.unique().scalars().all()
    granted: set[str] = {
        g.permission.code for g in grants if g.permission is not None
    }
    if perm.value not in granted:
        raise PermissionDeniedError(perm.value)


# ── List / query ──────────────────────────────────────────────────────────────

@router.get(
    "/pending",
    response_model=AdminWithdrawalListResponse,
    summary="List pending withdrawal requests",
)
async def list_pending_withdrawals(
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    staff: StaffUser = Depends(get_current_staff),
    db: AsyncSession = Depends(get_db),
) -> AdminWithdrawalListResponse:
    """Return all withdrawals in PENDING status awaiting admin action."""
    await _check_permission(staff, db, Permission.WITHDRAWAL_VIEW)
    withdrawals, total = await withdrawal_service.admin_list_withdrawals(
        db, status="pending", limit=limit, offset=offset
    )
    return AdminWithdrawalListResponse(
        items=[AdminWithdrawalResponse.from_withdrawal(w) for w in withdrawals],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "",
    response_model=AdminWithdrawalListResponse,
    summary="List all withdrawals (filtered)",
)
async def list_withdrawals(
    status: Annotated[Optional[str], Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    staff: StaffUser = Depends(get_current_staff),
    db: AsyncSession = Depends(get_db),
) -> AdminWithdrawalListResponse:
    """List all withdrawals with optional status filter."""
    await _check_permission(staff, db, Permission.WITHDRAWAL_VIEW)
    withdrawals, total = await withdrawal_service.admin_list_withdrawals(
        db, status=status, limit=limit, offset=offset
    )
    return AdminWithdrawalListResponse(
        items=[AdminWithdrawalResponse.from_withdrawal(w) for w in withdrawals],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/{withdrawal_id}",
    response_model=AdminWithdrawalResponse,
    summary="Get withdrawal details",
)
async def get_withdrawal(
    withdrawal_id: uuid.UUID,
    staff: StaffUser = Depends(get_current_staff),
    db: AsyncSession = Depends(get_db),
) -> AdminWithdrawalResponse:
    """Fetch full withdrawal record including balance linkage and staff fields."""
    await _check_permission(staff, db, Permission.WITHDRAWAL_VIEW)
    withdrawal = await withdrawal_service.admin_get_withdrawal(db, withdrawal_id)
    return AdminWithdrawalResponse.from_withdrawal(withdrawal)


# ── Actions ───────────────────────────────────────────────────────────────────

@router.post(
    "/{withdrawal_id}/process",
    response_model=AdminWithdrawalResponse,
    summary="Mark withdrawal as processing",
)
async def process_withdrawal(
    withdrawal_id: uuid.UUID,
    body: AdminProcessWithdrawalRequest,
    request: Request,
    staff: StaffUser = Depends(get_current_staff),
    db: AsyncSession = Depends(get_db),
) -> AdminWithdrawalResponse:
    """
    Mark a PENDING withdrawal as PROCESSING.
    No wallet mutation at this step — funds are already held.
    Records which staff member started processing.
    """
    await _check_permission(staff, db, Permission.WITHDRAWAL_PROCESS)
    withdrawal = await withdrawal_service.admin_process_withdrawal(
        db,
        withdrawal_id=withdrawal_id,
        staff_id=staff.id,
        note=body.note,
        ip_address=_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    # Snapshot scalar values BEFORE commit so they survive session expiry.
    _user_id = withdrawal.user_id
    _wid     = withdrawal.id
    _amount  = str(withdrawal.amount)
    await db.commit()

    # Phase 7: notify player (best-effort)
    from app.services import notification_service as ns
    try:
        await ns.notify_player_withdrawal_processing(
            db, user_id=_user_id,
            withdrawal_id=_wid, amount=_amount,
        )
        await db.commit()
    except Exception:
        pass
    return AdminWithdrawalResponse.from_withdrawal(withdrawal)


@router.post(
    "/{withdrawal_id}/mark-paid",
    response_model=AdminWithdrawalResponse,
    summary="Mark withdrawal as paid",
)
async def mark_paid(
    withdrawal_id: uuid.UUID,
    body: AdminMarkPaidRequest,
    request: Request,
    staff: StaffUser = Depends(get_current_staff),
    db: AsyncSession = Depends(get_db),
) -> AdminWithdrawalResponse:
    """
    Mark a PROCESSING withdrawal as PAID.

    Atomically:
    - Debits withdrawal_hold (finalises the outflow).
    - Records payout UTR.
    - Sets processed_by, processed_at.
    - Creates audit log.

    Once PAID the withdrawal cannot be reversed.
    """
    await _check_permission(staff, db, Permission.WITHDRAWAL_MARK_PAID)
    withdrawal = await withdrawal_service.admin_mark_paid(
        db,
        withdrawal_id=withdrawal_id,
        staff_id=staff.id,
        payout_utr=body.payout_utr,
        note=body.note,
        ip_address=_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    # Snapshot before commit
    _user_id = withdrawal.user_id
    _wid     = withdrawal.id
    _amount  = str(withdrawal.amount)
    await db.commit()

    # Phase 7: notify player (best-effort)
    from app.services import notification_service as ns
    try:
        await ns.notify_player_withdrawal_paid(
            db, user_id=_user_id,
            withdrawal_id=_wid, amount=_amount,
        )
        await db.commit()
    except Exception:
        pass
    return AdminWithdrawalResponse.from_withdrawal(withdrawal)


@router.post(
    "/{withdrawal_id}/reject",
    response_model=AdminWithdrawalResponse,
    summary="Reject a withdrawal request",
)
async def reject_withdrawal(
    withdrawal_id: uuid.UUID,
    body: AdminRejectWithdrawalRequest,
    request: Request,
    staff: StaffUser = Depends(get_current_staff),
    db: AsyncSession = Depends(get_db),
) -> AdminWithdrawalResponse:
    """
    Reject a PENDING or PROCESSING withdrawal.

    Atomically:
    - Releases withdrawal_hold back to winning_balance.
    - Marks withdrawal as REJECTED.
    - Records reason.
    - Creates audit log.

    Player's winning balance is fully restored.
    """
    await _check_permission(staff, db, Permission.WITHDRAWAL_REJECT)
    withdrawal = await withdrawal_service.admin_reject_withdrawal(
        db,
        withdrawal_id=withdrawal_id,
        staff_id=staff.id,
        reason=body.reason,
        ip_address=_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    # Snapshot before commit
    _user_id = withdrawal.user_id
    _wid     = withdrawal.id
    _amount  = str(withdrawal.amount)
    await db.commit()

    # Phase 7: notify player (best-effort)
    from app.services import notification_service as ns
    try:
        await ns.notify_player_withdrawal_rejected(
            db, user_id=_user_id,
            withdrawal_id=_wid, amount=_amount,
            reason=body.reason,
        )
        await db.commit()
    except Exception:
        pass
    return AdminWithdrawalResponse.from_withdrawal(withdrawal)
