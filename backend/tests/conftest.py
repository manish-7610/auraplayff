"""
Test configuration and fixtures.

Uses SQLite file-based database via aiosqlite.
A single `test_context` fixture creates the engine, runs create_all,
bootstraps, and exposes a session factory — all tests share this
single fixture instance within a test function.
"""
from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from typing import AsyncGenerator

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

# ── Set test env vars BEFORE any app imports ──────────────────────────────────
os.environ["APP_ENV"] = "testing"
os.environ["APP_DEBUG"] = "false"
os.environ.setdefault("APP_SECRET_KEY", "test-secret-key-for-unit-tests-only-32chars!")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://placeholder:placeholder@localhost/placeholder")
os.environ.setdefault("JWT_SECRET_KEY", "test-jwt-secret-key-for-unit-tests-32char!")
os.environ.setdefault("SUPER_ADMIN_AGENT_ID", "TEST-ADMIN")
os.environ.setdefault("SUPER_ADMIN_PASSWORD", "TestAdmin@123456")
os.environ.setdefault("CORS_ALLOWED_ORIGINS", "http://localhost:3000")

from app.config import get_settings
get_settings.cache_clear()

from app.database import Base


@dataclass
class TestContext:
    factory: async_sessionmaker


@pytest_asyncio.fixture(scope="function")
async def test_context() -> AsyncGenerator[TestContext, None]:
    """
    Single fixture that:
    1. Creates a fresh temp SQLite file
    2. Runs create_all (schema creation)
    3. Bootstraps (seeds permissions + super admin)
    4. Returns a session factory
    5. Cleans up on teardown

    All other fixtures derive from this one, guaranteeing they
    all use the exact same database file.
    """
    from app.services.bootstrap_service import run_bootstrap

    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    url = f"sqlite+aiosqlite:///{tmp.name}"

    engine = create_async_engine(
        url,
        echo=False,
        connect_args={"check_same_thread": False},
    )

    # Create schema
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
        autocommit=False,
    )

    # Bootstrap (seed permissions + create super admin)
    async with factory() as db:
        await run_bootstrap(db)
        await db.commit()

    yield TestContext(factory=factory)

    await engine.dispose()
    try:
        os.unlink(tmp.name)
    except OSError:
        pass


@pytest_asyncio.fixture(scope="function")
async def db_session(test_context: TestContext) -> AsyncGenerator[AsyncSession, None]:
    """Async session for direct DB verification in tests."""
    async with test_context.factory() as session:
        yield session
        await session.rollback()


@pytest_asyncio.fixture(scope="function")
async def test_app(test_context: TestContext) -> FastAPI:
    """FastAPI app with DB dependency overridden to use the test database."""
    from app.database import get_db
    from app.main import create_app

    async def override_get_db():
        async with test_context.factory() as session:
            try:
                yield session
            except Exception:
                await session.rollback()
                raise
            finally:
                await session.close()

    settings = get_settings()
    app = create_app(settings)
    app.dependency_overrides[get_db] = override_get_db
    return app


@pytest_asyncio.fixture(scope="function")
async def client(test_app: FastAPI) -> AsyncGenerator[AsyncClient, None]:
    """HTTP test client."""
    async with AsyncClient(
        transport=ASGITransport(app=test_app),
        base_url="http://testserver",
    ) as ac:
        yield ac


# ── Shared helpers ────────────────────────────────────────────────────────────

async def register_player(client: AsyncClient, **overrides):
    data = {
        "username": "testplayer",
        "password": "SecurePass@123",
        "confirm_password": "SecurePass@123",
        "ff_uid": "1234567890",
        **overrides,
    }
    return await client.post("/api/auth/register", json=data)


async def login_player(
    client: AsyncClient,
    username: str = "testplayer",
    password: str = "SecurePass@123",
):
    return await client.post(
        "/api/auth/login",
        json={"username": username, "password": password},
    )


async def login_staff(
    client: AsyncClient,
    agent_id: str = "TEST-ADMIN",
    password: str = "TestAdmin@123456",
):
    return await client.post(
        "/api/admin/auth/login",
        json={"agent_id": agent_id, "password": password},
    )
