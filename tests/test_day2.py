"""Day-2 operations — genestack.k8s_upgrade (kubespray) and genestack.backup_mariadb."""

from __future__ import annotations

import uuid
from pathlib import Path

from app.services import genestack_bridge as bridge
from app.services.catalog import get_operation


def _suffix() -> str:
    return uuid.uuid4().hex[:8]


def _create_env(client, headers, **fields) -> dict:
    resp = client.post(
        "/api/v1/environments",
        headers=headers,
        json={"name": f"env-day2-{_suffix()}", **fields},
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


# ------------------------------------------------------------ catalog shapes


def test_k8s_upgrade_op_in_catalog():
    op = get_operation("genestack.k8s_upgrade")
    assert op is not None
    assert op.required_role == "admin"
    assert op.mutating is True
    assert op.handler == "genestack_k8s_upgrade"
    assert op.timeout_seconds == 21600
    kube_version = next(p for p in op.params if p.name == "kube_version")
    assert kube_version.required is False


def test_backup_mariadb_op_in_catalog():
    op = get_operation("genestack.backup_mariadb")
    assert op is not None
    assert op.required_role == "operator"
    assert op.mutating is True
    assert op.handler == "genestack_backup_mariadb"
    assert op.timeout_seconds == 3600


# ------------------------------------------------------------ k8s_upgrade


def test_k8s_upgrade_dry_run_logs_ansible_command(
    client, admin_headers, tmp_path, genestack_root
):
    """Global test config is dry_run=True: command logged, nothing executed."""
    config_dir = tmp_path / "etc-genestack"
    (config_dir / "inventory").mkdir(parents=True)
    kubespray_dir = genestack_root / "submodules" / "kubespray"
    kubespray_dir.mkdir(parents=True, exist_ok=True)

    env = _create_env(
        client,
        admin_headers,
        genestack_path=str(genestack_root),
        genestack_config_dir=str(config_dir),
    )
    resp = _env_job(client, admin_headers, env["id"], "genestack.k8s_upgrade")
    assert resp.status_code == 201, resp.text
    job = resp.json()
    assert job["status"] == "success", job["error"]

    log_text = _job_log(client, admin_headers, job["id"])
    cmd = f"ansible-playbook upgrade-cluster.yml --become -i {config_dir}/inventory"
    assert cmd in log_text
    assert f"cwd={kubespray_dir}" in log_text
    assert "[dry-run] command not executed" in log_text

    audit = client.get(
        "/api/v1/audit",
        headers=admin_headers,
        params={"environment_id": env["id"], "action": "env.k8s_upgrade"},
    )
    entries = [e for e in audit.json() if e["action"] == "env.k8s_upgrade"]
    assert entries, "expected an env.k8s_upgrade audit entry"
    assert entries[0]["success"] is True
    assert entries[0]["details"]["inventory"] == str(config_dir / "inventory")


def test_k8s_upgrade_requires_environment(client, admin_headers):
    resp = client.post(
        "/api/v1/jobs",
        headers=admin_headers,
        json={"operation": "genestack.k8s_upgrade", "params": {}, "run_sync": True},
    )
    assert resp.status_code == 201, resp.text
    job = resp.json()
    assert job["status"] == "failed"
    assert "requires an environment" in job["error"]


def test_k8s_upgrade_succeeds_local_hub_without_config_dir(client, admin_headers):
    """K8s upgrade without config_dir succeeds as local-hub no-op."""
    env = _create_env(client, admin_headers)
    resp = _env_job(client, admin_headers, env["id"], "genestack.k8s_upgrade")
    assert resp.status_code == 201, resp.text
    job = resp.json()
    assert job["status"] == "success"  # local hub: no remote operation


def test_k8s_upgrade_missing_kubespray_dir_fails(client, admin_headers, tmp_path):
    """Local execution: missing kubespray submodule fails clearly (rc=2)."""
    config_dir = tmp_path / "etc-genestack"
    (config_dir / "inventory").mkdir(parents=True)
    empty_root = tmp_path / "genestack-no-submodule"
    empty_root.mkdir()

    env = _create_env(
        client,
        admin_headers,
        genestack_path=str(empty_root),
        genestack_config_dir=str(config_dir),
    )
    resp = _env_job(client, admin_headers, env["id"], "genestack.k8s_upgrade")
    job = resp.json()
    assert job["status"] == "failed"
    assert "kubespray submodule not found" in job["error"]
    assert str(empty_root / "submodules" / "kubespray") in job["error"]


def test_k8s_upgrade_nonzero_rc_fails_job(client, admin_headers, monkeypatch, tmp_path):
    config_dir = tmp_path / "etc-genestack"
    (config_dir / "inventory").mkdir(parents=True)
    env = _create_env(
        client,
        admin_headers,
        dry_run=False,
        deployer_ssh_host="deployer.example.com",
        genestack_config_dir=str(config_dir),
    )
    _capture_commands(monkeypatch, returncode=1)

    resp = _env_job(client, admin_headers, env["id"], "genestack.k8s_upgrade")
    job = resp.json()
    assert job["status"] == "failed"
    assert "k8s upgrade failed (rc=1)" in job["error"]


def _write_group_vars_file(config_dir) -> Path:
    """Create the env inventory group_vars k8s-cluster.yml with a version."""
    gv_dir = config_dir / "inventory" / "group_vars" / "k8s_cluster"
    gv_dir.mkdir(parents=True)
    file_path = gv_dir / "k8s-cluster.yml"
    file_path.write_text(
        "# k8s cluster group vars\n"
        "kube_config_dir: /etc/kubernetes\n"
        "kube_version: 1.31.4\n"
        "# kube_version_alt: 1.30.9\n"
        "kube_network_plugin: none\n",
        encoding="utf-8",
    )
    return file_path


def test_k8s_upgrade_kube_version_rewrites_file_with_backup(
    client, admin_headers, monkeypatch, tmp_path, genestack_root
):
    """Non-dry-run: kube_version line rewritten, original backed up beside it."""
    config_dir = tmp_path / "etc-genestack"
    group_vars_file = _write_group_vars_file(config_dir)
    (genestack_root / "submodules" / "kubespray").mkdir(parents=True, exist_ok=True)
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
        "genestack.k8s_upgrade",
        params={"kube_version": "1.34.0"},
    )
    job = resp.json()
    assert job["status"] == "success", job["error"]

    log_text = _job_log(client, admin_headers, job["id"])
    assert "kube_version set 1.31.4 -> 1.34.0" in log_text

    new_text = group_vars_file.read_text(encoding="utf-8")
    assert "kube_version: 1.34.0" in new_text
    assert "kube_version: 1.31.4" not in new_text
    # The commented-out line must survive untouched (only the active line rewrites)
    assert "# kube_version_alt: 1.30.9" in new_text
    # Backup next to the file, holding the original
    backups = list((group_vars_file.parent).glob("k8s-cluster.yml.bak-*"))
    assert backups, "expected a .bak-<timestamp> backup next to k8s-cluster.yml"
    assert "kube_version: 1.31.4" in backups[0].read_text(encoding="utf-8")

    # The playbook still runs against the inventory
    assert captured
    assert any(
        c["cmd"][0] == "ansible-playbook" and c["cmd"][1] == "upgrade-cluster.yml"
        for c in captured
    )

    log_text = _job_log(client, admin_headers, job["id"])
    assert "advisory only" not in log_text
    assert "kube_version 1.31.4 -> 1.34.0" in log_text


