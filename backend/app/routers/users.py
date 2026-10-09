"""
Player user self-view and profile-update endpoints — Phase 9 / Phase 11.

Routes
------
GET   /api/users/me   — authenticated player: safe profile fields only
PATCH /api/users/me   — update Free Fire display name (ff_name)

Security
--------
• Only the authenticated player can update their own profile.
• `username`  (login identifier) is immutable — not exposed in PATCH.
• `ff_uid`    (Free Fire account UID) is immutable — explicitly rejected if supplied.
• `ff_name`   (Free Fire display name) is the only editable field here.
• Password changes go through the separate reset-password flow.

ff_name falls back to `username` for display whenever it is NULL.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError
from app.database import get_db
from app.dependencies import get_current_user
from app.models.audit_log import ActorType
from app.models.user import User
from app.schemas.user import PlayerMeResponse, UpdateProfileRequest, UpdateProfileResponse
from app.services import audit_service

router = APIRouter(prefix="/api/users", tags=["Players — Self"])


@router.get(
    "/me",
    response_model=PlayerMeResponse,
    summary="Get authenticated player's own profile",
)
async def get_me(
    current_user: User = Depends(get_current_user),
) -> PlayerMeResponse:
    """
    Return the authenticated player's safe profile.
    Fields: id, username, ff_uid, ff_name, referral_code, role,
            is_active, is_banned, avatar_url, created_at, last_seen_at.

    Never returns: password_hash, token_version, reset tokens, wallet balances.
    Use /api/wallet/me for balances.
    """
    return PlayerMeResponse.from_user(current_user)


@router.patch(
    "/me",
    response_model=UpdateProfileResponse,
    summary="Update player's Free Fire display name",
)
async def update_profile(
    body: UpdateProfileRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> UpdateProfileResponse:
    """
    Update the authenticated player's Free Fire display name (ff_name).

    Rules:
    • ff_name must be 2–30 chars, letters/digits/spaces/underscores/hyphens/dots.
    • Whitespace-only values are rejected.
    • username (login identifier) is NOT changed — this call never touches it.
    • ff_uid (Free Fire account number) is NOT changed — this call never touches it.

    The updated ff_name is immediately reflected on leaderboards, results, and
    the player profile. Falls back to username when ff_name is NULL.
    """
    new_ff_name = body.ff_name  # already stripped + validated by pydantic

    # No-op if the player sets the same name they already have
    if current_user.ff_name == new_ff_name:
        return UpdateProfileResponse(
            message="Free Fire name is already set to this value.",
            ff_name=new_ff_name,
        )

    # Snapshot old value for audit log
    old_ff_name = current_user.ff_name

    # Persist the change
    current_user.ff_name = new_ff_name
    current_user.last_seen_at = datetime.now(timezone.utc)
    db.add(current_user)
    await db.flush([current_user])

    # Audit log — note the change without exposing any secret
    await audit_service.log(
        db=db,
        actor_type=ActorType.user,
        actor_id=str(current_user.id),
        action="user.ff_name_updated",
        entity_type="user",
        entity_id=str(current_user.id),
        old_value={"ff_name": old_ff_name},
        new_value={"ff_name": new_ff_name},
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )

    await db.commit()

    return UpdateProfileResponse(
        message="Free Fire name updated successfully.",
        ff_name=new_ff_name,
    )
