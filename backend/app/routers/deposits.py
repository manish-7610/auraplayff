"""
Player-facing deposit endpoints.

Routes
------
POST   /api/deposits/online                    — initiate Razorpay deposit
POST   /api/deposits/online/{id}/verify        — client-side payment verification
POST   /api/deposits/manual                    — create manual UPI deposit
POST   /api/deposits/manual/{id}/utr           — submit UTR for manual deposit
GET    /api/deposits/payment-config            — get player-facing payment config
GET    /api/deposits/qr-image                  — proxy QR image (private storage, player-safe)
GET    /api/deposits/upi-info                  — get platform UPI display info
GET    /api/deposits/{id}                      — get single deposit (own)
GET    /api/deposits                           — list own deposits

Security:
  - All routes require authenticated player (get_current_user).
  - Ownership enforced in service layer: user can only see/act on own deposits.
  - Status changes are never accepted from players.
  - Wallet credit never happens from these endpoints alone — server verifies.
  - The qr-image endpoint fetches from private Supabase storage server-side;
    the service-role key and bucket details are never sent to the browser.

Transaction pattern:
  Do NOT use `async with db.begin()` here.  The get_current_user dependency
  executes a SELECT on the shared session before the endpoint body runs,
  which causes SQLAlchemy to begin an implicit transaction automatically.
  Calling db.begin() again raises:
      InvalidRequestError: A transaction is already begun on this Session.
  Pattern used: call service functions directly on db, then await db.commit()
  for mutating operations.  Read-only endpoints need no explicit commit.
"""
from __future__ import annotations

import logging
import uuid
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_current_user, get_db, get_client_ip
from app.models.user import User
from app.schemas.deposit import (
    AdminDepositResponse,
    CreateManualDepositRequest,
    CreateOnlineDepositRequest,
    DepositListResponse,
    DepositResponse,
    OnlineDepositInitResponse,
    SubmitUTRRequest,
    VerifyOnlinePaymentRequest,
)
from app.services import deposit_service

router = APIRouter(prefix="/api/deposits", tags=["Deposits — Player"])


# ── Online deposit ─────────────────────────────────────────────────────────────

