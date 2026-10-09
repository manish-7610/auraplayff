# AuraPlayFF — Phase 4: Results, Prizes, Kill Rewards, Screenshots & Leaderboard

This document is the authoritative developer reference for Phase 4 backend features.

---

## 1. Result Lifecycle

```
(Tournament in RESULT_ENTRY status)
          │
          ▼
   Staff enters results
   POST /api/admin/tournaments/{id}/results
   → TournamentResult created with result_status = "entered"
          │
          ▼  (optional — can skip to completion)
   Staff verifies
   POST /api/admin/tournaments/{id}/results/{rid}/verify
   → result_status = "verified"
          │
          ▼
   Staff completes tournament
   POST /api/admin/tournaments/{id}/complete
   → assert_results_complete() — blocks if any ACTIVE entry has no result
   → distribute_all_payouts() — prizes + kill rewards credited atomically
   → update_player_stats_after_payout() — lifetime stats updated
   → mark_first_tournament_completed() — referral rewards triggered
   → result_status = "finalized" (immutable)
   → tournament.status = "completed"
```

### Immutability

Once `payout_distributed = True`:
- The result row cannot be modified via normal routes.
- Any exceptional correction requires a controlled admin mechanism that
  creates a new compensating transaction — never modifies the ledger retroactively.

---

## 2. Placement Rules

- Placement is 1-indexed (1st, 2nd, 3rd…).
- Placement must be unique within a tournament (`UNIQUE(tournament_id, placement)` DB constraint).
- Attempting to assign a duplicate placement returns `HTTP 409 DUPLICATE_PLACEMENT`.
- Backend pre-checks before DB write; DB constraint is the final guard.
- Only ACTIVE (non-refunded) entries can receive results.
- user_id and ff_uid are derived from the TournamentEntry — never from client input.

---

## 3. Kill Reward Rule (Confirmed ₹50 Tier)

**Only players in the final Top 10 placements are eligible for kill rewards.**

Progressive tiers for the confirmed ₹50 prize structure:

| Kills | Rate per kill |
|-------|---------------|
| 1–3   | ₹5 each       |
| 4–6   | ₹7 each       |
| 7+    | ₹10 each      |

### Calculation formula

```
reward = 0
for each tier [from_kill, to_kill, reward_per_kill]:
    count = min(kills_remaining, tier_capacity)
    reward += count × reward_per_kill
    kills_remaining -= count
```

### Examples

| Placement | Kills | Calculation             | Reward  |
|-----------|-------|-------------------------|---------|
| 1         | 8     | 3×₹5 + 3×₹7 + 2×₹10   | ₹51.00  |
| 5         | 3     | 3×₹5                   | ₹15.00  |
| 10        | 6     | 3×₹5 + 3×₹7            | ₹36.00  |
| 11        | 20    | Not eligible            | ₹0.00   |
| 1         | 0     | 0 kills                | ₹0.00   |

### Implementation

`prize_service.calculate_kill_reward(placement, kills, config)` is a **pure function** — no DB calls, no side effects. It is used identically by:
- The live kill-reward preview endpoint (`GET /api/admin/prize-configs/kill-reward-preview`)
- The actual payout distribution in `distribute_payout()`

Backend is always the source of truth. Client-supplied reward amounts are never trusted.

---

## 4. Confirmed ₹50 Prize Structure

| Position | Prize     |
|----------|-----------|
| 1st      | ₹1,000    |
| 2nd      | ₹300      |
| 3rd      | ₹100      |
| 4th–50th | ₹0        |
| Top 10   | + Kill rewards (progressive) |

These values are seeded by `prize_service.seed_fifty_rupee_config()` on startup (idempotent). They live in the `prize_configs` table and can be updated by an admin without code changes.

---

## 5. Configurable Prize Architecture

The `prize_configs` table has **one row per entry-fee tier**:

```
entry_fee         UNIQUE key  — e.g. 50.00
is_active         Whether this tier is usable for tournament completion
first_place_prize Numeric(12,2)
second_place_prize
third_place_prize
kill_reward_top_n Integer — how many top placements qualify (default 10)
kill_reward_tiers JSONB — ordered list of threshold objects:
                  [{"from_kill":1,"to_kill":3,"reward_per_kill":"5.00"}, ...]
                  to_kill=null means "all remaining kills"
```

