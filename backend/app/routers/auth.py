"""
Player authentication routes:
  POST /api/auth/register
  POST /api/auth/login
  POST /api/auth/refresh
  POST /api/auth/logout
  POST /api/auth/reset-password

Refresh tokens travel exclusively in httpOnly Secure cookies.
Access tokens are returned in the JSON body and kept in JS memory on the client.
"""
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Body, Cookie, Depends, Request, Response
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.exceptions import (
    FFUIDTakenError,
    InvalidReferralCodeError,
    InvalidCredentialsError,
    PasswordMismatchError,
    ResetTokenExpiredError,
    ResetTokenInvalidError,
    SelfReferralError,
    TokenInvalidError,
    UsernameTakenError,
    WeakPasswordError,
)
from app.core.rate_limiter import limiter
from app.core.security import (
    create_access_token,
    generate_referral_code,
    generate_refresh_token,
    hash_password,
    hash_token,
    password_reset_token_expiry,
    refresh_token_expiry,
    verify_password,
)
from app.database import get_db
from app.models.audit_log import ActorType
from app.models.password_reset_token import PasswordResetToken
from app.models.player_stats import PlayerStats
from app.models.refresh_token import RefreshToken
from app.models.user import User
from app.models.wallet import Wallet
from app.services import referral_service
from app.schemas.auth import (
    MessageResponse,
    PlayerLoginRequest,
    PlayerRegisterRequest,
    PlayerRegisterResponse,
    RefreshResponse,
    ResetPasswordRequest,
    ResetPasswordResponse,
    TokenResponse,
)
from app.services import audit_service

router = APIRouter(prefix="/api/auth", tags=["Player Auth"])

_REFRESH_COOKIE = "refresh_token"
_COOKIE_MAX_AGE = 60 * 60 * 24 * 7  # 7 days in seconds


def _set_refresh_cookie(response: Response, raw_token: str) -> None:
    settings = get_settings()
    # In production: frontend (Netlify) and backend (Render) are on different
    # origins, so the browser will only send the cookie on cross-origin requests
    # when SameSite=None; Secure=True is set (RFC 6265bis).
    # In local development: Secure=False + SameSite omitted (None) so the
    # httpOnly cookie is sent by the browser on cross-port fetch() calls
    # (e.g. localhost:5500 → localhost:8000 / 127.0.0.1:5500 → 127.0.0.1:8000).
    response.set_cookie(
        key=_REFRESH_COOKIE,
        value=raw_token,
        httponly=True,
        secure=settings.is_production,
        samesite="none" if settings.is_production else None,
        max_age=_COOKIE_MAX_AGE,
        path="/api/auth",
    )


def _clear_refresh_cookie(response: Response) -> None:
    response.delete_cookie(
        key=_REFRESH_COOKIE,
        path="/api/auth",
    )


# ── Register ──────────────────────────────────────────────────────────────────

