"""Bounded table growth: retention sweeps for the non-snapshot tables.

Snapshot pruning lives in collector.prune_old_snapshots; this sweep covers
everything else that otherwise grows forever: finished jobs (log_text goes
with the row), audit logs, expired session tokens, agent command relay rows,
resolved alert events, and per-env config version history (newest N kept).

Called daily-ish from the worker daemon loop (app.worker.runner.run_daemon);
each sweep is one transaction and returns per-table delete counts.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import (
    AgentCommand,
    AlertEvent,
    AuditLog,
    EnvConfigVersion,
    Job,
    JobStatus,
    SessionToken,
)


def _naive_utcnow() -> datetime:
    """SQLite stores naive datetimes; compare against a naive UTC cutoff."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def run_retention_sweep(db: Session, settings: Settings) -> dict[str, int]:
    """Delete rows past their retention; returns per-table delete counts."""
    now = _naive_utcnow()
    counts: dict[str, int] = {}

    # Finished jobs older than the retention window (log_text included).
    cutoff = now - timedelta(days=settings.retention_jobs_days)
    counts["jobs"] = db.execute(
        delete(Job).where(
            Job.status.in_([JobStatus.success, JobStatus.failed]),
            Job.finished_at < cutoff,
        )
    ).rowcount

    # Audit trail: 90 days by default.
    cutoff = now - timedelta(days=settings.retention_audit_days)
    counts["audit_logs"] = db.execute(
        delete(AuditLog).where(AuditLog.timestamp < cutoff)
    ).rowcount

    # Session tokens: expiry is the retention rule.
    counts["session_tokens"] = db.execute(
        delete(SessionToken).where(SessionToken.expires_at < now)
    ).rowcount

    # Agent command relay rows: short-lived plumbing, 7 days by default.
    cutoff = now - timedelta(days=settings.retention_agent_commands_days)
    counts["agent_commands"] = db.execute(
        delete(AgentCommand).where(AgentCommand.created_at < cutoff)
    ).rowcount

    # Resolved alert episodes only — firing alerts are never swept.
    cutoff = now - timedelta(days=settings.retention_alert_events_days)
    counts["alert_events"] = db.execute(
        delete(AlertEvent).where(
            AlertEvent.status == "resolved",
            AlertEvent.resolved_at < cutoff,
        )
    ).rowcount

    # Config versions: keep the newest N per environment regardless of age.
    keep = settings.retention_env_config_versions_keep
    deleted = 0
    env_ids = db.scalars(select(EnvConfigVersion.environment_id).distinct()).all()
    for env_id in env_ids:
        keep_ids = (
            select(EnvConfigVersion.id)
            .where(EnvConfigVersion.environment_id == env_id)
            .order_by(EnvConfigVersion.version.desc())
            .limit(keep)
        )
        deleted += db.execute(
            delete(EnvConfigVersion).where(
                EnvConfigVersion.environment_id == env_id,
                EnvConfigVersion.id.not_in(keep_ids),
            )
        ).rowcount
    counts["env_config_versions"] = deleted

    db.commit()
    return counts
