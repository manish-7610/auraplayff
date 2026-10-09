"""
Prize service — kill reward calculation, prize lookup, payout distribution.

This is the single authoritative source for all financial result calculations.
No route handler or result service calculates money independently.

Kill-reward algorithm (progressive tier logic):
  1. Player must be in top-N placements (kill_reward_top_n, default 10).
  2. Progressive tiers are applied in order:
       tier = {from_kill, to_kill (null = unlimited), reward_per_kill}
  3. Decimal arithmetic only — no floats.
  4. Breakdown string is generated alongside the amount for display/audit.

Placement prizes by entry fee:
  ₹10 → 1st ₹200  2nd ₹60   3rd ₹20
  ₹20 → 1st ₹400  2nd ₹120  3rd ₹40
  ₹30 → 1st ₹600  2nd ₹180  3rd ₹60
  ₹40 → 1st ₹800  2nd ₹240  3rd ₹80
  ₹50 → 1st ₹1000 2nd ₹300  3rd ₹100
  ₹100→ 1st ₹2000 2nd ₹600  3rd ₹200

Kill reward brackets (progressive within each tier):
  ₹10/₹20/₹30: kills 1-3 → ₹3/kill, 4-7 → ₹5/kill, 8+ → ₹7/kill
  ₹40:         kills 1-3 → ₹4/kill, 4-7 → ₹5/kill, 8+ → ₹8/kill
  ₹50:         kills 1-3 → ₹5/kill, 4-6 → ₹7/kill, 7+ → ₹10/kill
  ₹100:        kills 1-5 → ₹10/kill, 6+ → ₹15/kill

Idempotency keys for payout wallet transactions:
  Placement prize:  "prize:<result_id>"
  Kill reward:      "kill_reward:<result_id>"

Bootstrap:
  seed_all_prize_configs() is called by bootstrap_service on startup.
  It is idempotent — safe to call on every restart.
"""
from __future__ import annotations

import logging
from decimal import ROUND_DOWN, Decimal
from typing import Any

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    PayoutAlreadyDistributedError,
    PrizeConfigNotFoundError,
)
from app.models._types import utcnow
from app.models.audit_log import ActorType
from app.models.prize_config import PrizeConfig
from app.models.result import ResultStatus, TournamentResult
from app.services import audit_service, wallet_service

logger = logging.getLogger("auraplayff.prize")

_CENTS = Decimal("0.01")

# ── All confirmed prize configurations ───────────────────────────────────────
# These are the canonical business rules for all supported entry fees.
# All tiers are active. Kill calculation is PROGRESSIVE (not flat-rate).
#
# Placement prizes:
#   ₹10 → 1st ₹200,  2nd ₹60,   3rd ₹20
#   ₹20 → 1st ₹400,  2nd ₹120,  3rd ₹40
#   ₹30 → 1st ₹600,  2nd ₹180,  3rd ₹60
#   ₹40 → 1st ₹800,  2nd ₹240,  3rd ₹80
#   ₹50 → 1st ₹1000, 2nd ₹300,  3rd ₹100
#   ₹100→ 1st ₹2000, 2nd ₹600,  3rd ₹200
#
# Kill reward tiers (progressive — each bracket only covers its own kills):
#   ₹10/₹20/₹30: kills 1-3 →₹3, 4-7 →₹5, 8+ →₹7
#   ₹40:         kills 1-3 →₹4, 4-7 →₹5, 8+ →₹8
#   ₹50:         kills 1-3 →₹5, 4-6 →₹7, 7+ →₹10
#   ₹100:        kills 1-5 →₹10, 6+ →₹15

