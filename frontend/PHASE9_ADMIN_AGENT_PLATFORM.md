# Phase 9 — Super Admin + Agent Platform

**Completed:** September 2026  
**Built on top of:** Phases 1–8 (existing FastAPI backend + Vanilla HTML/CSS/JS frontend)

---

## 1. Overview

Phase 9 delivers the complete authenticated Super Admin and Agent operational platform. It reuses all existing backend infrastructure from Phases 1–8 without duplicating any existing functionality. The frontend is Vanilla HTML5 / CSS3 / ES Modules — no framework, no build tools, consistent with Phase 8.

---

## 2. Files Created

### Backend — New Routers

| File | Purpose |
|---|---|
| `backend/app/routers/users.py` | `GET /api/users/me` — player self-profile (safe fields only) |
| `backend/app/routers/admin_users.py` | Rewritten: list, detail, ban, unban, issue-password-reset |
| `backend/app/routers/admin_audit.py` | `GET /api/admin/audit-logs` — paginated audit log query |
| `backend/app/routers/admin_settings.py` | `GET/PUT /api/admin/settings` — platform settings CRUD |

### Backend — New Schemas

| File | Schemas |
|---|---|
| `backend/app/schemas/user.py` | `PlayerMeResponse`, `AdminUserSummary`, `AdminUserDetail`, `AdminUserListResponse` |
| `backend/app/schemas/audit.py` | `AuditLogResponse`, `AuditLogListResponse` |
| `backend/app/schemas/settings.py` | `PlatformSettingResponse`, `PlatformSettingsResponse`, `UpdateSettingRequest` |

### Backend — Modified Files

| File | Change |
|---|---|
| `backend/app/main.py` | Registered 3 new routers: users_me, admin_audit, admin_settings |

### Frontend — CSS

| File | Purpose |
|---|---|
| `frontend/assets/css/admin-theme.css` | Full admin design system: sidebar, topbar, cards, tables, forms, chips, modals, toasts, responsive layout |

### Frontend — Shared JS Modules

| File | Purpose |
|---|---|
| `frontend/assets/js/admin/admin-config.js` | API base URL, notification poll interval |
| `frontend/assets/js/admin/admin-api.js` | `adminFetch`, `adminGet/Post/Patch/Put`, silent refresh, `AdminApiError`, `isSuper()`, `isAgent()` |
| `frontend/assets/js/admin/admin-auth.js` | `adminLogin`, `adminLogout`, `restoreAdminSession`, `requireAdminSession`, role visibility |
| `frontend/assets/js/admin/admin-layout.js` | `initAdminLayout()`: sidebar injection, topbar, hamburger, notification badge poll |
| `frontend/assets/js/admin/admin-ui.js` | Toasts, modal helpers, skeleton loaders, `statusChip`, formatters, pagination, `escHtml`, `copyToClipboard` |
| `frontend/assets/js/admin/admin-dashboard.js` | Loads platform metrics, recent tournaments, pending actions, notifications, agent summary |
| `frontend/assets/js/admin/admin-tournaments.js` | Tournament list, create, detail, lifecycle transitions, room release, cancel, entries |
| `frontend/assets/js/admin/admin-players.js` | Player list/search, detail modal, ban/unban, password reset token |
| `frontend/assets/js/admin/admin-finance.js` | Deposit approve/reject, withdrawal process/mark-paid/reject (all `isSuper()` guarded) |
| `frontend/assets/js/admin/admin-results.js` | Tournament selector, result entry form, kill reward preview via backend API, verify result |
| `frontend/assets/js/admin/admin-screenshots.js` | Screenshot proof list, approve/reject within 24h review window, detail modal |
| `frontend/assets/js/admin/admin-agents.js` | Agent list, create, activate/deactivate, reset password, detail: performance/salary/grace/warnings |
| `frontend/assets/js/admin/admin-audit.js` | Audit log query with filters: action, actor, entity type (`isSuper()` guarded) |
| `frontend/assets/js/admin/admin-jobs.js` | Job dashboard cards, execution history, manual trigger (`isSuper()` guarded) |

