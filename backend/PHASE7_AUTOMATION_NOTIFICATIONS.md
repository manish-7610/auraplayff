# Phase 7 — Scheduler + Automation + Notification Foundation

**AuraPlayFF Backend**
Completed: 2026-09-13

---

## Table of Contents

1. [Scheduler Architecture](#1-scheduler-architecture)
2. [Tournament Auto-Transition](#2-tournament-auto-transition)
3. [Agent Performance Refresh](#3-agent-performance-refresh)
4. [Month-End Salary Snapshot](#4-month-end-salary-snapshot)
5. [Screenshot Retention Cleanup](#5-screenshot-retention-cleanup)
6. [Leaderboard Cache Refresh](#6-leaderboard-cache-refresh)
7. [Active User Metrics](#7-active-user-metrics)
8. [Notification System](#8-notification-system)
9. [Notification APIs](#9-notification-apis)
10. [Job Idempotency](#10-job-idempotency)
11. [Job Monitoring](#11-job-monitoring)
12. [Timezone Handling](#12-timezone-handling)
13. [Security](#13-security)
14. [Files Created / Modified](#14-files-created--modified)
15. [Verification Results](#15-verification-results)

---

## 1. Scheduler Architecture

### Technology

APScheduler 3.x (`AsyncIOScheduler`) — in-process async scheduler that
integrates cleanly with FastAPI's asyncio event loop.

### Startup / Shutdown

The scheduler is created and started inside the FastAPI `lifespan` context
manager in `app/main.py`, **after** bootstrap completes:

```
startup:
  run_bootstrap(db)          ← permissions, super admin, platform settings
  setup_scheduler()          ← register all jobs in _JOB_REGISTRY
  scheduler.start()          ← APScheduler begins ticking
  app.state.scheduler = ...  ← stored on app state for inspection

shutdown (after yield):
  scheduler.shutdown(wait=False)
```

The scheduler is **disabled when `SCHEDULER_ENABLED=false`** (e.g. in
testing environments) without removing any other functionality.

### Job Defaults

All jobs share these APScheduler defaults:

| Setting | Value | Reason |
|---|---|---|
| `coalesce` | `True` | Missed executions are merged into one |
| `max_instances` | `1` | Same job never runs twice concurrently |
| `misfire_grace_time` | `120 s` | Allow up to 2 min late start |

### Job Registry

Every job function is registered in `_JOB_REGISTRY` (inside
`app/services/scheduler_service.py`) by name so Super Admin can
**manually trigger** any job via the API without redeploying.

### Multi-Instance Safety

The current configuration uses APScheduler's default in-process
`MemoryJobStore`.  This is correct and safe for a single-instance
deployment.

For multi-replica deployments:
- Replace with `SQLAlchemyJobStore` backed by the same PostgreSQL DB.
- `max_instances=1` (already set) prevents duplicate execution.
- All jobs are idempotent regardless, so a brief double-execution
  during a rolling deploy is harmless.

### Separation of Concerns

**Jobs never contain business logic directly.**  Each job module
(`app/jobs/*.py`) calls the same service functions used by the admin API:

```
Scheduler fires → run_job() wrapper → job function → service function
                                                   ↘ same as Admin API call
```

This means every job can also be triggered manually by Super Admin with
identical behaviour.

---

## 2. Tournament Auto-Transition

**File:** `app/jobs/tournament_transition.py`
**Schedule:** Every 1 minute

### Rule

A CONFIRMED tournament is transitioned to LIVE if:
- `scheduled_at <= now()` (UTC)
- `player_count >= min_players` (default 40)
- `status == CONFIRMED` (verified after acquiring row lock)

If `player_count < min_players` the tournament is left untouched for
Admin/Agent to decide on cancellation.

### Room-Release Reminder

A separate function `run_room_release_reminder()` runs every 2 minutes.
It finds CONFIRMED tournaments whose `scheduled_at` is within 10 minutes
and emits an `agent.room_release_reminder` notification to the creating
agent.  Room credentials are **never** revealed — only a reminder to
release them manually.

The reminder is idempotent: `idempotency_key` on the notification row
prevents duplicate reminders for the same tournament.

### Concurrency Safety

```sql
SELECT ... WHERE status = 'confirmed' AND scheduled_at <= now()
FOR UPDATE SKIP LOCKED
```

`SKIP LOCKED` ensures that if two worker processes both run the job
simultaneously, they each process different rows — no double-transition.
After acquiring the lock, the status is re-validated before writing.

### Audit

Every auto-transition writes an `AuditLog` record with:
```
actor_type = system
actor_id   = "scheduler:tournament_auto_transition"
action     = "tournament.auto_transitioned_to_live"
```

### Notifications Triggered

- `tournament.started` → every active entry holder

---

## 3. Agent Performance Refresh

**File:** `app/jobs/agent_performance.py`
**Schedule:** Daily at 00:05 IST

### What It Does

Calls `performance_service.refresh_daily_performance()` for **every active
agent** for today's IST calendar date.

`completed_count` is always derived from actual Tournament records:
```
SELECT COUNT(*) FROM tournaments
WHERE created_by_id = agent.id
  AND entry_fee = tier.entry_fee
  AND status = 'completed'
  AND completed_at::date = target_date
```

Manual entries by agents are **never trusted**.

### Idempotency

`refresh_daily_performance()` uses check-then-insert/update logic.
Admin-set flags (`is_grace`, `is_warned`, `is_excused`, `status` =
grace/warned/deducted) are **never overwritten** by the scheduler.

Running the job twice for the same date is safe and produces the same
result.

### Notifications Triggered

After each agent's rows are refreshed:

| Condition | Notification |
|---|---|
| `status == exceeded` and `extra_count > 0` | `agent.extra_bonus_earned` |
| `status == met` | `agent.target_completed` |
| `status == pending` and `shortfall > 0` | `agent.daily_target_status` |

Warnings and grace are **NOT** issued automatically by the scheduler.
Super Admin must action these explicitly.

---

## 4. Month-End Salary Snapshot

**File:** `app/jobs/agent_performance.py`
**Schedule:** 1st of every month at 00:30 IST

### What It Does

Calls `salary_service.recalculate_salary_record()` for every active agent
for the **previous** IST calendar month.

Formula:
```
final_salary = base_salary
             + total_extra_bonus      (sum of daily extra_bonus)
             - total_deductions       (sum of daily deduction_amount)
             + manual_adjustment      (admin-entered, persists across recalcs)

final_salary is floored at 0 (never negative)
```

### What It Does NOT Do

- Does NOT automatically confirm salary (status stays `draft`).
- Does NOT pay salary or touch player wallets.
- Does NOT overwrite a `confirmed` or `paid` record.

Super Admin must review and confirm via `POST /api/admin/agents/{id}/salary/confirm`.

### Idempotency

`AgentSalaryRecord` has a UNIQUE constraint on `(agent_id, year, month)`.
`recalculate_salary_record()` skips any record already in `confirmed` or
`paid` status.  Running the job on the 1st, 2nd, and 3rd of the month all
produce the same result for the previous month.

### Notifications Triggered

- `agent.salary_calculated` → each agent (idempotent per salary record)

---

## 5. Screenshot Retention Cleanup

**File:** `app/jobs/screenshot_cleanup.py`
**Schedule:** Daily at 02:00 IST

### Retention Rule

Screenshots are retained for **~30 days** from upload (`expires_at`
column, set at upload time as `uploaded_at + 30 days`).

The **24-hour review window** (`review_deadline`) only controls normal UI
access.  It does **NOT** trigger deletion.

### Execution Flow

1. SELECT `ScreenshotProof WHERE expires_at <= now()` — batch of 200.
2. For each row:
   - DELETE the object from Supabase Storage via
     `DELETE /storage/v1/object/{bucket}/{storage_path}`.
   - HTTP 200/204 → success.  HTTP 404 → already absent, treat as success.
   - Any other status → log warning, skip DB deletion (retry next run).
3. DELETE the DB row only if storage deletion succeeded.
4. Return count of rows removed.

### Idempotency

- Rows whose storage deletion failed remain in the DB and will be retried
  on the next run.
- A 404 from Supabase is treated as success so orphaned DB rows are cleaned
  even if the storage object was manually deleted.
- Running the job twice for the same set processes rows that still exist.

### Configuration

```
SCREENSHOT_RETENTION_DAYS=30        ← days after upload before cleanup
SUPABASE_URL=...                     ← required for storage deletion
SUPABASE_SERVICE_ROLE_KEY=...        ← service-role key, never from DB
```

If Supabase credentials are not configured, the job logs a warning and
skips all deletions (DB rows are not removed).

---

## 6. Leaderboard Cache Refresh

**File:** `app/jobs/leaderboard_refresh.py`
**Schedule:** Every 5 minutes

### Design

The public leaderboard is query-driven by default.  This job pre-computes
the most expensive queries and stores results in an **in-process dict cache**
with TTL.

```python
# Cache helpers (importable by routers)
get_cache(key)           → value | None  (returns None if TTL expired)
set_cache(key, value, ttl_seconds)
invalidate_cache(key)
invalidate_all()
```

### Cache Keys

| Key | Contents |
|---|---|
| `leaderboard:players:winnings` | Top 50 players by total_winnings |
| `leaderboard:players:wins` | Top 50 players by tournament_wins |
| `leaderboard:players:kills` | Top 50 players by total_kills |
| `leaderboard:tournaments:all` | Last 50 completed tournaments |
| `leaderboard:tournaments:24h` | Completed tournaments in last 24 h |

Default TTL: **300 seconds** (configurable via `LEADERBOARD_CACHE_TTL_SECONDS`).

### Historical Data

Historical tournament and player_stats records are **never deleted** by
this job.  It is read-only against source tables.

### Multi-Replica Note

The in-process dict is per-replica.  For multi-replica deployments
replace the `_CACHE` dict with a Redis-backed cache using the same
`get_cache` / `set_cache` API — no router changes required.

---

## 7. Active User Metrics

**File:** `app/jobs/leaderboard_refresh.py` → `run_active_user_metrics()`
**Schedule:** Every 2 minutes

### Metrics Computed

| Metric | Definition |
|---|---|
| `online` | `last_seen_at >= now() - 3 minutes` |
| `active_today_ist` | `last_seen_at >= start of today in IST` |
| `active_last_24h` | `last_seen_at >= now() - 24 hours` |
| `total_registered` | `COUNT(users WHERE is_active = true)` |

Results cached under `metrics:active_users` with TTL of **120 seconds**.

The admin endpoint `GET /api/admin/metrics/active-users` can read from
this cache (cache-hit) or fall through to a live query (cache-miss).

---

## 8. Notification System

### Model

**File:** `app/models/notification.py`

```
notifications table
  id                   UUID PK
  recipient_type       VARCHAR(20)   player | agent | admin
  user_id              UUID FK → users CASCADE         (player only)
  staff_user_id        UUID FK → staff_users CASCADE   (agent/admin only)
  notification_type    VARCHAR(100)  e.g. "tournament.joined"
  title                VARCHAR(200)
  message              TEXT
  related_entity_type  VARCHAR(60)   e.g. "tournament"
  related_entity_id    VARCHAR(80)   e.g. tournament UUID string
  is_read              BOOLEAN       default false
  read_at              TIMESTAMPTZ
  idempotency_key      VARCHAR(300)  UNIQUE (nullable)
  meta                 JSONB         never contains secrets
  created_at           TIMESTAMPTZ
  expires_at           TIMESTAMPTZ   for future cleanup
```

### Service

**File:** `app/services/notification_service.py`

All notification creation goes through this service — never direct DB
inserts in routers or jobs.

```python
# Core
create_notification(db, *, recipient_type, user_id, staff_user_id,
                    notification_type, title, message, ...)

# Recipient helpers
notify_player(db, *, user_id, notification_type, title, message, ...)
notify_agent(db, *, staff_user_id, notification_type, title, message, ...)
notify_admin(db, *, notification_type, title, message, ...)  # → all super admins
```

`notify_admin()` broadcasts to every active `super_admin` with per-admin
idempotency keys so each admin gets exactly one copy.

`create_notification()` silently swallows `IntegrityError` on duplicate
`idempotency_key` — retried events produce exactly one notification.

### Notification Types

#### Player — Account
| Type | Event |
|---|---|
| `account.registration_success` | New player registered |
| `account.password_reset_requested` | Password reset requested |
| `account.password_changed` | Password successfully changed |

#### Player — Tournament
| Type | Event |
|---|---|
| `tournament.joined` | Player joined a tournament |
| `tournament.cancelled` | Tournament cancelled (with refund) |
| `tournament.room_released` | Room credentials available |
| `tournament.started` | Tournament went LIVE |
| `tournament.completed` | Tournament completed |
| `tournament.result_finalized` | Player's result confirmed |
| `tournament.prize_credited` | Prize money added to wallet |
| `tournament.kill_reward_credited` | Kill reward added to wallet |

#### Player — Deposit
| Type | Event |
|---|---|
| `deposit.initiated` | Deposit initiated |
| `deposit.online_success` | Online payment verified |
| `deposit.manual_submitted` | Manual UPI deposit submitted |
| `deposit.approved` | Manual deposit approved |
| `deposit.rejected` | Manual deposit rejected |

#### Player — Withdrawal
| Type | Event |
|---|---|
| `withdrawal.submitted` | Withdrawal request submitted |
| `withdrawal.processing` | Withdrawal being processed |
| `withdrawal.paid` | Withdrawal sent to UPI |
| `withdrawal.rejected` | Withdrawal rejected, funds restored |

#### Player — Referral
| Type | Event |
|---|---|
| `referral.reward_credited` | Referral bonus credited |

#### Agent
| Type | Event |
|---|---|
| `agent.tournament_created` | Agent created a tournament |
| `agent.tournament_cancelled` | Tournament cancelled |
| `agent.room_release_reminder` | ~10 min pre-start reminder |
| `agent.daily_target_status` | Daily target missed |
| `agent.target_completed` | Daily target met |
| `agent.extra_bonus_earned` | Extra tournaments completed |
| `agent.warning_issued` | Warning issued by Super Admin |
| `agent.grace_granted` | Grace granted by Super Admin |
| `agent.salary_calculated` | Month-end salary computed |
| `agent.salary_adjusted` | Manual salary adjustment applied |

#### Admin
| Type | Event |
|---|---|
| `admin.deposit_pending` | Manual deposit awaiting approval |
| `admin.withdrawal_pending` | New withdrawal request |
| `admin.tournament_cancelled` | Tournament cancelled |
| `admin.result_pending_verification` | Results entered, need review |
| `admin.screenshot_submitted` | Screenshot proof uploaded |
| `admin.agent_target_issue` | Agent target concern |
| `admin.agent_warning_action` | Warning issued |
| `admin.agent_grace_action` | Grace granted |
| `admin.system_error` | Scheduler job failure |

---

## 9. Notification APIs

### Player Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/notifications` | Paginated list (own only) |
| `GET` | `/api/notifications/unread-count` | Quick poll |
| `POST` | `/api/notifications/{id}/read` | Mark one as read |
| `POST` | `/api/notifications/read-all` | Mark all as read |

Query params: `limit` (1–100, default 20), `offset` (default 0),
`unread_only` (bool, default false).

### Staff Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/staff/notifications` | Paginated list |
| `GET` | `/api/staff/notifications/unread-count` | Quick poll |
| `POST` | `/api/staff/notifications/{id}/read` | Mark one as read |
| `POST` | `/api/staff/notifications/read-all` | Mark all as read |

Agents see only `staff_user_id == self.id`.
Super Admin sees all notifications where `staff_user_id IS NOT NULL`
(i.e. all agent and admin notifications; NOT player notifications).

### Security Model

- Ownership is enforced at the **query level** — a player querying another
  player's notification ID receives 404 (no information leakage about
  existence).
- The recipient columns are **never derived from client input** — always
  from the authenticated JWT identity.
- `is_read` and `read_at` are set server-side only.

---

## 10. Job Idempotency

Every scheduled job is safe to run multiple times.

| Job | Idempotency Mechanism |
|---|---|
| Tournament auto-transition | `FOR UPDATE SKIP LOCKED`; status re-validated after lock |
| Room-release reminder | `idempotency_key` UNIQUE on notification |
| Daily performance refresh | `AgentDailyPerformance` UNIQUE (agent, date, entry_fee); upsert logic |
| Month-end salary snapshot | `AgentSalaryRecord` UNIQUE (agent, year, month); skip confirmed/paid |
| Screenshot cleanup | DB row only deleted after successful storage delete; 404 = already gone |
| Leaderboard refresh | Pure read + in-memory overwrite; idempotent by nature |
| Active user metrics | Pure read + in-memory overwrite; idempotent by nature |
| Notifications | `idempotency_key` UNIQUE on notification row; `IntegrityError` silently swallowed |

---

## 11. Job Monitoring

### Admin API

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/admin/jobs` | Dashboard: latest run per job |
| `GET` | `/api/admin/jobs/history` | Paginated full history |
| `GET` | `/api/admin/jobs/registered` | List triggerable job names |
| `POST` | `/api/admin/jobs/{job_name}/trigger` | Manually trigger a job |

All endpoints: **Super Admin only** (role check, not permission check —
scheduler management is not delegated to agents).

### JobExecutionLog Table

```
job_execution_logs
  id               UUID PK
  job_name         VARCHAR(100)  indexed
  execution_id     VARCHAR(80)   UNIQUE — used for in-place status update
  started_at       TIMESTAMPTZ   indexed
  completed_at     TIMESTAMPTZ
  duration_ms      INTEGER
  status           VARCHAR(20)   running | success | failed | skipped
  records_affected INTEGER
  error_summary    TEXT          truncated to 2000 chars, no secrets
  meta             JSONB
```

### Job Execution Lifecycle

```
Phase A — INSERT row with status=running (separate session, committed immediately)
Phase B — Await the job coroutine (own session + transaction)
Phase C — UPDATE row: status=success/failed, completed_at, duration_ms
Phase D — On failure: notify_admin_system_error() best-effort
```

If the application is killed mid-run the row stays in `status=running`.
A future watchdog query can detect stale `running` rows (e.g.
`started_at < now() - interval '10 minutes'`).

---

## 12. Timezone Handling

**Business timezone: IST — Asia/Kolkata (UTC+5:30)**

| Context | Approach |
|---|---|
| DB storage | All timestamps stored as `TIMESTAMPTZ` (UTC) |
| Daily agent performance date | Computed as `datetime.now(IST).date()` |
| Month-end salary | Previous IST month via `datetime.now(IST)` |
| Cron schedules | APScheduler `CronTrigger(timezone='Asia/Kolkata')` |
| `active_today_ist` metric | IST day boundary converted to UTC for DB query |
| Tournament `scheduled_at` | Stored UTC; displayed in IST on frontend |
| Room-release reminder time | Formatted as IST for notification message |
| `run_tournament_auto_transition` | Compares `scheduled_at` against `utcnow()` (both UTC) |

**Naive local system time is never used for business rules.**

```python
from datetime import datetime, timedelta, timezone
_IST = timezone(timedelta(hours=5, minutes=30))

def _today_ist() -> date:
    return datetime.now(_IST).date()
```

---

## 13. Security

### Notification Security

- Recipient is always derived from authenticated identity or server-side
  logic; never from client input.
- Player can only read/mark-read notifications where `user_id == self.id`.
- Agent can only read/mark-read notifications where
  `staff_user_id == self.id`.
- Secrets (room passwords, JWT tokens, wallet keys, payment credentials)
  are **never stored** in `title`, `message`, or `meta`.
- Querying another user's notification ID returns 404 — no existence
  leakage.

### Scheduler Security

- `POST /api/admin/jobs/{job_name}/trigger` is Super Admin only.
- Agents cannot access any scheduler endpoint.
- `error_summary` is truncated to 2000 characters before storage — no
  risk of leaking large internal error traces.
- Job names that are not in `_JOB_REGISTRY` return 404 — no arbitrary
  code execution.

### Duplicate Execution Protection

- `idempotency_key` UNIQUE on `notifications`.
- `execution_id` UNIQUE on `job_execution_logs`.
- `FOR UPDATE SKIP LOCKED` on tournament transition.
- `max_instances=1` on every APScheduler job.

---

## 14. Files Created / Modified

### New Files

| File | Description |
|---|---|
| `app/models/notification.py` | Notification ORM model, RecipientType enum, NotificationType constants |
| `app/models/job_execution_log.py` | JobExecutionLog ORM model, JobStatus enum |
| `app/services/notification_service.py` | Centralised notification creation + ~30 domain event helpers |
| `app/services/scheduler_service.py` | run_job() wrapper, job registry, admin query helpers |
| `app/scheduler.py` | APScheduler setup, 7 job registrations, setup_scheduler() |
| `app/jobs/__init__.py` | Jobs package marker |
| `app/jobs/tournament_transition.py` | CONFIRMED→LIVE + room release reminder |
| `app/jobs/agent_performance.py` | Daily performance refresh + month-end salary snapshot |
| `app/jobs/screenshot_cleanup.py` | Supabase Storage retention cleanup (30 days) |
| `app/jobs/leaderboard_refresh.py` | In-process cache refresh + active user metrics |
| `app/routers/notifications.py` | Player + Staff notification APIs |
| `app/routers/admin_scheduler.py` | Super Admin job monitoring + manual trigger APIs |
| `app/schemas/notification.py` | Pydantic response schemas for notifications |
| `app/schemas/scheduler.py` | Pydantic response schemas for job monitoring |
| `alembic/versions/0007_notifications_jobs.py` | Migration: notifications + job_execution_logs tables |

### Modified Files

| File | Change |
|---|---|
| `app/models/__init__.py` | Added Phase 7 model exports |
| `app/core/permissions.py` | Added SCHEDULER_VIEW, SCHEDULER_TRIGGER permissions |
| `app/main.py` | Scheduler startup/shutdown in lifespan; Phase 7 routers registered |
| `app/config.py` | Added SCHEDULER_ENABLED, SCREENSHOT_RETENTION_DAYS, LEADERBOARD_CACHE_TTL_SECONDS |
| `.env.example` | Phase 7 configuration variables documented |
| `app/routers/auth.py` | Registration → notify_player_registered |
| `app/routers/tournaments.py` | Join → notify_player_tournament_joined |
| `app/routers/deposits.py` | Manual deposit submit → notify player + admin |
| `app/routers/admin_deposits.py` | Approve/reject → notify player |
| `app/routers/withdrawals.py` | Submit → notify player + admin |
| `app/routers/admin_withdrawals.py` | Process/paid/reject → notify player |
| `app/routers/admin_agents.py` | Grace/warning/salary adjust → notify agent |

---

## 15. Verification Results

```
Syntax check (compile):     26/26 files PASS
Import chain check:         17/17 modules PASS
APScheduler version:        3.11.3 (already installed)
httpx:                      installed (screenshot cleanup)
Migration chain (alembic):  0001 → 0002 → 0003 → 0004 → 0005 → 0006 → 0007 (HEAD)
Scheduler job count:        7 jobs configured and verified
```

### Scheduler Jobs Summary

| Job ID | Schedule | Trigger |
|---|---|---|
| `tournament_auto_transition` | Every 1 minute | IntervalTrigger |
| `room_release_reminder` | Every 2 minutes | IntervalTrigger |
| `daily_performance_refresh` | 00:05 IST daily | CronTrigger |
| `month_end_salary_snapshot` | 00:30 IST on 1st of month | CronTrigger |
| `screenshot_cleanup` | 02:00 IST daily | CronTrigger |
| `leaderboard_refresh` | Every 5 minutes | IntervalTrigger |
| `active_user_metrics` | Every 2 minutes | IntervalTrigger |

---

## Remaining Blockers

None.

Phase 7 is complete. Phase 8 can begin when approved.

**Do NOT start Phase 8 automatically.**
