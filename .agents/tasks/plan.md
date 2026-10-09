# Implementation Plan — AuraPlayFF Problem #3

**Workspace root:** `c:\Users\macrosoft\Desktop\auraplayff`  
**Test command:** `cd backend && python -m pytest` (381 tests baseline)  
**Branch:** work on the existing branch; do not push  

---

## Ground truth from audit

| Finding | Impact on plan |
|---------|---------------|
| `CANCELLABLE_STATUSES = frozenset({open, confirmed})` — `room_released` absent | Must add `room_released` here |
| `VALID_TRANSITIONS[room_released] = {live}` — `cancelled` absent | Must add `cancelled` here |
| `cancel_tournament` has a hard guard `if tournament.room_is_released: raise TournamentLockedAfterRoomReleaseError()` | Must remove this guard |
| `TournamentEntry` already has `deposit_amount_used`, `referral_amount_used`, `refund_deposit_tx_id`, `refund_referral_tx_id` | No schema change needed |
| `wallet_service.refund_entry_fee` already uses those columns with correct idempotency keys `refund_deposit:<entry_id>` / `refund_referral:<entry_id>` | No new refund logic needed — existing path reused |
| `cancel_tournament` already wraps status change + all refunds in a single DB transaction | No new transaction wrapping needed |
| Backend `list_tournaments` maps `?status=open` → `[TS.open]` only | Must add `TS.room_released` to this case |
| Frontend `isJoinable` already handles `room_released` | No change needed |
| Frontend "Open" tab chip sends `?status=open` → fixed by backend fix | No separate frontend API call change needed |
| Admin JS `_renderCancelBtn` shows cancel button only for `['open', 'confirmed']` | Must add `'room_released'` |
| Admin JS `_renderActionBtn` shows "tournament locked" text for `room_released` | Must update copy and add cancel button context |
| Success modal in `tournament-details.html` has no room-credential timing message | Must add credential timing notice |
| `formatDateTime(iso)` in `ui.js` already formats IST via `timeZone: 'Asia/Kolkata'` | Use it directly |
| `test_room_release_lock.py` B-series tests assert 409 for post-release cancel | Must rewrite those tests; A/C/D/E/F/G/H tests remain valid |
| `TournamentLockedAfterRoomReleaseError` exists in `exceptions.py` | Keep the class — it's still used for LIVE+ blocks; the guard is just moved |

---

## Item 1 — Backend model: allow `room_released → cancelled` transition

**What:** In `backend/app/models/tournament.py`, make two changes:

1. Add `TournamentStatus.room_released` to `CANCELLABLE_STATUSES`.
2. Add `TournamentStatus.cancelled` to `VALID_TRANSITIONS[TournamentStatus.room_released]`.

Also update the module-level docstring comment that says `CANCELLED — cancelled from OPEN or CONFIRMED` to say `OPEN, CONFIRMED, or ROOM_RELEASED`.

**Files:** `backend/app/models/tournament.py`

**Exact diff:**

```python
# BEFORE
CANCELLABLE_STATUSES: frozenset[TournamentStatus] = frozenset({
    TournamentStatus.open,
    TournamentStatus.confirmed,
})

VALID_TRANSITIONS: dict[TournamentStatus, set[TournamentStatus]] = {
    ...
    TournamentStatus.room_released: {TournamentStatus.live},
    ...
}
```

```python
# AFTER
CANCELLABLE_STATUSES: frozenset[TournamentStatus] = frozenset({
    TournamentStatus.open,
    TournamentStatus.confirmed,
    TournamentStatus.room_released,   # added: cancellable pre-LIVE after room release
})

VALID_TRANSITIONS: dict[TournamentStatus, set[TournamentStatus]] = {
    ...
    TournamentStatus.room_released: {TournamentStatus.live, TournamentStatus.cancelled},
    ...
}
```

**Verify:** `cd backend && python -m pytest tests/test_room_release_lock.py::test_joinable_statuses_contains_room_released tests/test_room_release_lock.py::test_joinable_statuses_does_not_contain_live tests/test_room_release_lock.py::test_joinable_statuses_still_contains_open -v` — all three pass (they test `JOINABLE_STATUSES` which is unchanged, confirming model import is clean after the edit).

---

## Item 2 — Backend service: remove room-release lock guard, permit cancellation from `room_released`

**What:** In `backend/app/services/tournament_service.py`, inside `cancel_tournament`:

