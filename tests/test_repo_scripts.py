"""Repo scripts surface — inventory service, genestack.repo_scripts.list, genestack.repo_script.run."""

from __future__ import annotations

import uuid
from pathlib import Path

from app.services import genestack_bridge as bridge
from app.services import repo_scripts
from app.services.catalog import get_operation


def _suffix() -> str:
    return uuid.uuid4().hex[:8]


def _write_fake_repo_tree(root: Path) -> Path:
    """Minimal genestack tree with scripts/, maintenances/, ops-tools/."""
    root = Path(root)
    scripts_dir = root / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    (scripts_dir / "backup-mariadb.sh").write_text(
        "#!/usr/bin/env bash\n"
        "# Dump every database in the mariadb cluster.\n"
        'echo "backup"\n',
        encoding="utf-8",
    )
    (scripts_dir / "cleanup-envoy-httproutes.sh").write_text(
        "#!/usr/bin/env bash\necho cleanup\n", encoding="utf-8"
    )
    (scripts_dir / "dangerous-wipe.sh").write_text(
        "#!/usr/bin/env bash\n# NOT allowlisted.\necho wipe\n", encoding="utf-8"
    )
    maint_dir = root / "maintenances"
    maint_dir.mkdir(parents=True, exist_ok=True)
    (maint_dir / "maintenance-longhorn-1.8.0-to-1.9.1.txt").write_text(
        "\nUpgrade Longhorn 1.8.0 to 1.9.1\n\nstep 1\n", encoding="utf-8"
    )
    tool_dir = root / "ops-tools" / "check_octavia_ovn"
    tool_dir.mkdir(parents=True, exist_ok=True)
    (tool_dir / "check.py").write_text("print('ok')\n", encoding="utf-8")
    return root


