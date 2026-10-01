"""hardware.terraform.plan / apply jobs."""

from __future__ import annotations

import uuid

SECRET = "tf-secret-MUST-NOT-LEAK"
ACCESS = "AKIATESTLEAKME"


def _suffix() -> str:
    return uuid.uuid4().hex[:8]


def _create_env(client, headers, *, dry_run: bool | None = None) -> str:
    body: dict = {"name": f"tf-env-{_suffix()}", "description": "terraform job test"}
    if dry_run is not None:
        body["dry_run"] = dry_run
    resp = client.post("/api/v1/environments", headers=headers, json=body)
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["id"]


def _create_account(client, headers, *, kind: str = "aws") -> dict:
    resp = client.post(
        "/api/v1/hardware/accounts",
        headers=headers,
        json={
            "kind": kind,
            "name": f"{kind}-{_suffix()}",
            "region": "us-east-1",
            "credentials": {"access_key": ACCESS, "secret_key": SECRET},
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _submit(client, headers, env_id: str, operation: str, params: dict | None = None):
    return client.post(
        f"/api/v1/environments/{env_id}/jobs",
        headers=headers,
        json={"operation": operation, "params": params or {}, "run_sync": True},
    )


def _assert_no_secrets(job: dict) -> None:
    blob = (
        str(job.get("params") or {})
        + (job.get("log_text") or "")
        + str(job.get("error") or "")
    )
    assert SECRET not in blob
    assert ACCESS not in blob
    params = job.get("params") or {}
    assert "secret_key" not in params
    assert "access_key" not in params
    assert "credentials" not in params


def test_catalog_contains_terraform_operations():
    from app.services.catalog import get_operation

    for op_id, handler in (
        ("hardware.terraform.plan", "hardware_terraform_plan"),
        ("hardware.terraform.apply", "hardware_terraform_apply"),
    ):
        op = get_operation(op_id)
        assert op is not None, op_id
        assert op.handler == handler
        assert op.required_role == "operator"
        assert op.mutating is True
        assert op.timeout_seconds == 1800
        names = {p.name for p in op.params}
        assert "account_id" in names
        assert "environment_id" in names
        assert "count" in names


def test_dry_run_apply_without_terraform_imports_hosts(
    client, admin_headers, monkeypatch
):
    monkeypatch.setattr("app.services.terraform.terraform_bin", lambda: None)
    env_id = _create_env(client, admin_headers)
    account = _create_account(client, admin_headers)

    plan = _submit(
        client,
        admin_headers,
        env_id,
        "hardware.terraform.plan",
        {"account_id": account["id"], "count": 2},
    )
    assert plan.status_code in (200, 201), plan.text
    plan_job = plan.json()
    assert plan_job["status"] == "success", plan_job
    assert plan_job.get("dry_run") is True
    _assert_no_secrets(plan_job)
    listed = client.get(f"/api/v1/environments/{env_id}/servers", headers=admin_headers)
    assert listed.status_code == 200, listed.text
    before = [s.get("hostname") for s in listed.json().get("servers") or []]
    assert "tf-aws-1" not in before

    apply = _submit(
        client,
        admin_headers,
        env_id,
        "hardware.terraform.apply",
        {"account_id": account["id"], "count": 2, "roles": ["compute"]},
    )
    assert apply.status_code in (200, 201), apply.text
    job = apply.json()
    assert job["status"] == "success", job
    assert job.get("dry_run") is True
    _assert_no_secrets(job)
    log = job.get("log_text") or ""
    assert "terraform apply" not in log.lower() or "dry-run" in log.lower()

    listed = client.get(f"/api/v1/environments/{env_id}/servers", headers=admin_headers)
    assert listed.status_code == 200, listed.text
    servers = listed.json().get("servers") or []
    by_name = {s.get("hostname"): s for s in servers}
    assert "tf-aws-1" in by_name
    assert "tf-aws-2" in by_name
    assert by_name["tf-aws-1"]["source"] == "terraform"
    assert by_name["tf-aws-1"]["ip"] == "192.0.2.11"
    assert "compute" in (by_name["tf-aws-1"].get("roles") or [])


def test_missing_account_id_fails_job(client, admin_headers, monkeypatch):
    monkeypatch.setattr("app.services.terraform.terraform_bin", lambda: None)
    env_id = _create_env(client, admin_headers)
    resp = _submit(client, admin_headers, env_id, "hardware.terraform.apply", {})
    assert resp.status_code in (200, 201, 400), resp.text
    if resp.status_code == 400:
        return
    job = resp.json()
    assert job["status"] == "failed", job
    err = (job.get("error") or "") + (job.get("log_text") or "")
    assert "account_id" in err


def test_unknown_account_id_fails_job(client, admin_headers, monkeypatch):
    monkeypatch.setattr("app.services.terraform.terraform_bin", lambda: None)
    env_id = _create_env(client, admin_headers)
    resp = _submit(
        client,
        admin_headers,
        env_id,
        "hardware.terraform.apply",
        {"account_id": str(uuid.uuid4())},
    )
    assert resp.status_code in (200, 201), resp.text
    job = resp.json()
    assert job["status"] == "failed", job
    err = (job.get("error") or "") + (job.get("log_text") or "")
    assert "not found" in err.lower() or "account" in err.lower()


def test_viewer_cannot_run_apply(client, admin_headers, viewer_headers, monkeypatch):
    monkeypatch.setattr("app.services.terraform.terraform_bin", lambda: None)
    env_id = _create_env(client, admin_headers)
    account = _create_account(client, admin_headers)
    resp = _submit(
        client,
        viewer_headers,
        env_id,
        "hardware.terraform.apply",
        {"account_id": account["id"]},
    )
    assert resp.status_code == 403, resp.text


def test_live_apply_without_terraform_fails(client, admin_headers, monkeypatch):
    monkeypatch.setattr("app.services.terraform.terraform_bin", lambda: None)
    env_id = _create_env(client, admin_headers, dry_run=False)
    account = _create_account(client, admin_headers)
    resp = _submit(
        client,
        admin_headers,
        env_id,
        "hardware.terraform.apply",
        {"account_id": account["id"]},
    )
    assert resp.status_code in (200, 201), resp.text
    job = resp.json()
    assert job["status"] == "failed", job
    err = (job.get("error") or "").lower()
    assert "terraform" in err
    _assert_no_secrets(job)
    listed = client.get(f"/api/v1/environments/{env_id}/servers", headers=admin_headers)
    names = [s.get("hostname") for s in listed.json().get("servers") or []]
    assert "tf-aws-1" not in names


def test_terraform_state_snapshot_and_restore(client, admin_headers, tmp_path):
    """Disk is the working copy; SQLite holds the last snapshot for restore."""
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models import TerraformState
    from app.services.crypto import FERNET_PREFIX
    from app.services.terraform import restore_state_file, snapshot_state_file

    env_id = _create_env(client, admin_headers)
    account = _create_account(client, admin_headers)
    work = tmp_path / "tf"
    work.mkdir()
    marker = "i-should-not-appear-in-sqlite-plaintext"
    (work / "terraform.tfstate").write_text(
        '{"version":4,"serial":3,"lineage":"abc","marker":"%s"}' % marker,
        encoding="utf-8",
    )
    db = SessionLocal()
    try:
        assert snapshot_state_file(
            db, environment_id=env_id, account_id=account["id"], work_dir=work
        )
        db.commit()
        row = db.scalar(
            select(TerraformState).where(
                TerraformState.environment_id == env_id,
                TerraformState.account_id == account["id"],
            )
        )
        assert row is not None
        assert row.serial == 3
        assert row.lineage == "abc"
        assert row.state_encrypted.startswith(FERNET_PREFIX)
        assert marker not in row.state_encrypted

        (work / "terraform.tfstate").unlink()
        assert restore_state_file(
            db, environment_id=env_id, account_id=account["id"], work_dir=work
        )
        restored = (work / "terraform.tfstate").read_text(encoding="utf-8")
        assert marker in restored
        assert '"serial": 3' in restored or '"serial":3' in restored

        (work / "terraform.tfstate").write_text(
            '{"version":4,"serial":9}', encoding="utf-8"
        )
        assert (
            restore_state_file(
                db, environment_id=env_id, account_id=account["id"], work_dir=work
            )
            is False
        )
        assert '"serial":9' in (work / "terraform.tfstate").read_text(encoding="utf-8")
    finally:
        db.close()
