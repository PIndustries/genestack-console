"""Observe/Reports dashboard endpoints (viewer).

Live tiles plus stored metric series in the Console — not an iframe of
another product. Empty series is a 200, never an error.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.deps import get_db, get_env_scoped, require_viewer
from app.models import Environment
from app.schemas import Principal
from app.services.demo import canned_observe, canned_observe_logs, is_demo_env
from app.services.observe import observe_environment, observe_fleet, observe_logs

router = APIRouter(prefix="/api/v1", tags=["observe"])


@router.get("/environments/{environment_id}/observe")
def get_environment_observe(
    db: Session = Depends(get_db),
    env: Environment = Depends(get_env_scoped("viewer")),
    settings: Settings = Depends(get_settings),
    hours: int = Query(24, ge=1, le=168),
) -> dict[str, Any]:
    """Live tiles plus downsampled series for one environment.

    ``series`` is empty when no samples exist for the window.
    """
    if is_demo_env(env):
        return canned_observe(env, hours=hours)
    return observe_environment(db, env, settings, hours=hours)


@router.get("/environments/{environment_id}/observe/logs")
def get_environment_logs(
    db: Session = Depends(get_db),
    env: Environment = Depends(get_env_scoped("viewer")),
    settings: Settings = Depends(get_settings),
    query: str | None = Query(default=None),
    namespace: str | None = Query(default=None),
    pod: str | None = Query(default=None),
    since: str = Query(default="15m"),
    limit: int = Query(default=200, ge=1, le=2000),
) -> dict[str, Any]:
    """Native Loki log query. Viewer+. Never returns credentials."""
    if is_demo_env(env):
        return canned_observe_logs(
            query=query, namespace=namespace, pod=pod, since=since, limit=limit
        )
    return observe_logs(
        env,
        settings,
        query=query,
        namespace=namespace,
        pod=pod,
        since=since,
        limit=limit,
    )


@router.get("/fleet/observe")
def get_fleet_observe(
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_viewer),
    settings: Settings = Depends(get_settings),
    hours: int = Query(24, ge=1, le=168),
) -> dict[str, Any]:
    """Per-environment live tiles and optional series rollup.

    Scoped to tenants the caller can see.
    """
    return observe_fleet(db, principal, settings, hours=hours)
