"""Job cancellation: API scoping/status rules + worker-side cancel flag checks."""

from __future__ import annotations

import uuid


def _suffix() -> str:
    return uuid.uuid4().hex[:8]


def _create_env(client, headers, tenant_id=None) -> str:
    body = {"name": f"cancel-env-{_suffix()}", "description": "cancel test"}
    if tenant_id:
        body["tenant_id"] = tenant_id
    resp = client.post("/api/v1/environments", headers=headers, json=body)
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["id"]


def _create_user(client, admin_headers, memberships=None):
    username = f"cancel-user-{_suffix()}"
    resp = client.post(
        "/api/v1/users",
        headers=admin_headers,
        json={"username": username, "password": "pw", "memberships": memberships or []},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _login_headers(client, username, password="pw"):
    resp = client.post(
        "/api/v1/auth/login", json={"username": username, "password": password}
    )
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


def _queue_job(client, headers, environment_id: str) -> dict:
    resp = client.post(
        f"/api/v1/environments/{environment_id}/jobs",
        headers=headers,
        json={"operation": "internal.health", "params": {}, "run_sync": False},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["status"] == "queued"
    return resp.json()


def _seed_running_job(environment_id: str | None = None) -> str:
    from app.db import SessionLocal
    from app.models import Job, JobStatus

    db = SessionLocal()
    try:
        job = Job(
            environment_id=environment_id,
            operation="internal.health",
            params={},
            status=JobStatus.running,
            log_text="",
            created_by="cancel-test",
        )
        db.add(job)
        db.commit()
        return job.id
    finally:
        db.close()


def _finish_job(job_id: str) -> None:
    """Tidy-up: never leave a fake running job behind for other tests."""
    from app.db import SessionLocal
    from app.models import Job, JobStatus

    db = SessionLocal()
    try:
        job = db.get(Job, job_id)
        if job is not None and job.status == JobStatus.running:
            job.status = JobStatus.failed
            db.commit()
    finally:
        db.close()


def test_cancel_queued_job_marks_failed(client, admin_headers):
    env_id = _create_env(client, admin_headers)
    job = _queue_job(client, admin_headers, env_id)

    resp = client.post(f"/api/v1/jobs/{job['id']}/cancel", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "failed", body
    assert "cancelled" in (body["error"] or "")
    assert body["finished_at"] is not None

    # Stays failed — the worker only claims queued rows.
    fetched = client.get(f"/api/v1/jobs/{job['id']}", headers=admin_headers)
    assert fetched.json()["status"] == "failed"


def test_cancel_completed_job_returns_409(client, admin_headers):
    resp = client.post(
        "/api/v1/jobs",
        headers=admin_headers,
        json={"operation": "internal.health", "params": {}, "run_sync": True},
    )
    assert resp.status_code in (200, 201), resp.text
    job = resp.json()
    assert job["status"] == "success"

    resp = client.post(f"/api/v1/jobs/{job['id']}/cancel", headers=admin_headers)
    assert resp.status_code == 409, resp.text


def test_cancel_missing_job_returns_404(client, admin_headers):
    resp = client.post("/api/v1/jobs/does-not-exist/cancel", headers=admin_headers)
    assert resp.status_code == 404, resp.text


def test_cancel_requires_operator(client, admin_headers, viewer_headers):
    env_id = _create_env(client, admin_headers)
    job = _queue_job(client, admin_headers, env_id)
    resp = client.post(f"/api/v1/jobs/{job['id']}/cancel", headers=viewer_headers)
    assert resp.status_code == 403, resp.text
    _finish_job(job["id"])


def test_cancel_running_job_sets_flag(client, admin_headers):
    """Running jobs get the best-effort flag; the worker marks them failed."""
    job_id = _seed_running_job()
    try:
        resp = client.post(f"/api/v1/jobs/{job_id}/cancel", headers=admin_headers)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "running", body
        assert body["cancel_requested"] is True
    finally:
        _finish_job(job_id)


def test_cancel_cross_tenant_forbidden(client, admin_headers):
    """An operator in tenant A cannot cancel a job in tenant B's env (403)."""
    tenant_a = client.post(
        "/api/v1/tenants", headers=admin_headers, json={"name": f"cancel-a-{_suffix()}"}
    ).json()
    tenant_b = client.post(
        "/api/v1/tenants", headers=admin_headers, json={"name": f"cancel-b-{_suffix()}"}
    ).json()
    env_b = _create_env(client, admin_headers, tenant_id=tenant_b["id"])
    job = _queue_job(client, admin_headers, env_b)

    operator_a = _create_user(
        client,
        admin_headers,
        memberships=[{"tenant_id": tenant_a["id"], "role": "operator"}],
    )
    headers_a = _login_headers(client, operator_a["username"])
    resp = client.post(f"/api/v1/jobs/{job['id']}/cancel", headers=headers_a)
    assert resp.status_code == 403, resp.text

    # An operator in the job's own tenant can cancel.
    operator_b = _create_user(
        client,
        admin_headers,
        memberships=[{"tenant_id": tenant_b["id"], "role": "operator"}],
    )
    headers_b = _login_headers(client, operator_b["username"])
    resp = client.post(f"/api/v1/jobs/{job['id']}/cancel", headers=headers_b)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "failed"


def test_cancel_running_pipeline_stops_between_items(monkeypatch):
    """A cancel flagged mid-run stops a multi-item pipeline at the next item."""
    from app.db import SessionLocal
    from app.models import Job
    from app.services import genestack_bridge as bridge
    from app.services.job_runner import JobRunner

    holder: dict[str, str] = {}
    calls: list[list[str]] = []

    def fake_run_command(cmd, **kwargs):  # noqa: ARG001
        calls.append(cmd)
        # Operator cancels mid-flight (separate session, like the API process).
        other = SessionLocal()
        try:
            row = other.get(Job, holder["job_id"])
            row.cancel_requested = True
            other.commit()
        finally:
            other.close()
        return {"ok": True, "returncode": 0, "dry_run": True, "message": "fake"}

    monkeypatch.setattr(bridge, "run_command", fake_run_command)

    db = SessionLocal()
    try:
        runner = JobRunner(db)
        job = runner.create_job(
            operation="genestack.pipeline.run",
            params={"stage": "core"},  # 3 items: keystone, placement, glance
            created_by="cancel-test",
        )
        db.commit()
        holder["job_id"] = job.id
        job = runner.run_job(job)
        assert job.status.value == "failed"
        assert "cancelled" in (job.error or "")
    finally:
        db.close()
    assert len(calls) == 1, f"pipeline must stop after item 1, ran {len(calls)}"
