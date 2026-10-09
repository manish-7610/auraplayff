"""
Admin payment configuration endpoints.

Routes
------
GET    /api/admin/payment-config                — get full payment config (Super Admin only)
PUT    /api/admin/payment-config                — update UPI/manual config fields (Super Admin only)
POST   /api/admin/payment-config/qr-upload      — upload / replace QR image (Super Admin only)
GET    /api/admin/payment-config/qr-signed-url  — get a short-lived signed URL for the current QR (Super Admin only)
POST   /api/admin/payment-config/activate       — activate manual payment (Super Admin only)
POST   /api/admin/payment-config/deactivate     — deactivate manual payment (Super Admin only)

Security
--------
- ALL routes are Super Admin only — no agent access, no player access.
- Agent requests return 403 (PermissionDeniedError).
- QR upload validated by magic-byte inspection (JPEG / PNG only, max 5 MB).
- QR image is stored in Supabase Storage using the service-role key.
  The storage path is server-generated — client cannot influence it.
- Old QR file is deleted from Supabase when replaced.
- No secrets are stored in platform_settings — only UPI ID, instructions, QR path.
- Razorpay credentials are environment variables and cannot be managed here.
- The signed URL endpoint generates a 5-minute Supabase signed URL server-side;
  the service-role key is never forwarded to the browser.

Transaction pattern
-------------------
This router uses the same pattern as admin_settings.py and the rest of the
project: bare db.execute() / db.add() followed by explicit db.commit().

Do NOT use `async with db.begin()` here.  The get_current_staff dependency
executes a SELECT on the shared session before the endpoint body runs, which
causes SQLAlchemy to begin an implicit transaction automatically.  Calling
db.begin() again on that same session raises:
    InvalidRequestError: A transaction is already begun on this Session.
The correct idiom for this project is: execute queries directly on db,
then call await db.commit() to persist mutations.
"""
from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, File, Request, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppError, PermissionDeniedError
from app.database import get_db
from app.dependencies import get_current_staff, get_client_ip
from app.models.audit_log import ActorType
from app.models.platform_setting import (
    PlatformSetting,
    SETTING_MANUAL_DEPOSIT_INSTRUCTIONS,
    SETTING_MANUAL_PAYMENT_ACTIVE,
    SETTING_QR_STORAGE_REF,
    SETTING_UPI_ACCOUNT_NAME,
    SETTING_UPI_DISPLAY_NAME,
    SETTING_UPI_ID,
)
from app.models.staff_user import StaffRole, StaffUser
from app.services import audit_service
from app.models._types import utcnow

logger = logging.getLogger("auraplayff.admin.payment_config")

router = APIRouter(
    prefix="/api/admin/payment-config",
    tags=["Admin — Payment Config"],
)

# ── Super Admin guard ─────────────────────────────────────────────────────────

def _require_super_admin(staff: StaffUser) -> None:
    """Raise 403 for anyone who is not Super Admin. Agents cannot access this."""
    if staff.role != StaffRole.super_admin:
        raise PermissionDeniedError("settings.manage")


# ── QR file validation constants ──────────────────────────────────────────────

_QR_MAX_BYTES = 5 * 1024 * 1024   # 5 MB
_QR_ALLOWED_MAGIC: dict[str, tuple[bytes, ...]] = {
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/png":  (b"\x89PNG\r\n\x1a\n",),
}

_QR_STORAGE_FOLDER = "payment-qr"   # subfolder inside the screenshots bucket


def _detect_qr_content_type(header: bytes) -> str | None:
    for mime, sigs in _QR_ALLOWED_MAGIC.items():
        for sig in sigs:
            if header.startswith(sig):
                return mime
    return None


def _safe_ext(content_type: str) -> str:
    return "jpg" if content_type == "image/jpeg" else "png"


# ── Supabase helpers ──────────────────────────────────────────────────────────

