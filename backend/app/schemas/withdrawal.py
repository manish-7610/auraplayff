"""
Pydantic v2 schemas for withdrawal endpoints.

Player-facing:
  CreateWithdrawalRequest       — POST /api/withdrawals
  WithdrawalResponse            — single withdrawal (player view)
  WithdrawalListResponse        — paginated list
  WalletWithdrawalSummary       — wallet balance with reservation breakdown

Admin-facing:
  AdminWithdrawalResponse       — full record with balance/staff fields
  AdminWithdrawalListResponse   — paginated list
  AdminProcessWithdrawalRequest — mark as processing
  AdminMarkPaidRequest          — mark as paid with UTR
  AdminRejectWithdrawalRequest  — reject with reason

Security:
  - Players never see: processed_by_id, wallet_*_tx_id, internal meta.
  - UPI ID is shown to the owning player only (obscured in list views if desired).
  - Amounts are strings to preserve Decimal precision.
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, Field, field_validator


# ── UPI ID validation ──────────────────────────────────────────────────────────
# Basic format: localpart@provider  (e.g. user@upi, 9999999999@paytm)
_UPI_RE = re.compile(r"^[\w.\-+]{2,50}@[\w]{2,30}$")


def _validate_upi_id(v: str) -> str:
    v = v.strip().lower()
    if not _UPI_RE.match(v):
        raise ValueError(
            "Invalid UPI ID format. Expected format: username@provider "
            "(e.g. player@upi, 9876543210@paytm)."
        )
    return v


# ── Player request schemas ─────────────────────────────────────────────────────

class CreateWithdrawalRequest(BaseModel):
    """
    Player requests a withdrawal of Winning Balance.
    Amount must be >= min_withdrawal_amount (₹100 default).
    Only winning_balance is withdrawable — service enforces this.
    """
    amount: Decimal = Field(
        ...,
        gt=Decimal("0"),
        description="Withdrawal amount in INR. Must be >= ₹100.",
    )
    upi_id: str = Field(
        ...,
        min_length=5,
        max_length=100,
        description="Destination UPI ID. Format: username@provider",
    )

    @field_validator("amount")
    @classmethod
    def validate_amount_precision(cls, v: Decimal) -> Decimal:
        if v != v.quantize(Decimal("0.01")):
            raise ValueError("Amount must have at most 2 decimal places.")
        return v

    @field_validator("upi_id")
    @classmethod
    def validate_upi(cls, v: str) -> str:
        return _validate_upi_id(v)


# ── Player response schemas ────────────────────────────────────────────────────

class WithdrawalResponse(BaseModel):
    """Single withdrawal as seen by the owning player."""
    id: uuid.UUID
    status: str
    amount: str
    currency: str
    upi_id: str
    payout_utr: Optional[str] = None     # Visible once paid
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_withdrawal(cls, w: "Withdrawal") -> "WithdrawalResponse":  # type: ignore[name-defined]  # noqa: F821
        return cls(
            id=w.id,
            status=w.status.value,
            amount=str(w.amount),
            currency=w.currency,
            upi_id=w.upi_id,
            payout_utr=w.payout_utr,
            created_at=w.created_at,
            updated_at=w.updated_at,
        )


class WithdrawalListResponse(BaseModel):
    items: list[WithdrawalResponse]
    total: int
    limit: int
    offset: int


class WalletWithdrawalSummary(BaseModel):
    """
    Full wallet balance breakdown including withdrawal reservation.
    Returned by GET /api/wallet/me or GET /api/withdrawals/balance-summary.
    """
    deposit_balance: str
    winning_balance: str
    referral_balance: str
    withdrawal_hold: str
    available_winning_balance: str   # winning_balance - withdrawal_hold
    total_balance: str               # deposit + winning + referral (informational)
    is_frozen: bool

    @classmethod
    def from_wallet(cls, wallet: "Wallet") -> "WalletWithdrawalSummary":  # type: ignore[name-defined]  # noqa: F821
        available = wallet.winning_balance - wallet.withdrawal_hold
        if available < Decimal("0"):
            available = Decimal("0")
        total = (
            wallet.deposit_balance
            + wallet.winning_balance
            + wallet.referral_balance
        )
        return cls(
            deposit_balance=str(wallet.deposit_balance),
            winning_balance=str(wallet.winning_balance),
            referral_balance=str(wallet.referral_balance),
            withdrawal_hold=str(wallet.withdrawal_hold),
            available_winning_balance=str(available),
            total_balance=str(total),
            is_frozen=wallet.is_frozen,
        )


# ── Admin request schemas ──────────────────────────────────────────────────────

class AdminProcessWithdrawalRequest(BaseModel):
    """Admin marks withdrawal as processing (started)."""
    note: Optional[str] = Field(None, max_length=500)


class AdminMarkPaidRequest(BaseModel):
    """Admin marks withdrawal as paid after transferring funds."""
    payout_utr: str = Field(
        ...,
        min_length=4,
        max_length=100,
        description="UTR or reference ID for the outgoing payment.",
    )
    note: Optional[str] = Field(None, max_length=500)


class AdminRejectWithdrawalRequest(BaseModel):
    """Admin rejects a pending/processing withdrawal."""
    reason: str = Field(..., min_length=5, max_length=500)


# ── Admin response schemas ─────────────────────────────────────────────────────

class AdminWithdrawalResponse(BaseModel):
    """Full withdrawal record for admin panel."""
    id: uuid.UUID
    user_id: uuid.UUID
    username: Optional[str] = None
    ff_name: Optional[str] = None
    status: str
    amount: str
    currency: str
    upi_id: str
    payout_utr: Optional[str] = None
    payout_note: Optional[str] = None
    processed_by_id: Optional[uuid.UUID] = None
    processed_at: Optional[datetime] = None
    wallet_reserve_tx_id: Optional[uuid.UUID] = None
    wallet_complete_tx_id: Optional[uuid.UUID] = None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_withdrawal(cls, w: "Withdrawal") -> "AdminWithdrawalResponse":  # type: ignore[name-defined]  # noqa: F821
        username: Optional[str] = None
        ff_name: Optional[str] = None
        try:
            if w.user is not None:
                username = w.user.username
                ff_name = w.user.ff_name  # may be None if player hasn't set it
        except Exception:
            pass  # relationship not loaded — fields stay None

        return cls(
            id=w.id,
            user_id=w.user_id,
            username=username,
            ff_name=ff_name,
            status=w.status.value,
            amount=str(w.amount),
            currency=w.currency,
            upi_id=w.upi_id,
            payout_utr=w.payout_utr,
            payout_note=w.payout_note,
            processed_by_id=w.processed_by_id,
            processed_at=w.processed_at,
            wallet_reserve_tx_id=w.wallet_reserve_tx_id,
            wallet_complete_tx_id=w.wallet_complete_tx_id,
            created_at=w.created_at,
            updated_at=w.updated_at,
        )


class AdminWithdrawalListResponse(BaseModel):
    items: list[AdminWithdrawalResponse]
    total: int
    limit: int
    offset: int