**Unconfigured tiers** (₹10, ₹20, ₹30, ₹40, ₹100) have `is_active=False`. Attempting to complete a tournament on one of these tiers raises `HTTP 422 PRIZE_CONFIG_NOT_FOUND` until a super admin activates and configures the tier.

---

## 6. Payout Distribution

### Triggered automatically by tournament completion

When `POST /api/admin/tournaments/{id}/complete` is called, the completion flow runs entirely in one DB transaction:

```
assert_results_complete()         — all active entries have results
set tournament.completed_at
get_prize_config(entry_fee)       — raises if not configured/active
distribute_all_payouts()          — iterates every non-paid result
  └── distribute_payout(result)
        ├── calculate placement prize server-side
        ├── calculate kill reward server-side
        ├── credit_prize()        wallet_service — idempotency key: "prize:<result_id>"
        ├── credit_kill_reward()  wallet_service — idempotency key: "kill_reward:<result_id>"
        └── stamp result: payout_distributed=True, result_status=finalized
update_player_stats_after_payout()
mark_first_tournament_completed() — referral rewards (for each active entrant)
COMMIT
```

If **any** step fails, the whole transaction is rolled back. The tournament stays in `RESULT_ENTRY`. No partial payout state is possible.

### Idempotency keys

| Operation          | Key pattern                  |
|--------------------|------------------------------|
| Placement prize    | `prize:<result_id>`          |
| Kill reward        | `kill_reward:<result_id>`    |

These keys are stable across retries. The `UNIQUE(idempotency_key)` constraint on `wallet_transactions` is the DB-level guard. `DuplicateTransactionError` on retry is treated as success.

---

## 7. Screenshot / Proof System

### Upload rules

| Rule | Value |
|------|-------|
| Upload window | `tournament.completed_at + 15 minutes` |
| Max screenshots per entry | 2 |
| Accepted formats | JPEG, PNG (validated by magic bytes — not filename) |
| Max file size | 5 MB |
| Who can upload | Player with ACTIVE entry only |

### Storage

- Private Supabase Storage bucket (`SUPABASE_STORAGE_BUCKET_SCREENSHOTS`)
- Storage path is **server-generated**: `screenshots/<tournament_id>/<user_id>/<uuid>.<ext>`
- Client cannot choose storage path (path traversal impossible)
- `SUPABASE_SERVICE_ROLE_KEY` is used server-side only, never exposed to frontend

### Review rules

| Rule | Value |
|------|-------|
| Staff review window | `tournament.completed_at + 24 hours` |
| After review window | Normal review endpoints blocked |
| File deletion | NOT deleted after review window |
| Retention period | ~30 days from upload (`expires_at`) |
| Cleanup | Future scheduled job uses `expires_at` index |

### Player endpoints

```
POST /api/tournaments/{id}/proof       — upload screenshot (multipart/form-data)
GET  /api/tournaments/{id}/my-proof    — view own uploaded proofs
```

### Admin endpoints

```
GET  /api/admin/tournaments/{id}/proof                     — list all proofs (screenshot.view)
GET  /api/admin/tournaments/{id}/proof/{proof_id}          — single proof
POST /api/admin/tournaments/{id}/proof/{proof_id}/review   — approve/reject
```

---

## 8. Leaderboard

### Tournament leaderboard (`GET /api/leaderboard/tournaments`)

- Returns completed tournaments with 1st-place winner info.
- `?window=24h` (default) — tournaments completed in last 24 hours.
- `?window=all` — all-time completed tournaments.
- Creator info comes from `tournament.created_by_display_name` (immutable snapshot).

### Player leaderboard (`GET /api/leaderboard/players`)

- Ranked by `?sort_by=winnings` (default), `wins`, or `kills`.
- Only players with ≥ 1 tournament participation are shown.
- Does NOT expose wallet balances or private data.

### Individual player stats (`GET /api/leaderboard/players/{username}`)

- Public stats: total_tournaments, total_wins, total_kills, total_winnings.
- No wallet, no referral, no private data.

---

