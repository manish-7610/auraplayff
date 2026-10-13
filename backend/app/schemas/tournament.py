"""
Pydantic v2 schemas for tournament endpoints.

Player-facing schemas NEVER expose:
  - room_id / room_password before they are released AND the player is enrolled
  - next_sheet_number (internal counter)
  - cancellation_reason detail in list views (only in detail views when cancelled)
  - other players' wallet/payment breakdown
  - staff internal IDs beyond creator display name

Admin schemas expose the full picture needed for operations and auditing.

Money amounts serialised as strings to avoid JavaScript float precision issues.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Optional, Self

from pydantic import BaseModel, Field, field_validator, model_validator

from app.services.tournament_service import SUPPORTED_ENTRY_FEES


# ── Shared helpers ─────────────────────────────────────────────────────────────

def _d(v: Decimal) -> str:
    return str(v)


# ── Player-facing: tournament list item ───────────────────────────────────────

class TournamentListItem(BaseModel):
    id: uuid.UUID
    tournament_code: str
    title: str
    game: str
    mode: str
    entry_fee: str
    max_players: int
    min_players: int
    player_count: int
    slots_remaining: int
    status: str
    scheduled_at: datetime
    created_by_display_name: str
    # Whether the requesting player has already joined (set by router)
    already_joined: bool = False

    @classmethod
    def from_tournament(
        cls,
        t: "Tournament",  # type: ignore[name-defined]  # noqa: F821
        *,
        already_joined: bool = False,
    ) -> "TournamentListItem":
        return cls(
            id=t.id,
            tournament_code=t.tournament_code,
            title=t.title,
            game=t.game.value,
            mode=t.mode.value,
            entry_fee=_d(t.entry_fee),
            max_players=t.max_players,
            min_players=t.min_players,
            player_count=t.player_count,
            slots_remaining=t.slots_remaining,
            status=t.status.value,
            scheduled_at=t.scheduled_at,
            created_by_display_name=t.created_by_display_name,
            already_joined=already_joined,
        )


class TournamentListResponse(BaseModel):
    items: list[TournamentListItem]
    total: int
    limit: int
    offset: int


# ── Player-facing: kill reward tier (for display only) ───────────────────────

class KillRewardTierDisplay(BaseModel):
    """A single progressive kill reward bracket shown to players."""
    from_kill: int
    to_kill: Optional[int]          # None means "and above"
    reward_per_kill: str            # decimal string, e.g. "5.00"


# ── Player-facing: tournament detail ─────────────────────────────────────────

class TournamentDetailResponse(BaseModel):
    id: uuid.UUID
    tournament_code: str
    title: str
    game: str
    mode: str
    entry_fee: str
    max_players: int
    min_players: int
    player_count: int
    slots_remaining: int
    status: str
    scheduled_at: datetime
    created_by_display_name: str
    # Only present when tournament is cancelled and player fetches detail
    cancellation_reason: Optional[str] = None
    # Lifecycle timestamps (public — players can see when it went live etc.)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    cancelled_at: Optional[datetime] = None
    already_joined: bool = False
    # Prize info — None when no active config exists for this entry fee
    first_place_prize: Optional[str] = None
    second_place_prize: Optional[str] = None
    third_place_prize: Optional[str] = None
    kill_reward_top_n: Optional[int] = None
    kill_reward_tiers: Optional[list[KillRewardTierDisplay]] = None

    @classmethod
    def from_tournament(
        cls,
        t: "Tournament",  # type: ignore[name-defined]  # noqa: F821
        *,
        already_joined: bool = False,
        prize_config: Optional["PrizeConfig"] = None,  # type: ignore[name-defined]  # noqa: F821
    ) -> "TournamentDetailResponse":
        # Build kill reward tiers for display if prize config is available
        kill_tiers = None
        if prize_config is not None and prize_config.kill_reward_tiers:
            kill_tiers = [
                KillRewardTierDisplay(
                    from_kill=tier["from_kill"],
                    to_kill=tier.get("to_kill"),
                    reward_per_kill=str(tier["reward_per_kill"]),
                )
                for tier in prize_config.kill_reward_tiers
            ]

        return cls(
            id=t.id,
            tournament_code=t.tournament_code,
            title=t.title,
            game=t.game.value,
            mode=t.mode.value,
            entry_fee=_d(t.entry_fee),
            max_players=t.max_players,
            min_players=t.min_players,
            player_count=t.player_count,
            slots_remaining=t.slots_remaining,
            status=t.status.value,
            scheduled_at=t.scheduled_at,
            created_by_display_name=t.created_by_display_name,
            cancellation_reason=(
                t.cancellation_reason if t.status.value == "cancelled" else None
            ),
            started_at=t.started_at,
            completed_at=t.completed_at,
            cancelled_at=t.cancelled_at,
            already_joined=already_joined,
            first_place_prize=_d(prize_config.first_place_prize) if prize_config else None,
            second_place_prize=_d(prize_config.second_place_prize) if prize_config else None,
            third_place_prize=_d(prize_config.third_place_prize) if prize_config else None,
            kill_reward_top_n=prize_config.kill_reward_top_n if prize_config else None,
            kill_reward_tiers=kill_tiers,
        )


# ── Player-facing: join response ──────────────────────────────────────────────

class JoinTournamentResponse(BaseModel):
    message: str = "Successfully joined the tournament."
    entry_id: uuid.UUID
    tournament_id: uuid.UUID
    tournament_code: str
    sheet_number: int
    entry_fee: str
    deposit_used: str
    referral_used: str
    total_paid: str


# ── Player-facing: my entry ───────────────────────────────────────────────────

class MyEntryResponse(BaseModel):
    entry_id: uuid.UUID
    tournament_id: uuid.UUID
    tournament_code: str
    tournament_title: str
    sheet_number: int
    entry_fee: str
    deposit_used: str
    referral_used: str
    total_paid: str
    status: str
    joined_at: datetime
    refunded_at: Optional[datetime] = None

    @classmethod
    def from_entry(
        cls,
        entry: "TournamentEntry",  # type: ignore[name-defined]  # noqa: F821
        *,
        tournament: "Tournament",  # type: ignore[name-defined]  # noqa: F821
    ) -> "MyEntryResponse":
        return cls(
            entry_id=entry.id,
            tournament_id=entry.tournament_id,
            tournament_code=tournament.tournament_code,
            tournament_title=tournament.title,
            sheet_number=entry.sheet_number,
            entry_fee=_d(entry.entry_fee_snapshot),
            deposit_used=_d(entry.deposit_amount_used),
            referral_used=_d(entry.referral_amount_used),
            total_paid=_d(entry.deposit_amount_used + entry.referral_amount_used),
            status=entry.status.value,
            joined_at=entry.joined_at,
            refunded_at=entry.refunded_at,
        )


# ── Player-facing: room credentials ──────────────────────────────────────────

class RoomCredentialsResponse(BaseModel):
    tournament_id: uuid.UUID
    tournament_code: str
    tournament_title: str
    sheet_number: int
    room_id: str
    room_password: str
    room_released_at: datetime
    scheduled_at: datetime


# ── Admin: create tournament request ─────────────────────────────────────────

class CreateTournamentRequest(BaseModel):
    title: str = Field(..., min_length=3, max_length=120)
    entry_fee: Decimal = Field(..., gt=Decimal("0"))
    scheduled_at: datetime = Field(..., description="Must be a future UTC datetime")
    game: str = Field(default="free_fire", pattern=r"^(free_fire)$")
    mode: str = Field(default="solo", pattern=r"^(solo)$")
    max_players: int = Field(default=50, ge=2, le=50)
    min_players: int = Field(default=40, ge=2, le=50)

    @field_validator("entry_fee")
    @classmethod
    def validate_entry_fee(cls, v: Decimal) -> Decimal:
        v = Decimal(str(v)).quantize(Decimal("0.01"))
        if v not in SUPPORTED_ENTRY_FEES:
            supported = ", ".join(f"₹{f}" for f in sorted(SUPPORTED_ENTRY_FEES))
            raise ValueError(
                f"Entry fee ₹{v} is not supported. Supported fees: {supported}"
            )
        return v

    @model_validator(mode="after")
    def validate_player_limits(self) -> Self:
        if self.min_players > self.max_players:
            raise ValueError("min_players cannot exceed max_players.")
        return self


# ── Admin: cancel request ─────────────────────────────────────────────────────

class CancelTournamentRequest(BaseModel):
    reason: str = Field(..., min_length=5, max_length=500)


# ── Admin: release room request ───────────────────────────────────────────────

class ReleaseRoomRequest(BaseModel):
    room_id: str = Field(..., min_length=1, max_length=50)
    room_password: str = Field(..., min_length=1, max_length=50)


# ── Admin: status transition request ─────────────────────────────────────────

class TransitionStatusRequest(BaseModel):
    target_status: str = Field(
        ...,
        description=(
            "Target status. Must be a valid next step in the lifecycle. "
            "Use the cancel endpoint for cancellations."
        ),
    )
    reason: Optional[str] = Field(None, max_length=500)


# ── Admin: tournament summary ─────────────────────────────────────────────────

class AdminTournamentSummary(BaseModel):
    id: uuid.UUID
    tournament_code: str
    title: str
    game: str
    mode: str
    entry_fee: str
    max_players: int
    min_players: int
    player_count: int
    slots_remaining: int
    status: str
    scheduled_at: datetime
    created_by_id: uuid.UUID
    created_by_display_name: str
    created_by_role: str
    created_at: datetime
    updated_at: datetime
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    cancelled_at: Optional[datetime] = None
    cancellation_reason: Optional[str] = None

    @classmethod
    def from_tournament(
        cls, t: "Tournament"  # type: ignore[name-defined]  # noqa: F821
    ) -> "AdminTournamentSummary":
        return cls(
            id=t.id,
            tournament_code=t.tournament_code,
            title=t.title,
            game=t.game.value,
            mode=t.mode.value,
            entry_fee=_d(t.entry_fee),
            max_players=t.max_players,
            min_players=t.min_players,
            player_count=t.player_count,
            slots_remaining=t.slots_remaining,
            status=t.status.value,
            scheduled_at=t.scheduled_at,
            created_by_id=t.created_by_id,
            created_by_display_name=t.created_by_display_name,
            created_by_role=t.created_by_role,
            created_at=t.created_at,
            updated_at=t.updated_at,
            started_at=t.started_at,
            completed_at=t.completed_at,
            cancelled_at=t.cancelled_at,
            cancellation_reason=t.cancellation_reason,
        )


# ── Admin: tournament detail (adds room credentials) ─────────────────────────

class AdminTournamentDetail(AdminTournamentSummary):
    room_id: Optional[str] = None
    room_password: Optional[str] = None
    room_released_at: Optional[datetime] = None
    room_released_by_id: Optional[uuid.UUID] = None
    completed_by_id: Optional[uuid.UUID] = None
    cancelled_by_id: Optional[uuid.UUID] = None
    next_sheet_number: int = 1

    @classmethod
    def from_tournament(  # type: ignore[override]
        cls, t: "Tournament"  # type: ignore[name-defined]  # noqa: F821
    ) -> "AdminTournamentDetail":
        base = AdminTournamentSummary.from_tournament(t)
        return cls(
            **base.model_dump(),
            room_id=t.room_id,
            room_password=t.room_password,
            room_released_at=t.room_released_at,
            room_released_by_id=t.room_released_by_id,
            completed_by_id=t.completed_by_id,
            cancelled_by_id=t.cancelled_by_id,
            next_sheet_number=t.next_sheet_number,
        )


class AdminTournamentListResponse(BaseModel):
    items: list[AdminTournamentSummary]
    total: int
    limit: int
    offset: int


# ── Admin: enrolled player entry ─────────────────────────────────────────────

class AdminEntryResponse(BaseModel):
    entry_id: uuid.UUID
    user_id: uuid.UUID
    username: str
    ff_uid: str
    ff_name: Optional[str] = None   # current Free Fire display name (live lookup)
    sheet_number: int
    entry_fee: str
    deposit_used: str
    referral_used: str
    total_paid: str
    status: str
    joined_at: datetime
    refunded_at: Optional[datetime] = None


class AdminEntryListResponse(BaseModel):
    items: list[AdminEntryResponse]
    total: int
    limit: int
    offset: int


# ── Generic action response ───────────────────────────────────────────────────

class TournamentActionResponse(BaseModel):
    message: str
    tournament_id: uuid.UUID
    tournament_code: str
    status: str
