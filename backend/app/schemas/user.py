"""
User-facing and admin user schemas — Phase 9 / Phase 11.

PlayerMeResponse      — safe player profile for GET /api/users/me
UpdateProfileRequest  — payload for PATCH /api/users/me (ff_name only)
AdminUserSummary      — one row in the admin user list
AdminUserDetail       — full admin view of a player
AdminUserListResponse

Never include: password_hash, reset tokens, or any secret field.

ff_name design
--------------
`username`  = immutable login identifier. Never changes. Not shown on leaderboard.
`ff_name`   = player-editable Free Fire display name (shown on leaderboard / results).
              NULL means not yet set; callers fall back to `username` for display.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, field_validator

# ── ff_name validation (shared constant) ─────────────────────────────────────
# Free Fire in-game names allow stylish Unicode characters, symbols, and fancy
# text — e.g. ꧁MANISH꧂, ★MANISH★, ᴍᴀɴɪsʜ, MANISH⚡, MANISH*7610*.
#
# Rules:
#   - 1–15 Unicode code-points (len() counts correctly for all BMP chars)
#   - Non-empty / non-whitespace-only after stripping
#   - No HTML angle brackets (< >) — prevents XSS; all other rendering is
#     escaped by the frontend's escapeHtml() anyway, but we block < > at source
#   - No ASCII control characters (0x00-0x1F, 0x7F)
#
# Everything else — including Unicode symbols, decorative chars, emoji ranges —
# is permitted because Free Fire itself allows them.
_FF_NAME_BLOCKED = re.compile(r'[<>\x00-\x1f\x7f]')


def validate_ff_name(v: str) -> str:
    """Shared validator for ff_name — used in UpdateProfileRequest and auth schema."""
    v = v.strip()
    if not v:
        raise ValueError("Free Fire name cannot be empty or whitespace only.")
    if len(v) > 15:
        raise ValueError("Free Fire name must be 15 characters or less.")
    if _FF_NAME_BLOCKED.search(v):
        raise ValueError(
            "Free Fire name must not contain HTML angle brackets or control characters."
        )
    return v


# ── Player self-view ──────────────────────────────────────────────────────────

class PlayerMeResponse(BaseModel):
    id: str
    username: str
    ff_uid: str
    ff_name: Optional[str]          # None = player hasn't set it yet
    referral_code: str
    role: str
    is_active: bool
    is_banned: bool
    avatar_url: Optional[str]
    created_at: datetime
    last_seen_at: Optional[datetime]

    model_config = {"from_attributes": True}

    @classmethod
    def from_user(cls, user: "User") -> "PlayerMeResponse":  # type: ignore[name-defined]
        return cls(
            id=str(user.id),
            username=user.username,
            ff_uid=user.ff_uid,
            ff_name=user.ff_name,
            referral_code=user.referral_code,
            role="player",
            is_active=user.is_active,
            is_banned=user.is_banned,
            avatar_url=user.avatar_url,
            created_at=user.created_at,
            last_seen_at=user.last_seen_at,
        )


# ── Player profile update ─────────────────────────────────────────────────────

class UpdateProfileRequest(BaseModel):
    """
    Payload for PATCH /api/users/me.

    Only `ff_name` is editable by the player:
      - `username`   is the login identifier — never editable.
      - `ff_uid`     is the Free Fire account number — never editable
                     (admins can update via admin panel only).
      - `password`   is changed via the separate reset-password flow.

    `ff_name` is required in this request: the player must always supply a
    non-empty name.  To clear it back to NULL the player would need to submit
    an empty string, which is explicitly rejected.
    """
    ff_name: str = Field(
        ...,
        min_length=1,
        max_length=15,
        description=(
            "Your Free Fire in-game display name. Enter the same name you use in Free Fire. "
            "Shown on tournaments, leaderboards, and results. Maximum 15 characters."
        ),
    )

    @field_validator("ff_name")
    @classmethod
    def _validate_ff_name(cls, v: str) -> str:
        return validate_ff_name(v)


class UpdateProfileResponse(BaseModel):
    """Returned after a successful PATCH /api/users/me."""
    message: str
    ff_name: str


# ── Admin user list/detail ────────────────────────────────────────────────────

class AdminUserSummary(BaseModel):
    id: str
    username: str
    ff_uid: str
    ff_name: Optional[str] = None   # added Phase 11
    referral_code: str
    is_active: bool
    is_banned: bool
    created_at: datetime
    last_seen_at: Optional[datetime]
    total_tournaments: int = 0
    total_wins: int = 0
    total_kills: int = 0
    total_winnings: str = "0.00"

    model_config = {"from_attributes": True}


class AdminUserDetail(AdminUserSummary):
    """Full player profile for admin detail view."""
    avatar_url: Optional[str]
    referred_by_id: Optional[str]
    referral_rewarded: bool
    deposit_balance: str = "0.00"
    winning_balance: str = "0.00"
    referral_balance: str = "0.00"
    withdrawal_hold: str = "0.00"
    is_frozen: bool = False


class AdminUserListResponse(BaseModel):
    items: list[AdminUserSummary]
    total: int
    limit: int
    offset: int
