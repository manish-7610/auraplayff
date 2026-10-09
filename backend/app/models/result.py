"""
TournamentResult model — per-player result for a completed tournament.

One row per TournamentEntry (UNIQUE constraint enforced).
The backend derives user_id and ff_uid from the TournamentEntry — never
from client input.

Result lifecycle:
  pending    — entry created but result not yet entered
  entered    — result data (placement + kills) recorded by staff
  verified   — result confirmed by authorised verifier
  finalized  — payout has been distributed; record is immutable

Payout tracking:
  prize_amount       — placement prize awarded (₹0 if outside top-3)
  kill_reward_amount — progressive kill reward (₹0 if outside top-10)
  total_payout       — prize_amount + kill_reward_amount
  payout_distributed — True once wallet_service has been called
  payout_tx_prize_id — WalletTransaction.id for placement prize credit
  payout_tx_kill_id  — WalletTransaction.id for kill reward credit

Immutability:
  Once payout_distributed=True, result_service rejects further edits.
  Any exceptional correction requires a controlled admin mechanism that
  never modifies the financial ledger retroactively.
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
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models._types import utcnow


class ResultStatus(str, pyenum.Enum):
    pending   = "pending"
    entered   = "entered"
    verified  = "verified"
    finalized = "finalized"   # payout done — immutable


class TournamentResult(Base):
    __tablename__ = "tournament_results"
    __table_args__ = (
        # One result per entry
        UniqueConstraint("entry_id", name="uq_result_entry"),
        # Placement must be unique within a tournament
        UniqueConstraint(
            "tournament_id", "placement",
            name="uq_result_tournament_placement",
        ),
        CheckConstraint("placement >= 1", name="ck_result_placement_positive"),
        CheckConstraint("kills >= 0", name="ck_result_kills_non_negative"),
        CheckConstraint(
            "prize_amount >= 0", name="ck_result_prize_non_negative"
        ),
        CheckConstraint(
            "kill_reward_amount >= 0", name="ck_result_kill_reward_non_negative"
        ),
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
    # Denormalised for fast queries — derived from TournamentEntry at creation
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    # FF UID snapshot at time of result entry (player's UID at that moment)
    ff_uid_snapshot: Mapped[str] = mapped_column(String(20), nullable=False)
    # FF display-name snapshot at time of result entry — preserves the name
    # the player used during this tournament even if they later change it.
    # NULL for results entered before migration 0012; fall back to live ff_name.
    ff_name_snapshot: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)

    # ── Result data ────────────────────────────────────────────────────────────
    placement: Mapped[int] = mapped_column(Integer, nullable=False)
    kills: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )

    # ── Payout snapshot (calculated at finalization) ───────────────────────────
    prize_amount: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, default=Decimal("0.00"), server_default=text("0")
    )
    kill_reward_amount: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, default=Decimal("0.00"), server_default=text("0")
    )
    # Whether this result is in the top-N eligible for kill rewards
    kill_reward_eligible: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    # kill reward breakdown string, e.g. "3×₹5 + 3×₹7 + 2×₹10"
    kill_reward_breakdown: Mapped[Optional[str]] = mapped_column(
        String(200), nullable=True
    )

    # ── Payout tracking ────────────────────────────────────────────────────────
    payout_distributed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    payout_distributed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Wallet transaction IDs for audit trail (soft references — no FK to avoid
    # circular constraints; wallet_transactions already have their own index)
    payout_tx_prize_id: Mapped[Optional[uuid.UUID]] = mapped_column(nullable=True)
    payout_tx_kill_id: Mapped[Optional[uuid.UUID]] = mapped_column(nullable=True)

    # ── Workflow tracking ──────────────────────────────────────────────────────
    result_status: Mapped[ResultStatus] = mapped_column(
        Enum(ResultStatus, name="result_status_enum", native_enum=False),
        nullable=False,
        default=ResultStatus.pending,
        server_default=text("'pending'"),
        index=True,
    )

    entered_by_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("staff_users.id", ondelete="SET NULL"), nullable=True
    )
    entered_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    verified_by_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("staff_users.id", ondelete="SET NULL"), nullable=True
    )
    verified_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        server_default=text("now()"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        server_default=text("now()"),
    )

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

    @property
    def total_payout(self) -> Decimal:
        return self.prize_amount + self.kill_reward_amount

    @property
    def is_immutable(self) -> bool:
        """Once payout is distributed, the result cannot be modified."""
        return self.payout_distributed

    def __repr__(self) -> str:
        return (
            f"<TournamentResult "
            f"tournament={self.tournament_id} "
            f"placement={self.placement} "
            f"kills={self.kills} "
            f"status={self.result_status.value}>"
        )
