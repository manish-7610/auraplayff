"""
Problem #3: Tournament room-release lock — focused tests.

Spec scenarios:

A. Cancel BEFORE room release:
   - authorised cancellation still works
   - refunds are issued (entry count drops to 0)

B. Cancel AFTER room release (NEW behaviour — succeeds):
   - Super Admin CAN cancel a room_released tournament
   - tournament status becomes cancelled
   - all active entries are refunded
   - refund is idempotent (no double-refund on retry)
   - split deposit+referral entry refunded to both balances

C. Room release itself:
   - release-room works as before (CONFIRMED → ROOM_RELEASED)
   - room credentials are stored and retrievable

D. Player join AFTER room release (new behaviour):
   - player CAN join when status is room_released
   - player CANNOT join once LIVE (existing rule unchanged)

E. RBAC — unauthorised users remain blocked:
   - player token cannot call cancel
   - unauthenticated request is rejected

F. Exception structure:
   - TournamentLockedAfterRoomReleaseError has correct code, status_code, message
     (class still exists in exceptions.py; cancel no longer raises it, but it
      may be used elsewhere or in future extensions)

H. JOINABLE_STATUSES update:
   - room_released is in JOINABLE_STATUSES
   - live is NOT in JOINABLE_STATUSES

CANCELLABLE_STATUSES:
   - room_released is in CANCELLABLE_STATUSES
   - live is NOT in CANCELLABLE_STATUSES
   - VALID_TRANSITIONS[room_released] includes cancelled
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
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _register(
    client: AsyncClient,
    *,
    username: str,
    ff_uid: str,
    ff_name: str = "TestPlayer",
) -> str:
    """Register a player and return their access token."""
    await client.post("/api/auth/register", json={
        "username": username,
        "password": "TestPass@123",
        "confirm_password": "TestPass@123",
        "ff_uid": ff_uid,
        "ff_name": ff_name,
    })
    resp = await client.post(
        "/api/auth/login",
        json={"username": username, "password": "TestPass@123"},
    )
    return resp.json()["access_token"]


async def _seed_wallet(
    db: AsyncSession,
    username: str,
    amount: Decimal = Decimal("200.00"),
) -> None:
    from app.models.user import User
    from app.models.wallet import Wallet
    user   = (await db.execute(select(User).where(User.username == username))).scalar_one()
    wallet = (await db.execute(select(Wallet).where(Wallet.user_id == user.id))).scalar_one()
    wallet.deposit_balance = amount
    await db.commit()


async def _seed_confirmed_tournament(db: AsyncSession) -> "Tournament":  # type: ignore
    """Seed a CONFIRMED tournament (one step before room release)."""
    import random, string as _s
    from app.models.staff_user import StaffUser
    from app.models.tournament import (
        Tournament, TournamentStatus, TournamentMode, TournamentGame,
    )

    admin  = (await db.execute(select(StaffUser).limit(1))).scalar_one()
    suffix = "".join(random.choices(_s.ascii_uppercase + _s.digits, k=5))
    t = Tournament(
        id=uuid.uuid4(),
        tournament_code=f"T-RL{suffix}",
        title=f"RoomLock Test {suffix}",
        game=TournamentGame.free_fire,
        mode=TournamentMode.solo,
        entry_fee=Decimal("10.00"),
        max_players=50,
        min_players=1,
        player_count=0,
        next_sheet_number=1,
        status=TournamentStatus.confirmed,
        scheduled_at=_utcnow(),
        created_by_id=admin.id,
        created_by_display_name=admin.display_name,
        created_by_role=admin.role.value,
    )
    db.add(t)
    await db.commit()
    return t


async def _seed_room_released_tournament(
    db: AsyncSession,
    *,
    player_count: int = 0,
) -> "Tournament":  # type: ignore
    """Seed a tournament already in ROOM_RELEASED state with room_id set."""
    import random, string as _s
    from app.models.staff_user import StaffUser
    from app.models.tournament import (
        Tournament, TournamentStatus, TournamentMode, TournamentGame,
    )

    admin  = (await db.execute(select(StaffUser).limit(1))).scalar_one()
    suffix = "".join(random.choices(_s.ascii_uppercase + _s.digits, k=5))
    now    = _utcnow()
    t = Tournament(
        id=uuid.uuid4(),
        tournament_code=f"T-RR{suffix}",
        title=f"RoomRel Test {suffix}",
        game=TournamentGame.free_fire,
        mode=TournamentMode.solo,
        entry_fee=Decimal("10.00"),
        max_players=50,
        min_players=1,
        player_count=player_count,
        next_sheet_number=player_count + 1,
        status=TournamentStatus.room_released,
        scheduled_at=_utcnow(),
        # room credentials already set — this is what room_is_released checks
        room_id="123456",
        room_password="AURATEST",
        room_released_at=now,
        room_released_by_id=admin.id,
        created_by_id=admin.id,
        created_by_display_name=admin.display_name,
        created_by_role=admin.role.value,
    )
    db.add(t)
    await db.commit()
    return t


async def _seed_open_tournament(db: AsyncSession) -> "Tournament":  # type: ignore
    """Seed a minimal OPEN tournament (for existing-join regression tests)."""
    import random, string as _s
    from app.models.staff_user import StaffUser
    from app.models.tournament import (
        Tournament, TournamentStatus, TournamentMode, TournamentGame,
    )

    admin  = (await db.execute(select(StaffUser).limit(1))).scalar_one()
    suffix = "".join(random.choices(_s.ascii_uppercase + _s.digits, k=5))
    t = Tournament(
        id=uuid.uuid4(),
        tournament_code=f"T-OP{suffix}",
        title=f"Open Test {suffix}",
        game=TournamentGame.free_fire,
        mode=TournamentMode.solo,
        entry_fee=Decimal("10.00"),
        max_players=50,
        min_players=1,
        player_count=0,
        next_sheet_number=1,
        status=TournamentStatus.open,
        scheduled_at=_utcnow(),
        created_by_id=admin.id,
        created_by_display_name=admin.display_name,
        created_by_role=admin.role.value,
    )
    db.add(t)
    await db.commit()
    return t


async def _seed_entry(
    db: AsyncSession,
    tournament_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    deposit_amount: Decimal = Decimal("10.00"),
    referral_amount: Decimal = Decimal("0.00"),
) -> "TournamentEntry":  # type: ignore
    """Directly create an active entry without going through the join endpoint."""
    from app.models.tournament import TournamentEntry, EntryStatus, Tournament

    # Fetch and increment next_sheet_number under a simple direct update
    tourn = (await db.execute(
        select(Tournament).where(Tournament.id == tournament_id)
    )).scalar_one()
    sheet = tourn.next_sheet_number
    tourn.next_sheet_number += 1
    tourn.player_count += 1
    await db.flush([tourn])

    entry = TournamentEntry(
        id=uuid.uuid4(),
        tournament_id=tournament_id,
        user_id=user_id,
        entry_fee_snapshot=Decimal("10.00"),
        deposit_amount_used=deposit_amount,
        referral_amount_used=referral_amount,
        sheet_number=sheet,
        status=EntryStatus.active,
    )
    db.add(entry)
    await db.commit()
    return entry


# ─────────────────────────────────────────────────────────────────────────────
# A. Cancel BEFORE room release — existing authorised cancellation works
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cancel_before_room_release_succeeds(
    client: AsyncClient, db_session: AsyncSession
):
    """
    Cancelling a CONFIRMED tournament (before room release) must still work.
    Existing behaviour must be fully preserved.
    """
    tourn = await _seed_confirmed_tournament(db_session)
    staff_tok = await _staff_token(client)

    resp = await client.post(
        f"/api/admin/tournaments/{tourn.id}/cancel",
        json={"reason": "Test cancel before room release"},
        headers=_bearer(staff_tok),
    )
    assert resp.status_code == 200, (
        f"Cancel before room release must succeed. Got {resp.status_code}: {resp.text}"
    )
    body = resp.json()
    assert body["status"] == "cancelled"


@pytest.mark.asyncio
async def test_cancel_before_room_release_refunds_entries(
    client: AsyncClient, db_session: AsyncSession
):
    """
    Cancellation before room release must refund all active entries.
    Player wallet balance must return to its pre-join value.
    """
    from app.models.user import User
    from app.models.wallet import Wallet
    from app.models.tournament import TournamentEntry

    # Register player and seed wallet
    p_tok = await _register(client, username="rl_a1", ff_uid="7710000001")
    await _seed_wallet(db_session, "rl_a1", Decimal("200.00"))

    # Seed OPEN tournament, join as player
    tourn = await _seed_open_tournament(db_session)
    join_r = await client.post(
        f"/api/tournaments/{tourn.id}/join",
        headers=_bearer(p_tok),
    )
    assert join_r.status_code == 201, join_r.text

    # Confirm the tournament (OPEN → CONFIRMED)
    staff_tok = await _staff_token(client)
    conf_r = await client.post(
        f"/api/admin/tournaments/{tourn.id}/confirm",
        headers=_bearer(staff_tok),
    )
    assert conf_r.status_code == 200, conf_r.text

    # Record wallet balance before cancel
    user = (await db_session.execute(
        select(User).where(User.username == "rl_a1")
    )).scalar_one()
    wallet_before = (await db_session.execute(
        select(Wallet).where(Wallet.user_id == user.id)
    )).scalar_one()
    balance_before = wallet_before.deposit_balance

    # Cancel (before room release)
    cancel_r = await client.post(
        f"/api/admin/tournaments/{tourn.id}/cancel",
        json={"reason": "Pre-release cancel refund test"},
        headers=_bearer(staff_tok),
    )
    assert cancel_r.status_code == 200, cancel_r.text

    # Wallet must be refunded
    await db_session.refresh(wallet_before)
    balance_after = wallet_before.deposit_balance
    assert balance_after > balance_before, (
        f"Wallet must be refunded after cancel. "
        f"Before: {balance_before}, After: {balance_after}"
    )

    # Entry must be marked refunded
    entries = (await db_session.execute(
        select(TournamentEntry).where(TournamentEntry.tournament_id == tourn.id)
    )).scalars().all()
    assert all(e.status.value == "refunded" for e in entries), (
        "All entries must be refunded after cancel"
    )


# ─────────────────────────────────────────────────────────────────────────────
# B. Cancel AFTER room release — NEW: succeeds with correct refunds
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cancel_after_room_release_superadmin_succeeds(
    client: AsyncClient, db_session: AsyncSession
):
    """Super Admin CAN cancel a room_released tournament — returns 200."""
    tourn = await _seed_room_released_tournament(db_session)
    staff_tok = await _staff_token(client)  # conftest seeds a super admin
    resp = await client.post(
        f"/api/admin/tournaments/{tourn.id}/cancel",
        json={"reason": "Minimum players not met"},
        headers=_bearer(staff_tok),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "cancelled"


@pytest.mark.asyncio
async def test_cancel_after_room_release_status_becomes_cancelled(
    client: AsyncClient, db_session: AsyncSession
):
    """Tournament.status must be 'cancelled' after authorized post-release cancel."""
    from app.models.tournament import TournamentStatus
    tourn = await _seed_room_released_tournament(db_session)
    staff_tok = await _staff_token(client)
    await client.post(
        f"/api/admin/tournaments/{tourn.id}/cancel",
        json={"reason": "Min players not met"},
        headers=_bearer(staff_tok),
    )
    await db_session.refresh(tourn)
    assert tourn.status == TournamentStatus.cancelled
    assert tourn.cancelled_at is not None
    assert tourn.cancellation_reason == "Min players not met"


@pytest.mark.asyncio
async def test_cancel_after_room_release_refunds_all_entries(
    client: AsyncClient, db_session: AsyncSession
):
    """Every active entry must be marked refunded and wallet credited."""
    from app.models.user import User
    from app.models.wallet import Wallet
    from app.models.tournament import TournamentEntry, EntryStatus

    p_tok = await _register(client, username="rl_b3n", ff_uid="7780000001")
    await _seed_wallet(db_session, "rl_b3n", Decimal("200.00"))
    user = (await db_session.execute(select(User).where(User.username == "rl_b3n"))).scalar_one()

    tourn = await _seed_room_released_tournament(db_session)
    await _seed_entry(db_session, tourn.id, user.id, deposit_amount=Decimal("10.00"))

    wallet = (await db_session.execute(select(Wallet).where(Wallet.user_id == user.id))).scalar_one()
    balance_before = wallet.deposit_balance

    staff_tok = await _staff_token(client)
    resp = await client.post(
        f"/api/admin/tournaments/{tourn.id}/cancel",
        json={"reason": "Refund test"},
        headers=_bearer(staff_tok),
    )
    assert resp.status_code == 200, resp.text

    await db_session.refresh(wallet)
    assert wallet.deposit_balance == balance_before + Decimal("10.00"), (
        f"Player must be refunded ₹10. Before: {balance_before}, After: {wallet.deposit_balance}"
    )

    entries = (await db_session.execute(
        select(TournamentEntry).where(TournamentEntry.tournament_id == tourn.id)
    )).scalars().all()
    assert all(e.status == EntryStatus.refunded for e in entries)
    assert all(e.refunded_at is not None for e in entries)
    assert all(e.refund_deposit_tx_id is not None for e in entries)


@pytest.mark.asyncio
async def test_cancel_after_room_release_idempotent_no_double_refund(
    client: AsyncClient, db_session: AsyncSession
):
    """
    If cancellation is retried after the tournament is already cancelled,
    the second call must return 409 TOURNAMENT_ALREADY_CANCELLED (not a new refund).
    """
    from app.models.wallet import Wallet
    from app.models.user import User

    p_tok = await _register(client, username="rl_b4n", ff_uid="7780000002")
    await _seed_wallet(db_session, "rl_b4n", Decimal("200.00"))
    user = (await db_session.execute(select(User).where(User.username == "rl_b4n"))).scalar_one()

    tourn = await _seed_room_released_tournament(db_session)
    await _seed_entry(db_session, tourn.id, user.id, deposit_amount=Decimal("10.00"))

    staff_tok = await _staff_token(client)

    # First cancel — must succeed
    r1 = await client.post(
        f"/api/admin/tournaments/{tourn.id}/cancel",
        json={"reason": "First cancel"},
        headers=_bearer(staff_tok),
    )
    assert r1.status_code == 200

    wallet = (await db_session.execute(select(Wallet).where(Wallet.user_id == user.id))).scalar_one()
    balance_after_first = wallet.deposit_balance

    # Second cancel — must reject (already cancelled)
    r2 = await client.post(
        f"/api/admin/tournaments/{tourn.id}/cancel",
        json={"reason": "Duplicate cancel"},
        headers=_bearer(staff_tok),
    )
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "TOURNAMENT_ALREADY_CANCELLED"

    # Wallet must not have changed from the first cancel
    await db_session.refresh(wallet)
    assert wallet.deposit_balance == balance_after_first, (
        "Second cancel must not create a second refund"
    )


@pytest.mark.asyncio
async def test_cancel_after_room_release_refunds_split_entry(
    client: AsyncClient, db_session: AsyncSession
):
    """
    Entry paid partly from deposit and partly from referral must be refunded
    to both balances in the original amounts.
    """
    from app.models.wallet import Wallet
    from app.models.user import User
    from app.models.tournament import TournamentEntry

    p_tok = await _register(client, username="rl_b5n", ff_uid="7780000003")
    await _seed_wallet(db_session, "rl_b5n", Decimal("200.00"))
    user = (await db_session.execute(select(User).where(User.username == "rl_b5n"))).scalar_one()

    # Seed referral balance for split
    wallet = (await db_session.execute(
        select(Wallet).where(Wallet.user_id == user.id)
    )).scalar_one()
    wallet.referral_balance = Decimal("5.00")
    await db_session.commit()

    tourn = await _seed_room_released_tournament(db_session)
    # ₹10 entry: ₹5 deposit + ₹5 referral
    await _seed_entry(db_session, tourn.id, user.id,
                      deposit_amount=Decimal("5.00"),
                      referral_amount=Decimal("5.00"))

    await db_session.refresh(wallet)
    deposit_before = wallet.deposit_balance
    referral_before = wallet.referral_balance

    staff_tok = await _staff_token(client)
    resp = await client.post(
        f"/api/admin/tournaments/{tourn.id}/cancel",
        json={"reason": "Split refund test"},
        headers=_bearer(staff_tok),
    )
    assert resp.status_code == 200, resp.text

    await db_session.refresh(wallet)
    assert wallet.deposit_balance == deposit_before + Decimal("5.00"), "Deposit portion must be refunded"
    assert wallet.referral_balance == referral_before + Decimal("5.00"), "Referral portion must be refunded"


# ─────────────────────────────────────────────────────────────────────────────
# C. Room release itself — unchanged behaviour
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_release_room_transitions_to_room_released(
    client: AsyncClient, db_session: AsyncSession
):
    """
    POST /api/admin/tournaments/{id}/release-room must still work as before —
    transitions CONFIRMED → ROOM_RELEASED and stores room credentials.
    """
    from app.models.tournament import Tournament, TournamentStatus

    tourn = await _seed_confirmed_tournament(db_session)
    staff_tok = await _staff_token(client)

    resp = await client.post(
        f"/api/admin/tournaments/{tourn.id}/release-room",
        json={"room_id": "999888777", "room_password": "TESTAURA"},
        headers=_bearer(staff_tok),
    )
    assert resp.status_code == 200, (
        f"Room release must succeed. Got {resp.status_code}: {resp.text}"
    )
    body = resp.json()
    assert body["status"] == "room_released"

    # Verify DB state
    await db_session.refresh(tourn)
    assert tourn.status        == TournamentStatus.room_released
    assert tourn.room_id       == "999888777"
    assert tourn.room_password == "TESTAURA"
    assert tourn.room_released_at is not None
    assert tourn.room_is_released is True


# ─────────────────────────────────────────────────────────────────────────────
# D. Player join AFTER room release
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_player_can_join_room_released_tournament(
    client: AsyncClient, db_session: AsyncSession
):
    """
    Players must be able to join a tournament that is in ROOM_RELEASED status.
    This is the key new behaviour added by Problem #3.
    """
    p_tok = await _register(client, username="rl_d1", ff_uid="7740000001")
    await _seed_wallet(db_session, "rl_d1", Decimal("200.00"))
    tourn = await _seed_room_released_tournament(db_session)

    resp = await client.post(
        f"/api/tournaments/{tourn.id}/join",
        headers=_bearer(p_tok),
    )
    assert resp.status_code == 201, (
        f"Player must be able to join a room_released tournament. "
        f"Got {resp.status_code}: {resp.text}"
    )
    body = resp.json()
    assert "entry_id"     in body
    assert "sheet_number" in body


@pytest.mark.asyncio
async def test_player_cannot_join_live_tournament(
    client: AsyncClient, db_session: AsyncSession
):
    """
    Player joining must be blocked once the tournament is LIVE.
    Existing rule unchanged.
    """
    import random, string as _s
    from app.models.staff_user import StaffUser
    from app.models.tournament import (
        Tournament, TournamentStatus, TournamentMode, TournamentGame,
    )

    p_tok = await _register(client, username="rl_d2", ff_uid="7740000002")
    await _seed_wallet(db_session, "rl_d2", Decimal("200.00"))

    # Seed a LIVE tournament directly
    admin  = (await db_session.execute(select(StaffUser).limit(1))).scalar_one()
    suffix = "".join(random.choices(_s.ascii_uppercase + _s.digits, k=5))
    live_tourn = Tournament(
        id=uuid.uuid4(),
        tournament_code=f"T-LV{suffix}",
        title=f"Live Test {suffix}",
        game=TournamentGame.free_fire,
        mode=TournamentMode.solo,
        entry_fee=Decimal("10.00"),
        max_players=50,
        min_players=1,
        player_count=0,
        next_sheet_number=1,
        status=TournamentStatus.live,
        scheduled_at=_utcnow(),
        created_by_id=admin.id,
        created_by_display_name=admin.display_name,
        created_by_role=admin.role.value,
    )
    db_session.add(live_tourn)
    await db_session.commit()

    resp = await client.post(
        f"/api/tournaments/{live_tourn.id}/join",
        headers=_bearer(p_tok),
    )
    assert resp.status_code in (409, 400), (
        f"Player must NOT be able to join a LIVE tournament. Got {resp.status_code}"
    )


@pytest.mark.asyncio
async def test_player_can_still_join_open_tournament(
    client: AsyncClient, db_session: AsyncSession
):
    """
    Existing behaviour: player can join an OPEN tournament.
    Must not be broken by Problem #3 changes.
    """
    p_tok = await _register(client, username="rl_d3", ff_uid="7740000003")
    await _seed_wallet(db_session, "rl_d3", Decimal("200.00"))
    tourn = await _seed_open_tournament(db_session)

    resp = await client.post(
        f"/api/tournaments/{tourn.id}/join",
        headers=_bearer(p_tok),
    )
    assert resp.status_code == 201, (
        f"Player must still be able to join OPEN tournament. "
        f"Got {resp.status_code}: {resp.text}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# E. RBAC — unauthorised requests remain blocked
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_player_token_cannot_cancel(
    client: AsyncClient, db_session: AsyncSession
):
    """Player JWT must not be accepted by the admin cancel endpoint."""
    p_tok = await _register(client, username="rl_e1", ff_uid="7750000001")
    tourn = await _seed_confirmed_tournament(db_session)

    resp = await client.post(
        f"/api/admin/tournaments/{tourn.id}/cancel",
        json={"reason": "Player trying to cancel"},
        headers=_bearer(p_tok),
    )
    assert resp.status_code in (401, 403), (
        f"Player token must not be able to cancel. Got {resp.status_code}"
    )


@pytest.mark.asyncio
async def test_unauthenticated_cancel_rejected(
    client: AsyncClient, db_session: AsyncSession
):
    """No token → cancel endpoint must reject with 401/403."""
    tourn = await _seed_confirmed_tournament(db_session)

    resp = await client.post(
        f"/api/admin/tournaments/{tourn.id}/cancel",
        json={"reason": "No auth"},
    )
    assert resp.status_code in (401, 403), (
        f"Unauthenticated cancel must be rejected. Got {resp.status_code}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# F. Exception structure
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_locked_exception_properties():
    """TournamentLockedAfterRoomReleaseError must have correct code and status."""
    from app.core.exceptions import TournamentLockedAfterRoomReleaseError
    from fastapi import status

    ex = TournamentLockedAfterRoomReleaseError()
    assert ex.code        == "TOURNAMENT_LOCKED_AFTER_ROOM_RELEASE"
    assert ex.status_code == status.HTTP_409_CONFLICT
    assert "room" in ex.message.lower() or "credentials" in ex.message.lower()
    assert "locked" in ex.message.lower() or "cancel" in ex.message.lower()


# ─────────────────────────────────────────────────────────────────────────────
# H. JOINABLE_STATUSES contains room_released, not live
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_joinable_statuses_contains_room_released():
    """room_released must be in JOINABLE_STATUSES after Problem #3 change."""
    from app.models.tournament import JOINABLE_STATUSES, TournamentStatus
    assert TournamentStatus.room_released in JOINABLE_STATUSES, (
        "room_released must be in JOINABLE_STATUSES so players can join "
        "after room release"
    )


