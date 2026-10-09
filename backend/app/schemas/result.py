"""
Pydantic v2 schemas for result and prize endpoints.

Player-facing schemas:
  - Never expose: entered_by_id, verified_by_id, payout_tx_ids, ff_uid_snapshot
    of other players, internal result IDs beyond what's needed.
  - Show: placement, kills, prize_amount, kill_reward, breakdown (once finalized)

Admin schemas:
  - Full picture: entered_by, verified_by, payout tx refs, immutability flag.

Kill reward preview (used by both player preview and admin entry UI):
  - Pure calculation — no DB write, no auth required for the preview
    endpoint beyond being staff.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Optional

from pydantic import BaseModel, Field


# ── Kill reward preview ────────────────────────────────────────────────────────

class KillRewardPreviewRequest(BaseModel):
    """Request body for the kill-reward preview endpoint."""
    placement: int = Field(..., ge=1, description="Final placement (1-indexed)")
    kills: int = Field(..., ge=0, description="Total kill count")
    entry_fee: Decimal = Field(..., gt=Decimal("0"), description="Tournament entry fee")


class KillRewardPreviewResponse(BaseModel):
    """
    Preview of kill reward for given placement + kills.
    Identical structure used by both preview endpoint and result detail.
    """
    eligible: bool
    placement: int
    kills: int
    reward: str          # Decimal as string
    breakdown: str       # e.g. "3×₹5 + 3×₹7 + 2×₹10"


# ── Admin: enter / update result ───────────────────────────────────────────────

class EnterResultRequest(BaseModel):
    entry_id: uuid.UUID = Field(..., description="TournamentEntry UUID")
    placement: int = Field(..., ge=1, description="Final placement (1-indexed)")
    kills: int = Field(..., ge=0, description="Kill count")


class UpdateResultRequest(BaseModel):
    placement: Optional[int] = Field(None, ge=1)
    kills: Optional[int] = Field(None, ge=0)


# ── Player-facing result item ──────────────────────────────────────────────────

class PlayerResultResponse(BaseModel):
    """Single result visible to the public leaderboard / player's own history."""
    result_id: uuid.UUID
    tournament_id: uuid.UUID
    tournament_code: str
    placement: int
    kills: int
    result_status: str
    # Payout fields — only populated once finalized
    prize_amount: Optional[str] = None
    kill_reward_amount: Optional[str] = None
    total_payout: Optional[str] = None
    kill_reward_breakdown: Optional[str] = None
    kill_reward_eligible: Optional[bool] = None

    @classmethod
    def from_result(
        cls,
        r: "TournamentResult",  # type: ignore[name-defined]  # noqa: F821
        *,
        tournament_code: str,
    ) -> "PlayerResultResponse":
        finalized = r.payout_distributed
        return cls(
            result_id=r.id,
            tournament_id=r.tournament_id,
            tournament_code=tournament_code,
            placement=r.placement,
            kills=r.kills,
            result_status=r.result_status.value,
            prize_amount=str(r.prize_amount) if finalized else None,
            kill_reward_amount=str(r.kill_reward_amount) if finalized else None,
            total_payout=str(r.total_payout) if finalized else None,
            kill_reward_breakdown=r.kill_reward_breakdown if finalized else None,
            kill_reward_eligible=r.kill_reward_eligible if finalized else None,
        )


class TournamentResultsListResponse(BaseModel):
    """Full result board for a tournament (public — no payout data until finalized)."""
    tournament_id: uuid.UUID
    tournament_code: str
    results: list[PlayerResultResponse]


# ── Admin: full result detail ─────────────────────────────────────────────────

class AdminResultResponse(BaseModel):
    result_id: uuid.UUID
    tournament_id: uuid.UUID
    entry_id: uuid.UUID
    user_id: uuid.UUID
    ff_uid_snapshot: str
    ff_name_snapshot: Optional[str] = None   # Free Fire name at result-entry time
    placement: int
    kills: int
    result_status: str
    # Payout
    prize_amount: str
    kill_reward_amount: str
    total_payout: str
    kill_reward_eligible: bool
    kill_reward_breakdown: Optional[str] = None
    payout_distributed: bool
    payout_distributed_at: Optional[datetime] = None
    payout_tx_prize_id: Optional[uuid.UUID] = None
    payout_tx_kill_id: Optional[uuid.UUID] = None
    # Workflow
    entered_by_id: Optional[uuid.UUID] = None
    entered_at: Optional[datetime] = None
    verified_by_id: Optional[uuid.UUID] = None
    verified_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime
    is_immutable: bool

    @classmethod
    def from_result(
        cls, r: "TournamentResult"  # type: ignore[name-defined]  # noqa: F821
    ) -> "AdminResultResponse":
        return cls(
            result_id=r.id,
            tournament_id=r.tournament_id,
            entry_id=r.entry_id,
            user_id=r.user_id,
            ff_uid_snapshot=r.ff_uid_snapshot,
            ff_name_snapshot=r.ff_name_snapshot,
            placement=r.placement,
            kills=r.kills,
            result_status=r.result_status.value,
            prize_amount=str(r.prize_amount),
            kill_reward_amount=str(r.kill_reward_amount),
            total_payout=str(r.total_payout),
            kill_reward_eligible=r.kill_reward_eligible,
            kill_reward_breakdown=r.kill_reward_breakdown,
            payout_distributed=r.payout_distributed,
            payout_distributed_at=r.payout_distributed_at,
            payout_tx_prize_id=r.payout_tx_prize_id,
            payout_tx_kill_id=r.payout_tx_kill_id,
            entered_by_id=r.entered_by_id,
            entered_at=r.entered_at,
            verified_by_id=r.verified_by_id,
            verified_at=r.verified_at,
            created_at=r.created_at,
            updated_at=r.updated_at,
            is_immutable=r.is_immutable,
        )