### Frontend — HTML Pages

| File | Title | Role Access |
|---|---|---|
| `frontend/admin/login.html` | Staff Login | Public (pre-auth) |
| `frontend/admin/index.html` | Dashboard | All staff |
| `frontend/admin/tournaments.html` | Tournaments | All staff |
| `frontend/admin/tournament-details.html` | Tournament Details | All staff |
| `frontend/admin/players.html` | Players | All staff |
| `frontend/admin/deposits.html` | Deposits | Super Admin only |
| `frontend/admin/withdrawals.html` | Withdrawals | Super Admin only |
| `frontend/admin/results.html` | Results | All staff (with permission) |
| `frontend/admin/screenshots.html` | Screenshots | All staff (with permission) |
| `frontend/admin/agents.html` | Agents | Super Admin only |
| `frontend/admin/agent-details.html` | Agent Details | Super Admin only |
| `frontend/admin/audit-logs.html` | Audit Logs | Super Admin only |
| `frontend/admin/jobs.html` | Scheduler | Super Admin only |
| `frontend/admin/notifications.html` | Notifications | All staff |
| `frontend/admin/settings.html` | Platform Settings | Super Admin only |

---

## 3. Backend Routes Added / Changed

### New Routes

| Method | Path | Auth | Permission | Description |
|---|---|---|---|---|
| GET | `/api/users/me` | `get_current_user` | None | Safe player self-profile |
| GET | `/api/admin/users` | `get_current_staff` | `user.view` | List/search players |
| GET | `/api/admin/users/{id}` | `get_current_staff` | `user.view` | Player detail + wallet + stats |
| POST | `/api/admin/users/{id}/ban` | `get_current_staff` | `user.ban` | Ban player, invalidate sessions |
| POST | `/api/admin/users/{id}/unban` | `get_current_staff` | `user.ban` | Unban player |
| POST | `/api/admin/users/{id}/issue-password-reset` | `get_current_staff` | `user.view` | Issue one-time reset token |
| GET | `/api/admin/audit-logs` | `get_current_staff` | `audit.view` | Paginated audit log |
| GET | `/api/admin/settings` | `get_current_staff` | `settings.manage` | All platform settings |
| GET | `/api/admin/settings/{key}` | `get_current_staff` | `settings.manage` | Single setting |
| PUT | `/api/admin/settings/{key}` | `get_current_staff` | `settings.manage` | Update setting value |

### Pre-existing Routes Used by Phase 9 UI (unchanged)

All admin routes from Phases 1–8 are consumed unchanged:
- `/api/admin/auth/*` — staff login/refresh/logout
- `/api/admin/tournaments/*` — full tournament lifecycle
- `/api/admin/deposits/*` — deposit approve/reject
- `/api/admin/withdrawals/*` — withdrawal process/mark-paid/reject
- `/api/admin/agents/*` — agent CRUD + performance + salary
- `/api/admin/targets` — tier target config
- `/api/admin/metrics/*` — active-users, tournaments, agents
- `/api/admin/jobs/*` — scheduler dashboard + history + trigger
- `/api/admin/wallets/*` — wallet view + adjust + freeze
- `/api/admin/referrals` — referral list
- `/api/admin/prize-configs/*` — prize config + kill reward preview
- `/api/admin/tournaments/{id}/proof/*` — screenshot review
- `/api/admin/tournaments/{id}/results/*` — result entry + verify
- `/api/staff/notifications/*` — staff notifications
- `/api/agent/*` — agent self-service

---

## 4. RBAC Matrix

### Super Admin
- Bypasses all permission checks at the backend level
- Can access every page and every action
- Is the only role that can: approve deposits, process withdrawals, manage agents, view audit logs, trigger scheduler jobs, change platform settings