_ALL_PRIZE_CONFIGS: list[dict[str, Any]] = [
    {
        "entry_fee": Decimal("10.00"),
        "is_active": True,
        "first_place_prize": Decimal("200.00"),
        "second_place_prize": Decimal("60.00"),
        "third_place_prize": Decimal("20.00"),
        "kill_reward_top_n": 10,
        "kill_reward_tiers": [
            {"from_kill": 1, "to_kill": 3,    "reward_per_kill": "3.00"},
            {"from_kill": 4, "to_kill": 7,    "reward_per_kill": "5.00"},
            {"from_kill": 8, "to_kill": None, "reward_per_kill": "7.00"},
        ],
        "notes": "₹10 entry fee prize structure.",
    },
    {
        "entry_fee": Decimal("20.00"),
        "is_active": True,
        "first_place_prize": Decimal("400.00"),
        "second_place_prize": Decimal("120.00"),
        "third_place_prize": Decimal("40.00"),
        "kill_reward_top_n": 10,
        "kill_reward_tiers": [
            {"from_kill": 1, "to_kill": 3,    "reward_per_kill": "3.00"},
            {"from_kill": 4, "to_kill": 7,    "reward_per_kill": "5.00"},
            {"from_kill": 8, "to_kill": None, "reward_per_kill": "7.00"},
        ],
        "notes": "₹20 entry fee prize structure.",
    },
    {
        "entry_fee": Decimal("30.00"),
        "is_active": True,
        "first_place_prize": Decimal("600.00"),
        "second_place_prize": Decimal("180.00"),
        "third_place_prize": Decimal("60.00"),
        "kill_reward_top_n": 10,
        "kill_reward_tiers": [
            {"from_kill": 1, "to_kill": 3,    "reward_per_kill": "3.00"},
            {"from_kill": 4, "to_kill": 7,    "reward_per_kill": "5.00"},
            {"from_kill": 8, "to_kill": None, "reward_per_kill": "7.00"},
        ],
        "notes": "₹30 entry fee prize structure.",
    },
    {
        "entry_fee": Decimal("40.00"),
        "is_active": True,
        "first_place_prize": Decimal("800.00"),
        "second_place_prize": Decimal("240.00"),
        "third_place_prize": Decimal("80.00"),
        "kill_reward_top_n": 10,
        "kill_reward_tiers": [
            {"from_kill": 1, "to_kill": 3,    "reward_per_kill": "4.00"},
            {"from_kill": 4, "to_kill": 7,    "reward_per_kill": "5.00"},
            {"from_kill": 8, "to_kill": None, "reward_per_kill": "8.00"},
        ],
        "notes": "₹40 entry fee prize structure.",
    },
    {
        "entry_fee": Decimal("50.00"),
        "is_active": True,
        "first_place_prize": Decimal("1000.00"),
        "second_place_prize": Decimal("300.00"),
        "third_place_prize": Decimal("100.00"),
        "kill_reward_top_n": 10,
        "kill_reward_tiers": [
            {"from_kill": 1, "to_kill": 3,    "reward_per_kill": "5.00"},
            {"from_kill": 4, "to_kill": 6,    "reward_per_kill": "7.00"},
            {"from_kill": 7, "to_kill": None, "reward_per_kill": "10.00"},
        ],
        "notes": "₹50 entry fee prize structure.",
    },
    {
        "entry_fee": Decimal("100.00"),
        "is_active": True,
        "first_place_prize": Decimal("2000.00"),
        "second_place_prize": Decimal("600.00"),
        "third_place_prize": Decimal("200.00"),
        "kill_reward_top_n": 10,
        "kill_reward_tiers": [
            {"from_kill": 1, "to_kill": 5,    "reward_per_kill": "10.00"},
            {"from_kill": 6, "to_kill": None, "reward_per_kill": "15.00"},
        ],
        "notes": "₹100 entry fee prize structure.",
    },
]

# Keep the old name as an alias so any external reference still works
_FIFTY_RUPEE_CONFIG: dict[str, Any] = _ALL_PRIZE_CONFIGS[4]  # ₹50 entry


# ── Bootstrap / seeding ───────────────────────────────────────────────────────

