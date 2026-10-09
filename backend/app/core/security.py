"""
Core security utilities:
  - Password hashing / verification (bcrypt cost ≥ 12)
  - JWT access token creation and decoding
  - Refresh token generation (cryptographically secure random, SHA-256 stored)
  - Password reset token generation (same pattern)
  - Token-version invalidation support
"""
from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from jose import JWTError, jwt
from passlib.context import CryptContext

from app.config import get_settings

# ── passlib / bcrypt compatibility shim ──────────────────────────────────────
# passlib 1.7.4 tries to read bcrypt.__about__.__version__ to detect the
# backend version.  bcrypt 4.x+ exposes __version__ directly at module level
# without the __about__ sub-module.  This shim prevents a noisy but harmless
# AttributeError traceback from appearing in test output.
try:
    import bcrypt as _bcrypt_mod
    if not hasattr(_bcrypt_mod, "__about__"):
        class _BcryptAbout:
            __version__ = getattr(_bcrypt_mod, "__version__", "4.0.0")
        _bcrypt_mod.__about__ = _BcryptAbout()  # type: ignore[attr-defined]
except Exception:
    pass

# ── Password hashing ─────────────────────────────────────────────────────────
# bcrypt with cost factor 12 (rounds parameter in passlib maps to cost)
_pwd_context = CryptContext(
    schemes=["bcrypt"],
    deprecated="auto",
    bcrypt__rounds=12,
)


def hash_password(plain: str) -> str:
    """Return a bcrypt hash of the plain-text password."""
    return _pwd_context.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    """Return True if plain matches hashed. Never raises — returns False on error."""
    try:
        return _pwd_context.verify(plain, hashed)
    except Exception:
        return False


# ── JWT helpers ───────────────────────────────────────────────────────────────
TokenAudience = Literal["player", "staff"]

# Claim keys
_CLAIM_SUB = "sub"
_CLAIM_AUD = "aud"
_CLAIM_TYPE = "type"           # "access"
_CLAIM_TV = "tv"               # token_version
_CLAIM_ROLE = "role"           # staff role (staff tokens only)


def create_access_token(
    subject: str | uuid.UUID,
    audience: TokenAudience,
    token_version: int,
    role: str | None = None,
    display_name: str | None = None,
    agent_id: str | None = None,
    expires_delta: timedelta | None = None,
) -> str:
    """
    Create a short-lived JWT access token.

    Args:
        subject:       user.id or staff_user.id (UUID as string)
        audience:      "player" or "staff" — enforced on decode
        token_version: current token_version from DB — embedded for session invalidation
        role:          staff role string (only included for staff tokens)
        display_name:  human-readable display name (staff tokens only — for session restore)
        agent_id:      human-readable agent ID, e.g. "AGT-0001" (staff tokens only)
        expires_delta: override default expiry
    """
    settings = get_settings()
    now = datetime.now(timezone.utc)
    exp = now + (
        expires_delta
        or timedelta(minutes=settings.jwt_access_token_expire_minutes)
    )

    payload: dict[str, Any] = {
        _CLAIM_SUB: str(subject),
        _CLAIM_AUD: audience,
        _CLAIM_TYPE: "access",
        _CLAIM_TV: token_version,
        "iat": now,
        "exp": exp,
        "jti": str(uuid.uuid4()),   # unique token ID (future blacklist use)
    }
    if role is not None:
        payload[_CLAIM_ROLE] = role
    # BUG #9 FIX: include display_name and agent_id for staff tokens so that
    # restoreAdminSession() can reconstruct the profile after a page reload
    # without exposing sensitive data (these are non-sensitive display values).
    if display_name is not None:
        payload["display_name"] = display_name
    if agent_id is not None:
        payload["agent_id"] = agent_id

    return jwt.encode(
        payload,
        settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
    )


def decode_access_token(
    token: str,
    expected_audience: TokenAudience,
) -> dict[str, Any]:
    """
    Decode and validate a JWT access token.

    Raises JWTError (jose) on any validation failure:
      - invalid signature
      - expired
      - wrong audience
      - wrong type
    """
    settings = get_settings()
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret_key,
            algorithms=[settings.jwt_algorithm],
            audience=expected_audience,
        )
    except JWTError:
        raise

    if payload.get(_CLAIM_TYPE) != "access":
        raise JWTError("Token type is not 'access'")

    return payload


def get_subject_from_payload(payload: dict[str, Any]) -> str:
    sub = payload.get(_CLAIM_SUB)
    if not sub:
        raise JWTError("Missing 'sub' claim")
    return sub


def get_token_version_from_payload(payload: dict[str, Any]) -> int:
    tv = payload.get(_CLAIM_TV)
    if tv is None:
        raise JWTError("Missing 'tv' claim")
    return int(tv)


def get_role_from_payload(payload: dict[str, Any]) -> str | None:
    return payload.get(_CLAIM_ROLE)


# ── Refresh token helpers ────────────────────────────────────────────────────

def generate_refresh_token() -> tuple[str, str]:
    """
    Generate a cryptographically secure refresh token.

    Returns:
        (raw_token, token_hash)
        raw_token  — returned to the client in a httpOnly cookie (never stored)
        token_hash — SHA-256 hex digest stored in the database
    """
    raw = secrets.token_hex(32)          # 64 hex chars = 32 bytes entropy
    token_hash = _sha256(raw)
    return raw, token_hash


def hash_token(raw: str) -> str:
    """Hash a raw token with SHA-256 for DB storage / lookup."""
    return _sha256(raw)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def refresh_token_expiry() -> datetime:
    settings = get_settings()
    return datetime.now(timezone.utc) + timedelta(
        days=settings.jwt_refresh_token_expire_days
    )


# ── Password reset token helpers ─────────────────────────────────────────────

def generate_password_reset_token() -> tuple[str, str]:
    """
    Generate a cryptographically secure one-time password reset token.

    Returns:
        (raw_token, token_hash)
    """
    raw = secrets.token_urlsafe(32)      # URL-safe, 43 chars
    token_hash = _sha256(raw)
    return raw, token_hash


def password_reset_token_expiry() -> datetime:
    settings = get_settings()
    return datetime.now(timezone.utc) + timedelta(
        minutes=settings.password_reset_token_expire_minutes
    )


# ── Referral code generation ─────────────────────────────────────────────────

_REFERRAL_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
_REFERRAL_LENGTH = 8


def generate_referral_code() -> str:
    """
    Generate an 8-character cryptographically secure random referral code.
    Characters: uppercase A-Z + digits 0-9.
    Uniqueness is enforced at the DB level (UNIQUE constraint on users.referral_code).
    """
    return "".join(secrets.choice(_REFERRAL_ALPHABET) for _ in range(_REFERRAL_LENGTH))
