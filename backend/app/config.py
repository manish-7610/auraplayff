"""
Application configuration loaded from environment variables.
All secrets must be set in .env (never hardcoded here).
"""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Application ──────────────────────────────────────────────────────────
    app_env: Literal["development", "production", "testing"] = "development"
    app_debug: bool = False
    app_secret_key: str

    # ── Database ─────────────────────────────────────────────────────────────
    database_url: str

    # ── JWT ──────────────────────────────────────────────────────────────────
    jwt_secret_key: str
    jwt_algorithm: str = "HS256"
    jwt_access_token_expire_minutes: int = 15
    jwt_refresh_token_expire_days: int = 7

    # ── CORS ─────────────────────────────────────────────────────────────────
    cors_allowed_origins: str = "http://localhost:3000"

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_allowed_origins.split(",") if o.strip()]

    # ── Super Admin Bootstrap ─────────────────────────────────────────────────
    super_admin_agent_id: str
    super_admin_password: str

    # ── Supabase Storage ─────────────────────────────────────────────────────
    supabase_url: str = ""
    supabase_service_role_key: str = ""
    supabase_storage_bucket_screenshots: str = "tournament-screenshots"

    @property
    def supabase_key_looks_valid(self) -> bool:
        """
        Returns True if the configured key appears to be a recognized Supabase key format:
          - Legacy service-role JWT:  starts with 'eyJ' (base64url-encoded JSON header)
          - New Secret key:           starts with 'sb_secret_'
        Both formats are accepted by Supabase Storage when sent on the apikey header.
        """
        key = self.supabase_service_role_key
        return bool(key) and (key.startswith("eyJ") or key.startswith("sb_secret_"))

    # ── Razorpay (Phase 2+) ──────────────────────────────────────────────────
    razorpay_mode: Literal["test", "live"] = "test"
    razorpay_key_id: str = ""
    razorpay_key_secret: str = ""
    razorpay_webhook_secret: str = ""

    # ── Rate Limiting ─────────────────────────────────────────────────────────
    rate_limit_login_per_minute: int = 5

    # ── Password Reset ────────────────────────────────────────────────────────
    password_reset_token_expire_minutes: int = 30

    # ── Scheduler / Automation (Phase 7) ─────────────────────────────────────
    # Retention period for screenshot files in Supabase Storage
    screenshot_retention_days: int = 30
    # Leaderboard in-process cache TTL in seconds
    leaderboard_cache_ttl_seconds: int = 300
    # Scheduler enabled flag — set to false in testing to prevent job startup
    scheduler_enabled: bool = True

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, v: str) -> str:
        if not v.startswith("postgresql"):
            raise ValueError("DATABASE_URL must be a PostgreSQL URL")
        # Catch the unfilled .env placeholder
        if "USER:PASSWORD@HOST:PORT" in v or v.endswith("HOST:PORT/DATABASE"):
            raise ValueError(
                "DATABASE_URL still contains placeholder text. "
                "Edit .env and replace USER, PASSWORD, HOST, PORT, DATABASE "
                "with your actual Supabase / PostgreSQL connection details."
            )
        return v

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def is_testing(self) -> bool:
        return self.app_env == "testing"


@lru_cache
def get_settings() -> Settings:
    """Return cached settings instance. Call get_settings.cache_clear() in tests."""
    return Settings()