def test_k8s_upgrade_kube_version_dry_run_does_not_write(
    client, admin_headers, monkeypatch, tmp_path, genestack_root
):
    config_dir = tmp_path / "etc-genestack"
    group_vars_file = _write_group_vars_file(config_dir)
    (genestack_root / "submodules" / "kubespray").mkdir(parents=True, exist_ok=True)
    _capture_commands(monkeypatch)

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
        "genestack.k8s_upgrade",
        params={"kube_version": "1.34.0"},
    )
    job = resp.json()
    assert job["status"] == "success", job["error"]

    # File untouched, no backup created
    assert "kube_version: 1.31.4" in group_vars_file.read_text(encoding="utf-8")
    assert not list(group_vars_file.parent.glob("k8s-cluster.yml.bak-*"))

    log_text = _job_log(client, admin_headers, job["id"])
    assert (
        f"[k8s-upgrade] would set kube_version=1.34.0 in {group_vars_file}" in log_text
    )
    assert "advisory only" not in log_text


def test_k8s_upgrade_kube_version_missing_file_fails_cleanly(
    client, admin_headers, monkeypatch, tmp_path, genestack_root
):
    """No k8s-cluster.yml -> clear error, job fails, no playbook run."""
    config_dir = tmp_path / "etc-genestack"
    (config_dir / "inventory").mkdir(parents=True)
    (genestack_root / "submodules" / "kubespray").mkdir(parents=True, exist_ok=True)
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
        "genestack.k8s_upgrade",
        params={"kube_version": "1.34.0"},
    )
    job = resp.json()
    assert job["status"] == "failed"
    assert "kube_version source file not found" in job["error"]
    assert str(
        config_dir / "inventory" / "group_vars" / "k8s_cluster" / "k8s-cluster.yml"
    ) in (job["error"])
    assert not captured, "playbook must not run when the group_vars file is missing"


