"""
Screenshot service — tournament proof upload, review, and retention logic.

Upload rules (enforced server-side, never client-supplied):
  - Window:  tournament.completed_at + 15 minutes.
  - Limit:   max 2 screenshots per tournament entry.
  - Format:  JPEG or PNG only (validated by magic-byte inspection).
  - Size:    configurable MAX_SCREENSHOT_SIZE_MB (default 5 MB).
  - Path:    server-generated UUID path — client cannot choose storage path.

Review rules:
  - Staff can review for 24 hours after tournament.completed_at.
  - Files are NOT deleted when review window closes.
  - Retention: ~30 days from upload (expires_at), for future cleanup job.

Supabase Storage integration:
  - Uses the service-role key from settings (NEVER exposed to frontend).
  - Uploads via the httpx-based supabase-py client.
  - If Supabase credentials are not configured, raises StorageError.
  - The storage path is:
      screenshots/<tournament_id>/<user_id>/<random_uuid>.<ext>

Security properties:
  - user_id and entry_id are derived from the authenticated JWT + DB lookup.
  - Client cannot supply storage_path, user_id, or entry_id.
  - MIME type is determined from file header bytes, not filename/Content-Type.
  - Path traversal is impossible: path is fully generated server-side.
"""
from __future__ import annotations

import logging
import os
import uuid
from datetime import timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    FileTooLargeError,
    InvalidFileTypeError,
    NotEnrolledError,
    ScreenshotLimitReachedError,
    ScreenshotUploadWindowClosedError,
    StorageError,
    TournamentAlreadyCancelledError,
)
from app.models._types import utcnow
from app.models.audit_log import ActorType
from app.models.screenshot import ProofReviewStatus, ScreenshotProof
from app.models.tournament import EntryStatus, TournamentEntry, TournamentStatus
from app.services import audit_service

logger = logging.getLogger("auraplayff.screenshot")

# Upload window: 15 minutes after tournament.completed_at
UPLOAD_WINDOW_MINUTES = 15
# Staff review window: 24 hours after tournament.completed_at
REVIEW_WINDOW_HOURS = 24
# File retention: 30 days after upload
RETENTION_DAYS = 30
# Maximum screenshots per entry
MAX_SCREENSHOTS_PER_ENTRY = 2
# Default maximum file size
MAX_SCREENSHOT_SIZE_MB = 5
MAX_SCREENSHOT_SIZE_BYTES = MAX_SCREENSHOT_SIZE_MB * 1024 * 1024

# Allowed MIME types and their magic-byte signatures
_ALLOWED_TYPES: dict[str, tuple[bytes, ...]] = {
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/png":  (b"\x89PNG\r\n\x1a\n",),
}
_ALLOWED_EXTENSIONS = {"jpg", "jpeg", "png"}


# ── File validation ───────────────────────────────────────────────────────────

def _detect_content_type(header_bytes: bytes) -> str | None:
    """
    Detect image type from file header magic bytes.
    Returns the MIME type string or None if not a supported image.
    Never trusts the filename extension or the client-supplied Content-Type.
    """
    for mime, signatures in _ALLOWED_TYPES.items():
        for sig in signatures:
            if header_bytes.startswith(sig):
                return mime
    return None


def _safe_extension(content_type: str) -> str:
    return "jpg" if content_type == "image/jpeg" else "png"


def validate_screenshot_file(
    file_bytes: bytes,
    original_filename: str,
) -> str:
    """
    Validate a screenshot file and return its detected MIME type.

    Raises:
        FileTooLargeError     — exceeds MAX_SCREENSHOT_SIZE_BYTES
        InvalidFileTypeError  — not a JPEG or PNG (magic-byte check)
    """
    if len(file_bytes) > MAX_SCREENSHOT_SIZE_BYTES:
        raise FileTooLargeError(MAX_SCREENSHOT_SIZE_MB)

    if len(file_bytes) < 8:
        raise InvalidFileTypeError()

    content_type = _detect_content_type(file_bytes[:16])
    if content_type is None:
        raise InvalidFileTypeError()

    return content_type


# ── Upload window check ───────────────────────────────────────────────────────

