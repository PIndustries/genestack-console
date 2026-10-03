"""Secret files exist while a job runs and are removed when it ends.

The console database holds the secrets. These tests never open SSH.
"""

from __future__ import annotations

import base64
import logging
import shlex
import uuid
from pathlib import Path

import pytest
import yaml

from app.config import get_settings
from app.db import SessionLocal
from app.models import Environment, JobStatus
from app.services import envconfig as envconfig_service
from app.services import genestack_bridge as bridge
from app.services import secret_lease
from app.services.crypto import decrypt_secret
from app.services.deploy import _fetch_kubeconfig
from app.services.envcontext import EnvContext, build_context
from app.services.job_runner import JobCancelledError, JobRunner
from app.services.ssh_keys import get_decrypted_private_key, store_key_pair

PASSWORD = "s3cret-lease-password"
SENTINEL = "kube-sentinel-value"

DOC = f"""\
secrets:
  netapp-cinder-backend:
    data:
      username: lease-user
      password: {PASSWORD}
chart_versions:
  keystone: 1.2.3
"""

GENERATED = """\
---
apiVersion: v1
kind: Secret
metadata:
  name: mariadb
  namespace: openstack
type: Opaque
data:
  password: Z2Vu
"""

ADMIN_CONF = f"""\
apiVersion: v1
clusters:
- cluster:
    certificate-authority-data: {SENTINEL}
    server: https://127.0.0.1:6443
  name: cluster.local
"""

CP_DOC = {
    "servers": {
        "cp1": {"ip": "10.0.0.11", "roles": ["k8s_control_plane"]},
    }
}


def _b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


def _capture_raw_logs(monkeypatch) -> list[str]:
    """Lines handed to append_log, before logredact masks them."""
    raw: list[str] = []
    real = JobRunner.append_log

    def wrapped(self, job, line):
        raw.append(line)
        return real(self, job, line)

    monkeypatch.setattr(JobRunner, "append_log", wrapped)
    return raw


def _assert_absent(blob: str, *needles: str) -> None:
    for needle in needles:
        assert needle not in blob, needle


def _key_lines(private: str) -> list[str]:
    return [line for line in private.splitlines() if len(line) > 20]


def _plant(config_dir: Path) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "kubesecrets.yaml").write_text(GENERATED, encoding="utf-8")
    (config_dir / "helm-chart-versions.yaml").write_text(
        "charts:\n  glance: 9\n", encoding="utf-8"
    )
    inventory = config_dir / "inventory"
    inventory.mkdir()
    (inventory / "inventory.yaml").write_text("operator: keep\n", encoding="utf-8")
    ssh_dir = config_dir / ".ssh"
    ssh_dir.mkdir()
    (ssh_dir / "known_hosts").write_text("preexisting-known-host\n", encoding="utf-8")


def _make_env(db, config_dir: Path, *, dry_run: bool) -> Environment:
    env = Environment(
        name=f"lease-{uuid.uuid4().hex[:8]}",
        genestack_config_dir=str(config_dir),
        dry_run=dry_run,
    )
    db.add(env)
    db.flush()
    store_key_pair(env)
    db.commit()
    return env


def _run_push(db, env: Environment, doc: str = DOC):
    envconfig_service.put_version(db, env, doc, "lease-test")
    db.commit()
    runner = JobRunner(db)
    job = runner.create_job(
        operation="genestack.config.push",
        params={},
        environment_id=env.id,
        created_by="lease-test",
    )
    db.commit()
    return runner.run_job(job)


def _ctx(config_dir: Path) -> EnvContext:
    return EnvContext(
        environment=None,
        genestack_root=config_dir,
        config_dir=config_dir.resolve(),
        dry_run=False,
        kubeconfig=None,
    )