Remove the entire "Room-release lock guard" block (lines ~305–310):

```python
# REMOVE THIS ENTIRE BLOCK:
if tournament.room_is_released:
    raise TournamentLockedAfterRoomReleaseError()
```

The remaining guards (`if tournament.status == TournamentStatus.cancelled`, `if tournament.status == TournamentStatus.completed`, `if tournament.status not in CANCELLABLE_STATUSES`) already cover all invalid states. After Item 1, `room_released` is in `CANCELLABLE_STATUSES`, so those guards naturally pass through. `live`, `result_entry`, `completed`, and `cancelled` are not in `CANCELLABLE_STATUSES` and will still raise `TournamentInvalidTransitionError` / `TournamentAlreadyCompletedError` / `TournamentAlreadyCancelledError`.

Also remove `TournamentLockedAfterRoomReleaseError` from the import list at the top of `tournament_service.py` only if it is no longer used anywhere else in the file. (Check — it is only used in that one guard block, so remove it from the import.)

**Files:** `backend/app/services/tournament_service.py`

**Verify:** `cd backend && python -m pytest tests/test_room_release_lock.py::test_cancel_before_room_release_succeeds tests/test_room_release_lock.py::test_cancel_before_room_release_refunds_entries -v` — both must still pass (pre-release cancellation unchanged).

---

## Item 3 — Backend router: fix player-facing listing to include `room_released` in Open filter

**What:** In `backend/app/routers/tournaments.py`, inside `list_tournaments`, change the `else` branch that handles the default `?status=open` filter:

```python
# BEFORE
else:
    filter_statuses = [TS.open]

# AFTER
else:
    # "open" tab includes room_released: both are joinable pre-LIVE
    filter_statuses = [TS.open, TS.room_released]
```

No other change in this file. The `status == "all"` and explicit status branches are unchanged.

**Files:** `backend/app/routers/tournaments.py`

**Verify:** `cd backend && python -m pytest tests/ -k "list" -v` — all existing listing tests pass (no new test yet; the new test is in Item 7).

---

## Item 4 — Backend: update `cancel_tournament` admin router docstring to reflect new allowed statuses

**What:** In `backend/app/routers/admin_tournaments.py`, update the `cancel_tournament` endpoint docstring and the route summary to say `OPEN|CONFIRMED|ROOM_RELEASED → CANCELLED` instead of `OPEN|CONFIRMED → CANCELLED`. This is a documentation-only change that keeps the code honest.

**Files:** `backend/app/routers/admin_tournaments.py`

**Exact changes:**

```python
# BEFORE in @router.post summary:
summary="Cancel tournament and atomically refund all entries (OPEN|CONFIRMED → CANCELLED)",

# AFTER:
summary="Cancel tournament and atomically refund all entries (OPEN|CONFIRMED|ROOM_RELEASED → CANCELLED)",
```

```python
# BEFORE in docstring:
"""
Cancels the tournament and atomically refunds every active entry.
...
"""

# AFTER — add one line to the docstring:
"""
Cancels the tournament and atomically refunds every active entry.
Permitted from OPEN, CONFIRMED, or ROOM_RELEASED status (pre-LIVE only).
Each player's deposit_amount_used → deposit_balance.
Each player's referral_amount_used → referral_balance.
All refunds use deterministic idempotency keys; safe to retry.
"""
```

**Verify:** `cd backend && python -c "from app.routers.admin_tournaments import router; print('import ok')"` — prints `import ok`.

---

## Item 5 — Frontend admin: show Cancel button for `room_released` status with warning dialog

**What:** In `frontend/assets/js/admin/admin-tournaments.js`, two functions need updating:

### 5A. `_renderCancelBtn` function

```javascript
// BEFORE
const cancellable = ['open', 'confirmed'].includes(t.status);
btn.style.display = cancellable ? 'inline-flex' : 'none';
btn.onclick = () => {
  const reason = prompt('Cancellation reason (required):');
  if (!reason || !reason.trim()) return;
  if (!confirmAction('Cancel tournament and refund all players?')) return;
  adminPost(...)
    ...
};
```

