"""
FF name display in tournament views — focused tests.

Covers:
 1. ff_name_snapshot is stored on TournamentResult at result entry.
 2. Changing ff_name after entry does NOT alter the snapshot.
 3. Admin entries endpoint returns ff_name for enrolled players.
 4. Player leaderboard returns display_name = ff_name when set.
 5. Player public stats returns display_name + raw ff_name.
 6. Player tournament history result rows return display_name.
 7. Admin completed results history returns ff_name_snapshot.
 8. Admin result list (active tournament) returns ff_name_snapshot.
 9. Admin player leaderboard returns display_name.
10. Admin tournament leaderboard result rows return display_name.
11. Username, FF UID, rankings, kills, prizes unchanged.
12. Historical snapshot survives profile ff_name update.
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
    resp = await login_staff(client)
    assert resp.status_code == 200
    return resp.json()["access_token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _register_and_login(
    client: AsyncClient,
    username: str,
    ff_uid: str,
    ff_name: str | None = None,
) -> str:
    payload = {
        "username": username,
        "password": "TestPass@123",
        "confirm_password": "TestPass@123",
        "ff_uid": ff_uid,
    }
    if ff_name:
        payload["ff_name"] = ff_name
    r = await client.post("/api/auth/register", json=payload)
    assert r.status_code == 201, r.text
    lr = await client.post("/api/auth/login", json={"username": username, "password": "TestPass@123"})
    assert lr.status_code == 200
    return lr.json()["access_token"]


async def _seed_completed_tournament_with_result(
    db: AsyncSession,
    *,
    username: str,
    ff_uid: str,
    ff_name: str | None,
    placement: int = 1,
    kills: int = 5,
) -> tuple:
    """
    Seed a completed tournament with one finalized result for a given player.
    Returns (tournament, result, user).
    """
    import random, string
    from app.core.security import hash_password
    from app.models.user import User
    from app.models.staff_user import StaffUser
    from app.models.tournament import Tournament, TournamentStatus, TournamentMode, TournamentGame, TournamentEntry, EntryStatus
    from app.models.result import TournamentResult, ResultStatus

    # Create player
    user = User(
        id=uuid.uuid4(),
        username=username,
        ff_uid=ff_uid,
        ff_name=ff_name,
        password_hash=hash_password("TestPass@123"),
        referral_code="".join(random.choices(string.ascii_uppercase, k=8)),
        is_active=True,
        is_banned=False,
    )
    db.add(user)
    await db.flush([user])

    admin = (await db.execute(select(StaffUser).limit(1))).scalar_one()
    now = _utcnow()
    suffix = "".join(random.choices(string.ascii_uppercase + string.digits, k=4))

    tourn = Tournament(
        id=uuid.uuid4(),
        tournament_code=f"T-DP{suffix}",
        title=f"Display Test {suffix}",
        game=TournamentGame.free_fire,
        mode=TournamentMode.solo,
        entry_fee=Decimal("50.00"),
        max_players=50,
        min_players=2,
        player_count=1,
        next_sheet_number=2,
        status=TournamentStatus.completed,
        scheduled_at=now,
        completed_at=now,
        created_by_id=admin.id,
        created_by_display_name=admin.display_name,
        created_by_role=admin.role.value,
    )
    db.add(tourn)
    await db.flush([tourn])

    entry = TournamentEntry(
        id=uuid.uuid4(),
        tournament_id=tourn.id,
        user_id=user.id,
        entry_fee_snapshot=Decimal("50.00"),
        sheet_number=1,
        status=EntryStatus.active,
    )
    db.add(entry)
    await db.flush([entry])

    result = TournamentResult(
        id=uuid.uuid4(),
        tournament_id=tourn.id,
        entry_id=entry.id,
        user_id=user.id,
        ff_uid_snapshot=ff_uid,
        ff_name_snapshot=ff_name,        # snapshot at entry time
        placement=placement,
        kills=kills,
        prize_amount=Decimal("400.00") if placement == 1 else Decimal("0.00"),
        kill_reward_amount=Decimal("25.00"),
        kill_reward_eligible=True,
        payout_distributed=True,
        payout_distributed_at=now,
        result_status=ResultStatus.finalized,
        entered_by_id=admin.id,
        entered_at=now,
        verified_by_id=admin.id,
        verified_at=now,
    )
    db.add(result)
    await db.commit()
    return tourn, result, user


async def _seed_open_tournament(db: AsyncSession) -> tuple:
    """Seed an open tournament ready for entries. Returns (tournament, admin)."""
    import random, string
    from app.models.staff_user import StaffUser
    from app.models.tournament import Tournament, TournamentStatus, TournamentMode, TournamentGame

    admin = (await db.execute(select(StaffUser).limit(1))).scalar_one()
    suffix = "".join(random.choices(string.ascii_uppercase + string.digits, k=4))

    tourn = Tournament(
        id=uuid.uuid4(),
        tournament_code=f"T-OPN{suffix}",
        title=f"Open Test {suffix}",
        game=TournamentGame.free_fire,
        mode=TournamentMode.solo,
        entry_fee=Decimal("50.00"),
        max_players=50,
        min_players=2,
        player_count=0,
        next_sheet_number=1,
        status=TournamentStatus.result_entry,
        scheduled_at=_utcnow(),
        created_by_id=admin.id,
        created_by_display_name=admin.display_name,
        created_by_role=admin.role.value,
    )
    db.add(tourn)
    await db.commit()
    return tourn, admin


# ─────────────────────────────────────────────────────────────────────────────
# 1. ff_name_snapshot stored at result entry
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ff_name_snapshot_stored_in_result_model():
    """TournamentResult model has ff_name_snapshot field."""
    from app.models.result import TournamentResult
    assert hasattr(TournamentResult, "ff_name_snapshot"), "ff_name_snapshot not on TournamentResult"


@pytest.mark.asyncio
async def test_ff_name_snapshot_in_admin_result_response():
    """AdminResultResponse schema has ff_name_snapshot field."""
    from app.schemas.result import AdminResultResponse
    assert "ff_name_snapshot" in AdminResultResponse.model_fields


@pytest.mark.asyncio
async def test_ff_name_snapshot_in_history_item_schema():
    """CompletedHistoryResultItem schema has ff_name_snapshot field."""
    from app.schemas.result import CompletedHistoryResultItem
    assert "ff_name_snapshot" in CompletedHistoryResultItem.model_fields


@pytest.mark.asyncio
async def test_result_service_snapshots_ff_name(client: AsyncClient, db_session: AsyncSession):
    """
    When a result is entered for a player who has ff_name set,
    the ff_name_snapshot on the result must equal their ff_name at that time.
    """
    from app.models.user import User
    from app.models.tournament import TournamentEntry, EntryStatus
    from app.models.result import TournamentResult
    from app.core.security import hash_password
    import random, string

    # Create player with ff_name
    uname  = "snaptest1"
    ff_uid = "5550000001"
    fname  = "★SnapKing★"
    user = User(
        id=uuid.uuid4(), username=uname, ff_uid=ff_uid, ff_name=fname,
        password_hash=hash_password("TestPass@123"),
        referral_code="SNAPTST1",
        is_active=True, is_banned=False,
    )
    db_session.add(user)
    await db_session.flush([user])

    tourn, admin = await _seed_open_tournament(db_session)

    entry = TournamentEntry(
        id=uuid.uuid4(), tournament_id=tourn.id, user_id=user.id,
        entry_fee_snapshot=Decimal("50.00"), sheet_number=1,
        status=EntryStatus.active,
    )
    db_session.add(entry)
    await db_session.commit()

    # Enter result via API
    staff_token = await _staff_token(client)
    resp = await client.post(
        f"/api/admin/tournaments/{tourn.id}/results",
        json={"entry_id": str(entry.id), "placement": 1, "kills": 8},
        headers=_auth(staff_token),
    )
    assert resp.status_code == 201, resp.text

    body = resp.json()
    assert body["ff_name_snapshot"] == fname, (
        f"Expected ff_name_snapshot='{fname}', got '{body['ff_name_snapshot']}'"
    )

    # Also verify directly in DB
    result_row = (await db_session.execute(
        select(TournamentResult).where(TournamentResult.entry_id == entry.id)
    )).scalar_one()
    assert result_row.ff_name_snapshot == fname


# ─────────────────────────────────────────────────────────────────────────────
# 2. Snapshot survives profile ff_name update (historical accuracy)
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_snapshot_unchanged_after_profile_name_change(
    client: AsyncClient, db_session: AsyncSession
):
    """
    Changing the player's ff_name via PATCH /api/users/me must NOT
    alter the ff_name_snapshot on existing results.
    """
    from app.models.user import User
    from app.models.tournament import TournamentEntry, EntryStatus
    from app.models.result import TournamentResult
    from app.core.security import hash_password

    original_name = "OriginalName"
    changed_name  = "NewName"
    uname  = "snaphistory1"
    ff_uid = "5550000002"

    user = User(
        id=uuid.uuid4(), username=uname, ff_uid=ff_uid, ff_name=original_name,
        password_hash=hash_password("TestPass@123"),
        referral_code="SNAPHIS1",
        is_active=True, is_banned=False,
    )
    db_session.add(user)
    await db_session.flush([user])

    tourn, _ = await _seed_open_tournament(db_session)

    entry = TournamentEntry(
        id=uuid.uuid4(), tournament_id=tourn.id, user_id=user.id,
        entry_fee_snapshot=Decimal("50.00"), sheet_number=1,
        status=EntryStatus.active,
    )
    db_session.add(entry)
    await db_session.commit()

    # Enter result (snapshot = original_name)
    staff_token = await _staff_token(client)
    r1 = await client.post(
        f"/api/admin/tournaments/{tourn.id}/results",
        json={"entry_id": str(entry.id), "placement": 1, "kills": 3},
        headers=_auth(staff_token),
    )
    assert r1.status_code == 201

    # Now player changes their ff_name
    player_token = await client.post(
        "/api/auth/login",
        json={"username": uname, "password": "TestPass@123"},
    )
    p_tok = player_token.json()["access_token"]
    patch_r = await client.patch(
        "/api/users/me",
        json={"ff_name": changed_name},
        headers=_auth(p_tok),
    )
    assert patch_r.status_code == 200

    # Snapshot must still be original_name
    result_row = (await db_session.execute(
        select(TournamentResult).where(TournamentResult.entry_id == entry.id)
    )).scalar_one()
    assert result_row.ff_name_snapshot == original_name, (
        f"Snapshot was mutated! Expected '{original_name}', got '{result_row.ff_name_snapshot}'"
    )

    # Player's live ff_name is now changed_name
    me = await client.get("/api/users/me", headers=_auth(p_tok))
    assert me.json()["ff_name"] == changed_name


# ─────────────────────────────────────────────────────────────────────────────
# 3. Admin entries endpoint returns ff_name
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_admin_entries_endpoint_returns_ff_name(
    client: AsyncClient, db_session: AsyncSession
):
    """GET /api/admin/tournaments/{id}/entries must include ff_name for each entry."""
    from app.models.user import User
    from app.models.tournament import TournamentEntry, EntryStatus
    from app.core.security import hash_password

    ff_name = "EntryKing"
    uname   = "entryfntest1"
    ff_uid  = "5550000003"

    user = User(
        id=uuid.uuid4(), username=uname, ff_uid=ff_uid, ff_name=ff_name,
        password_hash=hash_password("TestPass@123"),
        referral_code="ENTRYFN1",
        is_active=True, is_banned=False,
    )
    db_session.add(user)
    await db_session.flush([user])

    tourn, _ = await _seed_open_tournament(db_session)

    entry = TournamentEntry(
        id=uuid.uuid4(), tournament_id=tourn.id, user_id=user.id,
        entry_fee_snapshot=Decimal("50.00"), sheet_number=1,
        status=EntryStatus.active,
    )
    db_session.add(entry)
    await db_session.commit()

    staff_token = await _staff_token(client)
    resp = await client.get(
        f"/api/admin/tournaments/{tourn.id}/entries",
        headers=_auth(staff_token),
    )
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert len(items) >= 1

    our_entry = next((e for e in items if e["user_id"] == str(user.id)), None)
    assert our_entry is not None, "Our entry not found in entries list"
    assert our_entry.get("ff_name") == ff_name, (
        f"Expected ff_name='{ff_name}', got '{our_entry.get('ff_name')}'"
    )
    assert our_entry["username"] == uname


# ─────────────────────────────────────────────────────────────────────────────
# 4. Player leaderboard returns display_name
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_player_leaderboard_returns_display_name(
    client: AsyncClient, db_session: AsyncSession
):
    """GET /api/leaderboard/players must return display_name = ff_name when set."""
    from app.models.player_stats import PlayerStats

    _, _, user = await _seed_completed_tournament_with_result(
        db_session, username="lbdisplay1", ff_uid="5550000010", ff_name="★LBKing★"
    )

    # Simulate player_stats update so the player appears on leaderboard
    stats_row = (await db_session.execute(
        select(PlayerStats).where(PlayerStats.user_id == user.id)
    )).scalar_one_or_none()
    if stats_row is None:
        stats_row = PlayerStats(user_id=user.id)
        db_session.add(stats_row)
    stats_row.total_tournaments = 1
    stats_row.total_wins = 1
    stats_row.total_kills = 5
    stats_row.total_winnings = Decimal("425.00")
    await db_session.commit()

    # Need a valid player token to call the leaderboard
    player_token = await _register_and_login(
        client, username="lbviewerdisp1", ff_uid="5550000099"
    )
    resp = await client.get(
        "/api/leaderboard/players",
        params={"sort_by": "wins", "limit": 100},
        headers=_auth(player_token),
    )
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]

    our = next((p for p in items if p["username"] == "lbdisplay1"), None)
    assert our is not None, "Player not found in leaderboard"
    assert our["display_name"] == "★LBKing★", (
        f"Expected display_name='★LBKing★', got '{our.get('display_name')}'"
    )
    assert our["username"] == "lbdisplay1"   # login key unchanged


# ─────────────────────────────────────────────────────────────────────────────
# 5. Player public stats returns display_name and ff_name
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_player_public_stats_returns_display_name(
    client: AsyncClient, db_session: AsyncSession
):
    """GET /api/leaderboard/players/{username} must return display_name + ff_name."""
    _, _, user = await _seed_completed_tournament_with_result(
        db_session, username="pubstats1", ff_uid="5550000011", ff_name="꧁PubStar꧂"
    )

    player_token = await _register_and_login(
        client, username="pubstatsviewer1", ff_uid="5550000098"
    )
    resp = await client.get(
        "/api/leaderboard/players/pubstats1",
        headers=_auth(player_token),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["username"] == "pubstats1"
    assert body["ff_name"] == "꧁PubStar꧂"
    assert body["display_name"] == "꧁PubStar꧂"


# ─────────────────────────────────────────────────────────────────────────────
# 6. Player tournament history result rows have display_name
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_player_tournament_history_display_name(
    client: AsyncClient, db_session: AsyncSession
):
    """GET /api/leaderboard/players/{username}/tournament-history result rows include display_name."""
    _, _, user = await _seed_completed_tournament_with_result(
        db_session, username="histdisp1", ff_uid="5550000012", ff_name="HistKing"
    )

    player_token = await _register_and_login(
        client, username="histviewer1", ff_uid="5550000097"
    )
    resp = await client.get(
        "/api/leaderboard/players/histdisp1/tournament-history",
        headers=_auth(player_token),
    )
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert len(items) >= 1

    results = items[0]["results"]
    assert len(results) >= 1
    our_r = next((r for r in results if r["user_id"] == str(user.id)), None)
    assert our_r is not None
    assert our_r["display_name"] == "HistKing"
    assert our_r["username"] == "histdisp1"


# ─────────────────────────────────────────────────────────────────────────────
# 7. Admin completed results history returns ff_name_snapshot
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_admin_history_returns_ff_name_snapshot(
    client: AsyncClient, db_session: AsyncSession
):
    """GET /api/admin/results/history/{id} result rows must include ff_name_snapshot."""
    tourn, result, user = await _seed_completed_tournament_with_result(
        db_session, username="histsnap1", ff_uid="5550000013", ff_name="SnapStar"
    )

    staff_token = await _staff_token(client)
    resp = await client.get(
        f"/api/admin/results/history/{tourn.id}",
        headers=_auth(staff_token),
    )
    assert resp.status_code == 200, resp.text
    results = resp.json()["results"]
    assert len(results) >= 1

    our = next((r for r in results if r["user_id"] == str(user.id)), None)
    assert our is not None
    assert our["ff_name_snapshot"] == "SnapStar", (
        f"Expected ff_name_snapshot='SnapStar', got '{our.get('ff_name_snapshot')}'"
    )
    assert our["username"] == "histsnap1"
    assert our["ff_uid_snapshot"] == "5550000013"


# ─────────────────────────────────────────────────────────────────────────────
# 8. Admin result list (active tournament) returns ff_name_snapshot
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_admin_result_list_returns_ff_name_snapshot(
    client: AsyncClient, db_session: AsyncSession
):
    """GET /api/admin/tournaments/{id}/results must return ff_name_snapshot on each result."""
    from app.models.user import User
    from app.models.tournament import TournamentEntry, EntryStatus
    from app.core.security import hash_password

    ff_name = "ActiveResultKing"
    uname   = "actresult1"
    ff_uid  = "5550000014"

    user = User(
        id=uuid.uuid4(), username=uname, ff_uid=ff_uid, ff_name=ff_name,
        password_hash=hash_password("TestPass@123"),
        referral_code="ACTRSLT1",
        is_active=True, is_banned=False,
    )
    db_session.add(user)
    await db_session.flush([user])

    tourn, _ = await _seed_open_tournament(db_session)

    entry = TournamentEntry(
        id=uuid.uuid4(), tournament_id=tourn.id, user_id=user.id,
        entry_fee_snapshot=Decimal("50.00"), sheet_number=1,
        status=EntryStatus.active,
    )
    db_session.add(entry)
    await db_session.commit()

    staff_token = await _staff_token(client)

    # Enter the result — this triggers the snapshot
    enter_r = await client.post(
        f"/api/admin/tournaments/{tourn.id}/results",
        json={"entry_id": str(entry.id), "placement": 1, "kills": 7},
        headers=_auth(staff_token),
    )
    assert enter_r.status_code == 201, enter_r.text
    assert enter_r.json()["ff_name_snapshot"] == ff_name

    # Fetch all results for this tournament
    list_r = await client.get(
        f"/api/admin/tournaments/{tourn.id}/results",
        headers=_auth(staff_token),
    )
    assert list_r.status_code == 200
    results = list_r.json()["results"]
    assert any(r["ff_name_snapshot"] == ff_name for r in results), (
        f"ff_name_snapshot '{ff_name}' not found in results list"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 9. Admin player leaderboard returns display_name
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_admin_player_leaderboard_returns_display_name(
    client: AsyncClient, db_session: AsyncSession
):
    """GET /api/admin/metrics/leaderboard/players must include display_name."""
    from app.models.player_stats import PlayerStats

    _, _, user = await _seed_completed_tournament_with_result(
        db_session, username="adminlb1", ff_uid="5550000020", ff_name="AdminKing"
    )

    # Ensure player_stats row exists and is non-zero
    stats = (await db_session.execute(
        select(PlayerStats).where(PlayerStats.user_id == user.id)
    )).scalar_one_or_none()
    if stats is None:
        stats = PlayerStats(user_id=user.id)
        db_session.add(stats)
    stats.total_tournaments = 1
    stats.total_wins = 1
    stats.total_kills = 5
    stats.total_winnings = Decimal("425.00")
    await db_session.commit()

    staff_token = await _staff_token(client)
    resp = await client.get(
        "/api/admin/metrics/leaderboard/players",
        params={"limit": 100},
        headers=_auth(staff_token),
    )
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    our = next((p for p in items if p["username"] == "adminlb1"), None)
    assert our is not None, "Player not found in admin leaderboard"
    assert our["display_name"] == "AdminKing", (
        f"Expected display_name='AdminKing', got '{our.get('display_name')}'"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 10. Admin tournament leaderboard result rows return display_name
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_admin_tournament_leaderboard_returns_display_name(
    client: AsyncClient, db_session: AsyncSession
):
    """GET /api/admin/metrics/leaderboard/tournaments must include display_name in top_results."""
    tourn, result, user = await _seed_completed_tournament_with_result(
        db_session, username="admintlb1", ff_uid="5550000021", ff_name="TournKing"
    )

    staff_token = await _staff_token(client)
    resp = await client.get(
        "/api/admin/metrics/leaderboard/tournaments",
        params={"window": "all", "limit": 100},
        headers=_auth(staff_token),
    )
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]

    our_t = next((t for t in items if t["tournament_id"] == str(tourn.id)), None)
    assert our_t is not None, "Tournament not found in admin tournament leaderboard"

    top = our_t.get("top_results", [])
    assert len(top) >= 1
    our_r = next((r for r in top if r["user_id"] == str(user.id)), None)
    assert our_r is not None
    assert our_r["display_name"] == "TournKing", (
        f"Expected display_name='TournKing', got '{our_r.get('display_name')}'"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 11. Username, FF UID, rankings, kills, prizes unchanged
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_username_and_ff_uid_unchanged_in_leaderboard(
    client: AsyncClient, db_session: AsyncSession
):
    """Adding display_name must not alter username or ff_uid fields."""
    from app.models.player_stats import PlayerStats

    _, _, user = await _seed_completed_tournament_with_result(
        db_session, username="integrcheck1", ff_uid="5550000030", ff_name="IntegName"
    )

    stats = (await db_session.execute(
        select(PlayerStats).where(PlayerStats.user_id == user.id)
    )).scalar_one_or_none()
    if stats is None:
        stats = PlayerStats(user_id=user.id)
        db_session.add(stats)
    stats.total_tournaments = 1
    stats.total_wins = 1
    stats.total_kills = 5
    stats.total_winnings = Decimal("425.00")
    await db_session.commit()

    p_tok = await _register_and_login(client, username="integviewer1", ff_uid="5550000089")
    resp = await client.get(
        "/api/leaderboard/players",
        params={"sort_by": "wins", "limit": 100},
        headers=_auth(p_tok),
    )
    assert resp.status_code == 200
    items = resp.json()["items"]
    our = next((p for p in items if p["username"] == "integrcheck1"), None)
    assert our is not None
    # Core fields unchanged
    assert our["username"] == "integrcheck1"
    assert our["total_wins"] == 1
    assert our["total_kills"] == 5
    assert Decimal(our["total_winnings"]) == Decimal("425.00")
    # display_name present
    assert our["display_name"] == "IntegName"


@pytest.mark.asyncio
async def test_history_result_monetary_values_unchanged(
    client: AsyncClient, db_session: AsyncSession
):
    """ff_name_snapshot addition must not alter prize/kill reward/total payout values."""
    tourn, result, user = await _seed_completed_tournament_with_result(
        db_session, username="moneycheck1", ff_uid="5550000031",
        ff_name="MoneyKing", placement=1, kills=5,
    )

    staff_token = await _staff_token(client)
    resp = await client.get(
        f"/api/admin/results/history/{tourn.id}",
        headers=_auth(staff_token),
    )
    assert resp.status_code == 200
    results = resp.json()["results"]
    our = next((r for r in results if r["user_id"] == str(user.id)), None)
    assert our is not None
    assert Decimal(our["prize_amount"]) == Decimal("400.00")
    assert Decimal(our["kill_reward_amount"]) == Decimal("25.00")
    assert Decimal(our["total_payout"]) == Decimal("425.00")
    assert our["placement"] == 1
    assert our["kills"] == 5
    # ff fields
    assert our["ff_uid_snapshot"] == "5550000031"
    assert our["ff_name_snapshot"] == "MoneyKing"


# ─────────────────────────────────────────────────────────────────────────────
# 12. Player with no ff_name — falls back to username gracefully
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_display_name_fallback_to_username_when_no_ff_name(
    client: AsyncClient, db_session: AsyncSession
):
    """When ff_name is NULL, display_name must equal username (no crash, no empty string)."""
    from app.models.player_stats import PlayerStats

    _, _, user = await _seed_completed_tournament_with_result(
        db_session, username="noffname1", ff_uid="5550000040", ff_name=None
    )

    stats = (await db_session.execute(
        select(PlayerStats).where(PlayerStats.user_id == user.id)
    )).scalar_one_or_none()
    if stats is None:
        stats = PlayerStats(user_id=user.id)
        db_session.add(stats)
    stats.total_tournaments = 1
    stats.total_wins = 1
    stats.total_winnings = Decimal("425.00")
    await db_session.commit()

    p_tok = await _register_and_login(client, username="noffviewer1", ff_uid="5550000088")
    resp = await client.get(
        "/api/leaderboard/players",
        params={"sort_by": "wins", "limit": 100},
        headers=_auth(p_tok),
    )
    assert resp.status_code == 200
    items = resp.json()["items"]
    our = next((p for p in items if p["username"] == "noffname1"), None)
    assert our is not None
    # Falls back to username
    assert our["display_name"] == "noffname1"
    assert our["username"] == "noffname1"


# ─────────────────────────────────────────────────────────────────────────────
# 13. History endpoint returns live ff_name field (new — for pre-migration fallback)
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_admin_history_result_includes_live_ff_name_field(
    client: AsyncClient, db_session: AsyncSession
):
    """
    GET /api/admin/results/history/{id} result rows must include a
    `ff_name` field (live current name).  This is the fallback used by
    the frontend for results entered before migration 0012, where
    ff_name_snapshot is NULL.
    """
    tourn, result, user = await _seed_completed_tournament_with_result(
        db_session,
        username="livenamefb1",
        ff_uid="5550000050",
        ff_name="LiveFallback",
    )

    staff_token = await _staff_token(client)
    resp = await client.get(
        f"/api/admin/results/history/{tourn.id}",
        headers=_auth(staff_token),
    )
    assert resp.status_code == 200, resp.text
    results = resp.json()["results"]
    our = next((r for r in results if r["user_id"] == str(user.id)), None)
    assert our is not None

    # Schema must include the ff_name key (may be None if player has none)
    assert "ff_name" in our, "ff_name field missing from CompletedHistoryResultItem response"
    assert our["ff_name"] == "LiveFallback", (
        f"Expected ff_name='LiveFallback', got '{our.get('ff_name')}'"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 14. Pre-migration result (ff_name_snapshot=None): live ff_name fills in
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_history_null_snapshot_uses_live_ff_name_fallback(
    client: AsyncClient, db_session: AsyncSession
):
    """
    For a result where ff_name_snapshot is NULL (pre-migration 0012 scenario),
    the history endpoint must return the player's current ff_name in the
    `ff_name` field so the frontend can display it as a fallback.
    Priority chain: ff_name_snapshot -> ff_name -> username.
    """
    import random, string
    from app.core.security import hash_password
    from app.models.user import User
    from app.models.staff_user import StaffUser
    from app.models.tournament import Tournament, TournamentStatus, TournamentMode, TournamentGame, TournamentEntry, EntryStatus
    from app.models.result import TournamentResult, ResultStatus

    uname  = "premig1"
    ff_uid = "5550000051"
    current_ff_name = "PostMigration"

    user = User(
        id=uuid.uuid4(), username=uname, ff_uid=ff_uid,
        ff_name=current_ff_name,     # player has a name set NOW
        password_hash=hash_password("TestPass@123"),
        referral_code="PREMIG001",
        is_active=True, is_banned=False,
    )
    db_session.add(user)
    await db_session.flush([user])

    admin = (await db_session.execute(select(StaffUser).limit(1))).scalar_one()
    now = _utcnow()
    suffix = "".join(random.choices(string.ascii_uppercase + string.digits, k=4))

    tourn = Tournament(
        id=uuid.uuid4(),
        tournament_code=f"T-PM{suffix}",
        title=f"PreMig Test {suffix}",
        game=TournamentGame.free_fire,
        mode=TournamentMode.solo,
        entry_fee=Decimal("50.00"),
        max_players=50, min_players=2,
        player_count=1, next_sheet_number=2,
        status=TournamentStatus.completed,
        scheduled_at=now, completed_at=now,
        created_by_id=admin.id,
        created_by_display_name=admin.display_name,
        created_by_role=admin.role.value,
    )
    db_session.add(tourn)
    await db_session.flush([tourn])

    entry = TournamentEntry(
        id=uuid.uuid4(), tournament_id=tourn.id, user_id=user.id,
        entry_fee_snapshot=Decimal("50.00"), sheet_number=1,
        status=EntryStatus.active,
    )
    db_session.add(entry)
    await db_session.flush([entry])

    result = TournamentResult(
        id=uuid.uuid4(),
        tournament_id=tourn.id, entry_id=entry.id, user_id=user.id,
        ff_uid_snapshot=ff_uid,
        ff_name_snapshot=None,       # simulate pre-migration 0012: snapshot not recorded
        placement=1, kills=3,
        prize_amount=Decimal("400.00"),
        kill_reward_amount=Decimal("15.00"),
        kill_reward_eligible=True,
        payout_distributed=True,
        payout_distributed_at=now,
        result_status=ResultStatus.finalized,
        entered_by_id=admin.id, entered_at=now,
        verified_by_id=admin.id, verified_at=now,
    )
    db_session.add(result)
    await db_session.commit()

    staff_token = await _staff_token(client)
    resp = await client.get(
        f"/api/admin/results/history/{tourn.id}",
        headers=_auth(staff_token),
    )
    assert resp.status_code == 200, resp.text
    results_data = resp.json()["results"]
    our = next((r for r in results_data if r["user_id"] == str(user.id)), None)
    assert our is not None

    # Snapshot is NULL (pre-migration) — backend should surface live ff_name
    assert our["ff_name_snapshot"] is None, (
        "Expected ff_name_snapshot=None for this pre-migration result"
    )
    assert our["ff_name"] == current_ff_name, (
        f"Expected ff_name='{current_ff_name}' as live fallback, got '{our.get('ff_name')}'"
    )
    assert our["username"] == uname   # login name unchanged


# ─────────────────────────────────────────────────────────────────────────────
# 15. CompletedHistoryResultItem schema has ff_name field
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_completed_history_result_item_schema_has_ff_name():
    """CompletedHistoryResultItem Pydantic schema must declare ff_name field."""
    from app.schemas.result import CompletedHistoryResultItem
    fields = CompletedHistoryResultItem.model_fields
    assert "ff_name" in fields, "ff_name field missing from CompletedHistoryResultItem schema"
    assert "ff_name_snapshot" in fields, "ff_name_snapshot field missing"
    assert "username" in fields, "username field missing"
    # ff_name is nullable (Optional)
    assert fields["ff_name"].is_required() is False, "ff_name should be Optional (not required)"


# ─────────────────────────────────────────────────────────────────────────────
# 16. Stylised FF Name (Unicode) round-trips correctly through the API
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_unicode_ff_name_roundtrip(client: AsyncClient, db_session: AsyncSession):
    """
    A stylised FF Name with Unicode characters (꧁MANISH꧂) must be stored
    and returned without corruption through the entries and history endpoints.
    Verifies: username=manish7610, ff_name=꧁MANISH꧂, ff_uid=123456789
    """
    import random, string
    from app.core.security import hash_password
    from app.models.user import User

    uname   = "manish7610"
    ff_uid  = "123456789"
    ff_name = "꧁MANISH꧂"

    user = User(
        id=uuid.uuid4(), username=uname, ff_uid=ff_uid, ff_name=ff_name,
        password_hash=hash_password("TestPass@123"),
        referral_code="MANISH001",
        is_active=True, is_banned=False,
    )
    db_session.add(user)
    await db_session.commit()

    tourn, _ = await _seed_open_tournament(db_session)

    from app.models.tournament import TournamentEntry, EntryStatus
    entry = TournamentEntry(
        id=uuid.uuid4(), tournament_id=tourn.id, user_id=user.id,
        entry_fee_snapshot=Decimal("50.00"), sheet_number=1,
        status=EntryStatus.active,
    )
    db_session.add(entry)
    await db_session.commit()

    staff_token = await _staff_token(client)

    # 1. Entries endpoint must return the Unicode ff_name intact
    entries_r = await client.get(
        f"/api/admin/tournaments/{tourn.id}/entries",
        headers=_auth(staff_token),
    )
    assert entries_r.status_code == 200, entries_r.text
    our_entry = next(
        (e for e in entries_r.json()["items"] if e["user_id"] == str(user.id)), None
    )
    assert our_entry is not None
    assert our_entry["ff_name"] == "꧁MANISH꧂", (
        f"Expected ff_name='꧁MANISH꧂', got '{our_entry.get('ff_name')}'"
    )
    assert our_entry["username"] == "manish7610"
    assert our_entry["ff_uid"] == "123456789"

    # 2. Enter result — snapshot must preserve Unicode
    enter_r = await client.post(
        f"/api/admin/tournaments/{tourn.id}/results",
        json={"entry_id": str(entry.id), "placement": 1, "kills": 4},
        headers=_auth(staff_token),
    )
    assert enter_r.status_code == 201, enter_r.text
    assert enter_r.json()["ff_name_snapshot"] == "꧁MANISH꧂", (
        "Unicode ff_name_snapshot corrupted"
    )

    # 3. Result list must carry snapshot
    results_r = await client.get(
        f"/api/admin/tournaments/{tourn.id}/results",
        headers=_auth(staff_token),
    )
    assert results_r.status_code == 200
    our_r = next(
        (r for r in results_r.json()["results"] if r["ff_name_snapshot"] == "꧁MANISH꧂"),
        None,
    )
    assert our_r is not None, "Unicode ff_name_snapshot not in result list"