def assert_upload_window_open(tournament: Any) -> None:
    """
    Assert the 15-minute upload window is still open.
    Window = tournament.completed_at + UPLOAD_WINDOW_MINUTES.
    Raises ScreenshotUploadWindowClosedError if closed.
    """
    if tournament.status == TournamentStatus.cancelled:
        raise TournamentAlreadyCancelledError()

    if tournament.completed_at is None:
        from app.core.exceptions import ConflictError
        raise ConflictError("Tournament is not yet completed.")

    now = utcnow()
    completed_at = tournament.completed_at
    if completed_at.tzinfo is None:
        completed_at = completed_at.replace(tzinfo=timezone.utc)

    deadline = completed_at + timedelta(minutes=UPLOAD_WINDOW_MINUTES)
    if now > deadline:
        raise ScreenshotUploadWindowClosedError()


def assert_review_window_open(tournament: Any) -> None:
    """
    Assert the 24-hour staff review window is still open.
    Used by admin review endpoints to block access after the window.
    """
    if tournament.completed_at is None:
        return  # not yet completed — no window constraint yet

    now = utcnow()
    completed_at = tournament.completed_at
    if completed_at.tzinfo is None:
        completed_at = completed_at.replace(tzinfo=timezone.utc)

    deadline = completed_at + timedelta(hours=REVIEW_WINDOW_HOURS)
    if now > deadline:
        from app.core.exceptions import ConflictError
        raise ConflictError(
            "The 24-hour review window for this tournament has closed."
        )


# ── Supabase storage ──────────────────────────────────────────────────────────

async def _upload_to_supabase(
    storage_path: str,
    file_bytes: bytes,
    content_type: str,
) -> None:
    """
    Upload file bytes to the private Supabase storage bucket.

    Uses httpx for the HTTP call so it works in the async context without
    importing the full supabase-py client.  The service-role key is loaded
    from settings and never exposed outside this function.

    Raises StorageError on any failure.
    """
    from app.config import get_settings
    settings = get_settings()

    if not settings.supabase_url or not settings.supabase_service_role_key:
        raise StorageError(
            "Supabase storage is not configured. "
            "Set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY."
        )

    bucket = settings.supabase_storage_bucket_screenshots
    url = (
        f"{settings.supabase_url.rstrip('/')}"
        f"/storage/v1/object/{bucket}/{storage_path}"
    )
    headers = {
        "Authorization": f"Bearer {settings.supabase_service_role_key}",
        "Content-Type": content_type,
        "x-upsert": "false",  # never overwrite
    }

    try:
        import httpx
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(url, content=file_bytes, headers=headers)
            if response.status_code not in (200, 201):
                logger.error(
                    "Supabase upload failed: %s %s",
                    response.status_code,
                    response.text[:200],
                )
                raise StorageError(
                    f"Storage upload failed (HTTP {response.status_code})."
                )
    except StorageError:
        raise
    except Exception as exc:
        logger.exception("Supabase upload exception for path %s", storage_path)
        raise StorageError("Storage upload failed due to a network error.") from exc


async def _delete_from_supabase(storage_path: str) -> None:
    """
    Delete a file from Supabase storage.  Used for rollback if DB save fails.
    Errors are logged but never re-raised.
    """
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
    }
    try:
        import httpx
        async with httpx.AsyncClient(timeout=15.0) as client:
            await client.delete(url, headers=headers)
    except Exception:
        logger.exception("Failed to delete %s from Supabase (rollback)", storage_path)


# ── Upload screenshot ─────────────────────────────────────────────────────────

