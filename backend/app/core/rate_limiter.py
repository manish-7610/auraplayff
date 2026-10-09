"""
Rate limiting via slowapi (wraps limits library).

Current implementation uses in-memory storage — suitable for a single-process
deployment. The storage backend is isolated here so it can be swapped to Redis
(RedisStorage from limits library) without changing any route code.

Usage on a route:
    from app.core.rate_limiter import limiter
    from slowapi.errors import RateLimitExceeded

    @router.post("/login")
    @limiter.limit("5/minute")
    async def login(request: Request, ...):
        ...

The SlowAPI middleware must be added to the FastAPI app (done in main.py).
"""
from __future__ import annotations

from slowapi import Limiter
from slowapi.util import get_remote_address

# Key function: use real client IP (works behind proxy via X-Forwarded-For)
def _get_client_ip(request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client:
        return request.client.host
    return "unknown"


import os as _os

# The single shared limiter instance used across all routers
limiter = Limiter(
    key_func=_get_client_ip,
    default_limits=[],
    storage_uri="memory://",
    enabled=_os.environ.get("APP_ENV") != "testing",  # disabled in tests
)
