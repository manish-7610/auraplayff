"""
Mandatory ff_name onboarding — focused tests for Problem #2.

Spec scenarios:

A. Existing player WITH ff_name:
   - username+password login works
   - tournament join succeeds

B. New player WITHOUT ff_name:
   - username+password login works (auth itself is never blocked)
   - tournament join is rejected (403 FF_NAME_REQUIRED)
   - no wallet deduction occurs
   - no tournament entry record is created

C. Save FF Name then join:
   - PATCH /api/users/me sets ff_name
   - subsequent tournament join succeeds

D. Login identity assertions:
   - username+password is the only accepted login credential
   - ff_name cannot be used as a login username
   - ff_uid cannot be used as a login username

E. Existing ff_name validation (unchanged):
   - Unicode/stylish names accepted
   - 15-char Unicode limit enforced
   - HTML angle brackets rejected
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


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _register(
    client: AsyncClient,
    *,
    username: str,
    ff_uid: str,
    ff_name: str | None = None,
) -> dict:
    """Register a player. ff_name is optional — omit to simulate 'no gaming name'."""
    payload: dict = {
        "username": username,
        "password": "TestPass@123",
        "confirm_password": "TestPass@123",
        "ff_uid": ff_uid,
    }
    if ff_name is not None:
        payload["ff_name"] = ff_name
    resp = await client.post("/api/auth/register", json=payload)
    assert resp.status_code == 201, f"Registration failed: {resp.text}"
    return resp.json()


async def _login(client: AsyncClient, username: str, password: str = "TestPass@123") -> str:
    """Login and return the access token."""
    resp = await client.post(
        "/api/auth/login",
        json={"username": username, "password": password},
    )
    assert resp.status_code == 200, f"Login failed: {resp.text}"
    return resp.json()["access_token"]


async def _seed_open_tournament(db: AsyncSession) -> "Tournament":  # type: ignore[name-defined]
    """Seed a minimal OPEN tournament for join tests (₹10 entry fee)."""
    import random, string as _s
    from app.models.staff_user import StaffUser
    from app.models.tournament import Tournament, TournamentStatus, TournamentMode, TournamentGame
    from app.models._types import utcnow

    admin = (await db.execute(select(StaffUser).limit(1))).scalar_one()
    suffix = "".join(random.choices(_s.ascii_uppercase + _s.digits, k=5))
    t = Tournament(
        id=uuid.uuid4(),
        tournament_code=f"T-OB{suffix}",
        title=f"Onboarding Test {suffix}",
        game=TournamentGame.free_fire,
        mode=TournamentMode.solo,
        entry_fee=Decimal("10.00"),  # smallest supported fee (must be > 0)
        max_players=50,
        min_players=1,
        player_count=0,
        next_sheet_number=1,
        status=TournamentStatus.open,
        scheduled_at=utcnow(),
        created_by_id=admin.id,
        created_by_display_name=admin.display_name,
        created_by_role=admin.role.value,
    )
    db.add(t)
    await db.commit()
    return t


async def _seed_wallet(db: AsyncSession, username: str, amount: Decimal = Decimal("200.00")) -> None:
    """Give a registered player deposit balance so they can afford entry fees."""
    from app.models.user import User
    from app.models.wallet import Wallet

    user = (await db.execute(select(User).where(User.username == username))).scalar_one()
    wallet = (await db.execute(select(Wallet).where(Wallet.user_id == user.id))).scalar_one()
    wallet.deposit_balance = amount
    await db.commit()


# ─────────────────────────────────────────────────────────────────────────────
# A. Existing player WITH ff_name
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_player_with_ff_name_can_login(client: AsyncClient):
    """A player registered with ff_name must be able to log in normally."""
    await _register(client, username="ob_hff1", ff_uid="6610000001", ff_name="꧁GamingKing꧂")
    token = await _login(client, "ob_hff1")
    assert token, "Expected access token after login"


@pytest.mark.asyncio
async def test_player_with_ff_name_can_join_tournament(
    client: AsyncClient, db_session: AsyncSession
):
    """A player with ff_name set must be able to join an open tournament."""
    await _register(client, username="ob_hff2", ff_uid="6610000002", ff_name="★JoinKing★")
    token = await _login(client, "ob_hff2")
    tourn = await _seed_open_tournament(db_session)
    await _seed_wallet(db_session, "ob_hff2")

    resp = await client.post(
        f"/api/tournaments/{tourn.id}/join",
        headers=_bearer(token),
    )
    assert resp.status_code == 201, (
        f"Player with ff_name should be able to join. Got {resp.status_code}: {resp.text}"
    )
    body = resp.json()
    assert "entry_id" in body
    assert body["tournament_id"] == str(tourn.id)


@pytest.mark.asyncio
async def test_player_with_ff_name_login_does_not_trigger_onboarding_api(client: AsyncClient):
    """GET /api/users/me for a player with ff_name must return a non-null ff_name."""
    await _register(client, username="ob_hff3", ff_uid="6610000003", ff_name="NormalPlayer")
    token = await _login(client, "ob_hff3")

    resp = await client.get("/api/users/me", headers=_bearer(token))
    assert resp.status_code == 200
    me = resp.json()
    assert me["ff_name"] == "NormalPlayer", (
        f"Expected ff_name='NormalPlayer', got '{me.get('ff_name')}'"
    )
    assert me["username"] == "ob_hff3"


# ─────────────────────────────────────────────────────────────────────────────
# B. New player WITHOUT ff_name
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_player_without_ff_name_can_login(client: AsyncClient):
    """Authentication must work even when ff_name is not set — login is never blocked."""
    await _register(client, username="ob_nff1", ff_uid="6620000001")  # no ff_name
    token = await _login(client, "ob_nff1")
    assert token, "Login must succeed even without ff_name"


@pytest.mark.asyncio
async def test_player_without_ff_name_gets_null_from_me(client: AsyncClient):
    """GET /api/users/me must return ff_name=null for a player who never set it."""
    await _register(client, username="ob_nff2", ff_uid="6620000002")
    token = await _login(client, "ob_nff2")

    resp = await client.get("/api/users/me", headers=_bearer(token))
    assert resp.status_code == 200
    assert resp.json()["ff_name"] is None, "ff_name must be null when not set"


@pytest.mark.asyncio
async def test_join_blocked_without_ff_name_returns_403(
    client: AsyncClient, db_session: AsyncSession
):
    """
    POST /api/tournaments/{id}/join must return 403 FF_NAME_REQUIRED
    when the player has no ff_name set.
    """
    await _register(client, username="ob_nff3", ff_uid="6620000003")
    token = await _login(client, "ob_nff3")
    tourn = await _seed_open_tournament(db_session)
    # No wallet seeded intentionally — the ff_name guard fires before the wallet check

    resp = await client.post(
        f"/api/tournaments/{tourn.id}/join",
        headers=_bearer(token),
    )
    assert resp.status_code == 403, (
        f"Expected 403 FF_NAME_REQUIRED, got {resp.status_code}: {resp.text}"
    )
    body = resp.json()
    assert body["error"]["code"] == "FF_NAME_REQUIRED", (
        f"Expected error code FF_NAME_REQUIRED, got: {body}"
    )


@pytest.mark.asyncio
async def test_join_blocked_without_ff_name_no_entry_created(
    client: AsyncClient, db_session: AsyncSession
):
    """
    When FF_NAME_REQUIRED is raised, no TournamentEntry record must be created.
    """
    from app.models.tournament import TournamentEntry

    await _register(client, username="ob_nff4", ff_uid="6620000004")
    token = await _login(client, "ob_nff4")
    tourn = await _seed_open_tournament(db_session)
    # No wallet — guard fires before any wallet interaction

    resp = await client.post(
        f"/api/tournaments/{tourn.id}/join",
        headers=_bearer(token),
    )
    assert resp.status_code == 403

    # Confirm no entry was created in the database
    user_q = await db_session.execute(
        select(TournamentEntry).where(TournamentEntry.tournament_id == tourn.id)
    )
    entries = user_q.scalars().all()
    assert len(entries) == 0, (
        f"Expected 0 entries after FF_NAME_REQUIRED block, found {len(entries)}"
    )


@pytest.mark.asyncio
async def test_join_blocked_without_ff_name_no_wallet_deduction(
    client: AsyncClient, db_session: AsyncSession
):
    """
    When FF_NAME_REQUIRED is raised, the player's wallet must not be touched.
    Uses a paid tournament with seeded wallet balance to confirm no debit.
    """
    import random, string as _s
    from app.models.staff_user import StaffUser
    from app.models.tournament import Tournament, TournamentStatus, TournamentMode, TournamentGame
    from app.models.wallet import Wallet
    from app.models.user import User
    from app.models._types import utcnow
    from app.core.security import hash_password

    # Register player WITHOUT ff_name
    await _register(client, username="ob_nff5", ff_uid="6620000005")
    token = await _login(client, "ob_nff5")

    # Give the player deposit balance via direct DB seeding
    user_row = (await db_session.execute(
        select(User).where(User.username == "ob_nff5")
    )).scalar_one()

    wallet = (await db_session.execute(
        select(Wallet).where(Wallet.user_id == user_row.id)
    )).scalar_one()

    initial_deposit = Decimal("500.00")
    wallet.deposit_balance = initial_deposit
    await db_session.commit()

    # Create a paid (₹50) tournament
    admin = (await db_session.execute(select(StaffUser).limit(1))).scalar_one()
    suffix = "".join(random.choices(_s.ascii_uppercase + _s.digits, k=5))
    paid_tourn = Tournament(
        id=uuid.uuid4(),
        tournament_code=f"T-PD{suffix}",
        title=f"Paid Join Test {suffix}",
        game=TournamentGame.free_fire,
        mode=TournamentMode.solo,
        entry_fee=Decimal("50.00"),
        max_players=50,
        min_players=1,
        player_count=0,
        next_sheet_number=1,
        status=TournamentStatus.open,
        scheduled_at=utcnow(),
        created_by_id=admin.id,
        created_by_display_name=admin.display_name,
        created_by_role=admin.role.value,
    )
    db_session.add(paid_tourn)
    await db_session.commit()

    # Attempt join — must be rejected
    resp = await client.post(
        f"/api/tournaments/{paid_tourn.id}/join",
        headers=_bearer(token),
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "FF_NAME_REQUIRED"

    # Reload wallet and verify balance unchanged
    await db_session.refresh(wallet)
    assert wallet.deposit_balance == initial_deposit, (
        f"Wallet was debited despite FF_NAME_REQUIRED guard! "
        f"Expected {initial_deposit}, got {wallet.deposit_balance}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# C. Save FF Name then join
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_set_ff_name_then_join_succeeds(
    client: AsyncClient, db_session: AsyncSession
):
    """
    After a player with no ff_name sets one via PATCH /api/users/me,
    the tournament join must succeed.
    """
    await _register(client, username="ob_sfn1", ff_uid="6630000001")
    token = await _login(client, "ob_sfn1")
    tourn = await _seed_open_tournament(db_session)
    await _seed_wallet(db_session, "ob_sfn1")

    # First attempt — must be rejected
    r1 = await client.post(f"/api/tournaments/{tourn.id}/join", headers=_bearer(token))
    assert r1.status_code == 403
    assert r1.json()["error"]["code"] == "FF_NAME_REQUIRED"

    # Set ff_name via PATCH /api/users/me
    patch_r = await client.patch(
        "/api/users/me",
        json={"ff_name": "SavedKing"},
        headers=_bearer(token),
    )
    assert patch_r.status_code == 200, f"PATCH /api/users/me failed: {patch_r.text}"

    # Second attempt — must now succeed
    r2 = await client.post(f"/api/tournaments/{tourn.id}/join", headers=_bearer(token))
    assert r2.status_code == 201, (
        f"Join should succeed after setting ff_name. Got {r2.status_code}: {r2.text}"
    )
    assert "entry_id" in r2.json()


@pytest.mark.asyncio
async def test_set_ff_name_removes_onboarding_requirement(client: AsyncClient):
    """
    After saving ff_name, GET /api/users/me must return the new name (not null).
    This confirms the onboarding flag would be cleared on next login.
    """
    await _register(client, username="ob_sfn2", ff_uid="6630000002")
    token = await _login(client, "ob_sfn2")

    # Verify null before
    me_before = (await client.get("/api/users/me", headers=_bearer(token))).json()
    assert me_before["ff_name"] is None

    # Set name
    await client.patch("/api/users/me", json={"ff_name": "OnboardDone"}, headers=_bearer(token))

    # Verify set after
    me_after = (await client.get("/api/users/me", headers=_bearer(token))).json()
    assert me_after["ff_name"] == "OnboardDone", (
        f"Expected ff_name='OnboardDone' after PATCH, got '{me_after.get('ff_name')}'"
    )


# ─────────────────────────────────────────────────────────────────────────────
# D. Login identity — username+password only
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_login_requires_username_not_ff_name(client: AsyncClient):
    """
    Attempting to log in using the player's ff_name as the username must fail.
    ff_name is NOT a login credential.
    """
    await _register(
        client, username="ob_lid1", ff_uid="6640000001", ff_name="CoolGamer"
    )
    # Try using ff_name as the login username — must be rejected
    resp = await client.post(
        "/api/auth/login",
        json={"username": "CoolGamer", "password": "TestPass@123"},
    )
    assert resp.status_code in (401, 400), (
        f"Login with ff_name as username must fail, got {resp.status_code}"
    )


@pytest.mark.asyncio
async def test_login_requires_username_not_ff_uid(client: AsyncClient):
    """
    Attempting to log in using the player's ff_uid as the username must fail.
    ff_uid is NOT a login credential.
    """
    await _register(client, username="ob_lid2", ff_uid="6640000002")
    # Try using ff_uid as the login username — must be rejected
    resp = await client.post(
        "/api/auth/login",
        json={"username": "6640000002", "password": "TestPass@123"},
    )
    assert resp.status_code in (401, 400), (
        f"Login with ff_uid as username must fail, got {resp.status_code}"
    )


@pytest.mark.asyncio
async def test_login_works_with_username_and_password_only(client: AsyncClient):
    """
    The username+password login flow must work unchanged for players with ff_name set.
    """
    await _register(
        client, username="ob_lid3", ff_uid="6640000003", ff_name="꧁LoginKing꧂"
    )
    token = await _login(client, "ob_lid3")
    assert token, "Login must work with correct username+password"

    # Confirm /me returns the right user
    me = (await client.get("/api/users/me", headers=_bearer(token))).json()
    assert me["username"] == "ob_lid3"
    assert me["ff_name"] == "꧁LoginKing꧂"


@pytest.mark.asyncio
async def test_login_works_with_username_and_password_no_ff_name(client: AsyncClient):
    """
    username+password login must work even when ff_name is not set.
    The auth layer is completely independent of ff_name.
    """
    await _register(client, username="ob_lid4", ff_uid="6640000004")
    token = await _login(client, "ob_lid4")
    assert token

    me = (await client.get("/api/users/me", headers=_bearer(token))).json()
    assert me["username"] == "ob_lid4"
    assert me["ff_name"] is None


@pytest.mark.asyncio
async def test_wrong_password_still_fails(client: AsyncClient):
    """Incorrect password must continue to be rejected regardless of ff_name status."""
    await _register(client, username="ob_lid5", ff_uid="6640000005", ff_name="SecurePlayer")
    resp = await client.post(
        "/api/auth/login",
        json={"username": "ob_lid5", "password": "WrongPassword!123"},
    )
    assert resp.status_code in (401, 400), "Wrong password must be rejected"


# ─────────────────────────────────────────────────────────────────────────────
# E. Existing ff_name validation unchanged
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_unicode_ff_name_accepted_at_registration(client: AsyncClient):
    """Unicode / stylish FF names must be accepted at registration."""
    resp = await _register(
        client,
        username="ob_val1",
        ff_uid="6650000001",
        ff_name="꧁MANISH꧂",
    )
    assert resp.get("ff_name") == "꧁MANISH꧂" or resp.get("message") == "Registration successful."


@pytest.mark.asyncio
async def test_unicode_ff_name_accepted_via_patch(client: AsyncClient):
    """Unicode FF names set via PATCH /api/users/me must be accepted and stored."""
    await _register(client, username="ob_val2", ff_uid="6650000002")
    token = await _login(client, "ob_val2")

    for name in ["꧁MANISH꧂", "★MANISH★", "ᴍᴀɴɪsʜ", "MANISH⚡", "MANISH*7610*"]:
        resp = await client.patch(
            "/api/users/me", json={"ff_name": name}, headers=_bearer(token)
        )
        assert resp.status_code == 200, f"Unicode name '{name}' rejected: {resp.text}"
        me = (await client.get("/api/users/me", headers=_bearer(token))).json()
        assert me["ff_name"] == name, f"Stored name mismatch: expected '{name}', got '{me['ff_name']}'"


@pytest.mark.asyncio
async def test_ff_name_over_15_chars_rejected(client: AsyncClient):
    """ff_name > 15 Unicode code-points must be rejected."""
    await _register(client, username="ob_val3", ff_uid="6650000003")
    token = await _login(client, "ob_val3")

    resp = await client.patch(
        "/api/users/me",
        json={"ff_name": "A" * 16},   # 16 chars — one over the limit
        headers=_bearer(token),
    )
    assert resp.status_code == 422, (
        f"16-char ff_name must be rejected with 422, got {resp.status_code}"
    )


@pytest.mark.asyncio
async def test_ff_name_exactly_15_chars_accepted(client: AsyncClient):
    """ff_name of exactly 15 Unicode code-points must be accepted."""
    await _register(client, username="ob_val4", ff_uid="6650000004")
    token = await _login(client, "ob_val4")

    name_15 = "A" * 15
    resp = await client.patch(
        "/api/users/me", json={"ff_name": name_15}, headers=_bearer(token)
    )
    assert resp.status_code == 200, f"15-char ff_name must be accepted, got {resp.status_code}"


@pytest.mark.asyncio
async def test_ff_name_with_html_angle_brackets_rejected(client: AsyncClient):
    """ff_name containing < or > must be rejected (XSS prevention)."""
    await _register(client, username="ob_val5", ff_uid="6650000005")
    token = await _login(client, "ob_val5")

    for bad_name in ["<script>", ">xss", "a<b"]:
        resp = await client.patch(
            "/api/users/me", json={"ff_name": bad_name}, headers=_bearer(token)
        )
        assert resp.status_code == 422, (
            f"HTML name '{bad_name}' must be rejected with 422, got {resp.status_code}"
        )


@pytest.mark.asyncio
async def test_empty_ff_name_rejected(client: AsyncClient):
    """Empty or whitespace-only ff_name must be rejected."""
    await _register(client, username="ob_val6", ff_uid="6650000006")
    token = await _login(client, "ob_val6")

    for bad in ["", "   "]:
        resp = await client.patch(
            "/api/users/me", json={"ff_name": bad}, headers=_bearer(token)
        )
        assert resp.status_code in (422, 400), (
            f"Empty/whitespace ff_name must be rejected, got {resp.status_code} for '{bad}'"
        )


# ─────────────────────────────────────────────────────────────────────────────
# F. FFNameRequiredError schema / exception structure
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ff_name_required_exception_properties():
    """FFNameRequiredError must have the correct code, message, and status_code."""
    from app.core.exceptions import FFNameRequiredError
    from fastapi import status

    ex = FFNameRequiredError()
    assert ex.code         == "FF_NAME_REQUIRED"
    assert ex.status_code  == status.HTTP_403_FORBIDDEN
    assert "Free Fire name" in ex.message
    assert "joining" in ex.message.lower() or "tournament" in ex.message.lower()


@pytest.mark.asyncio
async def test_ff_name_guard_fires_before_wallet_on_zero_balance(
    client: AsyncClient, db_session: AsyncSession
):
    """
    Guard must fire even when the player has no wallet balance.
    The 403 must not be confused with an insufficient-balance error.
    """
    await _register(client, username="ob_guard1", ff_uid="6660000001")
    token = await _login(client, "ob_guard1")
    tourn = await _seed_open_tournament(db_session)
    # Deliberately NO wallet seeded — guard must fire before wallet check

    resp = await client.post(
        f"/api/tournaments/{tourn.id}/join", headers=_bearer(token)
    )
    # Must get 403 FF_NAME_REQUIRED, not 402 insufficient_balance
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "FF_NAME_REQUIRED"


@pytest.mark.asyncio
async def test_unauthenticated_join_returns_401(
    client: AsyncClient, db_session: AsyncSession
):
    """Join endpoint must still require authentication — no regression."""
    tourn = await _seed_open_tournament(db_session)
    resp = await client.post(f"/api/tournaments/{tourn.id}/join")
    assert resp.status_code in (401, 403), "Unauthenticated join must be rejected"
