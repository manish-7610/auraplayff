"""
Audit log schemas — Phase 9.

AuditLogResponse     — single audit entry
AuditLogListResponse — paginated list
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel


class AuditLogResponse(BaseModel):
    id: str
    actor_type: str
    actor_id: str
    action: str
    entity_type: str
    entity_id: str
    old_value: Optional[dict[str, Any]]
    new_value: Optional[dict[str, Any]]
    ip_address: Optional[str]
    user_agent: Optional[str]
    created_at: datetime

    model_config = {"from_attributes": True}

    @classmethod
    def from_log(cls, log: "AuditLog") -> "AuditLogResponse":  # type: ignore[name-defined]
        return cls(
            id=str(log.id),
            actor_type=log.actor_type.value,
            actor_id=log.actor_id,
            action=log.action,
            entity_type=log.entity_type,
            entity_id=log.entity_id,
            old_value=log.old_value,
            new_value=log.new_value,
            ip_address=log.ip_address,
            user_agent=log.user_agent,
            created_at=log.created_at,
        )


class AuditLogListResponse(BaseModel):
    items: list[AuditLogResponse]
    total: int
    limit: int
    offset: int
