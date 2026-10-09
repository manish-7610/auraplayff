"""
Screenshot retention cleanup job — Phase 7.

Rules (already approved in Phase 4)
-------------------------------------
- Screenshots are retained for ~30 days from upload (expires_at column).
- The 24-hour review window (review_deadline) only gates normal UI access;
  it does NOT trigger deletion.
- Deletion is idempotent: if storage removal fails the DB row is kept so
  the next run will retry.

Execution flow
--------------
1. SELECT rows WHERE expires_at <= now() (batch, LIMIT per run).
2. For each row:
   a. Attempt to delete the object from Supabase Storage via the HTTP API.
   b. Only delete the DB row if storage deletion succeeded (or the object
      was already absent — 404 is treated as success).
3. Return the number of rows permanently removed.

Security
--------
- Supabase service-role key is read from settings, never from the DB row.
- Storage path comes from the DB row (server-generated, never client input).
- No player data is returned to callers.

Idempotency
-----------
- Rows with a past expires_at that failed the previous run remain in the DB
  and will be picked up again on the next execution.
- Successful deletions remove the DB row, so the set shrinks monotonically.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models._types import utcnow
from app.models.screenshot import ScreenshotProof

logger = logging.getLogger("auraplayff.jobs.screenshot_cleanup")

# Maximum screenshots processed per single job run to cap DB/network load
BATCH_SIZE: int = 200


async def run_screenshot_cleanup(db: AsyncSession) -> int:
    """
    Delete expired screenshots from Supabase Storage and remove their DB rows.

    Returns the number of rows successfully cleaned up.
    """
    settings = get_settings()
    now = utcnow()

    stmt = (
        select(ScreenshotProof)
        .where(ScreenshotProof.expires_at <= now)
        .order_by(ScreenshotProof.expires_at)
        .limit(BATCH_SIZE)
    )
    result = await db.execute(stmt)
    expired: list[ScreenshotProof] = list(result.scalars().all())

    if not expired:
        logger.info("Screenshot cleanup: no expired screenshots found.")
        return 0

    logger.info("Screenshot cleanup: found %d expired screenshot(s) to process.", len(expired))

    supabase_url = settings.supabase_url.rstrip("/")
    bucket = settings.supabase_storage_bucket_screenshots
    service_key = settings.supabase_service_role_key

    if not supabase_url or not service_key:
        logger.warning(
            "Supabase credentials not configured — skipping storage deletion. "
            "DB rows will NOT be removed."
        )
        return 0

    cleaned = 0
    async with httpx.AsyncClient(timeout=30.0) as client:
        for proof in expired:
            storage_deleted = await _delete_from_storage(
                client=client,
                supabase_url=supabase_url,
                bucket=bucket,
                service_key=service_key,
                storage_path=proof.storage_path,
                proof_id=proof.id,
            )
            if storage_deleted:
                await db.delete(proof)
                cleaned += 1

    logger.info(
        "Screenshot cleanup complete: %d/%d screenshots removed.",
        cleaned, len(expired),
    )
    return cleaned


async def _delete_from_storage(
    *,
    client: httpx.AsyncClient,
    supabase_url: str,
    bucket: str,
    service_key: str,
    storage_path: str,
    proof_id: object,
) -> bool:
    """
    Delete a single object from Supabase Storage.

    Returns True if the object no longer exists (deleted or was already absent).
    Returns False if the deletion request failed with a non-404 error.
    """
    # Supabase Storage delete endpoint: DELETE /storage/v1/object/<bucket>/<path>
    url = f"{supabase_url}/storage/v1/object/{bucket}/{storage_path}"
    headers = {
        "Authorization": f"Bearer {service_key}",
        "apikey": service_key,
    }

    try:
        response = await client.delete(url, headers=headers)

        if response.status_code in (200, 204):
            logger.debug("Storage: deleted %s (proof_id=%s)", storage_path, proof_id)
            return True

        if response.status_code == 404:
            # Already absent — treat as success so the DB row is cleaned up
            logger.debug(
                "Storage: object not found (already deleted?) %s (proof_id=%s)",
                storage_path, proof_id,
            )
            return True

        logger.warning(
            "Storage: unexpected status %d deleting %s (proof_id=%s): %s",
            response.status_code, storage_path, proof_id, response.text[:200],
        )
        return False

    except httpx.RequestError as exc:
        logger.error(
            "Storage: network error deleting %s (proof_id=%s): %s",
            storage_path, proof_id, exc,
        )
        return False
