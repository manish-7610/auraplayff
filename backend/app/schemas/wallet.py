"""
Pydantic v2 schemas for wallet and referral endpoints.

Player-facing schemas never expose:
  - internal wallet UUIDs in ways that reveal other users' data
  - idempotency keys
  - created_by_id (staff identity)
  - raw balance_before snapshots on admin views (available in admin schemas)

Admin schemas add fields needed for support/audit work.
Money amounts are serialised as strings to preserve precision across JSON
(avoid JavaScript float issues with large Decimal values).
"""
from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Optional

from pydantic import BaseModel, Field, field_serializer, model_validator


# ── Shared helpers ─────────────────────────────────────────────────────────────

def _str_decimal(v: Decimal | None) -> str | None:
    if v is None:
        return None
    return str(v)


# ── Wallet summary ─────────────────────────────────────────────────────────────

class WalletSummaryResponse(BaseModel):
    """
    Player's own wallet overview — returned by GET /api/wallet/me.

    Phase 5 additions:
      withdrawal_hold            — amount currently reserved for pending withdrawals
      available_winning_balance  — winning_balance minus withdrawal_hold
    """

    wallet_id: uuid.UUID
    deposit_balance: str          # Withdrawable real-money balance
    winning_balance: str          # Tournament prizes / kill rewards
    referral_balance: str         # Non-withdrawable referral bonus
    withdrawal_hold: str          # Amount reserved for pending withdrawal requests
    available_winning_balance: str  # winning_balance - withdrawal_hold
    total_balance: str            # deposit + winning + referral (informational)
    is_frozen: bool
    updated_at: datetime

    @classmethod
    def from_wallet(cls, wallet: "Wallet") -> "WalletSummaryResponse":  # type: ignore[name-defined]  # noqa: F821
        from decimal import Decimal
        available = max(
            wallet.winning_balance - wallet.withdrawal_hold,
            Decimal("0"),
        )
        total = wallet.deposit_balance + wallet.winning_balance + wallet.referral_balance
        return cls(
            wallet_id=wallet.id,
            deposit_balance=str(wallet.deposit_balance),
            winning_balance=str(wallet.winning_balance),
            referral_balance=str(wallet.referral_balance),
            withdrawal_hold=str(wallet.withdrawal_hold),
            available_winning_balance=str(available),
            total_balance=str(total),
            is_frozen=wallet.is_frozen,
            updated_at=wallet.updated_at,
        )


# ── Wallet transaction (player-facing) ────────────────────────────────────────

class WalletTransactionResponse(BaseModel):
    """Single ledger entry as seen by the player."""

    id: uuid.UUID
    type: str
    balance_column: str
    direction: str
    amount: str
    balance_after: str
    reference_type: Optional[str] = None
    description: Optional[str] = None
    created_at: datetime

    @classmethod
    def from_tx(cls, tx: "WalletTransaction") -> "WalletTransactionResponse":  # type: ignore[name-defined]  # noqa: F821
        return cls(
            id=tx.id,
            type=tx.type.value,
            balance_column=tx.balance_column.value,
            direction=tx.direction.value,
            amount=str(tx.amount),
            balance_after=str(tx.balance_after),
            reference_type=tx.reference_type,
            description=tx.description,
            created_at=tx.created_at,
        )


class WalletTransactionListResponse(BaseModel):
    items: list[WalletTransactionResponse]
    total: int
    limit: int
    offset: int


# ── Referral info (player-facing) ─────────────────────────────────────────────

class ReferralInfoResponse(BaseModel):
    """Player's own referral code + incoming referral status."""

    my_referral_code: str
    # Who referred this user (if any)
    referred_by_username: Optional[str] = None
    referral_status: Optional[str] = None   # "pending" | "completed" | "rewarded"
    # Reward amounts
    reward_per_referral: str
    # outgoing referral summary
    total_referrals_made: int
    pending_referrals: int
    completed_referrals: int
    rewarded_referrals: int
    total_bonus_earned: str


class ReferralItemResponse(BaseModel):
    """Single outgoing referral as seen in the player's referral list."""

    referral_id: uuid.UUID
    referred_username: str
    referral_code_used: str
    created_at: datetime
    first_tournament_completed: bool
    first_tournament_completed_at: Optional[datetime] = None
    referrer_rewarded: bool
    referrer_rewarded_at: Optional[datetime] = None


class ReferralListResponse(BaseModel):
    items: list[ReferralItemResponse]
    total: int
    limit: int
    offset: int


# ── Admin wallet schemas ───────────────────────────────────────────────────────

