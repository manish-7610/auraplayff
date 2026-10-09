"""
Custom exception classes and consistent JSON error response format.

All API error responses use this shape:
  {
    "error": {
      "code": "MACHINE_READABLE_CODE",
      "message": "Human-readable description."
    }
  }

Internal details (stack traces, DB errors) are NEVER exposed in production.
"""
from __future__ import annotations

from fastapi import HTTPException, Request, status
from fastapi.responses import JSONResponse


# ── Error response builder ────────────────────────────────────────────────────

def error_response(code: str, message: str, status_code: int) -> JSONResponse:
    """Build a standardised JSON error response."""
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message}},
    )


# ── Domain exceptions — raised in service/router layers ──────────────────────

class AppError(Exception):
    """Base class for all application-layer exceptions."""

    def __init__(
        self,
        code: str,
        message: str,
        status_code: int = status.HTTP_400_BAD_REQUEST,
    ) -> None:
        self.code = code
        self.message = message
        self.status_code = status_code
        super().__init__(message)


# ── Auth errors ───────────────────────────────────────────────────────────────

class InvalidCredentialsError(AppError):
    def __init__(self) -> None:
        super().__init__(
            code="INVALID_CREDENTIALS",
            message="Username or password is incorrect.",
            status_code=status.HTTP_401_UNAUTHORIZED,
        )


class TokenExpiredError(AppError):
    def __init__(self) -> None:
        super().__init__(
            code="TOKEN_EXPIRED",
            message="Your session has expired. Please log in again.",
            status_code=status.HTTP_401_UNAUTHORIZED,
        )


class TokenInvalidError(AppError):
    def __init__(self, detail: str = "Invalid authentication token.") -> None:
        super().__init__(
            code="TOKEN_INVALID",
            message=detail,
            status_code=status.HTTP_401_UNAUTHORIZED,
        )


class SessionInvalidatedError(AppError):
    def __init__(self) -> None:
        super().__init__(
            code="SESSION_INVALIDATED",
            message="Your session has been invalidated. Please log in again.",
            status_code=status.HTTP_401_UNAUTHORIZED,
        )


class AccountInactiveError(AppError):
    def __init__(self) -> None:
        super().__init__(
            code="ACCOUNT_INACTIVE",
            message="This account is inactive.",
            status_code=status.HTTP_403_FORBIDDEN,
        )


class AccountBannedError(AppError):
    def __init__(self) -> None:
        super().__init__(
            code="ACCOUNT_BANNED",
            message="This account has been suspended.",
            status_code=status.HTTP_403_FORBIDDEN,
        )


class PermissionDeniedError(AppError):
    def __init__(self, permission: str = "") -> None:
        msg = "You do not have permission to perform this action."
        if permission:
            msg = f"Missing required permission: {permission}."
        super().__init__(
            code="PERMISSION_DENIED",
            message=msg,
            status_code=status.HTTP_403_FORBIDDEN,
        )


# ── Registration / user errors ────────────────────────────────────────────────

class FFNameRequiredError(AppError):
    """Raised when a player attempts to join a tournament without a Free Fire name set."""
    def __init__(self) -> None:
        super().__init__(
            code="FF_NAME_REQUIRED",
            message=(
                "You must set your Free Fire name before joining a tournament. "
                "Go to your profile to set it."
            ),
            status_code=status.HTTP_403_FORBIDDEN,
        )


class UsernameTakenError(AppError):
    def __init__(self) -> None:
        super().__init__(
            code="USERNAME_ALREADY_EXISTS",
            message="This username is already registered.",
            status_code=status.HTTP_409_CONFLICT,
        )


class FFUIDTakenError(AppError):
    def __init__(self) -> None:
        super().__init__(
            code="FF_UID_ALREADY_EXISTS",
            message="This Free Fire UID is already linked to an account.",
            status_code=status.HTTP_409_CONFLICT,
        )


class InvalidReferralCodeError(AppError):
    def __init__(self) -> None:
        super().__init__(
            code="INVALID_REFERRAL_CODE",
            message="The referral code provided is not valid.",
            status_code=status.HTTP_400_BAD_REQUEST,
        )


class SelfReferralError(AppError):
    def __init__(self) -> None:
        super().__init__(
            code="SELF_REFERRAL_NOT_ALLOWED",
            message="You cannot use your own referral code.",
            status_code=status.HTTP_400_BAD_REQUEST,
        )


class PasswordMismatchError(AppError):
    def __init__(self) -> None:
        super().__init__(
            code="PASSWORD_MISMATCH",
            message="Password and confirmation do not match.",
            status_code=status.HTTP_400_BAD_REQUEST,
        )