async def _upload_qr_to_supabase(
    storage_path: str, file_bytes: bytes, content_type: str
) -> None:
    from app.config import get_settings
    settings = get_settings()

    if not settings.supabase_url or not settings.supabase_service_role_key:
        raise AppError(
            "storage_not_configured",
            "Supabase storage is not configured. "
            "Set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY.",
            503,
        )

    # Guard: SUPABASE_URL must be the full HTTPS URL, not just the project ref.
    # Correct:   https://abcdefghij.supabase.co
    # Incorrect: abcdefghij  (project ref only — will cause a network error)
    if not settings.supabase_url.startswith("http"):
        raise AppError(
            "storage_misconfigured",
            "SUPABASE_URL must be a full HTTPS URL "
            "(e.g. https://<project-ref>.supabase.co). "
            "Check your .env file.",
            503,
        )

    # Guard: Supabase Storage keys — supports both legacy JWT (eyJ...) and
    # new-format Secret keys (sb_secret_...).  Either format works as long as
    # the key is non-empty and the SUPABASE_URL is correct.
    key = settings.supabase_service_role_key

    bucket = settings.supabase_storage_bucket_screenshots
    url = (
        f"{settings.supabase_url.rstrip('/')}"
        f"/storage/v1/object/{bucket}/{storage_path}"
    )
    # Send the key on BOTH headers so it works with:
    #   - Legacy JWT keys:   Authorization: Bearer eyJ...
    #   - New Secret keys:   apikey: sb_secret_...
    # Supabase Storage accepts either header; sending both is safe and backward-compatible.
    headers = {
        "Authorization": f"Bearer {key}",
        "apikey": key,
        "Content-Type": content_type,
        "x-upsert": "true",
    }

    try:
        import httpx
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(url, content=file_bytes, headers=headers)
            if resp.status_code not in (200, 201):
                # Parse the Supabase error body for actionable messaging
                supabase_error = ""
                try:
                    body = resp.json()
                    supabase_error = body.get("message") or body.get("error") or ""
                except Exception:
                    supabase_error = resp.text[:120]

                logger.error(
                    "QR Supabase upload failed: HTTP %s — %s",
                    resp.status_code, supabase_error or resp.text[:200],
                )

                # Auth failure: wrong or expired service-role key
                if resp.status_code in (400, 401, 403) and (
                    "Invalid Compact JWS" in supabase_error
                    or "AccessDenied" in supabase_error
                    or "Unauthorized" in supabase_error
                    or "unauthorized" in supabase_error.lower()
                    or "invalid" in supabase_error.lower()
                ):
                    raise AppError(
                        "storage_key_invalid",
                        "Supabase rejected the storage credentials. "
                        "Check that SUPABASE_SERVICE_ROLE_KEY in backend/.env is correct "
                        "(both legacy JWT eyJ... and new sb_secret_... formats are supported). "
                        "Verify the key in Supabase project → Settings → API, then restart the server.",
                        503,
                    )

                raise AppError(
                    "storage_upload_failed",
                    f"Storage upload failed (HTTP {resp.status_code}): {supabase_error}",
                    502,
                )
    except AppError:
        raise
    except Exception as exc:
        logger.exception("QR Supabase upload exception for path %s", storage_path)
        raise AppError(
            "storage_upload_failed",
            "Storage upload failed due to a network error.",
            502,
        ) from exc


async def _delete_qr_from_supabase(storage_path: str) -> None:
    """Best-effort delete of old QR image. Never raises."""
    if not storage_path:
        return
    try:
        from app.config import get_settings
        settings = get_settings()
        if not settings.supabase_url or not settings.supabase_service_role_key:
            return
        bucket = settings.supabase_storage_bucket_screenshots
        url = (
            f"{settings.supabase_url.rstrip('/')}"
            f"/storage/v1/object/{bucket}/{storage_path}"
        )
        headers = {
            "Authorization": f"Bearer {settings.supabase_service_role_key}",
            "apikey": settings.supabase_service_role_key,
        }
        import httpx
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.delete(url, headers=headers)
    except Exception:
        logger.warning("Could not delete old QR from storage: %s", storage_path)


# ── Setting helpers ───────────────────────────────────────────────────────────

async def _get_setting_row(db: AsyncSession, key: str) -> PlatformSetting | None:
    result = await db.execute(
        select(PlatformSetting).where(PlatformSetting.key == key)
    )
    return result.scalar_one_or_none()


