# Phase 5 — Deposits + Razorpay + Manual UPI + Withdrawals

## Overview

Phase 5 adds real-money deposit and withdrawal infrastructure to AuraPlayFF.  
All financial operations are server-side, transactional, Decimal-based, and idempotent.  
No wallet credit ever happens from a client callback alone.

---

## Architecture

```
app/
├── payments/
│   ├── base.py              # PaymentGateway abstract interface + dataclasses
│   ├── razorpay_adapter.py  # Razorpay implementation (httpx + HMAC-SHA256)
│   └── factory.py           # get_payment_gateway() — returns configured adapter
├── models/
│   ├── deposit.py           # Deposit model + DepositMethod/DepositStatus enums
│   ├── withdrawal.py        # Withdrawal model + WithdrawalStatus enum
│   ├── provider_event.py    # ProviderEvent model (webhook idempotency log)
│   └── platform_setting.py  # PlatformSetting model + SETTING_* constants
├── schemas/
│   ├── deposit.py           # Pydantic request/response schemas for deposits
│   └── withdrawal.py        # Pydantic request/response schemas for withdrawals
├── services/
│   ├── deposit_service.py   # Deposit business logic
│   ├── withdrawal_service.py # Withdrawal business logic
│   └── wallet_service.py    # Extended: reserve/release/complete withdrawal
└── routers/
    ├── deposits.py          # Player deposit endpoints
    ├── withdrawals.py       # Player withdrawal endpoints
    ├── admin_deposits.py    # Admin deposit management
    ├── admin_withdrawals.py # Admin withdrawal management
    └── webhooks.py          # Razorpay webhook receiver
```

---

## Online Deposit Flow (Razorpay)

```
Player                Backend                   Razorpay
  │                      │                          │
  │  POST /deposits/online│                          │
  │  { amount: 500 }      │                          │
  │ ─────────────────────>│                          │
  │                       │ Validate amount          │
  │                       │ Create Deposit(PENDING)  │
  │                       │ POST /v1/orders ─────────>
  │                       │                  order_id│
  │                       │ Store order_id on deposit│
  │  { key_id, order_id } │                          │
  │ <─────────────────────│                          │
  │                       │                          │
  │  [Razorpay Checkout]  │                          │
  │ ─────────────────────────────────────────────────>
  │  payment success      │                          │
  │                       │                          │
  │  POST /deposits/{id}/verify                      │
  │  { order_id, payment_id, signature }             │
  │ ─────────────────────>│                          │
  │                       │ HMAC verify signature    │
  │                       │ credit_deposit_balance() │
  │                       │ Deposit → PAID           │
  │  { status: paid }     │                          │
  │ <─────────────────────│                          │
  │                       │                          │
  │                       │       Webhook arrives ───>
  │                       │ Verify sig + idempotency │
  │                       │ Already PAID → ignore    │
  │                       │ Return 200               │
```

### Verification steps (server-side)

1. Load internal `Deposit` by `deposit_id` + `user_id` ownership check  
2. Confirm `provider_order_id` matches what we stored (not client-supplied)  
3. HMAC-SHA256: `HMAC(KEY_SECRET, "{order_id}|{payment_id}") == signature`  
4. Amount taken from internal `Deposit.amount` — never from client  
5. Idempotency key `deposit:{id}:paid` on `WalletTransaction` prevents double-credit  
6. `deposit.wallet_tx_id` FK set atomically — second call finds it set and returns  

---

## Webhook Idempotency

Every inbound Razorpay webhook is recorded in `provider_events` before processing.

```
Webhook arrives
    │
    ▼
Verify X-Razorpay-Signature (HMAC-SHA256 over raw body)
    │
    ▼
SELECT from provider_events WHERE provider='razorpay' AND event_id=?
    │
    ├── EXISTS → return {"status": "already_processed"} — 200 OK, no wallet change
    │
    └── NOT EXISTS →
            INSERT provider_events (status=received)
                │
                ├── IntegrityError (race) → return already_processed
                │
                └── SUCCESS →
                        Find deposit by provider_order_id
                        Check deposit.wallet_tx_id → already paid? → ignore
                        credit_deposit_balance() with idempotency_key
                        Deposit → PAID
                        provider_event → processed
```

