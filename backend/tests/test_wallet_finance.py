"""
T6 - Wallet money movement, deposit idempotency, withdrawal reservation/release,
tournament join, unique sheet number assignment.
All tests use the SQLite test DB via the shared conftest fixtures.
No Razorpay/Supabase calls.
"""
from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import login_player, login_staff, register_player

pytestmark = pytest.mark.asyncio


# ============================================================================
# Helpers
# ============================================================================

async def _register_and_fund(
    client: AsyncClient,
    db_session: AsyncSession,
    username: str,
    ff_uid: str,
    deposit_amount: Decimal = Decimal("500.00"),
) -> tuple[str, str]:
    """Register a player, fund wallet, return (user_id, access_token)."""
    from app.services.wallet_service import credit_deposit_balance

    reg = await register_player(client, username=username, ff_uid=ff_uid)
    assert reg.status_code == 201, reg.text
    user_id = reg.json()["id"]

    async with db_session.begin():
        await credit_deposit_balance(
            db_session,
            uuid.UUID(user_id),
            amount=deposit_amount,
            idempotency_key=f"test_fund:{username}:{user_id[:8]}",
            description="Test funding",
        )

    login_resp = await login_player(client, username=username)
    assert login_resp.status_code == 200, login_resp.text
    access_token = login_resp.json()["access_token"]
    return user_id, access_token


async def _create_open_tournament(
    client: AsyncClient,
    entry_fee: int = 50,
    max_players: int = 5,
) -> str:
    """Create a tournament as admin and return its ID."""
    staff_resp = await login_staff(client)
    staff_token = staff_resp.json()["access_token"]

    create_resp = await client.post(
        "/api/admin/tournaments",
        json={
            "title": f"Test Rs{entry_fee} Tournament",
            "entry_fee": entry_fee,
            "scheduled_at": "2099-01-01T10:00:00+00:00",
            "max_players": max_players,
            "min_players": 2,
            "game": "free_fire",
            "mode": "solo",
        },
        headers={"Authorization": f"Bearer {staff_token}"},
    )
    assert create_resp.status_code == 201, create_resp.text
    tournament_id = create_resp.json()["id"]

    open_resp = await client.post(
        f"/api/admin/tournaments/{tournament_id}/open",
        headers={"Authorization": f"Bearer {staff_token}"},
    )
    assert open_resp.status_code == 200, open_resp.text
    return tournament_id


# ============================================================================
# Wallet balances
# ============================================================================

async def test_wallet_created_on_registration(
    client: AsyncClient, db_session: AsyncSession
):
    """Every new player has a wallet with zero balances."""
    from app.models.wallet import Wallet

    reg = await register_player(client, username="walletcheck", ff_uid="7770000001")
    assert reg.status_code == 201
    user_id = uuid.UUID(reg.json()["id"])

    result = await db_session.execute(
        select(Wallet).where(Wallet.user_id == user_id)
    )
    wallet = result.scalar_one_or_none()
    assert wallet is not None
    assert wallet.deposit_balance == Decimal("0.00")
    assert wallet.winning_balance == Decimal("0.00")
    assert wallet.referral_balance == Decimal("0.00")
    assert wallet.withdrawal_hold == Decimal("0.00")
    assert wallet.is_frozen is False