async def seed_all_prize_configs(db: AsyncSession) -> None:
    """
    Idempotent: insert all prize configs that don't exist yet, activate
    inactive rows, and always sync kill_reward_tiers to the current
    canonical values in _ALL_PRIZE_CONFIGS.

    Called by bootstrap_service on application startup.

    Policy:
    - Prize amounts (1st/2nd/3rd place) are NOT overwritten for active rows —
      the admin can update them via the admin prize-config API.
    - kill_reward_tiers ARE always synced because they define the calculation
      algorithm, not a configurable value. Keeping them in sync prevents
      silent drift between the source code and the database.
    - is_active is set to True for all rows on first creation or if currently
      inactive.
    """
    for cfg_data in _ALL_PRIZE_CONFIGS:
        result = await db.execute(
            select(PrizeConfig).where(PrizeConfig.entry_fee == cfg_data["entry_fee"])
        )
        existing = result.scalar_one_or_none()

        if existing is None:
            # New row — create with all canonical values
            new_cfg = PrizeConfig(
                entry_fee=cfg_data["entry_fee"],
                is_active=cfg_data["is_active"],
                first_place_prize=cfg_data["first_place_prize"],
                second_place_prize=cfg_data["second_place_prize"],
                third_place_prize=cfg_data["third_place_prize"],
                kill_reward_top_n=cfg_data["kill_reward_top_n"],
                kill_reward_tiers=cfg_data["kill_reward_tiers"],
                notes=cfg_data["notes"],
            )
            db.add(new_cfg)
            await db.flush([new_cfg])
            logger.info("Seeded Rs%s prize configuration.", cfg_data["entry_fee"])

        elif not existing.is_active:
            # Row exists but is inactive — activate it with full canonical values
            existing.is_active = True
            existing.first_place_prize = cfg_data["first_place_prize"]
            existing.second_place_prize = cfg_data["second_place_prize"]
            existing.third_place_prize = cfg_data["third_place_prize"]
            existing.kill_reward_top_n = cfg_data["kill_reward_top_n"]
            existing.kill_reward_tiers = cfg_data["kill_reward_tiers"]
            existing.notes = cfg_data["notes"]
            await db.flush([existing])
            logger.info(
                "Activated and updated Rs%s prize configuration.", cfg_data["entry_fee"]
            )

        else:
            # Row is already active — only sync kill_reward_tiers (algorithm)
            # and kill_reward_top_n. Prize amounts are left for admin to manage.
            if existing.kill_reward_tiers != cfg_data["kill_reward_tiers"]:
                existing.kill_reward_tiers = cfg_data["kill_reward_tiers"]
                existing.kill_reward_top_n = cfg_data["kill_reward_top_n"]
                await db.flush([existing])
                logger.info(
                    "Synced kill_reward_tiers for Rs%s prize configuration.",
                    cfg_data["entry_fee"],
                )
            else:
                logger.debug(
                    "Rs%s prize configuration already up-to-date — skipped.",
                    cfg_data["entry_fee"],
                )


# Keep old name for any code still referencing it
async def seed_fifty_rupee_config(db: AsyncSession) -> None:
    """Legacy alias — calls seed_all_prize_configs."""
    await seed_all_prize_configs(db)


# ── Prize config lookup ───────────────────────────────────────────────────────

async def get_prize_config(
    db: AsyncSession,
    entry_fee: Decimal,
) -> PrizeConfig:
    """
    Load the active prize config for the given entry fee.
    Raises PrizeConfigNotFoundError if not found or not active.
    """
    result = await db.execute(
        select(PrizeConfig).where(
            PrizeConfig.entry_fee == entry_fee,
            PrizeConfig.is_active == True,  # noqa: E712
        )
    )
    config = result.scalar_one_or_none()
    if config is None:
        raise PrizeConfigNotFoundError(str(entry_fee))
    return config


# ── Kill reward calculation ───────────────────────────────────────────────────

class KillRewardResult:
    """Return value of calculate_kill_reward()."""
    __slots__ = ("eligible", "kills", "reward", "breakdown")

    def __init__(
        self,
        eligible: bool,
        kills: int,
        reward: Decimal,
        breakdown: str,
    ) -> None:
        self.eligible = eligible
        self.kills = kills
        self.reward = reward
        self.breakdown = breakdown

    def to_dict(self) -> dict[str, Any]:
        return {
            "eligible": self.eligible,
            "kills": self.kills,
            "reward": str(self.reward),
            "breakdown": self.breakdown,
        }


