"""
Support service — Help & Support request lifecycle.

Player operations:
  - create_support_request  — validate, persist, audit-log
  - list_my_requests        — paginated list for the authenticated player
  - get_my_request          — single request (player-owned only)

Admin/Agent operations:
  - list_all_requests       — paginated list with optional status filter
  - get_request             — any request by id (staff access)
  - update_request          — update status and/or add admin_response

Security:
  - Players can only read their own requests (enforced here, not just in the router).
  - Contact info (whatsapp_number, instagram_id) is only included in admin schemas.
  - Tournament title is denormalised at read time for display (never stored redundantly).
"""
from __future__ import annotations

import logging
import uuid
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models._types import utcnow
from app.models.audit_log import ActorType
from app.models.support_request import SupportRequest, SupportStatus
from app.schemas.support import (
    AdminSupportResponse,
    AdminSupportListResponse,
    AdminUpdateSupportRequest,
    PlayerSupportListResponse,
    PlayerSupportResponse,
    SupportRequestCreate,
)
from app.services import audit_service

logger = logging.getLogger("auraplayff.support")

_DEFAULT_LIMIT = 30
_MAX_LIMIT = 100


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _tournament_title(db: AsyncSession, tournament_id: uuid.UUID | None) -> str | None:
    """Fetch tournament title by id, returns None if not found or id is None."""
    if tournament_id is None:
        return None
    from app.models.tournament import Tournament
    result = await db.execute(
        select(Tournament.title).where(Tournament.id == tournament_id)
    )
    return result.scalar_one_or_none()


async def _username(db: AsyncSession, user_id: uuid.UUID) -> str:
    """Fetch username for a user_id."""
    from app.models.user import User
    result = await db.execute(
        select(User.username).where(User.id == user_id)
    )
    return result.scalar_one_or_none() or str(user_id)


def _to_player_response(
    req: SupportRequest,
    tournament_title: str | None,
) -> PlayerSupportResponse:
    return PlayerSupportResponse(
        id=req.id,
        tournament_id=req.tournament_id,
        tournament_title=tournament_title,
        message=req.message,
        status=req.status.value,
        admin_response=req.admin_response,
        created_at=req.created_at,
        updated_at=req.updated_at,
    )


async def _to_admin_response(
    db: AsyncSession,
    req: SupportRequest,
) -> AdminSupportResponse:
    username = await _username(db, req.user_id)
    t_title = await _tournament_title(db, req.tournament_id)
    return AdminSupportResponse(
        id=req.id,
        user_id=req.user_id,
        username=username,
        tournament_id=req.tournament_id,
        tournament_title=t_title,
        message=req.message,
        whatsapp_number=req.whatsapp_number,
        instagram_id=req.instagram_id,
        status=req.status.value,
        admin_response=req.admin_response,
        resolved_at=req.resolved_at,
        resolved_by_id=req.resolved_by_id,
        created_at=req.created_at,
        updated_at=req.updated_at,
    )


# ── Player operations ─────────────────────────────────────────────────────────

async def create_support_request(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    body: SupportRequestCreate,
) -> PlayerSupportResponse:
    """
    Create a new support request submitted by a player.

    Validates tournament ownership is NOT required — a player can reference
    any tournament (including ones they're not enrolled in, for general issues).
    Tournament existence is validated only if tournament_id is provided.
    """
    # Validate tournament exists if provided
    if body.tournament_id is not None:
        from app.models.tournament import Tournament
        t_result = await db.execute(
            select(Tournament.id, Tournament.title).where(Tournament.id == body.tournament_id)
        )
        row = t_result.first()
        if row is None:
            from app.core.exceptions import NotFoundError
            raise NotFoundError("Tournament")
        t_title = row[1]
    else:
        t_title = None

    req = SupportRequest(
        user_id=user_id,
        tournament_id=body.tournament_id,
        message=body.message.strip(),
        whatsapp_number=body.whatsapp_number,
        instagram_id=body.instagram_id,
        status=SupportStatus.pending,
    )
    db.add(req)
    await db.flush([req])

    await audit_service.log(
        db=db,
        actor_type=ActorType.user,
        actor_id=str(user_id),
        action="support.request_created",
        entity_type="support_request",
        entity_id=str(req.id),
        new_value={
            "tournament_id": str(body.tournament_id) if body.tournament_id else None,
            "status": SupportStatus.pending.value,
        },
    )

    logger.info("Support request created: id=%s user=%s", req.id, user_id)
    return _to_player_response(req, t_title)


