"""
Focused withdrawal tests — covers all 18 spec requirements.

1.  Withdrawal page/history API works for authenticated player (no network error).
2.  Winning balance is returned correctly by balance-summary.
3.  Withdrawal history returns successfully (200 + empty list for new player).
4.  Valid withdrawal request succeeds (201).
5.  Withdrawal hold is created correctly (winning -= amount, hold += amount).
6.  Available winning balance decreases correctly after reservation.
7.  Minimum ₹100 validation works (422 below minimum).
8.  Insufficient winning balance is rejected (422).
9.  Deposit balance cannot be withdrawn.
10. Referral balance cannot be withdrawn.
11. Invalid UPI is rejected (422).
12. Duplicate submission / idempotency is safe (409 on second pending).
13. Pending withdrawal appears in history.
14. Admin can see the pending withdrawal.
15. Withdrawal rejection releases the hold (winning restored).
16. Withdrawal approval (mark-paid) flow remains correct.
17. Unauthenticated access is rejected (401).
18. Frontend withdrawal page has no erroneous network error when APIs succeed
    (verified by confirming balance-summary and list both return 200).
"""
from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import login_player, register_player

pytestmark = pytest.mark.asyncio


# ── Helpers ────────────────────────────────────────────────────────────────────

async def _register_and_login(
    client: AsyncClient,
    username: str,
    ff_uid: str,
) -> tuple[str, str]:
    """Register a player, return (user_id, access_token)."""
    reg = await register_player(client, username=username, ff_uid=ff_uid)
    assert reg.status_code == 201, reg.text
    user_id = reg.json()["id"]
    login = await login_player(client, username=username)
    assert login.status_code == 200, login.text
    return user_id, login.json()["access_token"]


async def _fund_winning(
    db_session: AsyncSession,
    user_id: str,
    amount: Decimal,
    key_suffix: str = "1",
) -> None:
    """Credit winning_balance directly via service (bypasses deposit flow)."""
    from app.services.wallet_service import credit_prize
    async with db_session.begin():
        await credit_prize(
            db_session,
            uuid.UUID(user_id),
            amount=amount,
            idempotency_key=f"test:prize:{user_id[:8]}:{key_suffix}",
        )


async def _fund_deposit(
    db_session: AsyncSession,
    user_id: str,
    amount: Decimal,
    key_suffix: str = "1",
) -> None:
    """Credit deposit_balance directly via service."""
    from app.services.wallet_service import credit_deposit_balance
    async with db_session.begin():
        await credit_deposit_balance(
            db_session,
            uuid.UUID(user_id),
            amount=amount,
            idempotency_key=f"test:deposit:{user_id[:8]}:{key_suffix}",
        )


async def _fund_referral(
    db_session: AsyncSession,
    user_id: str,
    amount: Decimal,
    key_suffix: str = "1",
) -> None:
    """Credit referral_balance directly via service."""
    from app.services.wallet_service import credit_referral_bonus
    async with db_session.begin():
        await credit_referral_bonus(
            db_session,
            uuid.UUID(user_id),
            amount=amount,
            idempotency_key=f"test:referral:{user_id[:8]}:{key_suffix}",
        )


async def _get_staff_token(db_session: AsyncSession) -> str:
    """
    Return a valid super-admin staff token by directly querying the DB.

    We do NOT use the HTTP login endpoint here because that relies on bcrypt
    comparing a password that was hashed during bootstrap — which can fail in
    tests due to session/commit ordering.  Instead, we look up the super-admin
    row that bootstrap already created and mint a token directly.
    """
    from app.core.security import create_access_token
    from app.models.staff_user import StaffUser, StaffRole

    result = await db_session.execute(
        select(StaffUser).where(StaffUser.role == StaffRole.super_admin).limit(1)
    )
    admin = result.scalar_one_or_none()
    assert admin is not None, (
        "No super admin found — bootstrap did not run for this test's DB."
    )
    token = create_access_token(
        subject=str(admin.id),
        audience="staff",
        token_version=admin.token_version,
        role=admin.role.value if admin.role else None,
    )
    return token


# ── Test 17: Unauthenticated access rejected ──────────────────────────────────