class WeakPasswordError(AppError):
    def __init__(self, detail: str = "Password does not meet requirements.") -> None:
        super().__init__(
            code="WEAK_PASSWORD",
            message=detail,
            status_code=status.HTTP_400_BAD_REQUEST,
        )


# ── Password reset errors ─────────────────────────────────────────────────────

class ResetTokenInvalidError(AppError):
    def __init__(self) -> None:
        super().__init__(
            code="RESET_TOKEN_INVALID",
            message="The password reset token is invalid or has already been used.",
            status_code=status.HTTP_400_BAD_REQUEST,
        )


class ResetTokenExpiredError(AppError):
    def __init__(self) -> None:
        super().__init__(
            code="RESET_TOKEN_EXPIRED",
            message="The password reset token has expired. Please request a new one.",
            status_code=status.HTTP_400_BAD_REQUEST,
        )


# ── Wallet errors ─────────────────────────────────────────────────────────────

class WalletNotFoundError(AppError):
    def __init__(self) -> None:
        super().__init__(
            code="WALLET_NOT_FOUND",
            message="Wallet not found for this user.",
            status_code=status.HTTP_404_NOT_FOUND,
        )


class WalletFrozenError(AppError):
    def __init__(self) -> None:
        super().__init__(
            code="WALLET_FROZEN",
            message="This wallet is currently frozen. Contact support.",
            status_code=status.HTTP_403_FORBIDDEN,
        )


class InsufficientBalanceError(AppError):
    def __init__(self, balance_type: str = "balance") -> None:
        super().__init__(
            code="INSUFFICIENT_BALANCE",
            message=f"Insufficient {balance_type}. Please add funds and try again.",
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
        )


class InvalidAmountError(AppError):
    def __init__(self, detail: str = "Amount must be greater than zero.") -> None:
        super().__init__(
            code="INVALID_AMOUNT",
            message=detail,
            status_code=status.HTTP_400_BAD_REQUEST,
        )


class DuplicateTransactionError(AppError):
    """Raised when an idempotency key collision is detected."""
    def __init__(self) -> None:
        super().__init__(
            code="DUPLICATE_TRANSACTION",
            message="This transaction has already been processed.",
            status_code=status.HTTP_409_CONFLICT,
        )


class ReferralBonusLimitExceededError(AppError):
    """Raised when a player tries to pay more than 50% of entry fee from referral balance."""
    def __init__(self, max_referral: str, entry_fee: str) -> None:
        super().__init__(
            code="REFERRAL_BONUS_LIMIT_EXCEEDED",
            message=(
                f"You can use at most ₹{max_referral} of referral bonus "
                f"for this ₹{entry_fee} entry fee. "
                f"The remaining amount must come from your deposit balance."
            ),
            status_code=status.HTTP_400_BAD_REQUEST,
        )


class ReferralAlreadyRewardedError(AppError):
    def __init__(self) -> None:
        super().__init__(
            code="REFERRAL_ALREADY_REWARDED",
            message="Referral reward has already been credited for this referral.",
            status_code=status.HTTP_409_CONFLICT,
        )


class ReferralConditionNotMetError(AppError):
    def __init__(self) -> None:
        super().__init__(
            code="REFERRAL_CONDITION_NOT_MET",
            message="Referral reward cannot be credited yet. The referred user has not completed their first tournament.",
            status_code=status.HTTP_400_BAD_REQUEST,
        )


# ── Tournament errors ─────────────────────────────────────────────────────────

class TournamentNotFoundError(AppError):
    def __init__(self) -> None:
        super().__init__(
            code="TOURNAMENT_NOT_FOUND",
            message="Tournament not found.",
            status_code=status.HTTP_404_NOT_FOUND,
        )


class TournamentNotOpenError(AppError):
    def __init__(self, current_status: str = "") -> None:
        detail = "This tournament is not open for registration."
        if current_status:
            detail = f"This tournament cannot be joined — current status: {current_status}."
        super().__init__(
            code="TOURNAMENT_NOT_OPEN",
            message=detail,
            status_code=status.HTTP_409_CONFLICT,
        )


class TournamentFullError(AppError):
    def __init__(self) -> None:
        super().__init__(
            code="TOURNAMENT_FULL",
            message="This tournament is full. No slots are available.",
            status_code=status.HTTP_409_CONFLICT,
        )