def test_job_push_keeps_secrets_until_success(tmp_path, monkeypatch, caplog):
    """The file is present in the job body and gone after success."""
    caplog.set_level(logging.DEBUG)
    raw = _capture_raw_logs(monkeypatch)
    config_dir = tmp_path / "etc-genestack"
    _plant(config_dir)
    seen: dict[str, bool] = {}
    real = envconfig_service.push_rendered

    def wrapped(files, ctx, log=None, dry_run=True):
        result = real(files, ctx, log, dry_run)
        root = ctx.config_dir
        secret = root / "kubesecrets.yaml"
        assert secret.is_file()
        assert _b64(PASSWORD) in secret.read_text(encoding="utf-8")
        assert (root / ".ssh" / "id_ed25519").is_file()
        assert (root / ".ssh" / "known_hosts").is_file()
        charts = yaml.safe_load((root / "helm-chart-versions.yaml").read_text())
        assert charts["charts"]["glance"] == 9
        assert charts["charts"]["keystone"] == "1.2.3"
        assert (root / "inventory" / "inventory.yaml").read_text() == "operator: keep\n"
        seen["during"] = True
        return result

    monkeypatch.setattr(envconfig_service, "push_rendered", wrapped)

    db = SessionLocal()
    try:
        env = _make_env(db, config_dir, dry_run=False)
        private = get_decrypted_private_key(env)
        assert private
        job = _run_push(db, env)
        assert job.status == JobStatus.success, job.error
    finally:
        db.close()

    assert seen.get("during") is True
    root = config_dir.resolve()
    assert not (root / "kubesecrets.yaml").exists()
    assert not (root / ".ssh" / "id_ed25519").exists()
    assert not (root / ".ssh" / "id_ed25519.pub").exists()
    assert (root / ".ssh" / "known_hosts").read_text() == "preexisting-known-host\n"
    charts = yaml.safe_load((root / "helm-chart-versions.yaml").read_text())
    assert charts["charts"]["glance"] == 9
    assert charts["charts"]["keystone"] == "1.2.3"
    assert (root / "inventory" / "inventory.yaml").read_text() == "operator: keep\n"
    assert (root / ".genestack-manifest.yaml").is_file()
    backups = list((root / ".console-backup").glob("*/kubesecrets.yaml"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == GENERATED

    blob = "\n".join(raw) + "\n" + caplog.text
    _assert_absent(blob, PASSWORD, *_key_lines(private))


@pytest.mark.parametrize("boom", [RuntimeError, JobCancelledError])
def test_job_push_releases_after_error(tmp_path, monkeypatch, caplog, boom):
    """Failure and cancel both remove the files the job created."""
    caplog.set_level(logging.DEBUG)
    raw = _capture_raw_logs(monkeypatch)
    config_dir = tmp_path / "etc-genestack"
    _plant(config_dir)
    real = envconfig_service.push_rendered

    def wrapped(files, ctx, log=None, dry_run=True):
        real(files, ctx, log, dry_run)
        secret = ctx.config_dir / "kubesecrets.yaml"
        assert secret.is_file()
        assert _b64(PASSWORD) in secret.read_text(encoding="utf-8")
        assert (ctx.config_dir / ".ssh" / "id_ed25519").is_file()
        raise boom("stage failed")

    monkeypatch.setattr(envconfig_service, "push_rendered", wrapped)

    db = SessionLocal()
    try:
        env = _make_env(db, config_dir, dry_run=False)
        private = get_decrypted_private_key(env)
        assert private
        job = _run_push(db, env)
        assert job.status == JobStatus.failed
        assert job.error == "stage failed"
        assert PASSWORD not in (job.error or "")
    finally:
        db.close()

    root = config_dir.resolve()
    assert not (root / "kubesecrets.yaml").exists()
    assert not (root / ".ssh" / "id_ed25519").exists()
    assert (root / ".ssh" / "known_hosts").is_file()
    assert (root / "helm-chart-versions.yaml").is_file()
    assert (root / "inventory" / "inventory.yaml").is_file()
    assert (root / ".genestack-manifest.yaml").is_file()
    blob = "\n".join(raw) + "\n" + caplog.text
    _assert_absent(blob, PASSWORD, *_key_lines(private))


def test_dry_run_job_writes_and_deletes_nothing(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    raw = _capture_raw_logs(monkeypatch)
    config_dir = tmp_path / "etc-genestack"
    config_dir.mkdir()
    (config_dir / "kubesecrets.yaml").write_text(GENERATED, encoding="utf-8")

    db = SessionLocal()
    try:
        env = _make_env(db, config_dir, dry_run=True)
        private = get_decrypted_private_key(env)
        assert private
        job = _run_push(db, env)
        assert job.status == JobStatus.success, job.error
    finally:
        db.close()

    assert (config_dir / "kubesecrets.yaml").read_text(encoding="utf-8") == GENERATED
    names = {path.relative_to(config_dir).as_posix() for path in config_dir.rglob("*")}
    assert names == {"kubesecrets.yaml"}
    blob = "\n".join(raw) + "\n" + caplog.text
    _assert_absent(blob, PASSWORD, *_key_lines(private))


def test_push_outside_a_job_removes_secrets_before_return(tmp_path, monkeypatch):
    """A push that is not a job does not leave kubesecrets.yaml behind."""
    config_dir = tmp_path / "etc-genestack"
    config_dir.mkdir()
    password_b64 = _b64(PASSWORD)
    secret = (
        "---\n"
        "apiVersion: v1\n"
        "kind: Secret\n"
        "metadata:\n"
        "  name: netapp-cinder-backend\n"
        "  namespace: openstack\n"
        "type: Opaque\n"
        "data:\n"
        f"  password: {password_b64}\n"
    )
    files = {
        "kubesecrets.yaml": secret,
        "helm-chart-versions.yaml": "charts:\n  keystone: 1.2.3\n",
    }
    seen: list[bytes] = []
    real_write = Path.write_bytes

    def spy(self, data):
        real_write(self, data)
        if self.name == "kubesecrets.yaml":
            assert self.is_file()
            seen.append(self.read_bytes())

    monkeypatch.setattr(Path, "write_bytes", spy)
    assert secret_lease.current() is None
    envconfig_service.push_rendered(files, _ctx(config_dir), None, False)

    assert seen and password_b64.encode() in seen[0]
    assert secret_lease.current() is None
    root = config_dir.resolve()
    assert not (root / "kubesecrets.yaml").exists()
    assert "keystone" in (root / "helm-chart-versions.yaml").read_text()


def test_dry_run_push_outside_a_job_creates_nothing(tmp_path):
    config_dir = tmp_path / "etc-genestack"
    config_dir.mkdir()
    (config_dir / "kubesecrets.yaml").write_text(GENERATED, encoding="utf-8")
    envconfig_service.push_rendered(
        {"kubesecrets.yaml": "password: " + PASSWORD + "\n"},
        _ctx(config_dir),
        None,
        True,
    )
    assert (config_dir / "kubesecrets.yaml").read_text(encoding="utf-8") == GENERATED
    names = {path.name for path in config_dir.rglob("*")}
    assert names == {"kubesecrets.yaml"}


def test_lease_refuses_protected_paths(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.WARNING)
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    fake_home = tmp_path / "fake-home"
    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))
    helm = config_dir / "helm-chart-versions.yaml"
    helm.write_text(PASSWORD, encoding="utf-8")
    manifest = config_dir / ".genestack-manifest.yaml"
    manifest.write_text("pushed_files: []\n", encoding="utf-8")
    inventory = config_dir / "inventory"
    inventory.mkdir()
    (inventory / "inventory.yaml").write_text("operator: keep\n", encoding="utf-8")
    home_key = fake_home / ".ssh" / "id_ed25519"
    home_key.parent.mkdir(parents=True)
    home_key.write_text("do-not-touch\n", encoding="utf-8")
    staged = tmp_path / "data" / "kubeconfigs" / "env.yaml"
    staged.parent.mkdir(parents=True)
    staged.write_text("staged-kube\n", encoding="utf-8")

    removed: list[str] = []
    real_unlink = Path.unlink

    def spy_unlink(self, missing_ok=False):
        removed.append(str(self))
        return real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", spy_unlink)
    lease = secret_lease.SecretLease(config_dir=config_dir)
    for path in (helm, manifest, inventory, home_key, staged, config_dir / ".." / "x"):
        lease.note(path, kind="local")
    lease.release()

    assert removed == []
    assert helm.read_text(encoding="utf-8") == PASSWORD
    assert manifest.is_file()
    assert inventory.is_dir()
    assert (inventory / "inventory.yaml").read_text() == "operator: keep\n"
    assert home_key.read_text() == "do-not-touch\n"
    assert staged.read_text() == "staged-kube\n"
    assert PASSWORD not in caplog.text
    assert str(helm) in caplog.text


def test_delete_failure_logs_path_not_contents(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.WARNING)
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    target = config_dir / "kubesecrets.yaml"
    target.write_text(PASSWORD, encoding="utf-8")

    def boom(self, missing_ok=False):  # noqa: ARG001
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "unlink", boom)
    lines: list[str] = []
    lease = secret_lease.SecretLease(config_dir=config_dir)
    lease.note(target, kind="local")
    lease.release(lines.append)

    assert target.read_text(encoding="utf-8") == PASSWORD
    assert lines
    assert str(target) in lines[0]
    assert "permission denied" in lines[0]
    assert PASSWORD not in lines[0]
    assert str(target) in caplog.text
    assert "permission denied" in caplog.text
    assert PASSWORD not in caplog.text


