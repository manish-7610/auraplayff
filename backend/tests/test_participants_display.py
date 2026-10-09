"""
Participants table field-mapping tests.

Proves that GET /api/admin/tournaments/{id}/entries returns:
  - username   = login key     (never ff_name, never ff_uid)
  - ff_name    = gaming name   (never username, never ff_uid)
  - ff_uid     = numeric UID   (never username, never ff_name)
  - All three are independent; no cross-field contamination

Also proves:
  - Unicode / stylish FF names round-trip without corruption
  - When ff_name is NULL the column value is null/absent, not ff_uid
  - When ff_name is set it is not equal to ff_uid
  - Result Entry dropdown (admin-results) still gets the same correct fields
    (verified by inspecting the same /entries response the dropdown consumes)

Scenario: username=manish7610, ff_name=꧁MANISH꧂, ff_uid=123456789
"""
from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import login_staff


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

async def _staff_token(client: AsyncClient) -> str:
    resp = await login_staff(client)
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _seed_player_and_entry(
    db: AsyncSession,
    *,
    username: str,
    ff_uid: str,
    ff_name: str | None,
    referral_code: str,
) -> tuple:
    """Create a User + open tournament + TournamentEntry. Returns (user, tournament, entry)."""
    import random, string as _s
    from app.core.security import hash_password
    from app.models.user import User
    from app.models.staff_user import StaffUser
    from app.models.tournament import (
        Tournament, TournamentStatus, TournamentMode,
        TournamentGame, TournamentEntry, EntryStatus,
    )
    from app.models._types import utcnow

    user = User(
        id=uuid.uuid4(),
        username=username,
        ff_uid=ff_uid,
        ff_name=ff_name,
        password_hash=hash_password("TestPass@123"),
        referral_code=referral_code,
        is_active=True,
        is_banned=False,
    )
    db.add(user)
    await db.flush([user])

    admin = (await db.execute(select(StaffUser).limit(1))).scalar_one()
    suffix = "".join(random.choices(_s.ascii_uppercase + _s.digits, k=5))

    tourn = Tournament(
        id=uuid.uuid4(),
        tournament_code=f"T-PD{suffix}",
        title=f"PD Test {suffix}",
        game=TournamentGame.free_fire,
        mode=TournamentMode.solo,
        entry_fee=Decimal("40.00"),
        max_players=50,
        min_players=2,
        player_count=1,
        next_sheet_number=2,
        status=TournamentStatus.result_entry,
        scheduled_at=utcnow(),
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
        entry_fee_snapshot=Decimal("40.00"),
        sheet_number=1,
        status=EntryStatus.active,
    )
    db.add(entry)
    await db.commit()
    return user, tourn, entry


# ─────────────────────────────────────────────────────────────────────────────
# 1. API returns all three identity fields independently
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_entries_api_returns_all_three_identity_fields(
    client: AsyncClient, db_session: AsyncSession
):
    """
    GET /api/admin/tournaments/{id}/entries must return username, ff_name, and ff_uid
    as separate, independent fields in each entry item.
    """
    user, tourn, entry = await _seed_player_and_entry(
        db_session,
        username="manish7610",
        ff_uid="123456789",
        ff_name="꧁MANISH꧂",
        referral_code="MNSH0001",
    )

    token = await _staff_token(client)
    resp = await client.get(
        f"/api/admin/tournaments/{tourn.id}/entries",
        headers=_auth(token),
    )
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert len(items) == 1

    e = items[0]
    # All three fields must be present
    assert "username" in e
    assert "ff_uid"   in e
    assert "ff_name"  in e

    # Each field must hold exactly the right value
    assert e["username"] == "manish7610",  f"username wrong: {e['username']}"
    assert e["ff_uid"]   == "123456789",   f"ff_uid wrong:   {e['ff_uid']}"
    assert e["ff_name"]  == "꧁MANISH꧂",   f"ff_name wrong:  {e['ff_name']}"


# ─────────────────────────────────────────────────────────────────────────────
# 2. ff_uid is NEVER placed in the ff_name field
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ff_uid_is_never_placed_in_ff_name(
    client: AsyncClient, db_session: AsyncSession
):
    """
    The specific bug: ff_name must NOT contain the ff_uid value.
    This was the reported symptom: FF NAME column showed 123456789.
    """
    user, tourn, _ = await _seed_player_and_entry(
        db_session,
        username="bugcheck1",
        ff_uid="987654321",
        ff_name="★BugKing★",
        referral_code="BUGCK001",
    )

    token = await _staff_token(client)
    resp = await client.get(
        f"/api/admin/tournaments/{tourn.id}/entries",
        headers=_auth(token),
    )
    assert resp.status_code == 200, resp.text
    e = resp.json()["items"][0]

    # The critical assertion: ff_name must never equal ff_uid
    assert e["ff_name"] != e["ff_uid"], (
        f"BUG: ff_name is '{e['ff_name']}' which equals ff_uid '{e['ff_uid']}'"
    )
    assert e["ff_name"] == "★BugKing★"
    assert e["ff_uid"]  == "987654321"


