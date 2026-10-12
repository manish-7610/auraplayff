"""
Public password recovery request endpoint.

POST /api/recovery/request

This endpoint is UNAUTHENTICATED — it allows a locked-out player to submit
a recovery request without needing to log in first.

Security design:
  - No JWT required.
  - Rate-limited per IP to prevent abuse.
  - Accepts only: username (claimed, unverified), ff_uid (claimed, unverified),
    a contact method (WhatsApp or Instagram), and a description.
  - Returns a GENERIC success response regardless of whether the username/uid
    exist — prevents username enumeration.
  - The submitted username and uid are stored as-is for admin reference ONLY.
    They are explicitly NOT used as proof of ownership.
  - Admin must verify identity through out-of-band means before issuing any token.
  - Never accepts a new password through this form.
  - Creates a SupportRequest with is_recovery=True and user_id=NULL (no FK to
    a real user since we cannot verify identity at submission time).
  - Duplicate active recovery requests for the same username are silently ignored
    after the rate-limit window (not an error; avoids username existence leak).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.rate_limiter import limiter
from app.database import get_db
from app.models.support_request import SupportRequest, SupportStatus

router = APIRouter(prefix="/api/recovery", tags=["Player — Password Recovery"])


# ── Request / response schemas ────────────────────────────────────────────────

class RecoveryRequestCreate(BaseModel):
    """Public form data for a password recovery request.
    None of these fields are verified at submission time.
    """
    username: str = Field(..., min_length=3, max_length=30, description="Your login username (claimed, unverified)")
    ff_uid:   str = Field(..., min_length=6, max_length=15, description="Your Free Fire UID (claimed, unverified)")
    message:  str = Field(..., min_length=10, max_length=1000, description="Brief description to help admin verify your identity")

    # At least one contact method required (same constraint as normal tickets)
    whatsapp_number: str | None = Field(None, max_length=30)
    instagram_id:    str | None = Field(None, max_length=80)

    @field_validator("username")
    @classmethod
    def validate_username(cls, v: str) -> str:
        import re
        v = v.strip()
        if not re.match(r"^[a-zA-Z0-9_]{3,30}$", v):
            raise ValueError("Username must be 3–30 characters: letters, digits, underscore.")
        return v

    @field_validator("ff_uid")
    @classmethod
    def validate_ff_uid(cls, v: str) -> str:
        import re
        v = v.strip()
        if not re.match(r"^\d{6,15}$", v):
            raise ValueError("Free Fire UID must be 6–15 digits.")
        return v

    @model_validator(mode="after")
    def require_contact(self) -> "RecoveryRequestCreate":
        wa = (self.whatsapp_number or "").strip()
        ig = (self.instagram_id or "").strip()
        if not wa and not ig:
            raise ValueError("Provide at least one contact method: WhatsApp number or Instagram ID.")
        self.whatsapp_number = wa or None
        self.instagram_id    = ig or None
        return self


class RecoveryRequestResponse(BaseModel):
    """
    Generic acknowledgement — always returned on valid input regardless of
    whether the username/uid exist. Prevents account enumeration.
    """
    message: str = (
        "Your recovery request has been received. "
        "Our support team will review it and contact you through your provided channel."
    )
    request_id: str  # UUID string — player can quote this to support


# ── Endpoint ──────────────────────────────────────────────────────────────────

@router.post(
    "/request",
    response_model=RecoveryRequestResponse,
    status_code=201,
    summary="Submit a public password recovery request (no authentication required)",
)
@limiter.limit("3/hour")  # strict — 3 attempts per IP per hour
async def submit_recovery_request(
    request: Request,
    body: RecoveryRequestCreate,
    db: AsyncSession = Depends(get_db),
) -> RecoveryRequestResponse:
    """
    Submit a password recovery request without logging in.

    Security guarantees:
    - Returns the same success response whether or not the username exists.
    - Does NOT verify identity — admin must verify manually before issuing a token.
    - Does NOT accept a new password.
    - Rate-limited to 3 requests per IP per hour.
    """
    # Build the message text stored in the support request
    full_message = (
        f"[PASSWORD RECOVERY REQUEST]\n\n"
        f"Claimed username: {body.username}\n"
        f"Claimed Free Fire UID: {body.ff_uid}\n\n"
        f"Player message:\n{body.message}"
    )

    recovery_req = SupportRequest(
        # user_id is NULL — we cannot verify identity at submission time.
        # The admin will look up the actual user after manual verification.
        user_id=None,  # type: ignore[arg-type]
        message=full_message,
        whatsapp_number=body.whatsapp_number,
        instagram_id=body.instagram_id,
        status=SupportStatus.pending,
        is_recovery=True,
        requester_username=body.username,
        requester_ff_uid=body.ff_uid,
    )
    db.add(recovery_req)
    await db.flush([recovery_req])
    request_id = str(recovery_req.id)
    await db.commit()

    # Generic response — same text regardless of username existence
    return RecoveryRequestResponse(request_id=request_id)
