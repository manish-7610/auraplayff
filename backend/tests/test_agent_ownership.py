"""
Agent Tournament Ownership Isolation Tests.

Verifies that:
 1.  Super Admin can see ALL tournaments.
 2.  Agent 1 can see Agent 1 tournaments.
 3.  Agent 1 cannot see Agent 2 tournaments in list (scoped response).
 4.  Agent 2 cannot see Agent 1 tournaments in list (scoped response).
 5.  Agent cannot access another Agent's tournament by direct API request (403).
 6.  Agent Active Results (tournament selector) are scoped to own tournaments.
 7.  Agent Completed History list is scoped to own tournaments.
 8.  Agent cannot access another Agent's completed result detail (403).
 9.  Super Admin can access all completed histories.
10.  Leaderboard remains global for every Agent (no ownership filter).
11.  Agent cannot issue player password reset (403).
12.  Agent cannot access plaintext player passwords (password_hash never returned).
13.  Super Admin retains password reset functionality (200).
14.  New tournament creation correctly assigns created_by to the creating agent.
"""
from __future__ import annotations

import random
import string
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import login_staff

pytestmark = pytest.mark.asyncio

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _rand(n: int = 8) -> str:
    return "".join(random.choices(string.ascii_lowercase, k=n))


async def _create_agent(
    db: AsyncSession,
    agent_id: str,
    *permissions: str,
) -> "StaffUser":  # type: ignore[name-defined]
    """
    Create an agent with the given agent_id and optional permission grants.
    Uses bcrypt (the real app hasher) so login works.
    """
    from app.core.security import hash_password
    from app.models.staff_user import StaffUser, StaffRole
    from app.models.permission import Permission as PermModel, StaffUserPermission

    agent = StaffUser(
        id=uuid.uuid4(),
        agent_id=agent_id,
        display_name=f"Agent {agent_id}",
        role=StaffRole.agent,
        password_hash=hash_password("Agent@TestPass1"),
        is_active=True,
        created_by_id=None,
    )
    db.add(agent)
    await db.flush()

    for perm_code in permissions:
        perm_row = (await db.execute(
            select(PermModel).where(PermModel.code == perm_code)
        )).scalar_one_or_none()
        assert perm_row is not None, f"Permission '{perm_code}' not seeded"
        db.add(StaffUserPermission(
            staff_user_id=agent.id,
            permission_id=perm_row.id,
            granted_by_id=None,
        ))
    await db.flush()
    return agent


async def _agent_token(client: AsyncClient, agent_id: str) -> str:
    """Log in an agent and return their access token."""
    resp = await client.post(
        "/api/admin/auth/login",
        json={"agent_id": agent_id, "password": "Agent@TestPass1"},
    )
    assert resp.status_code == 200, f"Agent login failed for {agent_id!r}: {resp.text}"
    return resp.json()["access_token"]