def calculate_kill_reward(
    placement: int,
    kills: int,
    config: PrizeConfig,
) -> KillRewardResult:
    """
    Calculate the kill reward for a player given their placement and kill count.

    Args:
        placement:  Final placement (1-indexed).
        kills:      Total kill count (non-negative).
        config:     Active PrizeConfig for this tournament's entry fee.

    Returns:
        KillRewardResult with eligible flag, reward amount (Decimal), and
        human-readable breakdown string.

    Algorithm:
        1. If placement > kill_reward_top_n → not eligible, reward = 0.
        2. Iterate tiers in order; for each tier:
           count = min(kills_remaining, tier_capacity)
           tier_reward = count × reward_per_kill
        3. Sum all tier rewards.
        4. Build breakdown string: "3×₹5 + 3×₹7 + 2×₹10"

    This function is PURE (no DB calls) so it can be used in both the
    preview endpoint and the actual payout path identically.
    """
    if placement > config.kill_reward_top_n:
        return KillRewardResult(
            eligible=False,
            kills=kills,
            reward=Decimal("0.00"),
            breakdown="Placement outside Top 10",
        )

    if kills == 0:
        return KillRewardResult(
            eligible=True,
            kills=0,
            reward=Decimal("0.00"),
            breakdown="0 kills",
        )

    tiers: list[dict[str, Any]] = config.kill_reward_tiers or []
    if not tiers:
        return KillRewardResult(
            eligible=True,
            kills=kills,
            reward=Decimal("0.00"),
            breakdown="No kill reward tiers configured",
        )

    total_reward = Decimal("0.00")
    parts: list[str] = []
    kills_remaining = kills

    for tier in tiers:
        if kills_remaining <= 0:
            break

        from_kill: int = tier["from_kill"]
        to_kill: int | None = tier.get("to_kill")
        reward_per_kill = Decimal(str(tier["reward_per_kill"])).quantize(
            _CENTS, rounding=ROUND_DOWN
        )

        if to_kill is not None:
            tier_capacity = to_kill - from_kill + 1
        else:
            tier_capacity = kills_remaining  # unlimited

        count = min(kills_remaining, tier_capacity)
        if count <= 0:
            continue

        tier_total = (reward_per_kill * count).quantize(_CENTS, rounding=ROUND_DOWN)
        total_reward += tier_total
        parts.append(f"{count}×₹{reward_per_kill}")
        kills_remaining -= count

    breakdown = " + ".join(parts) if parts else "0 kills"
    total_reward = total_reward.quantize(_CENTS, rounding=ROUND_DOWN)

    return KillRewardResult(
        eligible=True,
        kills=kills,
        reward=total_reward,
        breakdown=breakdown,
    )


def get_placement_prize(placement: int, config: PrizeConfig) -> Decimal:
    """Return the placement prize for a given position. 0 for 4th and below."""
    if placement == 1:
        return config.first_place_prize
    if placement == 2:
        return config.second_place_prize
    if placement == 3:
        return config.third_place_prize
    return Decimal("0.00")


# ── Payout distribution ───────────────────────────────────────────────────────