---

## Manual UPI / UTR Deposit Flow

```
Player                           Backend                  Admin
  │                                 │                       │
  │  POST /deposits/manual          │                       │
  │  { amount: 200 }                │                       │
  │ ──────────────────────────────> │                       │
  │                                 │ Create Deposit(PENDING_MANUAL_VERIFICATION)
  │  { deposit, upi_info }          │                       │
  │ <────────────────────────────── │                       │
  │                                 │                       │
  │  [Player transfers ₹200 to UPI ID]                      │
  │                                 │                       │
  │  POST /deposits/manual/{id}/utr │                       │
  │  { utr: "HDFC123456789" }       │                       │
  │ ──────────────────────────────> │                       │
  │                                 │ Validate UTR uniqueness
  │                                 │ Store utr_normalized  │
  │  { status: pending_manual }     │                       │
  │ <────────────────────────────── │                       │
  │                                 │                       │
  │                                 │ GET /admin/deposits/pending-manual
  │                                 │ <────────────────────── │
  │                                 │                         │
  │                                 │ POST /admin/deposits/{id}/approve
  │                                 │ <────────────────────── │
  │                                 │ UTR uniqueness re-check │
  │                                 │ credit_deposit_balance()│
  │                                 │ Deposit → APPROVED      │
  │                                 │ Audit logged            │
  │  [Balance updated]              │ ──────────────────────> │
```

### UTR Security

- UTR is normalised to uppercase, stripped of whitespace  
- A partial unique index `uix_deposits_utr_approved` ensures one UTR = one approved deposit  
- Duplicate UTR submission across pending deposits is also blocked at service layer  
- Typing a UTR **never** credits the wallet — admin must explicitly approve  

---

## Withdrawal Flow

```
Player                     Backend                   Admin
  │                           │                         │
  │  GET /withdrawals/        │                         │
  │  balance-summary          │                         │
  │ ─────────────────────────>│                         │
  │  { winning: 800,          │                         │
  │    hold: 0,               │                         │
  │    available: 800 }       │                         │
  │ <─────────────────────────│                         │
  │                           │                         │
  │  POST /withdrawals        │                         │
  │  { amount: 300,           │                         │
  │    upi_id: "p@upi" }      │                         │
  │ ─────────────────────────>│                         │
  │                           │ Validate >= ₹100        │
  │                           │ Check available_winning │
  │                           │ Check no active request │
  │                           │ reserve_withdrawal()    │
  │                           │  winning  800 → 500     │
  │                           │  hold       0 → 300     │
  │  { status: pending }      │                         │
  │ <─────────────────────────│                         │
  │                           │                         │
  │                           │ GET /admin/withdrawals/pending
  │                           │ <───────────────────────│
  │                           │                         │
  │                           │ POST …/process          │
  │                           │ <───────────────────────│
  │                           │ PENDING → PROCESSING    │
  │                           │                         │
  │                           │ POST …/mark-paid        │
  │                           │ { payout_utr: "SBI…" }  │
  │                           │ <───────────────────────│
  │                           │ complete_withdrawal()   │
  │                           │  hold 300 → 0 (debit)   │
  │                           │ PROCESSING → PAID       │
  │  [Funds received]         │ Audit logged            │
```

### Balance Reservation Accounting

| Event | winning_balance | withdrawal_hold | Net |
|---|---|---|---|
| Start | 800 | 0 | — |
| reserve (request) | 500 | 300 | winning debited, hold credited |
| complete (paid) | 500 | 0 | hold debited (funds paid out) |

| Event | winning_balance | withdrawal_hold | Net |
|---|---|---|---|
| Start | 800 | 0 | — |
| reserve (request) | 500 | 300 | — |
| release (rejected) | 800 | 0 | hold debited, winning refunded |

### Idempotency keys on wallet ledger