# ─────────────────────────────────────────────────────────────────────────────
# 3. username is NEVER replaced by ff_name in the API response
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_username_is_never_replaced_by_ff_name(
    client: AsyncClient, db_session: AsyncSession
):
    """
    username field in the API response must always be the login key,
    never replaced by ff_name even when ff_name is set.
    """
    user, tourn, _ = await _seed_player_and_entry(
        db_session,
        username="loginkey1",
        ff_uid="111222333",
        ff_name="꧁GamingKing꧂",
        referral_code="LGNKY001",
    )

    token = await _staff_token(client)
    resp = await client.get(
        f"/api/admin/tournaments/{tourn.id}/entries",
        headers=_auth(token),
    )
    assert resp.status_code == 200, resp.text
    e = resp.json()["items"][0]

    assert e["username"] == "loginkey1",    f"username must be login key, got: {e['username']}"
    assert e["username"] != e["ff_name"],   f"username must not equal ff_name"
    assert e["username"] != e["ff_uid"],    f"username must not equal ff_uid"


# ─────────────────────────────────────────────────────────────────────────────
# 4. ff_name is NULL when player has not set a gaming name
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ff_name_is_null_not_ff_uid_when_not_set(
    client: AsyncClient, db_session: AsyncSession
):
    """
    When a player has no ff_name set (NULL), the API must return null for
    ff_name — NOT the ff_uid value.  This directly tests the reported bug
    scenario where the FF NAME column showed the UID.
    """
    user, tourn, _ = await _seed_player_and_entry(
        db_session,
        username="noffname2",
        ff_uid="444555666",
        ff_name=None,    # player has not set their gaming name
        referral_code="NOFN0002",
    )

    token = await _staff_token(client)
    resp = await client.get(
        f"/api/admin/tournaments/{tourn.id}/entries",
        headers=_auth(token),
    )
    assert resp.status_code == 200, resp.text
    e = resp.json()["items"][0]

    # ff_name must be null, not ff_uid
    assert e["ff_name"] is None, (
        f"ff_name should be null but got: '{e['ff_name']}'"
    )
    # ff_uid must still be present and correct
    assert e["ff_uid"] == "444555666"
    # username must still be correct
    assert e["username"] == "noffname2"


# ─────────────────────────────────────────────────────────────────────────────
# 5. Unicode / stylish FF names round-trip without corruption
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_unicode_ff_name_not_corrupted_in_entries_api(
    client: AsyncClient, db_session: AsyncSession
):
    """
    Unicode stylish Free Fire names must survive the full round-trip
    DB → API response → JSON without corruption.
    Tests the exact character set from the spec: ꧁MANISH꧂
    """
    stylish_names = [
        ("unicode1", "100000001", "꧁MANISH꧂",   "UNI00001"),
        ("unicode2", "100000002", "★MANISH★",    "UNI00002"),
        ("unicode3", "100000003", "ᴍᴀɴɪsʜ",     "UNI00003"),
        ("unicode4", "100000004", "MANISH⚡",    "UNI00004"),
        ("unicode5", "100000005", "MANISH*7610*", "UNI00005"),
    ]

    token = await _staff_token(client)

    for username, ff_uid, ff_name, ref_code in stylish_names:
        user, tourn, _ = await _seed_player_and_entry(
            db_session,
            username=username,
            ff_uid=ff_uid,
            ff_name=ff_name,
            referral_code=ref_code,
        )

        resp = await client.get(
            f"/api/admin/tournaments/{tourn.id}/entries",
            headers=_auth(token),
        )
        assert resp.status_code == 200, f"Failed for {username}: {resp.text}"
        e = resp.json()["items"][0]

        assert e["ff_name"] == ff_name, (
            f"Unicode corruption for {username}: "
            f"stored '{ff_name}', got '{e['ff_name']}'"
        )
        assert e["ff_uid"]   == ff_uid,    f"ff_uid wrong for {username}"
        assert e["username"] == username,  f"username wrong for {username}"
        # No cross-contamination
        assert e["ff_name"] != e["ff_uid"],   f"ff_name == ff_uid for {username}"
        assert e["ff_name"] != e["username"], f"ff_name == username for {username}"


