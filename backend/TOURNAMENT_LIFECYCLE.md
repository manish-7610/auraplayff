# AuraPlayFF — Tournament Engine: Developer Reference

This document describes the complete tournament lifecycle as implemented in
Phase 3. It is the authoritative reference for the backend tournament engine.

---

## 1. Tournament Lifecycle (Status Machine)

```
DRAFT
  │
  ▼ (staff: open)
OPEN  ──────────────────────────────────────────────────────► CANCELLED
  │                                                              ▲
  ▼ (staff: confirm)                                            │
CONFIRMED ───────────────────────────────────────────────────── │
  │                                                              │
  ▼ (staff: release-room)                                        │
ROOM_RELEASED                                                    │
  │                                                              │
  ▼ (staff/scheduler: go-live; requires player_count >= min)    │
LIVE                                                             │
  │
  ▼ (staff: enter-results)
RESULT_ENTRY
  │
  ▼ (staff: complete — triggers referral rewards)
COMPLETED
```

### Valid transitions

| From           | To               | Endpoint / trigger                        | Permission required          |
|----------------|------------------|-------------------------------------------|------------------------------|
| `draft`        | `open`           | `POST …/open`                             | `tournament.create`          |
| `open`         | `confirmed`      | `POST …/confirm`                          | `tournament.create`          |
| `open`         | `cancelled`      | `POST …/cancel`                           | `tournament.cancel`          |
| `confirmed`    | `room_released`  | `POST …/release-room` (+ room creds)      | `tournament.release_room`    |
| `confirmed`    | `cancelled`      | `POST …/cancel`                           | `tournament.cancel`          |
| `room_released`| `live`           | `POST …/go-live` or scheduler             | `tournament.manage_players`  |
| `live`         | `result_entry`   | `POST …/enter-results`                    | `tournament.enter_results`   |
| `result_entry` | `completed`      | `POST …/complete`                         | `tournament.verify_results`  |

**Terminal states:** `completed` and `cancelled` — no further transitions allowed.

**No backward transitions exist.** Attempting an invalid transition returns
`HTTP 409 INVALID_STATUS_TRANSITION`.

---

## 2. Supported Entry Fees

The allowed entry fees are defined once in `tournament_service.SUPPORTED_ENTRY_FEES`:

```
₹10  ₹20  ₹30  ₹40  ₹50  ₹100
```

Adding a new fee tier requires only adding its `Decimal` value to that frozenset.
All fee validation in `CreateTournamentRequest` and `create_tournament()` uses
this same set — there is no second place to update.

---

## 3. Player Counts

| Constant      | Value | Meaning                                         |
|---------------|-------|-------------------------------------------------|
| `max_players` | 50    | Hard ceiling — no joins accepted once full      |
| `min_players` | 40    | Minimum required to go LIVE                     |

- A tournament can be **OPEN with fewer than 40 players** — that is normal.
- At the time of the `go-live` action (staff or scheduler), if
  `player_count < min_players` the transition is **blocked** with
  `HTTP 409 INSUFFICIENT_PLAYERS`. The tournament stays in `ROOM_RELEASED`.
- Admin/Agent must either wait for more registrations or cancel.
- The system does **not** auto-cancel a tournament that has not reached minimum
  players — that is a manual staff decision.

---

## 4. Join Flow (Concurrency-Safe)

`POST /api/tournaments/{id}/join`

Every step runs inside a single database transaction. The server determines
all amounts — nothing financial is accepted from the client body.

```
1.  SELECT tournament FOR UPDATE (row lock)
2.  Assert status == OPEN
3.  SELECT entry WHERE tournament_id + user_id (duplicate check)
4.  Assert player_count < max_players (slot check)
5.  Read wallet (read-only) → calculate_entry_fee_split()
6.  Generate entry UUID (entry_id)
7.  INSERT TournamentEntry (sheet_number = next_sheet_number)
8.  tournament.player_count += 1
9.  tournament.next_sheet_number += 1
10. FLUSH entry + tournament → catches UNIQUE(tournament_id, user_id) race
11. wallet_service.debit_entry_fee() — wallet SELECT FOR UPDATE inside
12. FLUSH wallet + ledger rows
13. audit_service.log()
CALLER commits
```

