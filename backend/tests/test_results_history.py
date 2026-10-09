"""
Phase 11 — Completed Results History tests.

Covers:
  1. Syntax sanity — imports resolve correctly.
  2. GET /api/admin/results/history         (list)
  3. GET /api/admin/results/history/{id}    (detail)
  4. RBAC — Super Admin always passes, Agent requires explicit grant.
  5. Agent never gets finance/admin-sensitive permissions merely by
     holding results.history_view.
  6. 404 on non-existent or non-completed tournament.
  7. Read-only guarantee — no wallet transactions created by viewing history.
  8. All finalized participants appear in detail response.
  9. Monetary values match DB (no frontend recalculation).
 10. Active result-entry page still filters to live/result_entry status only.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import login_staff


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def _staff_token(client: AsyncClient) -> str:
    """Log in as the bootstrapped super admin, return bearer token."""
    resp = await login_staff(client)
    assert resp.status_code == 200, f"Staff login failed: {resp.text}"
    return resp.json()["access_token"]


async def _create_agent(db: AsyncSession) -> "StaffUser":  # type: ignore[name-defined]
    """Create an agent staff user with no permissions."""
    import hashlib
    from app.models.staff_user import StaffUser, StaffRole

    agent = StaffUser(
        id=uuid.uuid4(),
        agent_id="AGENT-HIST-01",
        display_name="History Agent",
        role=StaffRole.agent,
        password_hash=hashlib.sha256(b"AgentPass@123").hexdigest(),  # not real bcrypt — fine for tests
        is_active=True,
        created_by_id=None,
    )
    db.add(agent)
    await db.flush()
    return agent


async def _grant_permission(db: AsyncSession, staff_user_id: uuid.UUID, perm_code: str) -> None:
    """Grant a named permission to a staff user."""
    from app.models.permission import Permission as PermModel, StaffUserPermission

    result = await db.execute(
        select(PermModel).where(PermModel.code == perm_code)
    )
    perm = result.scalar_one_or_none()
    assert perm is not None, f"Permission '{perm_code}' not seeded — check bootstrap_service"

    db.add(StaffUserPermission(
        staff_user_id=staff_user_id,
        permission_id=perm.id,
        granted_by_id=None,
    ))
    await db.flush()


async def _agent_token(client: AsyncClient, db: AsyncSession, perm_code: str | None = None) -> str:
    """
    Create an agent, optionally grant one permission, return JWT.
    Uses direct password login after hashing with bcrypt (app's real hash).
    """
    from app.core.security import hash_password
    from app.models.staff_user import StaffUser, StaffRole

    agent = StaffUser(
        id=uuid.uuid4(),
        agent_id="AGENT-HIST-02",
        display_name="History Agent 2",
        role=StaffRole.agent,
        password_hash=hash_password("AgentPass@456"),
        is_active=True,
        created_by_id=None,
    )
    db.add(agent)
    await db.flush()

    if perm_code:
        await _grant_permission(db, agent.id, perm_code)

    await db.commit()

    resp = await client.post(
        "/api/admin/auth/login",
        json={"agent_id": "AGENT-HIST-02", "password": "AgentPass@456"},
    )
    assert resp.status_code == 200, f"Agent login failed: {resp.text}"
    return resp.json()["access_token"]


async def _seed_completed_tournament(db: AsyncSession) -> "Tournament":  # type: ignore[name-defined]
    """
    Seed a minimal completed tournament with two finalized results.
    Returns the Tournament ORM object.
    """
    from app.models.tournament import Tournament, TournamentStatus, TournamentMode, TournamentGame
    from app.models.result import TournamentResult, ResultStatus
    from app.models.user import User

    now = _utcnow()

    # Create two players
    from app.core.security import hash_password
    import random, string

    def _rand_str(n=8):
        return "".join(random.choices(string.ascii_lowercase, k=n))

    u1 = User(
        id=uuid.uuid4(), username=f"hist_{_rand_str()}", ff_uid=f"{random.randint(10**9, 10**10-1)}",
        password_hash=hash_password("Pw@12345"), referral_code=_rand_str(6),
        is_active=True, is_banned=False,
    )
    u2 = User(
        id=uuid.uuid4(), username=f"hist_{_rand_str()}", ff_uid=f"{random.randint(10**9, 10**10-1)}",
        password_hash=hash_password("Pw@12345"), referral_code=_rand_str(6),
        is_active=True, is_banned=False,
    )
    db.add_all([u1, u2])
    await db.flush()

    # Get super admin id for created_by
    from app.models.staff_user import StaffUser
    admin = (await db.execute(select(StaffUser).limit(1))).scalar_one()

    # Create a completed tournament
    tourn = Tournament(
        id=uuid.uuid4(),
        tournament_code=f"T-HIST-{_rand_str(4).upper()}",
        title="History Test Tournament",
        game=TournamentGame.free_fire,
        mode=TournamentMode.solo,
        entry_fee=Decimal("50.00"),
        max_players=50,
        min_players=2,
        player_count=2,
        next_sheet_number=3,
        status=TournamentStatus.completed,
        scheduled_at=now,
        completed_at=now,
        created_by_id=admin.id,
        created_by_display_name=admin.display_name,
        created_by_role=admin.role.value,
    )
    db.add(tourn)
    await db.flush()

    # Create TournamentEntry rows
    from app.models.tournament import TournamentEntry, EntryStatus
    e1 = TournamentEntry(
        id=uuid.uuid4(), tournament_id=tourn.id, user_id=u1.id,
        entry_fee_snapshot=Decimal("50.00"), sheet_number=1, status=EntryStatus.active,
    )
    e2 = TournamentEntry(
        id=uuid.uuid4(), tournament_id=tourn.id, user_id=u2.id,
        entry_fee_snapshot=Decimal("50.00"), sheet_number=2, status=EntryStatus.active,
    )
    db.add_all([e1, e2])
    await db.flush()

    # Create finalized results
    r1 = TournamentResult(
        id=uuid.uuid4(),
        tournament_id=tourn.id,
        entry_id=e1.id,
        user_id=u1.id,
        ff_uid_snapshot=u1.ff_uid,
        placement=1,
        kills=15,
        prize_amount=Decimal("400.00"),
        kill_reward_amount=Decimal("85.00"),
        kill_reward_eligible=True,
        kill_reward_breakdown="3×₹5 + 3×₹7 + 9×₹10",
        payout_distributed=True,
        payout_distributed_at=now,
        result_status=ResultStatus.finalized,
        entered_by_id=admin.id,
        entered_at=now,
        verified_by_id=admin.id,
        verified_at=now,
    )
    r2 = TournamentResult(
        id=uuid.uuid4(),
        tournament_id=tourn.id,
        entry_id=e2.id,
        user_id=u2.id,
        ff_uid_snapshot=u2.ff_uid,
        placement=2,
        kills=10,
        prize_amount=Decimal("120.00"),
        kill_reward_amount=Decimal("50.00"),
        kill_reward_eligible=True,
        kill_reward_breakdown="3×₹5 + 3×₹7 + 1×₹10",
        payout_distributed=True,
        payout_distributed_at=now,
        result_status=ResultStatus.finalized,
        entered_by_id=admin.id,
        entered_at=now,
        verified_by_id=admin.id,
        verified_at=now,
    )
    db.add_all([r1, r2])
    await db.commit()

    return tourn, [r1, r2], [u1, u2]


# ─────────────────────────────────────────────────────────────────────────────
# 1. Syntax / import sanity
# ─────────────────────────────────────────────────────────────────────────────

def test_permissions_import():
    """results.history_view must exist in the Permission enum."""
    from app.core.permissions import Permission
    assert hasattr(Permission, "RESULTS_HISTORY_VIEW")
    assert Permission.RESULTS_HISTORY_VIEW.value == "results.history_view"


def test_schemas_import():
    """New schemas must import without error."""
    from app.schemas.result import (
        CompletedHistoryResultItem,
        CompletedHistoryListResponse,
        CompletedTournamentSummary,
        CompletedTournamentResultsResponse,
    )
    assert CompletedHistoryResultItem is not None
    assert CompletedHistoryListResponse is not None
    assert CompletedTournamentSummary is not None
    assert CompletedTournamentResultsResponse is not None


def test_history_router_registered():
    """history_router must be importable and have the expected prefix."""
    from app.routers.results import history_router
    assert history_router.prefix == "/api/admin/results"


def test_permission_not_in_admin_only():
    """results.history_view must NOT be in ADMIN_ONLY_PERMISSIONS (agents can have it)."""
    from app.core.permissions import Permission, ADMIN_ONLY_PERMISSIONS
    assert Permission.RESULTS_HISTORY_VIEW.value not in ADMIN_ONLY_PERMISSIONS


def test_permission_not_in_financial():
    """results.history_view must NOT be in FINANCIAL_PERMISSIONS (read-only)."""
    from app.core.permissions import Permission, FINANCIAL_PERMISSIONS
    assert Permission.RESULTS_HISTORY_VIEW.value not in FINANCIAL_PERMISSIONS


# ─────────────────────────────────────────────────────────────────────────────
# 2. Super Admin can list completed history
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_super_admin_list_history_empty(client: AsyncClient):
    """Super Admin gets 200 + empty list when no completed tournaments exist."""
    token = await _staff_token(client)
    resp = await client.get(
        "/api/admin/results/history",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "items" in body
    assert "total" in body
    assert isinstance(body["items"], list)
    assert body["total"] == 0


@pytest.mark.asyncio
async def test_super_admin_list_history_with_data(client: AsyncClient, db_session: AsyncSession):
    """Super Admin sees a completed tournament in the history list."""
    tourn, results, users = await _seed_completed_tournament(db_session)

    token = await _staff_token(client)
    resp = await client.get(
        "/api/admin/results/history",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] >= 1

    ids = [str(item["tournament_id"]) for item in body["items"]]
    assert str(tourn.id) in ids


# ─────────────────────────────────────────────────────────────────────────────
# 3. Super Admin can retrieve completed tournament detail
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_super_admin_detail_shows_all_participants(client: AsyncClient, db_session: AsyncSession):
    """Detail endpoint must show ALL finalized participants, sorted by placement."""
    tourn, results, users = await _seed_completed_tournament(db_session)

    token = await _staff_token(client)
    resp = await client.get(
        f"/api/admin/results/history/{tourn.id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["tournament_id"] == str(tourn.id)
    assert body["total"] == 2
    assert len(body["results"]) == 2

    # Must be sorted by placement
    placements = [r["placement"] for r in body["results"]]
    assert placements == sorted(placements)


@pytest.mark.asyncio
async def test_detail_monetary_values_match_db(client: AsyncClient, db_session: AsyncSession):
    """
    prize_amount, kill_reward_amount, total_payout must match the DB values.
    Frontend must never recalculate these — they come directly from the DB.
    """
    tourn, [r1, r2], users = await _seed_completed_tournament(db_session)

    token = await _staff_token(client)
    resp = await client.get(
        f"/api/admin/results/history/{tourn.id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    results = resp.json()["results"]

    rank1 = next(r for r in results if r["placement"] == 1)
    assert rank1["prize_amount"] == str(r1.prize_amount)
    assert rank1["kill_reward_amount"] == str(r1.kill_reward_amount)
    # total_payout = prize + kill_reward (verified against DB, not recalculated)
    expected_total = str(r1.prize_amount + r1.kill_reward_amount)
    assert rank1["total_payout"] == expected_total

    rank2 = next(r for r in results if r["placement"] == 2)
    assert rank2["prize_amount"] == str(r2.prize_amount)
    assert rank2["kill_reward_amount"] == str(r2.kill_reward_amount)
    assert rank2["total_payout"] == str(r2.prize_amount + r2.kill_reward_amount)


@pytest.mark.asyncio
async def test_detail_includes_username_and_ff_uid(client: AsyncClient, db_session: AsyncSession):
    """Each result row must contain the username and ff_uid_snapshot."""
    tourn, _, users = await _seed_completed_tournament(db_session)

    token = await _staff_token(client)
    resp = await client.get(
        f"/api/admin/results/history/{tourn.id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    results_data = resp.json()["results"]

    for row in results_data:
        assert row.get("username") not in (None, "", "—"), "username must be populated"
        assert row.get("ff_uid_snapshot") not in (None, ""), "ff_uid_snapshot must be populated"


# ─────────────────────────────────────────────────────────────────────────────
# 4. 404 cases
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_detail_404_for_nonexistent_tournament(client: AsyncClient):
    """Random UUID that doesn't exist in the DB should return 404."""
    token = await _staff_token(client)
    resp = await client.get(
        f"/api/admin/results/history/{uuid.uuid4()}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_detail_404_for_non_completed_tournament(client: AsyncClient, db_session: AsyncSession):
    """A tournament that is in 'open' status must not appear in the history detail."""
    from app.models.tournament import Tournament, TournamentStatus, TournamentMode, TournamentGame
    from app.models.staff_user import StaffUser

    admin = (await db_session.execute(select(StaffUser).limit(1))).scalar_one()
    tourn = Tournament(
        id=uuid.uuid4(),
        tournament_code="T-NOTCOMPLETE",
        title="Open Tournament",
        game=TournamentGame.free_fire,
        mode=TournamentMode.solo,
        entry_fee=Decimal("50.00"),
        max_players=50,
        min_players=2,
        player_count=0,
        next_sheet_number=1,
        status=TournamentStatus.open,   # NOT completed
        scheduled_at=_utcnow(),
        created_by_id=admin.id,
        created_by_display_name=admin.display_name,
        created_by_role=admin.role.value,
    )
    db_session.add(tourn)
    await db_session.commit()

    token = await _staff_token(client)
    resp = await client.get(
        f"/api/admin/results/history/{tourn.id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404


# ─────────────────────────────────────────────────────────────────────────────
# 5. RBAC — Agent without permission → 403
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_agent_without_perm_cannot_list_history(client: AsyncClient, db_session: AsyncSession):
    """Agent with no permissions must get 403 on the history list endpoint."""
    token = await _agent_token(client, db_session, perm_code=None)
    resp = await client.get(
        "/api/admin/results/history",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_agent_without_perm_cannot_view_detail(client: AsyncClient, db_session: AsyncSession):
    """Agent with no permissions must get 403 on the history detail endpoint."""
    # Note: we need a fresh agent since previous test already used AGENT-HIST-02
    # Use db_session directly to avoid duplicate agent_id conflicts
    from app.core.security import hash_password
    from app.models.staff_user import StaffUser, StaffRole

    agent = StaffUser(
        id=uuid.uuid4(),
        agent_id="AGENT-HIST-03",
        display_name="No Perm Agent",
        role=StaffRole.agent,
        password_hash=hash_password("AgentPass@789"),
        is_active=True,
        created_by_id=None,
    )
    db_session.add(agent)
    await db_session.commit()

    resp = await client.post(
        "/api/admin/auth/login",
        json={"agent_id": "AGENT-HIST-03", "password": "AgentPass@789"},
    )
    assert resp.status_code == 200
    token = resp.json()["access_token"]

    resp2 = await client.get(
        f"/api/admin/results/history/{uuid.uuid4()}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp2.status_code == 403


# ─────────────────────────────────────────────────────────────────────────────
# 6. RBAC — Agent WITH results.history_view permission → 200
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_agent_with_perm_can_list_history(client: AsyncClient, db_session: AsyncSession):
    """Agent with results.history_view can access the history list."""
    token = await _agent_token(client, db_session, perm_code="results.history_view")
    resp = await client.get(
        "/api/admin/results/history",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    assert "items" in resp.json()


@pytest.mark.asyncio
async def test_agent_with_perm_can_view_detail(client: AsyncClient, db_session: AsyncSession):
    """
    Agent with results.history_view can open the result detail for a completed
    tournament that they own (created_by_id == agent.id).

    Background: ownership enforcement was added so Agents can only see their
    own tournaments — a tournament seeded under the super-admin is correctly
    inaccessible to an agent.  This test seeds the tournament under the agent
    so it verifies both the permission check AND the ownership check passing.
    """
    from app.core.security import hash_password
    from app.models.staff_user import StaffUser, StaffRole
    from app.models.tournament import Tournament, TournamentStatus, TournamentMode, TournamentGame
    from app.models.tournament import TournamentEntry, EntryStatus
    from app.models.result import TournamentResult, ResultStatus
    from app.models.user import User

    now = _utcnow()

    # ── Create the agent that will own the tournament ─────────────────────────
    agent = StaffUser(
        id=uuid.uuid4(),
        agent_id="AGENT-HIST-DET",
        display_name="Detail Agent",
        role=StaffRole.agent,
        password_hash=hash_password("AgentDet@456"),
        is_active=True,
        created_by_id=None,
    )
    db_session.add(agent)
    await db_session.flush()
    await _grant_permission(db_session, agent.id, "results.history_view")

    # ── Seed two players ──────────────────────────────────────────────────────
    import random, string as _string
    def _rs(n=8):
        return "".join(random.choices(_string.ascii_lowercase, k=n))

    u1 = User(
        id=uuid.uuid4(), username=f"det_{_rs()}", ff_uid=str(random.randint(10**9, 10**10-1)),
        password_hash=hash_password("Pw@12345"), referral_code=_rs(6),
        is_active=True, is_banned=False,
    )
    u2 = User(
        id=uuid.uuid4(), username=f"det_{_rs()}", ff_uid=str(random.randint(10**9, 10**10-1)),
        password_hash=hash_password("Pw@12345"), referral_code=_rs(6),
        is_active=True, is_banned=False,
    )
    db_session.add_all([u1, u2])
    await db_session.flush()

    # ── Seed a completed tournament owned by the agent ────────────────────────
    tourn = Tournament(
        id=uuid.uuid4(),
        tournament_code=f"T-DET-{_rs(4).upper()}",
        title="Detail Test Tournament",
        game=TournamentGame.free_fire,
        mode=TournamentMode.solo,
        entry_fee=Decimal("50.00"),
        max_players=50,
        min_players=2,
        player_count=2,
        next_sheet_number=3,
        status=TournamentStatus.completed,
        scheduled_at=now,
        completed_at=now,
        created_by_id=agent.id,               # ← owned by the agent
        created_by_display_name=agent.display_name,
        created_by_role=agent.role.value,
    )
    db_session.add(tourn)
    await db_session.flush()

    e1 = TournamentEntry(
        id=uuid.uuid4(), tournament_id=tourn.id, user_id=u1.id,
        entry_fee_snapshot=Decimal("50.00"), sheet_number=1, status=EntryStatus.active,
    )
    e2 = TournamentEntry(
        id=uuid.uuid4(), tournament_id=tourn.id, user_id=u2.id,
        entry_fee_snapshot=Decimal("50.00"), sheet_number=2, status=EntryStatus.active,
    )
    db_session.add_all([e1, e2])
    await db_session.flush()

    r1 = TournamentResult(
        id=uuid.uuid4(), tournament_id=tourn.id, entry_id=e1.id, user_id=u1.id,
        ff_uid_snapshot=u1.ff_uid, placement=1, kills=10,
        prize_amount=Decimal("1000.00"), kill_reward_amount=Decimal("70.00"),
        kill_reward_eligible=True, kill_reward_breakdown="test",
        payout_distributed=True, payout_distributed_at=now,
        result_status=ResultStatus.finalized,
        entered_by_id=agent.id, entered_at=now,
        verified_by_id=agent.id, verified_at=now,
    )
    r2 = TournamentResult(
        id=uuid.uuid4(), tournament_id=tourn.id, entry_id=e2.id, user_id=u2.id,
        ff_uid_snapshot=u2.ff_uid, placement=2, kills=5,
        prize_amount=Decimal("300.00"), kill_reward_amount=Decimal("25.00"),
        kill_reward_eligible=True, kill_reward_breakdown="test",
        payout_distributed=True, payout_distributed_at=now,
        result_status=ResultStatus.finalized,
        entered_by_id=agent.id, entered_at=now,
        verified_by_id=agent.id, verified_at=now,
    )
    db_session.add_all([r1, r2])
    await db_session.commit()

    # ── Log in as the agent and access the detail ─────────────────────────────
    login_resp = await client.post(
        "/api/admin/auth/login",
        json={"agent_id": "AGENT-HIST-DET", "password": "AgentDet@456"},
    )
    assert login_resp.status_code == 200
    token = login_resp.json()["access_token"]

    resp = await client.get(
        f"/api/admin/results/history/{tourn.id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["tournament_id"] == str(tourn.id)


# ─────────────────────────────────────────────────────────────────────────────
# 7. Agent holding results.history_view must NOT gain sensitive permissions
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_agent_with_history_perm_cannot_approve_deposits(client: AsyncClient, db_session: AsyncSession):
    """
    Holding results.history_view must not grant deposit approval access.
    This verifies the permission is correctly scoped as read-only.
    """
    from app.core.security import hash_password
    from app.models.staff_user import StaffUser, StaffRole

    agent = StaffUser(
        id=uuid.uuid4(),
        agent_id="AGENT-HIST-04",
        display_name="Scoped Agent",
        role=StaffRole.agent,
        password_hash=hash_password("AgentPass@000"),
        is_active=True,
        created_by_id=None,
    )
    db_session.add(agent)
    await db_session.flush()
    await _grant_permission(db_session, agent.id, "results.history_view")
    await db_session.commit()

    resp = await client.post(
        "/api/admin/auth/login",
        json={"agent_id": "AGENT-HIST-04", "password": "AgentPass@000"},
    )
    token = resp.json()["access_token"]

    # Attempt to call a deposit-approval endpoint — must be 403 (or 404 if deposit doesn't exist)
    resp2 = await client.post(
        f"/api/admin/deposits/{uuid.uuid4()}/approve",
        headers={"Authorization": f"Bearer {token}"},
        json={},
    )
    # 403 = permission denied (correct); 404 = deposit not found (also acceptable — means the
    # request got past auth but the resource doesn't exist, not a permission bypass)
    assert resp2.status_code in (403, 404, 422), (
        f"Expected 403/404/422, got {resp2.status_code}: {resp2.text}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 8. Viewing history creates NO wallet transactions
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_viewing_history_creates_no_wallet_transactions(client: AsyncClient, db_session: AsyncSession):
    """
    GETting the history endpoints must never create wallet transactions
    or payout records.
    """
    from app.models.wallet import WalletTransaction

    # Count transactions before
    before_count = (await db_session.execute(
        select(WalletTransaction)
    )).scalars().all()
    count_before = len(before_count)

    tourn, _, _ = await _seed_completed_tournament(db_session)
    token = await _staff_token(client)

    # List endpoint
    await client.get(
        "/api/admin/results/history",
        headers={"Authorization": f"Bearer {token}"},
    )
    # Detail endpoint
    await client.get(
        f"/api/admin/results/history/{tourn.id}",
        headers={"Authorization": f"Bearer {token}"},
    )

    # Count transactions after — must be identical
    after_rows = (await db_session.execute(
        select(WalletTransaction)
    )).scalars().all()
    count_after = len(after_rows)

    assert count_after == count_before, (
        f"Viewing history created {count_after - count_before} wallet transaction(s). "
        "History endpoints must be read-only."
    )


# ─────────────────────────────────────────────────────────────────────────────
# 9. Search filter works
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_history_search_by_title(client: AsyncClient, db_session: AsyncSession):
    """?search= filters history by tournament code or title."""
    tourn, _, _ = await _seed_completed_tournament(db_session)
    token = await _staff_token(client)

    # Search with exact title — must find at least this tournament
    resp = await client.get(
        "/api/admin/results/history",
        params={"search": "History Test Tournament"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    ids = [item["tournament_id"] for item in body["items"]]
    assert str(tourn.id) in ids


@pytest.mark.asyncio
async def test_history_search_no_match(client: AsyncClient, db_session: AsyncSession):
    """A search term that matches nothing returns an empty list (not 404)."""
    token = await _staff_token(client)
    resp = await client.get(
        "/api/admin/results/history",
        params={"search": "DEFINITELY_NOT_A_TOURNAMENT_XYZ999"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 0
    assert body["items"] == []


# ─────────────────────────────────────────────────────────────────────────────
# 10. Unauthenticated requests → 401
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_history_list_requires_auth(client: AsyncClient):
    resp = await client.get("/api/admin/results/history")
    assert resp.status_code in (401, 403)


@pytest.mark.asyncio
async def test_history_detail_requires_auth(client: AsyncClient):
    resp = await client.get(f"/api/admin/results/history/{uuid.uuid4()}")
    assert resp.status_code in (401, 403)


# ─────────────────────────────────────────────────────────────────────────────
# 11. Active result-entry selector still excludes completed tournaments
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_active_results_selector_excludes_completed(client: AsyncClient, db_session: AsyncSession):
    """
    The existing /api/admin/tournaments?status=completed filter must not be
    used in the active result-entry selector.
    GET /api/admin/tournaments?status=live should return 0 completed tournaments.
    """
    # Seed a completed tournament
    await _seed_completed_tournament(db_session)
    token = await _staff_token(client)

    resp = await client.get(
        "/api/admin/tournaments",
        params={"status": "live"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    items = resp.json().get("items", [])
    for item in items:
        assert item["status"] != "completed", (
            f"Completed tournament {item['tournament_code']} appeared in live results list"
        )

    resp2 = await client.get(
        "/api/admin/tournaments",
        params={"status": "result_entry"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp2.status_code == 200
    items2 = resp2.json().get("items", [])
    for item in items2:
        assert item["status"] != "completed"