```javascript
// AFTER
const cancellable = ['open', 'confirmed', 'room_released'].includes(t.status);
btn.style.display = cancellable ? 'inline-flex' : 'none';
btn.onclick = () => {
  const reason = prompt('Cancellation reason (required):');
  if (!reason || !reason.trim()) return;

  // Extra confirmation for post-release cancellation
  const confirmMsg = t.status === 'room_released'
    ? 'WARNING: Room credentials have already been released to players.\n\n' +
      'Are you sure you want to cancel this tournament?\n\n' +
      'All joined players will receive a full entry-fee refund. ' +
      'This action cannot be undone.'
    : 'Cancel tournament and refund all players?';

  if (!confirmAction(confirmMsg)) return;
  adminPost(`/api/admin/tournaments/${t.id}/cancel`, { reason: reason.trim() })
    .then(() => {
      showToast('Tournament cancelled and refunds issued', 'success');
      loadTournamentDetail(t.id);
    })
    .catch(err => showToast(err.displayMessage || 'Cancel failed', 'error'));
};
```

### 5B. `_renderActionBtn` function — `room_released` branch

In the `else if (t.status === 'room_released')` branch, replace the paragraph that says "cancellation is no longer allowed":

```javascript
// BEFORE (in the room_released branch paragraph):
'The tournament is now <strong>locked</strong> — cancellation is no longer allowed.'

// AFTER:
'Room credentials were released to players' + escHtml(releasedAt) + '. ' +
'Players may continue joining until the tournament goes live. ' +
'You may cancel this tournament using the Cancel button below if it cannot proceed ' +
'(e.g. minimum players not met) — all joined players will receive a full refund.'
```

**Files:** `frontend/assets/js/admin/admin-tournaments.js`

**Verify:** Load the admin tournament detail page for a `room_released` tournament in a browser. The Cancel button must be visible. Clicking it must show the warning text "WARNING: Room credentials have already been released" before confirming. For `open`/`confirmed` tournaments, the Cancel button must show the simpler existing confirmation message. For `live`, `result_entry`, `completed`, and `cancelled` tournaments, the Cancel button must remain hidden. (Manual browser test — no automated frontend test runner is set up for this project.)

---

## Item 6 — Frontend player: show room credential timing after successful join

**What:** In `frontend/tournament-details.html`, inside the `confirm-join-btn` click handler, after the success modal is populated and shown, add a "Room credentials timing" row to the `#success-breakdown` div.

The scheduled start time is available on the `tournament` object as `tournament.scheduled_at`. The credential release time is 10 minutes before start. Use the existing `formatDateTime` utility (already imported).

### 6A. Add credential timing to the success modal breakdown

In the `confirm-join-btn` click handler, find the `success-breakdown` innerHTML assignment and append one more row:

```javascript
// AFTER the existing join-confirm-row entries, add:
<div class="join-confirm-row" style="border-top:1px solid var(--color-border);margin-top:var(--space-3);padding-top:var(--space-3)">
  <span class="info-label" style="flex:1;min-width:0">🔐 Room Credentials Available</span>
  <span class="join-amount" style="font-size:var(--text-xs);text-align:right">
    ${formatDateTime(credentialReleaseTime)}
    <div style="font-size:10px;color:var(--color-text-muted);font-weight:400">10 min before ${formatDateTime(tournament.scheduled_at)}</div>
  </span>
</div>
```

Where `credentialReleaseTime` is computed as:

```javascript
const credentialReleaseTime = new Date(new Date(tournament.scheduled_at).getTime() - 10 * 60 * 1000).toISOString();
```

This uses `formatDateTime` which already outputs IST via `timeZone: 'Asia/Kolkata'`.

### 6B. Add credential timing to the entry panel for already-joined players (pre-release)

In the `showEntryPanel()` function, after the existing entry details rows, add a conditional block that shows the timing message when the room has not yet been released:

```javascript
// After the existing detail rows in showEntryPanel():
const isPreRelease = tournament.status === 'open' || tournament.status === 'confirmed' ||
  (tournament.status === 'room_released' && !tournament.room_id); // room_id absent = not yet set
// Actually: room_released means credentials ARE out; show them via the room section.
// So show timing message only when status is open or confirmed (not yet released).
const showTimingNotice = tournament.status === 'open' || tournament.status === 'confirmed';
if (showTimingNotice) {
  const releaseTime = new Date(new Date(tournament.scheduled_at).getTime() - 10 * 60 * 1000).toISOString();
  document.getElementById('entry-details').innerHTML += `
    <div class="info-row" style="margin-top:var(--space-3);padding-top:var(--space-3);border-top:1px solid var(--color-border)">
      <span class="info-label">🔐 Room Credentials</span>
      <span class="info-value" style="font-size:var(--text-xs);text-align:right">
        Available ${formatDateTime(releaseTime)}
        <div style="font-size:10px;color:var(--color-text-muted);font-weight:400">10 min before start</div>
      </span>
    </div>`;
}
```

