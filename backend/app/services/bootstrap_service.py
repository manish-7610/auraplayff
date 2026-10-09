"""
Bootstrap service — runs once on application startup.

1. Seeds the permissions table with all defined permission codes.
2. Creates the first Super Admin from environment variables if none exists.

Rules:
  - Idempotent: safe to run on every restart.
  - Never overwrites an existing Super Admin.
  - Never prints or logs passwords.
  - Never hardcodes credentials.
"""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.permissions import PERMISSION_DESCRIPTIONS, ALL_PERMISSION_CODES
from app.core.security import hash_password
from app.models.audit_log import ActorType
from app.models.permission import Permission as PermissionModel
from app.models.staff_user import StaffRole, StaffUser
from app.services import audit_service

logger = logging.getLogger("auraplayff.bootstrap")


async def run_bootstrap(db: AsyncSession) -> None:
    """Entry point called from the FastAPI lifespan event."""
    await _seed_permissions(db)
    await _bootstrap_super_admin(db)
    # Phase 5: seed platform payment settings
    await _seed_platform_settings(db)
    # Phase 6: seed agent target configurations
    await _seed_agent_target_configs(db)
    # Seed all entry fee prize configs (all 6 tiers active)
    from app.services.prize_service import seed_all_prize_configs
    await seed_all_prize_configs(db)


# ── Permission seeding ────────────────────────────────────────────────────────

async def _seed_permissions(db: AsyncSession) -> None:
    """
    Insert any permission codes that do not yet exist in the DB.
    Existing codes are left untouched (description updates are a no-op
    to avoid overwriting custom descriptions set by admin).
    """
    result = await db.execute(select(PermissionModel.code))
    existing_codes: set[str] = {row[0] for row in result.fetchall()}

    new_codes = ALL_PERMISSION_CODES - existing_codes
    if not new_codes:
        logger.debug("Permissions already seeded (%d codes)", len(existing_codes))
        return

    for code in sorted(new_codes):
        description = PERMISSION_DESCRIPTIONS.get(code, "")
        perm = PermissionModel(code=code, description=description)
        db.add(perm)

    await db.flush()
    logger.info("Seeded %d new permission(s): %s", len(new_codes), sorted(new_codes))


# ── Super Admin bootstrap ─────────────────────────────────────────────────────

async def _bootstrap_super_admin(db: AsyncSession) -> None:
    """
    Create the first Super Admin from environment variables if none exists.

    Environment variables required:
      SUPER_ADMIN_AGENT_ID    — human-readable login ID  (e.g. ADMIN-001)
      SUPER_ADMIN_PASSWORD    — strong password (will be bcrypt-hashed)

    Behavior:
      - If any super_admin already exists → skip entirely.
      - If the env vars are missing → log a warning and skip.
      - Never creates duplicate super admin accounts.
    """
    # Check if any super_admin already exists
    result = await db.execute(
        select(StaffUser.id).where(StaffUser.role == StaffRole.super_admin).limit(1)
    )
    if result.scalar_one_or_none() is not None:
        logger.debug("Super Admin already exists — bootstrap skipped.")
        return

    settings = get_settings()
    agent_id = settings.super_admin_agent_id.strip()
    raw_password = settings.super_admin_password

    if not agent_id or not raw_password:
        logger.warning(
            "SUPER_ADMIN_AGENT_ID or SUPER_ADMIN_PASSWORD not set. "
            "Super Admin was NOT created. Set these env vars and restart."
        )
        return

    password_hash = hash_password(raw_password)

    admin = StaffUser(
        agent_id=agent_id,
        display_name="Super Admin",
        password_hash=password_hash,
        role=StaffRole.super_admin,
    )
    db.add(admin)
    await db.flush([admin])

    await audit_service.log(
        db=db,
        actor_type=ActorType.system,
        actor_id=audit_service.make_actor_id_system(),
        action="super_admin.bootstrapped",
        entity_type="staff_user",
        entity_id=str(admin.id),
        new_value={"agent_id": agent_id, "role": "super_admin"},
    )

    # Log agent_id (not password) so operator knows bootstrap ran
    logger.info(
        "Super Admin bootstrapped successfully. agent_id=%r "
        "(Change SUPER_ADMIN_PASSWORD in .env immediately after first login.)",
        agent_id,
    )


