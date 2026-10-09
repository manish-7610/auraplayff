"""
Agent Tournament History — Super Admin feature tests.

Endpoint under test:
  GET /api/admin/agents/{agent_id}/tournaments

Covers:
 1.  Super Admin can open Agent1 Tournament History (200).
 2.  Agent1 history contains ONLY Agent1-created tournaments.
 3.  Agent2-created tournaments do NOT appear in Agent1's history.
 4.  Super Admin-created tournaments do NOT appear in Agent1's history.
 5.  Agent2 history contains ONLY Agent2-created tournaments.
 6.  An Agent cannot call another Agent's history endpoint (403).
 7.  Existing Agent tournament ownership tests remain untouched
     (verified by checking the key 403 guard still works).
 8.  Global Leaderboard behavior unchanged (separate public endpoint).
 9.  Existing Agent Details profile/today/month tabs unaffected
     (GET /api/admin/agents/{id} still works as before).
10.  No password / security regression (password_hash not in any response).
"""
from __future__ import annotations

import random
import string
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
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


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _super_admin_token(client: AsyncClient) -> str:
    resp = await login_staff(client)
    assert resp.status_code == 200, f"Super admin login failed: {resp.text}"
    return resp.json()["access_token"]


async def _create_agent(db: AsyncSession, agent_id: str) -> "StaffUser":  # type: ignore[name-defined]
    """Create a minimal agent account using bcrypt so login works."""
    from app.core.security import hash_password
    from app.models.staff_user import StaffUser, StaffRole

    agent = StaffUser(
        id=uuid.uuid4(),
        agent_id=agent_id,
        display_name=f"Agent {agent_id}",
        role=StaffRole.agent,
        password_hash=hash_password("Agent@Hist1"),
        is_active=True,
        created_by_id=None,
    )
    db.add(agent)
    await db.flush()
    return agent


async def _agent_token(client: AsyncClient, agent_id: str) -> str:
    resp = await client.post(
        "/api/admin/auth/login",
        json={"agent_id": agent_id, "password": "Agent@Hist1"},
    )
    assert resp.status_code == 200, f"Agent login failed for {agent_id!r}: {resp.text}"
    return resp.json()["access_token"]


