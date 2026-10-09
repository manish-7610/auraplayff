"""
Admin audit log endpoints — Phase 9.

Routes
------
GET /api/admin/audit-logs   — paginated audit log with filters

Permission: audit.view (super_admin always passes).
Sensitive fields are already scrubbed at write time — we only expose
what was recorded in the AuditLog table.
"""
from __future__ import annotations

from typing import Annotated, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import PermissionDeniedError
from app.core.permissions import Permission
from app.database import get_db
from app.dependencies import get_current_staff
from app.models.audit_log import AuditLog
from app.models.permission import Permission as PermModel, StaffUserPermission
from app.models.staff_user import StaffRole, StaffUser
from app.schemas.audit import AuditLogListResponse, AuditLogResponse

router = APIRouter(prefix="/api/admin/audit-logs", tags=["Admin — Audit Logs"])


async def _check_perm(staff: StaffUser, db: AsyncSession) -> None:
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
    granted: set[str] = {
        g.permission.code for g in grants if g.permission is not None
    }
    if Permission.AUDIT_VIEW.value not in granted:
        raise PermissionDeniedError(Permission.AUDIT_VIEW.value)


@router.get(
    "",
    response_model=AuditLogListResponse,
    summary="List audit logs with filters",
)
async def list_audit_logs(
    action: Annotated[Optional[str], Query(max_length=100)] = None,
    actor_id: Annotated[Optional[str], Query(max_length=80)] = None,
    entity_type: Annotated[Optional[str], Query(max_length=60)] = None,
    entity_id: Annotated[Optional[str], Query(max_length=80)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    staff: StaffUser = Depends(get_current_staff),
    db: AsyncSession = Depends(get_db),
) -> AuditLogListResponse:
    """
    Paginated audit log. Newest entries first.
    Filter by action prefix, actor_id, entity_type, or entity_id.
    """
    await _check_perm(staff, db)

    q = select(AuditLog)
    if action:
        q = q.where(AuditLog.action.ilike(f"%{action}%"))
    if actor_id:
        q = q.where(AuditLog.actor_id == actor_id)
    if entity_type:
        q = q.where(AuditLog.entity_type == entity_type)
    if entity_id:
        q = q.where(AuditLog.entity_id == entity_id)

    count_q = select(func.count()).select_from(q.subquery())
    total = (await db.execute(count_q)).scalar_one()

    rows = (
        await db.execute(
            q.order_by(AuditLog.created_at.desc()).limit(limit).offset(offset)
        )
    ).scalars().all()

    return AuditLogListResponse(
        items=[AuditLogResponse.from_log(r) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
    )
