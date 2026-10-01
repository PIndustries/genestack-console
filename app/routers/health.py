"""Health and readiness endpoints."""

from __future__ import annotations

import os
import time

from fastapi import APIRouter, HTTPException
from sqlalchemy import text

from app import __build__, __version__
from app.config import get_settings
from app.schemas import HealthResponse

router = APIRouter(tags=["health"])


def _uptime_seconds() -> float:
    from app.main import _startup_time

    if _startup_time is None:
        return 0
    return round(time.time() - _startup_time, 1)


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    settings = get_settings()
    return HealthResponse(
        status="ok",
        version=__version__,
        build=__build__,
        dry_run=settings.dry_run,
        genestack_root=settings.genestack_root,
        ansible_root=settings.ansible_root,
        uptime_seconds=_uptime_seconds(),
    )


@router.get("/health/ready")
def health_ready():
    """Readiness probe: returns 200 when the API is ready to serve requests."""
    from app.db import SessionLocal

    try:
        db = SessionLocal()
        db.execute(text("SELECT 1"))
        db.close()
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Database not ready: {e}")
    return {
        "status": "ready",
        "database": "ok",
        "uptime_seconds": _uptime_seconds(),
    }


@router.get("/health/live")
def health_live():
    """Liveness probe: always returns 200 for container orchestration."""
    return {
        "status": "alive",
        "pid": os.getpid(),
        "uptime_seconds": _uptime_seconds(),
    }


@router.get("/health/startup")
def health_startup():
    """Startup probe: returns 503 during initialization, 200 once started.

    Used by kubernetes startupProbe to give the container time to
    initialize before switching to readiness probes.
    """
    from app.main import _startup_time

    if _startup_time is None:
        raise HTTPException(
            status_code=503,
            detail="Startup in progress",
        )
    return {
        "status": "started",
        "uptime_seconds": _uptime_seconds(),
    }
