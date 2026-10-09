"""
Razorpay payment gateway adapter.

Implements the PaymentGateway interface for Razorpay.

Security rules:
  - RAZORPAY_KEY_SECRET and RAZORPAY_WEBHOOK_SECRET are read ONLY from
    environment/config — never hardcoded, never logged, never returned to client.
  - Signature verification uses HMAC-SHA256 exactly as Razorpay documents.
  - We do NOT use the razorpay Python SDK to avoid opaque SDK behaviour and
    to keep the adapter transparent. We use httpx for API calls and hmac for
    signature verification.

Razorpay API reference:
  https://razorpay.com/docs/api/

Test mode:
  Set RAZORPAY_MODE=test and use Razorpay test keys.
  The adapter behaves identically in test and live mode —
  only the credentials differ.

Amount convention:
  Razorpay API expects amounts in PAISE (integer, smallest unit).
  This adapter converts Decimal INR ↔ integer paise internally.
  All public interface surfaces use Decimal INR.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
from decimal import Decimal, ROUND_DOWN
from typing import Any

import httpx

from app.payments.base import (
    CreateOrderRequest,
    CreateOrderResult,
    GatewayConfigError,
    GatewayNetworkError,
    PaymentGateway,
    PaymentStatus,
    SignatureVerificationError,
    VerifyPaymentRequest,
    VerifyPaymentResult,
    WebhookVerifyRequest,
    WebhookVerifyResult,
)

logger = logging.getLogger("auraplayff.payments.razorpay")

_RAZORPAY_API_BASE = "https://api.razorpay.com/v1"
_TIMEOUT = 15.0  # seconds


def _inr_to_paise(amount: Decimal) -> int:
    """Convert INR Decimal to integer paise. Truncates sub-paise values."""
    return int((amount * 100).to_integral_value(rounding=ROUND_DOWN))


def _paise_to_inr(paise: int) -> Decimal:
    """Convert integer paise to INR Decimal with 2 d.p."""
    return (Decimal(paise) / Decimal(100)).quantize(Decimal("0.01"))


class RazorpayAdapter(PaymentGateway):
    """
    Concrete Razorpay adapter.

    Instantiated once per request (via factory) with credentials from settings.
    Never stores secrets on instance attributes accessible from outside.
    """

    def __init__(self, key_id: str, key_secret: str, webhook_secret: str) -> None:
        if not key_id or not key_secret:
            raise GatewayConfigError(
                "Razorpay KEY_ID and KEY_SECRET must be configured."
            )
        self._key_id = key_id
        self._key_secret = key_secret
        self._webhook_secret = webhook_secret

    # ── Public interface ──────────────────────────────────────────────────────

    async def create_order(self, req: CreateOrderRequest) -> CreateOrderResult:
        """
        Call Razorpay Orders API to create a new order.

        Returns CreateOrderResult with provider_order_id and confirmed amount.
        Raises GatewayNetworkError on HTTP / connectivity failures.
        """
        paise = _inr_to_paise(req.amount)
        payload: dict[str, Any] = {
            "amount": paise,
            "currency": req.currency,
            "receipt": req.receipt[:40] if req.receipt else "",  # Razorpay limit: 40 chars
            "notes": req.notes,
        }

        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                response = await client.post(
                    f"{_RAZORPAY_API_BASE}/orders",
                    json=payload,
                    auth=(self._key_id, self._key_secret),
                )
        except httpx.RequestError as exc:
            logger.error("Razorpay create_order network error: %s", exc)
            raise GatewayNetworkError(f"Could not reach Razorpay: {exc}") from exc

        if response.status_code not in (200, 201):
            # Log only status code + non-secret error description
            logger.error(
                "Razorpay create_order failed: status=%d body_preview=%.200s",
                response.status_code,
                response.text,
            )
            raise GatewayNetworkError(
                f"Razorpay order creation failed (HTTP {response.status_code})."
            )

        data: dict[str, Any] = response.json()
        confirmed_paise: int = data.get("amount", paise)
        status_str: str = data.get("status", "created")

        return CreateOrderResult(
            provider_order_id=data["id"],
            amount=_paise_to_inr(confirmed_paise),
            currency=data.get("currency", req.currency),
            status=self.normalize_payment_status(status_str),
            raw=data,
        )

    def verify_payment(self, req: VerifyPaymentRequest) -> VerifyPaymentResult:
        """
        Verify a client-reported payment using HMAC-SHA256.

        Razorpay signature formula:
          HMAC_SHA256(
              key   = KEY_SECRET,
              data  = f"{order_id}|{payment_id}"
          )

        This is a pure cryptographic check — no network call.
        Raises SignatureVerificationError if the signature does not match.
        """
        expected_signature = self._hmac_sha256(
            f"{req.provider_order_id}|{req.provider_payment_id}"
        )

        if not hmac.compare_digest(expected_signature, req.provider_signature):
            raise SignatureVerificationError()

        # Signature matches — payment is authentic
        # Amount verification is done separately by the deposit service
        # (we compare req.expected_amount against the internal deposit record)
        return VerifyPaymentResult(
            is_valid=True,
            status=PaymentStatus.CAPTURED,
            provider_payment_id=req.provider_payment_id,
            provider_order_id=req.provider_order_id,
            amount_verified=True,  # Service layer re-checks against DB record
            raw={},
        )

    def verify_webhook(self, req: WebhookVerifyRequest) -> WebhookVerifyResult:
        """
        Verify a Razorpay webhook delivery.

        Razorpay sends X-Razorpay-Signature: HMAC_SHA256(raw_body, WEBHOOK_SECRET)

        Returns a WebhookVerifyResult with normalised event data.
        Raises SignatureVerificationError if webhook signature is invalid.
        Raises GatewayConfigError if webhook secret is not configured.
        """
        if not self._webhook_secret:
            raise GatewayConfigError("Razorpay webhook secret is not configured.")

        expected_sig = self._hmac_sha256_bytes(req.raw_body, self._webhook_secret)
        if not hmac.compare_digest(expected_sig, req.signature_header):
            raise SignatureVerificationError()

        try:
            event_data: dict[str, Any] = json.loads(req.raw_body)
        except (json.JSONDecodeError, ValueError) as exc:
            raise GatewayNetworkError("Webhook body is not valid JSON.") from exc

        event_type: str = event_data.get("event", "")
        event_id: str = event_data.get("id", "")

        # Extract payment entity from payload
        payload_entity = (
            event_data.get("payload", {})
            .get("payment", {})
            .get("entity", {})
        )

        provider_order_id: str = payload_entity.get("order_id", "")
        provider_payment_id: str = payload_entity.get("id", "")
        amount_paise: int = payload_entity.get("amount", 0)
        currency: str = payload_entity.get("currency", "INR")
        provider_status: str = payload_entity.get("status", "")

        return WebhookVerifyResult(
            is_valid=True,
            event_type=event_type,
            event_id=event_id,
            provider_order_id=provider_order_id,
            provider_payment_id=provider_payment_id,
            amount=_paise_to_inr(amount_paise),
            currency=currency,
            status=self.normalize_payment_status(provider_status),
            raw=event_data,
        )

    def normalize_payment_status(self, provider_status: str) -> PaymentStatus:
        """Map Razorpay-specific status strings to our PaymentStatus enum."""
        mapping: dict[str, PaymentStatus] = {
            "created": PaymentStatus.CREATED,
            "authorized": PaymentStatus.AUTHORIZED,
            "captured": PaymentStatus.CAPTURED,
            "refunded": PaymentStatus.REFUNDED,
            "failed": PaymentStatus.FAILED,
        }
        return mapping.get(provider_status.lower(), PaymentStatus.FAILED)

    # ── Private helpers ───────────────────────────────────────────────────────

    def _hmac_sha256(self, data: str) -> str:
        """HMAC-SHA256 using KEY_SECRET, returns hex digest."""
        return hmac.new(
            self._key_secret.encode("utf-8"),
            data.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _hmac_sha256_bytes(self, data: bytes, secret: str) -> str:
        """HMAC-SHA256 over raw bytes using given secret, returns hex digest."""
        return hmac.new(
            secret.encode("utf-8"),
            data,
            hashlib.sha256,
        ).hexdigest()
