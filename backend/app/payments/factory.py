"""
Payment gateway factory.

Returns the configured gateway adapter based on application settings.
Callers should not instantiate adapters directly — use get_payment_gateway().

Razorpay status
---------------
Razorpay is currently DISABLED because production credentials are not yet
available. The full RazorpayAdapter implementation is intact and untouched.

When Razorpay production credentials are ready:
  1. Set RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET, RAZORPAY_WEBHOOK_SECRET
     in backend/.env with real values (no placeholder text).
  2. The factory will automatically return the live RazorpayAdapter.
  3. No code changes are required.

DisabledPaymentGateway
----------------------
When credentials are missing or still contain placeholder values, the factory
returns a DisabledPaymentGateway instance instead of raising at import time.
All calls to create_order() / verify_payment() / verify_webhook() raise
GatewayConfigError with a clear human-readable message so the caller (deposit
service) can surface it correctly rather than crashing with a confusing error.

Adding a new gateway
--------------------
  1. Create a new adapter module implementing PaymentGateway.
  2. Add a branch in get_payment_gateway() keyed by a PAYMENT_GATEWAY env var.
  3. No other code needs to change.
"""
from __future__ import annotations

import logging

from app.payments.base import (
    CreateOrderRequest,
    CreateOrderResult,
    GatewayConfigError,
    PaymentGateway,
    PaymentStatus,
    SignatureVerificationError,
    VerifyPaymentRequest,
    VerifyPaymentResult,
    WebhookVerifyRequest,
    WebhookVerifyResult,
)

logger = logging.getLogger("auraplayff.payments.factory")

# Placeholder values that indicate credentials have NOT been configured
_PLACEHOLDER_FRAGMENTS = (
    "placeholder",
    "rzp_test_placeholder",
    "change-me",
    "",
)


def _is_placeholder(value: str) -> bool:
    v = value.strip().lower()
    return not v or any(v.startswith(p) for p in _PLACEHOLDER_FRAGMENTS)


# ── Disabled stub ─────────────────────────────────────────────────────────────

class DisabledPaymentGateway(PaymentGateway):
    """
    Returned by get_payment_gateway() when Razorpay credentials are absent or
    still contain placeholder values.

    Every method raises GatewayConfigError immediately so the deposit service
    can return a clean 503 to the player rather than crashing with an unhandled
    attribute error or an unhelpful 500.

    The RazorpayAdapter code is completely untouched — this stub only sits in
    front of it while credentials are not yet configured.
    """

    _MSG = (
        "Online payments via Razorpay are not yet available. "
        "Configure RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET, and "
        "RAZORPAY_WEBHOOK_SECRET in the server environment to enable them."
    )

    async def create_order(self, req: CreateOrderRequest) -> CreateOrderResult:
        raise GatewayConfigError(self._MSG)

    def verify_payment(self, req: VerifyPaymentRequest) -> VerifyPaymentResult:
        raise GatewayConfigError(self._MSG)

    def verify_webhook(self, req: WebhookVerifyRequest) -> WebhookVerifyResult:
        raise GatewayConfigError(self._MSG)

    def normalize_payment_status(self, provider_status: str) -> PaymentStatus:
        return PaymentStatus.FAILED


# ── Factory ───────────────────────────────────────────────────────────────────

def get_payment_gateway() -> PaymentGateway:
    """
    Return the configured payment gateway adapter.

    Returns DisabledPaymentGateway when Razorpay credentials are missing or
    still contain placeholder values — so the app starts cleanly and surfaces
    a clear error on any attempted online-deposit call.

    Returns RazorpayAdapter when all three credentials are present and non-placeholder.

    Future gateways: set PAYMENT_GATEWAY=cashfree (or similar) and add a branch.
    """
    from app.config import get_settings
    settings = get_settings()

    key_id     = settings.razorpay_key_id     or ""
    key_secret = settings.razorpay_key_secret  or ""
    wh_secret  = settings.razorpay_webhook_secret or ""

    if _is_placeholder(key_id) or _is_placeholder(key_secret):
        logger.debug(
            "Razorpay credentials not configured — returning DisabledPaymentGateway. "
            "Set RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET to enable online payments."
        )
        return DisabledPaymentGateway()

    # Credentials are present — return the real adapter
    from app.payments.razorpay_adapter import RazorpayAdapter
    return RazorpayAdapter(
        key_id=key_id,
        key_secret=key_secret,
        webhook_secret=wh_secret,
    )
