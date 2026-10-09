"""
Payment provider webhook endpoints.

Routes
------
POST  /api/webhooks/razorpay   — Razorpay payment event webhook

Security design
---------------
- No player JWT or staff JWT required — webhooks come from Razorpay servers.
- Webhook signature is verified FIRST before any processing.
  A mismatched signature returns 400 immediately.
- Raw request body is read before FastAPI parses it (needed for HMAC check).
- ProviderEvent unique constraint provides final idempotency guard.
- No secret values are logged or returned in responses.
- Error responses intentionally minimal to avoid leaking internal state.
- Endpoint returns 200 for any "already processed" or "ignored" event so
  Razorpay does not retry indefinitely for non-error scenarios.

Razorpay retry behaviour
------------------------
Razorpay retries webhooks if it receives a non-2xx response.
We return:
  200  — processed, ignored, or already processed
  400  — invalid signature (Razorpay will NOT retry 400)
  500  — unexpected error (Razorpay will retry — acceptable for genuine failures)
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Header, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from fastapi import Depends

from app.services import deposit_service

logger = logging.getLogger("auraplayff.webhooks")

router = APIRouter(prefix="/api/webhooks", tags=["Webhooks"])


@router.post(
    "/razorpay",
    status_code=200,
    summary="Razorpay payment webhook",
    include_in_schema=False,  # Don't expose in public API docs
)
async def razorpay_webhook(
    request: Request,
    x_razorpay_signature: str = Header(
        ...,
        alias="X-Razorpay-Signature",
        description="HMAC-SHA256 signature sent by Razorpay",
    ),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """
    Receive and process a Razorpay webhook event.

    Processing order:
    1. Read raw body (required for HMAC verification).
    2. Verify webhook signature against RAZORPAY_WEBHOOK_SECRET.
    3. Insert ProviderEvent record (idempotency guard).
    4. Process event (payment.captured → credit wallet; payment.failed → mark failed).
    5. Return 200.

    Returns 400 for invalid signature.
    Returns 200 (with status=already_processed) for duplicate webhooks.
    """
    # Read raw body before any JSON parsing — required for HMAC check
    raw_body: bytes = await request.body()

    if not raw_body:
        logger.warning("Razorpay webhook received empty body")
        return JSONResponse(
            status_code=400,
            content={"error": "Empty request body"},
        )

    try:
        async with db.begin():
            result = await deposit_service.process_webhook_event(
                db,
                raw_body=raw_body,
                signature_header=x_razorpay_signature,
            )
        return JSONResponse(status_code=200, content=result)

    except Exception as exc:
        # Check if it's our AppError for invalid signature
        from app.core.exceptions import AppError
        if isinstance(exc, AppError) and exc.code in (
            "webhook_signature_invalid",
            "webhook_parse_error",
        ):
            # Do not log full stack trace for signature failures —
            # they may be probes / misconfigured clients
            logger.warning(
                "Webhook rejected: code=%s detail=%s",
                exc.code,
                exc.message,
            )
            return JSONResponse(
                status_code=400,
                content={"error": "Invalid webhook"},  # Intentionally vague
            )

        # Unexpected error — log fully, return 500 so Razorpay retries
        logger.exception(
            "Unexpected error processing Razorpay webhook: %s", exc
        )
        return JSONResponse(
            status_code=500,
            content={"error": "Internal error"},
        )
