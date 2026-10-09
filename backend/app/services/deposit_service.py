"""
Deposit service — orchestrates all deposit flows.

Responsibilities
----------------
1. validate_and_create_online_deposit()
   - Validates amount against platform settings.
   - Creates internal Deposit record (status=PENDING).
   - Calls payment gateway to create a provider order.
   - Returns data needed for frontend Razorpay checkout.
   - Does NOT credit wallet.

2. verify_online_payment()
   - Called after client reports payment success.
   - Verifies HMAC signature server-side.
   - Looks up the internal deposit by provider_order_id.
   - Checks amount + currency match.
   - Credits wallet atomically.
   - Idempotent: safe to call twice (second call is a no-op).

3. process_webhook_event()
   - Called by the Razorpay webhook endpoint.
   - Verifies webhook signature.
   - Inserts ProviderEvent row for idempotency guard.
   - Processes payment.captured → credit wallet.
   - Ignores already-processed events safely.

4. create_manual_deposit()
   - Creates Deposit record (status=PENDING_MANUAL_VERIFICATION).
   - Returns UPI display info from platform settings.

5. submit_utr()
   - Player submits their UTR for an existing manual deposit.
   - Validates UTR uniqueness (no double-acceptance across deposits).
   - Does NOT credit wallet.

6. get_platform_upi_info()
   - Returns UPI ID + instructions from PlatformSetting.

Admin flows (called by admin router):
7. approve_manual_deposit()   — credits wallet, marks APPROVED.
8. reject_manual_deposit()    — marks REJECTED, no wallet change.

Financial safety rules
----------------------
- Wallet credit happens ONCE inside the same DB transaction that changes
  deposit.status to PAID or APPROVED.
- wallet_tx_id FK prevents a second credit: if wallet_tx_id is already set,
  the deposit is already paid.
- provider_payment_id has a UNIQUE constraint preventing double-webhook credit.
- UTR uniqueness is enforced at the service layer (approved deposits only).
- All amounts are Decimal(12,2). No floats.
"""
from __future__ import annotations

import logging
import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppError, ConflictError, NotFoundError
from app.models._types import utcnow
from app.models.audit_log import ActorType
from app.models.deposit import Deposit, DepositMethod, DepositStatus
from app.models.platform_setting import (
    PlatformSetting,
    SETTING_MIN_DEPOSIT_AMOUNT,
    SETTING_MAX_DEPOSIT_AMOUNT,
    SETTING_UPI_ID,
    SETTING_UPI_DISPLAY_NAME,
    SETTING_UPI_ACCOUNT_NAME,
    SETTING_MANUAL_DEPOSIT_INSTRUCTIONS,
    SETTING_QR_STORAGE_REF,
    SETTING_MANUAL_PAYMENT_ACTIVE,
    SETTING_DEFAULTS,
)
from app.models.provider_event import ProviderEvent, ProviderEventStatus
from app.payments.base import (
    CreateOrderRequest,
    GatewayError,
    PaymentStatus,
    SignatureVerificationError,
    VerifyPaymentRequest,
    WebhookVerifyRequest,
)
from app.payments.factory import get_payment_gateway
from app.services import audit_service
from app.services import wallet_service

logger = logging.getLogger("auraplayff.deposit")

# ── Custom exceptions ─────────────────────────────────────────────────────────

class DepositNotFoundError(AppError):
    def __init__(self) -> None:
        super().__init__("Deposit not found.", status_code=404, code="deposit_not_found")

class DepositAlreadyProcessedError(AppError):
    def __init__(self) -> None:
        super().__init__(
            "This deposit has already been processed.",
            status_code=409,
            code="deposit_already_processed",
        )

class DepositAmountMismatchError(AppError):
    def __init__(self) -> None:
        super().__init__(
            "Payment amount does not match the deposit request.",
            status_code=409,
            code="deposit_amount_mismatch",
        )

