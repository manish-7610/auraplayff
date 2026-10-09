"""
Agent management service — Phase 6.

Responsibilities
----------------
1. generate_agent_id()     — produce next sequential AGT-NNNN
2. create_agent()          — create a new StaffUser(role=agent) with hashed password
3. get_agent()             — fetch any agent by UUID (admin only)
4. get_agent_by_agent_id() — fetch by agent_id string
5. list_agents()           — paginated list of all agents
6. activate_agent()        — set is_active=True
7. deactivate_agent()      — set is_active=False
8. reset_agent_password()  — bcrypt-hash new password, bump token_version
9. update_agent()          — change display_name

Security rules
--------------
- Only Super Admin may call any of these (enforced at router layer).
- Agents must never receive admin permissions through this service.
- Passwords are never returned or logged.
- agent_id is server-generated (never client-supplied).
- Token version is bumped on password reset so existing sessions become invalid.
- Role is always StaffRole.agent for new agents; never super_admin.
"""
from __future__ import annotations

import logging
import re
import uuid
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppError
from app.core.security import hash_password
from app.models._types import utcnow
from app.models.audit_log import ActorType
from app.models.staff_user import StaffRole, StaffUser
from app.services import audit_service

logger = logging.getLogger("auraplayff.agent")

# ── Custom exceptions ─────────────────────────────────────────────────────────

class AgentNotFoundError(AppError):
    def __init__(self) -> None:
        super().__init__("agent_not_found", "Agent not found.", 404)


class AgentIdTakenError(AppError):
    def __init__(self, agent_id: str) -> None:
        super().__init__(
            "agent_id_taken",
            f"Agent ID {agent_id!r} is already in use.",
            409,
        )


class CannotDeactivateSuperAdminError(AppError):
    def __init__(self) -> None:
        super().__init__(
            "cannot_deactivate_super_admin",
            "Super Admin accounts cannot be deactivated via this endpoint.",
            403,
        )


class CannotResetSuperAdminPasswordError(AppError):
    def __init__(self) -> None:
        super().__init__(
            "cannot_reset_super_admin_password",
            "Super Admin passwords cannot be reset via the agent endpoint.",
            403,
        )


# ── Agent ID generation ───────────────────────────────────────────────────────

async def generate_agent_id(db: AsyncSession) -> str:
    """
    Generate the next sequential agent ID in the format AGT-NNNN.
    Queries the highest existing AGT-NNNN numeric suffix and increments by 1.
    Starts at AGT-0001.

    Thread-safe: the surrounding DB transaction + row flush prevents races
    for typical agent creation volumes.
    """
    result = await db.execute(
        select(StaffUser.agent_id)
        .where(StaffUser.agent_id.like("AGT-%"))
        .order_by(StaffUser.agent_id.desc())
        .limit(1)
    )
    last: str | None = result.scalar_one_or_none()

    if last is None:
        next_num = 1
    else:
        m = re.match(r"AGT-(\d+)$", last)
        next_num = (int(m.group(1)) + 1) if m else 1

    return f"AGT-{next_num:04d}"


# ── Create ────────────────────────────────────────────────────────────────────