## 9. Active-User Metrics (`GET /api/admin/metrics/active-users`)

| Metric | Definition |
|--------|-----------|
| `online_now` | `last_seen_at` within last **3 minutes** (UTC) |
| `active_today_ist` | `last_seen_at` on current **IST calendar day** (UTC+05:30) |
| `active_last_24h` | `last_seen_at` within rolling **24 hours** (UTC) |
| `total_registered` | All active (non-banned) players |

Uses existing `User.last_seen_at` updated by `LastSeenMiddleware` — no new tracking table needed.

---

## 10. Agent Tournament Performance (`GET /api/admin/metrics/agents`)

Per-creator statistics:

| Field | Description |
|-------|-------------|
| `total` | Total tournaments created |
| `completed` | Completed tournaments |
| `cancelled` | Cancelled tournaments |
| `active` | Currently in open/confirmed/room_released/live/result_entry |
| `today` | Created today (UTC) |

Creator identity comes from immutable `tournament.created_by_id / display_name / role` — accurate even if the agent account is later modified.

Salary/bonus calculation is **not implemented** — deferred to Phase 5.

---

## 11. Database Tables Added (Migration 0004)

### `prize_configs`

| Column | Type | Notes |
|--------|------|-------|
| `id` | UUID PK | |
| `entry_fee` | NUMERIC(10,2) UNIQUE | Indexed |
| `is_active` | BOOLEAN DEFAULT false | |
| `first_place_prize` | NUMERIC(12,2) | |
| `second_place_prize` | NUMERIC(12,2) | |
| `third_place_prize` | NUMERIC(12,2) | |
| `kill_reward_top_n` | NUMERIC(4,0) DEFAULT 10 | |
| `kill_reward_tiers` | JSONB DEFAULT `[]` | Ordered tier list |
| `notes` | VARCHAR(500) | |
| `created_at / updated_at` | TIMESTAMPTZ | |

### `tournament_results`

| Column | Type | Notes |
|--------|------|-------|
| `id` | UUID PK | |
| `tournament_id` | UUID FK→tournaments | RESTRICT |
| `entry_id` | UUID FK→tournament_entries | RESTRICT, UNIQUE |
| `user_id` | UUID FK→users | RESTRICT |
| `ff_uid_snapshot` | VARCHAR(20) | Snapshot at result entry |
| `placement` | INTEGER | CHECK ≥ 1; UNIQUE per tournament |
| `kills` | INTEGER | CHECK ≥ 0 |
| `prize_amount` | NUMERIC(12,2) | Set at payout |
| `kill_reward_amount` | NUMERIC(12,2) | Set at payout |
| `kill_reward_eligible` | BOOLEAN | |
| `kill_reward_breakdown` | VARCHAR(200) | e.g. "3×₹5 + 3×₹7 + 2×₹10" |
| `payout_distributed` | BOOLEAN DEFAULT false | Indexed |
| `payout_distributed_at` | TIMESTAMPTZ | |
| `payout_tx_prize_id` | UUID | Soft ref to wallet_transactions |
| `payout_tx_kill_id` | UUID | Soft ref to wallet_transactions |
| `result_status` | VARCHAR(20) | pending/entered/verified/finalized |
| `entered_by_id` | UUID FK→staff_users | SET NULL |
| `verified_by_id` | UUID FK→staff_users | SET NULL |

### `screenshot_proofs`

| Column | Type | Notes |
|--------|------|-------|
| `id` | UUID PK | |
| `tournament_id` | UUID FK→tournaments | RESTRICT |
| `entry_id` | UUID FK→tournament_entries | RESTRICT |
| `user_id` | UUID FK→users | RESTRICT |
| `storage_path` | VARCHAR(500) | Server-generated |
| `original_filename` | VARCHAR(255) | Sanitised |
| `content_type` | VARCHAR(50) | image/jpeg or image/png |
| `file_size_bytes` | INTEGER | CHECK > 0 |
| `uploaded_at` | TIMESTAMPTZ | Indexed DESC |
| `upload_deadline` | TIMESTAMPTZ | completed_at + 15 min |
| `review_deadline` | TIMESTAMPTZ | completed_at + 24 h |
| `expires_at` | TIMESTAMPTZ | uploaded_at + 30 days; indexed for cleanup job |
| `review_status` | VARCHAR(20) | pending/approved/rejected |
| `reviewed_by_id` | UUID FK→staff_users | SET NULL |
| `review_notes` | VARCHAR(500) | |