def test_ssh_rm_is_one_path(tmp_path, monkeypatch):
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    target = config_dir / "kubesecrets.yaml"
    calls: list[tuple[list[str], dict]] = []

    def fake_run(argv, **kwargs):
        calls.append((list(argv), kwargs))

        class _Proc:
            returncode = 0
            stdout = ""
            stderr = ""

        return _Proc()

    monkeypatch.setattr(bridge.subprocess, "run", fake_run)
    lease = secret_lease.SecretLease(config_dir=config_dir)
    lease.note(target, kind="ssh", ssh_target="deploy@10.0.0.5")
    lease.release()

    assert len(calls) == 1
    argv, kwargs = calls[0]
    remote = argv[-1]
    assert argv[0] == "ssh"
    assert remote == shlex.join(["rm", "-f", "--", str(target)])
    assert "-rf" not in argv
    assert "bash" not in argv
    assert "bash" not in remote
    assert PASSWORD not in " ".join(argv)
    assert kwargs.get("env") is None
    assert kwargs.get("input") is None


def test_agent_rm_is_one_path(tmp_path, monkeypatch):
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    target = config_dir / "kubesecrets.yaml"
    calls: list[tuple] = []

    def fake_exec(env_id, kind, payload, **kwargs):
        calls.append((env_id, kind, payload, kwargs))
        return {"rc": 0}

    monkeypatch.setattr("app.services.agent_relay.agent_exec", fake_exec)
    lease = secret_lease.SecretLease(config_dir=config_dir)
    lease.note(str(target), kind="agent", agent_env_id="env-1")
    lease.release()

    assert len(calls) == 1
    env_id, kind, payload, kwargs = calls[0]
    assert env_id == "env-1"
    assert kind == "run_command"
    assert payload["cmd"] == ["rm", "-f", "--", str(target)]
    assert payload["env"] == {}
    assert kwargs["log_cb"] is None
    assert PASSWORD not in str(calls)