| Action | Idempotency key |
|---|---|
| Reserve (debit winning) | `withdrawal:{id}:reserve:debit` |
| Reserve (credit hold) | `withdrawal:{id}:reserve:credit` |
| Release (debit hold) | `withdrawal:{id}:release:debit` |
| Release (credit winning) | `withdrawal:{id}:release:credit` |
| Complete (debit hold) | `withdrawal:{id}:complete` |

---

## Balance Rules

| Balance | Withdrawable | Notes |
|---|---|---|
| Deposit Balance | No | Can only be spent on entry fees |
| Winning Balance | Yes (min ₹100) | Available = winning − hold |
| Referral Balance | Never | Non-withdrawable by design |
| Withdrawal Hold | — | Internal reservation, not a spendable balance |

---

## API Reference

### Player Endpoints

| Method | Path | Description |
|---|---|---|
| POST | `/api/deposits/online` | Create Razorpay order |
| POST | `/api/deposits/online/{id}/verify` | Verify payment (client callback) |
| POST | `/api/deposits/manual` | Create manual UPI deposit |
| POST | `/api/deposits/manual/{id}/utr` | Submit UTR |
| GET | `/api/deposits/upi-info` | Get UPI display info |
| GET | `/api/deposits/{id}` | Get deposit status |
| GET | `/api/deposits` | List own deposits |
| POST | `/api/withdrawals` | Submit withdrawal request |
| GET | `/api/withdrawals/balance-summary` | Wallet balance with hold |
| GET | `/api/withdrawals/{id}` | Get withdrawal status |
| GET | `/api/withdrawals` | List own withdrawals |

### Admin Endpoints

| Method | Path | Permission | Description |
|---|---|---|---|
| GET | `/api/admin/deposits/pending-manual` | `deposit.view` | Pending manual deposits |
| GET | `/api/admin/deposits` | `deposit.view` | All deposits (filtered) |
| GET | `/api/admin/deposits/{id}` | `deposit.view` | Deposit details |
| POST | `/api/admin/deposits/{id}/approve` | `deposit.approve` | Approve + credit wallet |
| POST | `/api/admin/deposits/{id}/reject` | `deposit.reject` | Reject deposit |
| GET | `/api/admin/withdrawals/pending` | `withdrawal.view` | Pending withdrawals |
| GET | `/api/admin/withdrawals` | `withdrawal.view` | All withdrawals |
| GET | `/api/admin/withdrawals/{id}` | `withdrawal.view` | Withdrawal details |
| POST | `/api/admin/withdrawals/{id}/process` | `withdrawal.process` | Mark processing |
| POST | `/api/admin/withdrawals/{id}/mark-paid` | `withdrawal.mark_paid` | Mark paid + UTR |
| POST | `/api/admin/withdrawals/{id}/reject` | `withdrawal.reject` | Reject + release hold |

### Webhook Endpoint

| Method | Path | Auth | Description |
|---|---|---|---|
| POST | `/api/webhooks/razorpay` | None (Razorpay server) | Razorpay payment events |

---

## RBAC — Phase 5 Permissions

| Code | Description | Default for Agent |
|---|---|---|
| `deposit.view` | Read deposit records | No |
| `deposit.approve` | Approve manual deposits — credits wallet | No |
| `deposit.reject` | Reject manual deposits | No |
| `withdrawal.view` | Read withdrawal records | No |
| `withdrawal.process` | Mark PENDING → PROCESSING | No |
| `withdrawal.approve` | Mark PROCESSING → PAID | No |
| `withdrawal.reject` | Reject → release hold | No |
| `withdrawal.mark_paid` | Enter UTR + mark paid | No |

Super Admin receives all permissions automatically.  
**Agents must never receive financial permissions by default.**

---

## Security Rules

