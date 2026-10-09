"""
Admin deposit management endpoints.

Routes
------
GET   /api/admin/deposits                      — list deposits (with filters)
GET   /api/admin/deposits/pending-manual       — list pending manual deposits
GET   /api/admin/deposits/{id}                 — get deposit details
POST  /api/admin/deposits/{id}/approve         — approve manual deposit
POST  /api/admin/deposits/{id}/reject          — reject manual deposit

Permissions:
  deposit.view    — required to view/list deposits
  deposit.approve — required to approve manual deposits
  deposit.reject  — required to reject manual deposits
  (Super Admin has all permissions automatically)

Security:
  - All routes require staff authentication.
  - Super Admin always passes; agents need explicit grants.
  - Status changes are only accepted from authenticated staff.
  - Wallet credit happens inside the service transaction, not here.

Transaction pattern (matches admin_payment_config.py and the rest of the project)
----------------------------------------------------------------------------------
Do NOT call `async with db.begin()` on ANY endpoint in this router.
The `get_current_staff` dependency executes a SELECT on the shared session,
which causes SQLAlchemy to autobegin a transaction automatically.  Calling
db.begin() again on that same session raises:
    InvalidRequestError: A transaction is already begun on this Session.
The correct idiom for this project is:
  - All endpoints: execute queries directly on db, call await db.commit()
    to persist mutations (same pattern as admin_payment_config.py).
  - Notification side-effects use a separate short-lived session so they
    never interfere with the request session lifecycle.
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
from app.schemas.deposit import (
    AdminApproveDepositRequest,
    AdminDepositListResponse,
    AdminDepositResponse,
    AdminRejectDepositRequest,
)
from app.services import deposit_service

router = APIRouter(prefix="/api/admin/deposits", tags=["Admin — Deposits"])


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
    "/pending-manual",
    response_model=AdminDepositListResponse,
    summary="List deposits pending manual verification",
)
async def list_pending_manual(
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    staff: StaffUser = Depends(get_current_staff),
    db: AsyncSession = Depends(get_db),
) -> AdminDepositListResponse:
    """Return all deposits currently awaiting admin UTR verification."""
    # FIX: _check_permission runs a SELECT which autobegins a transaction on
    # the session. Do NOT call db.begin() after it — execute queries directly.
    await _check_permission(staff, db, Permission.DEPOSIT_VIEW)
    deposits, total = await deposit_service.list_pending_manual_deposits(
        db, limit=limit, offset=offset
    )
    return AdminDepositListResponse(
        items=[AdminDepositResponse.from_deposit(d) for d in deposits],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "",
    response_model=AdminDepositListResponse,
    summary="List all deposits (filtered)",
)
async def list_deposits(
    status: Annotated[Optional[str], Query()] = None,
    method: Annotated[Optional[str], Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    staff: StaffUser = Depends(get_current_staff),
    db: AsyncSession = Depends(get_db),
) -> AdminDepositListResponse:
    """List deposits with optional status/method filter."""
    # FIX: same as list_pending_manual — no db.begin() after _check_permission.
    await _check_permission(staff, db, Permission.DEPOSIT_VIEW)
    deposits, total = await deposit_service.admin_list_deposits(
        db, status=status, method=method, limit=limit, offset=offset
    )
    return AdminDepositListResponse(
        items=[AdminDepositResponse.from_deposit(d) for d in deposits],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/{deposit_id}",
    response_model=AdminDepositResponse,
    summary="Get deposit details",
)
async def get_deposit(
    deposit_id: uuid.UUID,
    staff: StaffUser = Depends(get_current_staff),
    db: AsyncSession = Depends(get_db),
) -> AdminDepositResponse:
    """Fetch full deposit record including provider fields and admin notes."""
    # FIX: read-only — no db.begin() needed. Build DTO immediately after
    # fetch while the session is still active (no commit to expire the object).
    await _check_permission(staff, db, Permission.DEPOSIT_VIEW)
    deposit = await deposit_service.admin_get_deposit(db, deposit_id)
    return AdminDepositResponse.from_deposit(deposit)


# ── Actions ───────────────────────────────────────────────────────────────────

@router.post(
    "/{deposit_id}/approve",
    response_model=AdminDepositResponse,
    summary="Approve a manual deposit",
)
async def approve_deposit(
    deposit_id: uuid.UUID,
    body: AdminApproveDepositRequest,
    request: Request,
    staff: StaffUser = Depends(get_current_staff),
    db: AsyncSession = Depends(get_db),
) -> AdminDepositResponse:
    """
    Approve a manual UPI deposit after verifying the UTR.

    Atomically:
    - Verifies the deposit is in pending_manual_verification.
    - Checks UTR uniqueness.
    - Credits the player's deposit_balance.
    - Marks deposit as APPROVED.
    - Records staff actor and timestamp.
    - Creates audit log entry.
    """
    # Do NOT use `async with db.begin()` here.  get_current_staff already
    # executed a SELECT on this session, which autobegins a transaction.
    # Calling db.begin() again raises InvalidRequestError.  The correct
    # pattern (same as admin_payment_config.py) is: execute queries directly
    # on db, then call await db.commit() to persist mutations.
    await _check_permission(staff, db, Permission.DEPOSIT_APPROVE)
    deposit = await deposit_service.approve_manual_deposit(
        db,
        deposit_id=deposit_id,
        staff_id=staff.id,
        note=body.note,
        ip_address=_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    # Snapshot scalars before commit — commit expires ORM objects
    d_user_id = deposit.user_id
    d_id      = deposit.id
    d_amount  = str(deposit.amount)
    response  = AdminDepositResponse.from_deposit(deposit)
    await db.commit()

    # Phase 7: notify player (best-effort, separate session)
    from app.services import notification_service as ns
    try:
        from app.database import get_session_factory
        factory = get_session_factory()
        async with factory() as notif_db:
            async with notif_db.begin():
                await ns.notify_player_deposit_approved(
                    notif_db, user_id=d_user_id,
                    deposit_id=d_id, amount=d_amount,
                )
    except Exception:
        pass
    return response


@router.post(
    "/{deposit_id}/reject",
    response_model=AdminDepositResponse,
    summary="Reject a manual deposit",
)
async def reject_deposit(
    deposit_id: uuid.UUID,
    body: AdminRejectDepositRequest,
    request: Request,
    staff: StaffUser = Depends(get_current_staff),
    db: AsyncSession = Depends(get_db),
) -> AdminDepositResponse:
    """
    Reject a manual deposit request.
    No wallet change. Reason recorded in admin_note.
    Player is not credited.
    """
    # Do NOT use `async with db.begin()` here — same reason as approve_deposit.
    await _check_permission(staff, db, Permission.DEPOSIT_REJECT)
    deposit = await deposit_service.reject_manual_deposit(
        db,
        deposit_id=deposit_id,
        staff_id=staff.id,
        reason=body.reason,
        ip_address=_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    # Snapshot scalars before commit — commit expires ORM objects
    d_user_id = deposit.user_id
    d_id      = deposit.id
    d_amount  = str(deposit.amount)
    response  = AdminDepositResponse.from_deposit(deposit)
    await db.commit()

    # Phase 7: notify player (best-effort, separate session)
    from app.services import notification_service as ns
    try:
        from app.database import get_session_factory
        factory = get_session_factory()
        async with factory() as notif_db:
            async with notif_db.begin():
                await ns.notify_player_deposit_rejected(
                    notif_db, user_id=d_user_id,
                    deposit_id=d_id, amount=d_amount,
                    reason=body.reason,
                )
    except Exception:
        pass
    return response