def test_agent_rm_failure_logs_path(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.WARNING)
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    target = config_dir / "kubesecrets.yaml"

    def fake_exec(env_id, kind, payload, **kwargs):  # noqa: ARG001
        return {"rc": 1, "stderr": "permission denied"}

    monkeypatch.setattr("app.services.agent_relay.agent_exec", fake_exec)
    lines: list[str] = []
    lease = secret_lease.SecretLease(config_dir=config_dir)
    lease.note(target, kind="agent", agent_env_id="env-1")
    lease.release(lines.append)

    assert lines
    assert str(target) in lines[0]
    assert "permission denied" in lines[0]
    assert PASSWORD not in lines[0]
    assert PASSWORD not in caplog.text


def test_fetch_kubeconfig_stores_and_job_lease_removes_it(tmp_path, monkeypatch):
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    logs: list[str] = []

    def fake_run(cmd, **kwargs):
        assert list(cmd)[:2] == ["cat", "/etc/kubernetes/admin.conf"]
        assert kwargs.get("log") is None
        return {"returncode": 0, "stdout": ADMIN_CONF, "dry_run": False}

    monkeypatch.setattr(bridge, "run_command", fake_run)
    db = SessionLocal()
    lease = None
    try:
        env = Environment(
            name=f"lease-kube-{uuid.uuid4().hex[:8]}",
            genestack_config_dir=str(config_dir),
            dry_run=False,
        )
        db.add(env)
        db.commit()
        ctx = build_context(env, get_settings())
        lease = secret_lease.begin(ctx)
        _fetch_kubeconfig(
            CP_DOC, env, ctx, logs.append, dry_run=False, timeout=30, db=db
        )
        target = ctx.config_dir / "inventory" / "artifacts" / "admin.conf"
        assert target.is_file()
        assert (target.stat().st_mode & 0o777) == 0o600
        text = target.read_text(encoding="utf-8")
        assert "server: https://10.0.0.11:6443" in text
        assert "127.0.0.1" not in text
        assert SENTINEL in text
        assert "certificate-authority-data" in text
        lease.release()
        assert not target.exists()
        plain = decrypt_secret(env.kubeconfig_data)
        assert plain == text
    finally:
        if lease is not None:
            secret_lease.end(lease)
        db.close()

    blob = "\n".join(logs)
    assert SENTINEL not in blob
    assert "certificate-authority-data" not in blob
    assert PASSWORD not in blob


