"""Cluster state read endpoints: latest snapshot, history, live fleet rows."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.deps import get_db, get_env_scoped, require_viewer
from app.models import ClusterSnapshot, ConfigDrift, Environment, Membership
from app.schemas import ClusterSnapshotOut, Principal
from app.services.collector import DRIFTED_STATUSES

router = APIRouter(prefix="/api/v1", tags=["state"])

_HISTORY_MAX_LIMIT = 500


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@router.get("/environments/{environment_id}/state", response_model=ClusterSnapshotOut)
def get_environment_state(
    db: Session = Depends(get_db),
    env: Environment = Depends(get_env_scoped("viewer")),
) -> ClusterSnapshot:
    """Latest cluster snapshot for an environment; 404 until the first probe lands."""
    snap = db.scalar(
        select(ClusterSnapshot)
        .where(ClusterSnapshot.environment_id == env.id)
        .order_by(ClusterSnapshot.taken_at.desc())
        .limit(1)
    )
    if snap is None:
        raise HTTPException(status_code=404, detail="No snapshot recorded yet")
    return snap


@router.get(
    "/environments/{environment_id}/state/history",
    response_model=list[ClusterSnapshotOut],
)
def get_environment_state_history(
    db: Session = Depends(get_db),
    env: Environment = Depends(get_env_scoped("viewer")),
    limit: int = Query(50, ge=1, le=_HISTORY_MAX_LIMIT),
    hours: int = Query(24, ge=1),
) -> list[ClusterSnapshot]:
    """Snapshots from the last `hours` hours, newest first (limit capped at 500)."""
    cutoff = _utcnow() - timedelta(hours=hours)
    stmt = (
        select(ClusterSnapshot)
        .where(
            ClusterSnapshot.environment_id == env.id,
            ClusterSnapshot.taken_at >= cutoff,
        )
        .order_by(ClusterSnapshot.taken_at.desc())
        .limit(limit)
    )
    return list(db.scalars(stmt).all())


@router.get("/environments/{environment_id}/drift")
def get_environment_drift(
    db: Session = Depends(get_db),
    env: Environment = Depends(get_env_scoped("viewer")),
) -> dict[str, Any]:
    """Latest per-artifact config drift check for an environment.

    Empty ``artifacts`` with ``drifted: false`` until the collector's first
    drift check lands (no config doc, or no probe yet).
    """
    rows = list(
        db.scalars(
            select(ConfigDrift)
            .where(ConfigDrift.environment_id == env.id)
            .order_by(ConfigDrift.artifact)
        ).all()
    )
    return {
        "environment_id": env.id,
        "drifted": any(row.status in DRIFTED_STATUSES for row in rows),
        "checked_at": max((row.checked_at for row in rows), default=None),
        "artifacts": [
            {
                "artifact": row.artifact,
                "status": row.status,
                "expected_sha256": row.expected_sha256,
                "actual_sha256": row.actual_sha256,
                "detail": row.detail,
            }
            for row in rows
        ],
    }


@router.get("/fleet/live")
def get_fleet_live(
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_viewer),
) -> list[dict[str, Any]]:
    """One row per visible environment with its latest snapshot's health.

    Environments with no snapshot yet report health "unknown" and null
    snapshot fields.
    """
    env_stmt = select(Environment).order_by(Environment.name)
    if not principal.platform_admin:
        # Session users only see environments in tenants they belong to
        member_tenants = select(Membership.tenant_id).where(
            Membership.user_id == principal.user_id
        )
        env_stmt = env_stmt.where(Environment.tenant_id.in_(member_tenants))
    envs = list(db.scalars(env_stmt).all())

    # Latest snapshot per environment via a max(taken_at) subquery
    # (SQLAlchemy-2 style, SQLite-compatible).
    latest = (
        select(
            ClusterSnapshot.environment_id.label("environment_id"),
            func.max(ClusterSnapshot.taken_at).label("max_taken_at"),
        )
        .group_by(ClusterSnapshot.environment_id)
        .subquery()
    )
    snap_stmt = select(ClusterSnapshot).join(
        latest,
        (ClusterSnapshot.environment_id == latest.c.environment_id)
        & (ClusterSnapshot.taken_at == latest.c.max_taken_at),
    )
    snaps = {s.environment_id: s for s in db.scalars(snap_stmt).all()}

    # Aggregate drift state per environment (None = never checked)
    drift_rows = db.execute(
        select(ConfigDrift.environment_id, ConfigDrift.status).where(
            ConfigDrift.environment_id.in_([env.id for env in envs])
        )
    ).all()
    drift_by_env: dict[str, bool] = {}
    for env_id, status in drift_rows:
        drift_by_env[env_id] = (
            drift_by_env.get(env_id, False) or status in DRIFTED_STATUSES
        )

    rows: list[dict[str, Any]] = []
    for env in envs:
        snap = snaps.get(env.id)
        rows.append(
            {
                "environment_id": env.id,
                "name": env.name,
                "region": env.region,
                "tier": env.tier,
                "health": snap.health if snap else "unknown",
                "probe_ok": snap.probe_ok if snap else None,
                "error": snap.error if snap else None,
                "taken_at": snap.taken_at if snap else None,
                "summary": snap.summary if snap else None,
                "drifted": drift_by_env.get(env.id),
            }
        )
    return rows