If **any** step fails:
- No wallet debit occurs
- No entry is created
- No sheet number is consumed
- The DB transaction is rolled back

### Sheet number safety

Sheet numbers come from `Tournament.next_sheet_number`, incremented under the
same `FOR UPDATE` lock as the entry insertion. This is safe against concurrent
joins because:

- The tournament row lock prevents two requests from reading the same counter.
- The `UNIQUE(tournament_id, sheet_number)` DB constraint is a final guard.
- `MAX(sheet_number) + 1` is **never** used.

---

## 5. Entry Fee Split (Referral Bonus Rule)

```
max_referral = floor(entry_fee × 50%)
actual_referral = min(referral_balance, max_referral)
deposit_required = entry_fee − actual_referral
```

- A player **cannot** pay 100% of an entry fee from referral balance.
- If `deposit_balance < deposit_required` → `HTTP 402 INSUFFICIENT_BALANCE`.
- Both amounts are stored as `deposit_amount_used` and `referral_amount_used`
  on `TournamentEntry` (immutable snapshot).

### Examples

| Entry fee | Referral balance | deposit used | referral used |
|-----------|-----------------|--------------|---------------|
| ₹50       | ₹40             | ₹25          | ₹25 (capped)  |
| ₹50       | ₹10             | ₹40          | ₹10           |
| ₹50       | ₹0              | ₹50          | ₹0            |
| ₹100      | ₹60             | ₹50          | ₹50 (capped)  |

---

## 6. Cancellation and Refund Flow

`POST /api/admin/tournaments/{id}/cancel`  — requires `tournament.cancel`

Allowed from: `open`, `confirmed` only.

```
SELECT tournament FOR UPDATE
Assert status in {open, confirmed}
SELECT all TournamentEntry WHERE status = ACTIVE
FOR each entry:
    wallet_service.refund_entry_fee(
        deposit_amount = entry.deposit_amount_used,
        referral_amount = entry.referral_amount_used,
        idempotency_key_deposit = "refund_deposit:<entry_id>",
        idempotency_key_referral = "refund_referral:<entry_id>",
    )
    entry.status = REFUNDED
    entry.refunded_at = now
    entry.refund_deposit_tx_id = tx.id
    entry.refund_referral_tx_id = tx.id  (if applicable)
tournament.status = CANCELLED
tournament.player_count = 0
tournament.cancelled_at = now
audit_service.log(...)
CALLER commits (entire batch is atomic)
```

**Refund properties:**
- Deposit is returned to `deposit_balance` (same column it was deducted from).
- Referral bonus is returned to `referral_balance` (same column).
- Idempotency keys are deterministic: retrying the cancel endpoint for the
  same tournament is safe — already-refunded entries produce a
  `DUPLICATE_TRANSACTION` which is caught and ignored.
- If any single refund fails (e.g. wallet frozen), the whole transaction
  rolls back — no partial cancellation state.

---

## 7. Room Credentials

Room ID and password are released by authorised staff via:
`POST /api/admin/tournaments/{id}/release-room` — requires `tournament.release_room`

This also transitions the tournament from `confirmed` → `room_released`.

### Who can see room credentials?

| Actor             | Condition                                              | Can see? |
|-------------------|--------------------------------------------------------|----------|
| Player (enrolled) | Entry status = ACTIVE AND room_released_at is set      | ✅ Yes   |
| Player (enrolled) | Room not yet released                                  | ❌ No    |
| Player (not enrolled) | Any                                               | ❌ 403   |
| Admin staff       | Always (via admin detail endpoint)                     | ✅ Yes   |

Player endpoint: `GET /api/tournaments/{id}/room`

The player-facing list and detail endpoints **never** include room credentials,
regardless of release status.

---

## 8. Referral Integration

Referral rewards are triggered at **tournament completion**, not at join time.

When `POST /api/admin/tournaments/{id}/complete` is called:

1. `transition_status(target=COMPLETED)` runs.
2. `_trigger_referral_completions()` is called inside the same transaction.
3. For every ACTIVE entrant, `referral_service.mark_first_tournament_completed()` is called.
4. For each player who was referred (and hasn't had their first tournament completed before), this:
   - Sets `ReferralRelationship.first_tournament_completed = True`
   - Credits ₹20 to `referral_balance` of the referrer (User A)
   - Credits ₹20 to `referral_balance` of the referred user (User B)
   - Uses idempotency keys `"referral_bonus:<id>:referrer"` / `"referral_bonus:<id>:referred"`
5. All referral credits are part of the same DB transaction as the completion.

If a referral reward fails (e.g. wallet frozen), the exception is caught and
logged — it does **not** abort the tournament completion.

---

## 9. Automatic LIVE Transition (Scheduler Hook)

`tournament_service.attempt_scheduled_transition(db, tournament_id)` is the
entry-point for a background scheduler.

It transitions `room_released → live` when:
- `scheduled_at` has passed
- `player_count >= min_players`

It is **idempotent**: calling it on an already-LIVE tournament is a no-op.

The staff `go-live` endpoint uses this same function, so manual and automatic
paths share identical business logic.

To wire up an actual scheduler (APScheduler, Celery, pg_cron), call:
```python
await tournament_service.attempt_scheduled_transition(db, tournament_id)
await db.commit()
```

---

## 10. API Reference

### Player APIs

| Method | Path | Description |
|--------|------|-------------|
| `GET`  | `/api/tournaments` | List open/upcoming tournaments |
| `GET`  | `/api/tournaments?status=all` | List all non-cancelled tournaments |
| `GET`  | `/api/tournaments/{id}` | Tournament detail |
| `POST` | `/api/tournaments/{id}/join` | Join tournament (deducts entry fee) |
| `GET`  | `/api/tournaments/{id}/my-entry` | My entry + payment breakdown |
| `GET`  | `/api/tournaments/{id}/room` | Room ID + password (enrolled only, after release) |

### Admin APIs

| Method | Path | Permission |
|--------|------|------------|
| `POST` | `/api/admin/tournaments` | `tournament.create` |
| `GET`  | `/api/admin/tournaments` | Any staff |
| `GET`  | `/api/admin/tournaments/{id}` | Any staff |
| `POST` | `/api/admin/tournaments/{id}/open` | `tournament.create` |
| `POST` | `/api/admin/tournaments/{id}/confirm` | `tournament.create` |
| `POST` | `/api/admin/tournaments/{id}/release-room` | `tournament.release_room` |
| `POST` | `/api/admin/tournaments/{id}/go-live` | `tournament.manage_players` |
| `POST` | `/api/admin/tournaments/{id}/enter-results` | `tournament.enter_results` |
| `POST` | `/api/admin/tournaments/{id}/complete` | `tournament.verify_results` |
| `POST` | `/api/admin/tournaments/{id}/cancel` | `tournament.cancel` |
| `GET`  | `/api/admin/tournaments/{id}/entries` | `user.view` |

---

## 11. Database Tables

### `tournaments`

| Column | Type | Notes |
|--------|------|-------|
| `id` | UUID PK | |
| `tournament_code` | VARCHAR(20) UNIQUE | e.g. `T-0001` |
| `title` | VARCHAR(120) | |
| `game` | VARCHAR(20) | `free_fire` |
| `mode` | VARCHAR(20) | `solo` |
| `entry_fee` | NUMERIC(10,2) | CHECK > 0 |
| `max_players` | INTEGER | DEFAULT 50 |
| `min_players` | INTEGER | DEFAULT 40 |
| `player_count` | INTEGER | Atomic counter, CHECK ≥ 0 |
| `next_sheet_number` | INTEGER | Atomic counter, CHECK ≥ 1 |
| `status` | VARCHAR(20) | See lifecycle above |
| `scheduled_at` | TIMESTAMPTZ | Indexed |
| `room_id` | VARCHAR(50) | NULL until released |
| `room_password` | VARCHAR(50) | NULL until released |
| `room_released_at` | TIMESTAMPTZ | NULL until released |
| `room_released_by_id` | UUID FK staff | NULL until released |
| `started_at` | TIMESTAMPTZ | Set when → LIVE |
| `completed_at` | TIMESTAMPTZ | Set when → COMPLETED |
| `cancelled_at` | TIMESTAMPTZ | Set when → CANCELLED |
| `cancellation_reason` | TEXT | NULL unless cancelled |
| `created_by_id` | UUID FK staff | RESTRICT delete, indexed |
| `created_by_display_name` | VARCHAR(100) | Immutable snapshot |
| `created_by_role` | VARCHAR(20) | Immutable snapshot |
| `completed_by_id` | UUID FK staff | NULL until completed |
| `cancelled_by_id` | UUID FK staff | NULL until cancelled |
| `created_at` | TIMESTAMPTZ | |
| `updated_at` | TIMESTAMPTZ | |

### `tournament_entries`

| Column | Type | Notes |
|--------|------|-------|
| `id` | UUID PK | |
| `tournament_id` | UUID FK→tournaments | RESTRICT delete, indexed |
| `user_id` | UUID FK→users | RESTRICT delete, indexed |
| `sheet_number` | INTEGER | CHECK ≥ 1; UNIQUE(tournament, sheet) |
| `entry_fee_snapshot` | NUMERIC(10,2) | Immutable at join |
| `deposit_amount_used` | NUMERIC(10,2) | Immutable at join |
| `referral_amount_used` | NUMERIC(10,2) | Immutable at join |
| `status` | VARCHAR(20) | `active` \| `refunded` \| `disqualified` |
| `refunded_at` | TIMESTAMPTZ | NULL unless refunded |
| `refund_deposit_tx_id` | UUID | FK to wallet_transactions.id (soft) |
| `refund_referral_tx_id` | UUID | FK to wallet_transactions.id (soft) |
| `joined_at` | TIMESTAMPTZ | Indexed DESC |

**Unique constraints:**
- `UNIQUE(tournament_id, user_id)` — one entry per player per tournament
- `UNIQUE(tournament_id, sheet_number)` — no duplicate sheet numbers

---

## 12. Concurrency Protections Summary

| Race condition | Protection |
|----------------|------------|
| Two users get same sheet number | Tournament `FOR UPDATE` + `UNIQUE(tournament_id, sheet_number)` |
| Two users fill the 50th slot | Tournament `FOR UPDATE` + `player_count` check under lock |
| Double wallet debit on retry | Idempotency keys `entry_deposit:<entry_id>` + DB UNIQUE constraint |
| Duplicate join (same user) | Flush before wallet debit catches `UNIQUE(tournament_id, user_id)` early |
| Double refund on retry | Idempotency keys `refund_deposit:<entry_id>` + DB UNIQUE constraint |
| Join while cancellation runs | Both hold tournament `FOR UPDATE` — one waits, then sees `CANCELLED` |
| Room release after cancel | `assert_valid_transition` blocks; cancel is a terminal transition path |
| Status change races with join | Join checks status under `FOR UPDATE`; any concurrent status change waits |

---

## 13. Blockers / Dependencies for Phase 4 (Results + Prizes)

The following are **not yet implemented** and are required for the next phase:

1. **Result submission model** — `TournamentResult` table for per-player kill counts and placement.
2. **Prize distribution** — `wallet_service.credit_prize()` and `wallet_service.credit_kill_reward()` are implemented; the result service needs to call them with correct amounts.
3. **Screenshot/proof model** — Supabase Storage integration for match proof uploads.
4. **Kill reward leaderboard** — Top-10 kill reward logic and amounts not yet defined.
5. **Scheduler wiring** — `attempt_scheduled_transition()` is ready; needs APScheduler/Celery/pg_cron integration.
6. **Withdrawal system** — `withdrawal_request` / `withdrawal_completed` transaction types are in the wallet model; the flow is not yet implemented.
7. **Razorpay/payment gateway** — `credit_deposit_balance()` is implemented; UPI/QR verification and webhook handling are not yet implemented.