1. **No frontend trust** — wallet credit never happens from a client callback alone.  
2. **HMAC-SHA256** — both payment verification and webhooks use server-side HMAC.  
3. **Secrets never leave the server** — `KEY_SECRET` and `WEBHOOK_SECRET` are env-only.  
4. **Key ID is public** — only `razorpay_key_id` is returned to the frontend checkout.  
5. **Amount from DB** — `expected_amount` always comes from the internal `Deposit` record.  
6. **Idempotency everywhere** — double webhooks, duplicate requests, and retries are safe.  
7. **Row locking** — `SELECT FOR UPDATE` prevents race conditions on balance mutations.  
8. **Decimal only** — no float arithmetic in any financial calculation.  
9. **Partial UTR index** — same UTR cannot be approved twice across different deposits.  
10. **Ownership checks** — players can only see and act on their own deposits/withdrawals.  
11. **One-active-withdrawal rule** — prevents simultaneous over-reservation.  
12. **Audit trail** — every financial action is logged with actor, timestamp, and entity.  

---

## Platform Settings

Stored in `platform_settings` table. Seeded on startup. Super Admin can update.

| Key | Default | Public | Description |
|---|---|---|---|
| `min_withdrawal_amount` | 100.00 | No | Minimum withdrawal in INR |
| `min_deposit_amount` | 10.00 | Yes | Minimum deposit in INR |
| `max_deposit_amount` | 50000.00 | Yes | Maximum single deposit |
| `upi_id` | "" | Yes | UPI ID shown on manual deposit screen |
| `upi_display_name` | AuraPlayFF | Yes | Display name for UPI payment |
| `manual_deposit_instructions` | (text) | Yes | Instructions shown to player |
| `qr_storage_ref` | "" | Yes | Supabase storage path/URL for QR image |
| `deposit_methods_enabled` | ["online","manual"] | No | Active deposit methods |
| `withdrawal_enabled` | true | No | Global withdrawal kill switch |
| `online_deposit_enabled` | true | No | Gateway deposit kill switch |

---

## Migration 0005

File: `alembic/versions/0005_deposits_withdrawals_payments.py`

Creates:
- `deposits` table with 19 columns, 4 FKs, 1 partial unique index (UTR/approved)
- `withdrawals` table with 16 columns, 4 FKs, 3 unique constraints
- `provider_events` table with 9 columns, 1 composite unique index
- `platform_settings` table with 7 columns, 1 unique key index

No changes to existing Phase 1–4 tables.

---

## Financial Accounting Summary

```
Player Deposit Balance  ← online/manual deposits (after approval)
Player Winning Balance  ← tournament prizes, kill rewards
Player Referral Balance ← referral bonuses (non-withdrawable)
Withdrawal Hold         ← temporary reservation during withdrawal processing

Tournament Entry        ← debits deposit_balance (+ up to 50% referral_balance)
Tournament Prize/Kill   ← credits winning_balance
Withdrawal Request      ← debits winning_balance, credits withdrawal_hold
Withdrawal Paid         ← debits withdrawal_hold (funds leave platform)
Withdrawal Rejected     ← debits withdrawal_hold, credits winning_balance (refund)
```

Each flow has its own `TransactionType` and `idempotency_key` ensuring
every credit and debit is traceable, non-duplicable, and fully audited.

---

## Remaining Blockers

1. **Razorpay credentials** — `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`,
   `RAZORPAY_WEBHOOK_SECRET` must be configured in `.env` before the online
   deposit flow can be used. Obtain from Razorpay dashboard (test mode first).

2. **UPI ID** — `upi_id` in `platform_settings` is empty by default. Super Admin
   must set it to the actual merchant UPI ID before accepting manual deposits.

3. **Webhook URL registration** — the Razorpay dashboard must be configured to
   send webhooks to `https://yourdomain/api/webhooks/razorpay`.

4. **CORS** — ensure `CORS_ALLOWED_ORIGINS` includes the frontend origin.

5. **Full test suite** — comprehensive financial tests (concurrency, edge cases,
   amount precision) are deferred to the dedicated testing/security phase.

6. **Withdrawal notification** — notification delivery to player on status change
   is a stub. Wire up email/push when notification infrastructure is complete.

7. **Agent salary/bonus** — explicitly out of scope for Phase 5.

8. **Scheduler** — auto-expire of stale PENDING deposits is out of scope for Phase 5.