async def _seed_tournament(
    db: AsyncSession,
    owner,  # StaffUser
    *,
    status: str = "open",
    title: str | None = None,
    code: str | None = None,
) -> "Tournament":  # type: ignore[name-defined]
    """Insert a minimal tournament owned by the given staff user."""
    from app.models.tournament import Tournament, TournamentStatus, TournamentGame, TournamentMode

    t = Tournament(
        id=uuid.uuid4(),
        tournament_code=code or f"T-ATH-{_rand(5).upper()}",
        title=title or f"ATH Test {_rand(4)}",
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


# ─────────────────────────────────────────────────────────────────────────────
# TEST 1 — Super Admin can open Agent1 Tournament History (200)
# ─────────────────────────────────────────────────────────────────────────────

async def test_super_admin_can_open_agent_tournament_history(
    client: AsyncClient, db_session: AsyncSession
):
    """Super Admin gets 200 + well-formed response for any agent's history."""
    agent1 = await _create_agent(db_session, "ATH-AG1-T1")
    t = await _seed_tournament(db_session, agent1)
    await db_session.commit()

    token = await _super_admin_token(client)
    resp = await client.get(
        f"/api/admin/agents/{agent1.id}/tournaments",
        headers=_auth(token),
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "items" in data
    assert "total" in data
    assert isinstance(data["items"], list)
    ids = {item["id"] for item in data["items"]}
    assert str(t.id) in ids


# ─────────────────────────────────────────────────────────────────────────────
# TEST 2 — Agent1 history contains ONLY Agent1-created tournaments
# ─────────────────────────────────────────────────────────────────────────────

async def test_agent1_history_contains_only_agent1_tournaments(
    client: AsyncClient, db_session: AsyncSession
):
    """
    All tournaments returned by Agent1's history have
    created_by_display_name matching Agent1, not any other creator.
    """
    agent1 = await _create_agent(db_session, "ATH-AG1-T2")
    t1a = await _seed_tournament(db_session, agent1)
    t1b = await _seed_tournament(db_session, agent1)
    await db_session.commit()

    token = await _super_admin_token(client)
    resp = await client.get(
        f"/api/admin/agents/{agent1.id}/tournaments",
        headers=_auth(token),
    )
    assert resp.status_code == 200
    items = resp.json()["items"]
    ids = {item["id"] for item in items}
    assert str(t1a.id) in ids
    assert str(t1b.id) in ids
    # Every returned tournament must have been created by agent1
    for item in items:
        assert item["created_by_display_name"] == agent1.display_name, (
            f"Tournament {item['id']} has creator {item['created_by_display_name']!r}, "
            f"expected {agent1.display_name!r}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# TEST 3 — Agent2-created tournaments do NOT appear in Agent1's history
# ─────────────────────────────────────────────────────────────────────────────

async def test_agent2_tournaments_not_in_agent1_history(
    client: AsyncClient, db_session: AsyncSession
):
    """Tournament created by Agent2 must be absent from Agent1's history."""
    agent1 = await _create_agent(db_session, "ATH-AG1-T3")
    agent2 = await _create_agent(db_session, "ATH-AG2-T3")
    t_agent2 = await _seed_tournament(db_session, agent2)
    await db_session.commit()

    token = await _super_admin_token(client)
    resp = await client.get(
        f"/api/admin/agents/{agent1.id}/tournaments",
        headers=_auth(token),
    )
    assert resp.status_code == 200
    ids = {item["id"] for item in resp.json()["items"]}
    assert str(t_agent2.id) not in ids, (
        "Agent2's tournament must not appear in Agent1's history"
    )


# ─────────────────────────────────────────────────────────────────────────────
# TEST 4 — Super Admin-created tournaments do NOT appear in Agent1's history
# ─────────────────────────────────────────────────────────────────────────────

async def test_super_admin_tournaments_not_in_agent1_history(
    client: AsyncClient, db_session: AsyncSession
):
    """Tournament owned by Super Admin is absent from Agent1's history."""
    from app.models.staff_user import StaffUser

    admin_row = (await db_session.execute(select(StaffUser).limit(1))).scalar_one()
    agent1 = await _create_agent(db_session, "ATH-AG1-T4")
    t_admin = await _seed_tournament(db_session, admin_row)
    await db_session.commit()

    token = await _super_admin_token(client)
    resp = await client.get(
        f"/api/admin/agents/{agent1.id}/tournaments",
        headers=_auth(token),
    )
    assert resp.status_code == 200
    ids = {item["id"] for item in resp.json()["items"]}
    assert str(t_admin.id) not in ids, (
        "Super Admin's tournament must not appear in Agent1's history"
    )


# ─────────────────────────────────────────────────────────────────────────────
# TEST 5 — Agent2 history contains ONLY Agent2-created tournaments
# ─────────────────────────────────────────────────────────────────────────────

async def test_agent2_history_contains_only_agent2_tournaments(
    client: AsyncClient, db_session: AsyncSession
):
    """Agent2's history is correctly scoped — no cross-agent mixing."""
    agent1 = await _create_agent(db_session, "ATH-AG1-T5")
    agent2 = await _create_agent(db_session, "ATH-AG2-T5")
    t1 = await _seed_tournament(db_session, agent1)
    t2 = await _seed_tournament(db_session, agent2)
    await db_session.commit()

    token = await _super_admin_token(client)
    resp = await client.get(
        f"/api/admin/agents/{agent2.id}/tournaments",
        headers=_auth(token),
    )
    assert resp.status_code == 200
    ids = {item["id"] for item in resp.json()["items"]}
    assert str(t2.id) in ids, "Agent2's own tournament should appear"
    assert str(t1.id) not in ids, "Agent1's tournament must not appear in Agent2's history"


# ─────────────────────────────────────────────────────────────────────────────
# TEST 6 — An Agent cannot call another Agent's history endpoint (403)
# ─────────────────────────────────────────────────────────────────────────────

async def test_agent_cannot_access_another_agents_history_endpoint(
    client: AsyncClient, db_session: AsyncSession
):
    """
    Agent1 calling GET /api/admin/agents/{agent2_id}/tournaments must get 403.
    This verifies backend Super Admin guard, not just UI hiding.
    """
    agent1 = await _create_agent(db_session, "ATH-AG1-T6")
    agent2 = await _create_agent(db_session, "ATH-AG2-T6")
    await db_session.commit()

    token = await _agent_token(client, "ATH-AG1-T6")
    resp = await client.get(
        f"/api/admin/agents/{agent2.id}/tournaments",
        headers=_auth(token),
    )
    assert resp.status_code == 403, (
        f"Expected 403 when Agent1 accesses Agent2's tournament history, got {resp.status_code}: {resp.text}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# TEST 7 — Existing Agent tournament ownership isolation still works
# ─────────────────────────────────────────────────────────────────────────────

async def test_existing_ownership_guard_still_rejects_cross_agent_detail(
    client: AsyncClient, db_session: AsyncSession
):
    """
    The previously implemented ownership isolation on GET /api/admin/tournaments/{id}
    still returns 403 when Agent1 tries to access Agent2's tournament directly.
    This confirms we haven't broken any existing security work.
    """
    agent1 = await _create_agent(db_session, "ATH-AG1-T7")
    agent2 = await _create_agent(db_session, "ATH-AG2-T7")
    t2 = await _seed_tournament(db_session, agent2)
    await db_session.commit()

    token = await _agent_token(client, "ATH-AG1-T7")
    resp = await client.get(
        f"/api/admin/tournaments/{t2.id}",
        headers=_auth(token),
    )
    assert resp.status_code == 403, (
        f"Existing ownership guard broken — expected 403, got {resp.status_code}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# TEST 8 — Global Leaderboard behavior unchanged
# ─────────────────────────────────────────────────────────────────────────────

async def test_global_leaderboard_still_works(
    client: AsyncClient, db_session: AsyncSession
):
    """
    GET /api/leaderboard/tournaments (player-facing, public leaderboard)
    continues to return 200 and is not affected by the new history endpoint.
    """
    from app.core.security import hash_password
    from app.models.user import User

    # Register and log in a player to call the leaderboard
    player = User(
        id=uuid.uuid4(),
        username=f"lb_{_rand()}",
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
    assert login_resp.status_code == 200
    player_token = login_resp.json()["access_token"]

    resp = await client.get(
        "/api/leaderboard/tournaments",
        params={"window": "all", "limit": 5},
        headers={"Authorization": f"Bearer {player_token}"},
    )
    assert resp.status_code == 200, f"Leaderboard broken: {resp.text}"
    data = resp.json()
    assert "items" in data, "Leaderboard response shape changed"


# ─────────────────────────────────────────────────────────────────────────────
# TEST 9 — Existing Agent Details profile endpoint still works
# ─────────────────────────────────────────────────────────────────────────────

async def test_agent_details_profile_endpoint_unaffected(
    client: AsyncClient, db_session: AsyncSession
):
    """
    GET /api/admin/agents/{id} (the profile/today/month/salary data source)
    still returns 200 with the correct shape — we haven't broken any
    existing agent-details tabs.
    """
    agent = await _create_agent(db_session, "ATH-AG1-T9")
    await db_session.commit()

    token = await _super_admin_token(client)
    resp = await client.get(
        f"/api/admin/agents/{agent.id}",
        headers=_auth(token),
    )
    assert resp.status_code == 200, f"Agent detail broken: {resp.text}"
    data = resp.json()
    assert data["agent_id"] == "ATH-AG1-T9"
    assert "display_name" in data
    assert "role" in data
    # password_hash must never appear
    assert "password_hash" not in data
    assert "password" not in data


# ─────────────────────────────────────────────────────────────────────────────
# TEST 10 — No password/security regression in tournament history response
# ─────────────────────────────────────────────────────────────────────────────

async def test_tournament_history_response_contains_no_passwords(
    client: AsyncClient, db_session: AsyncSession
):
    """
    The tournament history endpoint must never expose password_hash or any
    plaintext password field in its response items.
    """
    agent = await _create_agent(db_session, "ATH-AG1-T10")
    await _seed_tournament(db_session, agent)
    await db_session.commit()

    token = await _super_admin_token(client)
    resp = await client.get(
        f"/api/admin/agents/{agent.id}/tournaments",
        headers=_auth(token),
    )
    assert resp.status_code == 200

    import json as _json
    raw = _json.dumps(resp.json())

    # No password-related fields must be present in any form
    for forbidden in ("password_hash", "hashed_password", "plaintext"):
        assert forbidden not in raw.lower(), (
            f"Field '{forbidden}' must not appear in tournament history response"
        )
    # Must not contain a bcrypt hash substring
    assert "$2b$" not in raw and "$2a$" not in raw, (
        "bcrypt hash must not appear in tournament history response"
    )


# ─────────────────────────────────────────────────────────────────────────────
# BONUS — Status filter and search work correctly
# ─────────────────────────────────────────────────────────────────────────────

async def test_tournament_history_status_filter(
    client: AsyncClient, db_session: AsyncSession
):
    """Status filter correctly restricts the result set."""
    agent = await _create_agent(db_session, "ATH-AG1-TF")
    t_open = await _seed_tournament(db_session, agent, status="open")
    t_comp = await _seed_tournament(db_session, agent, status="completed")
    await db_session.commit()

    token = await _super_admin_token(client)

    # Filter by open — should see t_open, not t_comp
    resp = await client.get(
        f"/api/admin/agents/{agent.id}/tournaments",
        params={"status": "open"},
        headers=_auth(token),
    )
    assert resp.status_code == 200
    ids = {item["id"] for item in resp.json()["items"]}
    assert str(t_open.id) in ids
    assert str(t_comp.id) not in ids

    # Filter by completed — should see t_comp, not t_open
    resp2 = await client.get(
        f"/api/admin/agents/{agent.id}/tournaments",
        params={"status": "completed"},
        headers=_auth(token),
    )
    assert resp2.status_code == 200
    ids2 = {item["id"] for item in resp2.json()["items"]}
    assert str(t_comp.id) in ids2
    assert str(t_open.id) not in ids2


async def test_tournament_history_search(
    client: AsyncClient, db_session: AsyncSession
):
    """Search by title substring works and stays scoped to the agent."""
    agent = await _create_agent(db_session, "ATH-AG1-TS")
    t_match  = await _seed_tournament(db_session, agent, title="AlphaTournament Special")
    t_nomatch = await _seed_tournament(db_session, agent, title="BetaTournament Regular")
    await db_session.commit()

    token = await _super_admin_token(client)
    resp = await client.get(
        f"/api/admin/agents/{agent.id}/tournaments",
        params={"search": "AlphaTournament"},
        headers=_auth(token),
    )
    assert resp.status_code == 200
    ids = {item["id"] for item in resp.json()["items"]}
    assert str(t_match.id) in ids
    assert str(t_nomatch.id) not in ids


async def test_tournament_history_pagination(
    client: AsyncClient, db_session: AsyncSession
):
    """Pagination correctly slices the result and total reflects all records."""
    agent = await _create_agent(db_session, "ATH-AG1-TP")
    # Seed 5 tournaments
    for _ in range(5):
        await _seed_tournament(db_session, agent)
    await db_session.commit()

    token = await _super_admin_token(client)

    # Page 1: limit=3
    resp1 = await client.get(
        f"/api/admin/agents/{agent.id}/tournaments",
        params={"limit": 3, "offset": 0},
        headers=_auth(token),
    )
    assert resp1.status_code == 200
    d1 = resp1.json()
    assert d1["total"] == 5
    assert len(d1["items"]) == 3

    # Page 2: offset=3 → 2 items remaining
    resp2 = await client.get(
        f"/api/admin/agents/{agent.id}/tournaments",
        params={"limit": 3, "offset": 3},
        headers=_auth(token),
    )
    assert resp2.status_code == 200
    d2 = resp2.json()
    assert d2["total"] == 5
    assert len(d2["items"]) == 2