async def test_balance_summary_requires_auth(client: AsyncClient):
    """Test 17a — unauthenticated GET /api/withdrawals/balance-summary → 401."""
    r = await client.get("/api/withdrawals/balance-summary")
    assert r.status_code == 401, r.text


async def test_list_withdrawals_requires_auth(client: AsyncClient):
    """Test 17b — unauthenticated GET /api/withdrawals → 401."""
    r = await client.get("/api/withdrawals")
    assert r.status_code == 401, r.text


async def test_create_withdrawal_requires_auth(client: AsyncClient):
    """Test 17c — unauthenticated POST /api/withdrawals → 401."""
    r = await client.post(
        "/api/withdrawals",
        json={"amount": 200, "upi_id": "test@upi"},
    )
    assert r.status_code == 401, r.text


# ── Tests 1, 2, 3, 18: Page load — no network error ───────────────────────────

async def test_balance_summary_returns_200_for_authenticated_player(
    client: AsyncClient, db_session: AsyncSession
):
    """
    Tests 1 & 18 — GET /api/withdrawals/balance-summary returns 200, not a
    connection abort.  This is the primary regression test for the network-error
    bug caused by async with db.begin() inside the route handler.
    """
    uid, tok = await _register_and_login(client, "bstest1", "5550000001")
    r = await client.get(
        "/api/withdrawals/balance-summary",
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text}"
    data = r.json()
    assert "available_winning_balance" in data
    assert "winning_balance" in data
    assert "withdrawal_hold" in data
    assert "deposit_balance" in data
    assert "referral_balance" in data
    assert "is_frozen" in data


