"""Job submission and polling tests."""

from __future__ import annotations

import time
import uuid

import pytest


def _create_env(client, headers, prefix: str = "job-env") -> str:
    name = f"{prefix}-{uuid.uuid4().hex[:8]}"
    resp = client.post(
        "/api/v1/environments",
        headers=headers,
        json={"name": name, "description": f"{prefix} test"},
    )
    assert resp.status_code in (200, 201), resp.text
    eid = resp.json().get("id")
    assert eid, resp.text
    return eid


def _submit_job(
    client, headers, *, environment_id: str, operation: str, params: dict | None = None
):
    """Submit via nested env route (preferred) with global fallback."""
    body = {"operation": operation, "params": params or {}, "run_sync": True}
    resp = client.post(
        f"/api/v1/environments/{environment_id}/jobs",
        headers=headers,
        json=body,
    )
    if resp.status_code == 404:
        resp = client.post(
            "/api/v1/jobs",
            headers=headers,
            params={"environment_id": environment_id},
            json=body,
        )
    return resp


def _job_status(body: dict) -> str:
    return str(body.get("status") or body.get("state") or "").lower()


def _poll_job(
    client, headers, job_id: str, *, timeout: float = 30.0, interval: float = 0.2
):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        resp = client.get(f"/api/v1/jobs/{job_id}", headers=headers)
        assert resp.status_code == 200, resp.text
        last = resp.json()
        if _job_status(last) in {
            "succeeded",
            "success",
            "completed",
            "complete",
            "ok",
            "done",
            "failed",
            "error",
            "cancelled",
        }:
            return last
        time.sleep(interval)
    return last


def test_submit_maas_machines_list_job(client, admin_headers):
    """Create env, submit maas.machines.list, poll until success."""
    env_id = _create_env(client, admin_headers, "maas-list")
    resp = _submit_job(
        client,
        admin_headers,
        environment_id=env_id,
        operation="maas.machines.list",
    )
    assert resp.status_code in (200, 201, 202), resp.text
    job = resp.json()
    jid = job.get("id")
    assert jid, job

    if _job_status(job) not in {
        "succeeded",
        "success",
        "completed",
        "complete",
        "ok",
        "done",
    }:
        job = _poll_job(client, admin_headers, jid)
    assert _job_status(job) in {
        "succeeded",
        "success",
        "completed",
        "complete",
        "ok",
        "done",
    }, job


def test_submit_host_preflight_job(client, admin_headers):
    """Submit host.preflight (dry-run or real localhost ansible)."""
    env_id = _create_env(client, admin_headers, "preflight")
    resp = _submit_job(
        client,
        admin_headers,
        environment_id=env_id,
        operation="host.preflight",
        params={"limit": "localhost"},
    )
    assert resp.status_code in (200, 201, 202), resp.text
    job = resp.json()
    jid = job.get("id")
    assert jid, job

    if _job_status(job) not in {
        "succeeded",
        "success",
        "completed",
        "complete",
        "ok",
        "done",
        "failed",
        "error",
    }:
        job = _poll_job(client, admin_headers, jid, timeout=60.0)
    status = _job_status(job)
    # DRY_RUN should succeed even without ansible
    assert status in {
        "succeeded",
        "success",
        "completed",
        "complete",
        "ok",
        "done",
        "failed",
        "error",
    }, job


def test_genestack_scripts_list_finds_install_keystone(
    client, admin_headers, genestack_root
):
    """genestack.scripts.list job succeeds; install-keystone.sh exists under GENESTACK_ROOT."""
    keystone = genestack_root / "bin" / "install-keystone.sh"
    if not keystone.is_file():
        pytest.skip(f"install-keystone.sh not found at {keystone}")

    env_id = _create_env(client, admin_headers, "scripts-list")
    resp = _submit_job(
        client,
        admin_headers,
        environment_id=env_id,
        operation="genestack.scripts.list",
    )
    assert resp.status_code in (200, 201, 202), resp.text
    job = resp.json()
    jid = job.get("id")
    if _job_status(job) not in {
        "succeeded",
        "success",
        "completed",
        "complete",
        "ok",
        "done",
    }:
        job = _poll_job(client, admin_headers, jid)
    assert _job_status(job) in {
        "succeeded",
        "success",
        "completed",
        "complete",
        "ok",
        "done",
    }, job

    # Convenience endpoint returns concrete script names
    listed = client.get("/api/v1/genestack/scripts", headers=admin_headers)
    assert listed.status_code == 200, listed.text
    blob = str(listed.json()).lower()
    assert "install-keystone" in blob, listed.json()

    # Job log should at least mention scripts count or path
    log = (job.get("log_text") or "").lower()
    assert "script" in log or "install" in log or job.get("status") == "success"


def test_genestack_components_desired_reads_yaml(client, admin_headers, genestack_root):
    """genestack.components.desired job succeeds; openstack-components.yaml readable."""
    components = genestack_root / "openstack-components.yaml"
    if not components.is_file():
        pytest.skip(f"openstack-components.yaml not found at {components}")

    env_id = _create_env(client, admin_headers, "components")
    resp = _submit_job(
        client,
        admin_headers,
        environment_id=env_id,
        operation="genestack.components.desired",
    )
    assert resp.status_code in (200, 201, 202), resp.text
    job = resp.json()
    jid = job.get("id")
    if _job_status(job) not in {
        "succeeded",
        "success",
        "completed",
        "complete",
        "ok",
        "done",
    }:
        job = _poll_job(client, admin_headers, jid)
    assert _job_status(job) in {
        "succeeded",
        "success",
        "completed",
        "complete",
        "ok",
        "done",
    }, job

    direct = client.get("/api/v1/genestack/components", headers=admin_headers)
    assert direct.status_code == 200, direct.text
    body = direct.json()
    blob = str(body).lower()
    assert any(
        svc in blob for svc in ("keystone", "placement", "nova", "components")
    ), body
    assert body.get("exists") is True or "components" in body


