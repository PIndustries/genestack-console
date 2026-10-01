"""Metrics chart endpoints: metric name discovery and downsampled series.

Data only exists when ``metrics.enabled: true`` is set in the console config
and the collector has scraped the environment. With metrics disabled or no
samples yet, these endpoints return 200 with empty results — never an error.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.config import get_settings
from app.deps import get_db, get_env_scoped
from app.models import Environment
from app.services.metrics import names_for_environment, series_for_environment

router = APIRouter(prefix="/api/v1", tags=["metrics"])


@router.get("/environments/{environment_id}/metrics/names")
def list_metric_names(
    db: Session = Depends(get_db),
    env: Environment = Depends(get_env_scoped("viewer")),
) -> list[dict[str, Any]]:
    """Distinct metric names with samples in the retention window.

    Each row: {name, samples, latest_ts}. Empty list when no data.
    """
    return names_for_environment(db, env.id, get_settings().metrics_retention_hours)


@router.get("/environments/{environment_id}/metrics/series")
def get_metric_series(
    db: Session = Depends(get_db),
    env: Environment = Depends(get_env_scoped("viewer")),
    name: str = Query(..., min_length=1, max_length=128),
    hours: int = Query(24, ge=1, le=168),
    bucket_minutes: int = Query(30, ge=5, le=720),
) -> dict[str, Any]:
    """Downsampled series for one metric, for charting.

    {name, hours, bucket_minutes, series: [{bucket_start_iso, avg, min, max,
    count}]}; series is empty when no data exists for the window.
    """
    return {
        "name": name,
        "hours": hours,
        "bucket_minutes": bucket_minutes,
        "series": series_for_environment(
            db, env.id, name, hours=hours, bucket_minutes=bucket_minutes
        ),
    }