class AdminResultListResponse(BaseModel):
    tournament_id: uuid.UUID
    results: list[AdminResultResponse]
    total: int


# ── Prize config schemas ──────────────────────────────────────────────────────

class PrizeConfigResponse(BaseModel):
    id: uuid.UUID
    entry_fee: str
    is_active: bool
    first_place_prize: str
    second_place_prize: str
    third_place_prize: str
    kill_reward_top_n: int
    kill_reward_tiers: list[dict[str, Any]]
    notes: Optional[str] = None

    @classmethod
    def from_config(
        cls, c: "PrizeConfig"  # type: ignore[name-defined]  # noqa: F821
    ) -> "PrizeConfigResponse":
        return cls(
            id=c.id,
            entry_fee=str(c.entry_fee),
            is_active=c.is_active,
            first_place_prize=str(c.first_place_prize),
            second_place_prize=str(c.second_place_prize),
            third_place_prize=str(c.third_place_prize),
            kill_reward_top_n=int(c.kill_reward_top_n),
            kill_reward_tiers=c.kill_reward_tiers or [],
            notes=c.notes,
        )


class AdminPayoutSummaryResponse(BaseModel):
    """Summary returned after payout distribution is triggered."""
    message: str
    tournament_id: uuid.UUID
    results_paid: int
    total_prize_distributed: str
    total_kill_reward_distributed: str
    total_distributed: str


# ── Prize config update request (Phase 10 — T5) ───────────────────────────────

class UpdatePrizeConfigRequest(BaseModel):
    """Super Admin updates a prize tier configuration."""
    is_active: Optional[bool] = None
    first_place_prize: Optional[Decimal] = Field(
        None, ge=Decimal("0"),
        description="First place prize amount (INR). 0 = no placement prize.",
    )
    second_place_prize: Optional[Decimal] = Field(None, ge=Decimal("0"))
    third_place_prize: Optional[Decimal] = Field(None, ge=Decimal("0"))
    kill_reward_top_n: Optional[int] = Field(
        None, ge=0, le=50,
        description="How many top placements are eligible for kill rewards. 0 = none.",
    )
    kill_reward_tiers: Optional[list[dict[str, Any]]] = Field(
        None,
        description=(
            "Kill reward tier list. Each tier: {from_kill, to_kill (null=unlimited), reward_per_kill}. "
            "Empty list = no kill rewards."
        ),
    )
    notes: Optional[str] = Field(None, max_length=500)


# ── Completed Results History schemas (Phase 11) ──────────────────────────────

class CompletedHistoryResultItem(BaseModel):
    """
    One finalized result row for the Completed History view.
    All monetary values are read from the DB — never recalculated on the frontend.
    Includes a denormalized username so the frontend needs no extra join call.
    """
    result_id: uuid.UUID
    tournament_id: uuid.UUID
    tournament_code: str
    tournament_title: str
    entry_fee: str
    completed_at: Optional[datetime] = None

    # Per-player fields
    user_id: uuid.UUID
    username: str
    ff_uid_snapshot: str
    ff_name_snapshot: Optional[str] = None   # FF name at tournament time (historical)
    ff_name: Optional[str] = None            # current FF name — fallback when snapshot is NULL
    placement: int
    kills: int

    # Finalized payout values — read from DB, never recalculated
    prize_amount: str
    kill_reward_amount: str
    total_payout: str
    kill_reward_eligible: bool
    kill_reward_breakdown: Optional[str] = None

    # Payout status
    payout_distributed: bool
    payout_distributed_at: Optional[datetime] = None
    result_status: str

    @classmethod
    def from_orm_row(
        cls,
        result: "TournamentResult",  # type: ignore[name-defined]  # noqa: F821
        *,
        tournament_code: str,
        tournament_title: str,
        entry_fee: str,
        completed_at: Optional[datetime],
        username: str,
        ff_name: Optional[str] = None,
    ) -> "CompletedHistoryResultItem":
        return cls(
            result_id=result.id,
            tournament_id=result.tournament_id,
            tournament_code=tournament_code,
            tournament_title=tournament_title,
            entry_fee=entry_fee,
            completed_at=completed_at,
            user_id=result.user_id,
            username=username,
            ff_uid_snapshot=result.ff_uid_snapshot,
            ff_name_snapshot=result.ff_name_snapshot,
            ff_name=ff_name,
            placement=result.placement,
            kills=result.kills,
            prize_amount=str(result.prize_amount),
            kill_reward_amount=str(result.kill_reward_amount),
            total_payout=str(result.prize_amount + result.kill_reward_amount),
            kill_reward_eligible=result.kill_reward_eligible,
            kill_reward_breakdown=result.kill_reward_breakdown,
            payout_distributed=result.payout_distributed,
            payout_distributed_at=result.payout_distributed_at,
            result_status=result.result_status.value,
        )


class CompletedTournamentSummary(BaseModel):
    """Lightweight summary of one completed tournament for the history list."""
    tournament_id: uuid.UUID
    tournament_code: str
    tournament_title: str
    entry_fee: str
    player_count: int
    completed_at: Optional[datetime] = None
    result_count: int
    payout_distributed_count: int


class CompletedHistoryListResponse(BaseModel):
    """Paginated list of completed tournaments (summary cards)."""
    items: list[CompletedTournamentSummary]
    total: int
    limit: int
    offset: int


class CompletedTournamentResultsResponse(BaseModel):
    """Full finalized result table for one completed tournament (read-only)."""
    tournament_id: uuid.UUID
    tournament_code: str
    tournament_title: str
    entry_fee: str
    completed_at: Optional[datetime] = None
    player_count: int
    results: list[CompletedHistoryResultItem]
    total: int