def _create_env(client, headers, **fields) -> dict:
    resp = client.post(
        "/api/v1/environments",
        headers=headers,
        json={"name": f"env-reposcripts-{_suffix()}", **fields},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _env_job(client, headers, env_id, operation, params=None):
    return client.post(
        f"/api/v1/environments/{env_id}/jobs",
        headers=headers,
        json={"operation": operation, "params": params or {}, "run_sync": True},
    )


def _job_log(client, headers, job_id) -> str:
    resp = client.get(f"/api/v1/jobs/{job_id}", headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()["log_text"]


def _capture_commands(monkeypatch, returncode=0):
    """Capture every run_command call with its kwargs."""

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


# ------------------------------------------------------------ inventory service


def test_list_repo_scripts_on_fake_tree(tmp_path):
    root = _write_fake_repo_tree(tmp_path / "genestack")
    inv = repo_scripts.list_repo_scripts(root)

    assert inv["genestack_root"] == str(root)
    by_name = {s["name"]: s for s in inv["scripts"]}
    assert set(by_name) == {
        "backup-mariadb.sh",
        "cleanup-envoy-httproutes.sh",
        "dangerous-wipe.sh",
    }
    assert by_name["backup-mariadb.sh"]["description"] == (
        "Dump every database in the mariadb cluster."
    )
    # no comment line -> empty description
    assert by_name["cleanup-envoy-httproutes.sh"]["description"] == ""
    # runnable flag mirrors the allowlist
    assert by_name["backup-mariadb.sh"]["runnable"] is True
    assert by_name["dangerous-wipe.sh"]["runnable"] is False

    assert inv["maintenances"] == [
        {
            "name": "maintenance-longhorn-1.8.0-to-1.9.1.txt",
            "path": str(
                root / "maintenances" / "maintenance-longhorn-1.8.0-to-1.9.1.txt"
            ),
            "title": "Upgrade Longhorn 1.8.0 to 1.9.1",
        }
    ]
    assert inv["ops_tools"] == [
        {
            "name": "check.py",
            "path": str(root / "ops-tools" / "check_octavia_ovn" / "check.py"),
        }
    ]
    assert inv["counts"] == {"scripts": 3, "maintenances": 1, "ops_tools": 1}
    assert inv["safe_scripts"] == sorted(repo_scripts.SAFE_REPO_SCRIPTS)


def test_list_repo_scripts_missing_dirs_empty(tmp_path):
    root = tmp_path / "no-such-root"
    inv = repo_scripts.list_repo_scripts(root)
    assert inv["scripts"] == []
    assert inv["maintenances"] == []
    assert inv["ops_tools"] == []
    assert inv["counts"] == {"scripts": 0, "maintenances": 0, "ops_tools": 0}


# ------------------------------------------------------------ catalog shapes


def test_repo_scripts_list_op_in_catalog():
    op = get_operation("genestack.repo_scripts.list")
    assert op is not None
    assert op.required_role == "viewer"
    assert op.mutating is False
    assert op.handler == "genestack_repo_scripts_list"


def test_repo_script_run_op_in_catalog():
    op = get_operation("genestack.repo_script.run")
    assert op is not None
    assert op.required_role == "operator"
    assert op.mutating is True
    assert op.handler == "genestack_repo_script_run"
    assert op.timeout_seconds == 3600
    script = next(p for p in op.params if p.name == "script")
    assert script.required is True
    args = next(p for p in op.params if p.name == "args")
    assert args.required is False
    for allowed in repo_scripts.SAFE_REPO_SCRIPTS:
        assert allowed in op.description


# ------------------------------------------------------------ list op


def test_repo_scripts_list_op_via_job(client, operator_headers, tmp_path):
    root = _write_fake_repo_tree(tmp_path / "genestack")
    env = _create_env(client, operator_headers, genestack_path=str(root))
    resp = _env_job(client, operator_headers, env["id"], "genestack.repo_scripts.list")
    assert resp.status_code == 201, resp.text
    job = resp.json()
    assert job["status"] == "success", job["error"]

    log_text = _job_log(client, operator_headers, job["id"])
    assert (
        f"repo scripts under {root}: scripts=3 maintenances=1 ops_tools=1" in log_text
    )
    assert "'count': 5" in log_text


def test_repo_scripts_list_visible_to_viewer_in_catalog(client, viewer_headers):
    """Viewer sees the read op in the catalog (job creation itself is operator+)."""
    resp = client.get("/api/v1/operations", headers=viewer_headers)
    assert resp.status_code == 200, resp.text
    ids = {op["id"] for op in resp.json()}
    assert "genestack.repo_scripts.list" in ids
    assert "genestack.repo_script.run" not in ids


# ------------------------------------------------------------ run op


def test_repo_script_run_dry_run_logs_command_with_args(
    client, operator_headers, tmp_path
):
    """Global test config is dry_run=True: command logged, nothing executed."""
    root = _write_fake_repo_tree(tmp_path / "genestack")
    env = _create_env(client, operator_headers, genestack_path=str(root))
    resp = _env_job(
        client,
        operator_headers,
        env["id"],
        "genestack.repo_script.run",
        params={"script": "backup-mariadb.sh", "args": "--namespace openstack"},
    )
    assert resp.status_code == 201, resp.text
    job = resp.json()
    assert job["status"] == "success", job["error"]

    log_text = _job_log(client, operator_headers, job["id"])
    assert "bash scripts/backup-mariadb.sh --namespace openstack" in log_text
    assert f"cwd={root}" in log_text
    assert "[dry-run] command not executed" in log_text

    audit = client.get(
        "/api/v1/audit",
        headers=operator_headers,
        params={"environment_id": env["id"], "action": "env.repo_script_run"},
    )
    entries = [e for e in audit.json() if e["action"] == "env.repo_script_run"]
    assert entries, "expected an env.repo_script_run audit entry"
    assert entries[0]["success"] is True
    assert entries[0]["details"]["script"] == "scripts/backup-mariadb.sh"
    assert entries[0]["details"]["args"] == "--namespace openstack"


def test_repo_script_run_not_allowlisted_fails_rc2(client, operator_headers, tmp_path):
    """Script exists under scripts/ but is not in SAFE_REPO_SCRIPTS."""
    root = _write_fake_repo_tree(tmp_path / "genestack")
    env = _create_env(client, operator_headers, genestack_path=str(root))
    resp = _env_job(
        client,
        operator_headers,
        env["id"],
        "genestack.repo_script.run",
        params={"script": "dangerous-wipe.sh"},
    )
    assert resp.status_code == 201, resp.text
    job = resp.json()
    assert job["status"] == "failed"
    assert "dangerous-wipe.sh" in job["error"]
    # the failure names the allowed set
    for allowed in repo_scripts.SAFE_REPO_SCRIPTS:
        assert allowed in job["error"]

    log_text = _job_log(client, operator_headers, job["id"])
    assert "[denied]" in log_text


def test_repo_script_run_unknown_script_fails(client, operator_headers, tmp_path):
    root = _write_fake_repo_tree(tmp_path / "genestack")
    env = _create_env(client, operator_headers, genestack_path=str(root))
    resp = _env_job(
        client,
        operator_headers,
        env["id"],
        "genestack.repo_script.run",
        params={
            "script": "cleanup-openstack-completed-jobs.sh"
        },  # allowlisted but absent
    )
    job = resp.json()
    assert job["status"] == "failed"
    assert "not runnable" in job["error"]


def test_repo_script_run_path_traversal_rejected(client, operator_headers, tmp_path):
    root = _write_fake_repo_tree(tmp_path / "genestack")
    env = _create_env(client, operator_headers, genestack_path=str(root))
    for bad in (
        "../bin/install-keystone.sh",
        "scripts/backup-mariadb.sh",
        "/etc/passwd",
    ):
        resp = _env_job(
            client,
            operator_headers,
            env["id"],
            "genestack.repo_script.run",
            params={"script": bad},
        )
        job = resp.json()
        assert job["status"] == "failed", bad
        assert "basename only" in job["error"], bad


def test_repo_script_run_ssh_target_threaded(
    client, operator_headers, monkeypatch, tmp_path
):
    root = _write_fake_repo_tree(tmp_path / "genestack")
    env = _create_env(
        client,
        operator_headers,
        dry_run=False,
        deployer_ssh_host="deployer.example.com",
        deployer_ssh_user="ubuntu",
        genestack_path=str(root),
    )
    captured = _capture_commands(monkeypatch)

    resp = _env_job(
        client,
        operator_headers,
        env["id"],
        "genestack.repo_script.run",
        params={"script": "backup-mariadb.sh", "args": "--all"},
    )
    job = resp.json()
    assert job["status"] == "success", job["error"]

    assert captured, "expected run_command to be called"
    call = captured[0]
    assert call["cmd"] == ["bash", "scripts/backup-mariadb.sh", "--all"]
    assert str(call["kwargs"]["cwd"]) == str(root)
    assert call["kwargs"]["ssh_target"] == "ubuntu@deployer.example.com"
    assert call["kwargs"]["remote_env"]["GENESTACK_BASE_DIR"] == str(root)


def test_repo_script_run_viewer_forbidden(
    client, operator_headers, viewer_headers, tmp_path
):
    root = _write_fake_repo_tree(tmp_path / "genestack")
    env = _create_env(client, operator_headers, genestack_path=str(root))
    resp = _env_job(
        client,
        viewer_headers,
        env["id"],
        "genestack.repo_script.run",
        params={"script": "backup-mariadb.sh"},
    )
    assert resp.status_code == 403


def test_repo_script_run_cross_tenant_forbidden(client, admin_headers, tmp_path):
    """An operator in tenant A cannot run scripts against tenant B's env."""
    root = _write_fake_repo_tree(tmp_path / "genestack")

    tenant_a = client.post(
        "/api/v1/tenants", headers=admin_headers, json={"name": f"rs-a-{_suffix()}"}
    ).json()
    tenant_b = client.post(
        "/api/v1/tenants", headers=admin_headers, json={"name": f"rs-b-{_suffix()}"}
    ).json()
    env_b = _create_env(
        client, admin_headers, tenant_id=tenant_b["id"], genestack_path=str(root)
    )

    username = f"rs-op-{_suffix()}"
    resp = client.post(
        "/api/v1/users",
        headers=admin_headers,
        json={
            "username": username,
            "password": "pw",
            "memberships": [{"tenant_id": tenant_a["id"], "role": "operator"}],
        },
    )
    assert resp.status_code == 201, resp.text
    login = client.post(
        "/api/v1/auth/login", json={"username": username, "password": "pw"}
    )
    headers = {"Authorization": f"Bearer {login.json()['token']}"}

    resp = _env_job(
        client,
        headers,
        env_b["id"],
        "genestack.repo_script.run",
        params={"script": "backup-mariadb.sh"},
    )
    assert resp.status_code == 403