async def _set_setting(
    db: AsyncSession, key: str, value: str, actor_agent_id: str
) -> None:
    row = await _get_setting_row(db, key)
    if row is None:
        row = PlatformSetting(
            key=key, value=value, is_public=True, updated_by=actor_agent_id
        )
        db.add(row)
    else:
        row.value = value
        row.updated_by = actor_agent_id
        row.updated_at = utcnow()


async def _get_full_config(db: AsyncSession) -> dict:
    keys = [
        SETTING_UPI_ID,
        SETTING_UPI_DISPLAY_NAME,
        SETTING_UPI_ACCOUNT_NAME,
        SETTING_MANUAL_DEPOSIT_INSTRUCTIONS,
        SETTING_QR_STORAGE_REF,
        SETTING_MANUAL_PAYMENT_ACTIVE,
    ]
    result = await db.execute(
        select(PlatformSetting).where(PlatformSetting.key.in_(keys))
    )
    rows = {r.key: r.value for r in result.scalars().all()}
    return {
        "upi_id": rows.get(SETTING_UPI_ID, ""),
        "upi_display_name": rows.get(SETTING_UPI_DISPLAY_NAME, "AuraPlayFF"),
        "upi_account_name": rows.get(SETTING_UPI_ACCOUNT_NAME, ""),
        "instructions": rows.get(SETTING_MANUAL_DEPOSIT_INSTRUCTIONS, ""),
        "qr_storage_ref": rows.get(SETTING_QR_STORAGE_REF, ""),
        "manual_payment_active": (
            rows.get(SETTING_MANUAL_PAYMENT_ACTIVE, "false") == "true"
        ),
    }


def _razorpay_status() -> tuple[bool, str]:
    """Return (configured, mode) for the Razorpay status display."""
    from app.config import get_settings
    s = get_settings()
    configured = bool(
        s.razorpay_key_id
        and not s.razorpay_key_id.startswith("rzp_test_placeholder")
        and s.razorpay_key_secret
        and not s.razorpay_key_secret.startswith("placeholder")
    )
    return configured, s.razorpay_mode


# ── Request / Response schemas ────────────────────────────────────────────────

class PaymentConfigResponse(BaseModel):
    upi_id: str
    upi_display_name: str
    upi_account_name: str
    instructions: str
    qr_storage_ref: str
    manual_payment_active: bool
    razorpay_configured: bool
    razorpay_mode: str


class UpdatePaymentConfigRequest(BaseModel):
    upi_id: str = Field(..., max_length=200)
    upi_display_name: str = Field(..., max_length=100)
    upi_account_name: str = Field("", max_length=200)
    instructions: str = Field(..., max_length=2000)


class QRUploadResponse(BaseModel):
    qr_storage_ref: str
    message: str


class QRSignedUrlResponse(BaseModel):
    signed_url: str
    expires_in_seconds: int


class ActivationResponse(BaseModel):
    manual_payment_active: bool
    message: str


# ── Signed URL helper ─────────────────────────────────────────────────────────

