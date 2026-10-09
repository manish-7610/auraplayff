"""
Help & Support routes — Phase 10.

Player routes (require player JWT):
  POST /api/support                  — submit a new support request
  GET  /api/support                  — list own support requests (paginated)
  GET  /api/support/{request_id}     — view own support request detail

Admin / Agent routes (require staff JWT + support.view permission):
  GET    /api/admin/support                   — list all requests (filterable)
  GET    /api/admin/support/{request_id}      — view request detail
  PATCH  /api/admin/support/{request_id}      — update status / add response

Security:
  - Players cannot access another player's request (404 returned).
  - Support.view permission is required for all staff routes.
  - Super admin always passes; agents must have explicit support.view grant.
  - Financial, wallet, and payment routes are NOT touched here.
"""
from __future__ import annotations

import uuid
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import PermissionDeniedError
from app.core.permissions import Permission
from app.database import get_db
from app.dependencies import get_current_staff, get_current_user
from app.models.permission import Permission as PermModel, StaffUserPermission
from app.models.staff_user import StaffRole, StaffUser
from app.models.user import User
from app.schemas.support import (
    AdminSupportListResponse,
    AdminSupportResponse,
    AdminUpdateSupportRequest,
    PlayerSupportListResponse,
    PlayerSupportResponse,
    SupportRequestCreate,
)
from app.services import support_service

player_router = APIRouter(prefix="/api/support", tags=["Player — Support"])
admin_router  = APIRouter(prefix="/api/admin/support", tags=["Admin — Support"])


# ── Permission helper (consistent with rest of codebase) ─────────────────────

async def _require_support_perm(staff: StaffUser, db: AsyncSession) -> None:
    """Require support.view permission. Super admin always passes."""
    if staff.role == StaffRole.super_admin:
        return
    from sqlalchemy.orm import joinedload
    from sqlalchemy import select
    result = await db.execute(
        select(StaffUserPermission)
        .options(joinedload(StaffUserPermission.permission))
        .where(StaffUserPermission.staff_user_id == staff.id)
    )
    grants = result.unique().scalars().all()
    granted: set[str] = {
        g.permission.code for g in grants if g.permission is not None
    }
    if Permission.SUPPORT_VIEW.value not in granted:
        raise PermissionDeniedError(Permission.SUPPORT_VIEW.value)


# ══════════════════════════════════════════════════════════════════════════════
# PLAYER ROUTES
# ══════════════════════════════════════════════════════════════════════════════

@player_router.post(
    "",
    response_model=PlayerSupportResponse,
    status_code=201,
    summary="Submit a new support request",
)
async def create_support_request(
    body: SupportRequestCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PlayerSupportResponse:
    """
    Submit a new support request.

    - tournament_id is optional; omit for a General / Other Issue.
    - At least one of whatsapp_number or instagram_id is required.
    - message must be at least 10 characters.
    """
    response = await support_service.create_support_request(
        db,
        user_id=current_user.id,
        body=body,
    )
    await db.commit()
    return response


@player_router.get(
    "",
    response_model=PlayerSupportListResponse,
    summary="List my support requests",
)
async def list_my_support_requests(
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PlayerSupportListResponse:
    """Returns only the authenticated player's own support requests."""
    return await support_service.list_my_requests(
        db,
        user_id=current_user.id,
        limit=limit,
        offset=offset,
    )


@player_router.get(
    "/{request_id}",
    response_model=PlayerSupportResponse,
    summary="View a support request",
)
async def get_my_support_request(
    request_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PlayerSupportResponse:
    """
    Returns a single support request.
    Returns 404 if the request does not exist OR belongs to a different player.
    """
    return await support_service.get_my_request(
        db,
        request_id=request_id,
        user_id=current_user.id,
    )


# ══════════════════════════════════════════════════════════════════════════════
# ADMIN / AGENT ROUTES
# ══════════════════════════════════════════════════════════════════════════════

@admin_router.get(
    "",
    response_model=AdminSupportListResponse,
    summary="List all support requests (Admin)",
)
async def admin_list_support_requests(
    status: Annotated[Optional[str], Query(max_length=20)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
    offset: Annotated[int, Query(ge=0)] = 0,
    current_staff: StaffUser = Depends(get_current_staff),
    db: AsyncSession = Depends(get_db),
) -> AdminSupportListResponse:
    """
    List all support requests across all players.
    Optionally filter by status: pending | in_progress | resolved.
    """
    await _require_support_perm(current_staff, db)
    return await support_service.list_all_requests(
        db,
        status=status,
        limit=limit,
        offset=offset,
    )


@admin_router.get(
    "/{request_id}",
    response_model=AdminSupportResponse,
    summary="View a support request detail (Admin)",
)
async def admin_get_support_request(
    request_id: uuid.UUID,
    current_staff: StaffUser = Depends(get_current_staff),
    db: AsyncSession = Depends(get_db),
) -> AdminSupportResponse:
    """Returns full support request detail including contact info."""
    await _require_support_perm(current_staff, db)
    req = await support_service.get_request(db, request_id=request_id)
    return await support_service._to_admin_response(db, req)


@admin_router.patch(
    "/{request_id}",
    response_model=AdminSupportResponse,
    summary="Update status / add response (Admin)",
)
async def admin_update_support_request(
    request_id: uuid.UUID,
    body: AdminUpdateSupportRequest,
    current_staff: StaffUser = Depends(get_current_staff),
    db: AsyncSession = Depends(get_db),
) -> AdminSupportResponse:
    """
    Update the status and/or add an admin response to a support request.

    - status: pending | in_progress | resolved
    - admin_response: text reply shown to the player

    At least one field must be provided.
    Setting status to 'resolved' records resolved_at and resolved_by.
    """
    await _require_support_perm(current_staff, db)
    response = await support_service.update_request(
        db,
        request_id=request_id,
        body=body,
        staff_id=current_staff.id,
    )
    await db.commit()
    return response
