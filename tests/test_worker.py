"""Worker, per-op timeout, queued-by-default, and stale-recovery tests."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture(autouse=True, scope="module")
def _ensure_tables():
    """Create tables so the file also passes when run standalone."""
    from app.db import init_db

    init_db()


def _create_env(client, headers, prefix: str = "worker-env") -> str:
    name = f"{prefix}-{uuid.uuid4().hex[:8]}"
    resp = client.post(
        "/api/v1/environments",
        headers=headers,
        json={"name": name, "description": f"{prefix} test"},
    )
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["id"]


def _seed_job(
    *,
    operation,
    status,
    environment_id=None,
    params=None,
    started_at=None,
    created_at=None,
):
    from app.db import SessionLocal
    from app.models import Job, JobStatus

    db = SessionLocal()
    try:
        kwargs = dict(
            environment_id=environment_id,
            operation=operation,
            params=params or {},
            status=JobStatus(status),
            log_text="",
            created_by="worker-test",
            started_at=started_at,
        )
        if created_at is not None:
            kwargs["created_at"] = created_at
        job = Job(**kwargs)
        db.add(job)
        db.commit()
        return job.id
    finally:
        db.close()


def _get_job_row(job_id: str):
    from app.db import SessionLocal
    from app.models import Job

    db = SessionLocal()
    try:
        job = db.get(Job, job_id)
        db.expunge(job)
        return job
    finally:
        db.close()


@pytest.fixture(autouse=True, scope="module")
def _neutralize_stale_queued():
    """Mark pre-existing queued jobs failed so worker tests see a clean queue.

    ``process_queued_jobs(once=True)`` drains only the oldest ``limit`` queued
    jobs, and the shared session DB accumulates stale queued rows from sibling
    test modules (alphabetically earlier). Without this, a worker test's own job
    can fall outside the drained batch (or a drain pass could execute the whole
    backlog). Neutralizing them makes each test's job the only candidate, so a
    single pass reaches it through the real claim path.
    """
    from app.db import SessionLocal, init_db
    from app.models import Job, JobStatus
    from sqlalchemy import select

    init_db()  # idempotent; makes this fixture order-independent
    db = SessionLocal()
    try:
        for job in db.scalars(select(Job).where(Job.status == JobStatus.queued)):
            job.status = JobStatus.failed
            job.error = "neutralized by worker test fixture"
        db.commit()
    finally:
        db.close()
    yield


# ----------------------------------------------------------- catalog timeouts


def test_catalog_long_ops_have_timeouts():
    from app.services.catalog import get_operation

    assert get_operation("genestack.pipeline.run").timeout_seconds == 14400
    assert get_operation("genestack.service.enable").timeout_seconds == 3600
    assert get_operation("genestack.host_setup").timeout_seconds == 3600
    assert get_operation("ansible.playbook.run").timeout_seconds == 3600
    # Read ops stay on the global default
    assert get_operation("internal.health").timeout_seconds is None
    assert get_operation("genestack.scripts.list").timeout_seconds is None


def _capture_run_command(monkeypatch):
    from app.services import genestack_bridge as bridge

    captured: list[dict] = []

    def fake_run_command(cmd, **kwargs):
        captured.append(kwargs)
        return {"ok": True, "returncode": 0, "dry_run": True, "message": "fake"}

    monkeypatch.setattr(bridge, "run_command", fake_run_command)
    return captured


def test_dispatch_uses_op_timeout(monkeypatch):
    """genestack.pipeline.run dispatches run_command with its 4h timeout."""
    from app.db import SessionLocal
    from app.services.job_runner import execute_operation

    captured = _capture_run_command(monkeypatch)
    db = SessionLocal()
    try:
        job = execute_operation(
            db,
            operation="genestack.pipeline.run",
            params={"stage": "hosts"},
            run_sync=True,
        )
        assert job.status.value == "success", job.error
    finally:
        db.close()
    assert captured, "pipeline ran no commands"
    assert all(c["timeout"] == 14400 for c in captured)


def test_dispatch_timeout_clamped_to_max(monkeypatch):
    """An op timeout above the 6h ceiling is clamped to MAX_JOB_TIMEOUT_SECONDS."""
    from app.db import SessionLocal
    from app.services import job_runner as jr
    from app.services.catalog import get_operation

    captured = _capture_run_command(monkeypatch)
    real_op = get_operation("genestack.pipeline.run")
    inflated = real_op.model_copy(update={"timeout_seconds": 999999})
    monkeypatch.setattr(jr, "get_operation", lambda _id: inflated)

    db = SessionLocal()
    try:
        job = jr.execute_operation(
            db,
            operation="genestack.pipeline.run",
            params={"stage": "hosts"},
            run_sync=True,
        )
        assert job.status.value == "success", job.error
    finally:
        db.close()
    assert captured
    assert all(c["timeout"] == jr.MAX_JOB_TIMEOUT_SECONDS == 21600 for c in captured)


def test_pipeline_deadline_stops_mid_loop(monkeypatch):
    """Per-job deadline: the first item burns the budget; item 2 never starts."""
    import time as time_mod

    from app.db import SessionLocal
    from app.services import genestack_bridge as bridge
    from app.services import job_runner as jr
    from app.services.catalog import get_operation

    real_monotonic = time_mod.monotonic
    offset = [0.0]
    monkeypatch.setattr(time_mod, "monotonic", lambda: real_monotonic() + offset[0])

    captured: list[dict] = []

    def fake_run_command(cmd, **kwargs):  # noqa: ARG001
        captured.append(kwargs)
        offset[0] += 120.0  # each item burns 2 minutes of the 60s job budget
        return {"ok": True, "returncode": 0, "dry_run": True, "message": "fake"}

    monkeypatch.setattr(bridge, "run_command", fake_run_command)

    real_op = get_operation("genestack.pipeline.run")
    tight = real_op.model_copy(update={"timeout_seconds": 60})
    monkeypatch.setattr(jr, "get_operation", lambda _id: tight)

    db = SessionLocal()
    try:
        job = jr.execute_operation(
            db,
            operation="genestack.pipeline.run",
            params={"stage": "core"},  # 3 items; deadline passes after item 1
            run_sync=True,
        )
        assert job.status.value == "failed"
        assert "deadline" in (job.error or "")
    finally:
        db.close()
    assert len(captured) == 1, f"deadline must stop the loop, ran {len(captured)} items"
    # The one command that ran got the remaining budget, not the full timeout.
    assert captured[0]["timeout"] <= 60


# -------------------------------------------------------- queued by default


def test_default_job_creation_is_queued(client, admin_headers):
    """POST without run_sync leaves the job queued; nothing runs inline."""
    env_id = _create_env(client, admin_headers, "default-queued")
    resp = client.post(
        f"/api/v1/environments/{env_id}/jobs",
        headers=admin_headers,
        json={"operation": "host.preflight", "params": {}},
    )
    assert resp.status_code == 201, resp.text
    job = resp.json()
    assert job["status"] == "queued", job
    assert job["started_at"] is None

    polled = client.get(f"/api/v1/jobs/{job['id']}", headers=admin_headers)
    assert polled.status_code == 200, polled.text
    assert polled.json()["status"] == "queued"


def test_explicit_run_sync_still_executes_inline(client, admin_headers):
    resp = client.post(
        "/api/v1/jobs",
        headers=admin_headers,
        json={"operation": "internal.health", "params": {}, "run_sync": True},
    )
    assert resp.status_code in (200, 201), resp.text
    assert resp.json()["status"] == "success"


# ------------------------------------------------------------------- worker


def test_worker_claims_queued_job_and_completes(client, admin_headers):
    from app.worker.runner import process_queued_jobs

    resp = client.post(
        "/api/v1/jobs",
        headers=admin_headers,
        json={"operation": "internal.health", "params": {}, "run_sync": False},
    )
    assert resp.status_code in (200, 201), resp.text
    job_id = resp.json()["id"]
    assert resp.json()["status"] == "queued"

    processed = process_queued_jobs(once=True)
    assert processed >= 1
    assert _get_job_row(job_id).status.value == "success"


def test_claim_is_atomic_no_double_run():
    """Two workers claiming the same queued job: exactly one wins."""
    from app.db import SessionLocal
    from app.worker.runner import claim_job

    job_id = _seed_job(operation="internal.health", status="queued")

    db1 = SessionLocal()
    db2 = SessionLocal()
    try:
        first = claim_job(db1, job_id)
        second = claim_job(db2, job_id)
        assert (first is None) != (second is None), "exactly one claim must succeed"
    finally:
        db1.close()
        db2.close()

    # tidy up: don't leave a running job behind
    from app.models import Job, JobStatus

    db = SessionLocal()
    try:
        job = db.get(Job, job_id)
        job.status = JobStatus.failed
        db.commit()
    finally:
        db.close()


def test_worker_skips_mutating_job_when_env_has_running_mutating(client, admin_headers):
    """Queued mutating job stays queued while its env has a running mutating job."""
    from app.db import SessionLocal
    from app.models import Job, JobStatus
    from app.worker.runner import process_queued_jobs

    env_id = _create_env(client, admin_headers, "lock-skip")
    resp = client.post(
        f"/api/v1/environments/{env_id}/jobs",
        headers=admin_headers,
        json={
            "operation": "genestack.service.enable",
            "params": {"service": "keystone"},
            "run_sync": False,
        },
    )
    assert resp.status_code == 201, resp.text
    queued_id = resp.json()["id"]

    running_id = _seed_job(
        operation="genestack.pipeline.run",
        status=JobStatus.running,
        environment_id=env_id,
        params={"stage": "core"},
    )

    process_queued_jobs(once=True, abandon_running=False)
    assert _get_job_row(queued_id).status.value == "queued"

    # Running job finishes; next pass picks the queued one up.
    db = SessionLocal()
    try:
        db.get(Job, running_id).status = JobStatus.success
        db.commit()
    finally:
        db.close()

    process_queued_jobs(once=True, abandon_running=False)
    assert _get_job_row(queued_id).status.value == "success"


# ----------------------------------------------------------- stale recovery


def test_stale_recovery_marks_old_running_job_failed(app):
    """API startup (lifespan) fails jobs stuck running past their timeout."""
    from fastapi.testclient import TestClient

    from app.services.job_runner import RECOVERY_ERROR

    old = datetime.now(timezone.utc) - timedelta(hours=7)
    job_id = _seed_job(
        operation="genestack.pipeline.run",  # 4h op timeout < 7h age
        status="running",
        started_at=old,
    )

    with TestClient(app):
        pass  # entering the context runs startup, incl. recover_stale_jobs

    job = _get_job_row(job_id)
    assert job.status.value == "failed"
    assert job.error == RECOVERY_ERROR
    assert job.finished_at is not None


def test_recover_stale_jobs_leaves_fresh_jobs_alone():
    from app.db import SessionLocal
    from app.services.job_runner import recover_stale_jobs

    fresh_id = _seed_job(operation="internal.health", status="queued")
    db = SessionLocal()
    try:
        assert recover_stale_jobs(db) == 0
    finally:
        db.close()
    assert _get_job_row(fresh_id).status.value == "queued"


def test_recover_stale_jobs_does_not_expire_old_queued():
    """Queued jobs older than the op timeout stay claimable (wait age ignored)."""
    from app.db import SessionLocal
    from app.models import JobStatus
    from app.services import events
    from app.services.job_runner import recover_stale_jobs
    from app.worker.runner import claim_job

    old = datetime.now(timezone.utc) - timedelta(hours=7)
    job_id = _seed_job(
        # Wait age >> settings.job_timeout_seconds and the 6h ceiling.
        operation="internal.health",
        status="queued",
        created_at=old,
    )
    db = SessionLocal()
    try:
        assert recover_stale_jobs(db) == 0
    finally:
        db.close()
    assert _get_job_row(job_id).status.value == "queued"

    # Still claimable (queued -> running). Clear a closed TestClient loop so
    # claim_job's publish_sync is a no-op rather than raising.
    events._loop = None
    db = SessionLocal()
    try:
        claimed = claim_job(db, job_id)
        assert claimed is not None
        assert claimed.status == JobStatus.running
        claimed.status = JobStatus.failed
        claimed.error = "tidy after claimability check"
        db.commit()
    finally:
        db.close()


def test_recover_stale_jobs_uses_settings_timeout_not_max_ceiling():
    """Ops without catalog timeout use settings.job_timeout_seconds (not 6h)."""
    from app.db import SessionLocal
    from app.config import get_settings
    from app.services.job_runner import RECOVERY_ERROR, recover_stale_jobs

    settings = get_settings()
    # Age past the default 600s settings timeout but well under the 6h ceiling.
    old = datetime.now(timezone.utc) - timedelta(seconds=settings.job_timeout_seconds + 60)
    job_id = _seed_job(
        operation="internal.health",  # no catalog timeout_seconds
        status="running",
        started_at=old,
    )
    db = SessionLocal()
    try:
        assert recover_stale_jobs(db) == 1
    finally:
        db.close()
    job = _get_job_row(job_id)
    assert job.status.value == "failed"
    assert job.error == RECOVERY_ERROR


def test_recover_stale_jobs_ignores_running_without_started_at():
    """Running rows missing started_at are not expired via created_at fallback."""
    from app.db import SessionLocal
    from app.services.job_runner import recover_stale_jobs

    old = datetime.now(timezone.utc) - timedelta(hours=7)
    job_id = _seed_job(
        operation="genestack.pipeline.run",
        status="running",
        started_at=None,
        created_at=old,
    )
    db = SessionLocal()
    try:
        assert recover_stale_jobs(db) == 0
    finally:
        db.close()
    assert _get_job_row(job_id).status.value == "running"
    # Tidy so later worker drains do not see an abandoned-able orphan.
    from app.models import Job, JobStatus

    db = SessionLocal()
    try:
        row = db.get(Job, job_id)
        row.status = JobStatus.failed
        row.error = "tidy after no-started_at check"
        db.commit()
    finally:
        db.close()


def test_worker_startup_abandons_running_jobs():
    """process_queued_jobs abandons prior-worker running jobs once at start."""
    from app.services import events
    from app.services.job_runner import ABANDONED_ERROR
    from app.worker.runner import process_queued_jobs

    running_id = _seed_job(
        operation="genestack.pipeline.run",
        status="running",
        started_at=datetime.now(timezone.utc),  # fresh — not stale by age
    )
    # Clear a closed TestClient loop so later claim publish is a no-op.
    events._loop = None
    process_queued_jobs(once=True, abandon_running=True)
    job = _get_job_row(running_id)
    assert job.status.value == "failed"
    assert job.error == ABANDONED_ERROR
    assert job.finished_at is not None


def test_api_lifespan_does_not_abandon_fresh_running(app):
    """API startup runs age-based stale recovery only — never abandon-all."""
    from fastapi.testclient import TestClient

    from app.services.job_runner import ABANDONED_ERROR, RECOVERY_ERROR

    fresh_id = _seed_job(
        operation="genestack.pipeline.run",
        status="running",
        started_at=datetime.now(timezone.utc),
    )
    with TestClient(app):
        pass

    job = _get_job_row(fresh_id)
    assert job.status.value == "running"
    assert job.error not in (ABANDONED_ERROR, RECOVERY_ERROR)
    # Tidy leftover running row.
    from app.db import SessionLocal
    from app.models import Job, JobStatus

    db = SessionLocal()
    try:
        row = db.get(Job, fresh_id)
        row.status = JobStatus.failed
        row.error = "tidy after api lifespan abandon check"
        db.commit()
    finally:
        db.close()