### 6C. Show cancellation + refund notice in entry panel when tournament is cancelled

In the `showEntryPanel()` function, add a cancellation-refund notice when `myEntry.status === 'refunded'`:

```javascript
if (myEntry.status === 'refunded') {
  document.getElementById('entry-panel').innerHTML += `
    <div class="alert alert-warning" style="margin-top:var(--space-3)">
      <span class="alert-icon">ℹ</span>
      <div>
        <div style="font-weight:600;margin-bottom:2px">Tournament Cancelled — Refunded</div>
        <div style="font-size:var(--text-xs)">Your entry fee has been refunded to your wallet.</div>
      </div>
    </div>`;
}
```

**Files:** `frontend/tournament-details.html`

**Verify:** Manually join a tournament in the browser. The success modal must show the "Room ID and Password will be available at [time] IST (10 minutes before [start time])" row. The joined entry panel for an `open`/`confirmed` tournament must show the timing notice. For a `room_released` tournament, the entry panel shows the actual credentials (existing behaviour). Manual browser test.

---

## Item 7 — Tests: rewrite B-series tests and add new scenarios

**What:** Rewrite `test_room_release_lock.py` to match the new behaviour, and add all required new test scenarios.

### Tests to DELETE (old behaviour — will fail after fix):

- `test_cancel_after_room_release_returns_409` — currently asserts 409; must become a 200 success test
- `test_cancel_after_room_release_status_unchanged` — currently asserts `room_released` after cancel; must become cancelled
- `test_cancel_after_room_release_no_wallet_deduction` — currently asserts wallet unchanged after cancel; must assert wallet is refunded
- `test_cancel_after_room_release_message_is_clear` — tests the error message of the lock error; no longer applicable
- `test_release_room_cannot_be_reversed_via_cancel` — currently asserts two consecutive cancel attempts both return 409; must be rewritten
- `test_locked_exception_properties` — tests `TournamentLockedAfterRoomReleaseError` behaviour; this exception class still exists but its scenario is no longer reachable via cancel. Keep as a unit test for the exception class itself (it still exists in `exceptions.py`), but remove the lock-related assertions about it being raised by cancel.

### Tests to ADD (new B-series and additional scenarios):

Replace the deleted tests with the following new test functions in `test_room_release_lock.py`:

#### B1-new: Authorized Super Admin can cancel a room_released tournament (200 success)

```python
async def test_cancel_after_room_release_superadmin_succeeds(client, db_session):
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
```

#### B2-new: Tournament becomes cancelled in DB after post-release cancel

```python
async def test_cancel_after_room_release_status_becomes_cancelled(client, db_session):
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
```

#### B3-new: All active entries are refunded after post-release cancel

```python
async def test_cancel_after_room_release_refunds_all_entries(client, db_session):
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
```

#### B4-new: Refund is idempotent — retrying cancellation does not double-refund

```python
async def test_cancel_after_room_release_idempotent_no_double_refund(client, db_session):
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
```

#### B5-new: Cancel after room release with mixed deposit+referral split refunds both correctly

```python
async def test_cancel_after_room_release_refunds_split_entry(client, db_session):
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
```

#### C-new: LIVE tournament is still not cancellable (regression guard)

```python
async def test_cancel_live_tournament_returns_409(client, db_session):
    """LIVE tournament must still be non-cancellable after Problem #3 fix."""
    import random, string as _s
    from app.models.staff_user import StaffUser
    from app.models.tournament import Tournament, TournamentStatus, TournamentMode, TournamentGame

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
```

#### L1-new: Backend listing API returns room_released tournaments for ?status=open

```python
async def test_list_open_includes_room_released(client, db_session):
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


async def test_list_open_excludes_cancelled(client, db_session):
    """GET /api/tournaments?status=open must NOT include cancelled tournaments."""
    from app.models.tournament import Tournament, TournamentStatus, TournamentMode, TournamentGame
    from app.models.staff_user import StaffUser
    import random, string as _s

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
```

#### CANCELLABLE_STATUSES unit test