### Agent
Agents have **explicit grants** in `staff_user_permissions`. They can be granted:

| Permission | What it allows |
|---|---|
| `tournament.create` | Create, open, confirm tournaments |
| `tournament.cancel` | Cancel tournaments |
| `tournament.manage_players` | Go-live action |
| `tournament.release_room` | Release room credentials |
| `tournament.enter_results` | Enter result data |
| `tournament.verify_results` | Verify + complete tournament |
| `user.view` | View player profiles |
| `screenshot.view` | View + review screenshots |
| `deposit.view` | View deposit records (read-only, no approval) |
| `withdrawal.view` | View withdrawal records (read-only, no processing) |
| `agent.view` | View other agent profiles |
| `agent.view_performance` | View performance + salary data |

### Financial Permission Matrix

| Action | Super Admin | Agent |
|---|---|---|
| Approve manual deposit | ✅ | ❌ Never |
| Reject manual deposit | ✅ | ❌ Never |
| Process withdrawal | ✅ | ❌ Never |
| Mark withdrawal paid | ✅ | ❌ Never |
| Reject withdrawal | ✅ | ❌ Never |
| Manual wallet adjust | ✅ | ❌ Never |
| Freeze/unfreeze wallet | ✅ | ❌ Never |

Defined in `ADMIN_ONLY_PERMISSIONS` and `FINANCIAL_PERMISSIONS` in `app/core/permissions.py`. The backend enforces this on every request — frontend role hiding is UI convenience only.

---

## 5. Tournament Management Flow

```
DRAFT → OPEN → CONFIRMED → ROOM_RELEASED → LIVE → RESULT_ENTRY → COMPLETED
                    ↓ (any cancellable state)
                CANCELLED (+ atomic refunds)
```

1. **Create** — POST `/api/admin/tournaments` (status = DRAFT)
2. **Open** — POST `.../open` — opens for player registration
3. **Confirm** — POST `.../confirm` — closes registration
4. **Release Room** — POST `.../release-room` with `{room_id, room_password}` — credentials stored server-side, served to joined players only via `/api/tournaments/{id}/room`
5. **Go Live** — POST `.../go-live` — enforces min_players check
6. **Enter Results Phase** — POST `.../enter-results`
7. **Complete** — POST `.../complete` — triggers referral reward logic
8. **Cancel** — POST `.../cancel` — atomically refunds all entries

Allowed entry fees: ₹10, ₹20, ₹30, ₹40, ₹50, ₹100  
Default max players: 50  
Minimum profitable target: 40

---

## 6. Room Credential Release Flow

- Credentials entered only at the CONFIRMED → ROOM_RELEASED transition
- Stored in `tournaments.room_id` and `tournaments.room_password` (server-side only)
- Exposed to joined players via `GET /api/tournaments/{id}/room` — guarded: must be active entry, room must be released, tournament not cancelled
- Admin/agent UI shows credentials in the `tournament-details.html` Room tab after release
- Credentials are **never** in static HTML or public source before authorization
- `room_released_at` and `room_released_by_id` are recorded

---

## 7. Deposit Flow

```
Player submits manual deposit → status: pending_manual_verification
Player submits UTR → UTR recorded (no wallet credit yet)
Admin approves → UTR uniqueness verified → deposit_balance credited → status: approved
Admin rejects → reason recorded → no wallet change → status: rejected
```

Online (Razorpay) deposits are verified server-side via HMAC — no admin action required.

---

## 8. Withdrawal Flow

```
Player requests withdrawal → winning_balance reserved to withdrawal_hold → status: pending
Admin clicks Process → status: processing
Admin enters payout UTR + Mark Paid → withdrawal_hold debited → status: paid
Admin rejects → withdrawal_hold released back to winning_balance → status: rejected
```

---

## 9. Result / Winner Declaration Flow

