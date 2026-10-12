# AuraPlayFF — Backend

Free Fire solo tournament platform backend. Built with FastAPI + async SQLAlchemy + PostgreSQL (Supabase).

---

## Architecture

```
FastAPI (async) → SQLAlchemy 2.x (asyncpg) → PostgreSQL (Supabase)
                                           → Supabase Storage (screenshots, Phase 2+)
```

- **Dual-token auth**: short-lived JWT access tokens (15 min) + httpOnly refresh cookies (7 days)
- **Audience separation**: player tokens (`aud: player`) and staff tokens (`aud: staff`) are cryptographically separate
- **RBAC**: Super Admin has all permissions; Agents get explicit DB grants only
- **Token versioning**: `token_version` in every JWT + DB check on every request; password reset and security events invalidate all sessions instantly
- **Immutable ledger**: `wallet_transactions` is INSERT-only; no UPDATE/DELETE ever

---

## Tech Stack

| Component | Version |
|---|---|
| Python | 3.11+ (tested on 3.14) |
| FastAPI | 0.111+ |
| SQLAlchemy | 2.0.x (async) |
| Alembic | 1.13+ |
| asyncpg | 0.29 (PostgreSQL async driver) |
| Pydantic | v2 |
| pydantic-settings | 2.x |
| bcrypt | 4.x (cost 12) |
| python-jose | 3.x (JWT) |
| passlib | 1.7 |
| slowapi | 0.1.9 (rate limiting) |

---

## Prerequisites