```python
async def test_cancellable_statuses_includes_room_released():
    """room_released must be in CANCELLABLE_STATUSES after Problem #3 fix."""
    from app.models.tournament import CANCELLABLE_STATUSES, TournamentStatus
    assert TournamentStatus.room_released in CANCELLABLE_STATUSES


async def test_cancellable_statuses_excludes_live():
    """live must NOT be in CANCELLABLE_STATUSES."""
    from app.models.tournament import CANCELLABLE_STATUSES, TournamentStatus
    assert TournamentStatus.live not in CANCELLABLE_STATUSES


async def test_valid_transitions_room_released_includes_cancelled():
    """VALID_TRANSITIONS[room_released] must include cancelled."""
    from app.models.tournament import VALID_TRANSITIONS, TournamentStatus
    assert TournamentStatus.cancelled in VALID_TRANSITIONS[TournamentStatus.room_released]
```

### Existing tests to preserve unchanged

The following existing tests in `test_room_release_lock.py` still pass after the fix and must not be deleted or modified:

- `test_cancel_before_room_release_succeeds` (A1)
- `test_cancel_before_room_release_refunds_entries` (A2)
- `test_release_room_transitions_to_room_released` (C1)
- `test_player_can_join_room_released_tournament` (D1)
- `test_player_cannot_join_live_tournament` (D2)
- `test_player_can_still_join_open_tournament` (D3)
- `test_player_token_cannot_cancel` (E1)
- `test_unauthenticated_cancel_rejected` (E2)
- `test_locked_exception_properties` (F1 — keep; exception class still exists, its properties remain valid)
- `test_status_unchanged_and_room_credentials_intact` — **delete** this test; after the fix, a successful cancel WILL change the status and credentials are no longer relevant
- `test_joinable_statuses_contains_room_released` (H1)
- `test_joinable_statuses_does_not_contain_live` (H2)
- `test_joinable_statuses_still_contains_open` (H3)
- `test_room_is_released_property_accurate` (H4)

**Files:** `backend/tests/test_room_release_lock.py`

**Verify:** `cd backend && python -m pytest tests/test_room_release_lock.py -v` — all tests pass. Then `cd backend && python -m pytest` — full suite (381 + new tests) passes with no regressions.

---

## Dependency order

```
Item 1 (model)
  └─ Item 2 (service) — depends on Item 1 having room_released in CANCELLABLE_STATUSES
       └─ Item 7 (tests B1-new through CANCELLABLE_STATUSES unit tests) — depends on Items 1+2
Item 3 (backend router listing) — independent of Items 1+2
  └─ Item 7 (tests L1-new, L2-new) — depends on Item 3
Item 4 (docstring) — independent, no dependencies
Item 5 (admin JS) — independent of backend items; no automated test
Item 6 (player HTML) — independent of backend items; no automated test
```

Items 1 → 2 → 7 must run in order. Items 3, 4, 5, 6 can run in any order among themselves once Items 1+2 are done (they don't depend on each other).

---

## Summary checklist

| # | File | Change |
|---|------|--------|
| 1 | `backend/app/models/tournament.py` | Add `room_released` to `CANCELLABLE_STATUSES`; add `cancelled` to `VALID_TRANSITIONS[room_released]` |
| 2 | `backend/app/services/tournament_service.py` | Remove `if tournament.room_is_released: raise TournamentLockedAfterRoomReleaseError()` guard block; remove unused import |
| 3 | `backend/app/routers/tournaments.py` | Change `filter_statuses = [TS.open]` default to `[TS.open, TS.room_released]` |
| 4 | `backend/app/routers/admin_tournaments.py` | Update summary string and docstring only |
| 5 | `frontend/assets/js/admin/admin-tournaments.js` | Add `'room_released'` to cancellable check; add warning dialog; update action-card copy |
| 6 | `frontend/tournament-details.html` | Add credential timing row to success modal; add timing notice to entry panel; add cancelled/refunded notice |
| 7 | `backend/tests/test_room_release_lock.py` | Delete 5 old B-series tests; add 10 new tests |

**No changes needed to:**
- `TournamentEntry` model (schema is complete)
- `wallet_service.py` (refund logic reuses existing path)
- `frontend/assets/js/tournaments.js` (isJoinable already handles room_released)
- `frontend/tournaments.html` (backend fix is sufficient; chip still sends `?status=open`)
- `backend/app/core/exceptions.py` (`TournamentLockedAfterRoomReleaseError` class stays — it is still used in the existing test F1 and may be useful for other guards)
- Any deposit, withdrawal, Razorpay, or payment file
