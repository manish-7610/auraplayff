"""
Payment gateway abstraction layer.

Defines the protocol/interface that every payment gateway adapter must implement.
Business logic (deposit service, webhook handler) depends ONLY on these abstractions
so a new gateway (PayU, Cashfree, Stripe, etc.) can be plugged in without touching
any wallet or deposit service code.

Convention
----------
Amounts are always passed and returned as Python Decimal with 2 d.p.
Currency is always a 3-letter ISO code string (e.g. "INR").
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any


# ── Status enum ───────────────────────────────────────────────────────────────

class PaymentStatus(StrEnum):
    """Normalised payment status — independent of provider terminology."""
    CREATED = "created"       # Order created, awaiting payment
    AUTHORIZED = "authorized" # Authorised but not captured (rare for Razorpay)
    CAPTURED = "captured"     # Payment captured / paid successfully
    FAILED = "failed"         # Payment attempt failed
    REFUNDED = "refunded"     # Payment refunded (full or partial)
    EXPIRED = "expired"       # Order expired without payment


# ── Request / result dataclasses ──────────────────────────────────────────────

@dataclass
class CreateOrderRequest:
    """Parameters needed to create a payment order with the provider."""
    amount: Decimal                   # e.g. Decimal("500.00")
    currency: str = "INR"
    receipt: str = ""                 # Internal deposit ID (used as receipt/notes)
    notes: dict[str, str] = field(default_factory=dict)


@dataclass
class CreateOrderResult:
    """What comes back after creating an order at the provider."""
    provider_order_id: str            # e.g. "order_ABC123"
    amount: Decimal                   # Amount as confirmed by provider
    currency: str
    status: PaymentStatus
    raw: dict[str, Any] = field(default_factory=dict)  # Full provider response


@dataclass
class VerifyPaymentRequest:
    """Data needed to verify a completed payment server-side."""
    provider_order_id: str
    provider_payment_id: str
    provider_signature: str           # HMAC signature sent by provider/client
    expected_amount: Decimal
    currency: str = "INR"


@dataclass
class VerifyPaymentResult:
    """Result of server-side payment verification."""
    is_valid: bool
    status: PaymentStatus
    provider_payment_id: str
    provider_order_id: str
    amount_verified: bool             # True if amount matches expected
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class WebhookVerifyRequest:
    """Inbound webhook payload from provider."""
    raw_body: bytes                   # Raw request body for signature check
    signature_header: str             # e.g. X-Razorpay-Signature value
    event_id: str = ""               # Provider event ID if available


@dataclass
class WebhookVerifyResult:
    """Result of webhook signature verification + event extraction."""
    is_valid: bool
    event_type: str                   # e.g. "payment.captured"
    event_id: str
    provider_order_id: str
    provider_payment_id: str
    amount: Decimal
    currency: str
    status: PaymentStatus
    raw: dict[str, Any] = field(default_factory=dict)


# ── Exceptions ────────────────────────────────────────────────────────────────

class GatewayError(Exception):
    """Base for all payment gateway errors."""
    def __init__(self, message: str = "Payment gateway error.", code: str = "gateway_error") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


class GatewayConfigError(GatewayError):
    """Raised when the gateway is not properly configured (missing credentials)."""
    def __init__(self, detail: str = "Payment gateway is not configured.") -> None:
        super().__init__(detail, code="gateway_config_error")


class GatewayNetworkError(GatewayError):
    """Raised when the provider API call fails due to network/HTTP error."""
    def __init__(self, detail: str = "Payment gateway network error.") -> None:
        super().__init__(detail, code="gateway_network_error")


class SignatureVerificationError(GatewayError):
    """Raised when HMAC signature does not match — possible tampering."""
    def __init__(self) -> None:
        super().__init__(
            "Payment signature verification failed.",
            code="signature_verification_failed",
        )


# ── Abstract base class ───────────────────────────────────────────────────────

class PaymentGateway(abc.ABC):
    """
    Abstract base for every payment gateway adapter.

    Each concrete adapter (Razorpay, Cashfree, PayU …) must implement:
      - create_order()
      - verify_payment()
      - verify_webhook()

    Adapters must:
      - Never log or expose secrets.
      - Always work with Decimal amounts (never float).
      - Raise GatewayError subclasses on failures.
      - Be stateless (no per-request mutable state on the instance).
    """

    @abc.abstractmethod
    async def create_order(self, req: CreateOrderRequest) -> CreateOrderResult:
        """Create a payment order with the provider and return order details."""
        ...

    @abc.abstractmethod
    def verify_payment(self, req: VerifyPaymentRequest) -> VerifyPaymentResult:
        """
        Verify a completed payment using the provider's signature algorithm.
        This is a synchronous cryptographic check — no network call needed.
        """
        ...

    @abc.abstractmethod
    def verify_webhook(self, req: WebhookVerifyRequest) -> WebhookVerifyResult:
        """
        Verify an inbound provider webhook: check signature, extract event data.
        This is synchronous (HMAC check only — no outbound network call).
        """
        ...

    @abc.abstractmethod
    def normalize_payment_status(self, provider_status: str) -> PaymentStatus:
        """Map a provider-specific status string to our canonical PaymentStatus."""
        ...
