# Phase 8 — Player Frontend

**AuraPlayFF · Free Fire Tournament Platform**
Completed: 2026-09-14

---

## Table of Contents

1. [Frontend Structure](#1-frontend-structure)
2. [Technology Stack](#2-technology-stack)
3. [Pages](#3-pages)
4. [API Integration](#4-api-integration)
5. [Authentication Flow](#5-authentication-flow)
6. [Tournament Flow](#6-tournament-flow)
7. [Wallet Flow](#7-wallet-flow)
8. [Deposit Flow](#8-deposit-flow)
9. [Withdrawal Flow](#9-withdrawal-flow)
10. [Referral Flow](#10-referral-flow)
11. [Screenshot Proof Flow](#11-screenshot-proof-flow)
12. [Notification Flow](#12-notification-flow)
13. [Leaderboard](#13-leaderboard)
14. [Navigation — Mobile & Desktop](#14-navigation--mobile--desktop)
15. [Configuration](#15-configuration)
16. [Security Model](#16-security-model)
17. [Error Handling](#17-error-handling)
18. [Responsive Design](#18-responsive-design)
19. [Verification Results](#19-verification-results)
20. [Known Limitations & Future Work](#20-known-limitations--future-work)

---

## 1. Frontend Structure

```
frontend/
├── index.html                  ← Public home page
├── login.html                  ← Player login
├── register.html               ← Player registration (supports ?ref= code)
├── tournaments.html            ← Browsable tournament list with filters
├── tournament-details.html     ← Single tournament: detail, join, room
├── my-matches.html             ← Player's joined tournaments
├── results.html                ← Tournament results + screenshot upload
├── wallet.html                 ← Wallet balances + transaction history
├── deposit.html                ← Online (Razorpay) + Manual UPI deposit
├── withdrawal.html             ← Withdrawal request + history
├── referral.html               ← Referral code + stats + list
├── leaderboard.html            ← Player and tournament leaderboards
├── profile.html                ← Player account info + stats
├── notifications.html          ← Notification list with mark-read
└── assets/
    ├── css/
    │   └── theme.css           ← Full design system (dark gaming theme)
    └── js/
        ├── config.js           ← API_BASE_URL, app constants (no secrets)
        ├── api.js              ← Centralised fetch client + token management
        ├── auth.js             ← register/login/logout/requireAuth/checkSession
        ├── ui.js               ← toast/modal/skeleton/format/pagination helpers
        ├── tournaments.js      ← All tournament + leaderboard + screenshot calls
        ├── wallet.js           ← Wallet/deposit/withdrawal/referral API calls
        ├── notifications.js    ← Notification API calls + badge polling
        └── layout.js           ← Sidebar/bottom-nav injection + logout wiring
```

---

## 2. Technology Stack

| Concern          | Choice                      | Reason                                              |
|------------------|-----------------------------|-----------------------------------------------------|
| HTML             | HTML5 semantic markup       | No framework overhead                               |
| CSS              | CSS3 custom properties      | Dark theme tokens, zero dependencies                |
| JavaScript       | Vanilla ES Modules (`type="module"`) | Native browser modules, no bundler needed |
| Font             | Inter (Google Fonts)        | Clean, readable at small sizes                      |
| Payments         | Razorpay Checkout (CDN)     | Loaded on demand — only public key sent to frontend |
| Icons/Emoji      | Unicode emoji               | Zero-dependency icon set                            |
| HTTP client      | `fetch` API                 | Native, no library needed                           |

No React, Vue, Angular, or build tools. Works directly when served from any static file server or the same origin as the FastAPI backend.

---

## 3. Pages

| Page                    | Auth  | API Endpoints Used                                                        |
|-------------------------|-------|---------------------------------------------------------------------------|
| `index.html`            | Optional | `GET /api/tournaments`, `GET /api/leaderboard/players`                 |
| `login.html`            | No    | `POST /api/auth/login`                                                    |
| `register.html`         | No    | `POST /api/auth/register`, `POST /api/auth/login`                         |
| `tournaments.html`      | Yes   | `GET /api/tournaments`                                                    |
| `tournament-details.html`| Yes  | `GET /api/tournaments/{id}`, `GET /api/tournaments/{id}/my-entry`, `POST /api/tournaments/{id}/join`, `GET /api/tournaments/{id}/room` |
| `my-matches.html`       | Yes   | `GET /api/tournaments`, `GET /api/tournaments/{id}/my-entry`, `GET /api/tournaments/{id}/room` |
| `results.html`          | Yes   | `GET /api/tournaments/{id}/results`, `GET /api/tournaments/{id}/my-result`, `POST /api/tournaments/{id}/proof`, `GET /api/tournaments/{id}/my-proof` |
| `wallet.html`           | Yes   | `GET /api/wallet/me`, `GET /api/wallet/me/transactions`                   |
| `deposit.html`          | Yes   | `POST /api/deposits/online`, `POST /api/deposits/online/{id}/verify`, `POST /api/deposits/manual`, `POST /api/deposits/manual/{id}/utr`, `GET /api/deposits/upi-info`, `GET /api/deposits` |
| `withdrawal.html`       | Yes   | `GET /api/withdrawals/balance-summary`, `POST /api/withdrawals`, `GET /api/withdrawals` |
| `referral.html`         | Yes   | `GET /api/wallet/me/referral`, `GET /api/wallet/me/referral/list`, `GET /api/wallet/me/referral/stats`, `GET /api/wallet/me` |
| `leaderboard.html`      | Yes   | `GET /api/leaderboard/players`, `GET /api/leaderboard/tournaments`        |
| `profile.html`          | Yes   | `GET /api/wallet/me`, `GET /api/wallet/me/referral`, `GET /api/leaderboard/players/{username}` |
| `notifications.html`    | Yes   | `GET /api/notifications`, `GET /api/notifications/unread-count`, `POST /api/notifications/{id}/read`, `POST /api/notifications/read-all` |

---

## 4. API Integration

### Centralised Client (`api.js`)

All HTTP calls go through `apiFetch()` in `api.js`. Pages never call `fetch()` directly.

```
apiFetch(path, options)
  → adds Authorization: Bearer <token> header
  → on 401: silently calls POST /api/auth/refresh (uses httpOnly cookie)
            retries original request once
            on second failure: clears token, redirects to /login.html
  → parses JSON responses
  → throws ApiError(status, code, message) on non-2xx
```

Convenience wrappers:
- `apiGet(path, params)` — GET with URLSearchParams
- `apiPost(path, body)` — POST with JSON body
- `apiPostForm(path, formData)` — POST multipart (screenshot upload)

### Token Handling

| Token         | Storage         | How it travels                        |
|---------------|-----------------|---------------------------------------|
| Access token  | JS memory only  | `Authorization: Bearer …` header      |
| Refresh token | httpOnly cookie | Sent automatically by browser to `/api/auth/*` |

- Access token is **never** written to `localStorage` or `sessionStorage`
- `sessionStorage` only stores `aura_username` (display name, non-sensitive, cleared on logout)
- On page reload the silent refresh endpoint restores the session automatically

---

## 5. Authentication Flow

### Login
```
User submits username + password
  → POST /api/auth/login
  → Response: { access_token }  +  Set-Cookie: refresh_token (httpOnly)
  → setAccessToken(token) in JS memory
  → sessionStorage.setItem('aura_username', username)  ← display only
  → redirect to /index.html (or ?next= param)
```

### Session Restore (on every page load)
```
requireAuth() / checkSession()
  → calls tryRestoreSession()
  → POST /api/auth/refresh  (browser auto-sends refresh cookie)
  → success: setAccessToken(new_token)
  → failure: redirect to /login.html?next=<current_page>
```

### Logout
```
Sidebar logout button or profile page
  → POST /api/auth/logout  (revokes refresh token server-side)
  → clearAccessToken()
  → sessionStorage.removeItem('aura_username')
  → redirect to /login.html
```

### Register + Auto-login
```
User fills form (username, ff_uid, password, confirm_password, referral_code?)
  → Client validates: username regex, ff_uid digits, password ≥ 8 chars, passwords match
  → POST /api/auth/register
  → On success: immediately calls login() → redirect to /index.html
  → referral_code pre-filled from ?ref= URL param (backend is authoritative)
```

### Password Reset
```
Admin issues a one-time reset token out-of-band
  → Player visits /login.html and uses reset form
  → POST /api/auth/reset-password { token, new_password, confirm_password }
  → All existing sessions invalidated server-side (token_version bump)
  → Player must log in again
```

---

## 6. Tournament Flow

### Browse
```
tournaments.html
  → GET /api/tournaments?status=open&limit=12&offset=…
  → Filter chips: open / confirmed / live / completed / all
  → Entry-fee filter chips: any / ₹10 / ₹20 / ₹30 / ₹50 / ₹100 (client-side)
  → Countdown timer rendered from tournament.scheduled_at (UTC)
  → already_joined field from backend drives Join/Joined button state
```

### Detail + Join
```
tournament-details.html?id=<uuid>
  → GET /api/tournaments/{id}          ← tournament detail
  → GET /api/tournaments/{id}/my-entry  ← existing entry (403 = not joined)
  → GET /api/wallet/me                  ← show available balance
  
JOIN FLOW:
  User clicks "Join" → confirm modal shows entry fee
  → POST /api/tournaments/{id}/join
  → Response: { sheet_number, entry_fee, deposit_used, referral_used, total_paid }
  → Success modal shows sheet number from backend (never generated client-side)
  → Page reloads tournament state
```

### Room Credentials
```
Available when tournament status = room_released or live
  → GET /api/tournaments/{id}/room
  → Guards enforced server-side:
      - Player must have ACTIVE entry
      - Room must be officially released (room_released_at set)
      - Tournament not cancelled
  → room_id and room_password shown with copy-to-clipboard buttons
  → Credentials never appear in page source before the API call
```

### My Matches
```
my-matches.html
  → GET /api/tournaments?status=all&limit=50
  → Client filters: items where already_joined=true
  → Local sort: live first, then by scheduled_at desc
  → Sheet number fetched per-tournament via GET /api/tournaments/{id}/my-entry
  → Inline room credential loader calls GET /api/tournaments/{id}/room
  → Status filter: all / upcoming / live / completed / cancelled
```

---

## 7. Wallet Flow

```
wallet.html
  → GET /api/wallet/me
  → Displays 4 balance types (all values from backend, never calculated client-side):
      deposit_balance       — deposited real money
      winning_balance       — prizes + kill rewards
      referral_balance      — non-withdrawable referral bonuses
      withdrawal_hold       — reserved for pending withdrawals

  → GET /api/wallet/me/transactions?limit=20&offset=…
  → Filterable by type (deposit, prize, entry_fee, referral_bonus, withdrawal, etc.)
  → Direction (credit/debit) indicated with colour-coded arrows
  → Shows balance_after snapshot per transaction
```

---

## 8. Deposit Flow

### Online (Razorpay)
```
deposit.html → Online tab
  → User enters amount
  → POST /api/deposits/online { amount }
  → Response: { razorpay_key_id (public), razorpay_order_id, amount, deposit_id }
  → Razorpay checkout.js loaded on demand from CDN
  → Razorpay checkout opened with public key only — KEY_SECRET never in frontend
  → On checkout success callback:
      POST /api/deposits/online/{deposit_id}/verify
          { razorpay_order_id, razorpay_payment_id, razorpay_signature }
      Server re-verifies HMAC — wallet credited only after server confirmation
  → Redirect to /wallet.html on success
```

### Manual UPI
```
deposit.html → Manual UPI tab
  Step 1: User enters amount
    → POST /api/deposits/manual { amount }
    → Response: { deposit: { id, amount, … }, upi_info: { upi_id, display_name } }
    → UPI ID comes from platform settings (never hardcoded)
    
  Step 2: User makes bank transfer, then submits UTR
    → POST /api/deposits/manual/{id}/utr { utr, payer_info? }
    → Admin reviews and approves — wallet NOT credited here
    
  Deposit history shown from GET /api/deposits (shows pending/approved/rejected)
```

---

## 9. Withdrawal Flow

```
withdrawal.html
  → GET /api/withdrawals/balance-summary
  → Shows available_winning_balance (= winning_balance − withdrawal_hold)
    NOT the raw winning_balance — prevents over-withdrawal
  
  User enters amount + UPI ID
  → Client validates: amount ≥ ₹100, valid UPI format, amount ≤ available
  → Confirm modal shows amount + UPI ID before submission
  → POST /api/withdrawals { amount, upi_id }
  → Backend immediately reserves funds in withdrawal_hold
  → Admin processes and marks paid manually
  
  History: GET /api/withdrawals — shows pending/processing/paid/rejected
  paid entries show payout_utr once admin marks paid
```

---

## 10. Referral Flow

```
referral.html
  → GET /api/wallet/me/referral    ← my code, referred-by status, stats
  → GET /api/wallet/me             ← referral_balance from wallet
  → GET /api/wallet/me/referral/list  ← paginated list of referred users

Referral code:
  - Displayed with copy-to-clipboard button
  - Referral link: /register.html?ref=<code>
  - Web Share API used when available

Stats shown (all from backend):
  - total_referrals_made
  - completed_referrals (played first tournament)
  - rewarded_referrals  (₹20 credited to both)
  - total_bonus_earned

Per-referral status:
  - pending  → friend registered, hasn't played yet
  - completed → friend played first tournament
  - rewarded  → ₹20 credited to referrer

register.html:
  - ?ref=CODE pre-fills the referral code field
  - Code is upper-cased and validated (8 uppercase alphanumeric)
  - Backend is authoritative for validity and reward activation
  - Reward is NOT manually triggered from frontend
```

---

## 11. Screenshot Proof Flow

```
results.html?id=<tournament_id>
  → GET /api/tournaments/{id}/my-proof
  → Response: { proofs: [...], uploads_remaining: 0–2 }
  
Upload zone shown only when:
  - Tournament status is completed or result_entry
  - uploads_remaining > 0
  - upload_deadline has not passed (from proof.upload_deadline field)

Upload:
  → User selects JPEG or PNG, max 5 MB
  → Client validates: file type, file size
  → POST /api/tournaments/{id}/proof  (multipart/form-data, field name: "file")
  → Backend enforces: 15-min upload window, max 2 files, JPEG/PNG only
  → Review status (pending/approved/rejected) shown from backend

After the 15-minute window:
  - Upload zone is hidden
  - Deadline notice shown
  - Backend enforces this — frontend cannot bypass it
```

---

## 12. Notification Flow

```
notifications.html
  → GET /api/notifications?limit=20&offset=…&unread_only=false/true
  → GET /api/notifications/unread-count  ← badge count
  → POST /api/notifications/{id}/read   ← mark one read (click on item)
  → POST /api/notifications/read-all    ← mark all read button

Badge polling:
  - startNotifPolling() called from initLayout() when logged in
  - Polls GET /api/notifications/unread-count every 60 seconds
  - Updates all [data-notif-badge] elements in sidebar + topbar + bottom nav

Deep linking from notification:
  - related_entity_type + related_entity_id drive navigation
  - tournament  → /tournament-details.html?id={id}
  - deposit     → /deposit.html
  - withdrawal  → /withdrawal.html
  - referral    → /referral.html
  - screenshot  → /results.html?id={id}

Notification types supported:
  account.*      tournament.*     deposit.*      withdrawal.*
  referral.*     agent.*          admin.*        scheduler.*
```

---

## 13. Leaderboard

```
leaderboard.html

Players tab:
  → GET /api/leaderboard/players?sort_by=wins|winnings|kills&limit=20&offset=…
  → Podium (top 3) shown with visual rank indicators for the first page
  → Full sortable table below the podium
  → Lifetime statistics — never reset on 24h cycle

Tournaments tab:
  → GET /api/leaderboard/tournaments?window=24h|all&limit=20&offset=…
  → 24h = rolling last 24 hours (server-side filter on completed_at)
  → all = all-time completed tournaments
  → Winner (1st place) shown per tournament from backend data
  → Historical data is never deleted

Both tabs support pagination.
```

---

## 14. Navigation — Mobile & Desktop

### Mobile (< 900px)
```
Top bar:
  ┌────────────────────────────────┐
  │ AuraPlayFF        🔔  💰      │  ← sticky topbar
  └────────────────────────────────┘

Bottom navigation (fixed, 64px height):
  ┌──────────────────────────────────────┐
  │  🏠      🏆      🎮      💰     🔔  │
  │ Home  Tournaments Matches Wallet Alerts │
  └──────────────────────────────────────┘
```

### Desktop (≥ 900px)
```
┌──────────────────────────────────────────────────────┐
│ AuraPlayFF          │  Main content area             │
│                     │                                │
│  🏠 Home            │                                │
│  🏆 Tournaments     │  (sidebar hidden on mobile,    │
│  🎮 My Matches      │   bottom nav hidden on desktop) │
│  💰 Wallet          │                                │
│  👤 Profile         │                                │
│ ─────────────────── │                                │
│  🔔 Notifications 3 │                                │
│  📊 Leaderboard     │                                │
│  🤝 Referral        │                                │
│  💳 Add Money       │                                │
│  💸 Withdraw        │                                │
│ ─────────────────── │                                │
│  👤 username        │                                │
│  🚪 Logout          │                                │
└──────────────────────────────────────────────────────┘
```

Both navs use `data-notif-badge` elements updated by the polling service.
Active page highlighted via `isActive()` path matching in `layout.js`.

---

## 15. Configuration

### `frontend/assets/js/config.js`

```javascript
const CONFIG = {
  API_BASE_URL:            'http://localhost:8000',  // ← change for production
  RAZORPAY_CHECKOUT_URL:   'https://checkout.razorpay.com/v1/checkout.js',
  NOTIFICATION_POLL_INTERVAL: 60_000,               // ms
  APP_NAME:                'AuraPlayFF',
};
```

**To point at a production backend:** change `API_BASE_URL` only. No other files need changing.

**What must NOT go in config.js:**
- JWT signing secret
- Supabase service-role key
- Razorpay KEY_SECRET
- Database password
- Any private credential

---

## 16. Security Model

### What the frontend never contains
| Prohibited Item              | Notes                                                  |
|------------------------------|--------------------------------------------------------|
| JWT signing secret           | Lives in backend `.env` only                           |
| Razorpay KEY_SECRET          | Only `key_id` (public) is sent to frontend             |
| Supabase service-role key    | Never exposed — backend uses it for storage operations |
| Database credentials         | Backend only                                           |
| Hardcoded access tokens      | Tokens kept in JS memory, never in DOM or storage      |

### Token security
- Access token: JS module-scope variable — not accessible outside the module
- Refresh token: httpOnly cookie, `samesite=lax`, scoped to `/api/auth` path only
- On 401: silent refresh attempted once, then redirect to login
- No token ever appears in `localStorage`

### Values that come exclusively from backend
- Wallet balances
- Entry fee amount
- Sheet numbers
- Room credentials
- Tournament status
- Kill reward amounts
- Prize amounts
- Referral reward amounts
- Upload deadlines

### sessionStorage usage
Only `aura_username` (display name) is stored — non-sensitive, cleared on logout.

### Input validation
Client-side validation is defence-in-depth only — the backend validates all inputs independently. Client-side errors give fast feedback; backend errors are the authoritative gate.

---

## 17. Error Handling

| HTTP Status | Frontend Behaviour                                          |
|-------------|-------------------------------------------------------------|
| 400         | Show inline error with `err.displayMessage`                 |
| 401         | Silent token refresh → retry → redirect to `/login.html`   |
| 403         | Show "not allowed" message; no detail leaked                |
| 404         | Show "not found" message                                    |
| 409         | Show conflict message (e.g. "already joined")               |
| 422         | Show field-level validation errors                          |
| 429         | Show rate-limit message with "please wait" guidance         |
| 500         | Show generic "server error, try again" message              |
| Network     | Show "check your connection" error                          |

Every API-driven section has three states:
- **Loading**: skeleton placeholders (never blank screens)
- **Empty**: friendly empty-state with icon, title, description, optional CTA
- **Error**: error message with retry button

---

## 18. Responsive Design

### Breakpoints
| Breakpoint | Behaviour                                              |
|------------|--------------------------------------------------------|
| < 360px    | Bottom nav hides labels; ultra-compact cards           |
| < 400px    | Single-column auth, referral code smaller              |
| < 480px    | Hero stacks vertically; single-column stats            |
| < 600px    | Reduced table padding                                  |
| < 900px    | Sidebar hidden; topbar + bottom nav visible            |
| ≥ 900px    | Sidebar visible (240px); topbar + bottom nav hidden    |

### Key responsive decisions
- **No horizontal scroll** on any standard mobile screen
- Tournament cards use `auto-fill minmax(280px, 1fr)` — self-wrap gracefully
- Room credentials stack vertically on mobile
- Balance grid: 2-column on mobile, 4-column on desktop
- Modals: full-screen slide-up on mobile (≤ 480px)
- Podium scales down proportionally on narrow screens
- All forms are single-column, full-width on mobile

---

## 19. Verification Results

| Check                         | Result                                    |
|-------------------------------|-------------------------------------------|
| HTML files present            | 14/14 ✅                                  |
| JS module files present       | 8/8 ✅                                    |
| CSS file present              | 1/1 ✅                                    |
| JS syntax (node --check)      | 0 errors ✅                               |
| HTML structure (doctype etc.) | 14/14 pass ✅                             |
| config.js before ES modules   | 14/14 pages ✅                            |
| API endpoint audit            | 35/35 match backend routes ✅             |
| Secret scan                   | CLEAN (1 false-positive in comment) ✅    |
| Inter-page href links         | All resolve ✅                            |
| theme.css linked everywhere   | 14/14 pages ✅                            |
| localStorage usage            | None ✅                                   |
| sessionStorage usage          | `aura_username` only (display, not sensitive) ✅ |

---

## 20. Known Limitations & Future Work

### No `/api/users/me` endpoint
The backend has no dedicated "get my own profile" endpoint. Profile data is assembled from:
- `GET /api/wallet/me` (balance info)
- `GET /api/wallet/me/referral` (referral code, referred-by)
- `GET /api/leaderboard/players/{username}` (tournament stats)

This requires the username to be known client-side (stored in `sessionStorage` at login). A future `/api/users/me` endpoint would cleanly solve this.

### Real-time updates
The current implementation polls notifications every 60 seconds. For live tournament updates (room released, result entered) a future WebSocket or Server-Sent Events implementation would provide instant feedback without polling.

### External Email/SMS
Not implemented. In-app notifications are sufficient for Phase 8. External delivery can be added in a later phase without changing the frontend notification system.

### Agent/Admin frontend
Not built in Phase 8. Agent and Admin interfaces are separate Phase 9+ work.

### Service worker / PWA
Not implemented. Adding a service worker for offline caching and installability is a straightforward future enhancement.

### Image proxy for screenshots
Screenshot proofs are stored in private Supabase Storage. The current implementation shows placeholder thumbnails after upload. A server-side signed URL endpoint would enable previewing uploaded screenshots in the admin review flow.
