"""
Shared column helpers for cross-dialect compatibility.
Models use these to work with both PostgreSQL (production) and SQLite (tests).
"""
from __future__ import annotations

from datetime import datetime, timezone


def utcnow() -> datetime:
    """Python-side default for timestamp columns — works on SQLite and PostgreSQL."""
    return datetime.now(timezone.utc)
