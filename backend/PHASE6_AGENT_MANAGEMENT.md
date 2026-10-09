# Phase 6 — Agent Management, Daily Targets, Salary & Bonus

## Overview

Phase 6 adds full operational agent management to AuraPlayFF.  
Super Admin creates and manages agents. Agents log in with their own credentials,
operate tournaments, and earn bonuses. All performance data derives from actual
tournament records — agents cannot manipulate counts. Salary is an internal
payroll record and never touches the player wallet system.

---

## Architecture

```
app/
├── models/
│   ├── agent_target_config.py   # AgentTargetConfig — per-tier daily targets + bonus rates
│   └── agent_performance.py     # AgentDailyPerformance, AgentGrace, AgentWarning,
│                                #   AgentSalaryRecord, DailyPerfStatus, SalaryRecordStatus
├── schemas/
│   └── agent.py                 # All Pydantic request/response schemas
├── services/
│   ├── agent_service.py         # CRUD + activate/deactivate + password reset
│   ├── performance_service.py   # refresh counts, grace, warning, excuse
│   └── salary_service.py        # deterministic monthly salary calculation
└── routers/
    ├── agent.py                 # Agent self-service: /api/agent/*
    └── admin_agents.py          # Admin management: /api/admin/agents/* + /api/admin/targets/*
```

---

## Agent Role & Authentication

- Agents authenticate via the existing `/api/admin/auth/login` endpoint.
- Login uses `agent_id` (format: `AGT-NNNN`) + password. No email.
- JWT audience is `"staff"` — same as Super Admin.
- Agent sessions are invalidated immediately when:
  - password is reset (`token_version` incremented)
  - account is deactivated (`token_version` incremented)
- Super Admin accounts are protected: cannot be deactivated or password-reset via agent endpoints.

---

## Agent ID Format

Server-generated sequential IDs: `AGT-0001`, `AGT-0002`, …  
Super Admin uses the configured `SUPER_ADMIN_AGENT_ID` (e.g. `ADMIN-001`).  
Clients never supply agent IDs.

---

## Daily Tournament Targets

Confirmed defaults (configurable via Super Admin):

| Entry Fee | Daily Required | Extra Bonus/Tournament | Deduction/Miss |
|-----------|---------------|------------------------|----------------|
| ₹10       | 8             | ₹25                    | ₹25            |
| ₹20       | 7             | ₹40                    | ₹40            |
| ₹30       | 6             | ₹60                    | ₹60            |
| ₹40       | 5             | ₹80                    | ₹80            |
| ₹50       | 4             | ₹100                   | ₹100           |
| ₹100      | 3             | ₹200                   | ₹200           |

Base monthly salary: **₹10,000** (configurable).

All values stored in `agent_target_configs` table. Super Admin updates via:
`PUT /api/admin/targets/{entry_fee}` or `PUT /api/admin/targets/base`.

---

## Completed Tournament Count

`AgentDailyPerformance.completed_count` is derived from Tournament records:

```sql
SELECT COUNT(*) FROM tournaments
WHERE created_by_id = :agent_id
  AND entry_fee = :tier_entry_fee
  AND status = 'completed'
  AND DATE(completed_at) = :target_date
```

**Agents can never manipulate this count.** It is recomputed on demand by
`performance_service.refresh_daily_performance()`.

An agent's daily target applies only to tiers they operate — there is no
requirement to cover all six tiers every day.

---

## Grace / Warning / Deduction Policy

| Miss # | Action | Wallet effect |
|--------|--------|---------------|
| 1st    | Grace opportunity (Super Admin grants explicitly) | None |
| 2nd    | Warning issued (Super Admin action) | None |
| 3rd+   | Deduction applied | None (payroll record only) |

- **Grace** zeroes the deduction for that day. One grace per daily performance row.
- **Warning** is informational — does NOT automatically deduct salary.
- **Deduction** formula: `shortfall × deduction_per_missing` (default = `extra_bonus_per_tournament`).
- All flags must be explicitly set by Super Admin. No automatic escalation.
- Agent can see their own warnings and graces via self-service API.

### Example: ₹50 tier, target=4, completed=2

```
shortfall = 2
deduction = 2 × ₹100 = ₹200
```

### Example: ₹100 tier, target=3, completed=2

```
shortfall = 1
deduction = 1 × ₹200 = ₹200
```

---

## Extra Bonus

When `completed_count > required_count`:

```
extra_count = completed - required
extra_bonus = extra_count × extra_bonus_per_tournament
```

Example: ₹50 tier, target=4, completed=6  
`extra_count = 2`, `extra_bonus = 2 × ₹100 = ₹200`

---

## Salary Calculation

```
final_salary = base_salary
             + SUM(daily extra_bonus this month)
             - SUM(daily deduction_amount this month)
             + manual_adjustment
```

`final_salary` is floored at ₹0 (never negative).

- Recalculated on demand: `salary_service.recalculate_salary_record()`
- Manual adjustment persists across recalculations.
- Once confirmed by Super Admin, the record is locked (status = `confirmed`).
- The salary record is an **internal payroll record** — no wallet mutations.

---

## API Reference

