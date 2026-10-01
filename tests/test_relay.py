"""DB relay tests: boot high-water marks, snapshot/alert/job re-publication."""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.db import SessionLocal, init_db
from app.models import (
    AlertEvent,
    AlertRule,
    ClusterSnapshot,
    Environment,
    Job,
    JobStatus,
)
from app.services import events
from app.services import relay as relay_module
from app.services.relay import DBRelay

_TOPICS = ["fleet", "alerts", "jobs"]


@pytest.fixture(scope="module", autouse=True)
def _ensure_tables():
    """Create tables so the file also passes when run standalone."""
    init_db()


def _suffix() -> str:
    return uuid.uuid4().hex[:8]


def _make_env() -> Environment:
    db = SessionLocal()
    try:
        env = Environment(name=f"relay-env-{_suffix()}")
        db.add(env)
        db.commit()
        db.refresh(env)
        return env
    finally:
        db.close()


def _add_snapshot(env_id: str, health: str = "healthy") -> int:
    db = SessionLocal()
    try:
        snap = ClusterSnapshot(
            environment_id=env_id,
            probe_ok=True,
            nodes=[],
            pods=[],
            helm=[],
            summary={
                "nodes_ready": 1,
                "nodes_total": 1,
                "pods_running": 1,
                "pods_pending": 0,
                "pods_failed": 0,
                "crashlooping": [],
            },
            health=health,
        )
        db.add(snap)
        db.commit()
        return snap.id
    finally:
        db.close()


def _add_rule() -> int:
    db = SessionLocal()
    try:
        rule = AlertRule(
            name=f"relay-rule-{_suffix()}",
            condition="pod_crashloop",
            severity="critical",
        )
        db.add(rule)
        db.commit()
        return rule.id
    finally:
        db.close()


def _add_job(status: JobStatus = JobStatus.running) -> str:
    db = SessionLocal()
    try:
        job = Job(
            operation="internal.health",
            params={},
            status=status,
            log_text="",
            created_by="relay-test",
        )
        db.add(job)
        db.commit()
        return job.id
    finally:
        db.close()


def _set_job_status(job_id: str, status: JobStatus) -> None:
    db = SessionLocal()
    try:
        job = db.get(Job, job_id)
        job.status = status
        db.commit()
    finally:
        db.close()


def _drain(queue: asyncio.Queue) -> list[tuple[str, dict]]:
    items = []
    while True:
        try:
            items.append(queue.get_nowait())
        except asyncio.QueueEmpty:
            return items


async def _initialized_relay(queue: asyncio.Queue) -> DBRelay:
    relay = DBRelay(SessionLocal, interval=0.05)
    await relay.initialize()
    await relay.poll_once()
    assert _drain(queue) == []  # boot state must not publish
    return relay


def test_boot_state_produces_no_events():
    asyncio.run(_test_boot_state_produces_no_events())


async def _test_boot_state_produces_no_events():
    env = _make_env()
    _add_snapshot(env.id)
    rule_id = _add_rule()
    job_id = _add_job(JobStatus.success)
    db = SessionLocal()
    try:
        db.add(AlertEvent(rule_id=rule_id, environment_id=env.id, status="firing"))
        db.commit()
    finally:
        db.close()

    queue = events.subscribe(_TOPICS + [f"env:{env.id}"])
    try:
        relay = DBRelay(SessionLocal, interval=0.05)
        await relay.initialize()
        await relay.poll_once()
        await relay.poll_once()
        assert _drain(queue) == []
    finally:
        events.unsubscribe(queue)

    # Existing rows also left no spurious job events behind for this job.
    assert relay._job_status.get(job_id) == "success"


def test_new_snapshot_publishes_env_and_fleet_events():
    asyncio.run(_test_new_snapshot_publishes_env_and_fleet_events())


async def _test_new_snapshot_publishes_env_and_fleet_events():
    env = _make_env()
    queue = events.subscribe(_TOPICS + [f"env:{env.id}"])
    try:
        relay = await _initialized_relay(queue)

        snap_id = _add_snapshot(env.id, health="degraded")
        await relay.poll_once()
        published = _drain(queue)
    finally:
        events.unsubscribe(queue)

    env_events = [p for t, p in published if t == f"env:{env.id}"]
    fleet_events = [p for t, p in published if t == "fleet"]
    assert len(env_events) == 1
    assert len(fleet_events) == 1

    env_payload = env_events[0]
    assert env_payload["type"] == "snapshot"
    assert env_payload["environment_id"] == env.id
    assert env_payload["health"] == "degraded"
    assert env_payload["summary"]["nodes_total"] == 1
    assert env_payload["taken_at"]

    fleet_payload = fleet_events[0]
    assert fleet_payload == {
        "type": "fleet",
        "environment_id": env.id,
        "health": "degraded",
        "taken_at": env_payload["taken_at"],
    }
    assert relay._snapshot_hwm == snap_id

    # No further polls without new rows.
    queue2 = events.subscribe(_TOPICS + [f"env:{env.id}"])
    try:
        await relay.poll_once()
        assert _drain(queue2) == []
    finally:
        events.unsubscribe(queue2)