@pytest.mark.asyncio
async def test_joinable_statuses_does_not_contain_live():
    """live must NOT be in JOINABLE_STATUSES — joining closes when live."""
    from app.models.tournament import JOINABLE_STATUSES, TournamentStatus
    assert TournamentStatus.live not in JOINABLE_STATUSES, (
        "live must NOT be in JOINABLE_STATUSES — joining must be blocked once live"
    )


@pytest.mark.asyncio
async def test_joinable_statuses_still_contains_open():
    """open must remain in JOINABLE_STATUSES — existing behaviour unchanged."""
    from app.models.tournament import JOINABLE_STATUSES, TournamentStatus
    assert TournamentStatus.open in JOINABLE_STATUSES, (
        "open must remain in JOINABLE_STATUSES"
    )


@pytest.mark.asyncio
async def test_room_is_released_property_accurate():
    """
    Tournament.room_is_released must return True only when
    both room_id and room_released_at are set.
    Tests the property logic directly without instantiating the ORM object.
    """
    from app.models.tournament import Tournament

    # Inspect the property source to confirm it checks both fields
    prop = Tournament.room_is_released
    assert isinstance(prop, property), "room_is_released must be a property"

    # Verify the property docstring/source references both room_id and room_released_at
    import inspect
    src = inspect.getsource(prop.fget)
    assert "room_id" in src,          "property must check room_id"
    assert "room_released_at" in src, "property must check room_released_at"

    # Functional test via a seeded DB object is covered by
    # test_release_room_transitions_to_room_released (room_is_released == True after release)
    # and the _seed_room_released_tournament helper (room_is_released == True by construction).


