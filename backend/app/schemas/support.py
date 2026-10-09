"""
Pydantic v2 schemas for Help & Support endpoints.

Player schemas:
  - SupportRequestCreate   — submitted by player
  - PlayerSupportResponse  — returned to player (no internal fields leaked)

Admin schemas:
  - AdminSupportResponse   — full detail including contact info and user data
  - AdminUpdateRequest     — update status and/or admin_response

Security invariants:
  - Players never see other players' requests.
  - Contact info (whatsapp_number, instagram_id) is only included in admin-facing schemas.
  - Player-facing list does not expose admin_response until the request exists
    (it IS shown so players can see the reply).
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, model_validator


# ── Player — Create ───────────────────────────────────────────────────────────

class SupportRequestCreate(BaseModel):
    """Submitted by the authenticated player."""

    tournament_id: Optional[uuid.UUID] = Field(
        None,
        description="Tournament UUID, or null for a General / Other Issue.",
    )
    message: str = Field(
        ...,
        min_length=10,
        max_length=2000,
        description="Describe the problem in detail.",
    )
    whatsapp_number: Optional[str] = Field(
        None,
        max_length=30,
        description="WhatsApp contact number (e.g. +91 98765 43210).",
    )
    instagram_id: Optional[str] = Field(
        None,
        max_length=80,
        description="Instagram username (without @).",
    )

    @model_validator(mode="after")
    def require_at_least_one_contact(self) -> "SupportRequestCreate":
        wa = (self.whatsapp_number or "").strip()
        ig = (self.instagram_id or "").strip()
        if not wa and not ig:
            raise ValueError(
                "Please provide at least one contact method: "
                "WhatsApp number or Instagram ID."
            )
        # Store stripped values
        self.whatsapp_number = wa or None
        self.instagram_id = ig or None
        return self


# ── Player — Response ─────────────────────────────────────────────────────────

class PlayerSupportResponse(BaseModel):
    """Support request as seen by the submitting player."""

    id: uuid.UUID
    tournament_id: Optional[uuid.UUID]
    tournament_title: Optional[str]       # denormalised for display
    message: str
    status: str
    admin_response: Optional[str]         # player can see the reply
    created_at: datetime
    updated_at: datetime


class PlayerSupportListResponse(BaseModel):
    requests: list[PlayerSupportResponse]
    total: int


# ── Admin — Response ──────────────────────────────────────────────────────────

class AdminSupportResponse(BaseModel):
    """Full support request detail for staff."""

    id: uuid.UUID
    user_id: uuid.UUID
    username: str                          # denormalised
    tournament_id: Optional[uuid.UUID]
    tournament_title: Optional[str]
    message: str
    whatsapp_number: Optional[str]
    instagram_id: Optional[str]
    status: str
    admin_response: Optional[str]
    resolved_at: Optional[datetime]
    resolved_by_id: Optional[uuid.UUID]
    created_at: datetime
    updated_at: datetime


class AdminSupportListResponse(BaseModel):
    requests: list[AdminSupportResponse]
    total: int
    limit: int
    offset: int


# ── Admin — Update ────────────────────────────────────────────────────────────

class AdminUpdateSupportRequest(BaseModel):
    """Body for PATCH /api/admin/support/{id}."""

    status: Optional[str] = Field(
        None,
        pattern=r"^(pending|in_progress|resolved)$",
        description="New status value.",
    )
    admin_response: Optional[str] = Field(
        None,
        max_length=2000,
        description="Staff reply shown to the player.",
    )

    @model_validator(mode="after")
    def require_at_least_one_field(self) -> "AdminUpdateSupportRequest":
        if self.status is None and self.admin_response is None:
            raise ValueError("Provide at least one of: status, admin_response.")
        return self
