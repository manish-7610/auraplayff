"""
Audit service — write immutable audit log entries.

Usage (inside a request handler or service that already has a db session):

    await audit_service.log(
        db=db,
        actor_type=ActorType.staff,
        actor_id=str(current_staff.id),
        action="deposit.approved",
        entity_type="deposit",
        entity_id=str(deposit.id),
        old_value={"status": "pending"},
        new_value={"status": "completed"},
        ip_address=request.client.host,
        user_agent=request.headers.get("user-agent"),
    )

Rules:
  - Audit logs are INSERT-only. Never UPDATE or DELETE.
  - Failures are logged to stderr but NEVER raise — a failed audit must not
    roll back the business transaction it is recording.
  - Call this INSIDE the same DB transaction where possible so the audit
    record is committed atomically with the action.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit_log import ActorType, AuditLog

logger = logging.getLogger("auraplayff.audit")


async def log(
    db: AsyncSession,
    *,
    actor_type: ActorType,
    actor_id: str,
    action: str,
    entity_type: str,
    entity_id: str,
    old_value: dict[str, Any] | None = None,
    new_value: dict[str, Any] | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> None:
    """
    Insert an audit log record.

    This function intentionally never raises so that an audit failure
    cannot abort a business-critical transaction.
    """
    try:
        entry = AuditLog(
            actor_type=actor_type,
            actor_id=actor_id,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            old_value=old_value,
            new_value=new_value,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        db.add(entry)
        # Flush but let the caller commit — keeps audit in the same transaction
        await db.flush([entry])
    except Exception:
        logger.exception(
            "Audit log FAILED: action=%s entity=%s/%s actor=%s/%s",
            action,
            entity_type,
            entity_id,
            actor_type,
            actor_id,
        )


def make_actor_id_system() -> str:
    """Canonical actor_id string for system-triggered audit entries."""
    return "system"
