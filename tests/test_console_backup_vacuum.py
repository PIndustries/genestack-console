"""console.backup / console.vacuum catalog operations and audited jobs.

These tests cover catalog registration, dry-run paths, role gates, and
audit. They do not schedule a timer and they do not touch a live cluster.
"""

from __future__ import annotations

from app.services import genestack_bridge as bridge
from app.services.catalog import get_operation


def test_console_backup_op_in_catalog():
    op = get_operation("console.backup")
    assert op is not None
    assert op.handler == "console_backup"
    assert op.required_role == "operator"
    assert op.mutating is True
    assert op.timeout_seconds == 1800
    names = {p.name for p in op.params}
    assert "backup_dir" in names
    assert "keep" in names
    assert "dry_run" in names


def test_console_vacuum_op_in_catalog():
    op = get_operation("console.vacuum")
    assert op is not None
    assert op.handler == "console_vacuum"
    assert op.required_role == "admin"
    assert op.mutating is True
    assert op.timeout_seconds == 1800


def test_console_backup_dry_run_prints_paths_and_audits(client, operator_headers):
    """Global test config is dry_run=True: no script execution, paths logged."""
    resp = client.post(
        "/api/v1/jobs",
        headers=operator_headers,
        json={
            "operation": "console.backup",
            "params": {"backup_dir": "/tmp/gsc-backup-test", "keep": 3},
            "run_sync": True,
        },
    )
    assert resp.status_code == 201, resp.text
    job = resp.json()
    assert job["status"] == "success", job.get("error")
    assert job.get("dry_run") is True
    log = job.get("log_text") or ""
    assert "[console.backup]" in log
    assert "dir=/tmp/gsc-backup-test" in log
    assert "keep=3" in log
    assert "dry_run=True" in log
    assert "would run backup-console.sh" in log

    audit = client.get(
        "/api/v1/audit",
        headers=operator_headers,
        params={"action": "console.backup"},
    )
    assert audit.status_code == 200, audit.text
    entries = [e for e in audit.json() if e["action"] == "console.backup"]
    assert entries, "expected a console.backup audit entry"
    assert entries[0]["success"] is True
    assert entries[0]["details"].get("dry_run") is True
    assert entries[0]["details"].get("keep") == 3
    assert entries[0]["details"].get("backup_dir") == "/tmp/gsc-backup-test"


def test_console_backup_default_keep_and_dir(client, operator_headers):
    resp = client.post(
        "/api/v1/jobs",
        headers=operator_headers,
        json={"operation": "console.backup", "params": {}, "run_sync": True},
    )
    assert resp.status_code == 201, resp.text
    job = resp.json()
    assert job["status"] == "success", job.get("error")
    assert job.get("dry_run") is True
    log = job.get("log_text") or ""
    assert "keep=7" in log
    assert "backups/console" in log


def test_console_backup_invalid_keep(client, operator_headers):
    resp = client.post(
        "/api/v1/jobs",
        headers=operator_headers,
        json={
            "operation": "console.backup",
            "params": {"keep": 0},
            "run_sync": True,
        },
    )
    assert resp.status_code == 201, resp.text
    job = resp.json()
    assert job["status"] == "failed"
    assert "keep must be" in (job.get("error") or "")


def test_console_backup_viewer_forbidden(client, viewer_headers):
    resp = client.post(
        "/api/v1/jobs",
        headers=viewer_headers,
        json={"operation": "console.backup", "params": {}, "run_sync": True},
    )
    # Global jobs endpoint requires operator+; viewer is rejected before op check.
    assert resp.status_code == 403