@router.post(
    "/register",
    response_model=PlayerRegisterResponse,
    status_code=201,
    summary="Register a new player account",
)
@limiter.limit("10/minute")
async def register(
    request: Request,
    body: PlayerRegisterRequest = Body(...),
    db: AsyncSession = Depends(get_db),
) -> PlayerRegisterResponse:
    """
    Create a new player account.

    Uniqueness of username and ff_uid is enforced both in application
    logic (pre-check with clear error messages) and at the DB level
    (UNIQUE constraints as final defense).
    """
    # All reads and writes use a single explicit transaction.
    # autobegin=False is set on the session factory so we own the transaction.
    # ── Validate referral code first (before any writes) ─────────────────────
    referrer: User | None = None
    if body.referral_code:
        result = await db.execute(
            select(User).where(User.referral_code == body.referral_code)
        )
        referrer = result.scalar_one_or_none()
        if referrer is None:
            raise InvalidReferralCodeError()

    # ── Check uniqueness (friendly errors before DB constraint fires) ─────────
    existing_username = await db.execute(
        select(User.id).where(User.username == body.username)
    )
    if existing_username.scalar_one_or_none() is not None:
        raise UsernameTakenError()

    existing_ff = await db.execute(
        select(User.id).where(User.ff_uid == body.ff_uid)
    )
    if existing_ff.scalar_one_or_none() is not None:
        raise FFUIDTakenError()

    # ── Generate unique referral code for the new user ────────────────────────
    new_code = await _generate_unique_referral_code(db)

    # ── Create user + wallet + player_stats — commit as unit ─────────────────
    user = User(
        username=body.username,
        ff_uid=body.ff_uid,
        ff_name=body.ff_name,      # optional Free Fire display name at registration
        password_hash=hash_password(body.password),
        referral_code=new_code,
        referred_by_id=referrer.id if referrer else None,
    )
    db.add(user)
    await db.flush([user])  # get user.id before creating wallet/referral

    # Self-referral guard (belt-and-suspenders after flush gives us the new ID)
    if referrer and referrer.id == user.id:
        raise SelfReferralError()

    wallet = Wallet(user_id=user.id)
    stats = PlayerStats(user_id=user.id)
    db.add(wallet)
    db.add(stats)
    await db.flush([wallet, stats])

    # ── Create referral relationship (if code was supplied) ───────────────────
    if referrer is not None and body.referral_code:
        await referral_service.create_referral_relationship(
            db,
            referrer=referrer,
            referred_user=user,
            referral_code_used=body.referral_code,
        )

    await audit_service.log(
        db=db,
        actor_type=ActorType.system,
        actor_id=audit_service.make_actor_id_system(),
        action="user.registered",
        entity_type="user",
        entity_id=str(user.id),
        new_value={
            "username": user.username,
            "ff_uid": user.ff_uid,
            "ff_name": user.ff_name,
            "referred_by": str(referrer.id) if referrer else None,
        },
        ip_address=_get_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    # BUG #19 FIX: snapshot non-PK fields before commit — they expire on commit
    # and accessing them afterward raises MissingGreenlet in async context.
    _notify_user_id   = user.id
    _notify_username  = user.username
    _resp_ff_uid      = user.ff_uid
    _resp_ref_code    = user.referral_code
    await db.commit()

    # Phase 7: welcome notification (best-effort, fresh session)
    # BUG #19 FIX: user.username is a regular (non-PK) column — after db.commit()
    # SQLAlchemy expires it.  Accessing it triggers an implicit async lazy load
    # which raises MissingGreenlet, silently swallowed by except, so the welcome
    # notification was never delivered.  Snapshot the values before commit.
    from app.services import notification_service as ns
    try:
        async with db.begin():
            await ns.notify_player_registered(db, user_id=_notify_user_id, username=_notify_username)
    except Exception:
        pass

    return PlayerRegisterResponse(
        id=str(_notify_user_id),
        username=_notify_username,
        ff_uid=_resp_ff_uid,
        ff_name=body.ff_name,
        referral_code=_resp_ref_code,
    )


# ── Login ─────────────────────────────────────────────────────────────────────

@router.post(
    "/login",
    response_model=TokenResponse,
    summary="Player login — returns access token; sets refresh cookie",
)
@limiter.limit("5/minute")
async def login(
    request: Request,
    response: Response,
    body: PlayerLoginRequest = Body(...),
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    result = await db.execute(select(User).where(User.username == body.username))
    user: User | None = result.scalar_one_or_none()

    # Constant-time: always call verify_password even if user not found
    # to prevent timing-based username enumeration
    dummy_hash = "$2b$12$aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    stored_hash = user.password_hash if user else dummy_hash
    password_ok = verify_password(body.password, stored_hash)

    if not user or not password_ok:
        raise InvalidCredentialsError()

    if not user.is_active:
        raise InvalidCredentialsError()   # Don't reveal banned state on login
    if user.is_banned:
        raise InvalidCredentialsError()

    # Issue tokens
    access_token = create_access_token(
        subject=user.id,
        audience="player",
        token_version=user.token_version,
    )
    raw_refresh, refresh_hash = generate_refresh_token()

    rt = RefreshToken(
        user_id=user.id,
        token_hash=refresh_hash,
        token_version=user.token_version,
        expires_at=refresh_token_expiry(),
        user_agent=request.headers.get("user-agent"),
        ip_address=_get_ip(request),
    )
    db.add(rt)

    await audit_service.log(
        db=db,
        actor_type=ActorType.user,
        actor_id=str(user.id),
        action="user.login",
        entity_type="user",
        entity_id=str(user.id),
        ip_address=_get_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    await db.commit()

    _set_refresh_cookie(response, raw_refresh)
    return TokenResponse(access_token=access_token)


# ── Refresh ───────────────────────────────────────────────────────────────────

@router.post(
    "/refresh",
    response_model=RefreshResponse,
    summary="Silently refresh player access token using refresh cookie",
)
async def refresh(
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
    refresh_token: str | None = Cookie(default=None, alias=_REFRESH_COOKIE),
) -> RefreshResponse:
    if not refresh_token:
        raise TokenInvalidError("No refresh token provided.")

    token_hash = hash_token(refresh_token)
    now = datetime.now(timezone.utc)

    result = await db.execute(
        select(RefreshToken).where(RefreshToken.token_hash == token_hash)
    )
    rt: RefreshToken | None = result.scalar_one_or_none()

    if rt is None or rt.is_revoked:
        raise TokenInvalidError("Refresh token is invalid.")
    # BUG #1 FIX: use astimezone() instead of replace() — replace() would
    # silently override an already-timezone-aware datetime returned by PostgreSQL
    # (TIMESTAMPTZ), causing incorrect expiry comparisons and spurious 401s.
    rt_expires = rt.expires_at if rt.expires_at.tzinfo is not None else rt.expires_at.replace(tzinfo=timezone.utc)
    if rt_expires.astimezone(timezone.utc) < now:
        raise TokenInvalidError("Refresh token has expired.")
    if rt.user_id is None:
        raise TokenInvalidError("Token type mismatch.")

    # Load user and validate token_version
    result = await db.execute(select(User).where(User.id == rt.user_id))
    user: User | None = result.scalar_one_or_none()

    if user is None or not user.is_active or user.is_banned:
        raise TokenInvalidError()
    if user.token_version != rt.token_version:
        raise TokenInvalidError("Session has been invalidated.")

    # Rotate refresh token (revoke old, issue new)
    new_raw, new_hash = generate_refresh_token()

    rt.is_revoked = True
    rt.revoked_at = now
    db.add(rt)

    new_rt = RefreshToken(
        user_id=user.id,
        token_hash=new_hash,
        token_version=user.token_version,
        expires_at=refresh_token_expiry(),
        user_agent=request.headers.get("user-agent"),
        ip_address=_get_ip(request),
    )
    db.add(new_rt)
    await db.commit()

    access_token = create_access_token(
        subject=user.id,
        audience="player",
        token_version=user.token_version,
    )
    _set_refresh_cookie(response, new_raw)
    return RefreshResponse(access_token=access_token)


# ── Logout ────────────────────────────────────────────────────────────────────

@router.post(
    "/logout",
    response_model=MessageResponse,
    summary="Player logout — revokes refresh token",
)
async def logout(
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
    refresh_token: str | None = Cookie(default=None, alias=_REFRESH_COOKIE),
) -> MessageResponse:
    if refresh_token:
        token_hash = hash_token(refresh_token)
        result = await db.execute(
            select(RefreshToken).where(RefreshToken.token_hash == token_hash)
        )
        rt: RefreshToken | None = result.scalar_one_or_none()
        if rt and not rt.is_revoked:
            rt.is_revoked = True
            rt.revoked_at = datetime.now(timezone.utc)
            db.add(rt)
            await db.commit()

    _clear_refresh_cookie(response)
    return MessageResponse(message="Logged out successfully.")


# ── Reset password (public — consumes one-time token) ────────────────────────

@router.post(
    "/reset-password",
    response_model=ResetPasswordResponse,
    summary="Consume admin-issued one-time token and set a new password",
)
@limiter.limit("10/minute")
async def reset_password(
    request: Request,
    response: Response,
    body: ResetPasswordRequest = Body(...),
    db: AsyncSession = Depends(get_db),
) -> ResetPasswordResponse:
    token_hash = hash_token(body.token)
    now = datetime.now(timezone.utc)

    result = await db.execute(
        select(PasswordResetToken).where(
            PasswordResetToken.token_hash == token_hash,
            PasswordResetToken.is_active == True,  # noqa: E712
        )
    )
    prt: PasswordResetToken | None = result.scalar_one_or_none()

    if prt is None:
        raise ResetTokenInvalidError()
    # BUG #11 FIX: use the same safe timezone conversion already applied to
    # RefreshToken.expires_at in the /refresh endpoint (BUG #1 fix).
    # .replace(tzinfo=…) silently overrides an already-timezone-aware datetime
    # from PostgreSQL TIMESTAMPTZ, corrupting expiry comparisons.
    prt_expires = prt.expires_at if prt.expires_at.tzinfo is not None else prt.expires_at.replace(tzinfo=timezone.utc)
    if prt_expires.astimezone(timezone.utc) < now:
        raise ResetTokenExpiredError()

    # Load user
    result = await db.execute(select(User).where(User.id == prt.user_id))
    user: User | None = result.scalar_one_or_none()
    if user is None:
        raise ResetTokenInvalidError()

    # 1. Update password
    user.password_hash = hash_password(body.new_password)
    # 2. Increment token_version — invalidates ALL existing sessions
    user.token_version += 1
    db.add(user)

    # 3. Mark reset token as used
    prt.is_active = False
    prt.used_at = now
    db.add(prt)

    # 4. Revoke all existing refresh tokens for this user
    await db.execute(
        update(RefreshToken)
        .where(
            RefreshToken.user_id == user.id,
            RefreshToken.is_revoked == False,  # noqa: E712
        )
        .values(is_revoked=True, revoked_at=now)
    )

    await audit_service.log(
        db=db,
        actor_type=ActorType.system,
        actor_id=audit_service.make_actor_id_system(),
        action="password_reset.completed",
        entity_type="user",
        entity_id=str(user.id),
        new_value={"token_version": user.token_version},
        ip_address=_get_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    await db.commit()

    _clear_refresh_cookie(response)
    return ResetPasswordResponse()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_ip(request: Request) -> str | None:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


async def _generate_unique_referral_code(db: AsyncSession) -> str:
    """Generate a referral code guaranteed unique in the DB (retry on collision)."""
    for _ in range(10):
        code = generate_referral_code()
        result = await db.execute(
            select(User.id).where(User.referral_code == code)
        )
        if result.scalar_one_or_none() is None:
            return code
    raise RuntimeError("Could not generate a unique referral code after 10 attempts.")
