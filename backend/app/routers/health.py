"""
Health check endpoint.
GET /health — returns app status and DB connectivity.
No authentication required (used by load balancers and monitoring).
"""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from app.database import check_db_connection

router = APIRouter(tags=["Health"])


class HealthResponse(BaseModel):
    status: str          # "ok" | "degraded"
    database: str        # "connected" | "error"
    version: str


@router.get("/health", response_model=HealthResponse, summary="Application health check")
async def health_check() -> HealthResponse:
    db_ok = await check_db_connection()
    return HealthResponse(
        status="ok" if db_ok else "degraded",
        database="connected" if db_ok else "error",
        version="1.0.0",
    )