async def _get_qr_signed_url(storage_path: str, expires_in: int = 300) -> str:
    """
    Generate a short-lived Supabase signed URL for the QR image.
    Service-role key is used server-side and never returned to the browser.
    Path guard must pass before any I/O.
    """
    if not storage_path.startswith("payment-qr/"):
        raise AppError("invalid_qr_path", "Invalid QR storage path.", 400)

    from app.config import get_settings
    settings = get_settings()

    if not settings.supabase_url or not settings.supabase_service_role_key:
        raise AppError(
            "storage_not_configured",
            "Supabase storage is not configured. "
            "Set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY.",
            503,
        )

    # Guard: full HTTPS URL required
    if not settings.supabase_url.startswith("http"):
        raise AppError(
            "storage_misconfigured",
            "SUPABASE_URL must be a full HTTPS URL "
            "(e.g. https://<project-ref>.supabase.co).",
            503,
        )

    # Guard: key must be non-empty — both legacy JWT (eyJ...) and
    # new-format Secret keys (sb_secret_...) are supported.
    key = settings.supabase_service_role_key

    bucket = settings.supabase_storage_bucket_screenshots
    url = (
        f"{settings.supabase_url.rstrip('/')}"
        f"/storage/v1/object/sign/{bucket}/{storage_path}"
    )
    # Send the key on both headers for compatibility with legacy JWT keys and
    # new-format sb_secret_ keys — Supabase Storage accepts either.
    headers = {
        "Authorization": f"Bearer {key}",
        "apikey": key,
        "Content-Type": "application/json",
    }
    payload = {"expiresIn": expires_in}

    try:
        import httpx
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code not in (200, 201):
                supabase_error = ""
                try:
                    body = resp.json()
                    supabase_error = body.get("message") or body.get("error") or ""
                except Exception:
                    supabase_error = resp.text[:120]
                logger.error(
                    "QR signed URL failed: HTTP %s — %s",
                    resp.status_code, supabase_error or resp.text[:200],
                )
                if resp.status_code in (400, 401, 403) and (
                    "Invalid Compact JWS" in supabase_error
                    or "AccessDenied" in supabase_error
                    or "Unauthorized" in supabase_error
                    or "unauthorized" in supabase_error.lower()
                    or "invalid" in supabase_error.lower()
                ):
                    raise AppError(
                        "storage_key_invalid",
                        "Supabase rejected the storage credentials. "
                        "Check SUPABASE_SERVICE_ROLE_KEY in backend/.env "
                        "(both eyJ... JWT and sb_secret_... formats are supported). "
                        "Verify the key in Supabase project → Settings → API.",
                        503,
                    )
                raise AppError(
                    "storage_signed_url_failed",
                    f"Could not generate signed URL (HTTP {resp.status_code}): {supabase_error}",
                    502,
                )
            data = resp.json()
            signed_path: str = data.get("signedURL") or data.get("signed_url") or ""
            if not signed_path:
                raise AppError(
                    "storage_signed_url_empty",
                    "Supabase returned an empty signed URL.",
                    502,
                )
            if signed_path.startswith("http"):
                return signed_path
            # Supabase returns a relative path like:
            #   /object/sign/<bucket>/<path>?token=...
            # The correct absolute URL needs /storage/v1 inserted:
            #   https://<ref>.supabase.co/storage/v1/object/sign/<bucket>/<path>?token=...
            base = settings.supabase_url.rstrip("/")
            if signed_path.startswith("/storage/v1"):
                return f"{base}{signed_path}"
            # Older format: path starts with /object/ — insert /storage/v1
            return f"{base}/storage/v1{signed_path}"
    except AppError:
        raise
    except Exception as exc:
        logger.exception("QR signed URL exception for path %s", storage_path)
        raise AppError(
            "storage_signed_url_failed",
            "Failed to generate signed URL due to a network error.",
            502,
        ) from exc


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get(
    "",
    response_model=PaymentConfigResponse,
    summary="Get full payment configuration (Super Admin only)",
)
async def get_payment_config(
    staff: StaffUser = Depends(get_current_staff),
    db: AsyncSession = Depends(get_db),
) -> PaymentConfigResponse:
    """
    Return the full payment configuration:
    - Manual UPI fields and active status
    - Whether Razorpay is configured (env-var check only, no credentials returned)

    Super Admin only. Agents receive 403.
    """
    _require_super_admin(staff)

    config = await _get_full_config(db)
    rzp_configured, rzp_mode = _razorpay_status()

    return PaymentConfigResponse(
        upi_id=config["upi_id"],
        upi_display_name=config["upi_display_name"],
        upi_account_name=config["upi_account_name"],
        instructions=config["instructions"],
        qr_storage_ref=config["qr_storage_ref"],
        manual_payment_active=config["manual_payment_active"],
        razorpay_configured=rzp_configured,
        razorpay_mode=rzp_mode,
    )