def test_alert_fired_then_resolved_relays_each_once():
    asyncio.run(_test_alert_fired_then_resolved_relays_each_once())


async def _test_alert_fired_then_resolved_relays_each_once():
    env = _make_env()
    rule_id = _add_rule()
    queue = events.subscribe(["alerts"])
    try:
        relay = await _initialized_relay(queue)

        db = SessionLocal()
        try:
            event = AlertEvent(
                rule_id=rule_id,
                environment_id=env.id,
                status="firing",
                details={"crashlooping": ["default/p1"]},
            )
            db.add(event)
            db.commit()
            event_id = event.id
        finally:
            db.close()

        await relay.poll_once()
        fired = [p for t, p in _drain(queue) if t == "alerts"]
        assert len(fired) == 1
        payload = fired[0]
        assert payload["type"] == "alert_fired"
        assert payload["event_id"] == event_id
        assert payload["rule_id"] == rule_id
        assert payload["rule_name"]
        assert payload["condition"] == "pod_crashloop"
        assert payload["severity"] == "critical"
        assert payload["environment_id"] == env.id
        assert payload["environment_name"] == env.name
        assert payload["fired_at"]
        assert payload["resolved_at"] is None
        assert payload["details"] == {"crashlooping": ["default/p1"]}

        # Steady state: repeated polls while still firing publish nothing.
        await relay.poll_once()
        assert _drain(queue) == []

        # Resolve the event: exactly one alert_resolved, then silence.
        db = SessionLocal()
        try:
            row = db.get(AlertEvent, event_id)
            row.status = "resolved"
            row.resolved_at = datetime.now(timezone.utc)
            db.commit()
        finally:
            db.close()

        await relay.poll_once()
        resolved = [p for t, p in _drain(queue) if t == "alerts"]
        assert len(resolved) == 1
        assert resolved[0]["type"] == "alert_resolved"
        assert resolved[0]["event_id"] == event_id
        assert resolved[0]["rule_name"] == payload["rule_name"]
        assert resolved[0]["resolved_at"]

        await relay.poll_once()
        assert _drain(queue) == []
        assert event_id not in relay._alert_status
    finally:
        events.unsubscribe(queue)


def test_job_status_transitions_relay_once_each():
    asyncio.run(_test_job_status_transitions_relay_once_each())


async def _test_job_status_transitions_relay_once_each():
    queue = events.subscribe(["jobs"])
    try:
        relay = await _initialized_relay(queue)

        job_id = _add_job(JobStatus.running)
        await relay.poll_once()
        running = [p for t, p in _drain(queue) if t == "jobs" and p["id"] == job_id]
        assert len(running) == 1
        assert running[0] == {
            "type": "job",
            "id": job_id,
            "environment_id": None,
            "operation": "internal.health",
            "status": "running",
            "dry_run": None,
        }

        # Repeated polls with no change publish nothing for this job.
        await relay.poll_once()
        assert _drain(queue) == []

        _set_job_status(job_id, JobStatus.success)
        await relay.poll_once()
        done = [p for t, p in _drain(queue) if t == "jobs" and p["id"] == job_id]
        assert len(done) == 1
        assert done[0]["status"] == "success"

        await relay.poll_once()
        assert _drain(queue) == []
    finally:
        events.unsubscribe(queue)


def test_job_log_growth_relays_without_status_change():
    asyncio.run(_test_job_log_growth_relays_without_status_change())


async def _test_job_log_growth_relays_without_status_change():
    queue = events.subscribe(["jobs"])
    try:
        relay = await _initialized_relay(queue)
        job_id = _add_job(JobStatus.running)
        await relay.poll_once()
        _drain(queue)

        db = SessionLocal()
        try:
            row = db.get(Job, job_id)
            row.log_text = "[t] $ bash bin/install-keystone.sh\nhelm running\n"
            db.commit()
        finally:
            db.close()

        await relay.poll_once()
        logs = [
            p for t, p in _drain(queue) if t == "jobs" and p.get("type") == "job_log"
        ]
        assert len(logs) == 1
        assert logs[0]["id"] == job_id
        assert "install-keystone.sh" in (logs[0].get("log_tail") or "")
        assert (logs[0].get("current") or {}).get("item") == "keystone"

        await relay.poll_once()
        assert [p for t, p in _drain(queue) if p.get("type") == "job_log"] == []
    finally:
        events.unsubscribe(queue)


# ---------------------------------------------------------------------------
# Bounded jobs scan
# ---------------------------------------------------------------------------