async def distribute_payout(
    db: AsyncSession,
    result: TournamentResult,
    config: PrizeConfig,
    *,
    actor_id: str = "system",
) -> TournamentResult:
    """
    Distribute prize and kill reward to a single player's result.

    Steps:
      1. Check not already distributed (idempotency guard at service level).
      2. Calculate placement prize and kill reward (server-side).
      3. Credit winning_balance via wallet_service with deterministic keys.
      4. Stamp payout amounts and tx IDs onto the result row.
      5. Mark payout_distributed = True and result_status = finalized.
      6. Flush (caller commits).

    Idempotency:
      - "prize:<result.id>"        — placement prize tx
      - "kill_reward:<result.id>"  — kill reward tx
      The UNIQUE constraint on WalletTransaction.idempotency_key is the
      DB-level guard; DuplicateTransactionError is treated as success here
      (already paid is a valid idempotent outcome).

    This function does NOT commit — the caller (distribute_all_payouts or
    a router) commits after all results are processed.
    """
    if result.payout_distributed:
        raise PayoutAlreadyDistributedError()

    # ── Calculate amounts server-side ──────────────────────────────────────────
    prize_amount = get_placement_prize(result.placement, config)
    kill_result = calculate_kill_reward(result.placement, result.kills, config)
    kill_amount = kill_result.reward

    now = utcnow()
    meta: dict[str, Any] = {
        "tournament_id": str(result.tournament_id),
        "result_id": str(result.id),
        "placement": result.placement,
        "kills": result.kills,
    }

    prize_tx_id: uuid.UUID | None = None
    kill_tx_id: uuid.UUID | None = None

    # ── Credit placement prize ─────────────────────────────────────────────────
    if prize_amount > Decimal("0"):
        from app.core.exceptions import DuplicateTransactionError
        try:
            prize_tx = await wallet_service.credit_prize(
                db,
                user_id=result.user_id,
                amount=prize_amount,
                idempotency_key=f"prize:{result.id}",
                reference_id=result.id,
                reference_type="tournament_result",
                description=(
                    f"Tournament prize: placement #{result.placement} "
                    f"(₹{prize_amount})"
                ),
                meta={**meta, "prize_type": "placement"},
            )
            prize_tx_id = prize_tx.id
        except DuplicateTransactionError:
            logger.warning(
                "Prize already credited for result %s — treating as success", result.id
            )

    # ── Credit kill reward ─────────────────────────────────────────────────────
    if kill_amount > Decimal("0"):
        from app.core.exceptions import DuplicateTransactionError
        try:
            kill_tx = await wallet_service.credit_kill_reward(
                db,
                user_id=result.user_id,
                amount=kill_amount,
                idempotency_key=f"kill_reward:{result.id}",
                reference_id=result.id,
                description=(
                    f"Kill reward: {kill_result.breakdown} "
                    f"(₹{kill_amount})"
                ),
                meta={**meta, "breakdown": kill_result.breakdown, "prize_type": "kill_reward"},
            )
            kill_tx_id = kill_tx.id
        except DuplicateTransactionError:
            logger.warning(
                "Kill reward already credited for result %s — treating as success", result.id
            )

    # ── Stamp result row ───────────────────────────────────────────────────────
    result.prize_amount = prize_amount
    result.kill_reward_amount = kill_amount
    result.kill_reward_eligible = kill_result.eligible
    result.kill_reward_breakdown = kill_result.breakdown
    result.payout_distributed = True
    result.payout_distributed_at = now
    result.payout_tx_prize_id = prize_tx_id
    result.payout_tx_kill_id = kill_tx_id
    result.result_status = ResultStatus.finalized
    result.updated_at = now

    await db.flush([result])

    await audit_service.log(
        db=db,
        actor_type=ActorType.system,
        actor_id=actor_id,
        action="result.payout_distributed",
        entity_type="tournament_result",
        entity_id=str(result.id),
        new_value={
            "placement": result.placement,
            "kills": result.kills,
            "prize_amount": str(prize_amount),
            "kill_reward_amount": str(kill_amount),
            "kill_breakdown": kill_result.breakdown,
            "prize_tx_id": str(prize_tx_id) if prize_tx_id else None,
            "kill_tx_id": str(kill_tx_id) if kill_tx_id else None,
        },
    )

    return result


async def distribute_all_payouts(
    db: AsyncSession,
    tournament_id: uuid.UUID,
    config: PrizeConfig,
    *,
    actor_id: str = "system",
) -> list[TournamentResult]:
    """
    Distribute payouts to ALL finalized/verified results for a tournament.
    Skips already-paid results (idempotent).
    Called by tournament completion flow.
    Returns list of results that were processed.
    """
    from app.models.result import TournamentResult

    result_rows = await db.execute(
        select(TournamentResult).where(
            TournamentResult.tournament_id == tournament_id,
            TournamentResult.payout_distributed == False,  # noqa: E712
            TournamentResult.result_status.in_(
                [ResultStatus.entered.value, ResultStatus.verified.value]
            ),
        )
    )
    results = list(result_rows.scalars().all())

    processed = []
    for r in results:
        await distribute_payout(db, r, config, actor_id=actor_id)
        processed.append(r)

    logger.info(
        "Distributed payouts for %d results in tournament %s",
        len(processed), tournament_id,
    )
    return processed