@router.put(
    "",
    response_model=PaymentConfigResponse,
    summary="Update manual payment UPI/text configuration (Super Admin only)",
)
async def update_payment_config(
    body: UpdatePaymentConfigRequest,
    request: Request,
    staff: StaffUser = Depends(get_current_staff),
    db: AsyncSession = Depends(get_db),
) -> PaymentConfigResponse:
    """
    Update UPI ID, display name, account name, and payment instructions.
    Does NOT accept a QR image — use /qr-upload for that.
    Does NOT change activation state — use /activate or /deactivate.

    Super Admin only. Agents receive 403. Audit-logged.
    """
    _require_super_admin(staff)

    old = await _get_full_config(db)

    await _set_setting(db, SETTING_UPI_ID, body.upi_id.strip(), staff.agent_id)
    await _set_setting(db, SETTING_UPI_DISPLAY_NAME, body.upi_display_name.strip(), staff.agent_id)
    await _set_setting(db, SETTING_UPI_ACCOUNT_NAME, body.upi_account_name.strip(), staff.agent_id)
    await _set_setting(db, SETTING_MANUAL_DEPOSIT_INSTRUCTIONS, body.instructions.strip(), staff.agent_id)

    await audit_service.log(
        db=db,
        actor_type=ActorType.staff,
        actor_id=str(staff.id),
        action="payment_config.updated",
        entity_type="platform_setting",
        entity_id="manual_payment_config",
        old_value={
            "upi_id": old["upi_id"],
            "upi_display_name": old["upi_display_name"],
            "upi_account_name": old["upi_account_name"],
        },
        new_value={
            "upi_id": body.upi_id.strip(),
            "upi_display_name": body.upi_display_name.strip(),
            "upi_account_name": body.upi_account_name.strip(),
        },
        ip_address=get_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )

    await db.commit()

    config = await _get_full_config(db)
    rzp_configured, rzp_mode = _razorpay_status()

    return PaymentConfigResponse(
        upi_id=config["upi_id"],
        upi_display_name=config["upi_display_name"],
        upi_account_name=config["upi_account_name"],
        instructions=config["instructions"],
        qr_storage_ref=config["qr_storage_ref"],
        manual_payment_active=config["manual_payment_active"],
        razorpay_configured=rzp_configured,
        razorpay_mode=rzp_mode,
    )


@router.get(
    "/qr-signed-url",
    response_model=QRSignedUrlResponse,
    summary="Get a short-lived signed URL for the current QR image (Super Admin only)",
)
async def get_qr_signed_url(
    staff: StaffUser = Depends(get_current_staff),
    db: AsyncSession = Depends(get_db),
) -> QRSignedUrlResponse:
    """
    Return a 5-minute Supabase signed URL for the currently configured QR image.

    The signed URL is generated server-side using the service-role key.
    Only the temporary, expiring URL is returned — the key is never exposed.

    Returns 404 if no QR image has been uploaded yet.
    Returns 503 if Supabase storage is not configured.

    Super Admin only. Agents receive 403.
    """
    _require_super_admin(staff)

    qr_row = await _get_setting_row(db, SETTING_QR_STORAGE_REF)
    qr_ref = qr_row.value if qr_row else ""

    if not qr_ref:
        raise AppError(
            "qr_not_found",
            "No QR image has been uploaded yet.",
            404,
        )

    signed_url = await _get_qr_signed_url(qr_ref, expires_in=300)
    return QRSignedUrlResponse(signed_url=signed_url, expires_in_seconds=300)


