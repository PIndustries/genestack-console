"""Unit tests for the Phase 4 SSH executor (per-env remote deploy hosts)."""

from __future__ import annotations

import uuid
from pathlib import Path

from app.config import get_settings
from app.models import Environment, JobStatus
from app.services import genestack_bridge as bridge
from app.services.crypto import encrypt_secret
from app.services.envcontext import build_context


def _env(**kwargs) -> Environment:
    kwargs.setdefault("id", f"env-ssh-{uuid.uuid4().hex[:8]}")
    kwargs.setdefault("name", kwargs["id"])
    return Environment(**kwargs)


# ------------------------------------------------------------ EnvContext


def test_ssh_target_user_and_host():
    settings = get_settings()
    ctx = build_context(
        _env(deployer_ssh_host="10.0.0.5", deployer_ssh_user="deploy"), settings
    )
    assert ctx.ssh_target == "deploy@10.0.0.5"
    assert ctx.is_remote is True


def test_ssh_target_host_only():
    settings = get_settings()
    ctx = build_context(_env(deployer_ssh_host="deployer.example.com"), settings)
    assert ctx.ssh_target == "deployer.example.com"
    assert ctx.is_remote is True


def test_ssh_target_unset_is_local():
    settings = get_settings()
    ctx = build_context(_env(), settings)
    assert ctx.ssh_target is None
    assert ctx.is_remote is False
    # env=None global context is always local
    assert build_context(None, settings).is_remote is False


def test_remote_env_scoped_keys_only(tmp_path):
    settings = get_settings()
    config_dir = tmp_path / "etc-genestack"
    (config_dir / "inventory").mkdir(parents=True)

    ctx = build_context(_env(genestack_config_dir=str(config_dir)), settings)
    remote = ctx.remote_env()
    assert remote["GENESTACK_BASE_DIR"] == str(ctx.genestack_root)
    assert remote["GENESTACK_CONFIG"] == str(config_dir)
    assert remote["GENESTACK_OVERRIDES_DIR"] == str(config_dir)
    assert remote["ANSIBLE_INVENTORY"] == str(config_dir / "inventory")
    # never ships the console's process environment
    assert "PATH" not in remote
    assert len(remote) == 4


def test_remote_env_excludes_staged_kubeconfig(tmp_path, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "data_dir", tmp_path)

    payload = "apiVersion: v1\nclusters: []\n"
    ctx = build_context(
        _env(kubeconfig_data=encrypt_secret(payload, settings)), settings
    )
    # staged blob is a console-local temp file: local env gets it, remote must not
    assert "KUBECONFIG" in ctx.subprocess_env()
    assert "KUBECONFIG" not in ctx.remote_env()
    ctx.cleanup()


def test_remote_env_includes_host_path_kubeconfig(tmp_path):
    settings = get_settings()
    kube = tmp_path / "kubeconfig"
    kube.write_text("apiVersion: v1\n", encoding="utf-8")

    ctx = build_context(_env(kubeconfig_path=str(kube)), settings)
    assert ctx.remote_env()["KUBECONFIG"] == str(kube)
    ctx.cleanup()


# ------------------------------------------------------------ run_command


def test_run_command_ssh_dry_run_display():
    logs: list[str] = []
    result = bridge.run_command(
        ["bash", "bin/install-keystone.sh"],
        cwd=Path("/opt/genestack"),
        dry_run=True,
        ssh_target="deploy@10.0.0.5",
        remote_env={
            "GENESTACK_CONFIG": "/etc/genestack",
            "KUBECONFIG": "/home/deploy/.kube/config",
        },
        log=logs.append,
    )
    assert result["dry_run"] is True
    assert result["cmd"][0] == "ssh"
    assert "BatchMode=yes" in result["cmd"]

    shown = result["message"]
    assert shown.startswith("Would run: ssh")
    assert "deploy@10.0.0.5" in shown
    assert "GENESTACK_CONFIG=/etc/genestack" in shown
    assert "KUBECONFIG=/home/deploy/.kube/config" in shown
    assert "cd /opt/genestack &&" in shown
    # same ssh-wrapped display is logged
    assert any("ssh" in line and "deploy@10.0.0.5" in line for line in logs)
    assert any("[dry-run]" in line for line in logs)