class AlreadyJoinedTournamentError(AppError):
    def __init__(self) -> None:
        super().__init__(
            code="ALREADY_JOINED_TOURNAMENT",
            message="You have already joined this tournament.",
            status_code=status.HTTP_409_CONFLICT,
        )


class TournamentInvalidTransitionError(AppError):
    def __init__(self, from_status: str, to_status: str) -> None:
        super().__init__(
            code="INVALID_STATUS_TRANSITION",
            message=f"Cannot transition tournament from '{from_status}' to '{to_status}'.",
            status_code=status.HTTP_409_CONFLICT,
        )


class TournamentAlreadyCancelledError(AppError):
    def __init__(self) -> None:
        super().__init__(
            code="TOURNAMENT_ALREADY_CANCELLED",
            message="This tournament has already been cancelled.",
            status_code=status.HTTP_409_CONFLICT,
        )


class TournamentAlreadyCompletedError(AppError):
    def __init__(self) -> None:
        super().__init__(
            code="TOURNAMENT_ALREADY_COMPLETED",
            message="This tournament has already been completed.",
            status_code=status.HTTP_409_CONFLICT,
        )


class RoomNotReleasedError(AppError):
    def __init__(self) -> None:
        super().__init__(
            code="ROOM_NOT_RELEASED",
            message="Room credentials have not been released yet. Check back shortly before the tournament starts.",
            status_code=status.HTTP_403_FORBIDDEN,
        )


class TournamentLockedAfterRoomReleaseError(AppError):
    """
    Raised when an admin/agent attempts to cancel (or make other prohibited
    changes to) a tournament after room credentials have already been released.

    Once the room is released players have received the room ID/password and
    are preparing to play — cancellation at this point is not allowed.
    """
    def __init__(self) -> None:
        super().__init__(
            code="TOURNAMENT_LOCKED_AFTER_ROOM_RELEASE",
            message=(
                "This tournament cannot be cancelled because room credentials "
                "have already been released to players. "
                "The tournament is locked once the room is released."
            ),
            status_code=status.HTTP_409_CONFLICT,
        )


class NotEnrolledError(AppError):
    def __init__(self) -> None:
        super().__init__(
            code="NOT_ENROLLED",
            message="You are not enrolled in this tournament.",
            status_code=status.HTTP_403_FORBIDDEN,
        )


class InvalidEntryFeeError(AppError):
    def __init__(self, fee: str = "") -> None:
        detail = "The specified entry fee is not supported."
        if fee:
            detail = f"Entry fee ₹{fee} is not a supported tournament entry fee."
        super().__init__(
            code="INVALID_ENTRY_FEE",
            message=detail,
            status_code=status.HTTP_400_BAD_REQUEST,
        )


class TournamentScheduleError(AppError):
    def __init__(self, detail: str = "Invalid scheduled time.") -> None:
        super().__init__(
            code="INVALID_SCHEDULE",
            message=detail,
            status_code=status.HTTP_400_BAD_REQUEST,
        )


class InsufficientPlayersError(AppError):
    def __init__(self, current: int, minimum: int) -> None:
        super().__init__(
            code="INSUFFICIENT_PLAYERS",
            message=(
                f"Tournament cannot start: only {current} player(s) registered, "
                f"minimum required is {minimum}."
            ),
            status_code=status.HTTP_409_CONFLICT,
        )


# ── Result / Prize errors ─────────────────────────────────────────────────────

class ResultNotFoundError(AppError):
    def __init__(self) -> None:
        super().__init__(
            code="RESULT_NOT_FOUND",
            message="No result found for this tournament entry.",
            status_code=status.HTTP_404_NOT_FOUND,
        )


class ResultAlreadyExistsError(AppError):
    def __init__(self) -> None:
        super().__init__(
            code="RESULT_ALREADY_EXISTS",
            message="A result has already been entered for this tournament entry.",
            status_code=status.HTTP_409_CONFLICT,
        )


class ResultAlreadyFinalizedError(AppError):
    def __init__(self) -> None:
        super().__init__(
            code="RESULT_ALREADY_FINALIZED",
            message="This result has been finalized and can no longer be modified.",
            status_code=status.HTTP_409_CONFLICT,
        )


class DuplicatePlacementError(AppError):
    def __init__(self, placement: int) -> None:
        super().__init__(
            code="DUPLICATE_PLACEMENT",
            message=f"Placement {placement} has already been assigned to another player in this tournament.",
            status_code=status.HTTP_409_CONFLICT,
        )