@router.post(
    "/online",
    response_model=OnlineDepositInitResponse,
    status_code=201,
    summary="Initiate Razorpay deposit",
)
async def create_online_deposit(
    body: CreateOnlineDepositRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> OnlineDepositInitResponse:
    """
    Create a Razorpay order for a deposit.
    Returns the Razorpay key_id and order_id needed by the frontend checkout.
    Does NOT credit wallet — wallet is credited only after payment is verified.
    """
    data = await deposit_service.validate_and_create_online_deposit(
        db,
        user_id=current_user.id,
        amount=body.amount,
        ip_address=get_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    await db.commit()
    return OnlineDepositInitResponse(**data)


@router.post(
    "/online/{deposit_id}/verify",
    response_model=DepositResponse,
    summary="Verify Razorpay payment (client callback)",
)
async def verify_online_payment(
    deposit_id: uuid.UUID,
    body: VerifyOnlinePaymentRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> DepositResponse:
    """
    Called after Razorpay checkout succeeds on the client.
    Server re-verifies HMAC signature. Credits wallet only if valid.
    Idempotent — safe to call twice (second call returns existing PAID deposit).
    """
    deposit = await deposit_service.verify_online_payment(
        db,
        user_id=current_user.id,
        deposit_id=deposit_id,
        razorpay_order_id=body.razorpay_order_id,
        razorpay_payment_id=body.razorpay_payment_id,
        razorpay_signature=body.razorpay_signature,
        ip_address=get_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    await db.commit()
    return DepositResponse.from_deposit(deposit)


# ── Manual UPI deposit ────────────────────────────────────────────────────────

@router.post(
    "/manual",
    response_model=dict,
    status_code=201,
    summary="Create manual UPI deposit request",
)
async def create_manual_deposit(
    body: CreateManualDepositRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Create a manual UPI deposit request.
    Returns deposit details + UPI display info for the payment screen.
    Does NOT credit wallet — admin must approve after verifying UTR.
    """
    deposit, upi_info = await deposit_service.create_manual_deposit(
        db,
        user_id=current_user.id,
        amount=body.amount,
        ip_address=get_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    await db.commit()

    # Phase 7: notify player and admins (best-effort, non-fatal)
    from app.services import notification_service as ns
    try:
        await ns.notify_player_deposit_manual_submitted(
            db, user_id=current_user.id,
            deposit_id=deposit.id, amount=str(deposit.amount),
        )
        await ns.notify_admin_deposit_pending(
            db, deposit_id=deposit.id,
            user_id=current_user.id, amount=str(deposit.amount),
        )
        await db.commit()
    except Exception:
        pass

    return {
        "deposit": DepositResponse.from_deposit(deposit).model_dump(),
        "upi_info": upi_info,
    }


@router.post(
    "/manual/{deposit_id}/utr",
    response_model=DepositResponse,
    summary="Submit UTR for manual deposit",
)
async def submit_utr(
    deposit_id: uuid.UUID,
    body: SubmitUTRRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> DepositResponse:
    """
    Player submits their UTR/transaction ID after making the UPI transfer.
    Submission does NOT credit wallet — admin must approve.
    """
    deposit = await deposit_service.submit_utr(
        db,
        user_id=current_user.id,
        deposit_id=deposit_id,
        utr=body.utr,
        payer_info=body.payer_info,
        ip_address=get_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    await db.commit()
    return DepositResponse.from_deposit(deposit)


# ── UPI display info ──────────────────────────────────────────────────────────

@router.get(
    "/payment-config",
    response_model=dict,
    summary="Get active manual payment configuration for deposit page",
)
async def get_payment_config(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Return the player-facing payment configuration.

    Exposes ONLY safe, public fields:
      - manual_payment_active: whether manual UPI deposits are currently enabled
      - upi_id, upi_display_name, upi_account_name, instructions, qr_ref
        (populated only when active — empty strings when inactive)

    Never exposes internal settings, Razorpay credentials, or admin-only flags.
    Requires player authentication.
    """
    return await deposit_service.get_public_payment_config(db)


_logger = logging.getLogger("auraplayff.deposits.qr_proxy")

# ── QR image proxy ────────────────────────────────────────────────────────────

@router.get(
    "/qr-image",
    summary="Proxy the UPI QR code image for authenticated players",
    response_class=Response,
)
async def get_qr_image(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """
    Fetch the UPI QR code image from private Supabase storage and stream it
    directly to the authenticated player.

    Security model:
    - Requires valid player JWT — unauthenticated callers receive 401.
    - Only serves the image when manual_payment_active is true AND a QR image
      has been uploaded (qr_storage_ref is non-empty).
    - Fetches the raw bytes from Supabase using the service-role key server-side.
      The service-role key, bucket name, and Supabase URL are never forwarded
      to the browser.
    - Returns 404 when manual payment is inactive or no QR is configured.

    Response: binary image (image/jpeg or image/png) with
    Cache-Control: private, max-age=300.
    """
    from app.config import get_settings

    config = await deposit_service.get_public_payment_config(db)

    # Only serve when manual payment is active and a QR is configured
    if not config.get("manual_payment_active"):
        return Response(status_code=404, content="Manual payment is not active.")

    qr_ref: str = config.get("qr_ref", "")
    if not qr_ref:
        return Response(status_code=404, content="No QR image configured.")

    # Path guard — must be under payment-qr/ (server-generated paths only)
    if not qr_ref.startswith("payment-qr/"):
        _logger.warning("Unexpected qr_ref path format: %s", qr_ref)
        return Response(status_code=404, content="No QR image configured.")

    settings = get_settings()
    if not settings.supabase_url or not settings.supabase_service_role_key:
        return Response(status_code=503, content="Storage not configured.")

    bucket = settings.supabase_storage_bucket_screenshots
    fetch_url = (
        f"{settings.supabase_url.rstrip('/')}"
        f"/storage/v1/object/{bucket}/{qr_ref}"
    )
    headers = {
        "Authorization": f"Bearer {settings.supabase_service_role_key}",
        "apikey": settings.supabase_service_role_key,
    }

    try:
        import httpx
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(fetch_url, headers=headers)

        if resp.status_code == 404:
            return Response(status_code=404, content="QR image not found in storage.")
        if resp.status_code not in (200,):
            _logger.error("QR proxy fetch failed: %s %s", resp.status_code, resp.text[:200])
            return Response(status_code=502, content="Failed to fetch QR image.")

        # Determine content type from magic bytes
        img_bytes = resp.content
        content_type = "image/jpeg"
        if img_bytes[:8] == b"\x89PNG\r\n\x1a\n":
            content_type = "image/png"

        return Response(
            content=img_bytes,
            media_type=content_type,
            headers={
                "Cache-Control": "private, max-age=300",
                "X-Content-Type-Options": "nosniff",
            },
        )
    except Exception:
        _logger.exception("QR proxy exception for ref %s", qr_ref)
        return Response(status_code=502, content="Failed to fetch QR image.")


@router.get(
    "/upi-info",
    response_model=dict,
    summary="Get platform UPI payment information",
)
async def get_upi_info(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Return UPI ID and instructions for manual deposit screen.
    These values come from platform settings — never hardcoded.
    """
    return await deposit_service.get_platform_upi_info(db)


# ── Query endpoints ───────────────────────────────────────────────────────────

@router.get(
    "/{deposit_id}",
    response_model=DepositResponse,
    summary="Get a specific deposit",
)
async def get_deposit(
    deposit_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> DepositResponse:
    """Fetch a deposit by ID. Player can only view their own deposits."""
    deposit = await deposit_service.get_deposit_for_user(
        db, deposit_id, current_user.id
    )
    return DepositResponse.from_deposit(deposit)


@router.get(
    "",
    response_model=DepositListResponse,
    summary="List own deposits",
)
async def list_deposits(
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> DepositListResponse:
    """Return paginated list of the authenticated player's deposits."""
    deposits, total = await deposit_service.list_deposits_for_user(
        db,
        user_id=current_user.id,
        limit=limit,
        offset=offset,
    )
    return DepositListResponse(
        items=[DepositResponse.from_deposit(d) for d in deposits],
        total=total,
        limit=limit,
        offset=offset,
    )