async def test_wallet_balance_api(
    client: AsyncClient, db_session: AsyncSession
):
    """GET /api/wallet/me returns correct balances."""
    user_id, token = await _register_and_fund(
        client, db_session, "balancetest", "7770000002", Decimal("100.00")
    )
    resp = await client.get(
        "/api/wallet/me",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert Decimal(data["deposit_balance"]) == Decimal("100.00")
    assert Decimal(data["winning_balance"]) == Decimal("0.00")


async def test_wallet_credit_creates_ledger_entry(
    client: AsyncClient, db_session: AsyncSession
):
    """Crediting deposit_balance must create a WalletTransaction record."""
    from app.models.wallet import WalletTransaction, TransactionType
    from app.services.wallet_service import credit_deposit_balance

    reg = await register_player(client, username="ledgertest", ff_uid="7770000003")
    user_id = uuid.UUID(reg.json()["id"])

    async with db_session.begin():
        await credit_deposit_balance(
            db_session, user_id,
            amount=Decimal("250.00"),
            idempotency_key="test_ledger:250",
        )

    txs = (
        await db_session.execute(
            select(WalletTransaction).where(
                WalletTransaction.user_id == user_id,
                WalletTransaction.type == TransactionType.deposit,
            )
        )
    ).scalars().all()
    assert len(txs) == 1
    assert txs[0].amount == Decimal("250.00")
    assert txs[0].balance_before == Decimal("0.00")
    assert txs[0].balance_after == Decimal("250.00")


async def test_wallet_negative_balance_impossible(
    client: AsyncClient, db_session: AsyncSession
):
    """Attempting to debit more than available balance must fail."""
    from app.core.exceptions import InsufficientBalanceError
    from app.services.wallet_service import (
        credit_deposit_balance, get_wallet_for_update, _record_transaction
    )
    from app.models.wallet import TransactionType, TransactionDirection, BalanceColumn

    reg = await register_player(client, username="negbaltest", ff_uid="7770000004")
    user_id = uuid.UUID(reg.json()["id"])

    async with db_session.begin():
        await credit_deposit_balance(
            db_session, user_id, amount=Decimal("50.00"),
            idempotency_key="test_neg:fund",
        )

    async with db_session.begin():
        wallet = await get_wallet_for_update(db_session, user_id)
        with pytest.raises(InsufficientBalanceError):
            await _record_transaction(
                db_session, wallet,
                tx_type=TransactionType.entry_fee,
                balance_col=BalanceColumn.deposit,
                direction=TransactionDirection.debit,
                amount=Decimal("9999.00"),
                idempotency_key="test_neg:overdraw",
            )


async def test_frozen_wallet_blocks_transactions(
    client: AsyncClient, db_session: AsyncSession
):
    """Frozen wallets must reject all transactions."""
    from app.core.exceptions import WalletFrozenError
    from app.services.wallet_service import (
        credit_deposit_balance, freeze_wallet,
        get_wallet_for_update, _record_transaction,
    )
    from app.models.wallet import TransactionType, TransactionDirection, BalanceColumn

    reg = await register_player(client, username="frozentest", ff_uid="7770000005")
    user_id = uuid.UUID(reg.json()["id"])

    async with db_session.begin():
        await credit_deposit_balance(
            db_session, user_id, amount=Decimal("100.00"),
            idempotency_key="test_freeze:fund",
        )
        await freeze_wallet(db_session, user_id)

    async with db_session.begin():
        wallet = await get_wallet_for_update(db_session, user_id)
        with pytest.raises(WalletFrozenError):
            await _record_transaction(
                db_session, wallet,
                tx_type=TransactionType.deposit,
                balance_col=BalanceColumn.deposit,
                direction=TransactionDirection.credit,
                amount=Decimal("1.00"),
                idempotency_key="test_freeze:credit_attempt",
            )


# ============================================================================
# Entry fee split (50% referral cap)
# ============================================================================

async def test_entry_fee_split_50pct_cap():
    """Referral balance can cover at most 50% of entry fee."""
    from app.services.wallet_service import calculate_entry_fee_split

    entry_fee = Decimal("50.00")
    deposit_balance = Decimal("500.00")
    referral_balance = Decimal("100.00")

    split = calculate_entry_fee_split(entry_fee, deposit_balance, referral_balance)
    assert split.referral_amount == Decimal("25.00")
    assert split.deposit_amount == Decimal("25.00")
    assert split.total == Decimal("50.00")


async def test_entry_fee_split_no_referral_balance():
    """When referral balance is 0, full entry fee comes from deposit."""
    from app.services.wallet_service import calculate_entry_fee_split

    split = calculate_entry_fee_split(
        Decimal("50.00"), Decimal("500.00"), Decimal("0.00")
    )
    assert split.referral_amount == Decimal("0.00")
    assert split.deposit_amount == Decimal("50.00")


async def test_entry_fee_split_insufficient_deposit():
    """If deposit balance < required deposit_amount, raise InsufficientBalanceError."""
    from app.core.exceptions import InsufficientBalanceError
    from app.services.wallet_service import calculate_entry_fee_split

    with pytest.raises(InsufficientBalanceError):
        calculate_entry_fee_split(
            Decimal("50.00"),
            Decimal("10.00"),
            Decimal("0.00"),
        )


async def test_entry_fee_referral_cannot_fully_pay():
    """Even with high referral balance, deposit must cover 50%+ of fee."""
    from app.core.exceptions import InsufficientBalanceError
    from app.services.wallet_service import calculate_entry_fee_split

    with pytest.raises(InsufficientBalanceError):
        calculate_entry_fee_split(
            Decimal("50.00"),
            Decimal("0.00"),
            Decimal("100.00"),
        )


async def test_entry_fee_split_all_tiers():
    """50% cap holds for every supported entry fee tier."""
    from app.services.wallet_service import calculate_entry_fee_split

    tiers = [Decimal(str(f)) for f in [10, 20, 30, 40, 50, 100]]
    for fee in tiers:
        split = calculate_entry_fee_split(fee, fee, fee * 2)
        assert split.referral_amount <= fee / 2, f"Referral cap violated for Rs{fee}"
        assert split.total == fee


# ============================================================================
# Deposit idempotency
# ============================================================================

async def test_deposit_credit_idempotent(client: AsyncClient, test_context):
    """The same idempotency key must never credit a wallet twice."""
    from app.core.exceptions import DuplicateTransactionError
    from app.services.wallet_service import credit_deposit_balance
    from app.models.wallet import Wallet

    reg = await register_player(client, username="idemptest", ff_uid="7770000006")
    user_id = uuid.UUID(reg.json()["id"])
    ikey = f"deposit_idemp:{user_id}"

    # First credit - must succeed
    async with test_context.factory() as sess1:
        async with sess1.begin():
            await credit_deposit_balance(
                sess1, user_id, amount=Decimal("100.00"), idempotency_key=ikey
            )

    # Second credit with same key - must raise DuplicateTransactionError
    async with test_context.factory() as sess2:
        async with sess2.begin():
            with pytest.raises(DuplicateTransactionError):
                await credit_deposit_balance(
                    sess2, user_id, amount=Decimal("100.00"), idempotency_key=ikey
                )

    # Balance must still be exactly Rs100 (not doubled)
    async with test_context.factory() as sess3:
        result = await sess3.execute(
            select(Wallet).where(Wallet.user_id == user_id)
        )
        wallet = result.scalar_one()
        assert wallet.deposit_balance == Decimal("100.00")


# ============================================================================
# Withdrawal reservation and release
# ============================================================================

async def test_withdrawal_reserve_debits_winning(
    client: AsyncClient, db_session: AsyncSession
):
    """Reserve withdrawal: winning decreases, hold increases."""
    from app.services.wallet_service import credit_prize, reserve_withdrawal
    from app.models.wallet import Wallet

    reg = await register_player(client, username="wdreserve1", ff_uid="7770000007")
    user_id = uuid.UUID(reg.json()["id"])
    wid = uuid.uuid4()

    async with db_session.begin():
        await credit_prize(
            db_session, user_id, amount=Decimal("200.00"),
            idempotency_key="test_wr:prize",
        )

    async with db_session.begin():
        await reserve_withdrawal(
            db_session, user_id, amount=Decimal("100.00"), withdrawal_id=wid
        )

    result = await db_session.execute(
        select(Wallet).where(Wallet.user_id == user_id)
    )
    wallet = result.scalar_one()
    assert wallet.winning_balance == Decimal("100.00")
    assert wallet.withdrawal_hold == Decimal("100.00")


async def test_withdrawal_reserve_insufficient_balance(
    client: AsyncClient, db_session: AsyncSession
):
    """Cannot reserve more than available winning balance."""
    from app.core.exceptions import InsufficientBalanceError
    from app.services.wallet_service import credit_prize, reserve_withdrawal

    reg = await register_player(client, username="wdreserve2", ff_uid="7770000008")
    user_id = uuid.UUID(reg.json()["id"])

    async with db_session.begin():
        await credit_prize(
            db_session, user_id, amount=Decimal("50.00"),
            idempotency_key="test_wr2:prize",
        )

    wid = uuid.uuid4()
    async with db_session.begin():
        with pytest.raises(InsufficientBalanceError):
            await reserve_withdrawal(
                db_session, user_id, amount=Decimal("999.00"), withdrawal_id=wid
            )


async def test_withdrawal_release_restores_winning(
    client: AsyncClient, db_session: AsyncSession
):
    """Releasing a withdrawal hold returns funds to winning_balance."""
    from app.services.wallet_service import (
        credit_prize, reserve_withdrawal, release_withdrawal_hold
    )
    from app.models.wallet import Wallet

    reg = await register_player(client, username="wdrelease", ff_uid="7770000009")
    user_id = uuid.UUID(reg.json()["id"])
    wid = uuid.uuid4()

    async with db_session.begin():
        await credit_prize(
            db_session, user_id, amount=Decimal("300.00"),
            idempotency_key="test_release:prize",
        )

    async with db_session.begin():
        await reserve_withdrawal(
            db_session, user_id, amount=Decimal("150.00"), withdrawal_id=wid
        )

    async with db_session.begin():
        await release_withdrawal_hold(
            db_session, user_id, amount=Decimal("150.00"), withdrawal_id=wid
        )

    result = await db_session.execute(
        select(Wallet).where(Wallet.user_id == user_id)
    )
    wallet = result.scalar_one()
    assert wallet.winning_balance == Decimal("300.00")
    assert wallet.withdrawal_hold == Decimal("0.00")


async def test_withdrawal_complete_finalises_debit(
    client: AsyncClient, db_session: AsyncSession
):
    """Completing a withdrawal permanently removes funds from withdrawal_hold."""
    from app.services.wallet_service import (
        credit_prize, reserve_withdrawal, complete_withdrawal
    )
    from app.models.wallet import Wallet

    reg = await register_player(client, username="wdcomplete", ff_uid="7770000010")
    user_id = uuid.UUID(reg.json()["id"])
    wid = uuid.uuid4()

    async with db_session.begin():
        await credit_prize(
            db_session, user_id, amount=Decimal("500.00"),
            idempotency_key="test_complete:prize",
        )

    async with db_session.begin():
        await reserve_withdrawal(
            db_session, user_id, amount=Decimal("200.00"), withdrawal_id=wid
        )

    async with db_session.begin():
        await complete_withdrawal(
            db_session, user_id, amount=Decimal("200.00"), withdrawal_id=wid
        )

    result = await db_session.execute(
        select(Wallet).where(Wallet.user_id == user_id)
    )
    wallet = result.scalar_one()
    assert wallet.winning_balance == Decimal("300.00")
    assert wallet.withdrawal_hold == Decimal("0.00")


async def test_available_winning_balance_excludes_hold(
    client: AsyncClient, test_context
):
    """available_winning_balance = winning_balance - withdrawal_hold."""
    from app.services.wallet_service import (
        credit_prize, reserve_withdrawal, get_available_winning_balance
    )

    reg = await register_player(client, username="availtest", ff_uid="7770000011")
    user_id = uuid.UUID(reg.json()["id"])
    wid = uuid.uuid4()

    async with test_context.factory() as sess1:
        async with sess1.begin():
            await credit_prize(
                sess1, user_id, amount=Decimal("400.00"),
                idempotency_key="test_avail:prize",
            )

    async with test_context.factory() as sess2:
        async with sess2.begin():
            await reserve_withdrawal(
                sess2, user_id, amount=Decimal("100.00"), withdrawal_id=wid
            )

    async with test_context.factory() as sess3:
        available = await get_available_winning_balance(sess3, user_id)
    # After prize(400) and reserve(100):
    # winning_balance = 400 - 100 (reserve debit) = 300
    # withdrawal_hold = 100
    # available = winning_balance - withdrawal_hold = 300 - 100 = 200
    assert available == Decimal("200.00")


# ============================================================================
# Tournament join - sheet number uniqueness
# ============================================================================

async def test_tournament_join_assigns_sheet_number(
    client: AsyncClient, db_session: AsyncSession
):
    """Each join must get a unique sheet number starting at 1."""
    tournament_id = await _create_open_tournament(client, entry_fee=50, max_players=10)
    uid1, tok1 = await _register_and_fund(
        client, db_session, "sheet1", "8880000001", Decimal("200.00")
    )
    uid2, tok2 = await _register_and_fund(
        client, db_session, "sheet2", "8880000002", Decimal("200.00")
    )

    j1 = await client.post(
        f"/api/tournaments/{tournament_id}/join",
        headers={"Authorization": f"Bearer {tok1}"},
    )
    j2 = await client.post(
        f"/api/tournaments/{tournament_id}/join",
        headers={"Authorization": f"Bearer {tok2}"},
    )
    assert j1.status_code in (200, 201), j1.text
    assert j2.status_code in (200, 201), j2.text

    sheet1 = j1.json()["sheet_number"]
    sheet2 = j2.json()["sheet_number"]
    assert sheet1 != sheet2
    assert {sheet1, sheet2} == {1, 2}


async def test_tournament_duplicate_join_rejected(
    client: AsyncClient, db_session: AsyncSession
):
    """Same player cannot join the same tournament twice."""
    tournament_id = await _create_open_tournament(client, entry_fee=50, max_players=10)
    user_id, tok = await _register_and_fund(
        client, db_session, "dupjoin", "8880000003", Decimal("200.00")
    )

    j1 = await client.post(
        f"/api/tournaments/{tournament_id}/join",
        headers={"Authorization": f"Bearer {tok}"},
    )
    j2 = await client.post(
        f"/api/tournaments/{tournament_id}/join",
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert j1.status_code in (200, 201)
    assert j2.status_code == 409


async def test_tournament_join_deducts_entry_fee(
    client: AsyncClient, db_session: AsyncSession
):
    """Joining must deduct exactly the entry fee from player wallet."""
    from app.models.wallet import Wallet

    tournament_id = await _create_open_tournament(client, entry_fee=50, max_players=10)
    user_id, tok = await _register_and_fund(
        client, db_session, "feecheck", "8880000004", Decimal("200.00")
    )

    await client.post(
        f"/api/tournaments/{tournament_id}/join",
        headers={"Authorization": f"Bearer {tok}"},
    )

    wallet = (
        await db_session.execute(
            select(Wallet).where(Wallet.user_id == uuid.UUID(user_id))
        )
    ).scalar_one()
    # Rs200 deposit - Rs50 entry fee = Rs150 remaining
    assert wallet.deposit_balance == Decimal("150.00")


async def test_tournament_join_insufficient_funds(
    client: AsyncClient, db_session: AsyncSession
):
    """Player with insufficient deposit balance cannot join."""
    tournament_id = await _create_open_tournament(client, entry_fee=50, max_players=10)
    user_id, tok = await _register_and_fund(
        client, db_session, "poorplayer", "8880000005", Decimal("10.00")
    )

    resp = await client.post(
        f"/api/tournaments/{tournament_id}/join",
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert resp.status_code in (400, 402, 422)


async def test_tournament_max_players_enforced(
    client: AsyncClient, db_session: AsyncSession
):
    """Tournament with max_players=3 must reject 4th join."""
    tournament_id = await _create_open_tournament(client, entry_fee=10, max_players=3)

    tokens = []
    for i in range(4):
        uid, tok = await _register_and_fund(
            client, db_session, f"maxtest{i}", f"999000{i:04d}", Decimal("100.00")
        )
        tokens.append(tok)

    responses = []
    for tok in tokens:
        resp = await client.post(
            f"/api/tournaments/{tournament_id}/join",
            headers={"Authorization": f"Bearer {tok}"},
        )
        responses.append(resp.status_code)

    # First 3 succeed (201 or 200), 4th must fail
    assert responses[0] in (200, 201)
    assert responses[1] in (200, 201)
    assert responses[2] in (200, 201)
    assert responses[3] in (400, 402, 409, 422)


async def test_tournament_cancel_refunds_all_players(
    client: AsyncClient, db_session: AsyncSession
):
    """Cancelling a tournament must atomically refund all joined players."""
    from app.models.wallet import Wallet

    tournament_id = await _create_open_tournament(client, entry_fee=50, max_players=10)

    uid1, tok1 = await _register_and_fund(
        client, db_session, "refund1", "7771000001", Decimal("200.00")
    )
    uid2, tok2 = await _register_and_fund(
        client, db_session, "refund2", "7771000002", Decimal("200.00")
    )
    await client.post(
        f"/api/tournaments/{tournament_id}/join",
        headers={"Authorization": f"Bearer {tok1}"},
    )
    await client.post(
        f"/api/tournaments/{tournament_id}/join",
        headers={"Authorization": f"Bearer {tok2}"},
    )

    staff_resp = await login_staff(client)
    staff_token = staff_resp.json()["access_token"]
    cancel_resp = await client.post(
        f"/api/admin/tournaments/{tournament_id}/cancel",
        json={"reason": "Test cancellation"},
        headers={"Authorization": f"Bearer {staff_token}"},
    )
    assert cancel_resp.status_code == 200, cancel_resp.text

    # Both players must have been refunded
    for uid in [uid1, uid2]:
        wallet = (
            await db_session.execute(
                select(Wallet).where(Wallet.user_id == uuid.UUID(uid))
            )
        ).scalar_one()
        assert wallet.deposit_balance == Decimal("200.00"), (
            f"Player {uid} was not refunded after cancellation"
        )