async def upload_screenshot(
    db: AsyncSession,
    *,
    tournament_id: uuid.UUID,
    user_id: uuid.UUID,
    file_bytes: bytes,
    original_filename: str,
) -> ScreenshotProof:
    """
    Validate and upload a screenshot proof for a completed tournament.

    Steps:
      1. Load tournament — assert completed, not cancelled.
      2. Assert upload window (completed_at + 15 min) is still open.
      3. Verify player has an ACTIVE entry.
      4. Assert upload limit < 2.
      5. Validate file (magic bytes + size).
      6. Generate a server-side storage path.
      7. Upload to Supabase (before DB write).
      8. Persist ScreenshotProof row.
      9. Audit log.
      10. Flush — caller commits.

    If DB flush fails after a successful upload, attempt to delete the
    uploaded object from Supabase to avoid orphaned files.
    """
    from app.models.tournament import Tournament

    # 1. Load and validate tournament state
    t_result = await db.execute(
        select(Tournament).where(Tournament.id == tournament_id)
    )
    tournament = t_result.scalar_one_or_none()
    if tournament is None:
        from app.core.exceptions import TournamentNotFoundError
        raise TournamentNotFoundError()

    # 2. Upload window
    assert_upload_window_open(tournament)

    # 3. Verify active entry
    e_result = await db.execute(
        select(TournamentEntry).where(
            TournamentEntry.tournament_id == tournament_id,
            TournamentEntry.user_id == user_id,
            TournamentEntry.status == EntryStatus.active,
        )
    )
    entry = e_result.scalar_one_or_none()
    if entry is None:
        raise NotEnrolledError()

    # 4. Check upload limit
    count_result = await db.execute(
        select(func.count()).select_from(ScreenshotProof).where(
            ScreenshotProof.entry_id == entry.id
        )
    )
    current_count = count_result.scalar_one()
    if current_count >= MAX_SCREENSHOTS_PER_ENTRY:
        raise ScreenshotLimitReachedError()

    # 5. Validate file
    content_type = validate_screenshot_file(file_bytes, original_filename)
    ext = _safe_extension(content_type)

    # 6. Generate server-side storage path
    safe_filename = f"{uuid.uuid4()}.{ext}"
    storage_path = f"screenshots/{tournament_id}/{user_id}/{safe_filename}"
    # Sanitise original filename (strip path components)
    safe_original = os.path.basename(original_filename)[:255]

    # Compute window timestamps
    now = utcnow()
    completed_at = tournament.completed_at
    if completed_at.tzinfo is None:
        completed_at = completed_at.replace(tzinfo=timezone.utc)

    upload_deadline = completed_at + timedelta(minutes=UPLOAD_WINDOW_MINUTES)
    review_deadline = completed_at + timedelta(hours=REVIEW_WINDOW_HOURS)
    expires_at = now + timedelta(days=RETENTION_DAYS)

    # 7. Upload to Supabase
    await _upload_to_supabase(storage_path, file_bytes, content_type)

    # 8. Persist record
    proof = ScreenshotProof(
        tournament_id=tournament_id,
        entry_id=entry.id,
        user_id=user_id,
        storage_path=storage_path,
        original_filename=safe_original,
        content_type=content_type,
        file_size_bytes=len(file_bytes),
        upload_deadline=upload_deadline,
        review_deadline=review_deadline,
        expires_at=expires_at,
        review_status=ProofReviewStatus.pending,
    )
    db.add(proof)

    try:
        await db.flush([proof])
    except Exception as exc:
        # Rollback: remove the uploaded file so storage stays clean
        await _delete_from_supabase(storage_path)
        raise

    # 9. Audit
    await audit_service.log(
        db=db,
        actor_type=ActorType.user,
        actor_id=str(user_id),
        action="screenshot.uploaded",
        entity_type="screenshot_proof",
        entity_id=str(proof.id),
        new_value={
            "tournament_id": str(tournament_id),
            "storage_path": storage_path,
            "content_type": content_type,
            "file_size_bytes": len(file_bytes),
        },
    )

    logger.info(
        "Screenshot uploaded: proof=%s entry=%s path=%s",
        proof.id, entry.id, storage_path,
    )
    return proof


# ── Fetch helpers ─────────────────────────────────────────────────────────────

async def list_proofs_for_entry(
    db: AsyncSession,
    entry_id: uuid.UUID,
) -> list[ScreenshotProof]:
    result = await db.execute(
        select(ScreenshotProof)
        .where(ScreenshotProof.entry_id == entry_id)
        .order_by(ScreenshotProof.uploaded_at.asc())
    )
    return list(result.scalars().all())


async def list_proofs_for_tournament(
    db: AsyncSession,
    tournament_id: uuid.UUID,
    *,
    review_status: ProofReviewStatus | None = None,
) -> list[ScreenshotProof]:
    q = select(ScreenshotProof).where(
        ScreenshotProof.tournament_id == tournament_id
    )
    if review_status is not None:
        q = q.where(ScreenshotProof.review_status == review_status)
    result = await db.execute(q.order_by(ScreenshotProof.uploaded_at.asc()))
    return list(result.scalars().all())


