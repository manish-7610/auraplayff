"""
Pydantic v2 schemas for screenshot/proof endpoints.

Player schemas:
  - Expose: id, uploaded_at, content_type, file_size_bytes, review_status.
  - Never expose: storage_path (private bucket URL), review_notes to players,
    reviewed_by_id, or other players' proofs.

Admin schemas:
  - Expose full detail including review_notes, reviewed_by, storage_path
    reference (path only — never a presigned URL in this schema; the
    admin can request a signed URL separately).
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


# ── Player-facing ─────────────────────────────────────────────────────────────

class MyProofResponse(BaseModel):
    """Screenshot as seen by the uploading player."""
    proof_id: uuid.UUID
    tournament_id: uuid.UUID
    entry_id: uuid.UUID
    original_filename: str
    content_type: str
    file_size_bytes: int
    uploaded_at: datetime
    upload_deadline: datetime
    review_status: str

    @classmethod
    def from_proof(
        cls, p: "ScreenshotProof"  # type: ignore[name-defined]  # noqa: F821
    ) -> "MyProofResponse":
        return cls(
            proof_id=p.id,
            tournament_id=p.tournament_id,
            entry_id=p.entry_id,
            original_filename=p.original_filename,
            content_type=p.content_type,
            file_size_bytes=p.file_size_bytes,
            uploaded_at=p.uploaded_at,
            upload_deadline=p.upload_deadline,
            review_status=p.review_status.value,
        )


class MyProofListResponse(BaseModel):
    proofs: list[MyProofResponse]
    uploads_remaining: int   # MAX_SCREENSHOTS_PER_ENTRY - len(proofs)


# ── Admin-facing ──────────────────────────────────────────────────────────────

class AdminProofResponse(BaseModel):
    """Full proof detail for staff review."""
    proof_id: uuid.UUID
    tournament_id: uuid.UUID
    entry_id: uuid.UUID
    user_id: uuid.UUID
    # storage_path is provided for reference only; never used to serve
    # content directly — admins use the Supabase dashboard or a signed URL endpoint
    storage_path: str
    original_filename: str
    content_type: str
    file_size_bytes: int
    uploaded_at: datetime
    upload_deadline: datetime
    review_deadline: datetime
    expires_at: datetime
    review_status: str
    reviewed_by_id: Optional[uuid.UUID] = None
    reviewed_at: Optional[datetime] = None
    review_notes: Optional[str] = None

    @classmethod
    def from_proof(
        cls, p: "ScreenshotProof"  # type: ignore[name-defined]  # noqa: F821
    ) -> "AdminProofResponse":
        return cls(
            proof_id=p.id,
            tournament_id=p.tournament_id,
            entry_id=p.entry_id,
            user_id=p.user_id,
            storage_path=p.storage_path,
            original_filename=p.original_filename,
            content_type=p.content_type,
            file_size_bytes=p.file_size_bytes,
            uploaded_at=p.uploaded_at,
            upload_deadline=p.upload_deadline,
            review_deadline=p.review_deadline,
            expires_at=p.expires_at,
            review_status=p.review_status.value,
            reviewed_by_id=p.reviewed_by_id,
            reviewed_at=p.reviewed_at,
            review_notes=p.review_notes,
        )


class AdminProofListResponse(BaseModel):
    proofs: list[AdminProofResponse]
    total: int


# ── Review request ─────────────────────────────────────────────────────────────

class ReviewProofRequest(BaseModel):
    decision: str = Field(
        ...,
        pattern=r"^(approved|rejected)$",
        description="Review decision: 'approved' or 'rejected'",
    )
    notes: Optional[str] = Field(None, max_length=500)


class ReviewProofResponse(BaseModel):
    message: str
    proof_id: uuid.UUID
    review_status: str
    reviewed_at: datetime