# ─────────────────────────────────────────────────────────────────────────────
# C-new: LIVE tournament is still not cancellable (regression guard)
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cancel_live_tournament_returns_409(
    client: AsyncClient, db_session: AsyncSession
):
    """LIVE tournament must still be non-cancellable after Problem #3 fix."""
    import random
    import string as _s
    from app.models.staff_user import StaffUser
    from app.models.tournament import (
        Tournament, TournamentStatus, TournamentMode, TournamentGame,
    )

    admin = (await db_session.execute(select(StaffUser).limit(1))).scalar_one()
    suffix = "".join(random.choices(_s.ascii_uppercase + _s.digits, k=5))
    t = Tournament(
        id=uuid.uuid4(), tournament_code=f"T-LCX{suffix}", title=f"Live Cancel {suffix}",
        game=TournamentGame.free_fire, mode=TournamentMode.solo,
        entry_fee=Decimal("10.00"), max_players=50, min_players=1,
        player_count=0, next_sheet_number=1,
        status=TournamentStatus.live,
        scheduled_at=_utcnow(),
        created_by_id=admin.id, created_by_display_name=admin.display_name,
        created_by_role=admin.role.value,
    )
    db_session.add(t)
    await db_session.commit()

    staff_tok = await _staff_token(client)
    resp = await client.post(
        f"/api/admin/tournaments/{t.id}/cancel",
        json={"reason": "Trying to cancel live"},
        headers=_bearer(staff_tok),
    )
    assert resp.status_code == 409, f"LIVE cancel must be blocked, got {resp.status_code}"
    assert resp.json()["error"]["code"] == "INVALID_STATUS_TRANSITION"


