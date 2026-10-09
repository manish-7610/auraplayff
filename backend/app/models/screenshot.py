"""
ScreenshotProof model — tournament match evidence uploaded by players.

Upload rules:
  - Maximum 2 screenshots per tournament entry.
  - JPEG / PNG only (validated server-side by content inspection).
  - Upload window: tournament.completed_at + 15 minutes.
  - After the window, uploads are rejected.

Review rules:
  - Staff can review screenshots for 24 hours after tournament.completed_at.
  - Files are NOT deleted after the review window.
  - Retention period: ~30 days from upload (expires_at).
  - A future cleanup job uses expires_at to permanently remove old evidence.
  - Storage path is server-generated (random UUID-based) — players cannot
    choose their path.

Storage:
  - Private Supabase Storage bucket (configured via SUPABASE_STORAGE_BUCKET_SCREENSHOTS).
  - The service-role key never leaves the backend.
  - storage_path is the full object path within the bucket.

Review status:
  pending  — uploaded, not yet reviewed
  approved — reviewed and accepted as valid evidence
  rejected — reviewed and flagged as invalid/manipulated
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

import enum as pyenum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models._types import utcnow


class ProofReviewStatus(str, pyenum.Enum):
    pending  = "pending"
    approved = "approved"
    rejected = "rejected"


class ScreenshotProof(Base):
    __tablename__ = "screenshot_proofs"
    __table_args__ = (
        CheckConstraint("file_size_bytes > 0", name="ck_proof_file_size_positive"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)

    tournament_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tournaments.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    entry_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tournament_entries.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    # Denormalised — derived from the entry, never from client
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    # ── Storage metadata ───────────────────────────────────────────────────────
    # Server-generated path in private Supabase bucket.
    # Format: "screenshots/<tournament_id>/<user_id>/<uuid>.<ext>"
    storage_path: Mapped[str] = mapped_column(String(500), nullable=False)
    # Sanitised original filename (client-supplied but stripped of path components)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(50), nullable=False)  # image/jpeg or image/png
    file_size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)

    # ── Window tracking ────────────────────────────────────────────────────────
    # When this proof was uploaded
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        server_default=text("now()"),
        index=True,
    )
    # Upload must have occurred before this time (completed_at + 15 minutes)
    upload_deadline: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    # After this, staff review is no longer accessible via normal UI (completed_at + 24h)
    review_deadline: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    # Files are retained until this date (~30 days from upload) for cleanup job
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )

    # ── Review ─────────────────────────────────────────────────────────────────
    review_status: Mapped[ProofReviewStatus] = mapped_column(
        Enum(ProofReviewStatus, name="proof_review_status_enum", native_enum=False),
        nullable=False,
        default=ProofReviewStatus.pending,
        server_default=text("'pending'"),
        index=True,
    )
    reviewed_by_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("staff_users.id", ondelete="SET NULL"), nullable=True
    )
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    review_notes: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)

    # ── Relationships ──────────────────────────────────────────────────────────
    tournament: Mapped["Tournament"] = relationship(  # type: ignore[name-defined]
        "Tournament", lazy="select"
    )
    entry: Mapped["TournamentEntry"] = relationship(  # type: ignore[name-defined]
        "TournamentEntry", lazy="select"
    )
    user: Mapped["User"] = relationship(  # type: ignore[name-defined]
        "User", lazy="select"
    )

    def __repr__(self) -> str:
        return (
            f"<ScreenshotProof "
            f"entry={self.entry_id} "
            f"status={self.review_status.value} "
            f"path={self.storage_path!r}>"
        )
