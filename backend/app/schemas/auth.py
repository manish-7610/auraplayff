"""
Pydantic v2 schemas for authentication endpoints.
These are the ONLY shapes accepted/returned by auth routes.
Password hashes are never included in any response schema.
"""
from __future__ import annotations

import re
from typing import Optional

from pydantic import BaseModel, Field, field_validator, model_validator

# ── Shared validators ─────────────────────────────────────────────────────────

_USERNAME_RE = re.compile(r"^[a-zA-Z0-9_]{3,30}$")
_FF_UID_RE = re.compile(r"^\d{6,15}$")          # Free Fire UIDs are numeric
_REFERRAL_CODE_RE = re.compile(r"^[A-Z0-9]{8}$")


def _validate_username(v: str) -> str:
    v = v.strip()
    if not _USERNAME_RE.match(v):
        raise ValueError(
            "Username must be 3–30 characters, letters, digits, or underscores only."
        )
    return v


def _validate_ff_uid(v: str) -> str:
    v = v.strip()
    if not _FF_UID_RE.match(v):
        raise ValueError("Free Fire UID must be 6–15 digits.")
    return v


def _validate_password_strength(v: str) -> str:
    if len(v) < 8:
        raise ValueError("Password must be at least 8 characters long.")
    if len(v) > 128:
        raise ValueError("Password must not exceed 128 characters.")
    return v


# ── Player registration ───────────────────────────────────────────────────────

class PlayerRegisterRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=30)
    password: str = Field(..., min_length=8, max_length=128)
    confirm_password: str = Field(..., min_length=8, max_length=128)
    ff_uid: str = Field(..., min_length=6, max_length=15)
    ff_name: Optional[str] = Field(
        None,
        min_length=1,
        max_length=15,
        description="Free Fire in-game display name (optional at registration; can be set later).",
    )
    referral_code: Optional[str] = Field(None, min_length=8, max_length=8)

    @field_validator("username")
    @classmethod
    def validate_username(cls, v: str) -> str:
        return _validate_username(v)

    @field_validator("ff_uid")
    @classmethod
    def validate_ff_uid(cls, v: str) -> str:
        return _validate_ff_uid(v)

    @field_validator("ff_name")
    @classmethod
    def validate_ff_name(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        if not v:
            return None   # treat blank string the same as omitted
        import re as _re
        _FF_NAME_BLOCKED = _re.compile(r'[<>\x00-\x1f\x7f]')
        if len(v) > 15:
            raise ValueError("Free Fire name must be 15 characters or less.")
        if _FF_NAME_BLOCKED.search(v):
            raise ValueError(
                "Free Fire name must not contain HTML angle brackets or control characters."
            )
        return v

    @field_validator("password")
    @classmethod
    def validate_password(cls, v: str) -> str:
        return _validate_password_strength(v)

    @field_validator("referral_code")
    @classmethod
    def validate_referral_code(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip().upper()
        if not _REFERRAL_CODE_RE.match(v):
            raise ValueError("Referral code must be 8 uppercase alphanumeric characters.")
        return v

    @model_validator(mode="after")
    def passwords_match(self) -> "PlayerRegisterRequest":
        if self.password != self.confirm_password:
            raise ValueError("Password and confirmation do not match.")
        return self


class PlayerRegisterResponse(BaseModel):
    id: str
    username: str
    ff_uid: str
    ff_name: Optional[str] = None
    referral_code: str
    message: str = "Registration successful."


# ── Login ─────────────────────────────────────────────────────────────────────

class PlayerLoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=30)
    password: str = Field(..., min_length=1, max_length=128)


class StaffLoginRequest(BaseModel):
    agent_id: str = Field(..., min_length=1, max_length=20)
    password: str = Field(..., min_length=1, max_length=128)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    # refresh_token is sent as httpOnly cookie — NOT in response body


# ── Refresh ───────────────────────────────────────────────────────────────────

class RefreshResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


# ── Password reset ────────────────────────────────────────────────────────────

class IssuePasswordResetResponse(BaseModel):
    reset_token: str          # raw token returned to admin who then gives it to player
    expires_in_minutes: int
    message: str = "Password reset token issued."


class ResetPasswordRequest(BaseModel):
    token: str = Field(..., min_length=10)
    new_password: str = Field(..., min_length=8, max_length=128)
    confirm_password: str = Field(..., min_length=8, max_length=128)

    @field_validator("new_password")
    @classmethod
    def validate_password(cls, v: str) -> str:
        return _validate_password_strength(v)

    @model_validator(mode="after")
    def passwords_match(self) -> "ResetPasswordRequest":
        if self.new_password != self.confirm_password:
            raise ValueError("Password and confirmation do not match.")
        return self


class ResetPasswordResponse(BaseModel):
    message: str = "Password has been reset. Please log in with your new password."


# ── Generic message ───────────────────────────────────────────────────────────

class MessageResponse(BaseModel):
    message: str
