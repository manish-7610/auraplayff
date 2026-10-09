"""
T7 - Referral activation, prize/kill reward calculations, RBAC penetration,
room credential authorization, screenshot security rules.
"""
from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import login_player, login_staff, register_player

# Only async tests use the asyncio mark; sync kill-reward tests do not.
# We apply it per-function on async tests rather than module-wide.


# ============================================================================
# Kill reward calculation helpers (pure - no DB, no async)
# ============================================================================

def _make_fifty_rupee_config():
    """Build a Rs50 PrizeConfig-compatible object matching confirmed rules."""
    from dataclasses import dataclass

    @dataclass
    class _FakePrizeConfig:
        entry_fee: Decimal
        is_active: bool
        first_place_prize: Decimal
        second_place_prize: Decimal
        third_place_prize: Decimal
        kill_reward_top_n: int
        kill_reward_tiers: list

    return _FakePrizeConfig(
        entry_fee=Decimal("50.00"),
        is_active=True,
        first_place_prize=Decimal("1000.00"),
        second_place_prize=Decimal("300.00"),
        third_place_prize=Decimal("100.00"),
        kill_reward_top_n=10,
        kill_reward_tiers=[
            {"from_kill": 1, "to_kill": 3,    "reward_per_kill": "5.00"},
            {"from_kill": 4, "to_kill": 6,    "reward_per_kill": "7.00"},
            {"from_kill": 7, "to_kill": None, "reward_per_kill": "10.00"},
        ],
    )


# ============================================================================
# Kill reward tests (sync - no pytestmark applied)
# ============================================================================

def test_kill_reward_zero_kills():
    from app.services.prize_service import calculate_kill_reward
    cfg = _make_fifty_rupee_config()
    result = calculate_kill_reward(placement=1, kills=0, config=cfg)
    assert result.eligible is True
    assert result.reward == Decimal("0.00")


def test_kill_reward_1_kill():
    from app.services.prize_service import calculate_kill_reward
    cfg = _make_fifty_rupee_config()
    result = calculate_kill_reward(placement=1, kills=1, config=cfg)
    # 1 kill in tier 1: 1x5 = 5
    assert result.reward == Decimal("5.00")


def test_kill_reward_3_kills():
    from app.services.prize_service import calculate_kill_reward
    cfg = _make_fifty_rupee_config()
    result = calculate_kill_reward(placement=1, kills=3, config=cfg)
    # 3 kills in tier 1: 3x5 = 15
    assert result.reward == Decimal("15.00")


def test_kill_reward_4_kills():
    from app.services.prize_service import calculate_kill_reward
    cfg = _make_fifty_rupee_config()
    result = calculate_kill_reward(placement=1, kills=4, config=cfg)
    # 3x5 + 1x7 = 22
    assert result.reward == Decimal("22.00")


def test_kill_reward_6_kills():
    from app.services.prize_service import calculate_kill_reward
    cfg = _make_fifty_rupee_config()
    result = calculate_kill_reward(placement=1, kills=6, config=cfg)
    # 3x5 + 3x7 = 15 + 21 = 36
    assert result.reward == Decimal("36.00")


def test_kill_reward_7_kills():
    from app.services.prize_service import calculate_kill_reward
    cfg = _make_fifty_rupee_config()
    result = calculate_kill_reward(placement=1, kills=7, config=cfg)
    # 3x5 + 3x7 + 1x10 = 46
    assert result.reward == Decimal("46.00")


