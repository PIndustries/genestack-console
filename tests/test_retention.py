"""Retention sweep: bounded growth for jobs/audit/sessions/agent cmd/alerts/versions."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
import yaml


def _ago(**kwargs) -> datetime:
    """Naive UTC (SQLite storage form) some time in the past."""
    return datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(**kwargs)


def _future(**kwargs) -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(**kwargs)


@pytest.fixture()
def db():
    from app.db import SessionLocal, init_db

    init_db()
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def settings():
    from app.config import get_settings

    return get_settings()


@pytest.fixture()
def env_id(db):
    from app.models import Environment

    env = Environment(name=f"retention-env-{uuid.uuid4().hex[:8]}")
    db.add(env)
    db.commit()
    return env.id


def _count(db, model) -> int:
    from sqlalchemy import func, select

    return db.scalar(select(func.count()).select_from(model))


def test_sweep_deletes_old_finished_jobs_keeps_recent_and_active(db, settings):
    from app.models import Job, JobStatus
    from app.services.retention import run_retention_sweep

    old = Job(
        operation="internal.health",
        status=JobStatus.success,
        log_text="old job log",
        finished_at=_ago(days=40),
    )
    recent = Job(
        operation="internal.health",
        status=JobStatus.failed,
        log_text="recent job log",
        finished_at=_ago(days=2),
    )
    active = Job(operation="internal.health", status=JobStatus.queued, log_text="")
    db.add_all([old, recent, active])
    db.commit()
    old_id, recent_id, active_id = old.id, recent.id, active.id

    counts = run_retention_sweep(db, settings)
    assert counts["jobs"] >= 1
    assert db.get(Job, old_id) is None  # log_text goes with the row
    assert db.get(Job, recent_id) is not None
    assert db.get(Job, active_id) is not None  # unfinished jobs are never swept


def test_sweep_deletes_old_audit_logs_keeps_recent(db, settings):
    from app.models import AuditLog
    from app.services.retention import run_retention_sweep

    old = AuditLog(actor="t", action="t.old", timestamp=_ago(days=100))
    recent = AuditLog(actor="t", action="t.recent", timestamp=_ago(days=10))
    db.add_all([old, recent])
    db.commit()
    old_id, recent_id = old.id, recent.id

    counts = run_retention_sweep(db, settings)
    assert counts["audit_logs"] >= 1
    assert db.get(AuditLog, old_id) is None
    assert db.get(AuditLog, recent_id) is not None


def test_sweep_deletes_expired_session_tokens_keeps_valid(db, settings):
    from app.models import SessionToken, User
    from app.services.retention import run_retention_sweep

    user = User(username=f"retention-user-{uuid.uuid4().hex[:8]}")
    db.add(user)
    db.flush()
    expired = SessionToken(
        token=f"exp-{uuid.uuid4().hex}", user_id=user.id, expires_at=_ago(hours=1)
    )
    valid = SessionToken(
        token=f"val-{uuid.uuid4().hex}", user_id=user.id, expires_at=_future(hours=1)
    )
    db.add_all([expired, valid])
    db.commit()
    expired_token, valid_token = expired.token, valid.token

    counts = run_retention_sweep(db, settings)
    assert counts["session_tokens"] >= 1
    assert db.get(SessionToken, expired_token) is None
    assert db.get(SessionToken, valid_token) is not None


def test_sweep_deletes_old_agent_commands_keeps_recent(db, settings, env_id):
    from app.models import AgentCommand
    from app.services.retention import run_retention_sweep

    old = AgentCommand(
        environment_id=env_id, kind="run_command", payload={}, created_at=_ago(days=10)
    )
    recent = AgentCommand(
        environment_id=env_id, kind="run_command", payload={}, created_at=_ago(days=1)
    )
    db.add_all([old, recent])
    db.commit()
    old_id, recent_id = old.id, recent.id

    counts = run_retention_sweep(db, settings)
    assert counts["agent_commands"] >= 1
    assert db.get(AgentCommand, old_id) is None
    assert db.get(AgentCommand, recent_id) is not None


def test_sweep_deletes_old_resolved_alerts_keeps_recent_and_firing(
    db, settings, env_id
):
    from app.models import AlertEvent, AlertRule
    from app.services.retention import run_retention_sweep

    rule = AlertRule(
        name=f"retention-rule-{uuid.uuid4().hex[:8]}", condition="node_not_ready"
    )
    db.add(rule)
    db.flush()
    old_resolved = AlertEvent(
        rule_id=rule.id,
        environment_id=env_id,
        status="resolved",
        fired_at=_ago(days=40),
        resolved_at=_ago(days=39),
    )
    recent_resolved = AlertEvent(
        rule_id=rule.id,
        environment_id=env_id,
        status="resolved",
        fired_at=_ago(days=2),
        resolved_at=_ago(days=1),
    )
    old_firing = AlertEvent(
        rule_id=rule.id,
        environment_id=env_id,
        status="firing",
        fired_at=_ago(days=60),
    )
    db.add_all([old_resolved, recent_resolved, old_firing])
    db.commit()
    ids = (old_resolved.id, recent_resolved.id, old_firing.id)

    counts = run_retention_sweep(db, settings)
    assert counts["alert_events"] >= 1
    assert db.get(AlertEvent, ids[0]) is None
    assert db.get(AlertEvent, ids[1]) is not None
    assert db.get(AlertEvent, ids[2]) is not None  # firing alerts are never swept


def test_sweep_keeps_latest_n_config_versions_per_env(db, settings, env_id):
    from sqlalchemy import select

    from app.models import EnvConfigVersion
    from app.services.retention import run_retention_sweep

    keep = settings.retention_env_config_versions_keep
    for version in range(1, keep + 6):  # keep + 5 versions
        db.add(
            EnvConfigVersion(
                environment_id=env_id,
                version=version,
                yaml_text=f"version: {version}",
            )
        )
    db.commit()

    counts = run_retention_sweep(db, settings)
    assert counts["env_config_versions"] == 5
    remaining = db.scalars(
        select(EnvConfigVersion.version)
        .where(EnvConfigVersion.environment_id == env_id)
        .order_by(EnvConfigVersion.version)
    ).all()
    assert len(remaining) == keep
    assert remaining == list(range(6, keep + 6))  # the NEWEST versions survive


def test_retention_config_knobs(tmp_path):
    """The retention: section overrides the defaults."""
    from app.config import load_settings

    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        yaml.safe_dump(
            {
                "data_dir": str(tmp_path / "data"),
                "retention": {
                    "jobs_days": 10,
                    "audit_days": 20,
                    "agent_commands_days": 3,
                    "alert_events_days": 5,
                    "env_config_versions_keep": 7,
                },
            }
        ),
        encoding="utf-8",
    )
    settings = load_settings(cfg)
    assert settings.retention_jobs_days == 10
    assert settings.retention_audit_days == 20
    assert settings.retention_agent_commands_days == 3
    assert settings.retention_alert_events_days == 5
    assert settings.retention_env_config_versions_keep == 7


def test_retention_config_defaults(tmp_path):
    from app.config import load_settings

    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        yaml.safe_dump({"data_dir": str(tmp_path / "data")}), encoding="utf-8"
    )
    settings = load_settings(cfg)
    assert settings.retention_jobs_days == 30
    assert settings.retention_audit_days == 90
    assert settings.retention_agent_commands_days == 7
    assert settings.retention_alert_events_days == 30
    assert settings.retention_env_config_versions_keep == 50


def test_daemon_first_tick_runs_sweep(monkeypatch, settings):
    """The worker daemon loop runs the retention sweep on its first tick."""
    from app.worker import runner as worker_runner

    calls: list[int] = []
    monkeypatch.setattr(worker_runner, "process_queued_jobs", lambda **kw: 0)
    monkeypatch.setattr(
        worker_runner,
        "run_retention_sweep_safe",
        lambda s: calls.append(1),
    )
    worker_runner.run_daemon(
        interval=0.01, limit=1, collector_enabled=False, max_ticks=1
    )
    assert calls, "retention sweep did not run on the first daemon tick"


def test_sweep_smoke_empty_tables(db, settings):
    from app.services.retention import run_retention_sweep

    counts = run_retention_sweep(db, settings)
    assert set(counts) == {
        "jobs",
        "audit_logs",
        "session_tokens",
        "agent_commands",
        "alert_events",
        "env_config_versions",
    }