def test_fetch_skips_preexisting_kubeconfig(tmp_path, monkeypatch):
    config_dir = tmp_path / "cfg"
    target = config_dir / "inventory" / "artifacts" / "admin.conf"
    target.parent.mkdir(parents=True)
    target.write_text("original\n", encoding="utf-8")

    def fake_run(cmd, **kwargs):  # noqa: ARG001
        raise AssertionError(f"should not run {cmd}")

    monkeypatch.setattr(bridge, "run_command", fake_run)
    db = SessionLocal()
    lease = None
    try:
        env = Environment(
            name=f"lease-kube-keep-{uuid.uuid4().hex[:8]}",
            genestack_config_dir=str(config_dir),
            kubeconfig_path=str(target),
            dry_run=False,
        )
        db.add(env)
        db.commit()
        ctx = build_context(env, get_settings())
        lease = secret_lease.begin(ctx)
        _fetch_kubeconfig(
            CP_DOC, env, ctx, lambda _line: None, dry_run=False, timeout=30, db=db
        )
        lease.release()
        assert env.kubeconfig_data in (None, "")
    finally:
        if lease is not None:
            secret_lease.end(lease)
        db.close()
    assert target.read_text(encoding="utf-8") == "original\n"


def test_remember_without_a_job_deletes_immediately(tmp_path):
    config_dir = tmp_path / "cfg"
    target = config_dir / "inventory" / "artifacts" / "admin.conf"
    target.parent.mkdir(parents=True)
    target.write_text(SENTINEL + "\n", encoding="utf-8")
    kept = config_dir / "helm-chart-versions.yaml"
    kept.write_text("charts: {}\n", encoding="utf-8")
    assert secret_lease.current() is None
    secret_lease.remember(target, config_dir=config_dir)
    assert not target.exists()
    assert kept.is_file()
    assert secret_lease.current() is None


def test_dry_lease_deletes_nothing(tmp_path):
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    target = config_dir / "kubesecrets.yaml"
    target.write_text(GENERATED, encoding="utf-8")
    lease = secret_lease.SecretLease(dry_run=True, config_dir=config_dir)
    lease.note(target, kind="local")
    lease.release()
    assert target.read_text(encoding="utf-8") == GENERATED
    assert lease._entries == []