- Python 3.11 or higher
- A Supabase project with PostgreSQL enabled (or any PostgreSQL 14+ instance)
- `asyncpg` requires a C compiler on Windows — install [Microsoft C++ Build Tools](https://visualstudio.microsoft.com/visual-cpp-build-tools/) if not present

---

## Virtual Environment Setup

```bash
cd backend
python -m venv .venv

# Windows
.venv\Scripts\activate

# macOS/Linux
source .venv/bin/activate
```

---

## Dependency Installation

```bash
pip install -r requirements.txt
```

> **Note for Windows users:** `asyncpg` requires C++ Build Tools. If the build fails, install [Build Tools for Visual Studio](https://visualstudio.microsoft.com/visual-cpp-build-tools/) and retry.

---

## Environment Variables

Copy `.env.example` to `.env` and fill in every value:

```bash
cp .env.example .env
```

Required variables:

| Variable | Description |
|---|---|
| `APP_ENV` | `development` / `production` / `testing` |
| `APP_SECRET_KEY` | Random 32+ char secret (app-level) |
| `DATABASE_URL` | `postgresql+asyncpg://user:pass@host:port/db` |
| `JWT_SECRET_KEY` | Random 32+ char secret (separate from APP_SECRET_KEY) |
| `SUPER_ADMIN_AGENT_ID` | Login ID for the first Super Admin (e.g. `ADMIN-001`) |
| `SUPER_ADMIN_PASSWORD` | Strong password for the first Super Admin |
| `CORS_ALLOWED_ORIGINS` | Comma-separated allowed origins (e.g. `http://localhost:3000`) |

Optional (Phase 2+):

| Variable | Description |
|---|---|
| `SUPABASE_URL` | Your Supabase project URL |
| `SUPABASE_SERVICE_ROLE_KEY` | Supabase service role key (never expose to frontend) |
| `RAZORPAY_MODE` | `test` or `live` |
| `RAZORPAY_KEY_ID` | Razorpay key ID |
| `RAZORPAY_KEY_SECRET` | Razorpay key secret |
| `RAZORPAY_WEBHOOK_SECRET` | Razorpay webhook signing secret |

**Never commit `.env` to version control.** The `.gitignore` already excludes it.

---

## Supabase / PostgreSQL Setup

1. Create a [Supabase](https://supabase.com) project
2. In Supabase dashboard → Project Settings → Database → Connection string
3. Choose **Direct connection** (not pooler) for migrations
4. Copy the `postgresql://...` string and add `+asyncpg` after `postgresql`:
   ```
   postgresql+asyncpg://postgres.xxxx:PASSWORD@aws-0-ap-south-1.pooler.supabase.com:5432/postgres
   ```
5. Paste it as `DATABASE_URL` in your `.env`

---

## Alembic Database Migrations

### Run migrations (apply all pending)
```bash
cd backend
alembic upgrade head
```

### Check current migration state
```bash
alembic current
```

### Rollback one migration
```bash
alembic downgrade -1
```

### Generate a new migration (after model changes)
```bash
alembic revision --autogenerate -m "describe your change"
```

### View migration history
```bash
alembic history --verbose
```

> **Important:** Always run `alembic upgrade head` before starting the server for the first time on a fresh database.

---

## Super Admin Bootstrap

The first Super Admin is created automatically at startup from environment variables:

```env
SUPER_ADMIN_AGENT_ID=ADMIN-001
SUPER_ADMIN_PASSWORD=YourStrongPassword@123
```

**Rules:**
- Bootstrap only runs if no Super Admin exists in the database
- Runs on every app startup — safe to restart repeatedly (idempotent)
- The password is bcrypt-hashed (cost 12) before storage
- The plaintext password is never logged
- Change `SUPER_ADMIN_PASSWORD` in `.env` immediately after first login

---

## Run FastAPI Locally

```bash
cd backend
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

The server will:
1. Connect to the database
2. Seed all permission codes (idempotent)
3. Create Super Admin if none exists
4. Start accepting requests at `http://localhost:8000`

**API Documentation** (development only):
- Swagger UI: `http://localhost:8000/api/docs`
- ReDoc: `http://localhost:8000/api/redoc`
- OpenAPI JSON: `http://localhost:8000/api/openapi.json`

> Docs are disabled in production (`APP_ENV=production`).

---

## Health Check

```bash
curl http://localhost:8000/health
```

Expected response:
```json
{
  "status": "ok",
  "database": "connected",
  "version": "1.0.0"
}
```

If `database` is `"error"`, check your `DATABASE_URL` and network access.

---

## Run Tests

Tests use SQLite in-memory (via aiosqlite) — **no PostgreSQL required to run tests**.

```bash
cd backend
python -m pytest tests/ -v
```

Run only unit tests (faster, no DB):
```bash
python -m pytest tests/test_security_unit.py -v
```

Run with coverage:
```bash
python -m pytest tests/ --cov=app --cov-report=term-missing
```

**Note:** Integration tests are slow (~3 min) because bcrypt cost-12 hashing is run for each test's Super Admin bootstrap. This is intentional — tests use real security parameters.

---

## Project Structure

```
backend/
├── app/
│   ├── main.py              # FastAPI app factory + lifespan
│   ├── config.py            # Pydantic-settings (all from env vars)
│   ├── database.py          # Async SQLAlchemy engine + session factory
│   ├── dependencies.py      # get_db, get_current_user, get_current_staff, require_permission
│   ├── models/              # SQLAlchemy ORM models
│   ├── schemas/             # Pydantic v2 request/response schemas
│   ├── routers/             # FastAPI route handlers
│   ├── services/            # Business logic (audit, bootstrap)
│   └── core/                # Security, exceptions, middleware, RBAC, rate limiter
├── alembic/
│   ├── env.py               # Alembic async config (reads DATABASE_URL from env)
│   └── versions/
│       └── 0001_initial_schema.py
├── tests/
│   ├── conftest.py          # Fixtures (SQLite test DB, shared test context)
│   ├── test_player_auth.py
│   ├── test_staff_auth.py
│   ├── test_bootstrap.py
│   ├── test_password_reset.py
│   ├── test_security.py
│   └── test_security_unit.py
├── .env.example
├── requirements.txt
├── alembic.ini
└── pytest.ini
```

---

## API Endpoints (Phase 1)

| Method | Path | Description |
|---|---|---|
| GET | `/health` | Health + DB connectivity check |
| POST | `/api/auth/register` | Player registration |
| POST | `/api/auth/login` | Player login (returns access token + sets refresh cookie) |
| POST | `/api/auth/refresh` | Silent token refresh via cookie |
| POST | `/api/auth/logout` | Revoke refresh token |
| POST | `/api/auth/reset-password` | Consume one-time token, set new password |
| POST | `/api/admin/auth/login` | Staff login |
| POST | `/api/admin/auth/refresh` | Staff token refresh |
| POST | `/api/admin/auth/logout` | Staff logout |
| POST | `/api/admin/users/{id}/issue-password-reset` | Issue reset token for a player |

---

## Security Notes

- All secrets must be in `.env` — never in source code
- `JWT_SECRET_KEY` and `APP_SECRET_KEY` must be different values
- Both must be at least 32 random characters
- Password reset tokens are stored as SHA-256 hashes — not the raw token
- Refresh tokens are stored as SHA-256 hashes — not the raw token
- The Super Admin password is bcrypt-hashed at cost 12
- Token audience (`aud`) is cryptographically enforced — player tokens are rejected on staff routes and vice versa
- Rate limiting: 5 login attempts per minute per IP (disabled in testing)
- Security headers on every response: `X-Content-Type-Options`, `X-Frame-Options`, `CSP`, `HSTS`, `Referrer-Policy`

---

## Development Workflow

1. Make model changes in `app/models/`
2. Run `alembic revision --autogenerate -m "describe change"` to generate a migration
3. Review the generated migration file in `alembic/versions/`
4. Apply: `alembic upgrade head`
5. Write/update tests
6. Run: `python -m pytest tests/ -v`
7. Start server: `uvicorn app.main:app --reload`