1. Admin/agent selects a LIVE or RESULT_ENTRY tournament
2. For each player entry: enter placement + kills
3. Backend calculates prize and kill reward amounts via `POST /api/admin/tournaments/{id}/results`
4. Kill reward preview available via `GET /api/admin/prize-configs/kill-reward-preview?placement=N&kills=N&entry_fee=N`
5. After all entries: verify individual results via `POST .../results/{id}/verify`
6. Complete tournament via `POST .../complete` — triggers payout distribution and referral rewards

**Kill reward rules (₹50 tier, as stored in backend `prize_configs`):**
- Kills 1–3 → ₹5 each
- Kills 4–6 → ₹7 each
- Kills 7+ → ₹10 each
- Example 8 kills: 3×₹5 + 3×₹7 + 2×₹10 = ₹51

Frontend displays a preview but backend calculates and validates all final amounts.  
Lifetime player stats (`total_wins`, `total_kills`, `total_winnings`) are updated by the backend — never reset.

---

## 10. Screenshot Review Flow

- Players upload max 2 screenshots within 15 minutes of tournament completion
- File constraints: JPEG/PNG, max 5 MB, validated by magic bytes on backend
- Admin/agent review window: 24 hours from upload
- Storage retention: ~30 days (`expires_at` field)
- Files stored in private Supabase bucket — `storage_path` is in DB but no public URL
- Admin review: `POST /api/admin/tournaments/{id}/proof/{id}/review` with `{decision: "approved"|"rejected", notes}`
- **Known gap:** No signed URL endpoint exists yet — direct image preview requires a Phase 10 addition

---

## 11. Agent Performance / Salary Flow

### Daily Targets (from `agent_target_configs` table)

| Tier | Daily Target | Extra Bonus/tournament |
|---|---|---|
| ₹10 | 8 | ₹25 |
| ₹20 | 7 | ₹40 |
| ₹30 | 6 | ₹60 |
| ₹40 | 5 | ₹80 |
| ₹50 | 4 | ₹100 |
| ₹100 | 3 | ₹200 |

**Base salary:** ₹10,000/month

### Salary Policy
1. First missed target → grace opportunity (one per daily_performance record)
2. Second miss → warning (informational)
3. Further misses → deduction: `shortfall × tier_extra_bonus`
4. Manual adjustment possible via `POST /api/admin/agents/{id}/salary/adjust`
5. Salary locked via `POST /api/admin/agents/{id}/salary/confirm`
6. Month-end snapshot auto-generated by `month_end_salary_snapshot` job

All calculations performed server-side. Frontend displays values from API only.

---

## 12. Scheduler Integration

Existing Phase 7 jobs — all monitored via `GET /api/admin/jobs`:

| Job Name | Frequency | Description |
|---|---|---|
| `tournament_auto_transition` | Every ~5 min | CONFIRMED → LIVE when scheduled time reached + min players met |
| `room_release_reminder` | Every ~5 min | Notifies creating agent when room release is due |
| `daily_performance_refresh` | Daily (IST midnight) | Recalculates agent daily performance from tournament records |
| `month_end_salary_snapshot` | 1st of each month | Generates salary records for previous month |
| `screenshot_cleanup` | Daily | Deletes expired screenshots from Supabase storage |
| `leaderboard_refresh` | Every ~5 min | Pre-computes and caches leaderboard datasets |
| `active_user_metrics` | Every 2 min | Caches online/daily/24h user counts |

Manual trigger: `POST /api/admin/jobs/{name}/trigger` — Super Admin only (role check, not permission).

---

## 13. Audit Logging

All sensitive actions write to `audit_logs` table via `audit_service.log()`:

| Action | Logged by |
|---|---|
| `user.registered` | `auth.py` |
| `user.login` / `staff.login` | `auth.py`, `staff_auth.py` |
| `staff.logout` | `staff_auth.py` |
| `password_reset.issued` | `admin_users.py` |
| `password_reset.completed` | `auth.py` |
| `user.banned` / `user.unbanned` | `admin_users.py` |
| `settings.updated` | `admin_settings.py` |
| `agent.*` (create, activate, deactivate, reset-password) | `admin_agents.py` |
| Deposit approve/reject | `admin_deposits.py` |
| Withdrawal process/paid/reject | `admin_withdrawals.py` |
| Wallet adjust/freeze/unfreeze | `admin_wallet.py` |
| Tournament state transitions | `admin_tournaments.py` |

Fields: `actor_type`, `actor_id`, `action`, `entity_type`, `entity_id`, `old_value`, `new_value`, `ip_address`, `user_agent`, `created_at`. Secrets are never logged.

---

## 14. Notifications

Staff notifications use `GET /api/staff/notifications` — audience-aware:
- Super Admin: sees all staff notifications
- Agent: sees own notifications only

Notification types include: deposit alerts, withdrawal alerts, tournament alerts, result alerts, agent performance alerts, scheduler alerts, system events.

Frontend polls unread count every 30 seconds via `GET /api/staff/notifications/unread-count`. Badge shown in topbar and sidebar nav.

---

## 15. Platform Settings

Configurable at runtime via `/api/admin/settings` (Super Admin only).

| Key | Description |
|---|---|
| `min_withdrawal_amount` | Minimum withdrawal amount (e.g. "100.00") |
| `min_deposit_amount` | Minimum deposit amount |
| `max_deposit_amount` | Maximum deposit per transaction |
| `upi_id` | Platform UPI ID for manual deposits |
| `upi_display_name` | Display name shown next to UPI ID |
| `manual_deposit_instructions` | Player-facing instructions |
| `qr_storage_ref` | Supabase path for QR code image |
| `withdrawal_enabled` | "true"/"false" — kills all withdrawals |
| `online_deposit_enabled` | "true"/"false" — kills Razorpay deposits |
| `deposit_methods_enabled` | JSON array: `["online","manual"]` |

Secrets (JWT key, Razorpay secret, Supabase service role, DB password) are **never** stored here — they are environment variables only.

---

## 16. Responsive Design

All admin pages target these breakpoints:

| Breakpoint | Layout |
|---|---|
| ≥900px (desktop) | Fixed sidebar (240px) + topbar + content area |
| 768–900px (tablet) | Collapsible sidebar via hamburger |
| 480–768px (mobile) | Full-width, stacked forms, scrollable tables |
| 360–480px (small mobile) | 1-column stat grid, compact padding |

- Tables: horizontal scroll inside `.table-container` wrapper — no body overflow
- Forms: `form-row` collapses to 1 column on mobile
- Modals: `max-height: 90vh` with scroll, full-width on small screens
- Sidebar: `transform: translateX(-100%)` on mobile, toggled via `.open` class + overlay

---

## 17. Security Review Results

### Authentication
- ✅ Access token: JS module-level memory only — never `localStorage`, never cookies
- ✅ Refresh token: httpOnly cookie `staff_refresh_token` scoped to `/api/admin/auth`
- ✅ Staff tokens have `audience="staff"` — player tokens are rejected by all admin routes
- ✅ `sessionStorage` stores only display_name, agent_id, role (not tokens)
- ✅ Logout clears access token from memory + revokes refresh token server-side

### RBAC
- ✅ Super Admin bypasses all DB permission checks (no leakage risk)
- ✅ Agents require explicit grants — no permissions by default
- ✅ All admin routers have `_check_perm()` / `_require_super_admin()` inline
- ✅ Financial operations (`deposit.approve`, `withdrawal.mark_paid`, `wallet.manual_adjust`, etc.) are in `ADMIN_ONLY_PERMISSIONS` — agents can never receive them
- ✅ Frontend `isSuper()` guards on deposits, withdrawals, audit logs, scheduler, settings (UI convenience — backend is authoritative)
- ✅ `.admin-only` CSS class hides nav items from agents — backend RBAC remains the real enforcement