# ── Platform settings seeding (Phase 5) ──────────────────────────────────────

async def _seed_platform_settings(db: AsyncSession) -> None:
    """
    Seed default platform payment settings if they don't exist yet.
    Existing settings are never overwritten (admin may have changed them).

    Public settings (is_public=True) can be returned to authenticated players.
    Internal settings (is_public=False) are staff-only.
    """
    from sqlalchemy import select
    from app.models.platform_setting import PlatformSetting, SETTING_DEFAULTS

    _public_keys = {
        "upi_id",
        "upi_display_name",
        "upi_account_name",
        "manual_deposit_instructions",
        "qr_storage_ref",
        "manual_payment_active",
        "min_deposit_amount",
        "max_deposit_amount",
    }

    result = await db.execute(select(PlatformSetting.key))
    existing_keys: set[str] = {row[0] for row in result.fetchall()}

    new_settings: list[PlatformSetting] = []
    for key, default_value in SETTING_DEFAULTS.items():
        if key in existing_keys:
            continue
        setting = PlatformSetting(
            key=key,
            value=default_value,
            is_public=key in _public_keys,
            updated_by="system",
        )
        new_settings.append(setting)
        db.add(setting)

    if new_settings:
        await db.flush(new_settings)
        logger.info(
            "Seeded %d platform setting(s): %s",
            len(new_settings),
            [s.key for s in new_settings],
        )
    else:
        logger.debug("Platform settings already seeded.")


# ── Agent target config seeding (Phase 6) ────────────────────────────────────

async def _seed_agent_target_configs(db: AsyncSession) -> None:
    """
    Seed default daily target configs for each entry-fee tier and base salary.

    Defaults (from Phase 6 spec):
      ₹10  → 8/day,   extra bonus ₹25
      ₹20  → 7/day,   extra bonus ₹40
      ₹30  → 6/day,   extra bonus ₹60
      ₹40  → 5/day,   extra bonus ₹80
      ₹50  → 4/day,   extra bonus ₹100
      ₹100 → 3/day,   extra bonus ₹200
      base monthly salary → ₹10,000

    Idempotent: only creates rows that don't already exist.
    Existing rows are never overwritten (admin may have changed them).
    """
    from decimal import Decimal
    from sqlalchemy import select
    from app.models.agent_target_config import (
        AgentTargetConfig,
        DEFAULT_TIER_CONFIGS,
        DEFAULT_BASE_MONTHLY_SALARY,
    )

    # Check which entry_fee tiers already exist
    result = await db.execute(
        select(AgentTargetConfig.entry_fee)
    )
    existing_fees: set = {str(row[0]) for row in result.fetchall()}

    new_rows: list[AgentTargetConfig] = []

    # Seed tier configs
    for entry_fee_str, daily_required, extra_bonus_str in DEFAULT_TIER_CONFIGS:
        if entry_fee_str in existing_fees:
            continue
        cfg = AgentTargetConfig(
            entry_fee=Decimal(entry_fee_str),
            label=f"₹{int(Decimal(entry_fee_str))} tier",
            daily_required=daily_required,
            extra_bonus_per_tournament=Decimal(extra_bonus_str),
            deduction_per_missing=Decimal(extra_bonus_str),  # default: same as bonus
            is_active=True,
            updated_by="system",
        )
        db.add(cfg)
        new_rows.append(cfg)

    # Seed base salary config (entry_fee IS NULL)
    if "None" not in existing_fees:
        base_cfg = AgentTargetConfig(
            entry_fee=None,
            label="Base monthly salary",
            base_monthly_salary=DEFAULT_BASE_MONTHLY_SALARY,
            is_active=True,
            updated_by="system",
        )
        db.add(base_cfg)
        new_rows.append(base_cfg)

    if new_rows:
        await db.flush(new_rows)
        logger.info(
            "Seeded %d agent target config row(s).", len(new_rows)
        )
    else:
        logger.debug("Agent target configs already seeded.")
