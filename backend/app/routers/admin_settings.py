"""
Admin platform settings endpoints — Phase 9.

Routes
------
GET  /api/admin/settings            — list all platform settings
GET  /api/admin/settings/{key}      — get single setting
PUT  /api/admin/settings/{key}      — update a setting value

Permission: settings.manage (super_admin only in practice via ADMIN_ONLY_PERMISSIONS).
Secret values (Razorpay keys, DB password, JWT secret) are NEVER stored in
platform_settings — they live only in environment variables.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError, PermissionDeniedError
from app.core.permissions import Permission
from app.database import get_db
from app.dependencies import get_current_staff
from app.models.audit_log import ActorType
from app.models.permission import Permission as PermModel, StaffUserPermission
from app.models.platform_setting import PlatformSetting
from app.models.staff_user import StaffRole, StaffUser
from app.schemas.settings import (
    PlatformSettingResponse,
    PlatformSettingsResponse,
    UpdateSettingRequest,
)
from app.services import audit_service

router = APIRouter(prefix="/api/admin/settings", tags=["Admin — Settings"])


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
    if Permission.SETTINGS_MANAGE.value not in granted:
        raise PermissionDeniedError(Permission.SETTINGS_MANAGE.value)


def _get_ip(request: Request) -> str | None:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


@router.get(
    "",
    response_model=PlatformSettingsResponse,
    summary="List all platform settings (admin view)",
)
async def list_settings(
    staff: StaffUser = Depends(get_current_staff),
    db: AsyncSession = Depends(get_db),
) -> PlatformSettingsResponse:
    """Return all platform settings. Requires settings.manage permission."""
    await _check_perm(staff, db)

    rows = (
        await db.execute(select(PlatformSetting).order_by(PlatformSetting.key))
    ).scalars().all()

    return PlatformSettingsResponse(
        settings=[
            PlatformSettingResponse(
                key=r.key,
                value=r.value,
                description=r.description,
                is_public=r.is_public,
                updated_at=r.updated_at,
                updated_by=r.updated_by,
            )
            for r in rows
        ]
    )


@router.get(
    "/{key}",
    response_model=PlatformSettingResponse,
    summary="Get a single platform setting",
)
async def get_setting(
    key: str,
    staff: StaffUser = Depends(get_current_staff),
    db: AsyncSession = Depends(get_db),
) -> PlatformSettingResponse:
    """Fetch a single platform setting by key."""
    await _check_perm(staff, db)

    row = (
        await db.execute(select(PlatformSetting).where(PlatformSetting.key == key))
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError("PlatformSetting")

    return PlatformSettingResponse(
        key=row.key,
        value=row.value,
        description=row.description,
        is_public=row.is_public,
        updated_at=row.updated_at,
        updated_by=row.updated_by,
    )


@router.put(
    "/{key}",
    response_model=PlatformSettingResponse,
    summary="Update a platform setting value",
)
async def update_setting(
    key: str,
    body: UpdateSettingRequest,
    request: Request,
    staff: StaffUser = Depends(get_current_staff),
    db: AsyncSession = Depends(get_db),
) -> PlatformSettingResponse:
    """
    Update a platform setting by key.
    Only existing keys can be updated — no arbitrary new keys can be created
    via this endpoint (prevents storing secrets by accident).
    Audit-logged.
    """
    await _check_perm(staff, db)

    row = (
        await db.execute(select(PlatformSetting).where(PlatformSetting.key == key))
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError("PlatformSetting")

    old_value = row.value
    row.value = body.value
    row.updated_by = staff.agent_id
    db.add(row)

    await audit_service.log(
        db=db,
        actor_type=ActorType.staff,
        actor_id=str(staff.id),
        action="settings.updated",
        entity_type="platform_setting",
        entity_id=key,
        old_value={"value": old_value},
        new_value={"value": body.value},
        ip_address=_get_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    await db.commit()

    return PlatformSettingResponse(
        key=row.key,
        value=row.value,
        description=row.description,
        is_public=row.is_public,
        updated_at=row.updated_at,
        updated_by=row.updated_by,
    )