async def list_my_requests(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    limit: int = _DEFAULT_LIMIT,
    offset: int = 0,
) -> PlayerSupportListResponse:
    """Return paginated support requests for the authenticated player only."""
    limit = min(limit, _MAX_LIMIT)

    count_result = await db.execute(
        select(func.count()).select_from(SupportRequest).where(
            SupportRequest.user_id == user_id
        )
    )
    total = count_result.scalar_one()

    result = await db.execute(
        select(SupportRequest)
        .where(SupportRequest.user_id == user_id)
        .order_by(SupportRequest.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    reqs = list(result.scalars().all())

    items: list[PlayerSupportResponse] = []
    for req in reqs:
        t_title = await _tournament_title(db, req.tournament_id)
        items.append(_to_player_response(req, t_title))

    return PlayerSupportListResponse(requests=items, total=total)


async def get_my_request(
    db: AsyncSession,
    *,
    request_id: uuid.UUID,
    user_id: uuid.UUID,
) -> PlayerSupportResponse:
    """
    Fetch a single support request.
    Raises 404 if not found or if it belongs to a different user.
    """
    result = await db.execute(
        select(SupportRequest).where(SupportRequest.id == request_id)
    )
    req: SupportRequest | None = result.scalar_one_or_none()

    if req is None or req.user_id != user_id:
        from app.core.exceptions import NotFoundError
        raise NotFoundError("SupportRequest")

    t_title = await _tournament_title(db, req.tournament_id)
    return _to_player_response(req, t_title)


# ── Admin / Agent operations ──────────────────────────────────────────────────

async def list_all_requests(
    db: AsyncSession,
    *,
    status: str | None = None,
    limit: int = _DEFAULT_LIMIT,
    offset: int = 0,
) -> AdminSupportListResponse:
    """Return all support requests, optionally filtered by status."""
    limit = min(limit, _MAX_LIMIT)

    q = select(SupportRequest)
    count_q = select(func.count()).select_from(SupportRequest)

    if status:
        try:
            status_enum = SupportStatus(status)
            q = q.where(SupportRequest.status == status_enum)
            count_q = count_q.where(SupportRequest.status == status_enum)
        except ValueError:
            pass  # ignore invalid filter value

    count_result = await db.execute(count_q)
    total = count_result.scalar_one()

    result = await db.execute(
        q.order_by(SupportRequest.created_at.desc()).limit(limit).offset(offset)
    )
    reqs = list(result.scalars().all())

    items: list[AdminSupportResponse] = []
    for req in reqs:
        items.append(await _to_admin_response(db, req))

    return AdminSupportListResponse(
        requests=items,
        total=total,
        limit=limit,
        offset=offset,
    )


async def get_request(
    db: AsyncSession,
    *,
    request_id: uuid.UUID,
) -> SupportRequest:
    """Fetch any support request by id (staff use). Raises 404 if not found."""
    result = await db.execute(
        select(SupportRequest).where(SupportRequest.id == request_id)
    )
    req: SupportRequest | None = result.scalar_one_or_none()
    if req is None:
        from app.core.exceptions import NotFoundError
        raise NotFoundError("SupportRequest")
    return req


async def update_request(
    db: AsyncSession,
    *,
    request_id: uuid.UUID,
    body: AdminUpdateSupportRequest,
    staff_id: uuid.UUID,
) -> AdminSupportResponse:
    """
    Update status and/or admin_response for a support request.

    - Transitioning to 'resolved' sets resolved_at and resolved_by_id.
    - Transitioning away from 'resolved' clears resolved_at and resolved_by_id.
    - admin_response is updated if provided (non-None).
    """
    req = await get_request(db, request_id=request_id)
    now = utcnow()

    old_status = req.status.value

    if body.status is not None:
        new_status = SupportStatus(body.status)
        req.status = new_status
        if new_status == SupportStatus.resolved:
            req.resolved_at = now
            req.resolved_by_id = staff_id
        else:
            req.resolved_at = None
            req.resolved_by_id = None

    if body.admin_response is not None:
        req.admin_response = body.admin_response.strip() or None

    req.updated_at = now
    await db.flush([req])

    await audit_service.log(
        db=db,
        actor_type=ActorType.staff,
        actor_id=str(staff_id),
        action="support.request_updated",
        entity_type="support_request",
        entity_id=str(req.id),
        old_value={"status": old_status},
        new_value={
            "status": req.status.value,
            "has_response": req.admin_response is not None,
        },
    )

    logger.info(
        "Support request updated: id=%s staff=%s status=%s",
        req.id, staff_id, req.status.value,
    )
    return await _to_admin_response(db, req)
