"""
Tournament and TournamentEntry models.

Tournament lifecycle (status machine):
  DRAFT          — created but not yet open for registration
  OPEN           — accepting player registrations
  CONFIRMED      — registration closed, waiting for start
  ROOM_RELEASED  — room credentials published to enrolled players
  LIVE           — tournament is currently running
  RESULT_ENTRY   — match finished, results being entered
  COMPLETED      — results verified and prizes distributed
  CANCELLED      — cancelled from OPEN, CONFIRMED, or ROOM_RELEASED (all entries refunded)

Valid forward transitions:
  DRAFT         → OPEN
  OPEN          → CONFIRMED | CANCELLED
  CONFIRMED     → ROOM_RELEASED | CANCELLED
  ROOM_RELEASED → LIVE
  LIVE          → RESULT_ENTRY
  RESULT_ENTRY  → COMPLETED

No backward transitions. No transitions from COMPLETED or CANCELLED.

Supported entry fees are validated in tournament_service.py against
SUPPORTED_ENTRY_FEES — adding a new fee value requires only one change there.

Sheet numbers 1–50 are assigned atomically via a DB sequence (tournament-scoped
counter on the Tournament row itself) with a FOR UPDATE lock, so no two players
can get the same sheet number regardless of concurrent requests.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Optional

import enum as pyenum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models._types import utcnow


# ── Status enums ──────────────────────────────────────────────────────────────

class TournamentStatus(str, pyenum.Enum):
    draft          = "draft"
    open           = "open"
    confirmed      = "confirmed"
    room_released  = "room_released"
    live           = "live"
    result_entry   = "result_entry"
    completed      = "completed"
    cancelled      = "cancelled"


class TournamentMode(str, pyenum.Enum):
    solo = "solo"


class TournamentGame(str, pyenum.Enum):
    free_fire = "free_fire"


class EntryStatus(str, pyenum.Enum):
    active       = "active"
    refunded     = "refunded"
    disqualified = "disqualified"


# ── Valid status transitions ──────────────────────────────────────────────────
# Defines the only allowed (from_status → to_status) moves.
# Enforced in tournament_service.py — never bypassed.

VALID_TRANSITIONS: dict[TournamentStatus, set[TournamentStatus]] = {
    TournamentStatus.draft:         {TournamentStatus.open},
    TournamentStatus.open:          {TournamentStatus.confirmed, TournamentStatus.cancelled},
    TournamentStatus.confirmed:     {TournamentStatus.room_released, TournamentStatus.cancelled},
    TournamentStatus.room_released: {TournamentStatus.live, TournamentStatus.cancelled},
    TournamentStatus.live:          {TournamentStatus.result_entry},
    TournamentStatus.result_entry:  {TournamentStatus.completed},
    TournamentStatus.completed:     set(),   # terminal
    TournamentStatus.cancelled:     set(),   # terminal
}

# States from which cancellation is allowed
CANCELLABLE_STATUSES: frozenset[TournamentStatus] = frozenset({
    TournamentStatus.open,
    TournamentStatus.confirmed,
    TournamentStatus.room_released,   # cancellable pre-LIVE after room release
})

# States that accept new player joins
JOINABLE_STATUSES: frozenset[TournamentStatus] = frozenset({
    TournamentStatus.open,
    TournamentStatus.room_released,   # players may join until the tournament goes LIVE
})


# ── Tournament model ──────────────────────────────────────────────────────────

class Tournament(Base):
    __tablename__ = "tournaments"
    __table_args__ = (
        CheckConstraint("entry_fee > 0", name="ck_tournament_entry_fee_positive"),
        CheckConstraint("max_players > 0", name="ck_tournament_max_players_positive"),
        CheckConstraint(
            "min_players <= max_players",
            name="ck_tournament_min_le_max",
        ),
        CheckConstraint(
            "player_count >= 0",
            name="ck_tournament_player_count_non_negative",
        ),
        CheckConstraint(
            "next_sheet_number >= 1",
            name="ck_tournament_next_sheet_positive",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)

    # Human-readable sequential code, e.g. "T-0001"
    tournament_code: Mapped[str] = mapped_column(
        String(20), nullable=False, unique=True, index=True
    )

    title: Mapped[str] = mapped_column(String(120), nullable=False)

    game: Mapped[TournamentGame] = mapped_column(
        Enum(TournamentGame, name="tournament_game_enum", native_enum=False),
        nullable=False,
        default=TournamentGame.free_fire,
        server_default=text("'free_fire'"),
    )
    mode: Mapped[TournamentMode] = mapped_column(
        Enum(TournamentMode, name="tournament_mode_enum", native_enum=False),
        nullable=False,
        default=TournamentMode.solo,
        server_default=text("'solo'"),
    )

    entry_fee: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    max_players: Mapped[int] = mapped_column(
        Integer, nullable=False, default=50, server_default=text("50")
    )
    min_players: Mapped[int] = mapped_column(
        Integer, nullable=False, default=40, server_default=text("40")
    )

    # Running count of ACTIVE entries — updated atomically on join/refund.
    # Used for slot availability checks without an extra COUNT query.
    player_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )

    # Next sheet number to assign — incremented atomically under FOR UPDATE lock.
    # Starts at 1; after 50 assignments the tournament is full.
    next_sheet_number: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )

    status: Mapped[TournamentStatus] = mapped_column(
        Enum(TournamentStatus, name="tournament_status_enum", native_enum=False),
        nullable=False,
        default=TournamentStatus.draft,
        server_default=text("'draft'"),
        index=True,
    )

    scheduled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )

    # ── Room credentials (nullable until released) ────────────────────────────
    room_id: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    room_password: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    room_released_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    room_released_by_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("staff_users.id", ondelete="SET NULL"), nullable=True
    )

    # ── Lifecycle timestamps ──────────────────────────────────────────────────
    started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cancelled_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cancellation_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # ── Creator tracking (immutable) ──────────────────────────────────────────
    created_by_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("staff_users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    # Snapshot of the creator's display name at creation time so reports
    # remain accurate even if the staff account is later renamed.
    created_by_display_name: Mapped[str] = mapped_column(String(100), nullable=False)
    created_by_role: Mapped[str] = mapped_column(String(20), nullable=False)

    # ── Completion/cancellation staff ─────────────────────────────────────────
    completed_by_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("staff_users.id", ondelete="SET NULL"), nullable=True
    )
    cancelled_by_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("staff_users.id", ondelete="SET NULL"), nullable=True
    )

    # ── Timestamps ────────────────────────────────────────────────────────────
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        server_default=text("now()"),
        index=True,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        server_default=text("now()"),
    )

    # ── Relationships ─────────────────────────────────────────────────────────
    entries: Mapped[list["TournamentEntry"]] = relationship(
        "TournamentEntry",
        back_populates="tournament",
        lazy="select",
    )
    created_by: Mapped["StaffUser"] = relationship(  # type: ignore[name-defined]
        "StaffUser",
        foreign_keys=[created_by_id],
        lazy="select",
    )

    # ── Helpers ───────────────────────────────────────────────────────────────

    @property
    def slots_remaining(self) -> int:
        return max(0, self.max_players - self.player_count)

    @property
    def is_full(self) -> bool:
        return self.player_count >= self.max_players

    @property
    def is_cancellable(self) -> bool:
        return self.status in CANCELLABLE_STATUSES

    @property
    def room_is_released(self) -> bool:
        return self.room_id is not None and self.room_released_at is not None

    def __repr__(self) -> str:
        return (
            f"<Tournament {self.tournament_code} "
            f"status={self.status.value} "
            f"fee={self.entry_fee} "
            f"players={self.player_count}/{self.max_players}>"
        )


# ── TournamentEntry model ─────────────────────────────────────────────────────

class TournamentEntry(Base):
    """
    One row per player per tournament.

    Payment breakdown is snapshotted at join time so historical accounting
    remains correct even if tournament configuration later changes.

    Sheet numbers are globally unique within a tournament (1–max_players)
    and assigned in strict join order via the Tournament.next_sheet_number
    counter under a FOR UPDATE lock.
    """
    __tablename__ = "tournament_entries"
    __table_args__ = (
        UniqueConstraint(
            "tournament_id", "user_id",
            name="uq_tournament_entry_user",
        ),
        UniqueConstraint(
            "tournament_id", "sheet_number",
            name="uq_tournament_entry_sheet",
        ),
        CheckConstraint("sheet_number >= 1", name="ck_entry_sheet_number_positive"),
        CheckConstraint(
            "entry_fee_snapshot > 0",
            name="ck_entry_fee_snapshot_positive",
        ),
        CheckConstraint(
            "deposit_amount_used >= 0",
            name="ck_entry_deposit_non_negative",
        ),
        CheckConstraint(
            "referral_amount_used >= 0",
            name="ck_entry_referral_non_negative",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)

    tournament_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tournaments.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    # Sheet number assigned at join time — immutable, 1-indexed
    sheet_number: Mapped[int] = mapped_column(Integer, nullable=False)

    # ── Payment snapshot (immutable after join) ───────────────────────────────
    # Exact entry fee at time of join (snapshot, not FK to tournament)
    entry_fee_snapshot: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False
    )
    # How much came from deposit_balance
    deposit_amount_used: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False, default=Decimal("0.00"), server_default=text("0")
    )
    # How much came from referral_balance (max 50% of entry fee)
    referral_amount_used: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False, default=Decimal("0.00"), server_default=text("0")
    )

    # ── Status ────────────────────────────────────────────────────────────────
    status: Mapped[EntryStatus] = mapped_column(
        Enum(EntryStatus, name="entry_status_enum", native_enum=False),
        nullable=False,
        default=EntryStatus.active,
        server_default=text("'active'"),
        index=True,
    )

    # ── Refund tracking ───────────────────────────────────────────────────────
    refunded_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # IDs of the wallet transactions that performed the refund (for audit trail)
    refund_deposit_tx_id: Mapped[Optional[uuid.UUID]] = mapped_column(nullable=True)
    refund_referral_tx_id: Mapped[Optional[uuid.UUID]] = mapped_column(nullable=True)

    # ── Timestamps ────────────────────────────────────────────────────────────
    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        server_default=text("now()"),
        index=True,
    )

    # ── Relationships ─────────────────────────────────────────────────────────
    tournament: Mapped[Tournament] = relationship(
        "Tournament", back_populates="entries"
    )
    user: Mapped["User"] = relationship(  # type: ignore[name-defined]
        "User", lazy="select"
    )

    def __repr__(self) -> str:
        return (
            f"<TournamentEntry "
            f"tournament={self.tournament_id} "
            f"user={self.user_id} "
            f"sheet={self.sheet_number} "
            f"status={self.status.value}>"
        )