class DepositBelowMinimumError(AppError):
    def __init__(self, minimum: str) -> None:
        super().__init__(
            f"Deposit amount is below the minimum of ₹{minimum}.",
            status_code=422,
            code="deposit_below_minimum",
        )

class DepositAboveMaximumError(AppError):
    def __init__(self, maximum: str) -> None:
        super().__init__(
            f"Deposit amount exceeds the maximum of ₹{maximum}.",
            status_code=422,
            code="deposit_above_maximum",
        )

class UTRAlreadyUsedError(AppError):
    def __init__(self) -> None:
        super().__init__(
            "This UTR has already been submitted for another deposit.",
            status_code=409,
            code="utr_already_used",
        )

class UTRAlreadySubmittedError(AppError):
    def __init__(self) -> None:
        super().__init__(
            "A UTR has already been submitted for this deposit.",
            status_code=409,
            code="utr_already_submitted",
        )

class DepositNotPendingManualError(AppError):
    def __init__(self, status: str) -> None:
        super().__init__(
            f"Deposit cannot be actioned in its current status: {status}.",
            status_code=409,
            code="deposit_not_pending_manual",
        )

class GatewayUnavailableError(AppError):
    def __init__(self, detail: str = "Payment gateway is unavailable.") -> None:
        super().__init__(detail, status_code=503, code="gateway_unavailable")


# ── Platform setting helpers ──────────────────────────────────────────────────

async def _get_setting(db: AsyncSession, key: str) -> str:
    """Fetch a platform setting value. Falls back to hardcoded default."""
    result = await db.execute(
        select(PlatformSetting.value).where(PlatformSetting.key == key)
    )
    row = result.scalar_one_or_none()
    if row is not None:
        return row
    return SETTING_DEFAULTS.get(key, "")


async def _get_decimal_setting(db: AsyncSession, key: str) -> Decimal:
    value = await _get_setting(db, key)
    try:
        return Decimal(value)
    except Exception:
        return Decimal(SETTING_DEFAULTS.get(key, "0"))


# ── Public API ────────────────────────────────────────────────────────────────