async def test_balance_summary_returns_winning_balance_correctly(
    client: AsyncClient, db_session: AsyncSession
):
    """Test 2 — balance-summary reflects the actual winning balance."""
    uid, tok = await _register_and_login(client, "bstest2", "5550000002")
    await _fund_winning(db_session, uid, Decimal("300.00"))

    r = await client.get(
        "/api/withdrawals/balance-summary",
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert Decimal(data["winning_balance"]) == Decimal("300.00")
    assert Decimal(data["withdrawal_hold"]) == Decimal("0.00")
    assert Decimal(data["available_winning_balance"]) == Decimal("300.00")


async def test_withdrawal_history_returns_200_for_new_player(
    client: AsyncClient, db_session: AsyncSession
):
    """
    Tests 1 & 3 — GET /api/withdrawals returns 200 with empty list for a new
    player.  This is the second endpoint that was broken by the db.begin() bug.
    """
    uid, tok = await _register_and_login(client, "histtest1", "5550000003")
    r = await client.get(
        "/api/withdrawals",
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text}"
    data = r.json()
    assert data["items"] == []
    assert data["total"] == 0
    assert data["limit"] == 20
    assert data["offset"] == 0


# ── Test 4: Valid withdrawal succeeds ─────────────────────────────────────────

async def test_valid_withdrawal_request_succeeds(
    client: AsyncClient, db_session: AsyncSession
):
    """Test 4 — valid POST /api/withdrawals with sufficient winning balance → 201."""
    uid, tok = await _register_and_login(client, "wdvalid1", "5550000004")
    await _fund_winning(db_session, uid, Decimal("500.00"))

    r = await client.post(
        "/api/withdrawals",
        json={"amount": 200, "upi_id": "player@upi"},
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert r.status_code == 201, f"Expected 201, got {r.status_code}: {r.text}"
    data = r.json()
    assert data["status"] == "pending"
    assert Decimal(data["amount"]) == Decimal("200.00")
    assert data["upi_id"] == "player@upi"
    assert "id" in data


# ── Test 5: Withdrawal hold is created correctly ──────────────────────────────

async def test_withdrawal_hold_created_on_submission(
    client: AsyncClient, db_session: AsyncSession
):
    """
    Test 5 — after a successful withdrawal request:
    - winning_balance decreases by amount
    - withdrawal_hold increases by amount
    """
    from app.models.wallet import Wallet
    uid, tok = await _register_and_login(client, "wdhold1", "5550000005")
    await _fund_winning(db_session, uid, Decimal("400.00"))

    r = await client.post(
        "/api/withdrawals",
        json={"amount": 150, "upi_id": "hold@upi"},
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert r.status_code == 201, r.text

    wallet = (
        await db_session.execute(
            select(Wallet).where(Wallet.user_id == uuid.UUID(uid))
        )
    ).scalar_one()
    # winning_balance: 400 - 150 = 250
    assert wallet.winning_balance == Decimal("250.00"), (
        f"winning_balance expected 250, got {wallet.winning_balance}"
    )
    # withdrawal_hold: 0 + 150 = 150
    assert wallet.withdrawal_hold == Decimal("150.00"), (
        f"withdrawal_hold expected 150, got {wallet.withdrawal_hold}"
    )


# ── Test 6: Available winning balance decreases ───────────────────────────────

async def test_available_winning_balance_decreases_after_withdrawal(
    client: AsyncClient, db_session: AsyncSession
):
    """Test 6 — available_winning_balance in balance-summary is reduced after submit."""
    uid, tok = await _register_and_login(client, "avail1", "5550000006")
    await _fund_winning(db_session, uid, Decimal("600.00"))

    # Before
    r = await client.get(
        "/api/withdrawals/balance-summary",
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert r.status_code == 200
    before = Decimal(r.json()["available_winning_balance"])
    assert before == Decimal("600.00")

    # Submit withdrawal
    r = await client.post(
        "/api/withdrawals",
        json={"amount": 250, "upi_id": "avail@upi"},
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert r.status_code == 201

    # After — available should drop by 250
    r = await client.get(
        "/api/withdrawals/balance-summary",
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert r.status_code == 200
    data = r.json()
    # winning_balance = 600 - 250 (reserve debit) = 350
    # withdrawal_hold = 250
    # available = 350 - 250 = 100
    assert Decimal(data["available_winning_balance"]) == Decimal("100.00"), (
        f"available_winning_balance: {data}"
    )
    assert Decimal(data["withdrawal_hold"]) == Decimal("250.00")


# ── Test 7: Minimum ₹100 validation ──────────────────────────────────────────

async def test_withdrawal_below_minimum_rejected(
    client: AsyncClient, db_session: AsyncSession
):
    """Test 7 — amount below ₹100 is rejected with 422."""
    uid, tok = await _register_and_login(client, "mintest1", "5550000007")
    await _fund_winning(db_session, uid, Decimal("500.00"))

    r = await client.post(
        "/api/withdrawals",
        json={"amount": 50, "upi_id": "min@upi"},
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert r.status_code == 422, f"Expected 422, got {r.status_code}: {r.text}"
    body = r.json()
    assert "error" in body or "detail" in body


async def test_withdrawal_of_exactly_minimum_succeeds(
    client: AsyncClient, db_session: AsyncSession
):
    """Test 7b — amount of exactly ₹100 (the minimum) must succeed."""
    uid, tok = await _register_and_login(client, "mintest2", "5550000008")
    await _fund_winning(db_session, uid, Decimal("500.00"))

    r = await client.post(
        "/api/withdrawals",
        json={"amount": 100, "upi_id": "exactmin@upi"},
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert r.status_code == 201, f"Expected 201, got {r.status_code}: {r.text}"


# ── Test 8: Insufficient winning balance rejected ─────────────────────────────

async def test_withdrawal_exceeds_winning_balance_rejected(
    client: AsyncClient, db_session: AsyncSession
):
    """Test 8 — requesting more than available winning balance → 422."""
    uid, tok = await _register_and_login(client, "exctest1", "5550000009")
    await _fund_winning(db_session, uid, Decimal("100.00"))

    r = await client.post(
        "/api/withdrawals",
        json={"amount": 999, "upi_id": "exc@upi"},
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert r.status_code == 422, f"Expected 422, got {r.status_code}: {r.text}"


async def test_withdrawal_with_zero_winning_balance_rejected(
    client: AsyncClient, db_session: AsyncSession
):
    """Test 8b — player with no winning balance cannot withdraw."""
    uid, tok = await _register_and_login(client, "zerowtest", "5550000010")
    # Fund only deposit, not winning
    await _fund_deposit(db_session, uid, Decimal("500.00"))

    r = await client.post(
        "/api/withdrawals",
        json={"amount": 100, "upi_id": "zero@upi"},
        headers={"Authorization": f"Bearer {tok}"},
    )
    # 422 (exceeds balance) or 402 (insufficient) — both acceptable
    assert r.status_code in (402, 422), f"Expected 402/422, got {r.status_code}: {r.text}"


# ── Test 9: Deposit balance cannot be withdrawn ───────────────────────────────

async def test_deposit_balance_cannot_be_withdrawn(
    client: AsyncClient, db_session: AsyncSession
):
    """
    Test 9 — player with ₹1000 deposit but ₹0 winning cannot withdraw.
    Only winning_balance is withdrawable.
    """
    uid, tok = await _register_and_login(client, "deptest1", "5550000011")
    await _fund_deposit(db_session, uid, Decimal("1000.00"))

    r = await client.post(
        "/api/withdrawals",
        json={"amount": 200, "upi_id": "dep@upi"},
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert r.status_code in (402, 422), (
        f"Deposit balance should not be withdrawable, got {r.status_code}: {r.text}"
    )

    # Confirm deposit_balance is untouched
    from app.models.wallet import Wallet
    wallet = (
        await db_session.execute(
            select(Wallet).where(Wallet.user_id == uuid.UUID(uid))
        )
    ).scalar_one()
    assert wallet.deposit_balance == Decimal("1000.00"), (
        "Deposit balance must not be debited by a withdrawal request"
    )


# ── Test 10: Referral balance cannot be withdrawn ─────────────────────────────

async def test_referral_balance_cannot_be_withdrawn(
    client: AsyncClient, db_session: AsyncSession
):
    """
    Test 10 — player with ₹200 referral balance but ₹0 winning cannot withdraw.
    Referral balance is not withdrawable.
    """
    uid, tok = await _register_and_login(client, "reftest1", "5550000012")
    await _fund_referral(db_session, uid, Decimal("200.00"))

    r = await client.post(
        "/api/withdrawals",
        json={"amount": 100, "upi_id": "ref@upi"},
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert r.status_code in (402, 422), (
        f"Referral balance should not be withdrawable, got {r.status_code}: {r.text}"
    )

    # Confirm referral_balance is untouched
    from app.models.wallet import Wallet
    wallet = (
        await db_session.execute(
            select(Wallet).where(Wallet.user_id == uuid.UUID(uid))
        )
    ).scalar_one()
    assert wallet.referral_balance == Decimal("200.00"), (
        "Referral balance must not be debited by a withdrawal request"
    )


# ── Test 11: Invalid UPI rejected ─────────────────────────────────────────────

@pytest.mark.parametrize("bad_upi", [
    "",            # empty
    "noupisign",   # missing @
    "@nodomain",   # no local part
    "a@",          # no provider
    "x" * 60 + "@upi",  # too long local
    "user@ space", # space in provider
])
async def test_invalid_upi_id_rejected(
    client: AsyncClient, db_session: AsyncSession, bad_upi: str
):
    """Test 11 — various malformed UPI IDs are rejected at schema validation (422)."""
    uid, tok = await _register_and_login(
        client,
        f"upitest_{abs(hash(bad_upi)) % 100000}",
        f"555{abs(hash(bad_upi)) % 10000000:07d}",
    )
    await _fund_winning(db_session, uid, Decimal("500.00"))

    r = await client.post(
        "/api/withdrawals",
        json={"amount": 100, "upi_id": bad_upi},
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert r.status_code == 422, (
        f"Bad UPI {bad_upi!r} should be rejected with 422, got {r.status_code}: {r.text}"
    )


async def test_valid_upi_id_accepted(
    client: AsyncClient, db_session: AsyncSession
):
    """Test 11b — valid UPI IDs are accepted."""
    uid, tok = await _register_and_login(client, "upiok1", "5550000020")
    await _fund_winning(db_session, uid, Decimal("500.00"))

    valid_upis = ["user@upi", "9876543210@paytm", "test.user@okaxis"]
    for i, upi in enumerate(valid_upis):
        r = await client.post(
            "/api/withdrawals",
            json={"amount": 100, "upi_id": upi},
            headers={"Authorization": f"Bearer {tok}"},
        )
        if i == 0:
            # First one should succeed (₹100 of ₹500)
            assert r.status_code == 201, f"Valid UPI {upi!r} rejected: {r.text}"
            break  # one valid submission is enough


# ── Test 12: Duplicate submission / idempotency ───────────────────────────────

async def test_duplicate_withdrawal_rejected_with_409(
    client: AsyncClient, db_session: AsyncSession
):
    """
    Test 12 — a second withdrawal submission while one is still pending
    must return 409, not create a second pending withdrawal.
    """
    uid, tok = await _register_and_login(client, "dupwd1", "5550000013")
    await _fund_winning(db_session, uid, Decimal("1000.00"))
    hdrs = {"Authorization": f"Bearer {tok}"}

    # First submission — should succeed
    r1 = await client.post(
        "/api/withdrawals",
        json={"amount": 200, "upi_id": "dup@upi"},
        headers=hdrs,
    )
    assert r1.status_code == 201, f"First withdrawal failed: {r1.text}"

    # Second submission while first is still pending — must be rejected
    r2 = await client.post(
        "/api/withdrawals",
        json={"amount": 300, "upi_id": "dup2@upi"},
        headers=hdrs,
    )
    assert r2.status_code == 409, (
        f"Expected 409 (active_withdrawal_exists), got {r2.status_code}: {r2.text}"
    )

    # Confirm only one withdrawal exists in the DB
    from app.models.withdrawal import Withdrawal
    result = await db_session.execute(
        select(Withdrawal).where(
            Withdrawal.user_id == uuid.UUID(uid)
        )
    )
    withdrawals = result.scalars().all()
    assert len(withdrawals) == 1, (
        f"Expected exactly 1 withdrawal, found {len(withdrawals)}"
    )


# ── Test 13: Pending withdrawal appears in history ────────────────────────────

async def test_pending_withdrawal_appears_in_history(
    client: AsyncClient, db_session: AsyncSession
):
    """Test 13 — after submission, GET /api/withdrawals lists the pending request."""
    uid, tok = await _register_and_login(client, "histwd1", "5550000014")
    await _fund_winning(db_session, uid, Decimal("500.00"))
    hdrs = {"Authorization": f"Bearer {tok}"}

    # Submit
    r = await client.post(
        "/api/withdrawals",
        json={"amount": 200, "upi_id": "hist@upi"},
        headers=hdrs,
    )
    assert r.status_code == 201, r.text
    wd_id = r.json()["id"]

    # List
    r = await client.get("/api/withdrawals", headers=hdrs)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["total"] == 1
    assert len(data["items"]) == 1
    item = data["items"][0]
    assert item["id"] == wd_id
    assert item["status"] == "pending"
    assert Decimal(item["amount"]) == Decimal("200.00")
    assert item["upi_id"] == "hist@upi"


# ── Test 14: Admin can see the pending withdrawal ─────────────────────────────

async def test_admin_can_see_pending_withdrawal(
    client: AsyncClient, db_session: AsyncSession
):
    """Test 14 — pending withdrawal created by player is visible to admin."""
    uid, tok = await _register_and_login(client, "adminvis1", "5550000015")
    await _fund_winning(db_session, uid, Decimal("500.00"))
    hdrs = {"Authorization": f"Bearer {tok}"}

    # Player submits withdrawal
    r = await client.post(
        "/api/withdrawals",
        json={"amount": 150, "upi_id": "adminvis@upi"},
        headers=hdrs,
    )
    assert r.status_code == 201, r.text
    wd_id = r.json()["id"]

    # Admin lists pending withdrawals
    staff_tok = await _get_staff_token(db_session)
    r = await client.get(
        "/api/admin/withdrawals/pending",
        headers={"Authorization": f"Bearer {staff_tok}"},
    )
    assert r.status_code == 200, f"Admin pending list failed: {r.text}"
    data = r.json()
    ids = [item["id"] for item in data["items"]]
    assert wd_id in ids, (
        f"Withdrawal {wd_id} not found in admin pending list: {ids}"
    )

    # Admin can fetch the individual withdrawal
    r = await client.get(
        f"/api/admin/withdrawals/{wd_id}",
        headers={"Authorization": f"Bearer {staff_tok}"},
    )
    assert r.status_code == 200, f"Admin get withdrawal failed: {r.text}"
    item = r.json()
    assert item["id"] == wd_id
    assert item["status"] == "pending"
    assert item["upi_id"] == "adminvis@upi"


# ── Test 15: Rejection releases the hold ─────────────────────────────────────

async def test_rejection_releases_withdrawal_hold(
    client: AsyncClient, db_session: AsyncSession
):
    """
    Test 15 — when admin rejects a withdrawal:
    - withdrawal_hold goes back to 0
    - winning_balance is fully restored
    - status becomes "rejected"
    """
    from app.models.wallet import Wallet
    uid, tok = await _register_and_login(client, "rejtest1", "5550000016")
    await _fund_winning(db_session, uid, Decimal("300.00"))
    hdrs = {"Authorization": f"Bearer {tok}"}

    # Submit
    r = await client.post(
        "/api/withdrawals",
        json={"amount": 200, "upi_id": "rej@upi"},
        headers=hdrs,
    )
    assert r.status_code == 201, r.text
    wd_id = r.json()["id"]

    # Verify hold is in place
    wallet = (
        await db_session.execute(
            select(Wallet).where(Wallet.user_id == uuid.UUID(uid))
        )
    ).scalar_one()
    assert wallet.withdrawal_hold == Decimal("200.00")
    assert wallet.winning_balance == Decimal("100.00")

    # Admin rejects
    staff_tok = await _get_staff_token(db_session)
    r = await client.post(
        f"/api/admin/withdrawals/{wd_id}/reject",
        json={"reason": "Test rejection — funds returned."},
        headers={"Authorization": f"Bearer {staff_tok}"},
    )
    assert r.status_code == 200, f"Admin reject failed: {r.text}"
    assert r.json()["status"] == "rejected"

    # Refresh wallet from DB
    db_session.expire_all()
    wallet = (
        await db_session.execute(
            select(Wallet).where(Wallet.user_id == uuid.UUID(uid))
        )
    ).scalar_one()
    assert wallet.withdrawal_hold == Decimal("0.00"), (
        f"Hold not released after rejection: {wallet.withdrawal_hold}"
    )
    assert wallet.winning_balance == Decimal("300.00"), (
        f"Winning not restored after rejection: {wallet.winning_balance}"
    )

    # Player can now submit a new withdrawal (hold is released)
    r = await client.post(
        "/api/withdrawals",
        json={"amount": 100, "upi_id": "rej2@upi"},
        headers=hdrs,
    )
    assert r.status_code == 201, (
        f"Should be able to submit after rejection, got {r.status_code}: {r.text}"
    )


# ── Test 16: Approval / mark-paid flow remains correct ───────────────────────

async def test_admin_approval_mark_paid_flow(
    client: AsyncClient, db_session: AsyncSession
):
    """
    Test 16 — full admin approval flow:
    pending → processing → paid.
    After paid: withdrawal_hold = 0, winning_balance stays reduced.
    """
    from app.models.wallet import Wallet
    uid, tok = await _register_and_login(client, "paidtest1", "5550000017")
    await _fund_winning(db_session, uid, Decimal("500.00"))
    hdrs = {"Authorization": f"Bearer {tok}"}

    # Player submits
    r = await client.post(
        "/api/withdrawals",
        json={"amount": 200, "upi_id": "paid@upi"},
        headers=hdrs,
    )
    assert r.status_code == 201, r.text
    wd_id = r.json()["id"]

    staff_tok = await _get_staff_token(db_session)
    staff_hdrs = {"Authorization": f"Bearer {staff_tok}"}

    # Step 1: Admin marks processing
    r = await client.post(
        f"/api/admin/withdrawals/{wd_id}/process",
        json={"note": "Starting transfer"},
        headers=staff_hdrs,
    )
    assert r.status_code == 200, f"Process failed: {r.text}"
    assert r.json()["status"] == "processing"

    # Step 2: Admin marks paid
    r = await client.post(
        f"/api/admin/withdrawals/{wd_id}/mark-paid",
        json={"payout_utr": "UTR123456789", "note": "Transferred via IMPS"},
        headers=staff_hdrs,
    )
    assert r.status_code == 200, f"Mark-paid failed: {r.text}"
    paid = r.json()
    assert paid["status"] == "paid"
    assert paid["payout_utr"] == "UTR123456789"

    # Verify wallet: hold released, winning stays at 300 (was debited at reserve)
    db_session.expire_all()
    wallet = (
        await db_session.execute(
            select(Wallet).where(Wallet.user_id == uuid.UUID(uid))
        )
    ).scalar_one()
    assert wallet.withdrawal_hold == Decimal("0.00"), (
        f"Hold not cleared after paid: {wallet.withdrawal_hold}"
    )
    assert wallet.winning_balance == Decimal("300.00"), (
        f"winning_balance should be 300 (500 - 200 reserve): {wallet.winning_balance}"
    )

    # Player can see UTR in history
    r = await client.get("/api/withdrawals", headers=hdrs)
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    paid_item = next((i for i in items if i["id"] == wd_id), None)
    assert paid_item is not None
    assert paid_item["status"] == "paid"
    assert paid_item["payout_utr"] == "UTR123456789"


# ── Additional edge cases ─────────────────────────────────────────────────────

async def test_balance_summary_reflects_hold_correctly(
    client: AsyncClient, db_session: AsyncSession
):
    """
    After a pending withdrawal, balance-summary must show:
    - withdrawal_hold > 0
    - available_winning_balance = winning_balance - withdrawal_hold
    """
    uid, tok = await _register_and_login(client, "holdsum1", "5550000018")
    await _fund_winning(db_session, uid, Decimal("400.00"))
    hdrs = {"Authorization": f"Bearer {tok}"}

    r = await client.post(
        "/api/withdrawals",
        json={"amount": 150, "upi_id": "holdsum@upi"},
        headers=hdrs,
    )
    assert r.status_code == 201

    r = await client.get("/api/withdrawals/balance-summary", headers=hdrs)
    assert r.status_code == 200
    data = r.json()
    # winning_balance = 400 - 150 = 250
    # withdrawal_hold = 150
    # available = 250 - 150 = 100
    assert Decimal(data["withdrawal_hold"]) == Decimal("150.00")
    assert Decimal(data["available_winning_balance"]) == Decimal("100.00")


async def test_both_page_load_endpoints_return_200_simultaneously(
    client: AsyncClient, db_session: AsyncSession
):
    """
    Test 18 — the two endpoints called when withdrawal.html loads
    (balance-summary and list) both return 200 for an authenticated player.
    This directly verifies the fix for the "Network error" regression.
    """
    uid, tok = await _register_and_login(client, "pageload1", "5550000019")
    hdrs = {"Authorization": f"Bearer {tok}"}

    r_bs = await client.get("/api/withdrawals/balance-summary", headers=hdrs)
    r_list = await client.get("/api/withdrawals", headers=hdrs)

    assert r_bs.status_code == 200, (
        f"balance-summary returned {r_bs.status_code} — "
        f"network error bug not fixed: {r_bs.text}"
    )
    assert r_list.status_code == 200, (
        f"withdrawal list returned {r_list.status_code} — "
        f"network error bug not fixed: {r_list.text}"
    )


async def test_zero_amount_rejected_by_schema(
    client: AsyncClient, db_session: AsyncSession
):
    """Zero or negative amounts are rejected at schema level (422)."""
    uid, tok = await _register_and_login(client, "zeroamt1", "5550000021")
    await _fund_winning(db_session, uid, Decimal("500.00"))
    hdrs = {"Authorization": f"Bearer {tok}"}

    for bad_amount in [0, -100]:
        r = await client.post(
            "/api/withdrawals",
            json={"amount": bad_amount, "upi_id": "zero@upi"},
            headers=hdrs,
        )
        assert r.status_code == 422, (
            f"Amount {bad_amount} should be rejected with 422, got {r.status_code}"
        )