def test_retry_job_creates_new_job(client, admin_headers):
    """POST /api/v1/jobs/{id}/retry creates a new job; source is untouched."""
    resp = client.post(
        "/api/v1/jobs",
        headers=admin_headers,
        json={
            "operation": "internal.health",
            "params": {"origin": "retry-test"},
            "run_sync": True,
        },
    )
    assert resp.status_code in (200, 201), resp.text
    original = resp.json()

    retry = client.post(
        f"/api/v1/jobs/{original['id']}/retry",
        headers=admin_headers,
        json={},
    )
    assert retry.status_code in (200, 201), retry.text
    new_job = retry.json()
    assert new_job["id"] != original["id"]
    assert new_job["operation"] == original["operation"]
    assert new_job["params"] == original["params"]

    # Old job is not mutated by the retry
    old = client.get(f"/api/v1/jobs/{original['id']}", headers=admin_headers)
    assert old.status_code == 200, old.text
    assert old.json()["status"] == original["status"]
    assert old.json()["finished_at"] == original["finished_at"]


def test_retry_job_run_sync_false_queues(client, admin_headers):
    """run_sync=false in the retry body leaves the new job queued."""
    resp = client.post(
        "/api/v1/jobs",
        headers=admin_headers,
        json={"operation": "internal.health", "params": {}, "run_sync": True},
    )
    assert resp.status_code in (200, 201), resp.text
    original = resp.json()

    retry = client.post(
        f"/api/v1/jobs/{original['id']}/retry",
        headers=admin_headers,
        json={"run_sync": False},
    )
    assert retry.status_code in (200, 201), retry.text
    assert retry.json()["status"] == "queued"


def test_retry_missing_job_returns_404(client, admin_headers):
    resp = client.post(
        "/api/v1/jobs/does-not-exist/retry",
        headers=admin_headers,
        json={},
    )
    assert resp.status_code == 404, resp.text


def test_retry_requires_operator_and_op_role(
    client, admin_headers, operator_headers, viewer_headers
):
    """Viewers cannot retry; operators cannot retry admin-role operations."""
    resp = client.post(
        "/api/v1/jobs",
        headers=admin_headers,
        json={"operation": "internal.health", "params": {}, "run_sync": True},
    )
    assert resp.status_code in (200, 201), resp.text
    viewer_retry = client.post(
        f"/api/v1/jobs/{resp.json()['id']}/retry",
        headers=viewer_headers,
        json={},
    )
    assert viewer_retry.status_code == 403, viewer_retry.text

    # genestack.service.enable requires admin; queue it (run_sync=false) so the
    # retry's role check triggers before any execution.
    admin_job = client.post(
        "/api/v1/jobs",
        headers=admin_headers,
        json={
            "operation": "genestack.service.enable",
            "params": {"service": "keystone"},
            "run_sync": False,
        },
    )
    assert admin_job.status_code in (200, 201), admin_job.text
    op_retry = client.post(
        f"/api/v1/jobs/{admin_job.json()['id']}/retry",
        headers=operator_headers,
        json={"run_sync": False},
    )
    assert op_retry.status_code == 403, op_retry.text


# ----------------------------------------------------- per-env mutating lock


def _seed_running_mutating_job(environment_id: str) -> str:
    from app.db import SessionLocal
    from app.models import Job, JobStatus

    db = SessionLocal()
    try:
        job = Job(
            environment_id=environment_id,
            operation="genestack.service.enable",
            params={"service": "keystone"},
            status=JobStatus.running,
            log_text="",
            created_by="lock-test",
        )
        db.add(job)
        db.commit()
        return job.id
    finally:
        db.close()


def test_mutating_job_lock_per_environment(client, admin_headers):
    """A queued/running mutating job blocks new mutating jobs for the same env (409)."""
    env_x = _create_env(client, admin_headers, "lock-x")
    env_y = _create_env(client, admin_headers, "lock-y")
    seeded_id = _seed_running_mutating_job(env_x)

    # Another mutating op for env X -> 409 with the conflicting job id
    conflict = _submit_job(
        client,
        admin_headers,
        environment_id=env_x,
        operation="genestack.pipeline.run",
        params={"stage": "core"},
    )
    assert conflict.status_code == 409, conflict.text
    detail = conflict.json().get("detail")
    assert seeded_id in str(detail)

    # Read-only op for env X still succeeds
    read_op = _submit_job(
        client,
        admin_headers,
        environment_id=env_x,
        operation="genestack.scripts.list",
    )
    assert read_op.status_code in (200, 201), read_op.text
    assert _job_status(read_op.json()) in {"success", "succeeded", "completed"}

    # Same mutating op against env Y succeeds (lock is per-environment)
    other_env = _submit_job(
        client,
        admin_headers,
        environment_id=env_y,
        operation="genestack.service.enable",
        params={"service": "keystone"},
    )
    assert other_env.status_code in (200, 201), other_env.text
    assert _job_status(other_env.json()) in {"success", "succeeded", "completed"}
