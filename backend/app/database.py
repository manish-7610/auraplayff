"""
Async SQLAlchemy engine and session factory.
All database access must go through get_db() dependency.
"""
from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings


class Base(DeclarativeBase):
    """Base class for all ORM models."""
    pass


def _build_engine(url: str | None = None):
    settings = get_settings()
    db_url = url or settings.database_url
    return create_async_engine(
        db_url,
        echo=settings.app_debug,
        pool_pre_ping=True,           # detect stale connections
        pool_size=10,
        max_overflow=20,
        pool_recycle=3600,
    )


# Module-level engine and session factory — created lazily
_engine = None
_async_session_factory = None


def get_engine():
    global _engine
    if _engine is None:
        _engine = _build_engine()
    return _engine


def get_session_factory():
    global _async_session_factory
    if _async_session_factory is None:
        _async_session_factory = async_sessionmaker(
            bind=get_engine(),
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
            autocommit=False,
        )
    return _async_session_factory


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency: yields an async database session."""
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def check_db_connection() -> bool:
    """Used by the health endpoint to verify DB connectivity."""
    from sqlalchemy import text
    try:
        async with get_engine().connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