class AdminWalletSummaryResponse(BaseModel):
    """Admin view of a player wallet — includes frozen state and all balances."""

    wallet_id: uuid.UUID
    user_id: uuid.UUID
    username: str
    deposit_balance: str
    winning_balance: str
    referral_balance: str
    withdrawal_hold: str
    total_balance: str
    is_frozen: bool
    updated_at: datetime

    @classmethod
    def from_wallet_user(
        cls,
        wallet: "Wallet",  # type: ignore[name-defined]  # noqa: F821
        username: str,
    ) -> "AdminWalletSummaryResponse":
        total = (
            wallet.deposit_balance
            + wallet.winning_balance
            + wallet.referral_balance
        )
        return cls(
            wallet_id=wallet.id,
            user_id=wallet.user_id,
            username=username,
            deposit_balance=str(wallet.deposit_balance),
            winning_balance=str(wallet.winning_balance),
            referral_balance=str(wallet.referral_balance),
            withdrawal_hold=str(wallet.withdrawal_hold),
            total_balance=str(total),
            is_frozen=wallet.is_frozen,
            updated_at=wallet.updated_at,
        )


class AdminWalletTransactionResponse(BaseModel):
    """Admin view of a ledger entry — includes balance_before and idempotency_key."""

    id: uuid.UUID
    wallet_id: uuid.UUID
    user_id: uuid.UUID
    type: str
    balance_column: str
    direction: str
    amount: str
    balance_before: str
    balance_after: str
    idempotency_key: Optional[str] = None
    reference_id: Optional[uuid.UUID] = None
    reference_type: Optional[str] = None
    description: Optional[str] = None
    meta: Optional[dict[str, Any]] = None
    created_at: datetime
    created_by_id: Optional[uuid.UUID] = None

    @classmethod
    def from_tx(cls, tx: "WalletTransaction") -> "AdminWalletTransactionResponse":  # type: ignore[name-defined]  # noqa: F821
        return cls(
            id=tx.id,
            wallet_id=tx.wallet_id,
            user_id=tx.user_id,
            type=tx.type.value,
            balance_column=tx.balance_column.value,
            direction=tx.direction.value,
            amount=str(tx.amount),
            balance_before=str(tx.balance_before),
            balance_after=str(tx.balance_after),
            idempotency_key=tx.idempotency_key,
            reference_id=tx.reference_id,
            reference_type=tx.reference_type,
            description=tx.description,
            meta=tx.meta,
            created_at=tx.created_at,
            created_by_id=tx.created_by_id,
        )


class AdminTransactionListResponse(BaseModel):
    items: list[AdminWalletTransactionResponse]
    total: int
    limit: int
    offset: int


class AdminReferralItemResponse(BaseModel):
    """Admin view of a single referral relationship."""

    referral_id: uuid.UUID
    referrer_id: uuid.UUID
    referrer_username: str
    referred_user_id: uuid.UUID
    referred_username: str
    referral_code_used: str
    created_at: datetime
    first_tournament_completed: bool
    first_tournament_completed_at: Optional[datetime] = None
    referrer_rewarded: bool
    referrer_rewarded_at: Optional[datetime] = None
    referred_user_rewarded: bool
    referred_user_rewarded_at: Optional[datetime] = None


class AdminReferralListResponse(BaseModel):
    items: list[AdminReferralItemResponse]
    total: int
    limit: int
    offset: int


# ── Admin adjustment request ───────────────────────────────────────────────────

class AdminAdjustRequest(BaseModel):
    """
    Body for POST /api/admin/wallets/{user_id}/adjust

    balance_column: "deposit" | "winning" | "referral"
    direction:      "credit" | "debit"
    amount:         positive value, 2 decimal places max
    reason:         mandatory free-text explanation (audit trail)
    """

    balance_column: str = Field(..., pattern=r"^(deposit|winning|referral)$")
    direction: str = Field(..., pattern=r"^(credit|debit)$")
    amount: Decimal = Field(..., gt=Decimal("0"), decimal_places=2)
    reason: str = Field(..., min_length=5, max_length=500)

    @model_validator(mode="after")
    def amount_precision(self) -> "AdminAdjustRequest":
        # Ensure no more than 2 decimal places
        if self.amount != self.amount.quantize(Decimal("0.01")):
            raise ValueError("Amount must have at most 2 decimal places.")
        return self


class AdminAdjustResponse(BaseModel):
    message: str
    transaction_id: uuid.UUID
    balance_column: str
    direction: str
    amount: str
    new_balance: str


# ── Freeze / unfreeze ──────────────────────────────────────────────────────────

class WalletFreezeResponse(BaseModel):
    message: str
    wallet_id: uuid.UUID
    is_frozen: bool