async def get_proof(
    db: AsyncSession,
    proof_id: uuid.UUID,
) -> ScreenshotProof:
    result = await db.execute(
        select(ScreenshotProof).where(ScreenshotProof.id == proof_id)
    )
    proof = result.scalar_one_or_none()
    if proof is None:
        from app.core.exceptions import NotFoundError
        raise NotFoundError("ScreenshotProof")
    return proof


# ── Staff review ──────────────────────────────────────────────────────────────

async def review_proof(
    db: AsyncSession,
    proof_id: uuid.UUID,
    *,
    decision: ProofReviewStatus,
    notes: str | None,
    reviewed_by_id: uuid.UUID,
) -> ScreenshotProof:
    """
    Staff review action: approve or reject a screenshot.
    Only valid during the 24-hour review window.
    Caller commits.
    """
    proof = await get_proof(db, proof_id)

    # Load tournament to check review window
    from app.models.tournament import Tournament
    t_result = await db.execute(
        select(Tournament).where(Tournament.id == proof.tournament_id)
    )
    tournament = t_result.scalar_one_or_none()
    if tournament:
        assert_review_window_open(tournament)

    now = utcnow()
    old_status = proof.review_status.value
    proof.review_status = decision
    proof.reviewed_by_id = reviewed_by_id
    proof.reviewed_at = now
    proof.review_notes = notes
    await db.flush([proof])

    await audit_service.log(
        db=db,
        actor_type=ActorType.staff,
        actor_id=str(reviewed_by_id),
        action="screenshot.reviewed",
        entity_type="screenshot_proof",
        entity_id=str(proof_id),
        old_value={"review_status": old_status},
        new_value={
            "review_status": decision.value,
            "notes": notes,
        },
    )
    return proof


# ── Signed URL generation ─────────────────────────────────────────────────────

async def get_signed_url(proof: "ScreenshotProof", expires_in: int = 300) -> str:  # noqa: F821
    """
    Generate a short-lived signed URL for a private Supabase Storage object.

    Args:
        proof:      ScreenshotProof ORM row containing storage_path.
        expires_in: Signed URL lifetime in seconds (default 300 = 5 minutes).

    Returns:
        The signed URL string.

    Security:
        - The service-role key is loaded from settings and used only server-side.
        - The path is validated to prevent traversal outside the screenshots prefix.
        - The URL expires after `expires_in` seconds.

    Raises:
        StorageError if Supabase is not configured or the API call fails.
    """
    from app.config import get_settings
    settings = get_settings()

    if not settings.supabase_url or not settings.supabase_service_role_key:
        raise StorageError(
            "Supabase storage is not configured. "
            "Set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY."
        )

    # Path traversal guard — all screenshot paths must start with screenshots/
    storage_path = proof.storage_path
    if not storage_path.startswith("screenshots/"):
        raise StorageError("Invalid storage path.")

    bucket = settings.supabase_storage_bucket_screenshots
    url = (
        f"{settings.supabase_url.rstrip('/')}"
        f"/storage/v1/object/sign/{bucket}/{storage_path}"
    )
    headers = {
        "Authorization": f"Bearer {settings.supabase_service_role_key}",
        "Content-Type": "application/json",
    }
    payload = {"expiresIn": expires_in}

    try:
        import httpx
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(url, json=payload, headers=headers)
            if response.status_code not in (200, 201):
                logger.error(
                    "Supabase signed URL failed: %s %s",
                    response.status_code,
                    response.text[:200],
                )
                raise StorageError(
                    f"Could not generate signed URL (HTTP {response.status_code})."
                )
            data = response.json()
            # Supabase returns: {"signedURL": "/storage/v1/..."}
            signed_path = data.get("signedURL") or data.get("signed_url") or ""
            if not signed_path:
                raise StorageError("Supabase returned empty signed URL.")
            # Build absolute URL
            if signed_path.startswith("http"):
                return signed_path
            return f"{settings.supabase_url.rstrip('/')}{signed_path}"
    except StorageError:
        raise
    except Exception as exc:
        logger.exception("Signed URL exception for path %s", storage_path)
        raise StorageError("Failed to generate signed URL.") from exc
