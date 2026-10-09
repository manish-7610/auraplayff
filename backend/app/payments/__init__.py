"""
Payment gateway package.
Exports the gateway factory and base types so callers import from here.
"""
from app.payments.base import (
    PaymentGateway,
    CreateOrderRequest,
    CreateOrderResult,
    VerifyPaymentRequest,
    VerifyPaymentResult,
    WebhookVerifyRequest,
    WebhookVerifyResult,
    PaymentStatus,
    GatewayError,
    GatewayConfigError,
    GatewayNetworkError,
    SignatureVerificationError,
)
from app.payments.factory import get_payment_gateway

__all__ = [
    "PaymentGateway",
    "CreateOrderRequest",
    "CreateOrderResult",
    "VerifyPaymentRequest",
    "VerifyPaymentResult",
    "WebhookVerifyRequest",
    "WebhookVerifyResult",
    "PaymentStatus",
    "GatewayError",
    "GatewayConfigError",
    "GatewayNetworkError",
    "SignatureVerificationError",
    "get_payment_gateway",
]