### Agent Self-Service

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/agent/me` | Own profile |
| GET | `/api/agent/performance/today` | Today's performance (refreshes counts) |
| GET | `/api/agent/performance/month` | Current month rollup |
| GET | `/api/agent/performance/month/{year}/{month}` | Specific month |
| GET | `/api/agent/tournaments` | Own tournament history |
| GET | `/api/agent/salary` | Current month salary estimate |
| GET | `/api/agent/salary/{year}/{month}` | Specific month salary |
| GET | `/api/agent/warnings` | Own warnings |
| GET | `/api/agent/graces` | Own graces |

### Admin — Agent Management

| Method | Path | Permission | Description |
|--------|------|------------|-------------|
| POST | `/api/admin/agents` | `agent.create` | Create agent |
| GET | `/api/admin/agents` | `agent.view` | List agents |
| GET | `/api/admin/agents/{id}` | `agent.view` | Get agent |
| PATCH | `/api/admin/agents/{id}` | `agent.manage` | Update display name |
| POST | `/api/admin/agents/{id}/activate` | `agent.manage` | Activate |
| POST | `/api/admin/agents/{id}/deactivate` | `agent.manage` | Deactivate |
| POST | `/api/admin/agents/{id}/reset-password` | `agent.reset_password` | Reset password |

### Admin — Performance

| Method | Path | Permission | Description |
|--------|------|------------|-------------|
| GET | `/api/admin/agents/{id}/performance/today` | `agent.view_performance` | Today |
| GET | `/api/admin/agents/{id}/performance/month/{yr}/{mo}` | `agent.view_performance` | Month rollup |
| GET | `/api/admin/agents/{id}/performance/daily` | `agent.view_performance` | Paginated daily |
| POST | `/api/admin/agents/{id}/performance/refresh` | `agent.view_performance` | Refresh counts |
| POST | `/api/admin/agents/{id}/grace` | `agent.manage_performance` | Grant grace |
| POST | `/api/admin/agents/{id}/warning` | `agent.manage_performance` | Issue warning |
| POST | `/api/admin/agents/{id}/excuse-day` | `agent.manage_performance` | Excuse day |
| GET | `/api/admin/agents/{id}/graces` | `agent.view_performance` | List graces |
| GET | `/api/admin/agents/{id}/warnings` | `agent.view_performance` | List warnings |

### Admin — Salary

| Method | Path | Permission | Description |
|--------|------|------------|-------------|
| GET | `/api/admin/agents/{id}/salary` | `agent.view_performance` | Current month |
| GET | `/api/admin/agents/{id}/salary/{yr}/{mo}` | `agent.view_performance` | Specific month |
| POST | `/api/admin/agents/{id}/salary/adjust` | `agent.manage_salary` | Manual adjustment |
| POST | `/api/admin/agents/{id}/salary/confirm` | `agent.manage_salary` | Confirm record |
| GET | `/api/admin/agents/{id}/salary/history` | `agent.view_performance` | History |

### Admin — Target Configuration

| Method | Path | Permission | Description |
|--------|------|------------|-------------|
| GET | `/api/admin/targets` | `agent.view` | List all configs |
| PUT | `/api/admin/targets/{entry_fee}` | `agent.manage_targets` | Update tier config |
| PUT | `/api/admin/targets/base` | `agent.manage_targets` | Update base salary |

---

## RBAC Permissions — Phase 6

| Code | Description | Default for Agent |
|------|-------------|-------------------|
| `agent.create` | Create new agent accounts | No |
| `agent.view` | View agent profiles | No |
| `agent.manage` | Activate/deactivate/update agents | No |
| `agent.reset_password` | Reset agent password | No |
| `agent.view_performance` | View performance, salary, grace, warnings | No |
| `agent.manage_performance` | Grant grace, issue warnings, excuse days | No |
| `agent.manage_salary` | Apply manual adjustments, confirm salary | No |
| `agent.manage_targets` | Configure daily targets and bonus rates | No |

Super Admin receives all permissions automatically.  
**Agents never receive any of these permissions by default.**

---

## Security Protections

1. **No self-escalation** — agents cannot grant themselves permissions.
2. **No count manipulation** — `completed_count` derives from DB tournament records only.
3. **No salary modification** — agents cannot touch salary records (no endpoints).
4. **No grace self-grant** — only Super Admin can grant grace or excused status.
5. **No cross-agent access** — agent self-service endpoints always use JWT identity.
6. **Password never returned** — `password_hash` never appears in any response schema.
7. **Session invalidation** — password reset and deactivation bump `token_version`.
8. **Super Admin protected** — deactivate/reset-password endpoints block super_admin accounts.
9. **Decimal only** — all salary and bonus calculations use `Decimal(12,2)`.
10. **Audit trail** — every sensitive action is logged with actor, timestamp, entity.

---

## Migration 0006

File: `alembic/versions/0006_agent_performance_salary.py`

Creates:
- `agent_target_configs` — 1 unique constraint on `entry_fee`
- `agent_daily_performances` — composite unique `(agent_id, perf_date, entry_fee)`
- `agent_graces` — unique `daily_performance_id` (one grace per day)
- `agent_warnings` — FK to `agent_daily_performances` (nullable)
- `agent_salary_records` — composite unique `(agent_id, year, month)`

No changes to Phase 1–5 tables.

---

## Bootstrap Seeding

On every application startup, `_seed_agent_target_configs()` inserts:
- 6 tier config rows (₹10/₹20/₹30/₹40/₹50/₹100) with default targets/bonuses.
- 1 base salary row (entry_fee=NULL, base_monthly_salary=₹10,000).

Existing rows are never overwritten. Safe to restart repeatedly.

---

## Remaining Blockers

1. **Scheduler** — `refresh_daily_performance()` is called on-demand (API endpoint or
   agent self-service load). A nightly scheduler to auto-refresh all active agents
   is explicitly deferred to a future phase.

2. **Month-end automation** — `recalculate_salary_record()` and `confirm_salary_record()`
   require manual Super Admin action. Auto-confirmation on month boundary is deferred.

3. **Notification delivery** — warning/grace/salary-confirmation events do not yet
   push notifications to agents. Wire to notification service when available.

4. **Full test suite** — comprehensive performance calculation tests (edge cases,
   concurrent refreshes, boundary months) are deferred to the testing phase.