def test_console_backup_live_invokes_script(client, operator_headers, monkeypatch):
    """With dry_run forced off, script is invoked locally (no SSH/agent)."""
    from app.services import job_runner as jr

    captured: list[dict] = []

    def fake_run_command(cmd, **kwargs):
        captured.append({"cmd": [str(c) for c in cmd], "kwargs": kwargs})
        return {"returncode": 0, "dry_run": False, "message": "rc=0"}

    monkeypatch.setattr(bridge, "run_command", fake_run_command)

    real_build = jr.build_context

    def build_live(env, settings=None):
        ctx = real_build(env, settings)
        ctx.dry_run = False
        return ctx

    monkeypatch.setattr(jr, "build_context", build_live)

    resp = client.post(
        "/api/v1/jobs",
        headers=operator_headers,
        json={
            "operation": "console.backup",
            "params": {"backup_dir": "/tmp/gsc-live-backup", "keep": 5},
            "run_sync": True,
        },
    )
    assert resp.status_code == 201, resp.text
    job = resp.json()
    assert job["status"] == "success", job.get("error")
    assert job.get("dry_run") is False
    assert captured, "expected run_command to be called"
    cmd = captured[0]["cmd"]
    assert cmd[0] == "bash"
    assert cmd[1].endswith("scripts/backup-console.sh")
    assert "/tmp/gsc-live-backup" in cmd
    assert "--keep" in cmd and "5" in cmd
    assert "--config" in cmd
    assert captured[0]["kwargs"].get("ssh_target") is None
    assert captured[0]["kwargs"].get("agent_env_id") is None
    log = job.get("log_text") or ""
    assert "console backup complete" in log


def test_console_vacuum_dry_run_stats_and_audits(client, admin_headers, monkeypatch):
    """Dry-run invokes vacuum script with --dry-run (stats only)."""
    captured: list[dict] = []

    def fake_run_command(cmd, **kwargs):
        captured.append({"cmd": [str(c) for c in cmd], "kwargs": kwargs})
        return {"returncode": 0, "dry_run": False, "message": "rc=0"}

    monkeypatch.setattr(bridge, "run_command", fake_run_command)

    resp = client.post(
        "/api/v1/jobs",
        headers=admin_headers,
        json={"operation": "console.vacuum", "params": {}, "run_sync": True},
    )
    assert resp.status_code == 201, resp.text
    job = resp.json()
    assert job["status"] == "success", job.get("error")
    assert job.get("dry_run") is True
    log = job.get("log_text") or ""
    assert "[console.vacuum]" in log
    assert "dry_run=True" in log
    assert "freelist/page stats" in log

    assert captured, "expected vacuum script invocation"
    cmd = captured[0]["cmd"]
    assert cmd[0] == "bash"
    assert cmd[1].endswith("scripts/vacuum-console.sh")
    assert "--dry-run" in cmd
    assert "--config" in cmd
    assert captured[0]["kwargs"].get("ssh_target") is None

    audit = client.get(
        "/api/v1/audit",
        headers=admin_headers,
        params={"action": "console.vacuum"},
    )
    assert audit.status_code == 200, audit.text
    entries = [e for e in audit.json() if e["action"] == "console.vacuum"]
    assert entries, "expected a console.vacuum audit entry"
    assert entries[0]["success"] is True
    assert entries[0]["details"].get("dry_run") is True


def test_console_vacuum_operator_forbidden(client, operator_headers):
    resp = client.post(
        "/api/v1/jobs",
        headers=operator_headers,
        json={"operation": "console.vacuum", "params": {}, "run_sync": True},
    )
    assert resp.status_code == 403
    assert "admin" in resp.json()["detail"]


def test_console_vacuum_live_omits_dry_run_flag(client, admin_headers, monkeypatch):
    from app.services import job_runner as jr

    captured: list[dict] = []

    def fake_run_command(cmd, **kwargs):
        captured.append({"cmd": [str(c) for c in cmd], "kwargs": kwargs})
        return {"returncode": 0, "dry_run": False, "message": "rc=0"}

    monkeypatch.setattr(bridge, "run_command", fake_run_command)

    real_build = jr.build_context

    def build_live(env, settings=None):
        ctx = real_build(env, settings)
        ctx.dry_run = False
        return ctx

    monkeypatch.setattr(jr, "build_context", build_live)

    resp = client.post(
        "/api/v1/jobs",
        headers=admin_headers,
        json={"operation": "console.vacuum", "params": {}, "run_sync": True},
    )
    assert resp.status_code == 201, resp.text
    job = resp.json()
    assert job["status"] == "success", job.get("error")
    assert job.get("dry_run") is False
    assert captured
    cmd = captured[0]["cmd"]
    assert "--dry-run" not in cmd
    log = job.get("log_text") or ""
    assert "console vacuum complete" in log