# ─────────────────────────────────────────────────────────────────────────────
# 6. Result Entry dropdown gets the same correct fields
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_result_entry_dropdown_fields_unchanged(
    client: AsyncClient, db_session: AsyncSession
):
    """
    The Result Entry dropdown (admin-results.js _populateEntrySelector) consumes
    the same /entries endpoint.  Verify it still receives all three fields
    needed for its label: '[#N] username / FF: ff_name (ff_uid)'
    This test must NOT fail — the dropdown was already working correctly.
    """
    user, tourn, entry = await _seed_player_and_entry(
        db_session,
        username="dropdown1",
        ff_uid="777888999",
        ff_name="DropKing",
        referral_code="DROP0001",
    )

    token = await _staff_token(client)
    resp = await client.get(
        f"/api/admin/tournaments/{tourn.id}/entries",
        headers=_auth(token),
    )
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    e = items[0]

    # The dropdown needs all four of these fields present and correct
    assert e["entry_id"]     == str(entry.id),  "entry_id missing"
    assert e["sheet_number"] == 1,              "sheet_number wrong"
    assert e["username"]     == "dropdown1",    "username wrong"
    assert e["ff_name"]      == "DropKing",     "ff_name wrong"
    assert e["ff_uid"]       == "777888999",    "ff_uid wrong"

    # Simulate exactly what _populateEntrySelector builds:
    # `[#1] dropdown1 / FF: DropKing (777888999)`
    expected_label = f"[#{e['sheet_number']}] {e['username']} / FF: {e['ff_name']} ({e['ff_uid']})"
    assert expected_label == "[#1] dropdown1 / FF: DropKing (777888999)"


# ─────────────────────────────────────────────────────────────────────────────
# 7. Result Entry dropdown when ff_name is NULL
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_result_entry_dropdown_fields_when_no_ff_name(
    client: AsyncClient, db_session: AsyncSession
):
    """
    When ff_name is NULL, the dropdown falls back to '[#N] username (ff_uid)'.
    Verify that the API still supplies a correct ff_uid and null ff_name
    so the dropdown can build the right label.
    """
    user, tourn, entry = await _seed_player_and_entry(
        db_session,
        username="nodrop1",
        ff_uid="111333555",
        ff_name=None,
        referral_code="NDROP001",
    )

    token = await _staff_token(client)
    resp = await client.get(
        f"/api/admin/tournaments/{tourn.id}/entries",
        headers=_auth(token),
    )
    assert resp.status_code == 200, resp.text
    e = resp.json()["items"][0]

    assert e["ff_name"]  is None,        "ff_name must be null"
    assert e["ff_uid"]   == "111333555", "ff_uid must be correct"
    assert e["username"] == "nodrop1",   "username must be correct"

    # Simulate the dropdown no-ff_name branch:
    # `[#1] nodrop1 (111333555)`
    expected_label = f"[#{e['sheet_number']}] {e['username']} ({e['ff_uid']})"
    assert expected_label == "[#1] nodrop1 (111333555)"


# ─────────────────────────────────────────────────────────────────────────────
# 8. Multiple players — no cross-contamination between rows
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_multiple_players_no_cross_contamination(
    client: AsyncClient, db_session: AsyncSession
):
    """
    When multiple players are enrolled, each row must map to its own
    username/ff_name/ff_uid without cross-contamination.
    """
    import random, string as _s
    from app.core.security import hash_password
    from app.models.user import User
    from app.models.staff_user import StaffUser
    from app.models.tournament import (
        Tournament, TournamentStatus, TournamentMode,
        TournamentGame, TournamentEntry, EntryStatus,
    )
    from app.models._types import utcnow

    players = [
        ("multi1", "200000001", "꧁Alpha꧂",  "MLT00001"),
        ("multi2", "200000002", "★Beta★",    "MLT00002"),
        ("multi3", "200000003", None,         "MLT00003"),  # no ff_name
    ]

    admin = (await db_session.execute(select(StaffUser).limit(1))).scalar_one()
    suffix = "MULTI"

    tourn = Tournament(
        id=uuid.uuid4(),
        tournament_code=f"T-{suffix}",
        title=f"Multi Test",
        game=TournamentGame.free_fire,
        mode=TournamentMode.solo,
        entry_fee=Decimal("40.00"),
        max_players=50,
        min_players=2,
        player_count=3,
        next_sheet_number=4,
        status=TournamentStatus.result_entry,
        scheduled_at=utcnow(),
        created_by_id=admin.id,
        created_by_display_name=admin.display_name,
        created_by_role=admin.role.value,
    )
    db_session.add(tourn)
    await db_session.flush([tourn])

    expected = {}
    for i, (uname, ff_uid, ff_name, ref_code) in enumerate(players, start=1):
        u = User(
            id=uuid.uuid4(),
            username=uname, ff_uid=ff_uid, ff_name=ff_name,
            password_hash=hash_password("TestPass@123"),
            referral_code=ref_code,
            is_active=True, is_banned=False,
        )
        db_session.add(u)
        await db_session.flush([u])

        entry = TournamentEntry(
            id=uuid.uuid4(), tournament_id=tourn.id, user_id=u.id,
            entry_fee_snapshot=Decimal("40.00"),
            sheet_number=i, status=EntryStatus.active,
        )
        db_session.add(entry)
        expected[uname] = {"ff_uid": ff_uid, "ff_name": ff_name}

    await db_session.commit()

    token = await _staff_token(client)
    resp = await client.get(
        f"/api/admin/tournaments/{tourn.id}/entries",
        headers=_auth(token),
    )
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert len(items) == 3

    for item in items:
        uname = item["username"]
        assert uname in expected, f"Unexpected username: {uname}"
        exp = expected[uname]

        assert item["ff_uid"]  == exp["ff_uid"],  (
            f"{uname}: ff_uid expected '{exp['ff_uid']}', got '{item['ff_uid']}'"
        )
        assert item["ff_name"] == exp["ff_name"], (
            f"{uname}: ff_name expected '{exp['ff_name']}', got '{item['ff_name']}'"
        )
        # No row should have ff_name == ff_uid (unless player stupidly named themselves their UID)
        if item["ff_name"] is not None:
            assert item["ff_name"] != item["ff_uid"], (
                f"{uname}: ff_name must not equal ff_uid"
            )