def test_k8s_upgrade_kube_version_no_active_key_fails_cleanly(
    client, admin_headers, monkeypatch, tmp_path, genestack_root
):
    """File exists but has no active kube_version line -> refuse to guess."""
    config_dir = tmp_path / "etc-genestack"
    gv_dir = config_dir / "inventory" / "group_vars" / "k8s_cluster"
    gv_dir.mkdir(parents=True)
    (gv_dir / "k8s-cluster.yml").write_text(
        "kube_config_dir: /etc/kubernetes\n# kube_version: 1.30.9\n", encoding="utf-8"
    )
    (genestack_root / "submodules" / "kubespray").mkdir(parents=True, exist_ok=True)
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
        "genestack.k8s_upgrade",
        params={"kube_version": "1.34.0"},
    )
    job = resp.json()
    assert job["status"] == "failed"
    assert "no active 'kube_version:' line" in job["error"]
    assert not captured


def test_k8s_upgrade_ssh_target_threaded(client, admin_headers, monkeypatch, tmp_path):
    config_dir = tmp_path / "etc-genestack"
    (config_dir / "inventory").mkdir(parents=True)
    env = _create_env(
        client,
        admin_headers,
        dry_run=False,
        deployer_ssh_host="deployer.example.com",
        deployer_ssh_user="ubuntu",
        genestack_config_dir=str(config_dir),
    )
    captured = _capture_commands(monkeypatch)

    resp = _env_job(client, admin_headers, env["id"], "genestack.k8s_upgrade")
    job = resp.json()
    assert job["status"] == "success", job["error"]

    assert captured, "expected run_command to be called"
    call = captured[0]
    assert call["kwargs"]["ssh_target"] == "ubuntu@deployer.example.com"
    assert call["kwargs"]["remote_env"]["GENESTACK_CONFIG"] == str(config_dir)
    assert call["cmd"] == [
        "ansible-playbook",
        "upgrade-cluster.yml",
        "--become",
        "-i",
        str(config_dir / "inventory"),
    ]


def test_k8s_upgrade_operator_forbidden(client, admin_headers, operator_headers):
    env = _create_env(client, admin_headers)
    resp = _env_job(client, operator_headers, env["id"], "genestack.k8s_upgrade")
    assert resp.status_code == 403


# ------------------------------------------------------------ backup_mariadb


def test_backup_mariadb_dry_run_logs_script_command(
    client, operator_headers, genestack_root
):
    """Global test config is dry_run=True: command logged, nothing executed."""
    env = _create_env(client, operator_headers, genestack_path=str(genestack_root))
    resp = _env_job(client, operator_headers, env["id"], "genestack.backup_mariadb")
    assert resp.status_code == 201, resp.text
    job = resp.json()
    assert job["status"] == "success", job["error"]

    log_text = _job_log(client, operator_headers, job["id"])
    assert "bash scripts/backup-mariadb.sh" in log_text
    assert f"cwd={genestack_root}" in log_text
    assert "[dry-run] command not executed" in log_text
    # Result message notes where the script writes the dumps
    assert "$HOME/backup/mariadb" in log_text

    audit = client.get(
        "/api/v1/audit",
        headers=operator_headers,
        params={"environment_id": env["id"], "action": "env.backup_mariadb"},
    )
    entries = [e for e in audit.json() if e["action"] == "env.backup_mariadb"]
    assert entries, "expected an env.backup_mariadb audit entry"
    assert entries[0]["success"] is True


def test_backup_mariadb_requires_environment(client, operator_headers):
    resp = client.post(
        "/api/v1/jobs",
        headers=operator_headers,
        json={"operation": "genestack.backup_mariadb", "params": {}, "run_sync": True},
    )
    assert resp.status_code == 201, resp.text
    job = resp.json()
    assert job["status"] == "failed"
    assert "requires an environment" in job["error"]


def test_backup_mariadb_nonzero_rc_fails_job(client, operator_headers, monkeypatch):
    env = _create_env(client, operator_headers, dry_run=False)
    _capture_commands(monkeypatch, returncode=1)

    resp = _env_job(client, operator_headers, env["id"], "genestack.backup_mariadb")
    job = resp.json()
    assert job["status"] == "failed"
    assert "mariadb backup failed (rc=1)" in job["error"]


def test_backup_mariadb_ssh_target_threaded(
    client, operator_headers, monkeypatch, genestack_root
):
    env = _create_env(
        client,
        operator_headers,
        dry_run=False,
        deployer_ssh_host="deployer.example.com",
        deployer_ssh_user="ubuntu",
        genestack_path=str(genestack_root),
    )
    captured = _capture_commands(monkeypatch)

    resp = _env_job(client, operator_headers, env["id"], "genestack.backup_mariadb")
    job = resp.json()
    assert job["status"] == "success", job["error"]

    assert captured, "expected run_command to be called"
    call = captured[0]
    assert call["kwargs"]["ssh_target"] == "ubuntu@deployer.example.com"
    assert call["cmd"] == ["bash", "scripts/backup-mariadb.sh"]
    assert str(call["kwargs"]["cwd"]) == str(genestack_root)


def test_backup_mariadb_viewer_forbidden(client, operator_headers, viewer_headers):
    env = _create_env(client, operator_headers)
    resp = _env_job(client, viewer_headers, env["id"], "genestack.backup_mariadb")
    assert resp.status_code == 403