class InvalidPlacementError(AppError):
    def __init__(self, detail: str = "Placement must be a positive integer.") -> None:
        super().__init__(
            code="INVALID_PLACEMENT",
            message=detail,
            status_code=status.HTTP_400_BAD_REQUEST,
        )


class ResultsIncompleteError(AppError):
    def __init__(self, entered: int, expected: int) -> None:
        super().__init__(
            code="RESULTS_INCOMPLETE",
            message=(
                f"Cannot complete tournament: results entered for {entered} of "
                f"{expected} active participants. All results must be entered before completion."
            ),
            status_code=status.HTTP_409_CONFLICT,
        )


class PayoutAlreadyDistributedError(AppError):
    def __init__(self) -> None:
        super().__init__(
            code="PAYOUT_ALREADY_DISTRIBUTED",
            message="Prizes have already been distributed for this tournament.",
            status_code=status.HTTP_409_CONFLICT,
        )


class PrizeConfigNotFoundError(AppError):
    def __init__(self, entry_fee: str) -> None:
        super().__init__(
            code="PRIZE_CONFIG_NOT_FOUND",
            message=(
                f"No prize configuration found for ₹{entry_fee} entry fee. "
                f"An admin must configure prizes for this tier before completion."
            ),
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )


# ── Screenshot errors ─────────────────────────────────────────────────────────

class ScreenshotUploadWindowClosedError(AppError):
    def __init__(self) -> None:
        super().__init__(
            code="UPLOAD_WINDOW_CLOSED",
            message="The 15-minute screenshot upload window has closed.",
            status_code=status.HTTP_403_FORBIDDEN,
        )


class ScreenshotLimitReachedError(AppError):
    def __init__(self) -> None:
        super().__init__(
            code="SCREENSHOT_LIMIT_REACHED",
            message="You have already uploaded the maximum 2 screenshots for this tournament.",
            status_code=status.HTTP_409_CONFLICT,
        )


class InvalidFileTypeError(AppError):
    def __init__(self) -> None:
        super().__init__(
            code="INVALID_FILE_TYPE",
            message="Only JPEG and PNG images are accepted.",
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
        )


class FileTooLargeError(AppError):
    def __init__(self, max_mb: int) -> None:
        super().__init__(
            code="FILE_TOO_LARGE",
            message=f"File exceeds the maximum allowed size of {max_mb} MB.",
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
        )


class StorageError(AppError):
    def __init__(self, detail: str = "File storage operation failed.") -> None:
        super().__init__(
            code="STORAGE_ERROR",
            message=detail,
            status_code=status.HTTP_502_BAD_GATEWAY,
        )


# ── Generic ───────────────────────────────────────────────────────────────────

class NotFoundError(AppError):
    def __init__(self, entity: str = "Resource") -> None:
        super().__init__(
            code="NOT_FOUND",
            message=f"{entity} not found.",
            status_code=status.HTTP_404_NOT_FOUND,
        )


class ConflictError(AppError):
    def __init__(self, message: str = "A conflict occurred.") -> None:
        super().__init__(
            code="CONFLICT",
            message=message,
            status_code=status.HTTP_409_CONFLICT,
        )


# ── FastAPI exception handlers ────────────────────────────────────────────────

async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    """Handler registered on the FastAPI app for AppError subclasses."""
    return error_response(exc.code, exc.message, exc.status_code)


async def http_exception_handler(
    request: Request, exc: HTTPException
) -> JSONResponse:
    """Convert FastAPI HTTPExceptions to the standard error shape."""
    code_map = {
        400: "BAD_REQUEST",
        401: "UNAUTHORIZED",
        403: "FORBIDDEN",
        404: "NOT_FOUND",
        405: "METHOD_NOT_ALLOWED",
        422: "VALIDATION_ERROR",
        429: "RATE_LIMIT_EXCEEDED",
        500: "INTERNAL_ERROR",
    }
    code = code_map.get(exc.status_code, "HTTP_ERROR")
    detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
    return error_response(code, detail, exc.status_code)


async def unhandled_exception_handler(
    request: Request, exc: Exception
) -> JSONResponse:
    """
    Catch-all for unhandled exceptions.
    Never expose internal details in production.
    """
    import logging
    from app.config import get_settings

    logger = logging.getLogger("auraplayff.errors")
    logger.exception("Unhandled exception on %s %s", request.method, request.url)

    if get_settings().app_debug:
        import traceback
        detail = traceback.format_exc()
        return error_response("INTERNAL_ERROR", detail, 500)

    return error_response(
        "INTERNAL_ERROR",
        "An unexpected error occurred. Please try again later.",
        500,
    )
