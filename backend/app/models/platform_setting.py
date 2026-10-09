"""
PlatformSetting — key/value store for operational platform configuration.

Used for settings that Super Admin should be able to change at runtime
without a code deploy:
  - UPI ID for manual deposits
  - Manual deposit instructions
  - Minimum withdrawal amount
  - Enabled deposit methods
  - QR storage reference

Security
--------
- Settings are loaded by the deposit/withdrawal service at runtime.
- Secret keys (Razorpay) are NEVER stored here — they come from environment.
- Player-facing APIs expose only non-sensitive setting values (UPI ID,
  instructions, QR reference). Secret/internal settings are never returned
  to players.

Seeded on startup with safe defaults via bootstrap_service.
Super Admin can update via admin settings API (future phase).
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, JSON, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models._types import utcnow

_jsonb = JSONB().with_variant(JSON(), "sqlite")

# ── Well-known setting keys ───────────────────────────────────────────────────
# Import these constants wherever you need to read a setting.

SETTING_MIN_WITHDRAWAL_AMOUNT = "min_withdrawal_amount"       # Decimal string e.g. "100.00"
SETTING_MIN_DEPOSIT_AMOUNT = "min_deposit_amount"             # Decimal string e.g. "10.00"
SETTING_MAX_DEPOSIT_AMOUNT = "max_deposit_amount"             # Decimal string e.g. "50000.00"
SETTING_UPI_ID = "upi_id"                                     # e.g. "auraplayff@upi"
SETTING_UPI_DISPLAY_NAME = "upi_display_name"                 # e.g. "AuraPlayFF"
SETTING_UPI_ACCOUNT_NAME = "upi_account_name"                 # Receiver/account name shown to player
SETTING_MANUAL_DEPOSIT_INSTRUCTIONS = "manual_deposit_instructions"
SETTING_QR_STORAGE_REF = "qr_storage_ref"                    # Supabase storage path for QR image
SETTING_MANUAL_PAYMENT_ACTIVE = "manual_payment_active"       # "true" | "false"
SETTING_DEPOSIT_METHODS_ENABLED = "deposit_methods_enabled"   # JSON array ["online","manual"]
SETTING_WITHDRAWAL_ENABLED = "withdrawal_enabled"             # "true" | "false"
SETTING_ONLINE_DEPOSIT_ENABLED = "online_deposit_enabled"     # "true" | "false"

# ── Defaults ──────────────────────────────────────────────────────────────────
SETTING_DEFAULTS: dict[str, str] = {
    SETTING_MIN_WITHDRAWAL_AMOUNT: "100.00",
    SETTING_MIN_DEPOSIT_AMOUNT: "10.00",
    SETTING_MAX_DEPOSIT_AMOUNT: "50000.00",
    SETTING_UPI_ID: "",
    SETTING_UPI_DISPLAY_NAME: "AuraPlayFF",
    SETTING_UPI_ACCOUNT_NAME: "",
    SETTING_MANUAL_DEPOSIT_INSTRUCTIONS: (
        "Please transfer the exact amount to the UPI ID shown and enter your UTR number."
    ),
    SETTING_QR_STORAGE_REF: "",
    SETTING_MANUAL_PAYMENT_ACTIVE: "false",
    SETTING_DEPOSIT_METHODS_ENABLED: '["online","manual"]',
    SETTING_WITHDRAWAL_ENABLED: "true",
    SETTING_ONLINE_DEPOSIT_ENABLED: "true",
}

# Settings that must NEVER be returned to players
_INTERNAL_SETTING_KEYS: frozenset[str] = frozenset({
    SETTING_DEPOSIT_METHODS_ENABLED,
    SETTING_WITHDRAWAL_ENABLED,
    SETTING_ONLINE_DEPOSIT_ENABLED,
})


class PlatformSetting(Base):
    """
    Single row per setting key.
    All values stored as text (cast at application layer).
    """
    __tablename__ = "platform_settings"
    __table_args__ = (
        UniqueConstraint("key", name="uq_platform_settings_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    key: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_public: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False,
        server_default=text("false"),
        comment="If true, value can be returned to authenticated players",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        server_default=text("now()"),
    )
    updated_by: Mapped[str | None] = mapped_column(
        String(80), nullable=True,
        comment="agent_id or 'system' of the actor who last updated this setting",
    )

    def __repr__(self) -> str:
        return f"<PlatformSetting key={self.key!r} value={self.value!r}>"
