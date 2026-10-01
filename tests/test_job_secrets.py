"""Secret job params — scrubbed at rest, masked in API/audit, restored in memory.

Covers the security fix for catalog-marked secret params
(OperationSpec.secret_params, e.g. baremetal.node.register's bmc_password):
the jobs row and audit log never hold the plaintext, JobRead serves "***",
and execution still receives the real value (in-process and via the worker).
"""

from __future__ import annotations

import uuid

from sqlalchemy import select

from app.db import SessionLocal
from app.models import AuditLog, BaremetalNode, Job, JobStatus
from app.services.crypto import FERNET_PREFIX, decrypt_secret
from app.services.job_runner import SECRET_PARAM_MASK, scrub_stored_job_secrets

SECRET = "calvin"


def _suffix() -> str:
    return uuid.uuid4().hex[:8]


def _create_env(client, headers, **fields):
    body = {"name": f"secret-env-{_suffix()}", **fields}
    resp = client.post("/api/v1/environments", headers=headers, json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _register_params(name, **extra):
    return {
        "name": name,
        "bmc_host": "bmc1.example.com",
        "bmc_username": "root",
        "bmc_password": SECRET,
        # explicit MAC: no redfish probe in tests
        "pxe_mac": "11:22:33:44:55:66",
        **extra,
    }


def _submit_register(client, headers, env_id, name, run_sync=True):
    resp = client.post(
        f"/api/v1/environments/{env_id}/jobs",
        headers=headers,
        json={
            "operation": "baremetal.node.register",
            "params": _register_params(name),
            "run_sync": run_sync,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _job_row(job_id) -> dict:
    db = SessionLocal()
    try:
        job = db.get(Job, job_id)
        return {
            "operation": job.operation,
            "params": job.params,
            "secret_params": job.secret_params,
            "status": job.status,
        }
    finally:
        db.close()


def _node_row(env_id, name):
    db = SessionLocal()
    try:
        node = db.scalar(
            select(BaremetalNode).where(
                BaremetalNode.environment_id == env_id,
                BaremetalNode.name == name,
            )
        )
        return None if node is None else {"bmc_password": node.bmc_password}
    finally:
        db.close()


def test_register_params_scrubbed_at_rest_and_in_api(client, admin_headers):
    env = _create_env(client, admin_headers)
    job = _submit_register(client, admin_headers, env["id"], f"bm-{_suffix()}")
    assert job["status"] == "success", job

    # The create response itself never carries the plaintext
    assert job["params"]["bmc_password"] == SECRET_PARAM_MASK
    assert SECRET not in str(job["params"])

    # At rest: params scrubbed, real value only fernet-encrypted
    row = _job_row(job["id"])
    assert row["params"]["bmc_password"] == SECRET_PARAM_MASK
    assert SECRET not in str(row["params"])
    assert row["secret_params"]["bmc_password"].startswith(FERNET_PREFIX)
    assert decrypt_secret(row["secret_params"]["bmc_password"]) == SECRET

    # JobRead serves the scrubbed copy
    got = client.get(f"/api/v1/jobs/{job['id']}", headers=admin_headers)
    assert got.status_code == 200, got.text
    assert got.json()["params"]["bmc_password"] == SECRET_PARAM_MASK
    assert SECRET not in got.text

    # The job.started audit entry copies the scrubbed params too
    audit = client.get(
        "/api/v1/audit",
        headers=admin_headers,
        params={"environment_id": env["id"], "action": "job.started"},
    )
    assert audit.status_code == 200, audit.text
    entries = [
        e
        for e in audit.json()
        if e["action"] == "job.started" and e["resource_id"] == job["id"]
    ]
    assert entries, "expected a job.started audit entry"
    assert entries[0]["details"]["params"]["bmc_password"] == SECRET_PARAM_MASK
    assert SECRET not in str(entries[0]["details"])


def test_execution_receives_real_password(client, admin_headers):
    """Scrubbed at rest, but the handler registers with the real password."""
    env = _create_env(client, admin_headers, dry_run=False)
    name = f"bm-{_suffix()}"
    job = _submit_register(client, admin_headers, env["id"], name)
    assert job["status"] == "success", job

    node = _node_row(env["id"], name)
    assert node is not None, "register handler never ran with a real password"
    assert node["bmc_password"].startswith(FERNET_PREFIX)
    assert decrypt_secret(node["bmc_password"]) == SECRET


def test_queued_worker_path_restores_secret_from_row(client, admin_headers):
    """Queued job claimed by the worker: secrets come from the row, not memory."""
    from app.worker.runner import process_queued_jobs

    env = _create_env(client, admin_headers, dry_run=False)
    name = f"bm-{_suffix()}"
    job = _submit_register(client, admin_headers, env["id"], name, run_sync=False)
    assert job["status"] == "queued", job

    assert process_queued_jobs(job_id=job["id"]) == 1

    row = _job_row(job["id"])
    assert row["status"] == JobStatus.success
    assert row["params"]["bmc_password"] == SECRET_PARAM_MASK
    node = _node_row(env["id"], name)
    assert node is not None
    assert decrypt_secret(node["bmc_password"]) == SECRET


def test_tenant_viewer_sees_masked_params(client, admin_headers):
    tenant = client.post(
        "/api/v1/tenants", headers=admin_headers, json={"name": f"tenant-{_suffix()}"}
    )
    assert tenant.status_code == 201, tenant.text
    env = _create_env(client, admin_headers, tenant_id=tenant.json()["id"])

    username = f"user-{_suffix()}"
    resp = client.post(
        "/api/v1/users",
        headers=admin_headers,
        json={
            "username": username,
            "password": "pw",
            "memberships": [{"tenant_id": tenant.json()["id"], "role": "viewer"}],
        },
    )
    assert resp.status_code == 201, resp.text
    login = client.post(
        "/api/v1/auth/login", json={"username": username, "password": "pw"}
    )
    assert login.status_code == 200, login.text
    viewer_headers = {"Authorization": f"Bearer {login.json()['token']}"}

    job = _submit_register(client, admin_headers, env["id"], f"bm-{_suffix()}")
    assert job["status"] == "success", job

    got = client.get(f"/api/v1/jobs/{job['id']}", headers=viewer_headers)
    assert got.status_code == 200, got.text
    assert got.json()["params"]["bmc_password"] == SECRET_PARAM_MASK
    assert SECRET not in got.text

    listed = client.get(
        "/api/v1/jobs", headers=viewer_headers, params={"environment_id": env["id"]}
    )
    assert listed.status_code == 200, listed.text
    assert SECRET not in listed.text


def test_historical_row_scrub(client, admin_headers):
    """Pre-fix rows (plaintext params + audit copies) are scrubbed, idempotently."""
    env = _create_env(client, admin_headers)
    db = SessionLocal()
    try:
        legacy = Job(
            environment_id=env["id"],
            operation="baremetal.node.register",
            params=_register_params(f"bm-{_suffix()}"),
            status=JobStatus.success,
            log_text="",
            created_by="legacy",
        )
        db.add(legacy)
        db.flush()
        audit = AuditLog(
            actor="legacy",
            action="job.started",
            resource_type="job",
            resource_id=legacy.id,
            environment_id=env["id"],
            details={
                "operation": "baremetal.node.register",
                "params": _register_params(legacy.params["name"]),
            },
            success=True,
        )
        db.add(audit)
        db.commit()
        legacy_id, audit_id = legacy.id, audit.id

        changed = scrub_stored_job_secrets(db)
        assert changed == 2

        db.expire_all()
        row = db.get(Job, legacy_id)
        assert row.params["bmc_password"] == SECRET_PARAM_MASK
        assert SECRET not in str(row.params)
        assert decrypt_secret(row.secret_params["bmc_password"]) == SECRET
        entry = db.get(AuditLog, audit_id)
        assert entry.details["params"]["bmc_password"] == SECRET_PARAM_MASK
        assert SECRET not in str(entry.details)

        # Idempotent: a second pass touches nothing
        assert scrub_stored_job_secrets(db) == 0
    finally:
        db.close()


def test_retry_carries_encrypted_secret_forward(client, admin_headers):
    """A retry of a scrubbed job still executes with the real secret."""
    env = _create_env(client, admin_headers, dry_run=False)
    name = f"bm-{_suffix()}"
    job = _submit_register(client, admin_headers, env["id"], name)
    assert job["status"] == "success", job

    resp = client.post(
        f"/api/v1/jobs/{job['id']}/retry",
        headers=admin_headers,
        json={"run_sync": True},
    )
    assert resp.status_code == 201, resp.text
    retried = resp.json()
    assert retried["status"] == "success", retried
    assert retried["params"]["bmc_password"] == SECRET_PARAM_MASK
    row = _job_row(retried["id"])
    assert decrypt_secret(row["secret_params"]["bmc_password"]) == SECRET
