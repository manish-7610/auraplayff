"""
Pydantic v2 schemas for deposit endpoints.

Player-facing schemas:
  CreateOnlineDepositRequest   — POST /api/deposits/online
  CreateManualDepositRequest   — POST /api/deposits/manual
  SubmitUTRRequest             — POST /api/deposits/manual/{id}/utr
  VerifyOnlinePaymentRequest   — POST /api/deposits/online/{id}/verify
  DepositResponse              — single deposit (player view)
  DepositListResponse          — paginated list

Admin-facing schemas:
  AdminDepositResponse         — single deposit with staff fields
  AdminDepositListResponse     — paginated list
  AdminApproveDepositRequest   — POST /api/admin/deposits/{id}/approve
  AdminRejectDepositRequest    — POST /api/admin/deposits/{id}/reject

Security rules:
  - Player schemas never expose: provider_payment_id in raw form, admin_note,
    verified_by_id, wallet_tx_id, meta internals.
  - Amounts serialised as strings to preserve Decimal precision across JSON.
  - Status changes are never accepted from players.
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, Field, field_validator, model_validator


# ── Shared validator ───────────────────────────────────────────────────────────

_UTR_RE = re.compile(r"^[A-Za-z0-9]{8,50}$")


def _validate_utr(v: str) -> str:
    v = v.strip()
    if not _UTR_RE.match(v):
        raise ValueError(
            "UTR must be 8–50 alphanumeric characters with no spaces or special chars."
        )
    return v.upper()


# ── Player request schemas ─────────────────────────────────────────────────────

class CreateOnlineDepositRequest(BaseModel):
    """Player requests a new Razorpay deposit order."""
    amount: Decimal = Field(
        ...,
        gt=Decimal("0"),
        description="Deposit amount in INR. Must be >= minimum deposit amount.",
    )

    @field_validator("amount")
    @classmethod
    def validate_amount_precision(cls, v: Decimal) -> Decimal:
        if v != v.quantize(Decimal("0.01")):
            raise ValueError("Amount must have at most 2 decimal places.")
        return v


class VerifyOnlinePaymentRequest(BaseModel):
    """
    Client sends this after Razorpay checkout succeeds.
    Server MUST re-verify the signature — never trust this alone.
    """
    razorpay_order_id: str = Field(..., min_length=1, max_length=100)
    razorpay_payment_id: str = Field(..., min_length=1, max_length=100)
    razorpay_signature: str = Field(..., min_length=1, max_length=256)


class CreateManualDepositRequest(BaseModel):
    """Player initiates a manual UPI deposit — just declares the amount."""
    amount: Decimal = Field(
        ...,
        gt=Decimal("0"),
        description="Deposit amount in INR. Must be >= minimum deposit amount.",
    )

    @field_validator("amount")
    @classmethod
    def validate_amount_precision(cls, v: Decimal) -> Decimal:
        if v != v.quantize(Decimal("0.01")):
            raise ValueError("Amount must have at most 2 decimal places.")
        return v


class SubmitUTRRequest(BaseModel):
    """
    Player submits UTR/transaction ID after making the UPI payment.
    Submission does NOT credit the wallet — admin must approve.
    """
    utr: str = Field(..., min_length=8, max_length=50)
    payer_info: Optional[str] = Field(
        None,
        max_length=200,
        description="Optional payer VPA, bank reference, or note.",
    )

    @field_validator("utr")
    @classmethod
    def validate_utr(cls, v: str) -> str:
        return _validate_utr(v)


# ── Player response schemas ────────────────────────────────────────────────────

class OnlineDepositInitResponse(BaseModel):
    """
    Returned after creating a Razorpay order.
    Contains only what the frontend Razorpay SDK needs to open checkout.
    NEVER includes KEY_SECRET.
    """
    deposit_id: uuid.UUID
    razorpay_key_id: str          # Public key — safe to send to frontend
    razorpay_order_id: str
    amount: str                   # INR string e.g. "500.00"
    currency: str
    description: str = "AuraPlayFF Deposit"


class DepositResponse(BaseModel):
    """Single deposit record as seen by the owning player."""
    id: uuid.UUID
    method: str
    status: str
    amount: str
    currency: str
    utr: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_deposit(cls, d: "Deposit") -> "DepositResponse":  # type: ignore[name-defined]  # noqa: F821
        return cls(
            id=d.id,
            method=d.method.value,
            status=d.status.value,
            amount=str(d.amount),
            currency=d.currency,
            utr=d.utr,
            created_at=d.created_at,
            updated_at=d.updated_at,
        )


class DepositListResponse(BaseModel):
    items: list[DepositResponse]
    total: int
    limit: int
    offset: int


# ── Admin response schemas ─────────────────────────────────────────────────────

class AdminDepositResponse(BaseModel):
    """Full deposit record for admin panel."""
    id: uuid.UUID
    user_id: uuid.UUID
    username: Optional[str] = None          # populated when user is eagerly loaded
    method: str
    status: str
    amount: str
    currency: str
    provider: Optional[str] = None
    provider_order_id: Optional[str] = None
    provider_payment_id: Optional[str] = None
    utr: Optional[str] = None
    utr_normalized: Optional[str] = None
    payer_info: Optional[str] = None
    admin_note: Optional[str] = None
    verified_by_id: Optional[uuid.UUID] = None
    verified_at: Optional[datetime] = None
    wallet_tx_id: Optional[uuid.UUID] = None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_deposit(cls, d: "Deposit") -> "AdminDepositResponse":  # type: ignore[name-defined]  # noqa: F821
        # Safely surface username when the user relationship is already loaded.
        username: Optional[str] = None
        try:
            if d.user is not None:
                username = d.user.username
        except Exception:
            pass  # relationship not loaded — username stays None
        return cls(
            id=d.id,
            user_id=d.user_id,
            username=username,
            method=d.method.value,
            status=d.status.value,
            amount=str(d.amount),
            currency=d.currency,
            provider=d.provider,
            provider_order_id=d.provider_order_id,
            provider_payment_id=d.provider_payment_id,
            utr=d.utr,
            utr_normalized=d.utr_normalized,
            payer_info=d.payer_info,
            admin_note=d.admin_note,
            verified_by_id=d.verified_by_id,
            verified_at=d.verified_at,
            wallet_tx_id=d.wallet_tx_id,
            created_at=d.created_at,
            updated_at=d.updated_at,
        )


class AdminDepositListResponse(BaseModel):
    items: list[AdminDepositResponse]
    total: int
    limit: int
    offset: int


class AdminApproveDepositRequest(BaseModel):
    """Admin approves a pending manual deposit."""
    note: Optional[str] = Field(None, max_length=500)


class AdminRejectDepositRequest(BaseModel):
    """Admin rejects a pending manual deposit."""
    reason: str = Field(..., min_length=3, max_length=500)