def _insert_job(
    status: JobStatus,
    *,
    created_at: datetime | None = None,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
) -> str:
    db = SessionLocal()
    try:
        job = Job(
            operation="internal.health",
            params={},
            status=status,
            log_text="",
            created_by="relay-test",
        )
        if created_at is not None:
            job.created_at = created_at
        job.started_at = started_at
        job.finished_at = finished_at
        db.add(job)
        db.commit()
        return job.id
    finally:
        db.close()


def test_jobs_poll_skips_old_untracked_jobs():
    asyncio.run(_test_jobs_poll_skips_old_untracked_jobs())


async def _test_jobs_poll_skips_old_untracked_jobs():
    queue = events.subscribe(["jobs"])
    try:
        relay = await _initialized_relay(queue)

        # A terminal job whose timestamps all predate the poll window is
        # neither discovered nor tracked — the scan stays bounded.
        old = datetime.now(timezone.utc) - timedelta(days=2)
        old_id = _insert_job(
            JobStatus.success, created_at=old, started_at=old, finished_at=old
        )
        await relay.poll_once()
        assert [p for t, p in _drain(queue) if p.get("id") == old_id] == []
        assert old_id not in relay._job_status

        # An old-created job that only recently started IS discovered via the
        # window, with the unchanged payload shape.
        late_id = _insert_job(
            JobStatus.running,
            created_at=old,
            started_at=datetime.now(timezone.utc),
        )
        await relay.poll_once()
        seen = [p for t, p in _drain(queue) if p.get("id") == late_id]
        assert seen == [
            {
                "type": "job",
                "id": late_id,
                "environment_id": None,
                "operation": "internal.health",
                "status": "running",
                "dry_run": None,
            }
        ]

        # Age every timestamp out of the window, then transition: the job is
        # still scanned because it is tracked, and the transition is relayed.
        db = SessionLocal()
        try:
            job = db.get(Job, late_id)
            job.status = JobStatus.success
            job.started_at = old
            job.finished_at = old
            db.commit()
        finally:
            db.close()
        await relay.poll_once()
        done = [p for t, p in _drain(queue) if p.get("id") == late_id]
        assert len(done) == 1
        assert done[0]["status"] == "success"

        await relay.poll_once()
        assert _drain(queue) == []
    finally:
        events.unsubscribe(queue)


def test_tracked_jobs_map_is_capped(monkeypatch):
    asyncio.run(_test_tracked_jobs_map_is_capped(monkeypatch))


async def _test_tracked_jobs_map_is_capped(monkeypatch):
    monkeypatch.setattr(relay_module, "_MAX_TRACKED_JOBS", 3)
    queue = events.subscribe(["jobs"])
    try:
        relay = DBRelay(SessionLocal, interval=0.05)
        await relay.initialize()
        # Explicit increasing created_at so scan/eviction order is deterministic.
        base = datetime.now(timezone.utc)
        ids = [
            _insert_job(JobStatus.running, created_at=base + timedelta(seconds=i))
            for i in range(5)
        ]
        await relay.poll_once()
        published = [p for t, p in _drain(queue) if t == "jobs"]
        assert [p["id"] for p in published if p["id"] in ids] == ids

        # Cap enforced: only the 3 most-recently-observed ids remain tracked.
        assert len(relay._job_status) == 3
        assert ids[0] not in relay._job_status
        assert ids[1] not in relay._job_status
        assert all(job_id in relay._job_status for job_id in ids[2:])
    finally:
        events.unsubscribe(queue)


def test_env_scoped_dry_run_job_payload_has_ui_fields():
    asyncio.run(_test_env_scoped_dry_run_job_payload_has_ui_fields())


async def _test_env_scoped_dry_run_job_payload_has_ui_fields():
    """The jobs-topic payload carries everything the UI renders from a job
    transition: id, status, environment_id (tenant filtering + env matching),
    operation, and dry_run (the rehearsal pill)."""
    env = _make_env()
    queue = events.subscribe(["jobs"])
    try:
        relay = await _initialized_relay(queue)

        db = SessionLocal()
        try:
            job = Job(
                environment_id=env.id,
                operation="genestack.verify",
                params={"level": "quick"},
                status=JobStatus.running,
                log_text="",
                created_by="relay-test",
                dry_run=True,
            )
            db.add(job)
            db.commit()
            job_id = job.id
        finally:
            db.close()

        await relay.poll_once()
        running = [p for t, p in _drain(queue) if t == "jobs" and p["id"] == job_id]
        assert running == [
            {
                "type": "job",
                "id": job_id,
                "environment_id": env.id,
                "operation": "genestack.verify",
                "status": "running",
                "dry_run": True,
            }
        ]

        # The terminal transition keeps the same fields.
        _set_job_status(job_id, JobStatus.success)
        await relay.poll_once()
        done = [p for t, p in _drain(queue) if t == "jobs" and p["id"] == job_id]
        assert len(done) == 1
        assert done[0]["status"] == "success"
        assert done[0]["environment_id"] == env.id
        assert done[0]["dry_run"] is True
    finally:
        events.unsubscribe(queue)