### Secrets
- ✅ No JWT secret in frontend
- ✅ No Razorpay key secret in frontend (only public `key_id` via player deposit flow)
- ✅ No Supabase service role key in frontend
- ✅ No database password in frontend
- ✅ No password hashes returned by any API
- ✅ Settings UI has `HIDDEN_KEYS` filter — rejects any key containing `jwt_secret`, `db_password`, `razorpay_secret`, `supabase_key`, `service_role`

### Financial Safety
- ✅ No wallet credit from frontend
- ✅ No client-authoritative prize calculation — all amounts from backend
- ✅ Withdrawal hold accounting is atomic server-side
- ✅ UTR uniqueness enforced by DB partial unique index

### Room Credentials
- ✅ Never in static HTML
- ✅ Served only to active joined players after backend authorization
- ✅ Admin UI shows credentials only after release (from server response)

### Screenshots
- ✅ Private Supabase bucket — no public URLs
- ✅ Backend validates file type by magic bytes, not filename extension
- ✅ `storage_path` is an internal reference — no signed URL exposed (known gap)

---

## 18. Lightweight Verification Results

| Check | Result |
|---|---|
| Python AST syntax (98 files) | ✅ 0 errors |
| Phase 9 router imports (14 routers) | ✅ All OK |
| Schema imports (8 modules) | ✅ All OK |
| JS syntax — node --check (14 files) | ✅ 0 errors |
| HTML existence (15 files) | ✅ All present |
| Migration chain (0001→0007) | ✅ Sequential, no Phase 9 migration needed |
| Secret scan — 29 JS+HTML files | ✅ CLEAN |
| localStorage token usage | ✅ Only in comments, never used |
| sessionStorage token usage | ✅ Only display_name/agent_id/role |
| Inter-page link audit (hrefs + imports) | ✅ All resolve |
| Backend RBAC (all admin routers) | ✅ All have permission checks |
| `isSuper()` guards on sensitive modules | ✅ finance, audit, jobs all guarded |

---

## 19. Known Gaps / TODOs for Phase 10

1. **Screenshot signed URLs** — No endpoint to generate Supabase signed URLs for image preview. Admin currently sees storage_path only. A `GET /api/admin/tournaments/{id}/proof/{id}/signed-url` endpoint using `supabase_service_role_key` would close this gap.

2. **Agent permission grant/revoke UI** — `AGENT_GRANT_PERMISSIONS` and permission revoke endpoints don't exist yet. Agents currently receive permissions only at bootstrap or via direct DB. A `POST /api/admin/agents/{id}/permissions` endpoint would enable this.

3. **Prize config editor** — `GET /api/admin/prize-configs` and `GET /api/admin/prize-configs/{fee}` exist but no `POST/PUT` endpoints. Prize structures cannot be edited at runtime without DB access.

4. **Full pytest integration suite** — Explicitly deferred. Phase 10 should run the full test suite.

5. **Real-time updates** — All admin pages poll on demand or manual refresh. WebSocket/SSE integration is a future enhancement.

6. **Email/SMS notifications** — No email or SMS delivery. Notification system is in-app only.

7. **Razorpay payout API** — Withdrawal `mark-paid` is manual UTR entry. Automated Razorpay payout API integration is a future enhancement.

8. **Admin-to-admin notifications** — Staff notifications exist but no admin-to-admin messaging or escalation workflow.

---

## 20. Recommended Next Phase

**Phase 10: Testing, Hardening & Production Readiness**

1. Run full pytest suite (unit + integration)
2. Add screenshot signed URL endpoint (Supabase `create_signed_url`)
3. Add agent permission grant/revoke endpoints + UI
4. Add prize config editor endpoints + UI
5. E2E flow testing: registration → deposit → join tournament → result entry → withdrawal
6. Load testing on tournament join atomicity
7. Production deployment checklist: HTTPS, CORS origins, secure cookie flags, scheduler health monitoring
8. Penetration testing on auth token flow and financial endpoints