def test_run_command_ssh_exec_argv(monkeypatch):
    captured = {}

    class _Proc:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured.update(kwargs)
        return _Proc()

    monkeypatch.setattr(bridge.subprocess, "run", fake_run)
    result = bridge.run_command(
        ["bash", "bin/install-keystone.sh"],
        cwd=Path("/opt/genestack"),
        dry_run=False,
        env={"LOCAL": "ignored"},
        extra_env={"LOCAL_EXTRA": "ignored"},
        ssh_target="deploy@10.0.0.5",
        remote_env={
            "GENESTACK_BASE_DIR": "/opt/genestack",
            "GENESTACK_CONFIG": "/etc/gs dir",
        },
    )
    assert result["returncode"] == 0

    argv = captured["argv"]
    assert argv[:9] == [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "UserKnownHostsFile=/dev/null",
    ]
    assert argv[9] == "deploy@10.0.0.5"
    remote = argv[10]
    assert remote.startswith("GENESTACK_BASE_DIR=/opt/genestack")
    assert "GENESTACK_CONFIG='/etc/gs dir'" in remote  # values shlex-quoted
    assert "cd /opt/genestack &&" in remote
    assert remote.endswith("bash bin/install-keystone.sh")
    # ssh runs locally: the console's env/extra_env/cwd are not forwarded
    assert captured["env"] is None
    assert captured["cwd"] is None


def test_run_command_local_unchanged_without_ssh_target(monkeypatch):
    captured = {}

    class _Proc:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured.update(kwargs)
        return _Proc()

    monkeypatch.setattr(bridge.subprocess, "run", fake_run)
    bridge.run_command(
        ["echo", "hi"], dry_run=False, extra_env={"GENESTACK_CONFIG": "/etc/gs"}
    )
    assert captured["argv"] == ["echo", "hi"]
    assert captured["env"]["GENESTACK_CONFIG"] == "/etc/gs"


# ------------------------------------------------------------ enable_service


def test_enable_service_forwards_ssh_target(monkeypatch, genestack_root):
    captured = {}

    def fake_run_command(cmd, **kwargs):
        captured.update(kwargs)
        return {"ok": True, "returncode": 0, "cmd": cmd}

    monkeypatch.setattr(bridge, "run_command", fake_run_command)
    remote = {"GENESTACK_BASE_DIR": str(genestack_root)}
    result = bridge.enable_service(
        "keystone",
        genestack_root,
        dry_run=False,
        ssh_target="deploy@10.0.0.5",
        remote_env=remote,
    )
    assert result.get("ok") is True
    assert captured["ssh_target"] == "deploy@10.0.0.5"
    assert captured["remote_env"] == remote


# ------------------------------------------------------------ run_playbook


def test_run_playbook_remote_skips_local_gates(monkeypatch, genestack_root, tmp_path):
    """Remote: missing local playbook/ansible must not trigger the dry-run fallback."""
    ansible_root = tmp_path / "ansible"  # playbook not present locally
    monkeypatch.setattr(bridge.shutil, "which", lambda _name: None)  # no local ansible

    captured = {}

    def fake_run_command(cmd, **kwargs):
        captured["cmd"] = cmd
        captured.update(kwargs)
        return {"ok": True, "returncode": 0, "cmd": cmd}

    monkeypatch.setattr(bridge, "run_command", fake_run_command)

    # Control: live local execution with missing ansible now hard-fails
    # instead of silently reporting a green dry-run (a rehearsal result for a
    # real run would mask a host that has no ansible).
    local = bridge.run_playbook(
        "host_preflight.yml",
        ansible_root=ansible_root,
        genestack_root=genestack_root,
        dry_run=False,
        allowlist=None,
    )
    assert local["ok"] is False
    assert local["returncode"] == 2
    assert "not a dry-run" in local["error"]

    result = bridge.run_playbook(
        "host_preflight.yml",
        ansible_root=ansible_root,
        genestack_root=genestack_root,
        dry_run=False,
        ssh_target="deploy@10.0.0.5",
        remote_env={"GENESTACK_BASE_DIR": str(genestack_root)},
        allowlist=None,
    )
    assert result.get("ok") is True
    assert captured["ssh_target"] == "deploy@10.0.0.5"
    assert captured["remote_env"] == {"GENESTACK_BASE_DIR": str(genestack_root)}
    assert (
        captured["cmd"][0] == "ansible-playbook"
    )  # resolved on the remote, not locally
    assert captured["cmd"][1] == "host_preflight.yml"


# ------------------------------------------------------------ job_runner


def test_job_runner_dry_run_logs_ssh_wrapped_command(client, genestack_root):
    from app.db import SessionLocal
    from app.services.job_runner import JobRunner

    db = SessionLocal()
    try:
        env = Environment(
            name=f"env-ssh-job-{uuid.uuid4().hex[:8]}",
            deployer_ssh_host="deployer.example.com",
            deployer_ssh_user="deploy",
            genestack_path=str(genestack_root),
        )
        db.add(env)
        db.commit()

        runner = JobRunner(db)
        job = runner.create_job(
            operation="genestack.service.enable",
            params={"service": "keystone"},
            environment_id=env.id,
            created_by="ssh-test",
        )
        db.commit()
        job = runner.run_job(job)

        # global test config is dry_run=True -> command is logged, not executed
        assert job.status == JobStatus.success, job.error
        log_text = job.log_text or ""
        assert "deploy@deployer.example.com" in log_text
        assert any(
            "ssh" in line and "install-keystone.sh" in line
            for line in log_text.splitlines()
        )
    finally:
        db.close()