# ─────────────────────────────────────────────────────────────────────────────
# 9. AdminEntryResponse schema field declarations
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_admin_entry_response_schema_has_correct_fields():
    """
    AdminEntryResponse Pydantic schema must declare username, ff_uid, and ff_name
    as separate fields with correct types.
    """
    from app.schemas.tournament import AdminEntryResponse
    fields = AdminEntryResponse.model_fields

    assert "username" in fields,   "username field missing from AdminEntryResponse"
    assert "ff_uid"   in fields,   "ff_uid field missing from AdminEntryResponse"
    assert "ff_name"  in fields,   "ff_name field missing from AdminEntryResponse"

    # ff_name must be Optional (not required)
    assert fields["ff_name"].is_required() is False, "ff_name should be Optional"
    # ff_uid must be required (it's always set)
    assert fields["ff_uid"].is_required() is True,  "ff_uid should be required"
    # username must be required
    assert fields["username"].is_required() is True, "username should be required"


# ─────────────────────────────────────────────────────────────────────────────
# 10. Exact scenario from bug report: manish7610 / ꧁MANISH꧂ / 123456789
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_exact_bug_report_scenario(
    client: AsyncClient, db_session: AsyncSession
):
    """
    Reproduces the exact scenario from the bug report:
      username = manish7610
      ff_name  = ꧁MANISH꧂
      ff_uid   = 123456789

    Asserts:
      - PLAYER column data (username) = 'manish7610'
      - FF NAME column data (ff_name) = '꧁MANISH꧂'   (NOT '123456789')
      - FF UID column data  (ff_uid)  = '123456789'   (NOT '꧁MANISH꧂')
    """
    user, tourn, _ = await _seed_player_and_entry(
        db_session,
        username="manish7610",
        ff_uid="123456789",
        ff_name="꧁MANISH꧂",
        referral_code="MNSH0002",
    )

    token = await _staff_token(client)
    resp = await client.get(
        f"/api/admin/tournaments/{tourn.id}/entries",
        headers=_auth(token),
    )
    assert resp.status_code == 200, resp.text
    e = resp.json()["items"][0]

    # ── PLAYER column data ────────────────────────────────────────────────────
    player_col = e["username"]
    assert player_col == "manish7610", (
        f"PLAYER column: expected 'manish7610', got '{player_col}'"
    )

    # ── FF NAME column data ───────────────────────────────────────────────────
    ff_name_col = e["ff_name"]
    assert ff_name_col == "꧁MANISH꧂", (
        f"FF NAME column: expected '꧁MANISH꧂', got '{ff_name_col}'\n"
        f"  (if this shows '123456789', the ff_uid is being placed in ff_name — THE BUG)"
    )
    assert ff_name_col != "123456789", (
        f"BUG CONFIRMED: FF NAME column contains the FF UID value '123456789'"
    )

    # ── FF UID column data ────────────────────────────────────────────────────
    ff_uid_col = e["ff_uid"]
    assert ff_uid_col == "123456789", (
        f"FF UID column: expected '123456789', got '{ff_uid_col}'"
    )
    assert ff_uid_col != "꧁MANISH꧂", (
        f"BUG: FF UID column contains the FF name value"
    )

    # ── Cross-field independence ──────────────────────────────────────────────
    assert e["username"] != e["ff_name"], "username must not equal ff_name"
    assert e["username"] != e["ff_uid"],  "username must not equal ff_uid"
    assert e["ff_name"]  != e["ff_uid"],  "ff_name must not equal ff_uid"