---

## 12. API Reference Summary

### Player APIs

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/tournaments/{id}/results` | Tournament result leaderboard |
| `GET` | `/api/tournaments/{id}/my-result` | Own result + payout |
| `POST` | `/api/tournaments/{id}/proof` | Upload screenshot |
| `GET` | `/api/tournaments/{id}/my-proof` | Own uploaded proofs |
| `GET` | `/api/leaderboard/tournaments` | Recent completed winners |
| `GET` | `/api/leaderboard/players` | Top player rankings |
| `GET` | `/api/leaderboard/players/{username}` | Individual player stats |

### Admin APIs

| Method | Path | Permission |
|--------|------|------------|
| `GET` | `/api/admin/tournaments/{id}/results` | Any staff |
| `POST` | `/api/admin/tournaments/{id}/results` | `tournament.enter_results` |
| `PUT` | `/api/admin/tournaments/{id}/results/{rid}` | `tournament.enter_results` |
| `POST` | `/api/admin/tournaments/{id}/results/{rid}/verify` | `tournament.verify_results` |
| `GET` | `/api/admin/prize-configs/kill-reward-preview` | Any staff |
| `GET` | `/api/admin/prize-configs` | Any staff |
| `GET` | `/api/admin/prize-configs/{entry_fee}` | Any staff |
| `GET` | `/api/admin/tournaments/{id}/proof` | `screenshot.view` |
| `GET` | `/api/admin/tournaments/{id}/proof/{pid}` | `screenshot.view` |
| `POST` | `/api/admin/tournaments/{id}/proof/{pid}/review` | `screenshot.view` |
| `GET` | `/api/admin/metrics/active-users` | Any staff |
| `GET` | `/api/admin/metrics/agents` | Any staff |
| `GET` | `/api/admin/metrics/tournaments` | Any staff |

---

## 13. Security Properties

| Concern | Protection |
|---------|------------|
| Duplicate result entry | `UNIQUE(entry_id)` on tournament_results + service pre-check |
| Duplicate placement | `UNIQUE(tournament_id, placement)` + service pre-check |
| Double payout | Idempotency keys `prize:<id>` + `UNIQUE(idempotency_key)` on wallet_transactions |
| Payout before results ready | `assert_results_complete()` blocks completion |
| Payout on unconfigured tier | `get_prize_config(is_active=True)` raises 422 |
| Result modification after payout | `result.is_immutable` check blocks edits |
| Client-supplied amounts | All prize/reward amounts calculated server-side |
| Screenshot path traversal | Path fully generated server-side; client input never used for path |
| Wrong file type | Magic-byte validation (not filename/Content-Type) |
| Screenshot outside window | `assert_upload_window_open()` blocks after +15 min |
| Screenshot for another player | `user_id` derived from JWT, entry verified against tournament |
| Supabase key exposure | Service-role key only used in `screenshot_service._upload_to_supabase()` |

---

## 14. Blockers for Phase 5

1. **Withdrawal flow** — `withdrawal_request` / `withdrawal_completed` transaction types exist; the endpoint + admin approval workflow are not yet implemented.
2. **Razorpay / UPI deposit** — `credit_deposit_balance()` is ready; payment gateway webhooks and deposit request model are not yet built.
3. **Agent salary/bonus** — creator tracking is in place; payout calculation and disbursement are deferred.
4. **Scheduler wiring** — `attempt_scheduled_transition()` (LIVE), screenshot cleanup (30-day), and leaderboard refresh hooks are architecturally ready but not connected to a job scheduler (APScheduler/Celery/pg_cron).
5. **Prize tier configuration UI** — admin API endpoints to update `prize_configs` (create/update) are not yet implemented; super admin must update the DB directly until Phase 5.
6. **Unconfigured prize tiers** — ₹10, ₹20, ₹30, ₹40, ₹100 prize configs need business confirmation before activation.