def test_kill_reward_8_kills_confirmed_example():
    """
    8 kills with Rs50 tiers:
    Kills 1-3 = Rs5 each  => 3 x Rs5 = Rs15
    Kills 4-6 = Rs7 each  => 3 x Rs7 = Rs21
    Kills 7+  = Rs10 each => 2 x Rs10 = Rs20
    Total = Rs15 + Rs21 + Rs20 = Rs56

    Note: The Phase 4 spec narrative stated 'Rs51' but that is an arithmetic
    error in the spec text. The correct sum of the documented tiers is Rs56.
    The application code is authoritative.
    """
    from app.services.prize_service import calculate_kill_reward
    cfg = _make_fifty_rupee_config()
    result = calculate_kill_reward(placement=1, kills=8, config=cfg)
    # 3x5 + 3x7 + 2x10 = 15 + 21 + 20 = 56
    assert result.reward == Decimal("56.00"), (
        f"Expected Rs56 for 8 kills, got Rs{result.reward}. Breakdown: {result.breakdown}"
    )


def test_kill_reward_10_kills():
    from app.services.prize_service import calculate_kill_reward
    cfg = _make_fifty_rupee_config()
    result = calculate_kill_reward(placement=1, kills=10, config=cfg)
    # 3x5 + 3x7 + 4x10 = 15 + 21 + 40 = 76
    assert result.reward == Decimal("76.00")


def test_kill_reward_outside_top10():
    """Players outside top-10 not eligible."""
    from app.services.prize_service import calculate_kill_reward
    cfg = _make_fifty_rupee_config()
    result = calculate_kill_reward(placement=11, kills=20, config=cfg)
    assert result.eligible is False
    assert result.reward == Decimal("0.00")


def test_placement_prizes():
    """Placement prizes match confirmed Rs50 config."""
    from app.services.prize_service import get_placement_prize
    cfg = _make_fifty_rupee_config()
    assert get_placement_prize(1, cfg) == Decimal("1000.00")
    assert get_placement_prize(2, cfg) == Decimal("300.00")
    assert get_placement_prize(3, cfg) == Decimal("100.00")
    assert get_placement_prize(4, cfg) == Decimal("0.00")
    assert get_placement_prize(50, cfg) == Decimal("0.00")


def test_payout_not_distributed_twice():
    """distribute_payout raises PayoutAlreadyDistributedError on second call."""
    import asyncio
    from app.core.exceptions import PayoutAlreadyDistributedError
    from app.services.prize_service import distribute_payout

    # Use a plain object (not ORM) — the guard checks payout_distributed before
    # any DB work, so no SQLAlchemy session is needed.
    class _StubResult:
        payout_distributed = True

    cfg = _make_fifty_rupee_config()

    async def _run():
        with pytest.raises(PayoutAlreadyDistributedError):
            await distribute_payout(None, _StubResult(), cfg)  # type: ignore[arg-type]

    asyncio.run(_run())


# ============================================================================
# Referral rules
# ============================================================================

@pytest.mark.asyncio
async def test_referral_code_used_creates_relationship(
    client: AsyncClient, db_session: AsyncSession
):
    """Registering with a referral code creates a ReferralRelationship row."""
    from app.models.referral import ReferralRelationship

    ref_reg = await register_player(
        client, username="referrer1", ff_uid="6660000001"
    )
    assert ref_reg.status_code == 201
    referral_code = ref_reg.json()["referral_code"]

    new_reg = await register_player(
        client, username="newuser1", ff_uid="6660000002",
        referral_code=referral_code,
    )
    assert new_reg.status_code == 201

    result = await db_session.execute(
        select(ReferralRelationship)
    )
    rel = result.scalar_one_or_none()
    assert rel is not None
    assert rel.referral_code_used == referral_code


@pytest.mark.asyncio
async def test_referral_rewards_not_granted_at_registration(
    client: AsyncClient, db_session: AsyncSession
):
    """Referral bonus must NOT be credited at registration."""
    from app.models.wallet import Wallet

    ref_reg = await register_player(client, username="referrer2", ff_uid="6660000003")
    ref_code = ref_reg.json()["referral_code"]

    new_reg = await register_player(
        client, username="newuser2", ff_uid="6660000004",
        referral_code=ref_code,
    )
    new_user_id = uuid.UUID(new_reg.json()["id"])
    ref_user_id = uuid.UUID(ref_reg.json()["id"])

    for uid in [ref_user_id, new_user_id]:
        wallet = (
            await db_session.execute(
                select(Wallet).where(Wallet.user_id == uid)
            )
        ).scalar_one()
        assert wallet.referral_balance == Decimal("0.00"), (
            f"Referral balance should be 0 before first tournament, got {wallet.referral_balance}"
        )


