"""
Screenshot / proof routes.

Player routes (require player JWT):
  POST /api/tournaments/{id}/proof      — upload a screenshot
  GET  /api/tournaments/{id}/my-proof   — view own uploaded proofs

Admin routes (require staff JWT):
  GET  /api/admin/tournaments/{id}/proof            — list all proofs [screenshot.view]
  GET  /api/admin/tournaments/{id}/proof/{proof_id} — single proof   [screenshot.view]
  POST /api/admin/tournaments/{id}/proof/{proof_id}/review — review  [screenshot.view]

File upload uses multipart/form-data.
The backend validates content via magic-byte inspection — not filename/extension alone.
Storage path is server-generated — client cannot influence it.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, File, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError, PermissionDeniedError
from app.core.permissions import Permission
from app.database import get_db
from app.dependencies import get_current_staff, get_current_user
from app.models.permission import Permission as PermModel, StaffUserPermission
from app.models.screenshot import ProofReviewStatus, ScreenshotProof
from app.models.staff_user import StaffRole, StaffUser
from app.models.user import User
from app.schemas.screenshot import (
    AdminProofListResponse,
    AdminProofResponse,
    MyProofListResponse,
    MyProofResponse,
    ReviewProofRequest,
    ReviewProofResponse,
)
from app.services import screenshot_service
from app.services.screenshot_service import MAX_SCREENSHOTS_PER_ENTRY

player_router = APIRouter(prefix="/api/tournaments", tags=["Player — Screenshots"])
admin_router = APIRouter(prefix="/api/admin/tournaments", tags=["Admin — Screenshots"])


# ── Permission helper ─────────────────────────────────────────────────────────

async def _require_perm(
    staff: StaffUser, db: AsyncSession, perm: Permission
) -> None:
    if staff.role == StaffRole.super_admin:
        return
    # BUG #7 FIX: joinedload replaces N separate db.get() calls
    from sqlalchemy.orm import joinedload
    result = await db.execute(
        select(StaffUserPermission)
        .options(joinedload(StaffUserPermission.permission))
        .where(StaffUserPermission.staff_user_id == staff.id)
    )
    grants = result.unique().scalars().all()
    granted: set[str] = {
        g.permission.code for g in grants if g.permission is not None
    }
    if perm.value not in granted:
        raise PermissionDeniedError(perm.value)


# ══════════════════════════════════════════════════════════════════════════════
# PLAYER ROUTES
# ══════════════════════════════════════════════════════════════════════════════

@player_router.post(
    "/{tournament_id}/proof",
    response_model=MyProofResponse,
    status_code=201,
    summary="Upload a screenshot proof for a completed tournament",
)
async def upload_proof(
    tournament_id: uuid.UUID,
    file: UploadFile = File(..., description="JPEG or PNG screenshot, max 5 MB"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MyProofResponse:
    """
    Upload a screenshot after tournament completion.

    Rules enforced server-side:
    - Upload window = tournament.completed_at + 15 minutes.
    - Maximum 2 screenshots per entry.
    - JPEG or PNG only (validated by magic bytes — not just filename).
    - File size ≤ 5 MB.
    - Player must have an ACTIVE entry in this tournament.
    - Storage path is server-generated; client cannot choose it.
    """
    file_bytes = await file.read()
    original_filename = file.filename or "screenshot"

    proof = await screenshot_service.upload_screenshot(
        db,
        tournament_id=tournament_id,
        user_id=current_user.id,
        file_bytes=file_bytes,
        original_filename=original_filename,
    )
    await db.commit()
    return MyProofResponse.from_proof(proof)


@player_router.get(
    "/{tournament_id}/my-proof",
    response_model=MyProofListResponse,
    summary="View your uploaded proofs for a tournament",
)
async def get_my_proofs(
    tournament_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MyProofListResponse:
    """
    Returns the authenticated player's screenshot proofs.
    Does NOT expose storage_path, review_notes, or other players' proofs.
    """
    from app.services import tournament_service
    entry = await tournament_service.get_entry(db, tournament_id, current_user.id)
    if entry is None:
        from app.core.exceptions import NotEnrolledError
        raise NotEnrolledError()

    proofs = await screenshot_service.list_proofs_for_entry(db, entry.id)
    return MyProofListResponse(
        proofs=[MyProofResponse.from_proof(p) for p in proofs],
        uploads_remaining=max(0, MAX_SCREENSHOTS_PER_ENTRY - len(proofs)),
    )


# ══════════════════════════════════════════════════════════════════════════════
# ADMIN ROUTES
# ══════════════════════════════════════════════════════════════════════════════

@admin_router.get(
    "/{tournament_id}/proof",
    response_model=AdminProofListResponse,
    summary="List all screenshot proofs for a tournament (Admin)",
)
async def admin_list_proofs(
    tournament_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_staff: StaffUser = Depends(get_current_staff),
    review_status: str | None = None,
) -> AdminProofListResponse:
    """
    Returns all proofs for this tournament.
    Accessible during the 24-hour review window (enforced in screenshot_service).
    Includes storage_path for reference (path in private bucket only).
    """
    await _require_perm(current_staff, db, Permission.SCREENSHOT_VIEW)

    status_filter: ProofReviewStatus | None = None
    if review_status:
        try:
            status_filter = ProofReviewStatus(review_status)
        except ValueError:
            status_filter = None

    proofs = await screenshot_service.list_proofs_for_tournament(
        db, tournament_id, review_status=status_filter
    )
    return AdminProofListResponse(
        proofs=[AdminProofResponse.from_proof(p) for p in proofs],
        total=len(proofs),
    )


@admin_router.get(
    "/{tournament_id}/proof/{proof_id}",
    response_model=AdminProofResponse,
    summary="Get a single screenshot proof (Admin)",
)
async def admin_get_proof(
    tournament_id: uuid.UUID,
    proof_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_staff: StaffUser = Depends(get_current_staff),
) -> AdminProofResponse:
    await _require_perm(current_staff, db, Permission.SCREENSHOT_VIEW)
    proof = await screenshot_service.get_proof(db, proof_id)
    # Verify the proof belongs to this tournament
    if proof.tournament_id != tournament_id:
        raise NotFoundError("ScreenshotProof")
    return AdminProofResponse.from_proof(proof)


@admin_router.get(
    "/{tournament_id}/proof/{proof_id}/signed-url",
    summary="Get a short-lived signed URL for a screenshot (Admin)",
)
async def admin_get_proof_signed_url(
    tournament_id: uuid.UUID,
    proof_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_staff: StaffUser = Depends(get_current_staff),
) -> dict:
    """
    Generate a short-lived (5-minute) signed URL for previewing a screenshot
    from the private Supabase storage bucket.

    Security:
    - Requires screenshot.view permission.
    - The URL expires in 300 seconds — never return a permanent public URL.
    - The Supabase service-role key is used server-side only; it is never
      returned to the client.
    - storage_path is validated to start with "screenshots/" to prevent
      path traversal to other bucket paths.
    """
    await _require_perm(current_staff, db, Permission.SCREENSHOT_VIEW)
    proof = await screenshot_service.get_proof(db, proof_id)
    if proof.tournament_id != tournament_id:
        raise NotFoundError("ScreenshotProof")

    signed_url = await screenshot_service.get_signed_url(proof)
    return {"signed_url": signed_url, "expires_in_seconds": 300}


@admin_router.post(
    "/{tournament_id}/proof/{proof_id}/review",
    response_model=ReviewProofResponse,
    summary="Review (approve/reject) a screenshot proof (Admin)",
)
async def review_proof(
    tournament_id: uuid.UUID,
    proof_id: uuid.UUID,
    body: ReviewProofRequest,
    db: AsyncSession = Depends(get_db),
    current_staff: StaffUser = Depends(get_current_staff),
) -> ReviewProofResponse:
    """
    Approve or reject a screenshot.
    Only accessible during the 24-hour review window (enforced in service).
    Every review decision is audit-logged.
    """
    await _require_perm(current_staff, db, Permission.SCREENSHOT_VIEW)

    proof = await screenshot_service.get_proof(db, proof_id)
    if proof.tournament_id != tournament_id:
        raise NotFoundError("ScreenshotProof")

    decision = ProofReviewStatus(body.decision)
    proof = await screenshot_service.review_proof(
        db,
        proof_id=proof_id,
        decision=decision,
        notes=body.notes,
        reviewed_by_id=current_staff.id,
    )
    await db.commit()

    return ReviewProofResponse(
        message=f"Screenshot {decision.value}.",
        proof_id=proof.id,
        review_status=proof.review_status.value,
        reviewed_at=proof.reviewed_at,  # type: ignore[arg-type]
    )
