"""
AuraPlayFF — FastAPI application entry point.

Startup sequence:
  1. Configure logging
  2. Create FastAPI app with lifespan (bootstrap + shutdown)
  3. Register middleware (order matters — outermost first)
  4. Register exception handlers
  5. Include routers
"""
from __future__ import annotations

import logging
import logging.config
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from app.config import get_settings
from app.core.exceptions import (
    AppError,
    app_error_handler,
    http_exception_handler,
    unhandled_exception_handler,
)
from app.core.middleware import (
    LastSeenMiddleware,
    RequestIDMiddleware,
    SecurityHeadersMiddleware,
)
from app.core.rate_limiter import limiter
from app.database import get_session_factory

# ── Logging configuration ─────────────────────────────────────────────────────

def _configure_logging(debug: bool) -> None:
    level = logging.DEBUG if debug else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # Quieten noisy libraries
    logging.getLogger("sqlalchemy.engine").setLevel(
        logging.DEBUG if debug else logging.WARNING
    )
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


logger = logging.getLogger("auraplayff")


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Runs on startup and shutdown."""
    settings = get_settings()
    _configure_logging(settings.app_debug)
    logger.info("AuraPlayFF starting — env=%s", settings.app_env)

    # T11 Safety guard: warn loudly if debug mode is on in production
    if settings.is_production and settings.app_debug:
        logger.critical(
            "SECURITY WARNING: APP_DEBUG=true in production environment! "
            "Stack traces may be exposed to clients. Set APP_DEBUG=false immediately."
        )

    # Supabase storage key sanity check — warn at startup, not at first upload
    if settings.supabase_url and settings.supabase_service_role_key:
        if not settings.supabase_key_looks_valid:
            logger.warning(
                "CONFIGURATION WARNING: SUPABASE_SERVICE_ROLE_KEY does not look like "
                "a recognized Supabase key (expected 'eyJ...' JWT or 'sb_secret_...' Secret key). "
                "QR image upload and screenshot storage will fail until this is fixed. "
                "Get the correct key from Supabase project → Settings → API → "
                "service_role (legacy) or Secret key (new format), "
                "set it in backend/.env, then restart."
            )
        else:
            # Key looks valid — ensure the storage bucket exists (creates it if missing).
            # Best-effort: if this fails the server still starts; uploads will fail
            # with a clear 'Bucket not found' error rather than a confusing auth error.
            try:
                import httpx as _httpx
                import json as _json
                _base = settings.supabase_url.rstrip("/")
                _key  = settings.supabase_service_role_key
                _bucket = settings.supabase_storage_bucket_screenshots
                _hdrs = {
                    "Authorization": f"Bearer {_key}",
                    "apikey": _key,
                    "Content-Type": "application/json",
                }
                async with _httpx.AsyncClient(timeout=10.0) as _c:
                    _check = await _c.get(f"{_base}/storage/v1/bucket/{_bucket}", headers=_hdrs)
                    if _check.status_code == 404 or (
                        _check.status_code == 400
                        and "not found" in _check.text.lower()
                    ):
                        _payload = _json.dumps({
                            "id": _bucket, "name": _bucket, "public": False
                        }).encode()
                        _create = await _c.post(
                            f"{_base}/storage/v1/bucket", headers=_hdrs, content=_payload
                        )
                        if _create.status_code in (200, 201):
                            logger.info(
                                "Supabase Storage: created bucket '%s'.", _bucket
                            )
                        else:
                            logger.warning(
                                "Supabase Storage: could not create bucket '%s': HTTP %s — %s",
                                _bucket, _create.status_code, _create.text[:200],
                            )
                    elif _check.status_code in (200, 201):
                        logger.info("Supabase Storage: bucket '%s' OK.", _bucket)
                    else:
                        logger.warning(
                            "Supabase Storage: bucket check returned HTTP %s — %s",
                            _check.status_code, _check.text[:200],
                        )
            except Exception as _storage_err:
                logger.warning(
                    "Supabase Storage: bucket check/create failed (non-fatal): %s",
                    _storage_err,
                )

    # Run bootstrap (permission seeding + super admin creation)
    # Wrapped in try/except so the server starts even if the DB is temporarily
    # unreachable — health endpoint will report "degraded" until DB is available.
    try:
        from app.services.bootstrap_service import run_bootstrap
        factory = get_session_factory()
        async with factory() as db:
            async with db.begin():
                await run_bootstrap(db)
        logger.info("Bootstrap complete.")
    except Exception as _bootstrap_err:
        logger.error(
            "Bootstrap failed — DB may be unreachable or DATABASE_URL is not configured. "
            "Server will start in degraded mode. Error: %s",
            _bootstrap_err,
        )
        logger.error(
            "Fix: edit backend/.env and set DATABASE_URL to your PostgreSQL/Supabase "
            "connection string, then restart the server."
        )

    # Start APScheduler — Phase 7
    from app.scheduler import setup_scheduler
    if settings.scheduler_enabled:
        scheduler = setup_scheduler()
        scheduler.start()
        app.state.scheduler = scheduler
        logger.info("Scheduler started with %d jobs.", len(scheduler.get_jobs()))
    else:
        app.state.scheduler = None
        logger.info("Scheduler disabled (SCHEDULER_ENABLED=false).")

    logger.info("Application ready.")
    yield

    # Graceful scheduler shutdown
    if hasattr(app.state, "scheduler") and app.state.scheduler.running:
        app.state.scheduler.shutdown(wait=False)
        logger.info("Scheduler shut down.")

    logger.info("AuraPlayFF shutting down.")


# ── Application factory ───────────────────────────────────────────────────────

def create_app(settings=None) -> FastAPI:
    if settings is None:
        settings = get_settings()

    app = FastAPI(
        title="AuraPlayFF API",
        description="Free Fire solo tournament platform — backend API",
        version="1.0.0",
        docs_url="/api/docs" if not settings.is_production else None,
        redoc_url="/api/redoc" if not settings.is_production else None,
        openapi_url="/api/openapi.json" if not settings.is_production else None,
        lifespan=lifespan,
    )

    # ── Rate limiter state ────────────────────────────────────────────────────
    app.state.limiter = limiter

    # ── Middleware (added in reverse order of execution — last added = outermost) ──
    # 1. CORS (must be registered before security headers so preflight works)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
        expose_headers=["X-Request-ID"],
    )

    # 2. slowapi rate limiting middleware
    app.add_middleware(SlowAPIMiddleware)

    # 3. Request ID (needs to be available for all other middleware and handlers)
    app.add_middleware(RequestIDMiddleware)

    # 4. Security headers (applied to every response)
    app.add_middleware(SecurityHeadersMiddleware)

    # 5. Last-seen update (fire-and-forget, after response is generated)
    app.add_middleware(LastSeenMiddleware)

    from fastapi import HTTPException as _HTTPException
    # ── Exception handlers ────────────────────────────────────────────────────
    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.add_exception_handler(_HTTPException, http_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)

    # ── Routers ───────────────────────────────────────────────────────────────
    from app.routers.health import router as health_router
    from app.routers.auth import router as player_auth_router
    from app.routers.staff_auth import router as staff_auth_router
    from app.routers.admin_users import router as admin_users_router
    from app.routers.wallet import router as player_wallet_router
    from app.routers.admin_wallet import router as admin_wallet_router
    from app.routers.admin_wallet import referral_router as admin_referral_router
    from app.routers.tournaments import router as player_tournament_router
    from app.routers.admin_tournaments import router as admin_tournament_router
    # Phase 4
    from app.routers.results import (
        player_router as player_results_router,
        admin_router as admin_results_router,
        config_router as prize_config_router,
        history_router as results_history_router,
    )
    from app.routers.leaderboard import (
        public_router as leaderboard_router,
        admin_router as admin_metrics_router,
    )

    app.include_router(health_router)
    app.include_router(player_auth_router)
    app.include_router(staff_auth_router)
    app.include_router(admin_users_router)
    app.include_router(player_wallet_router)
    app.include_router(admin_wallet_router)
    app.include_router(admin_referral_router)
    app.include_router(player_tournament_router)
    app.include_router(admin_tournament_router)
    # Phase 4 routers
    app.include_router(player_results_router)
    app.include_router(admin_results_router)
    app.include_router(prize_config_router)
    app.include_router(results_history_router)  # Phase 11 — completed results history
    app.include_router(leaderboard_router)
    app.include_router(admin_metrics_router)
    # Phase 5 — Deposits, Withdrawals, Webhooks
    from app.routers.deposits import router as player_deposits_router
    from app.routers.withdrawals import router as player_withdrawals_router
    from app.routers.admin_deposits import router as admin_deposits_router
    from app.routers.admin_withdrawals import router as admin_withdrawals_router
    from app.routers.webhooks import router as webhooks_router

    app.include_router(player_deposits_router)
    app.include_router(player_withdrawals_router)
    app.include_router(admin_deposits_router)
    app.include_router(admin_withdrawals_router)
    app.include_router(webhooks_router)
    # Phase 6 — Agent Management, Performance, Salary
    from app.routers.agent import router as agent_self_router
    from app.routers.admin_agents import router as admin_agents_router
    from app.routers.admin_agents import targets_router as admin_targets_router

    app.include_router(agent_self_router)
    app.include_router(admin_agents_router)
    app.include_router(admin_targets_router)

    # Phase 7 — Notifications, Scheduler monitoring
    from app.routers.notifications import player_router as player_notif_router
    from app.routers.notifications import staff_router as staff_notif_router
    from app.routers.admin_scheduler import router as admin_scheduler_router

    app.include_router(player_notif_router)
    app.include_router(staff_notif_router)
    app.include_router(admin_scheduler_router)

    # Phase 9 — Users/me, Admin users list/detail/ban, Audit logs, Settings
    from app.routers.users import router as users_me_router
    from app.routers.admin_audit import router as admin_audit_router
    from app.routers.admin_settings import router as admin_settings_router
    from app.routers.admin_payment_config import router as admin_payment_config_router

    app.include_router(users_me_router)
    app.include_router(admin_audit_router)
    app.include_router(admin_settings_router)
    app.include_router(admin_payment_config_router)

    # Phase 10 — Help & Support
    from app.routers.support import (
        player_router as player_support_router,
        admin_router as admin_support_router,
    )

    app.include_router(player_support_router)
    app.include_router(admin_support_router)

    return app


# Module-level app instance used by uvicorn
# Only created when env vars are available.
#
# If required environment variables are missing, we raise SystemExit with a
# clear message instead of silently setting app=None (which causes uvicorn to
# fail with "TypeError: 'NoneType' object is not callable").
#
# During pytest, conftest.py sets all required env vars BEFORE this module is
# imported, so Settings() succeeds and create_app() returns normally.
try:
    app = create_app()
except Exception as _startup_err:  # pragma: no cover
    import sys as _sys
    import logging as _logging

    _log = _logging.getLogger("auraplayff")
    _log.error("Failed to create app: %s", _startup_err)

    # Detect whether this is a missing-env-vars problem (pydantic ValidationError)
    # vs. some other import error, and give a tailored message.
    _err_type = type(_startup_err).__name__
    if _err_type == "ValidationError" or "Field required" in str(_startup_err):
        _sys.exit(
            "\n"
            "══════════════════════════════════════════════════════════════\n"
            "  AuraPlayFF — missing required environment variables\n"
            "══════════════════════════════════════════════════════════════\n"
            "\n"
            f"  {_startup_err}\n"
            "\n"
            "  Fix: copy .env.example to .env and fill in your values:\n"
            "\n"
            "    cd backend\n"
            "    copy .env.example .env          # Windows\n"
            "    # then edit .env — at minimum set:\n"
            "    #   DATABASE_URL       = postgresql+asyncpg://...\n"
            "    #   APP_SECRET_KEY     = (any long random string)\n"
            "    #   JWT_SECRET_KEY     = (any long random string)\n"
            "    #   SUPER_ADMIN_AGENT_ID = ADMIN-001\n"
            "    #   SUPER_ADMIN_PASSWORD = (your chosen admin password)\n"
            "\n"
            "══════════════════════════════════════════════════════════════\n"
        )
    else:
        _sys.exit(
            f"\nAuraPlayFF failed to start: {_err_type}: {_startup_err}\n"
            "Check the logs above for details.\n"
        )