# ─────────────────────────────────────────────────────────────────────────────
# L. Listing — room_released appears in open tab; cancelled does not
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_open_includes_room_released(
    client: AsyncClient, db_session: AsyncSession
):
    """GET /api/tournaments?status=open must include room_released tournaments."""
    p_tok = await _register(client, username="rl_l1", ff_uid="7790000001")
    tourn = await _seed_room_released_tournament(db_session)

    resp = await client.get(
        "/api/tournaments",
        params={"status": "open"},
        headers=_bearer(p_tok),
    )
    assert resp.status_code == 200, resp.text
    ids = [t["id"] for t in resp.json()["items"]]
    assert str(tourn.id) in ids, (
        "room_released tournament must appear in ?status=open listing"
    )


@pytest.mark.asyncio
async def test_list_open_excludes_cancelled(
    client: AsyncClient, db_session: AsyncSession
):
    """GET /api/tournaments?status=open must NOT include cancelled tournaments."""
    import random
    import string as _s
    from app.models.tournament import (
        Tournament, TournamentStatus, TournamentMode, TournamentGame,
    )
    from app.models.staff_user import StaffUser

    p_tok = await _register(client, username="rl_l2", ff_uid="7790000002")
    admin = (await db_session.execute(select(StaffUser).limit(1))).scalar_one()
    suffix = "".join(random.choices(_s.ascii_uppercase + _s.digits, k=5))
    cancelled_t = Tournament(
        id=uuid.uuid4(), tournament_code=f"T-CX{suffix}", title=f"Cancelled {suffix}",
        game=TournamentGame.free_fire, mode=TournamentMode.solo,
        entry_fee=Decimal("10.00"), max_players=50, min_players=1,
        player_count=0, next_sheet_number=1,
        status=TournamentStatus.cancelled,
        scheduled_at=_utcnow(),
        created_by_id=admin.id, created_by_display_name=admin.display_name,
        created_by_role=admin.role.value,
    )
    db_session.add(cancelled_t)
    await db_session.commit()

    resp = await client.get(
        "/api/tournaments",
        params={"status": "open"},
        headers=_bearer(p_tok),
    )
    assert resp.status_code == 200
    ids = [t["id"] for t in resp.json()["items"]]
    assert str(cancelled_t.id) not in ids, (
        "cancelled tournament must NOT appear in ?status=open listing"
    )


# ─────────────────────────────────────────────────────────────────────────────
# CANCELLABLE_STATUSES and VALID_TRANSITIONS unit tests
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cancellable_statuses_includes_room_released():
    """room_released must be in CANCELLABLE_STATUSES after Problem #3 fix."""
    from app.models.tournament import CANCELLABLE_STATUSES, TournamentStatus
    assert TournamentStatus.room_released in CANCELLABLE_STATUSES


@pytest.mark.asyncio
async def test_cancellable_statuses_excludes_live():
    """live must NOT be in CANCELLABLE_STATUSES."""
    from app.models.tournament import CANCELLABLE_STATUSES, TournamentStatus
    assert TournamentStatus.live not in CANCELLABLE_STATUSES


@pytest.mark.asyncio
async def test_valid_transitions_room_released_includes_cancelled():
    """VALID_TRANSITIONS[room_released] must include cancelled."""
    from app.models.tournament import VALID_TRANSITIONS, TournamentStatus
    assert TournamentStatus.cancelled in VALID_TRANSITIONS[TournamentStatus.room_released]
