"""genestack.hyperconverged_lab job tests — the destructive lab deployer."""

from __future__ import annotations

import uuid

from app.services import genestack_bridge as bridge
from app.services.catalog import get_operation, validate_params


def _suffix() -> str:
    return uuid.uuid4().hex[:8]


def _create_env(client, headers, **fields) -> dict:
    resp = client.post(
        "/api/v1/environments",
        headers=headers,
        json={"name": f"env-hcl-{_suffix()}", **fields},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _env_job(client, headers, env_id, params=None):
    return client.post(
        f"/api/v1/environments/{env_id}/jobs",
        headers=headers,
        json={
            "operation": "genestack.hyperconverged_lab",
            "params": params or {},
            "run_sync": True,
        },
    )


def _job_log(client, headers, job_id) -> str:
    resp = client.get(f"/api/v1/jobs/{job_id}", headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()["log_text"]


def _capture_commands(monkeypatch, returncode=0):
    """Capture every run_command call with its kwargs (test_deploy.py pattern)."""

    def fake_run_command(cmd, **kwargs):
        _capture_commands.captured.append(
            {"cmd": [str(c) for c in cmd], "kwargs": kwargs}
        )
        return {
            "returncode": returncode,
            "dry_run": False,
            "message": f"rc={returncode}",
        }

    _capture_commands.captured = []
    monkeypatch.setattr(bridge, "run_command", fake_run_command)
    return _capture_commands.captured


# ------------------------------------------------------------ catalog shape


def test_hyperconverged_lab_op_in_catalog():
    op = get_operation("genestack.hyperconverged_lab")
    assert op is not None
    assert op.required_role == "admin"
    assert op.mutating is True
    assert op.handler == "genestack_hyperconverged_lab"
    assert op.timeout_seconds == 14400

    names = {p.name: p for p in op.params}
    assert names["platform"].required is True
    assert names["include"].required is False
    assert names["extra_args"].required is False
    assert names["dry_run"].required is False
    assert names["dry_run"].type == "boolean"

    # missing required platform is rejected by param validation
    assert validate_params("genestack.hyperconverged_lab", {})
    assert (
        validate_params("genestack.hyperconverged_lab", {"platform": "kubespray"}) == []
    )


# ------------------------------------------------------------ dry run


def test_hcl_dry_run_logs_command_with_platform_and_include(
    client, admin_headers, tmp_path, genestack_root
):
    """Global test config is dry_run=True: exact command logged, nothing executed."""
    config_dir = tmp_path / "etc-genestack"
    (config_dir / "inventory").mkdir(parents=True)

    env = _create_env(
        client,
        admin_headers,
        genestack_path=str(genestack_root),
        genestack_config_dir=str(config_dir),
    )
    resp = _env_job(
        client,
        admin_headers,
        env["id"],
        params={"platform": "kubespray", "include": "glance,keystone"},
    )
    assert resp.status_code == 201, resp.text
    job = resp.json()
    assert job["status"] == "success", job["error"]

    log_text = _job_log(client, admin_headers, job["id"])
    assert "bash scripts/hyperconverged-lab.sh kubespray -i glance,keystone" in log_text
    assert "[dry-run] command not executed" in log_text

    audit = client.get(
        "/api/v1/audit",
        headers=admin_headers,
        params={"environment_id": env["id"], "action": "env.hyperconverged_lab"},
    )
    entries = [e for e in audit.json() if e["action"] == "env.hyperconverged_lab"]
    assert entries, "expected an env.hyperconverged_lab audit entry"
    assert entries[0]["success"] is True
    assert entries[0]["details"]["platform"] == "kubespray"
    assert entries[0]["details"]["include"] == "glance,keystone"


def test_hcl_dry_run_param_forces_rehearsal_even_when_env_not_pinned(
    client, admin_headers, monkeypatch, tmp_path, genestack_root
):
    """params.dry_run=True must force a rehearsal even when env dry_run=False.

    The fake bridge records every call regardless of the flag (the real one
    short-circuits), so the honest signal is that the handler passed
    dry_run=True to the bridge for this destructive op.
    """
    config_dir = tmp_path / "etc-genestack"
    (config_dir / "inventory").mkdir(parents=True)
    captured = _capture_commands(monkeypatch)

    env = _create_env(
        client,
        admin_headers,
        dry_run=False,
        genestack_path=str(genestack_root),
        genestack_config_dir=str(config_dir),
    )
    resp = _env_job(
        client,
        admin_headers,
        env["id"],
        params={"platform": "talos", "dry_run": True},
    )
    job = resp.json()
    assert job["status"] == "success", job["error"]

    assert len(captured) == 1
    assert captured[0]["kwargs"]["dry_run"] is True
    assert captured[0]["cmd"] == ["bash", "scripts/hyperconverged-lab.sh", "talos"]


# ------------------------------------------------------------ validation


def test_hcl_invalid_platform_rejected(client, admin_headers, tmp_path, genestack_root):
    config_dir = tmp_path / "etc-genestack"
    (config_dir / "inventory").mkdir(parents=True)

    env = _create_env(
        client,
        admin_headers,
        genestack_path=str(genestack_root),
        genestack_config_dir=str(config_dir),
    )
    resp = _env_job(client, admin_headers, env["id"], params={"platform": "coreos"})
    assert resp.status_code == 201, resp.text
    job = resp.json()
    assert job["status"] == "failed"
    assert "Invalid hyperconverged-lab platform 'coreos'" in job["error"]
    assert "kubespray, talos" in job["error"]

    log_text = _job_log(client, admin_headers, job["id"])
    assert "[denied]" in log_text


# ------------------------------------------------------------ live path


def test_hcl_non_dry_run_executes_via_bridge(
    client, admin_headers, monkeypatch, tmp_path, genestack_root
):
    """Non-dry-run: the command executes through bridge.run_command with the
    right args, cwd, and ssh/env plumbing (test_deploy.py capture pattern)."""
    config_dir = tmp_path / "etc-genestack"
    (config_dir / "inventory").mkdir(parents=True)
    captured = _capture_commands(monkeypatch)

    env = _create_env(
        client,
        admin_headers,
        dry_run=False,
        genestack_path=str(genestack_root),
        genestack_config_dir=str(config_dir),
    )
    resp = _env_job(
        client,
        admin_headers,
        env["id"],
        params={"platform": "talos", "include": "cinder-volume", "extra_args": "-x"},
    )
    job = resp.json()
    assert job["status"] == "success", job["error"]

    assert len(captured) == 1
    call = captured[0]
    assert call["cmd"] == [
        "bash",
        "scripts/hyperconverged-lab.sh",
        "talos",
        "-i",
        "cinder-volume",
        "-x",
    ]
    assert str(call["kwargs"]["cwd"]) == str(genestack_root)
    assert call["kwargs"]["dry_run"] is False
    assert call["kwargs"]["ssh_target"] is None
    assert call["kwargs"]["timeout"] == 14400


def test_hcl_non_dry_run_nonzero_rc_fails_job(
    client, admin_headers, monkeypatch, tmp_path, genestack_root
):
    config_dir = tmp_path / "etc-genestack"
    (config_dir / "inventory").mkdir(parents=True)
    _capture_commands(monkeypatch, returncode=7)

    env = _create_env(
        client,
        admin_headers,
        dry_run=False,
        genestack_path=str(genestack_root),
        genestack_config_dir=str(config_dir),
    )
    resp = _env_job(client, admin_headers, env["id"], params={"platform": "kubespray"})
    job = resp.json()
    assert job["status"] == "failed"
    assert "hyperconverged-lab failed (rc=7)" in job["error"]


def test_hcl_ssh_target_threaded(
    client, admin_headers, monkeypatch, tmp_path, genestack_root
):
    config_dir = tmp_path / "etc-genestack"
    (config_dir / "inventory").mkdir(parents=True)
    captured = _capture_commands(monkeypatch)

    env = _create_env(
        client,
        admin_headers,
        dry_run=False,
        deployer_ssh_host="deployer.example.com",
        deployer_ssh_user="ubuntu",
        genestack_path=str(genestack_root),
        genestack_config_dir=str(config_dir),
    )
    resp = _env_job(client, admin_headers, env["id"], params={"platform": "kubespray"})
    job = resp.json()
    assert job["status"] == "success", job["error"]

    assert captured
    call = captured[0]
    assert call["kwargs"]["ssh_target"] == "ubuntu@deployer.example.com"
    assert call["kwargs"]["remote_env"]["GENESTACK_CONFIG"] == str(config_dir)
    assert call["cmd"][1] == "scripts/hyperconverged-lab.sh"


def test_hcl_operator_forbidden(client, admin_headers, operator_headers, tmp_path):
    env = _create_env(client, admin_headers)
    resp = _env_job(client, operator_headers, env["id"], params={"platform": "talos"})
    assert resp.status_code == 403


def test_hcl_viewer_forbidden(client, admin_headers, viewer_headers, tmp_path):
    env = _create_env(client, admin_headers)
    resp = _env_job(client, viewer_headers, env["id"], params={"platform": "talos"})
    assert resp.status_code == 403