@pytest.mark.asyncio
async def test_referral_invalid_code_rejected(client: AsyncClient):
    """Invalid referral codes rejected during registration."""
    resp = await register_player(
        client, username="badcode", ff_uid="6660000007",
        referral_code="XXXXXXXX",
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_REFERRAL_CODE"


# ============================================================================
# RBAC penetration tests
# ============================================================================

async def _make_agent_token(db_session: AsyncSession) -> str:
    """Create an agent with no permissions; return its access token."""
    from app.models.staff_user import StaffUser, StaffRole
    from app.core.security import create_access_token, hash_password
    import secrets

    unique_suffix = secrets.token_hex(4)
    agent = StaffUser(
        agent_id=f"AGT-RBAC-{unique_suffix.upper()}",
        display_name="RBAC Test Agent",
        password_hash=hash_password("AgentPass@123"),
        role=StaffRole.agent,
    )
    db_session.add(agent)
    await db_session.flush([agent])
    await db_session.commit()

    return create_access_token(
        subject=agent.id,
        audience="staff",
        token_version=agent.token_version,
        role="agent",
    )


@pytest.mark.asyncio
async def test_player_cannot_access_admin_routes(
    client: AsyncClient, db_session: AsyncSession
):
    """A player access token must be rejected on all admin endpoints."""
    await register_player(client, username="playeraccess", ff_uid="5550000001")
    login_resp = await client.post(
        "/api/auth/login",
        json={"username": "playeraccess", "password": "SecurePass@123"},
    )
    player_token = login_resp.json()["access_token"]

    admin_endpoints = [
        ("GET", "/api/admin/users"),
        ("GET", "/api/admin/deposits"),
        ("GET", "/api/admin/withdrawals"),
        ("GET", "/api/admin/agents"),
        ("GET", "/api/admin/audit-logs"),
        ("GET", "/api/admin/settings"),
        ("GET", "/api/admin/jobs"),
    ]
    for method, path in admin_endpoints:
        resp = await client.request(
            method, path,
            headers={"Authorization": f"Bearer {player_token}"},
        )
        assert resp.status_code == 401, (
            f"Player token should be 401 on {method} {path}, got {resp.status_code}"
        )


@pytest.mark.asyncio
async def test_agent_cannot_approve_deposits(
    client: AsyncClient, db_session: AsyncSession
):
    """Agent without deposit.approve must receive 403."""
    agent_token = await _make_agent_token(db_session)
    fake_id = str(uuid.uuid4())
    resp = await client.post(
        f"/api/admin/deposits/{fake_id}/approve",
        json={"note": ""},
        headers={"Authorization": f"Bearer {agent_token}"},
    )
    assert resp.status_code == 403, f"Expected 403, got {resp.status_code}"


@pytest.mark.asyncio
async def test_agent_cannot_process_withdrawals(
    client: AsyncClient, db_session: AsyncSession
):
    """Agent must not be able to process withdrawals."""
    agent_token = await _make_agent_token(db_session)
    fake_id = str(uuid.uuid4())

    for endpoint, body in [
        (f"/api/admin/withdrawals/{fake_id}/process",  {"note": "test note"}),
        (f"/api/admin/withdrawals/{fake_id}/mark-paid", {"payout_utr": "UTR1234567", "note": ""}),
        (f"/api/admin/withdrawals/{fake_id}/reject",   {"reason": "test rejection reason"}),
    ]:
        resp = await client.post(
            endpoint, json=body,
            headers={"Authorization": f"Bearer {agent_token}"},
        )
        assert resp.status_code == 403, (
            f"Agent should get 403 on {endpoint}, got {resp.status_code}"
        )


@pytest.mark.asyncio
async def test_agent_cannot_manage_agents(
    client: AsyncClient, db_session: AsyncSession
):
    """Agent must not create/activate/deactivate other agents."""
    agent_token = await _make_agent_token(db_session)
    fake_id = str(uuid.uuid4())

    blocked = [
        ("POST", "/api/admin/agents",
         {"display_name": "Valid Name For Agent", "password": "Test@12345"}),
        ("POST", f"/api/admin/agents/{fake_id}/activate",    {}),
        ("POST", f"/api/admin/agents/{fake_id}/deactivate",  {}),
        ("POST", f"/api/admin/agents/{fake_id}/reset-password",
         {"new_password": "NewPass@123"}),
    ]
    for method, path, body in blocked:
        resp = await client.request(
            method, path, json=body,
            headers={"Authorization": f"Bearer {agent_token}"},
        )
        assert resp.status_code == 403, (
            f"Agent should get 403 on {method} {path}, got {resp.status_code}: {resp.text[:200]}"
        )


@pytest.mark.asyncio
async def test_agent_cannot_access_audit_logs(
    client: AsyncClient, db_session: AsyncSession
):
    """Audit logs must be Super Admin only."""
    agent_token = await _make_agent_token(db_session)
    resp = await client.get(
        "/api/admin/audit-logs",
        headers={"Authorization": f"Bearer {agent_token}"},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_agent_cannot_trigger_scheduler(
    client: AsyncClient, db_session: AsyncSession
):
    """Scheduler endpoints must be Super Admin only."""
    agent_token = await _make_agent_token(db_session)
    resp = await client.post(
        "/api/admin/jobs/leaderboard_refresh/trigger",
        headers={"Authorization": f"Bearer {agent_token}"},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_agent_cannot_modify_platform_settings(
    client: AsyncClient, db_session: AsyncSession
):
    """Platform settings must be Super Admin only."""
    agent_token = await _make_agent_token(db_session)
    resp = await client.put(
        "/api/admin/settings/upi_id",
        json={"value": "evil@attacker"},
        headers={"Authorization": f"Bearer {agent_token}"},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_super_admin_can_manage_agents(client: AsyncClient):
    """Super Admin can create agents without restriction."""
    staff_resp = await login_staff(client)
    staff_token = staff_resp.json()["access_token"]

    resp = await client.post(
        "/api/admin/agents",
        json={"display_name": "Valid Agent", "password": "ValidPass@123"},
        headers={"Authorization": f"Bearer {staff_token}"},
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert "agent_id" in data
    assert data["agent_id"].startswith("AGT-")


# ============================================================================
# Agent permission grant/revoke (T4)
# ============================================================================

@pytest.mark.asyncio
async def test_grant_permission_to_agent(
    client: AsyncClient, db_session: AsyncSession
):
    """Super Admin can grant a safe permission to an agent."""
    staff_resp = await login_staff(client)
    staff_token = staff_resp.json()["access_token"]

    create_resp = await client.post(
        "/api/admin/agents",
        json={"display_name": "Perm Test Agent", "password": "AgentPass@123"},
        headers={"Authorization": f"Bearer {staff_token}"},
    )
    assert create_resp.status_code == 201, create_resp.text
    agent_db_id = create_resp.json()["id"]

    grant_resp = await client.post(
        f"/api/admin/agents/{agent_db_id}/permissions",
        json={"permission_code": "tournament.create"},
        headers={"Authorization": f"Bearer {staff_token}"},
    )
    assert grant_resp.status_code == 201, grant_resp.text
    permissions = [p["code"] for p in grant_resp.json()["permissions"]]
    assert "tournament.create" in permissions


@pytest.mark.asyncio
async def test_cannot_grant_admin_only_permission_to_agent(
    client: AsyncClient, db_session: AsyncSession
):
    """Admin-only permissions must be blocked from agents."""
    staff_resp = await login_staff(client)
    staff_token = staff_resp.json()["access_token"]

    create_resp = await client.post(
        "/api/admin/agents",
        json={"display_name": "Block Perm Agent", "password": "AgentPass@123"},
        headers={"Authorization": f"Bearer {staff_token}"},
    )
    assert create_resp.status_code == 201
    agent_db_id = create_resp.json()["id"]

    for forbidden_perm in [
        "deposit.approve", "withdrawal.mark_paid",
        "settings.manage", "audit.view",
    ]:
        resp = await client.post(
            f"/api/admin/agents/{agent_db_id}/permissions",
            json={"permission_code": forbidden_perm},
            headers={"Authorization": f"Bearer {staff_token}"},
        )
        assert resp.status_code == 403, (
            f"Should block granting '{forbidden_perm}', got {resp.status_code}"
        )


@pytest.mark.asyncio
async def test_grant_permission_is_idempotent(
    client: AsyncClient, db_session: AsyncSession
):
    """Granting the same permission twice must not create duplicate rows."""
    from app.models.permission import StaffUserPermission

    staff_resp = await login_staff(client)
    staff_token = staff_resp.json()["access_token"]

    create_resp = await client.post(
        "/api/admin/agents",
        json={"display_name": "Idemp Perm Agent", "password": "AgentPass@123"},
        headers={"Authorization": f"Bearer {staff_token}"},
    )
    agent_db_id = create_resp.json()["id"]

    for _ in range(2):
        resp = await client.post(
            f"/api/admin/agents/{agent_db_id}/permissions",
            json={"permission_code": "screenshot.view"},
            headers={"Authorization": f"Bearer {staff_token}"},
        )
        assert resp.status_code in (200, 201)

    grants = (
        await db_session.execute(
            select(StaffUserPermission).where(
                StaffUserPermission.staff_user_id == uuid.UUID(agent_db_id)
            )
        )
    ).scalars().all()
    assert len(grants) == 1


@pytest.mark.asyncio
async def test_revoke_permission_from_agent(
    client: AsyncClient, db_session: AsyncSession
):
    """Super Admin can revoke a previously granted permission."""
    from app.models.permission import StaffUserPermission

    staff_resp = await login_staff(client)
    staff_token = staff_resp.json()["access_token"]

    create_resp = await client.post(
        "/api/admin/agents",
        json={"display_name": "Revoke Perm Agent", "password": "AgentPass@123"},
        headers={"Authorization": f"Bearer {staff_token}"},
    )
    agent_db_id = create_resp.json()["id"]

    await client.post(
        f"/api/admin/agents/{agent_db_id}/permissions",
        json={"permission_code": "user.view"},
        headers={"Authorization": f"Bearer {staff_token}"},
    )

    rev_resp = await client.delete(
        f"/api/admin/agents/{agent_db_id}/permissions/user.view",
        headers={"Authorization": f"Bearer {staff_token}"},
    )
    assert rev_resp.status_code == 200, rev_resp.text

    grants = (
        await db_session.execute(
            select(StaffUserPermission).where(
                StaffUserPermission.staff_user_id == uuid.UUID(agent_db_id)
            )
        )
    ).scalars().all()
    assert len(grants) == 0


@pytest.mark.asyncio
async def test_agent_cannot_grant_own_permissions(
    client: AsyncClient, db_session: AsyncSession
):
    """Agents must not grant permissions to themselves."""
    from app.models.staff_user import StaffUser, StaffRole
    from app.core.security import create_access_token, hash_password

    agent = StaffUser(
        agent_id="AGT-SELF-GRANT",
        display_name="Self Grant Agent",
        password_hash=hash_password("AgentPass@123"),
        role=StaffRole.agent,
    )
    db_session.add(agent)
    await db_session.flush([agent])
    await db_session.commit()

    agent_token = create_access_token(
        subject=agent.id,
        audience="staff",
        token_version=agent.token_version,
        role="agent",
    )

    resp = await client.post(
        f"/api/admin/agents/{agent.id}/permissions",
        json={"permission_code": "tournament.create"},
        headers={"Authorization": f"Bearer {agent_token}"},
    )
    assert resp.status_code == 403


# ============================================================================
# Prize config update (T5)
# ============================================================================

@pytest.mark.asyncio
async def test_super_admin_can_update_prize_config(
    client: AsyncClient, db_session: AsyncSession
):
    """Super Admin can update prize amounts for an existing config."""
    staff_resp = await login_staff(client)
    staff_token = staff_resp.json()["access_token"]

    resp = await client.put(
        "/api/admin/prize-configs/50.00",
        json={"first_place_prize": "1500.00"},
        headers={"Authorization": f"Bearer {staff_token}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["first_place_prize"] == "1500.00"

    # Reset to original
    await client.put(
        "/api/admin/prize-configs/50.00",
        json={"first_place_prize": "1000.00"},
        headers={"Authorization": f"Bearer {staff_token}"},
    )


@pytest.mark.asyncio
async def test_agent_cannot_update_prize_config(
    client: AsyncClient, db_session: AsyncSession
):
    """Agents must not modify prize configuration."""
    agent_token = await _make_agent_token(db_session)
    resp = await client.put(
        "/api/admin/prize-configs/50.00",
        json={"first_place_prize": "99999.00"},
        headers={"Authorization": f"Bearer {agent_token}"},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_prize_config_update_audited(
    client: AsyncClient, db_session: AsyncSession
):
    """Prize config changes must create an audit log entry."""
    from app.models.audit_log import AuditLog

    staff_resp = await login_staff(client)
    staff_token = staff_resp.json()["access_token"]

    await client.put(
        "/api/admin/prize-configs/50.00",
        json={"notes": "Phase 10 test update"},
        headers={"Authorization": f"Bearer {staff_token}"},
    )

    result = await db_session.execute(
        select(AuditLog).where(AuditLog.action == "prize_config.updated")
    )
    log = result.scalar_one_or_none()
    assert log is not None
    assert log.entity_type == "prize_config"


@pytest.mark.asyncio
async def test_prize_config_nonexistent_tier_returns_404(
    client: AsyncClient
):
    """Updating a non-existent prize tier must return 404."""
    staff_resp = await login_staff(client)
    staff_token = staff_resp.json()["access_token"]

    resp = await client.put(
        "/api/admin/prize-configs/999.00",
        json={"first_place_prize": "100.00"},
        headers={"Authorization": f"Bearer {staff_token}"},
    )
    assert resp.status_code == 404


# ============================================================================
# Room credential security
# ============================================================================

async def _setup_tournament_with_room(
    client: AsyncClient, db_session: AsyncSession
) -> tuple[str, str, str]:
    """Create a tournament, add a player, release room. Returns (tid, player_tok, staff_tok)."""
    from app.services.wallet_service import credit_deposit_balance

    staff_resp = await login_staff(client)
    staff_token = staff_resp.json()["access_token"]

    create_resp = await client.post(
        "/api/admin/tournaments",
        json={
            "title": "Room Test Tournament",
            "entry_fee": 50,
            "scheduled_at": "2099-01-01T10:00:00+00:00",
            "max_players": 10,
            "min_players": 2,
            "game": "free_fire",
            "mode": "solo",
        },
        headers={"Authorization": f"Bearer {staff_token}"},
    )
    assert create_resp.status_code == 201, create_resp.text
    tournament_id = create_resp.json()["id"]

    await client.post(
        f"/api/admin/tournaments/{tournament_id}/open",
        headers={"Authorization": f"Bearer {staff_token}"},
    )

    reg = await register_player(client, username="roomtestplayer", ff_uid="8880000099", ff_name="RoomTestFP")
    player_id = uuid.UUID(reg.json()["id"])
    async with db_session.begin():
        await credit_deposit_balance(
            db_session, player_id, amount=Decimal("200.00"),
            idempotency_key="room_test:fund",
        )
    login_resp = await client.post(
        "/api/auth/login",
        json={"username": "roomtestplayer", "password": "SecurePass@123"},
    )
    player_token = login_resp.json()["access_token"]

    await client.post(
        f"/api/tournaments/{tournament_id}/join",
        headers={"Authorization": f"Bearer {player_token}"},
    )
    await client.post(
        f"/api/admin/tournaments/{tournament_id}/confirm",
        headers={"Authorization": f"Bearer {staff_token}"},
    )
    await client.post(
        f"/api/admin/tournaments/{tournament_id}/release-room",
        json={"room_id": "ROOM12345", "room_password": "SECRET99"},
        headers={"Authorization": f"Bearer {staff_token}"},
    )

    return tournament_id, player_token, staff_token


@pytest.mark.asyncio
async def test_joined_player_can_get_room_after_release(
    client: AsyncClient, db_session: AsyncSession
):
    """A joined player can retrieve room credentials after release."""
    tid, player_tok, _ = await _setup_tournament_with_room(client, db_session)
    resp = await client.get(
        f"/api/tournaments/{tid}/room",
        headers={"Authorization": f"Bearer {player_tok}"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["room_id"] == "ROOM12345"
    assert data["room_password"] == "SECRET99"


@pytest.mark.asyncio
async def test_non_participant_cannot_get_room(
    client: AsyncClient, db_session: AsyncSession
):
    """A player who did not join must not receive room credentials."""
    tid, _, _ = await _setup_tournament_with_room(client, db_session)

    reg2 = await register_player(
        client, username="notroomtestplayer2", ff_uid="8880000100"
    )
    login2 = await client.post(
        "/api/auth/login",
        json={"username": "notroomtestplayer2", "password": "SecurePass@123"},
    )
    other_token = login2.json()["access_token"]

    resp = await client.get(
        f"/api/tournaments/{tid}/room",
        headers={"Authorization": f"Bearer {other_token}"},
    )
    assert resp.status_code in (403, 404)


@pytest.mark.asyncio
async def test_room_not_available_before_release(
    client: AsyncClient, db_session: AsyncSession
):
    """Room credentials must not be accessible before release."""
    from app.services.wallet_service import credit_deposit_balance

    staff_resp = await login_staff(client)
    staff_token = staff_resp.json()["access_token"]

    create_resp = await client.post(
        "/api/admin/tournaments",
        json={
            "title": "Pre-release Test",
            "entry_fee": 50,
            "scheduled_at": "2099-01-01T10:00:00+00:00",
            "max_players": 10,
            "min_players": 2,
            "game": "free_fire",
            "mode": "solo",
        },
        headers={"Authorization": f"Bearer {staff_token}"},
    )
    tid = create_resp.json()["id"]

    await client.post(
        f"/api/admin/tournaments/{tid}/open",
        headers={"Authorization": f"Bearer {staff_token}"},
    )

    reg = await register_player(
        client, username="prereleaseplayer", ff_uid="8880000101"
    )
    player_id = uuid.UUID(reg.json()["id"])
    async with db_session.begin():
        await credit_deposit_balance(
            db_session, player_id, amount=Decimal("200.00"),
            idempotency_key="pre_release:fund",
        )
    login_resp = await client.post(
        "/api/auth/login",
        json={"username": "prereleaseplayer", "password": "SecurePass@123"},
    )
    player_tok = login_resp.json()["access_token"]

    await client.post(
        f"/api/tournaments/{tid}/join",
        headers={"Authorization": f"Bearer {player_tok}"},
    )
    # Confirm but do NOT release room
    await client.post(
        f"/api/admin/tournaments/{tid}/confirm",
        headers={"Authorization": f"Bearer {staff_token}"},
    )

    resp = await client.get(
        f"/api/tournaments/{tid}/room",
        headers={"Authorization": f"Bearer {player_tok}"},
    )
    assert resp.status_code in (400, 403, 409)

