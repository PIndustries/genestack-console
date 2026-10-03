"""genestack.config.push job tests (Phase 6, Milestone B)."""

from __future__ import annotations

import base64
import os
import re
import stat
import threading
import uuid
from pathlib import Path

import yaml
from sqlalchemy import select

from app.db import SessionLocal
from app.models import AgentCommand, Environment, JobStatus
from app.services import envconfig as envconfig_service
from app.services import genestack_bridge as bridge
from app.services.job_runner import JobRunner
from tests.test_agent_relay import (
    _create_token,
    _fake_exec_agent_loop,
    _handshake,
    _start_relay_pump,
)

DOC = """\
provider: kubespray
components:
  keystone: true
helm_overrides:
  keystone:
    replicas: 3
"""

SYNC_DOC = """\
provider: kubespray
deploy:
  ssh_host: deployer.example.com
  ssh_user: deploy
maas:
  url: http://maas.example.com:5240
  api_key: consumer:token:secret
"""


def _suffix() -> str:
    return uuid.uuid4().hex[:8]


def _create_env(client, headers, **fields) -> dict:
    resp = client.post(
        "/api/v1/environments",
        headers=headers,
        json={"name": f"env-push-{_suffix()}", **fields},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _put_doc(client, headers, env_id, doc=DOC):
    resp = client.put(
        f"/api/v1/environments/{env_id}/config",
        headers=headers,
        json={"yaml_text": doc},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["version"]


def _push_job(client, headers, env_id, run_sync=True):
    resp = client.post(
        f"/api/v1/environments/{env_id}/jobs",
        headers=headers,
        json={"operation": "genestack.config.push", "params": {}, "run_sync": run_sync},
    )
    return resp


def _capture_local_writes(monkeypatch) -> list[tuple[str, bytes]]:
    """Record bytes passed to the local writer. The job then removes secret files."""
    captured: list[tuple[str, bytes]] = []
    real = envconfig_service._push_file_local

    def wrapped(target, backup, data, log):
        real(target, backup, data, log)
        captured.append((str(target), bytes(data)))

    monkeypatch.setattr(envconfig_service, "_push_file_local", wrapped)
    return captured


def _kubesecret_writes(captured: list[tuple[str, bytes]], config_dir) -> list[bytes]:
    needle = (Path(config_dir) / "kubesecrets.yaml").resolve()
    return [data for path, data in captured if Path(path).resolve() == needle]


def test_push_dry_run_logs_files_writes_nothing(client, admin_headers, tmp_path):
    """Global test config is dry_run=True: the plan is logged, nothing written."""
    config_dir = tmp_path / "etc-genestack"
    config_dir.mkdir()
    env = _create_env(client, admin_headers, genestack_config_dir=str(config_dir))
    version = _put_doc(client, admin_headers, env["id"])

    resp = _push_job(client, admin_headers, env["id"])
    assert resp.status_code == 201, resp.text
    job = resp.json()
    assert job["status"] == "success", job["error"]

    log_text = client.get(f"/api/v1/jobs/{job['id']}", headers=admin_headers).json()[
        "log_text"
    ]
    assert f"config version {version}" in log_text
    assert "[dry-run] would write" in log_text
    assert "bytes" in log_text
    # Nothing written, not even backup dirs
    assert list(config_dir.rglob("*")) == []


def test_push_local_writes_files_and_backs_up(client, admin_headers, tmp_path):
    config_dir = tmp_path / "etc-genestack"
    config_dir.mkdir()
    # Pre-existing file that the push will overwrite -> must be backed up
    (config_dir / "provider").write_text("old-provider\n", encoding="utf-8")

    env = _create_env(
        client, admin_headers, genestack_config_dir=str(config_dir), dry_run=False
    )
    _put_doc(client, admin_headers, env["id"])

    resp = _push_job(client, admin_headers, env["id"])
    assert resp.status_code == 201, resp.text
    job = resp.json()
    assert job["status"] == "success", job["error"]

    assert (config_dir / "provider").read_text(encoding="utf-8") == "kubespray\n"
    assert yaml.safe_load((config_dir / "openstack-components.yaml").read_text()) == {
        "components": {"keystone": True}
    }
    overrides = config_dir / "helm-configs" / "keystone" / "console-rendered.yaml"
    assert yaml.safe_load(overrides.read_text()) == {"replicas": 3}

    # Files are written 0644
    mode = stat.S_IMODE(os.stat(config_dir / "provider").st_mode)
    assert mode == 0o644

    # The pre-existing provider file was backed up under .console-backup/<ts>/
    backups = list((config_dir / ".console-backup").glob("*/provider"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "old-provider\n"

    log_text = client.get(f"/api/v1/jobs/{job['id']}", headers=admin_headers).json()[
        "log_text"
    ]
    assert "[backup]" in log_text
    assert "[write]" in log_text


def test_push_without_config_document_fails(client, admin_headers, tmp_path):
    config_dir = tmp_path / "etc-genestack"
    config_dir.mkdir()
    env = _create_env(client, admin_headers, genestack_config_dir=str(config_dir))

    resp = _push_job(client, admin_headers, env["id"])
    assert resp.status_code == 201, resp.text
    job = resp.json()
    assert job["status"] == "failed"
    assert "no config document" in job["error"]


def test_push_without_config_dir_succeeds_local_hub(client, admin_headers):
    """Push without config_dir succeeds as local-hub no-op."""
    env = _create_env(client, admin_headers, dry_run=False)
    _put_doc(client, admin_headers, env["id"])

    resp = _push_job(client, admin_headers, env["id"])
    job = resp.json()
    assert job["status"] == "success"  # local hub: no remote push needed


def test_push_syncs_deploy_onto_environment(client, admin_headers, tmp_path):
    config_dir = tmp_path / "etc-genestack"
    config_dir.mkdir()
    env = _create_env(
        client, admin_headers, genestack_config_dir=str(config_dir), dry_run=False
    )
    # A leftover maas block in the document must not fail the push.
    _put_doc(client, admin_headers, env["id"], SYNC_DOC)

    resp = _push_job(client, admin_headers, env["id"])
    assert resp.json()["status"] == "success", resp.json()["error"]

    db = SessionLocal()
    try:
        row = db.get(Environment, env["id"])
        assert row.deployer_ssh_host == "deployer.example.com"
        assert row.deployer_ssh_user == "deploy"
        assert not hasattr(row, "maas_url")
    finally:
        db.close()


def test_push_records_audit_with_version(client, admin_headers, tmp_path):
    config_dir = tmp_path / "etc-genestack"
    config_dir.mkdir()
    env = _create_env(client, admin_headers, genestack_config_dir=str(config_dir))
    version = _put_doc(client, admin_headers, env["id"])

    resp = _push_job(client, admin_headers, env["id"])
    assert resp.json()["status"] == "success", resp.json()["error"]

    audit = client.get(
        "/api/v1/audit",
        headers=admin_headers,
        params={"environment_id": env["id"], "action": "env.config.push"},
    )
    assert audit.status_code == 200, audit.text
    entries = [e for e in audit.json() if e["action"] == "env.config.push"]
    assert entries, "expected an env.config.push audit entry"
    assert entries[0]["details"]["version"] == version


def test_push_conflict_409(client, admin_headers, tmp_path):
    """A queued mutating job blocks a second mutating job for the same env."""
    config_dir = tmp_path / "etc-genestack"
    config_dir.mkdir()
    env = _create_env(client, admin_headers, genestack_config_dir=str(config_dir))
    _put_doc(client, admin_headers, env["id"])

    # Leave a mutating job queued (not run) for this env
    first = _push_job(client, admin_headers, env["id"], run_sync=False)
    assert first.status_code == 201, first.text
    assert first.json()["status"] == "queued"

    second = _push_job(client, admin_headers, env["id"])
    assert second.status_code == 409, second.text


def test_push_viewer_cannot_create_job(client, admin_headers, viewer_headers, tmp_path):
    config_dir = tmp_path / "etc-genestack"
    config_dir.mkdir()
    env = _create_env(client, admin_headers, genestack_config_dir=str(config_dir))
    resp = _push_job(client, viewer_headers, env["id"])
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# SSH executor push (service level, monkeypatched subprocess)
# ---------------------------------------------------------------------------


def test_push_ssh_builds_base64_write_commands(monkeypatch, tmp_path):
    captured: list[list[str]] = []

    class _Proc:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(argv, **kwargs):  # noqa: ARG001
        captured.append(list(argv))
        return _Proc()

    monkeypatch.setattr(bridge.subprocess, "run", fake_run)

    db = SessionLocal()
    try:
        env = Environment(
            name=f"env-push-ssh-{_suffix()}",
            deployer_ssh_host="deployer.example.com",
            deployer_ssh_user="deploy",
            genestack_config_dir="/etc/genestack",
            dry_run=False,
        )
        db.add(env)
        db.commit()
        envconfig_service.put_version(db, env, DOC, "ssh-test")
        db.commit()

        runner = JobRunner(db)
        job = runner.create_job(
            operation="genestack.config.push",
            params={},
            environment_id=env.id,
            created_by="ssh-test",
        )
        db.commit()
        job = runner.run_job(job)
        assert job.status == JobStatus.success, job.error
    finally:
        db.close()

    # One ssh invocation per rendered file (DOC renders 3) plus the
    # .genestack-manifest.yaml (previous-push prune bookkeeping)
    writes = [argv for argv in captured if "base64 -d" in argv[10]]
    assert len(writes) == 4
    for argv in writes:
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
        remote = argv[10]
        assert "base64 -d >" in remote
        assert "chmod 0644" in remote
        # Pre-existing files are copied into .console-backup/<ts>/ first
        assert ".console-backup/" in remote
        assert "cp -n" in remote
    remotes = " ".join(argv[10] for argv in captured)
    assert "/etc/genestack/provider" in remotes
    assert "/etc/genestack/openstack-components.yaml" in remotes
    assert "/etc/genestack/helm-configs/keystone/console-rendered.yaml" in remotes
    assert "/etc/genestack/.genestack-manifest.yaml" in remotes


# ---------------------------------------------------------------------------
# kubesecrets.yaml merge on push (no mass rotation)
# ---------------------------------------------------------------------------


def _b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


# Shape mirrors bin/create-secrets.sh output (the "generated" secrets file)
GENERATED_KUBESECRETS = f"""\
---
apiVersion: v1
kind: Secret
metadata:
  name: mariadb
  namespace: openstack
type: Opaque
data:
  root-password: {_b64("generated-root-pw")}
  password: {_b64("generated-mariadb-pw")}
---
apiVersion: v1
kind: Secret
metadata:
  name: keystone-rabbitmq-password
  namespace: openstack
type: Opaque
data:
  username: {_b64("keystone")}
  password: {_b64("generated-rabbitmq-pw")}
"""

SECRETS_DOC = """\
secrets:
  netapp-cinder-backend:
    data:
      username: admin
      password: s3cret
  keystone-rabbitmq-password:
    data:
      password: console-rabbitmq-pw
"""


def _manifests_by_name(text: str) -> dict:
    return {d["metadata"]["name"]: d for d in yaml.safe_load_all(text)}


def test_push_merges_kubesecrets_local(client, admin_headers, tmp_path, monkeypatch):
    """Generated entries survive the merge; the job then removes the file."""
    config_dir = tmp_path / "etc-genestack"
    config_dir.mkdir()
    (config_dir / "kubesecrets.yaml").write_text(
        GENERATED_KUBESECRETS, encoding="utf-8"
    )
    captured = _capture_local_writes(monkeypatch)

    env = _create_env(
        client, admin_headers, genestack_config_dir=str(config_dir), dry_run=False
    )
    _put_doc(client, admin_headers, env["id"], SECRETS_DOC)

    resp = _push_job(client, admin_headers, env["id"])
    assert resp.status_code == 201, resp.text
    job = resp.json()
    assert job["status"] == "success", job["error"]

    writes = _kubesecret_writes(captured, config_dir)
    assert len(writes) == 1
    merged = _manifests_by_name(writes[0].decode())
    # The deploy host does not keep the merged file after the job.
    assert not (config_dir / "kubesecrets.yaml").exists()

    # Generated entry untouched (the no-mass-rotation rule)
    assert merged["mariadb"]["data"] == {
        "root-password": _b64("generated-root-pw"),
        "password": _b64("generated-mariadb-pw"),
    }
    # Console entry added, namespace defaulted, values base64 plaintext
    assert merged["netapp-cinder-backend"] == {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": "netapp-cinder-backend", "namespace": "openstack"},
        "type": "Opaque",
        "data": {"username": _b64("admin"), "password": _b64("s3cret")},
    }
    # Name conflict: the console-rendered manifest wins wholesale
    assert merged["keystone-rabbitmq-password"]["data"] == {
        "password": _b64("console-rabbitmq-pw")
    }
    # The pre-merge file was backed up
    backups = list((config_dir / ".console-backup").glob("*/kubesecrets.yaml"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == GENERATED_KUBESECRETS


def test_push_kubesecrets_written_when_absent_local(
    client, admin_headers, tmp_path, monkeypatch
):
    """No existing file -> the rendered bytes are the doc, then the job removes them."""
    config_dir = tmp_path / "etc-genestack"
    config_dir.mkdir()
    captured = _capture_local_writes(monkeypatch)
    env = _create_env(
        client, admin_headers, genestack_config_dir=str(config_dir), dry_run=False
    )
    _put_doc(client, admin_headers, env["id"], SECRETS_DOC)

    resp = _push_job(client, admin_headers, env["id"])
    assert resp.json()["status"] == "success", resp.json()["error"]

    writes = _kubesecret_writes(captured, config_dir)
    assert len(writes) == 1
    merged = _manifests_by_name(writes[0].decode())
    assert set(merged) == {"netapp-cinder-backend", "keystone-rabbitmq-password"}
    assert not (config_dir / "kubesecrets.yaml").exists()


def test_push_merges_kubesecrets_ssh(monkeypatch, tmp_path):
    """Remote path: one ssh cat to read, merged content written back, log redacted."""
    captured: list[list[str]] = []

    class _Proc:
        def __init__(self, returncode=0, stdout=""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = ""

    def fake_run(argv, **kwargs):  # noqa: ARG001
        captured.append(list(argv))
        remote = argv[10]
        if remote.startswith("cat "):
            return _Proc(stdout=GENERATED_KUBESECRETS)
        return _Proc()

    monkeypatch.setattr(bridge.subprocess, "run", fake_run)

    db = SessionLocal()
    try:
        env = Environment(
            name=f"env-push-ssh-secrets-{_suffix()}",
            deployer_ssh_host="deployer.example.com",
            deployer_ssh_user="deploy",
            genestack_config_dir="/etc/genestack",
            dry_run=False,
        )
        db.add(env)
        db.commit()
        envconfig_service.put_version(db, env, SECRETS_DOC, "ssh-test")
        db.commit()

        runner = JobRunner(db)
        job = runner.create_job(
            operation="genestack.config.push",
            params={},
            environment_id=env.id,
            created_by="ssh-test",
        )
        db.commit()
        job = runner.run_job(job)
        assert job.status == JobStatus.success, job.error
        log_text = job.log_text or ""
    finally:
        db.close()

    # One remote read of the existing file, then one write. The manifest read
    # (a separate `cat`) is excluded.
    cats = [
        argv
        for argv in captured
        if argv[10].startswith("cat ") and "kubesecrets.yaml" in argv[10]
    ]
    assert len(cats) == 1
    assert cats[0][10] == "cat /etc/genestack/kubesecrets.yaml"

    writes = [
        argv
        for argv in captured
        if "kubesecrets.yaml" in argv[10] and "base64 -d" in argv[10]
    ]
    assert len(writes) == 1
    match = re.search(r"echo (\S+) \| base64 -d", writes[0][10])
    payload_b64 = match.group(1)
    merged = _manifests_by_name(base64.b64decode(payload_b64).decode())
    assert merged["mariadb"]["data"]["password"] == _b64("generated-mariadb-pw")
    assert merged["netapp-cinder-backend"]["data"]["password"] == _b64("s3cret")

    # The base64 payload (plaintext secrets) never lands in the job log
    assert payload_b64 not in log_text
    assert "<redacted>" in log_text


# ---------------------------------------------------------------------------
# helm-chart-versions.yaml merge on push (doc pins overlay the full file)
# ---------------------------------------------------------------------------


# Shape mirrors the repo's helm-chart-versions.yaml bootstrap copies into the
# config dir: a top-level charts: mapping of chart name -> version.
FULL_CHART_VERSIONS = """\
---
charts:
  mariadb-operator: 0.38.1
  keystone: 2026.1.8+db238e7c3
  glance: 2026.1.9+7cce5ac45
"""

CHARTS_DOC = """\
chart_versions:
  keystone: 2026.1.9+abcdef123
  cinder: 2026.1.9+a2a343968
"""


def test_push_merges_chart_versions_local(client, admin_headers, tmp_path):
    """Doc charts overlay the existing file: other keys preserved, doc wins."""
    config_dir = tmp_path / "etc-genestack"
    config_dir.mkdir()
    (config_dir / "helm-chart-versions.yaml").write_text(
        FULL_CHART_VERSIONS, encoding="utf-8"
    )

    env = _create_env(
        client, admin_headers, genestack_config_dir=str(config_dir), dry_run=False
    )
    _put_doc(client, admin_headers, env["id"], CHARTS_DOC)

    resp = _push_job(client, admin_headers, env["id"])
    assert resp.status_code == 201, resp.text
    job = resp.json()
    assert job["status"] == "success", job["error"]

    merged = yaml.safe_load(
        (config_dir / "helm-chart-versions.yaml").read_text(encoding="utf-8")
    )
    assert merged["charts"] == {
        # Existing charts the doc does not pin survive untouched
        "mariadb-operator": "0.38.1",
        "glance": "2026.1.9+7cce5ac45",
        # Conflict: the doc's pin wins per key
        "keystone": "2026.1.9+abcdef123",
        # New doc-only chart is added
        "cinder": "2026.1.9+a2a343968",
    }

    # The pre-merge file was backed up
    backups = list((config_dir / ".console-backup").glob("*/helm-chart-versions.yaml"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == FULL_CHART_VERSIONS

    log_text = client.get(f"/api/v1/jobs/{job['id']}", headers=admin_headers).json()[
        "log_text"
    ]
    assert "writing partial file" not in log_text


def test_push_chart_versions_written_when_absent_local(client, admin_headers, tmp_path):
    """No existing file -> partial doc-only write with a job-log warning."""
    config_dir = tmp_path / "etc-genestack"
    config_dir.mkdir()
    env = _create_env(
        client, admin_headers, genestack_config_dir=str(config_dir), dry_run=False
    )
    _put_doc(client, admin_headers, env["id"], CHARTS_DOC)

    resp = _push_job(client, admin_headers, env["id"])
    assert resp.status_code == 201, resp.text
    job = resp.json()
    assert job["status"] == "success", job["error"]

    rendered = yaml.safe_load(
        (config_dir / "helm-chart-versions.yaml").read_text(encoding="utf-8")
    )
    assert rendered == {
        "charts": {"keystone": "2026.1.9+abcdef123", "cinder": "2026.1.9+a2a343968"}
    }

    log_text = client.get(f"/api/v1/jobs/{job['id']}", headers=admin_headers).json()[
        "log_text"
    ]
    assert (
        "helm-chart-versions.yaml not present on target; writing partial file with 2 chart(s)"
        in log_text
    )


def test_push_merges_chart_versions_ssh(monkeypatch, tmp_path):
    """Remote path: one ssh cat to read, merged content written back."""
    captured: list[list[str]] = []

    class _Proc:
        def __init__(self, returncode=0, stdout=""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = ""

    def fake_run(argv, **kwargs):  # noqa: ARG001
        captured.append(list(argv))
        remote = argv[10]
        if remote.startswith("cat "):
            return _Proc(stdout=FULL_CHART_VERSIONS)
        return _Proc()

    monkeypatch.setattr(bridge.subprocess, "run", fake_run)

    db = SessionLocal()
    try:
        env = Environment(
            name=f"env-push-ssh-charts-{_suffix()}",
            deployer_ssh_host="deployer.example.com",
            deployer_ssh_user="deploy",
            genestack_config_dir="/etc/genestack",
            dry_run=False,
        )
        db.add(env)
        db.commit()
        envconfig_service.put_version(db, env, CHARTS_DOC, "ssh-test")
        db.commit()

        runner = JobRunner(db)
        job = runner.create_job(
            operation="genestack.config.push",
            params={},
            environment_id=env.id,
            created_by="ssh-test",
        )
        db.commit()
        job = runner.run_job(job)
        assert job.status == JobStatus.success, job.error
    finally:
        db.close()

    # One remote read of the existing file, then one write. The manifest read
    # (a separate `cat`) is excluded.
    cats = [
        argv
        for argv in captured
        if argv[10].startswith("cat ") and "helm-chart-versions.yaml" in argv[10]
    ]
    assert len(cats) == 1
    assert cats[0][10] == "cat /etc/genestack/helm-chart-versions.yaml"

    writes = [
        argv
        for argv in captured
        if "helm-chart-versions.yaml" in argv[10] and "base64 -d" in argv[10]
    ]
    assert len(writes) == 1
    match = re.search(r"echo (\S+) \| base64 -d", writes[0][10])
    merged = yaml.safe_load(base64.b64decode(match.group(1)).decode())
    assert merged["charts"] == {
        "mariadb-operator": "0.38.1",
        "glance": "2026.1.9+7cce5ac45",
        "keystone": "2026.1.9+abcdef123",
        "cinder": "2026.1.9+a2a343968",
    }


def test_push_ssh_failure_fails_job(monkeypatch, tmp_path):
    class _Proc:
        returncode = 1
        stdout = ""
        stderr = "permission denied"

    def fake_run(argv, **kwargs):  # noqa: ARG001
        return _Proc()

    monkeypatch.setattr(bridge.subprocess, "run", fake_run)

    db = SessionLocal()
    try:
        env = Environment(
            name=f"env-push-ssh-fail-{_suffix()}",
            deployer_ssh_host="deployer.example.com",
            genestack_config_dir="/etc/genestack",
            dry_run=False,
        )
        db.add(env)
        db.commit()
        envconfig_service.put_version(db, env, DOC, "ssh-test")
        db.commit()

        runner = JobRunner(db)
        job = runner.create_job(
            operation="genestack.config.push",
            params={},
            environment_id=env.id,
            created_by="ssh-test",
        )
        db.commit()
        job = runner.run_job(job)
        assert job.status == JobStatus.failed
        assert "failed to write" in (job.error or "")
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Agent executor push (file_write relay rows)
# ---------------------------------------------------------------------------


def _agent_rows(env_id: str) -> list[AgentCommand]:
    db = SessionLocal()
    try:
        rows = db.scalars(
            select(AgentCommand).where(AgentCommand.environment_id == env_id)
        ).all()
        for row in rows:
            db.expunge(row)
        return list(rows)
    finally:
        db.close()


def test_push_via_agent_ships_file_write_rows(client, admin_headers, tmp_path):
    """Agent available: every rendered file ships as a file_write relay row."""
    config_dir = tmp_path / "etc-genestack"
    config_dir.mkdir()
    env = _create_env(
        client, admin_headers, genestack_config_dir=str(config_dir), dry_run=False
    )
    _put_doc(client, admin_headers, env["id"])
    token = _create_token(client, admin_headers, env["id"])
    url = f"/api/v1/agents/connect?token={token['token']}"

    stop = _start_relay_pump()
    frames: list = []
    try:
        with client.websocket_connect(url) as ws:
            _handshake(ws, token["token"])
            threading.Thread(
                target=_fake_exec_agent_loop, args=(ws, frames), daemon=True
            ).start()
            resp = _push_job(client, admin_headers, env["id"])
            assert resp.status_code == 201, resp.text
            job = resp.json()
            assert job["status"] == "success", job["error"]
    finally:
        stop.set()

    # The fake agent executed the writes locally.
    assert (config_dir / "provider").read_text(encoding="utf-8") == "kubespray\n"
    assert yaml.safe_load((config_dir / "openstack-components.yaml").read_text()) == {
        "components": {"keystone": True}
    }

    # One file_write row per rendered file (DOC renders 3 + the env's SSH key
    # pair) plus the .genestack-manifest.yaml bookkeeping file.
    writes = [row for row in _agent_rows(env["id"]) if row.kind == "file_write"]
    assert len(writes) == 6
    for row in writes:
        assert row.status == "done"
        assert row.result == {"rc": 0}
        payload = row.payload
        assert payload["path"].startswith(str(config_dir))
        assert ".console-backup/" in payload["backup_dir"]
        assert payload["mode"] == "0644"
        base64.b64decode(payload["b64"])  # valid base64
    paths = {row.payload["path"] for row in writes}
    assert str(config_dir / "provider") in paths
    assert str(config_dir / ".ssh" / "id_ed25519") in paths
    assert str(config_dir / ".ssh" / "id_ed25519.pub") in paths

    # The wire frames carried the same fields as the rows.
    fw_frames = [f for f in frames if f.get("type") == "file_write"]
    assert len(fw_frames) == 6
    assert {f["id"] for f in fw_frames} == {row.id for row in writes}

    log_text = client.get(f"/api/v1/jobs/{job['id']}", headers=admin_headers).json()[
        "log_text"
    ]
    assert "via agent" in log_text
    assert "[dry-run]" not in log_text


def test_push_via_agent_kubesecrets_redacted(client, admin_headers, tmp_path):
    """Secrets doc via agent: merged file written, b64 payload never logged."""
    config_dir = tmp_path / "etc-genestack"
    config_dir.mkdir()
    env = _create_env(
        client, admin_headers, genestack_config_dir=str(config_dir), dry_run=False
    )
    _put_doc(client, admin_headers, env["id"], SECRETS_DOC)
    token = _create_token(client, admin_headers, env["id"])
    url = f"/api/v1/agents/connect?token={token['token']}"

    stop = _start_relay_pump()
    frames: list = []
    try:
        with client.websocket_connect(url) as ws:
            _handshake(ws, token["token"])
            threading.Thread(
                target=_fake_exec_agent_loop, args=(ws, frames), daemon=True
            ).start()
            resp = _push_job(client, admin_headers, env["id"])
            assert resp.status_code == 201, resp.text
            job = resp.json()
            assert job["status"] == "success", job["error"]
    finally:
        stop.set()

    # Merge read got no usable existing file, so the file_write payload is the
    # rendered entries. The agent rm at job end removes that file.
    secret_rows = [
        row
        for row in _agent_rows(env["id"])
        if row.kind == "file_write" and row.payload["path"].endswith("kubesecrets.yaml")
    ]
    assert len(secret_rows) == 1
    merged = _manifests_by_name(
        base64.b64decode(secret_rows[0].payload["b64"]).decode()
    )
    assert merged["netapp-cinder-backend"]["data"] == {
        "username": _b64("admin"),
        "password": _b64("s3cret"),
    }
    assert not (config_dir / "kubesecrets.yaml").exists()

    # Redaction: the kubesecrets b64 payload appears in NO log line.
    secret_rows = [
        row
        for row in _agent_rows(env["id"])
        if row.kind == "file_write" and row.payload["path"].endswith("kubesecrets.yaml")
    ]
    assert len(secret_rows) == 1
    log_text = client.get(f"/api/v1/jobs/{job['id']}", headers=admin_headers).json()[
        "log_text"
    ]
    assert secret_rows[0].payload["b64"] not in log_text
    assert "s3cret" not in log_text