@router.post(
    "/qr-upload",
    response_model=QRUploadResponse,
    summary="Upload or replace QR code image (Super Admin only)",
)
async def upload_qr_image(
    request: Request,
    file: UploadFile = File(..., description="JPEG or PNG QR image, max 5 MB"),
    staff: StaffUser = Depends(get_current_staff),
    db: AsyncSession = Depends(get_db),
) -> QRUploadResponse:
    """
    Upload or replace the UPI QR code image.

    Rules:
    - JPEG or PNG only (validated by magic bytes — not filename or Content-Type).
    - Maximum 5 MB.
    - Storage path is server-generated — client cannot choose it.
    - Old QR image is deleted from Supabase after successful upload.
    - The new storage path is saved in the qr_storage_ref platform setting.

    Super Admin only. Agents receive 403. Audit-logged.
    """
    _require_super_admin(staff)

    file_bytes = await file.read()

    if len(file_bytes) > _QR_MAX_BYTES:
        raise AppError(
            "qr_file_too_large",
            f"QR image exceeds maximum size of {_QR_MAX_BYTES // (1024 * 1024)} MB.",
            422,
        )
    if len(file_bytes) < 8:
        raise AppError("qr_invalid_file", "Invalid file.", 422)

    content_type = _detect_qr_content_type(file_bytes[:16])
    if content_type is None:
        raise AppError(
            "qr_invalid_file_type",
            "Only JPEG and PNG images are accepted for the QR code.",
            422,
        )

    ext = _safe_ext(content_type)
    storage_path = f"{_QR_STORAGE_FOLDER}/{uuid.uuid4()}.{ext}"

    # Read old QR ref for later cleanup (before any mutations)
    old_row = await _get_setting_row(db, SETTING_QR_STORAGE_REF)
    old_ref = old_row.value if old_row else ""

    # Upload to Supabase (outside DB transaction — network I/O)
    await _upload_qr_to_supabase(storage_path, file_bytes, content_type)

    # Persist new storage ref
    await _set_setting(db, SETTING_QR_STORAGE_REF, storage_path, staff.agent_id)

    await audit_service.log(
        db=db,
        actor_type=ActorType.staff,
        actor_id=str(staff.id),
        action="payment_config.qr_uploaded",
        entity_type="platform_setting",
        entity_id="qr_storage_ref",
        old_value={"qr_storage_ref": old_ref},
        new_value={"qr_storage_ref": storage_path},
        ip_address=get_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )

    await db.commit()

    # Best-effort delete old QR after successful commit
    if old_ref and old_ref != storage_path:
        await _delete_qr_from_supabase(old_ref)

    logger.info(
        "QR image uploaded by %s: path=%s size=%d content_type=%s",
        staff.agent_id, storage_path, len(file_bytes), content_type,
    )
    return QRUploadResponse(
        qr_storage_ref=storage_path,
        message="QR image uploaded successfully.",
    )


@router.post(
    "/activate",
    response_model=ActivationResponse,
    summary="Activate manual UPI payment (Super Admin only)",
)
async def activate_manual_payment(
    request: Request,
    staff: StaffUser = Depends(get_current_staff),
    db: AsyncSession = Depends(get_db),
) -> ActivationResponse:
    """
    Enable manual UPI payment for players.
    Super Admin only. Agents receive 403. Audit-logged.
    """
    _require_super_admin(staff)

    await _set_setting(db, SETTING_MANUAL_PAYMENT_ACTIVE, "true", staff.agent_id)

    await audit_service.log(
        db=db,
        actor_type=ActorType.staff,
        actor_id=str(staff.id),
        action="payment_config.manual_activated",
        entity_type="platform_setting",
        entity_id=SETTING_MANUAL_PAYMENT_ACTIVE,
        old_value={"manual_payment_active": "false"},
        new_value={"manual_payment_active": "true"},
        ip_address=get_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )

    await db.commit()

    return ActivationResponse(
        manual_payment_active=True,
        message="Manual UPI payment has been activated.",
    )


@router.post(
    "/deactivate",
    response_model=ActivationResponse,
    summary="Deactivate manual UPI payment (Super Admin only)",
)
async def deactivate_manual_payment(
    request: Request,
    staff: StaffUser = Depends(get_current_staff),
    db: AsyncSession = Depends(get_db),
) -> ActivationResponse:
    """
    Disable manual UPI payment for players.
    Players will see 'Manual payment unavailable' until reactivated.
    Super Admin only. Agents receive 403. Audit-logged.
    """
    _require_super_admin(staff)

    await _set_setting(db, SETTING_MANUAL_PAYMENT_ACTIVE, "false", staff.agent_id)

    await audit_service.log(
        db=db,
        actor_type=ActorType.staff,
        actor_id=str(staff.id),
        action="payment_config.manual_deactivated",
        entity_type="platform_setting",
        entity_id=SETTING_MANUAL_PAYMENT_ACTIVE,
        old_value={"manual_payment_active": "true"},
        new_value={"manual_payment_active": "false"},
        ip_address=get_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )

    await db.commit()

    return ActivationResponse(
        manual_payment_active=False,
        message="Manual UPI payment has been deactivated.",
    )