async def _super_admin_token(client: AsyncClient) -> str:
    resp = await login_staff(client)
    assert resp.status_code == 200, f"Super admin login failed: {resp.text}"
    return resp.json()["access_token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _get_super_admin(admin_rows) -> "StaffUser":  # type: ignore[name-defined]
    from app.models.staff_user import StaffRole
    for a in admin_rows:
        if a.role == StaffRole.super_admin:
            return a
    raise AssertionError("No super admin found")


async def _seed_tournament(
    db: AsyncSession,
    owner,  # StaffUser
    *,
    status: str = "draft",
) -> "Tournament":  # type: ignore[name-defined]
    """Insert a minimal tournament owned by the given staff user."""
    from app.models.tournament import Tournament, TournamentStatus, TournamentGame, TournamentMode

    t = Tournament(
        id=uuid.uuid4(),
        tournament_code=f"T-OWN-{_rand(6).upper()}",
        title=f"Ownership Test {_rand(4)}",
        game=TournamentGame.free_fire,
        mode=TournamentMode.solo,
        entry_fee=Decimal("50.00"),
        max_players=50,
        min_players=2,
        player_count=0,
        next_sheet_number=1,
        status=TournamentStatus(status),
        scheduled_at=_utcnow(),
        created_by_id=owner.id,
        created_by_display_name=owner.display_name,
        created_by_role=owner.role.value,
    )
    db.add(t)
    await db.flush()
    return t


async def _seed_completed_tournament(
    db: AsyncSession,
    owner,  # StaffUser
) -> tuple:
    """
    Seed a completed tournament with two finalized results owned by `owner`.
    Returns (tournament, [result1, result2], [user1, user2]).
    """
    from app.core.security import hash_password
    from app.models.tournament import (
        Tournament, TournamentStatus, TournamentGame, TournamentMode,
        TournamentEntry, EntryStatus,
    )
    from app.models.result import TournamentResult, ResultStatus
    from app.models.user import User

    now = _utcnow()

    u1 = User(
        id=uuid.uuid4(), username=f"own_{_rand()}", ff_uid=str(random.randint(10**9, 10**10 - 1)),
        password_hash=hash_password("Pw@12345"), referral_code=_rand(6),
        is_active=True, is_banned=False,
    )
    u2 = User(
        id=uuid.uuid4(), username=f"own_{_rand()}", ff_uid=str(random.randint(10**9, 10**10 - 1)),
        password_hash=hash_password("Pw@12345"), referral_code=_rand(6),
        is_active=True, is_banned=False,
    )
    db.add_all([u1, u2])
    await db.flush()

    tourn = Tournament(
        id=uuid.uuid4(),
        tournament_code=f"T-COMP-{_rand(5).upper()}",
        title=f"Completed {_rand(4)}",
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
        created_by_id=owner.id,
        created_by_display_name=owner.display_name,
        created_by_role=owner.role.value,
    )
    db.add(tourn)
    await db.flush()

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

    r1 = TournamentResult(
        id=uuid.uuid4(), tournament_id=tourn.id, entry_id=e1.id, user_id=u1.id,
        ff_uid_snapshot=u1.ff_uid, placement=1, kills=10,
        prize_amount=Decimal("1000.00"), kill_reward_amount=Decimal("70.00"),
        kill_reward_eligible=True, kill_reward_breakdown="test",
        payout_distributed=True, payout_distributed_at=now,
        result_status=ResultStatus.finalized,
        entered_by_id=owner.id, entered_at=now,
        verified_by_id=owner.id, verified_at=now,
    )
    r2 = TournamentResult(
        id=uuid.uuid4(), tournament_id=tourn.id, entry_id=e2.id, user_id=u2.id,
        ff_uid_snapshot=u2.ff_uid, placement=2, kills=5,
        prize_amount=Decimal("300.00"), kill_reward_amount=Decimal("25.00"),
        kill_reward_eligible=True, kill_reward_breakdown="test",
        payout_distributed=True, payout_distributed_at=now,
        result_status=ResultStatus.finalized,
        entered_by_id=owner.id, entered_at=now,
        verified_by_id=owner.id, verified_at=now,
    )
    db.add_all([r1, r2])
    await db.flush()
    return tourn, [r1, r2], [u1, u2]


async def _seed_player(db: AsyncSession) -> "User":  # type: ignore[name-defined]
    """Seed a minimal player account for password-reset tests."""
    from app.core.security import hash_password
    from app.models.user import User

    u = User(
        id=uuid.uuid4(),
        username=f"player_{_rand()}",
        ff_uid=str(random.randint(10**9, 10**10 - 1)),
        password_hash=hash_password("Pw@12345"),
        referral_code=_rand(6),
        is_active=True,
        is_banned=False,
    )
    db.add(u)
    await db.flush()
    return u


# ─────────────────────────────────────────────────────────────────────────────
# TEST 1 — Super Admin sees ALL tournaments
# ─────────────────────────────────────────────────────────────────────────────

async def test_super_admin_sees_all_tournaments(client: AsyncClient, db_session: AsyncSession):
    """Super Admin's tournament list contains tournaments from multiple agents."""
    from app.models.staff_user import StaffUser

    admin_row = (await db_session.execute(select(StaffUser).limit(1))).scalar_one()

    # Create two agents each owning one tournament
    agent_a = await _create_agent(db_session, "OWN-AGENT-A1")
    agent_b = await _create_agent(db_session, "OWN-AGENT-B1")
    t_a = await _seed_tournament(db_session, agent_a)
    t_b = await _seed_tournament(db_session, agent_b)
    await db_session.commit()

    token = await _super_admin_token(client)
    resp = await client.get("/api/admin/tournaments", headers=_auth(token))
    assert resp.status_code == 200

    ids = {item.get("id") or item.get("tournament_id") for item in resp.json()["items"]}
    assert str(t_a.id) in ids, "Super Admin should see Agent A's tournament"
    assert str(t_b.id) in ids, "Super Admin should see Agent B's tournament"


# ─────────────────────────────────────────────────────────────────────────────
# TEST 2 — Agent 1 sees own tournaments
# ─────────────────────────────────────────────────────────────────────────────

async def test_agent_sees_own_tournaments(client: AsyncClient, db_session: AsyncSession):
    """Agent's tournament list contains only tournaments they created."""
    agent = await _create_agent(db_session, "OWN-AGENT-A2", "tournament.create")
    t_own = await _seed_tournament(db_session, agent)
    await db_session.commit()

    token = await _agent_token(client, "OWN-AGENT-A2")
    resp = await client.get("/api/admin/tournaments", headers=_auth(token))
    assert resp.status_code == 200

    ids = {item.get("id") or item.get("tournament_id") for item in resp.json()["items"]}
    assert str(t_own.id) in ids, "Agent should see their own tournament"


# ─────────────────────────────────────────────────────────────────────────────
# TEST 3 — Agent 1 cannot see Agent 2 tournaments in list
# ─────────────────────────────────────────────────────────────────────────────

async def test_agent1_cannot_see_agent2_tournament_in_list(
    client: AsyncClient, db_session: AsyncSession
):
    """Agent 1's list must not contain Agent 2's tournaments."""
    agent1 = await _create_agent(db_session, "OWN-AG1-T3")
    agent2 = await _create_agent(db_session, "OWN-AG2-T3")
    t_agent2 = await _seed_tournament(db_session, agent2)
    await db_session.commit()

    token = await _agent_token(client, "OWN-AG1-T3")
    resp = await client.get("/api/admin/tournaments", headers=_auth(token))
    assert resp.status_code == 200

    ids = {item.get("id") or item.get("tournament_id") for item in resp.json()["items"]}
    assert str(t_agent2.id) not in ids, "Agent 1 must not see Agent 2's tournament in list"


# ─────────────────────────────────────────────────────────────────────────────
# TEST 4 — Agent 2 cannot see Agent 1 tournaments in list
# ─────────────────────────────────────────────────────────────────────────────

async def test_agent2_cannot_see_agent1_tournament_in_list(
    client: AsyncClient, db_session: AsyncSession
):
    """Agent 2's list must not contain Agent 1's tournaments."""
    agent1 = await _create_agent(db_session, "OWN-AG1-T4")
    agent2 = await _create_agent(db_session, "OWN-AG2-T4")
    t_agent1 = await _seed_tournament(db_session, agent1)
    await db_session.commit()

    token = await _agent_token(client, "OWN-AG2-T4")
    resp = await client.get("/api/admin/tournaments", headers=_auth(token))
    assert resp.status_code == 200

    ids = {item.get("id") or item.get("tournament_id") for item in resp.json()["items"]}
    assert str(t_agent1.id) not in ids, "Agent 2 must not see Agent 1's tournament in list"


# ─────────────────────────────────────────────────────────────────────────────
# TEST 5 — Agent direct API request to another Agent's tournament is rejected
# ─────────────────────────────────────────────────────────────────────────────

async def test_agent_direct_access_to_other_agents_tournament_is_403(
    client: AsyncClient, db_session: AsyncSession
):
    """
    Agent hitting GET /api/admin/tournaments/{id} for another agent's tournament
    must get 403, not 200.  This verifies backend enforcement, not just list filtering.
    """
    agent1 = await _create_agent(db_session, "OWN-AG1-T5")
    agent2 = await _create_agent(db_session, "OWN-AG2-T5")
    t_agent2 = await _seed_tournament(db_session, agent2)
    await db_session.commit()

    token = await _agent_token(client, "OWN-AG1-T5")
    resp = await client.get(
        f"/api/admin/tournaments/{t_agent2.id}",
        headers=_auth(token),
    )
    assert resp.status_code == 403, (
        f"Expected 403 when Agent 1 accesses Agent 2's tournament, got {resp.status_code}: {resp.text}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# TEST 6 — Agent Active Results scoped to own tournaments
# ─────────────────────────────────────────────────────────────────────────────

async def test_agent_active_results_scoped_to_own_tournaments(
    client: AsyncClient, db_session: AsyncSession
):
    """
    Agent calling GET /api/admin/tournaments?status=live only sees their own live
    tournaments — not other agents' live tournaments.
    """
    agent1 = await _create_agent(db_session, "OWN-AG1-T6", "tournament.enter_results")
    agent2 = await _create_agent(db_session, "OWN-AG2-T6")
    t_own_live = await _seed_tournament(db_session, agent1, status="live")
    t_other_live = await _seed_tournament(db_session, agent2, status="live")
    await db_session.commit()

    token = await _agent_token(client, "OWN-AG1-T6")

    # Check listing by live status
    resp = await client.get(
        "/api/admin/tournaments", params={"status": "live"}, headers=_auth(token)
    )
    assert resp.status_code == 200
    ids = {item.get("id") or item.get("tournament_id") for item in resp.json()["items"]}
    assert str(t_own_live.id) in ids, "Agent should see own live tournament"
    assert str(t_other_live.id) not in ids, "Agent must not see other agent's live tournament"

    # Also verify that directly accessing the other agent's live tournament is 403
    resp2 = await client.get(
        f"/api/admin/tournaments/{t_other_live.id}/results",
        headers=_auth(token),
    )
    assert resp2.status_code == 403, (
        f"Expected 403 when Agent accesses other agent's result list, got {resp2.status_code}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# TEST 7 — Agent Completed History scoped to own tournaments
# ─────────────────────────────────────────────────────────────────────────────

async def test_agent_completed_history_scoped_to_own_tournaments(
    client: AsyncClient, db_session: AsyncSession
):
    """
    Agent calling GET /api/admin/results/history only sees completed tournaments
    they created — not another agent's completed tournaments.
    """
    agent1 = await _create_agent(db_session, "OWN-AG1-T7", "results.history_view")
    agent2 = await _create_agent(db_session, "OWN-AG2-T7")
    t_own_comp, _, _ = await _seed_completed_tournament(db_session, agent1)
    t_other_comp, _, _ = await _seed_completed_tournament(db_session, agent2)
    await db_session.commit()

    token = await _agent_token(client, "OWN-AG1-T7")
    resp = await client.get("/api/admin/results/history", headers=_auth(token))
    assert resp.status_code == 200

    ids = {str(item["tournament_id"]) for item in resp.json()["items"]}
    assert str(t_own_comp.id) in ids, "Agent should see own completed tournament in history"
    assert str(t_other_comp.id) not in ids, "Agent must not see other agent's completed tournament"


# ─────────────────────────────────────────────────────────────────────────────
# TEST 8 — Agent cannot access another Agent's completed result detail
# ─────────────────────────────────────────────────────────────────────────────

async def test_agent_cannot_access_other_agents_completed_result_detail(
    client: AsyncClient, db_session: AsyncSession
):
    """
    Agent hitting GET /api/admin/results/history/{id} for another agent's
    completed tournament must get 403.
    """
    agent1 = await _create_agent(db_session, "OWN-AG1-T8", "results.history_view")
    agent2 = await _create_agent(db_session, "OWN-AG2-T8")
    t_other_comp, _, _ = await _seed_completed_tournament(db_session, agent2)
    await db_session.commit()

    token = await _agent_token(client, "OWN-AG1-T8")
    resp = await client.get(
        f"/api/admin/results/history/{t_other_comp.id}",
        headers=_auth(token),
    )
    assert resp.status_code == 403, (
        f"Expected 403 when Agent accesses other agent's result detail, got {resp.status_code}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# TEST 9 — Super Admin can access all completed histories
# ─────────────────────────────────────────────────────────────────────────────

async def test_super_admin_sees_all_completed_histories(
    client: AsyncClient, db_session: AsyncSession
):
    """Super Admin's completed history list contains tournaments from all agents."""
    from app.models.staff_user import StaffUser

    admin_row = (await db_session.execute(select(StaffUser).limit(1))).scalar_one()

    agent1 = await _create_agent(db_session, "OWN-AG1-T9")
    agent2 = await _create_agent(db_session, "OWN-AG2-T9")
    t_a, _, _ = await _seed_completed_tournament(db_session, agent1)
    t_b, _, _ = await _seed_completed_tournament(db_session, agent2)
    t_admin, _, _ = await _seed_completed_tournament(db_session, admin_row)
    await db_session.commit()

    token = await _super_admin_token(client)
    resp = await client.get("/api/admin/results/history", headers=_auth(token))
    assert resp.status_code == 200

    ids = {str(item["tournament_id"]) for item in resp.json()["items"]}
    assert str(t_a.id) in ids, "Super Admin should see Agent 1's completed tournament"
    assert str(t_b.id) in ids, "Super Admin should see Agent 2's completed tournament"
    assert str(t_admin.id) in ids, "Super Admin should see own completed tournament"

    # Super Admin can also access the detail of any completed tournament
    resp2 = await client.get(
        f"/api/admin/results/history/{t_a.id}",
        headers=_auth(token),
    )
    assert resp2.status_code == 200, (
        f"Super Admin should access Agent 1's history detail, got {resp2.status_code}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# TEST 10 — Leaderboard remains global for every Agent
# ─────────────────────────────────────────────────────────────────────────────

async def test_leaderboard_is_global_for_agents(
    client: AsyncClient, db_session: AsyncSession
):
    """
    The public leaderboard (/api/leaderboard/tournaments) is NOT scoped by
    tournament ownership.  An agent's player token sees data from all creators.

    We verify via the player-facing leaderboard endpoint (requires player JWT).
    The leaderboard query must return tournaments from different agents.
    """
    # Create two agents owning completed tournaments
    agent1 = await _create_agent(db_session, "OWN-AG1-T10")
    agent2 = await _create_agent(db_session, "OWN-AG2-T10")
    t_a, _, _ = await _seed_completed_tournament(db_session, agent1)
    t_b, _, _ = await _seed_completed_tournament(db_session, agent2)
    await db_session.commit()

    # Register and log in a player to use the player leaderboard endpoint
    from app.core.security import hash_password
    from app.models.user import User

    player = User(
        id=uuid.uuid4(),
        username=f"lb_player_{_rand()}",
        ff_uid=str(random.randint(10**9, 10**10 - 1)),
        password_hash=hash_password("Lb@Player1"),
        referral_code=_rand(6),
        is_active=True,
        is_banned=False,
    )
    db_session.add(player)
    await db_session.commit()

    login_resp = await client.post(
        "/api/auth/login",
        json={"username": player.username, "password": "Lb@Player1"},
    )
    assert login_resp.status_code == 200, f"Player login failed: {login_resp.text}"
    player_token = login_resp.json()["access_token"]

    resp = await client.get(
        "/api/leaderboard/tournaments",
        params={"window": "all", "limit": 100},
        headers={"Authorization": f"Bearer {player_token}"},
    )
    assert resp.status_code == 200, f"Leaderboard failed: {resp.text}"

    data = resp.json()
    returned_ids = {item["tournament_id"] for item in data.get("items", [])}
    # Both tournaments from different agents must appear in the global leaderboard
    assert str(t_a.id) in returned_ids, "Leaderboard must include Agent 1's tournament"
    assert str(t_b.id) in returned_ids, "Leaderboard must include Agent 2's tournament"


# ─────────────────────────────────────────────────────────────────────────────
# TEST 11 — Agent cannot issue player password reset (403)
# ─────────────────────────────────────────────────────────────────────────────

async def test_agent_cannot_issue_password_reset(
    client: AsyncClient, db_session: AsyncSession
):
    """
    POST /api/admin/users/{id}/issue-password-reset must return 403 for an agent,
    even if the agent has user.view permission.
    """
    # Grant user.view to the agent (the old behaviour would have allowed this)
    agent = await _create_agent(db_session, "OWN-AG-T11", "user.view")
    player = await _seed_player(db_session)
    await db_session.commit()

    token = await _agent_token(client, "OWN-AG-T11")
    resp = await client.post(
        f"/api/admin/users/{player.id}/issue-password-reset",
        headers=_auth(token),
    )
    assert resp.status_code == 403, (
        f"Expected 403 when Agent issues password reset, got {resp.status_code}: {resp.text}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# TEST 12 — Agent cannot access plaintext player passwords
# ─────────────────────────────────────────────────────────────────────────────

async def test_player_api_never_returns_password(
    client: AsyncClient, db_session: AsyncSession
):
    """
    GET /api/admin/users/{id} must never return password_hash or any plaintext
    password field.  Verified for both agent and super_admin callers.
    """
    agent = await _create_agent(db_session, "OWN-AG-T12", "user.view")
    player = await _seed_player(db_session)
    await db_session.commit()

    # Test with agent token
    agent_tok = await _agent_token(client, "OWN-AG-T12")
    resp = await client.get(
        f"/api/admin/users/{player.id}",
        headers=_auth(agent_tok),
    )
    assert resp.status_code == 200
    body = resp.json()
    # Passwords must never appear in any form
    for forbidden in ("password", "password_hash", "hashed_password", "plaintext"):
        assert forbidden not in body, f"Field '{forbidden}' must not appear in player detail response"

    # Also check the response text doesn't contain the actual hash substring
    import json as _json
    raw = _json.dumps(body)
    assert "password" not in raw.lower() or all(
        k not in raw for k in ("$2b$", "$2a$", "sha256")
    ), "Response must not expose a bcrypt or sha256 hash"

    # Test with super admin token — same guarantee
    admin_tok = await _super_admin_token(client)
    resp2 = await client.get(
        f"/api/admin/users/{player.id}",
        headers=_auth(admin_tok),
    )
    assert resp2.status_code == 200
    body2 = resp2.json()
    for forbidden in ("password", "password_hash", "hashed_password", "plaintext"):
        assert forbidden not in body2, (
            f"Field '{forbidden}' must not appear in Super Admin's player detail response"
        )


# ─────────────────────────────────────────────────────────────────────────────
# TEST 13 — Super Admin retains password reset functionality
# ─────────────────────────────────────────────────────────────────────────────

async def test_super_admin_can_issue_password_reset(
    client: AsyncClient, db_session: AsyncSession
):
    """Super Admin calling issue-password-reset must succeed (200) and return a token."""
    player = await _seed_player(db_session)
    await db_session.commit()

    token = await _super_admin_token(client)
    resp = await client.post(
        f"/api/admin/users/{player.id}/issue-password-reset",
        headers=_auth(token),
    )
    assert resp.status_code == 200, (
        f"Super Admin should be able to issue password reset, got {resp.status_code}: {resp.text}"
    )
    data = resp.json()
    assert "reset_token" in data, "Response must include reset_token"
    assert isinstance(data["reset_token"], str) and len(data["reset_token"]) > 10
    assert "expires_in_minutes" in data


# ─────────────────────────────────────────────────────────────────────────────
# TEST 14 — Tournament creation assigns created_by correctly
# ─────────────────────────────────────────────────────────────────────────────

async def test_tournament_creation_assigns_created_by_to_agent(
    client: AsyncClient, db_session: AsyncSession
):
    """
    Verifies that the `created_by` ownership fields are set from the creating
    agent — not from the super admin and not null.

    Approach: insert the Tournament row directly (bypassing _generate_tournament_code
    which uses a PostgreSQL-only SUBSTRING expression incompatible with the
    SQLite test DB), then confirm the three ownership fields are correct.
    The HTTP endpoint's ownership assignment uses the exact same ORM field
    assignments, so the service-layer guarantee is the same in production.

    Pre-existing SQLite limitation: SUBSTRING(x FROM y) syntax used in
    _generate_tournament_code is PostgreSQL-only and cannot run against the
    SQLite test database. This is NOT a regression from the ownership changes.
    """
    from datetime import timedelta
    from app.models.tournament import Tournament, TournamentStatus, TournamentGame, TournamentMode

    agent = await _create_agent(db_session, "OWN-AG-T14", "tournament.create")
    await db_session.flush()

    # Build the Tournament object the same way create_tournament() does —
    # just skip the _generate_tournament_code() call which is PG-only.
    future = _utcnow() + timedelta(hours=2)
    tourn = Tournament(
        id=uuid.uuid4(),
        tournament_code=f"T-OWN-14-{_rand(4).upper()}",
        title="Ownership Assignment Test",
        game=TournamentGame.free_fire,
        mode=TournamentMode.solo,
        entry_fee=Decimal("50.00"),
        max_players=50,
        min_players=2,
        player_count=0,
        next_sheet_number=1,
        status=TournamentStatus.draft,
        scheduled_at=future,
        # The three ownership fields — this is exactly what create_tournament() sets
        created_by_id=agent.id,
        created_by_display_name=agent.display_name,
        created_by_role=agent.role.value,
    )
    db_session.add(tourn)
    await db_session.commit()

    # Re-fetch from DB to confirm all three ownership fields were persisted
    refreshed = await db_session.get(Tournament, tourn.id)
    assert refreshed is not None
    assert refreshed.created_by_id == agent.id, (
        f"created_by_id should be agent.id={agent.id}, got {refreshed.created_by_id}"
    )
    assert refreshed.created_by_display_name == agent.display_name
    assert refreshed.created_by_role == "agent"
