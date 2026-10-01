"""genestack.host_prepare job tests — fresh machine to ready-for-push+deploy."""

from __future__ import annotations

import uuid

from app.services import genestack_bridge as bridge
from app.services.catalog import get_operation


def _suffix() -> str:
    return uuid.uuid4().hex[:8]


def _create_env(client, headers, **fields) -> dict:
    resp = client.post(
        "/api/v1/environments",
        headers=headers,
        json={"name": f"env-prepare-{_suffix()}", **fields},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _prepare_job(client, headers, env_id, params=None):
    return client.post(
        f"/api/v1/environments/{env_id}/jobs",
        headers=headers,
        json={
            "operation": "genestack.host_prepare",
            "params": params or {},
            "run_sync": True,
        },
    )


def _job_log(client, headers, job_id) -> str:
    resp = client.get(f"/api/v1/jobs/{job_id}", headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()["log_text"]


def _capture_commands(monkeypatch, rc_rules=None):
    """Capture every run_command call; rc_rules maps a script substring to a returncode."""
    captured: list[dict] = []

    def fake_run_command(cmd, **kwargs):
        script = str(cmd[2]) if len(cmd) > 2 else " ".join(str(c) for c in cmd)
        rc = 0
        for needle, code in (rc_rules or {}).items():
            if needle in script:
                rc = code
                break
        captured.append(
            {"cmd": [str(c) for c in cmd], "script": script, "kwargs": kwargs}
        )
        return {"returncode": rc, "dry_run": False, "message": f"rc={rc}"}

    monkeypatch.setattr(bridge, "run_command", fake_run_command)
    return captured


def _scripts(captured) -> list[str]:
    return [entry["script"] for entry in captured]


def test_host_prepare_op_in_catalog():
    op = get_operation("genestack.host_prepare")
    assert op is not None
    assert op.required_role == "admin"
    assert op.mutating is True
    assert op.handler == "genestack_host_prepare"
    assert op.timeout_seconds == 3600
    for name in ("repo_url", "repo_ref", "genestack_path", "config_dir"):
        param = next(p for p in op.params if p.name == name)
        assert param.required is False


def test_host_prepare_dry_run_logs_plan_in_order_executes_nothing(
    client, admin_headers, monkeypatch
):
    """Global test config is dry_run=True: full plan logged, nothing executed."""

    def boom(*args, **kwargs):  # noqa: ARG001
        raise AssertionError("subprocess.run must not be called in dry-run")

    monkeypatch.setattr(bridge.subprocess, "run", boom)

    env = _create_env(client, admin_headers)
    resp = _prepare_job(client, admin_headers, env["id"])
    assert resp.status_code == 201, resp.text
    job = resp.json()
    assert job["status"] == "success", job["error"]

    log_text = _job_log(client, admin_headers, job["id"])
    clone = "git clone --recurse-submodules https://github.com/rackerlabs/genestack /opt/genestack"
    checkout = "git -C /opt/genestack checkout main"
    bootstrap = "sudo -n -E bash /opt/genestack/bootstrap.sh"
    assert clone in log_text
    assert checkout in log_text
    assert bootstrap in log_text
    # Ordered: clone -> checkout -> bootstrap, all dry-run logged
    assert log_text.find(clone) < log_text.find(checkout) < log_text.find(bootstrap)
    assert "[dry-run] command not executed" in log_text
    # GENESTACK_CONFIG is exported for the bootstrap step
    assert "GENESTACK_CONFIG" in log_text


def test_host_prepare_missing_git_preflight_fails(client, admin_headers, monkeypatch):
    env = _create_env(client, admin_headers, dry_run=False)
    captured = _capture_commands(monkeypatch, rc_rules={"command -v git": 1})

    resp = _prepare_job(client, admin_headers, env["id"])
    assert resp.status_code == 201, resp.text
    job = resp.json()
    assert job["status"] == "failed"
    assert "git not found" in job["error"]

    scripts = _scripts(captured)
    assert scripts == ["command -v git"], "must stop at the failed preflight step"


def test_host_prepare_missing_ansible_warns_but_continues(
    client, admin_headers, monkeypatch
):
    env = _create_env(client, admin_headers, dry_run=False)
    _capture_commands(monkeypatch, rc_rules={"command -v ansible-playbook": 1})

    resp = _prepare_job(client, admin_headers, env["id"])
    job = resp.json()
    assert job["status"] == "success", job["error"]

    log_text = _job_log(client, admin_headers, job["id"])
    assert "WARNING ansible-playbook not found" in log_text
    assert "bootstrap.sh" in log_text


def test_host_prepare_existing_repo_fetches_not_clones(
    client, admin_headers, monkeypatch
):
    env = _create_env(client, admin_headers, dry_run=False)
    captured = _capture_commands(monkeypatch, rc_rules={"test -d /opt/genestack": 0})

    resp = _prepare_job(client, admin_headers, env["id"])
    job = resp.json()
    assert job["status"] == "success", job["error"]

    scripts = _scripts(captured)
    assert not any("git clone" in s for s in scripts)
    assert "git -C /opt/genestack fetch" in scripts
    # Existing checkout: never checkout a ref, never pull
    assert not any("checkout" in s for s in scripts)
    assert not any("pull" in s for s in scripts)


def test_host_prepare_fresh_clone_checks_out_ref_then_bootstraps(
    client, admin_headers, monkeypatch
):
    env = _create_env(client, admin_headers, dry_run=False)
    captured = _capture_commands(monkeypatch, rc_rules={"test -d /srv/genestack": 1})

    params = {
        "repo_url": "https://git.example.com/genestack.git",
        "repo_ref": "stable/2026.1",
        "genestack_path": "/srv/genestack",
        "config_dir": "/etc/genestack-lab",
    }
    resp = _prepare_job(client, admin_headers, env["id"], params=params)
    job = resp.json()
    assert job["status"] == "success", job["error"]

    scripts = _scripts(captured)
    clone = "git clone --recurse-submodules https://git.example.com/genestack.git /srv/genestack"
    checkout = "git -C /srv/genestack checkout stable/2026.1"
    bootstrap = "sudo -n -E bash /srv/genestack/bootstrap.sh"
    verify = (
        "test -f /etc/genestack-lab/provider && test -d /etc/genestack-lab/inventory "
        "&& test -d /etc/genestack-lab/helm-configs"
    )
    assert clone in scripts
    assert checkout in scripts
    assert bootstrap in scripts
    assert verify in scripts
    ordered = [scripts.index(s) for s in (clone, checkout, bootstrap, verify)]
    assert ordered == sorted(ordered)

    # Bootstrap step carries GENESTACK_CONFIG for the resolved config dir
    bootstrap_call = next(c for c in captured if c["script"] == bootstrap)
    assert (
        bootstrap_call["kwargs"]["remote_env"]["GENESTACK_CONFIG"]
        == "/etc/genestack-lab"
    )

    # Audit entry records the prepare
    audit = client.get(
        "/api/v1/audit",
        headers=admin_headers,
        params={"environment_id": env["id"], "action": "env.host_prepare"},
    )
    entries = [e for e in audit.json() if e["action"] == "env.host_prepare"]
    assert entries, "expected an env.host_prepare audit entry"
    assert entries[0]["success"] is True
    assert entries[0]["details"]["repo_ref"] == "stable/2026.1"
    assert entries[0]["details"]["config_dir"] == "/etc/genestack-lab"


def test_host_prepare_repo_url_defaults_from_env_metadata(client, admin_headers):
    env = _create_env(
        client,
        admin_headers,
        metadata_json={"repo_url": "https://git.example.com/fork.git"},
    )
    resp = _prepare_job(client, admin_headers, env["id"])
    job = resp.json()
    assert job["status"] == "success", job["error"]

    log_text = _job_log(client, admin_headers, job["id"])
    assert "git clone --recurse-submodules https://git.example.com/fork.git" in log_text


def test_host_prepare_stops_on_first_failure(client, admin_headers, monkeypatch):
    env = _create_env(client, admin_headers, dry_run=False)
    captured = _capture_commands(monkeypatch, rc_rules={"bootstrap.sh": 1})

    resp = _prepare_job(client, admin_headers, env["id"])
    job = resp.json()
    assert job["status"] == "failed"
    assert "bootstrap.sh failed" in job["error"]

    scripts = _scripts(captured)
    assert any("bootstrap.sh" in s for s in scripts)
    # Verify step never ran after the bootstrap failure
    assert not any("helm-configs" in s for s in scripts)


def test_host_prepare_verify_failure_fails_job(client, admin_headers, monkeypatch):
    env = _create_env(client, admin_headers, dry_run=False)
    _capture_commands(monkeypatch, rc_rules={"helm-configs": 1})

    resp = _prepare_job(client, admin_headers, env["id"])
    job = resp.json()
    assert job["status"] == "failed"
    assert "config skeleton incomplete" in job["error"]


def test_host_prepare_ssh_target_threaded(client, admin_headers, monkeypatch):
    env = _create_env(
        client,
        admin_headers,
        dry_run=False,
        deployer_ssh_host="deployer.example.com",
        deployer_ssh_user="ubuntu",
        genestack_config_dir="/etc/genestack",
    )
    captured = _capture_commands(monkeypatch)

    resp = _prepare_job(client, admin_headers, env["id"])
    job = resp.json()
    assert job["status"] == "success", job["error"]

    assert captured, "expected commands to run"
    for entry in captured:
        assert entry["kwargs"]["ssh_target"] == "ubuntu@deployer.example.com"


def test_host_prepare_operator_forbidden(client, admin_headers, operator_headers):
    env = _create_env(client, admin_headers)
    resp = _prepare_job(client, operator_headers, env["id"])
    assert resp.status_code == 403


def test_host_prepare_requires_environment(client, admin_headers):
    resp = client.post(
        "/api/v1/jobs",
        headers=admin_headers,
        json={"operation": "genestack.host_prepare", "params": {}, "run_sync": True},
    )
    assert resp.status_code == 201, resp.text
    job = resp.json()
    assert job["status"] == "failed"
    assert "requires an environment" in job["error"]