async def create_agent(
    db: AsyncSession,
    *,
    display_name: str,
    password: str,
    created_by_id: uuid.UUID,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> StaffUser:
    """
    Create a new Agent (StaffRole.agent).

    Steps:
    1. Generate the next sequential AGT-NNNN agent_id.
    2. Hash password with bcrypt (cost 12).
    3. Insert StaffUser row.
    4. Audit log.

    Never returns the plaintext password.
    Never assigns super_admin role.
    """
    agent_id = await generate_agent_id(db)

    # Guard against the (unlikely) race where two agents are created simultaneously
    existing = await db.execute(
        select(StaffUser.id).where(StaffUser.agent_id == agent_id)
    )
    if existing.scalar_one_or_none() is not None:
        raise AgentIdTakenError(agent_id)

    password_hash = hash_password(password)

    agent = StaffUser(
        agent_id=agent_id,
        display_name=display_name.strip(),
        password_hash=password_hash,
        role=StaffRole.agent,
        is_active=True,
        token_version=0,
        created_by_id=created_by_id,
    )
    db.add(agent)
    await db.flush([agent])

    await audit_service.log(
        db=db,
        actor_type=ActorType.staff,
        actor_id=str(created_by_id),
        action="agent.created",
        entity_type="staff_user",
        entity_id=str(agent.id),
        new_value={
            "agent_id": agent_id,
            "display_name": display_name,
            "role": "agent",
        },
        ip_address=ip_address,
        user_agent=user_agent,
    )

    logger.info("Agent created: agent_id=%r display_name=%r", agent_id, display_name)
    return agent


# ── Read ──────────────────────────────────────────────────────────────────────

async def get_agent(db: AsyncSession, agent_uuid: uuid.UUID) -> StaffUser:
    """Fetch an agent by primary-key UUID. Raises AgentNotFoundError if absent."""
    result = await db.execute(
        select(StaffUser).where(StaffUser.id == agent_uuid)
    )
    agent = result.scalar_one_or_none()
    if agent is None:
        raise AgentNotFoundError()
    return agent


async def get_agent_by_agent_id(db: AsyncSession, agent_id: str) -> StaffUser:
    """Fetch an agent by login agent_id string."""
    result = await db.execute(
        select(StaffUser).where(StaffUser.agent_id == agent_id)
    )
    agent = result.scalar_one_or_none()
    if agent is None:
        raise AgentNotFoundError()
    return agent


async def list_agents(
    db: AsyncSession,
    *,
    role: StaffRole | None = StaffRole.agent,
    is_active: bool | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[StaffUser], int]:
    """
    Return paginated list of staff users (default: agents only).
    Pass role=None to list all staff including Super Admins.
    """
    q = select(StaffUser)
    if role is not None:
        q = q.where(StaffUser.role == role)
    if is_active is not None:
        q = q.where(StaffUser.is_active == is_active)

    count_result = await db.execute(
        select(func.count()).select_from(q.subquery())
    )
    total = count_result.scalar_one()

    rows = await db.execute(
        q.order_by(StaffUser.created_at.desc()).limit(limit).offset(offset)
    )
    return list(rows.scalars().all()), total


# ── Update ────────────────────────────────────────────────────────────────────

async def update_agent(
    db: AsyncSession,
    agent_uuid: uuid.UUID,
    *,
    display_name: str | None = None,
    admin_id: uuid.UUID,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> StaffUser:
    """Update an agent's display name. Only non-None fields are changed."""
    agent = await get_agent(db, agent_uuid)

    old_values: dict = {}
    new_values: dict = {}

    if display_name is not None and display_name.strip() != agent.display_name:
        old_values["display_name"] = agent.display_name
        agent.display_name = display_name.strip()
        new_values["display_name"] = agent.display_name

    if new_values:
        agent.updated_at = utcnow()  # type: ignore[attr-defined]
        await db.flush([agent])
        await audit_service.log(
            db=db,
            actor_type=ActorType.staff,
            actor_id=str(admin_id),
            action="agent.updated",
            entity_type="staff_user",
            entity_id=str(agent.id),
            old_value=old_values,
            new_value=new_values,
            ip_address=ip_address,
            user_agent=user_agent,
        )

    return agent


# ── Activate / Deactivate ─────────────────────────────────────────────────────

async def activate_agent(
    db: AsyncSession,
    agent_uuid: uuid.UUID,
    *,
    admin_id: uuid.UUID,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> StaffUser:
    """Set agent is_active = True."""
    agent = await get_agent(db, agent_uuid)
    if agent.is_active:
        return agent

    agent.is_active = True
    await db.flush([agent])

    await audit_service.log(
        db=db,
        actor_type=ActorType.staff,
        actor_id=str(admin_id),
        action="agent.activated",
        entity_type="staff_user",
        entity_id=str(agent.id),
        old_value={"is_active": False},
        new_value={"is_active": True},
        ip_address=ip_address,
        user_agent=user_agent,
    )
    return agent


async def deactivate_agent(
    db: AsyncSession,
    agent_uuid: uuid.UUID,
    *,
    admin_id: uuid.UUID,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> StaffUser:
    """Set agent is_active = False. Super Admin accounts cannot be deactivated here."""
    agent = await get_agent(db, agent_uuid)

    if agent.role == StaffRole.super_admin:
        raise CannotDeactivateSuperAdminError()

    if not agent.is_active:
        return agent

    agent.is_active = False
    # Bump token_version to invalidate all existing sessions
    agent.token_version += 1
    await db.flush([agent])

    await audit_service.log(
        db=db,
        actor_type=ActorType.staff,
        actor_id=str(admin_id),
        action="agent.deactivated",
        entity_type="staff_user",
        entity_id=str(agent.id),
        old_value={"is_active": True},
        new_value={"is_active": False},
        ip_address=ip_address,
        user_agent=user_agent,
    )
    return agent


# ── Password reset ────────────────────────────────────────────────────────────

async def reset_agent_password(
    db: AsyncSession,
    agent_uuid: uuid.UUID,
    new_password: str,
    *,
    admin_id: uuid.UUID,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> StaffUser:
    """
    Reset an agent's password.

    - bcrypt-hashes the new password
    - increments token_version (invalidates all existing sessions)
    - audits the action (does NOT log the password or its hash)
    """
    agent = await get_agent(db, agent_uuid)

    if agent.role == StaffRole.super_admin:
        raise CannotResetSuperAdminPasswordError()

    agent.password_hash = hash_password(new_password)
    agent.token_version += 1
    await db.flush([agent])

    await audit_service.log(
        db=db,
        actor_type=ActorType.staff,
        actor_id=str(admin_id),
        action="agent.password_reset",
        entity_type="staff_user",
        entity_id=str(agent.id),
        new_value={"token_version": agent.token_version},
        ip_address=ip_address,
        user_agent=user_agent,
    )
    logger.info("Agent password reset: agent_id=%r by admin=%s", agent.agent_id, admin_id)
    return agent