async def validate_and_create_online_deposit(
    db: AsyncSession,
    user_id: uuid.UUID,
    amount: Decimal,
    *,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> dict[str, Any]:
    """
    Step 1 of online deposit flow.

    1. Validate amount vs platform limits.
    2. Create internal Deposit row (status=PENDING).
    3. Create Razorpay order via gateway adapter.
    4. Store provider_order_id on the deposit.
    5. Return data for frontend checkout.

    Does NOT credit wallet. Wallet is credited only after verify_online_payment()
    or process_webhook_event() confirms capture.
    """
    from app.config import get_settings
    settings = get_settings()

    min_amount = await _get_decimal_setting(db, SETTING_MIN_DEPOSIT_AMOUNT)
    max_amount = await _get_decimal_setting(db, SETTING_MAX_DEPOSIT_AMOUNT)

    if amount < min_amount:
        raise DepositBelowMinimumError(str(min_amount))
    if amount > max_amount:
        raise DepositAboveMaximumError(str(max_amount))

    # Create internal deposit record first
    deposit = Deposit(
        user_id=user_id,
        method=DepositMethod.online,
        status=DepositStatus.pending,
        amount=amount,
        currency="INR",
        provider="razorpay",
    )
    db.add(deposit)
    await db.flush([deposit])  # Get deposit.id before calling gateway

    # Call gateway to create order
    try:
        gateway = get_payment_gateway()
        order_result = await gateway.create_order(
            CreateOrderRequest(
                amount=amount,
                currency="INR",
                receipt=str(deposit.id)[:40],
                notes={"deposit_id": str(deposit.id), "user_id": str(user_id)},
            )
        )
    except GatewayError as exc:
        # Roll back the deposit row on gateway failure
        await db.rollback()
        logger.error("Gateway error creating order for user=%s: %s", user_id, exc)
        raise GatewayUnavailableError(str(exc)) from exc

    # Store provider order ID
    deposit.provider_order_id = order_result.provider_order_id
    deposit.meta = {"razorpay_order_status": order_result.status.value}
    deposit.updated_at = utcnow()
    await db.flush([deposit])

    await audit_service.log(
        db=db,
        actor_type=ActorType.user,
        actor_id=str(user_id),
        action="deposit.online_initiated",
        entity_type="deposit",
        entity_id=str(deposit.id),
        new_value={
            "amount": str(amount),
            "provider_order_id": order_result.provider_order_id,
            "status": DepositStatus.pending.value,
        },
        ip_address=ip_address,
        user_agent=user_agent,
    )

    return {
        "deposit_id": deposit.id,
        "razorpay_key_id": settings.razorpay_key_id,  # Public key only
        "razorpay_order_id": order_result.provider_order_id,
        "amount": str(amount),
        "currency": "INR",
        "description": "AuraPlayFF Deposit",
    }


async def verify_online_payment(
    db: AsyncSession,
    user_id: uuid.UUID,
    deposit_id: uuid.UUID,
    razorpay_order_id: str,
    razorpay_payment_id: str,
    razorpay_signature: str,
    *,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> Deposit:
    """
    Step 2 of online deposit — client-side verification trigger.

    Verifies HMAC signature. Credits wallet if valid.
    Idempotent: if already PAID, returns deposit without re-crediting.

    Security: never trusts client-supplied amount — uses deposit record amount.
    """
    # Load deposit and confirm ownership
    result = await db.execute(
        select(Deposit).where(
            Deposit.id == deposit_id,
            Deposit.user_id == user_id,
        )
    )
    deposit: Deposit | None = result.scalar_one_or_none()
    if deposit is None:
        raise DepositNotFoundError()

    # Idempotency: already paid
    if deposit.status == DepositStatus.paid:
        return deposit

    # Must be pending online deposit
    if deposit.status not in (DepositStatus.pending,):
        raise DepositAlreadyProcessedError()

    # Verify provider_order_id matches what we stored
    if deposit.provider_order_id != razorpay_order_id:
        raise DepositNotFoundError()

    # Verify HMAC signature
    try:
        gateway = get_payment_gateway()
        verify_result = gateway.verify_payment(
            VerifyPaymentRequest(
                provider_order_id=razorpay_order_id,
                provider_payment_id=razorpay_payment_id,
                provider_signature=razorpay_signature,
                expected_amount=deposit.amount,
            )
        )
    except SignatureVerificationError as exc:
        logger.warning(
            "Payment signature verification failed: deposit=%s user=%s",
            deposit_id, user_id,
        )
        await audit_service.log(
            db=db,
            actor_type=ActorType.user,
            actor_id=str(user_id),
            action="deposit.signature_failed",
            entity_type="deposit",
            entity_id=str(deposit.id),
            new_value={"reason": "signature_mismatch"},
            ip_address=ip_address,
        )
        raise AppError(
            "payment_signature_invalid",
            "Payment verification failed. Please contact support if money was deducted.",
            400,
        ) from exc

    if not verify_result.is_valid:
        raise AppError(
            "payment_verification_failed",
            "Payment could not be verified.",
            400,
        )

    # Credit wallet atomically
    deposit = await _credit_deposit_and_mark_paid(
        db=db,
        deposit=deposit,
        provider_payment_id=razorpay_payment_id,
        triggered_by="client_verify",
        actor_id=str(user_id),
        actor_type=ActorType.user,
        ip_address=ip_address,
        user_agent=user_agent,
    )
    return deposit


async def process_webhook_event(
    db: AsyncSession,
    raw_body: bytes,
    signature_header: str,
) -> dict[str, str]:
    """
    Process an inbound Razorpay webhook.

    1. Verify webhook signature.
    2. Insert ProviderEvent for idempotency (UNIQUE on provider+event_id).
    3. If event_type == "payment.captured": credit wallet and mark deposit PAID.
    4. If event_type == "payment.failed": mark deposit FAILED.
    5. Return {"status": "processed"} or {"status": "ignored"}.

    Idempotency: a duplicate webhook returns {"status": "already_processed"}
    without touching the wallet.
    """
    # Verify webhook signature
    try:
        gateway = get_payment_gateway()
        webhook_result = gateway.verify_webhook(
            WebhookVerifyRequest(
                raw_body=raw_body,
                signature_header=signature_header,
                event_id="",
            )
        )
    except SignatureVerificationError:
        logger.warning("Webhook signature verification failed")
        raise AppError(
            "webhook_signature_invalid",
            "Invalid webhook signature.",
            400,
        )
    except GatewayError as exc:
        raise AppError("webhook_parse_error", str(exc), 400)

    event_id = webhook_result.event_id or f"rzp_{webhook_result.provider_order_id}"

    # Idempotency guard: try to insert ProviderEvent
    existing = await db.execute(
        select(ProviderEvent).where(
            ProviderEvent.provider == "razorpay",
            ProviderEvent.event_id == event_id,
        )
    )
    if existing.scalar_one_or_none() is not None:
        logger.info("Webhook already processed: event_id=%s", event_id)
        return {"status": "already_processed"}

    # Create ProviderEvent record (status=received initially)
    # Safe subset of payload — never store secrets
    safe_payload: dict[str, Any] = {
        "event_type": webhook_result.event_type,
        "provider_order_id": webhook_result.provider_order_id,
        "provider_payment_id": webhook_result.provider_payment_id,
        "amount": str(webhook_result.amount),
        "currency": webhook_result.currency,
        "status": webhook_result.status.value,
    }

    provider_event = ProviderEvent(
        provider="razorpay",
        event_id=event_id,
        event_type=webhook_result.event_type,
        processing_status=ProviderEventStatus.received,
        raw_event=safe_payload,
    )
    db.add(provider_event)

    try:
        await db.flush([provider_event])
    except IntegrityError:
        # Race condition: another request inserted same event_id simultaneously
        await db.rollback()
        logger.info("Webhook race condition resolved — already processed: %s", event_id)
        return {"status": "already_processed"}

    # Find the related deposit
    if not webhook_result.provider_order_id:
        provider_event.processing_status = ProviderEventStatus.ignored
        provider_event.error_detail = "No order_id in webhook"
        await db.flush([provider_event])
        return {"status": "ignored"}

    dep_result = await db.execute(
        select(Deposit).where(
            Deposit.provider_order_id == webhook_result.provider_order_id,
            Deposit.method == DepositMethod.online,
        )
    )
    deposit: Deposit | None = dep_result.scalar_one_or_none()

    if deposit is None:
        provider_event.processing_status = ProviderEventStatus.ignored
        provider_event.error_detail = (
            f"No deposit for order_id={webhook_result.provider_order_id}"
        )
        await db.flush([provider_event])
        logger.warning(
            "Webhook for unknown order: %s", webhook_result.provider_order_id
        )
        return {"status": "ignored"}

    # Link event to deposit
    provider_event.deposit_id = deposit.id

    event_type = webhook_result.event_type.lower()

    if event_type == "payment.captured":
        if deposit.status == DepositStatus.paid:
            # Already paid (client verify may have run first)
            provider_event.processing_status = ProviderEventStatus.ignored
            provider_event.error_detail = "Deposit already paid (client verify ran first)"
            await db.flush([provider_event])
            return {"status": "ignored"}

        # Credit wallet
        await _credit_deposit_and_mark_paid(
            db=db,
            deposit=deposit,
            provider_payment_id=webhook_result.provider_payment_id,
            triggered_by="webhook",
            actor_id="system",
            actor_type=ActorType.system,
        )
        provider_event.processing_status = ProviderEventStatus.processed
        await db.flush([provider_event])
        return {"status": "processed"}

    elif event_type == "payment.failed":
        if deposit.status not in DepositStatus.terminal_states():
            deposit.status = DepositStatus.failed
            deposit.updated_at = utcnow()
            await db.flush([deposit])
        provider_event.processing_status = ProviderEventStatus.processed
        await db.flush([provider_event])
        return {"status": "processed"}

    else:
        # Event type we don't act on (e.g. order.paid, refund.processed)
        provider_event.processing_status = ProviderEventStatus.ignored
        await db.flush([provider_event])
        return {"status": "ignored"}


async def create_manual_deposit(
    db: AsyncSession,
    user_id: uuid.UUID,
    amount: Decimal,
    *,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> tuple[Deposit, dict[str, str]]:
    """
    Create a manual UPI deposit request.

    Returns (deposit, upi_info) where upi_info contains display information
    for the frontend payment screen (UPI ID, instructions, QR ref).

    Does NOT credit wallet. Admin must approve via approve_manual_deposit().
    """
    min_amount = await _get_decimal_setting(db, SETTING_MIN_DEPOSIT_AMOUNT)
    max_amount = await _get_decimal_setting(db, SETTING_MAX_DEPOSIT_AMOUNT)

    if amount < min_amount:
        raise DepositBelowMinimumError(str(min_amount))
    if amount > max_amount:
        raise DepositAboveMaximumError(str(max_amount))

    deposit = Deposit(
        user_id=user_id,
        method=DepositMethod.manual,
        status=DepositStatus.pending_manual_verification,
        amount=amount,
        currency="INR",
        provider=None,
    )
    db.add(deposit)
    await db.flush([deposit])

    await audit_service.log(
        db=db,
        actor_type=ActorType.user,
        actor_id=str(user_id),
        action="deposit.manual_initiated",
        entity_type="deposit",
        entity_id=str(deposit.id),
        new_value={"amount": str(amount), "status": deposit.status.value},
        ip_address=ip_address,
        user_agent=user_agent,
    )

    upi_info = await get_platform_upi_info(db)
    return deposit, upi_info


async def submit_utr(
    db: AsyncSession,
    user_id: uuid.UUID,
    deposit_id: uuid.UUID,
    utr: str,
    payer_info: str | None = None,
    *,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> Deposit:
    """
    Player submits UTR after making the UPI payment.

    Validates:
    - Deposit belongs to user and is in pending_manual_verification status.
    - UTR not already submitted on this deposit.
    - Normalised UTR not already approved on another deposit.

    Does NOT credit wallet.
    """
    result = await db.execute(
        select(Deposit).where(
            Deposit.id == deposit_id,
            Deposit.user_id == user_id,
        )
    )
    deposit: Deposit | None = result.scalar_one_or_none()
    if deposit is None:
        raise DepositNotFoundError()

    if deposit.status != DepositStatus.pending_manual_verification:
        raise DepositNotPendingManualError(deposit.status.value)

    if deposit.utr is not None:
        raise UTRAlreadySubmittedError()

    utr_normalized = utr.strip().upper()

    # Check if this UTR is already approved on another deposit
    conflict = await db.execute(
        select(Deposit.id).where(
            Deposit.utr_normalized == utr_normalized,
            Deposit.status == DepositStatus.approved,
            Deposit.id != deposit_id,
        )
    )
    if conflict.scalar_one_or_none() is not None:
        raise UTRAlreadyUsedError()

    # Also block if UTR is pending review on another deposit (prevent dual-submission)
    conflict2 = await db.execute(
        select(Deposit.id).where(
            Deposit.utr_normalized == utr_normalized,
            Deposit.status == DepositStatus.pending_manual_verification,
            Deposit.id != deposit_id,
        )
    )
    if conflict2.scalar_one_or_none() is not None:
        raise UTRAlreadyUsedError()

    deposit.utr = utr
    deposit.utr_normalized = utr_normalized
    deposit.payer_info = payer_info
    deposit.updated_at = utcnow()
    await db.flush([deposit])

    await audit_service.log(
        db=db,
        actor_type=ActorType.user,
        actor_id=str(user_id),
        action="deposit.utr_submitted",
        entity_type="deposit",
        entity_id=str(deposit.id),
        new_value={"utr": utr_normalized},
        ip_address=ip_address,
        user_agent=user_agent,
    )

    return deposit


async def approve_manual_deposit(
    db: AsyncSession,
    deposit_id: uuid.UUID,
    staff_id: uuid.UUID,
    *,
    note: str | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> Deposit:
    """
    Admin approves a manual deposit.

    Atomically:
    1. Validates deposit is in pending_manual_verification and has a UTR.
    2. Checks UTR uniqueness (no other approved deposit with same UTR).
    3. Credits player's deposit_balance via wallet_service.
    4. Sets deposit.status = APPROVED, wallet_tx_id = tx.id.
    5. Records audit log.

    Idempotency: if wallet_tx_id already set, returns deposit without re-crediting.
    """
    result = await db.execute(
        select(Deposit).where(Deposit.id == deposit_id)
    )
    deposit: Deposit | None = result.scalar_one_or_none()
    if deposit is None:
        raise DepositNotFoundError()

    # Idempotency guard
    if deposit.status == DepositStatus.approved:
        return deposit

    if deposit.status != DepositStatus.pending_manual_verification:
        raise DepositNotPendingManualError(deposit.status.value)

    if not deposit.utr_normalized:
        raise AppError(
            "deposit_no_utr",
            "Deposit cannot be approved: player has not submitted a UTR yet.",
            422,
        )

    # Final UTR uniqueness check before crediting
    conflict = await db.execute(
        select(Deposit.id).where(
            Deposit.utr_normalized == deposit.utr_normalized,
            Deposit.status == DepositStatus.approved,
            Deposit.id != deposit_id,
        )
    )
    if conflict.scalar_one_or_none() is not None:
        raise UTRAlreadyUsedError()

    # Credit wallet (idempotency_key = deposit:<deposit_id>:approve)
    idempotency_key = f"deposit:{deposit_id}:approve"
    tx = await wallet_service.credit_deposit_balance(
        db,
        user_id=deposit.user_id,
        amount=deposit.amount,
        idempotency_key=idempotency_key,
        reference_id=deposit.id,
        reference_type="deposit",
        description=f"Manual UPI deposit approved — UTR: {deposit.utr}",
        meta={"deposit_id": str(deposit_id), "utr": deposit.utr},
        created_by_id=staff_id,
    )

    # Update deposit record in same transaction
    deposit.status = DepositStatus.approved
    deposit.wallet_tx_id = tx.id
    deposit.verified_by_id = staff_id
    deposit.verified_at = utcnow()
    deposit.admin_note = note
    deposit.updated_at = utcnow()
    await db.flush([deposit])

    await audit_service.log(
        db=db,
        actor_type=ActorType.staff,
        actor_id=str(staff_id),
        action="deposit.manual_approved",
        entity_type="deposit",
        entity_id=str(deposit.id),
        old_value={"status": "pending_manual_verification"},
        new_value={
            "status": "approved",
            "amount": str(deposit.amount),
            "utr": deposit.utr,
            "wallet_tx_id": str(tx.id),
        },
        ip_address=ip_address,
        user_agent=user_agent,
    )

    return deposit


async def reject_manual_deposit(
    db: AsyncSession,
    deposit_id: uuid.UUID,
    staff_id: uuid.UUID,
    *,
    reason: str,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> Deposit:
    """
    Admin rejects a manual deposit.

    No wallet changes. Records reason in admin_note.
    """
    result = await db.execute(
        select(Deposit).where(Deposit.id == deposit_id)
    )
    deposit: Deposit | None = result.scalar_one_or_none()
    if deposit is None:
        raise DepositNotFoundError()

    if deposit.status == DepositStatus.rejected:
        return deposit

    if deposit.status not in (
        DepositStatus.pending_manual_verification,
    ):
        raise DepositNotPendingManualError(deposit.status.value)

    deposit.status = DepositStatus.rejected
    deposit.admin_note = reason
    deposit.verified_by_id = staff_id
    deposit.verified_at = utcnow()
    deposit.updated_at = utcnow()
    await db.flush([deposit])

    await audit_service.log(
        db=db,
        actor_type=ActorType.staff,
        actor_id=str(staff_id),
        action="deposit.manual_rejected",
        entity_type="deposit",
        entity_id=str(deposit.id),
        old_value={"status": "pending_manual_verification"},
        new_value={"status": "rejected", "reason": reason},
        ip_address=ip_address,
        user_agent=user_agent,
    )

    return deposit


async def get_platform_upi_info(db: AsyncSession) -> dict[str, str]:
    """Return UPI display info (safe for player-facing API)."""
    return {
        "upi_id": await _get_setting(db, SETTING_UPI_ID),
        "upi_display_name": await _get_setting(db, SETTING_UPI_DISPLAY_NAME),
        "upi_account_name": await _get_setting(db, SETTING_UPI_ACCOUNT_NAME),
        "instructions": await _get_setting(db, SETTING_MANUAL_DEPOSIT_INSTRUCTIONS),
        "qr_ref": await _get_setting(db, SETTING_QR_STORAGE_REF),
    }


async def get_public_payment_config(db: AsyncSession) -> dict:
    """
    Return the publicly-safe payment configuration for the player deposit page.

    Exposes ONLY:
      - manual payment active/inactive flag
      - UPI ID, account name, display name, instructions, QR ref
      (when active)

    Never exposes internal settings, Razorpay credentials, or admin-only fields.
    Used by GET /api/deposits/payment-config.
    """
    active_raw = await _get_setting(db, SETTING_MANUAL_PAYMENT_ACTIVE)
    is_active = active_raw.lower() == "true"

    config: dict = {"manual_payment_active": is_active}

    if is_active:
        config["upi_id"] = await _get_setting(db, SETTING_UPI_ID)
        config["upi_display_name"] = await _get_setting(db, SETTING_UPI_DISPLAY_NAME)
        config["upi_account_name"] = await _get_setting(db, SETTING_UPI_ACCOUNT_NAME)
        config["instructions"] = await _get_setting(db, SETTING_MANUAL_DEPOSIT_INSTRUCTIONS)
        config["qr_ref"] = await _get_setting(db, SETTING_QR_STORAGE_REF)
    else:
        config["upi_id"] = ""
        config["upi_display_name"] = ""
        config["upi_account_name"] = ""
        config["instructions"] = ""
        config["qr_ref"] = ""

    return config


async def get_deposit_for_user(
    db: AsyncSession,
    deposit_id: uuid.UUID,
    user_id: uuid.UUID,
) -> Deposit:
    """Fetch a single deposit, enforcing ownership."""
    result = await db.execute(
        select(Deposit).where(
            Deposit.id == deposit_id,
            Deposit.user_id == user_id,
        )
    )
    deposit = result.scalar_one_or_none()
    if deposit is None:
        raise DepositNotFoundError()
    return deposit


async def list_deposits_for_user(
    db: AsyncSession,
    user_id: uuid.UUID,
    *,
    limit: int = 20,
    offset: int = 0,
) -> tuple[list[Deposit], int]:
    """Return paginated deposits for a player (own only)."""
    q = select(Deposit).where(Deposit.user_id == user_id)
    count_result = await db.execute(
        select(func.count()).select_from(q.subquery())
    )
    total = count_result.scalar_one()

    rows = await db.execute(
        q.order_by(Deposit.created_at.desc()).limit(limit).offset(offset)
    )
    return list(rows.scalars().all()), total


async def list_pending_manual_deposits(
    db: AsyncSession,
    *,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[Deposit], int]:
    """Return pending manual deposits for admin review."""
    from sqlalchemy.orm import selectinload
    q = select(Deposit).where(
        Deposit.method == DepositMethod.manual,
        Deposit.status == DepositStatus.pending_manual_verification,
    )
    count_result = await db.execute(
        select(func.count()).select_from(q.subquery())
    )
    total = count_result.scalar_one()

    rows = await db.execute(
        q.options(selectinload(Deposit.user))
        .order_by(Deposit.created_at.asc()).limit(limit).offset(offset)
    )
    return list(rows.scalars().all()), total


async def admin_list_deposits(
    db: AsyncSession,
    *,
    status: str | None = None,
    method: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[Deposit], int]:
    """Admin: list deposits with optional status/method filter."""
    from sqlalchemy.orm import selectinload
    q = select(Deposit)
    if status:
        q = q.where(Deposit.status == status)
    if method:
        q = q.where(Deposit.method == method)

    count_result = await db.execute(
        select(func.count()).select_from(q.subquery())
    )
    total = count_result.scalar_one()

    rows = await db.execute(
        q.options(selectinload(Deposit.user))
        .order_by(Deposit.created_at.desc()).limit(limit).offset(offset)
    )
    return list(rows.scalars().all()), total


async def admin_get_deposit(db: AsyncSession, deposit_id: uuid.UUID) -> Deposit:
    """Admin: fetch any deposit by ID, with user eagerly loaded."""
    from sqlalchemy.orm import selectinload
    result = await db.execute(
        select(Deposit)
        .options(selectinload(Deposit.user))
        .where(Deposit.id == deposit_id)
    )
    deposit = result.scalar_one_or_none()
    if deposit is None:
        raise DepositNotFoundError()
    return deposit


# ── Private helpers ───────────────────────────────────────────────────────────

async def _credit_deposit_and_mark_paid(
    db: AsyncSession,
    deposit: Deposit,
    provider_payment_id: str,
    *,
    triggered_by: str,
    actor_id: str,
    actor_type: ActorType,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> Deposit:
    """
    Internal: credit wallet and mark deposit as PAID atomically.
    Called by both verify_online_payment() and process_webhook_event().

    Guard: if wallet_tx_id is already set on the deposit, this is a no-op —
    the wallet was already credited (prevents double-credit on race conditions).
    """
    # Double-credit guard
    if deposit.wallet_tx_id is not None:
        return deposit

    idempotency_key = f"deposit:{deposit.id}:paid"

    tx = await wallet_service.credit_deposit_balance(
        db,
        user_id=deposit.user_id,
        amount=deposit.amount,
        idempotency_key=idempotency_key,
        reference_id=deposit.id,
        reference_type="deposit",
        description=f"Online deposit via Razorpay — payment_id: {provider_payment_id}",
        meta={
            "deposit_id": str(deposit.id),
            "provider_payment_id": provider_payment_id,
            "triggered_by": triggered_by,
        },
    )

    deposit.status = DepositStatus.paid
    deposit.provider_payment_id = provider_payment_id
    deposit.wallet_tx_id = tx.id
    deposit.updated_at = utcnow()
    await db.flush([deposit])

    await audit_service.log(
        db=db,
        actor_type=actor_type,
        actor_id=actor_id,
        action="deposit.online_paid",
        entity_type="deposit",
        entity_id=str(deposit.id),
        old_value={"status": "pending"},
        new_value={
            "status": "paid",
            "amount": str(deposit.amount),
            "provider_payment_id": provider_payment_id,
            "triggered_by": triggered_by,
            "wallet_tx_id": str(tx.id),
        },
        ip_address=ip_address,
        user_agent=user_agent,
    )

    logger.info(
        "Deposit credited: deposit_id=%s user=%s amount=%s triggered_by=%s",
        deposit.id,
        deposit.user_id,
        deposit.amount,
        triggered_by,
    )
    return deposit
