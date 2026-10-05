"""genestack.deploy job tests — one-click push config + full pipeline."""

from __future__ import annotations

import threading
import time
import uuid
from pathlib import Path

from app.db import SessionLocal
from app.models import Environment
from app.services import deploy as deploy_service
from app.services import genestack_bridge as bridge
from app.services.crypto import decrypt_secret
from app.services.catalog import get_operation
from app.services.service_registry import PIPELINE_STAGES
from tests.test_agent_relay import (
    _create_token,
    _fake_exec_agent_loop,
    _handshake,
    _start_relay_pump,
)
from tests.test_ovh import (
    _byoi_env_setup,
    _mock_transport,
    _ovh_byoi_servers,
    _patch_client_factory,
)

DOC = """\
provider: kubespray
components:
  keystone: true
"""


def _suffix() -> str:
    return uuid.uuid4().hex[:8]


def _create_env(client, headers, **fields) -> dict:
    resp = client.post(
        "/api/v1/environments",
        headers=headers,
        json={"name": f"env-deploy-{_suffix()}", **fields},
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


def _deploy_job(client, headers, env_id, params=None, run_sync=True):
    body = {"operation": "genestack.deploy", "params": params or {}}
    if run_sync:
        body["run_sync"] = True
    return client.post(
        f"/api/v1/environments/{env_id}/jobs",
        headers=headers,
        json=body,
    )


def _job_log(client, headers, job_id) -> str:
    resp = client.get(f"/api/v1/jobs/{job_id}", headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()["log_text"]


def test_deploy_op_in_catalog():
    op = get_operation("genestack.deploy")
    assert op is not None
    assert op.required_role == "admin"
    assert op.mutating is True
    assert op.handler == "genestack_deploy"
    assert op.timeout_seconds == 21600


def test_deploy_dry_run_logs_push_and_all_stages_in_order(
    client, admin_headers, tmp_path
):
    """Global test config is dry_run=True: full plan logged, nothing executed."""
    config_dir = tmp_path / "etc-genestack"
    config_dir.mkdir()
    env = _create_env(client, admin_headers, genestack_config_dir=str(config_dir))
    version = _put_doc(client, admin_headers, env["id"])

    resp = _deploy_job(client, admin_headers, env["id"])
    assert resp.status_code == 201, resp.text
    job = resp.json()
    assert job["status"] == "success", job["error"]

    log_text = _job_log(client, admin_headers, job["id"])
    # Phase 1: push logged with version, nothing written
    assert f"[deploy] phase 1: push config version={version}" in log_text
    assert "[dry-run] would write" in log_text
    assert f"[deploy] pushed config version={version}" in log_text
    # Phase 2: every stage announced in PIPELINE_STAGES order
    total = len(PIPELINE_STAGES)
    positions = []
    for index, stage in enumerate(PIPELINE_STAGES, start=1):
        marker = f"[deploy] === stage {index}/{total}: {stage['id']}"
        pos = log_text.find(marker)
        assert pos != -1, f"missing stage marker: {marker}"
        positions.append(pos)
    assert positions == sorted(positions), "stages logged out of order"
    assert "testing skipped" in log_text or "run Tempest separately" in log_text
    assert "bin/install-tempest.sh" not in log_text
    # Nothing executed: config dir untouched
    assert list(config_dir.rglob("*")) == []


def test_deploy_without_config_document_fails(client, admin_headers, tmp_path):
    config_dir = tmp_path / "etc-genestack"
    config_dir.mkdir()
    env = _create_env(client, admin_headers, genestack_config_dir=str(config_dir))

    resp = _deploy_job(client, admin_headers, env["id"])
    assert resp.status_code == 201, resp.text
    job = resp.json()
    assert job["status"] == "failed"
    assert "no config document" in job["error"]


def test_deploy_without_config_dir_attempts_local_execution(client, admin_headers):
    """Deploy without config_dir attempts local execution and may fail on missing scripts."""
    env = _create_env(client, admin_headers, dry_run=False)
    _put_doc(client, admin_headers, env["id"])

    resp = _deploy_job(client, admin_headers, env["id"])
    job = resp.json()
    # Local hub attempts deploy but may fail on missing scripts (rc=127 "command not found")
    assert job["status"] == "failed"
    assert "rc=127" in job["error"] or "cert-manager" in job["error"]


def test_deploy_param_dry_run_false_cannot_force_live_on_dry_run_env(
    client, admin_headers, tmp_path
):
    """dry_run clamp: params.dry_run=false never overrides a dry-run-pinned env."""
    config_dir = tmp_path / "etc-genestack"
    config_dir.mkdir()
    env = _create_env(
        client, admin_headers, genestack_config_dir=str(config_dir), dry_run=True
    )
    _put_doc(client, admin_headers, env["id"])

    resp = _deploy_job(client, admin_headers, env["id"], params={"dry_run": False})
    assert resp.status_code == 201, resp.text
    job = resp.json()
    assert job["status"] == "success", job["error"]
    assert job["dry_run"] is True, job  # still a rehearsal

    log_text = _job_log(client, admin_headers, job["id"])
    assert "[dry-run] would write" in log_text
    assert list(config_dir.rglob("*")) == []  # nothing executed


def test_deploy_param_dry_run_true_forces_rehearsal_on_wet_env(
    client, admin_headers, tmp_path
):
    """The clamp is one-directional: params.dry_run=true rehearses a live env."""
    config_dir = tmp_path / "etc-genestack"
    config_dir.mkdir()
    env = _create_env(
        client, admin_headers, genestack_config_dir=str(config_dir), dry_run=False
    )
    _put_doc(client, admin_headers, env["id"])

    resp = _deploy_job(client, admin_headers, env["id"], params={"dry_run": True})
    assert resp.status_code == 201, resp.text
    job = resp.json()
    assert job["status"] == "success", job["error"]
    assert job["dry_run"] is True, job

    log_text = _job_log(client, admin_headers, job["id"])
    assert "[dry-run] would write" in log_text
    assert list(config_dir.rglob("*")) == []


def test_deploy_stops_at_first_failing_stage(
    client, admin_headers, tmp_path, monkeypatch
):
    config_dir = tmp_path / "etc-genestack"
    config_dir.mkdir()
    env = _create_env(
        client, admin_headers, genestack_config_dir=str(config_dir), dry_run=False
    )
    _put_doc(client, admin_headers, env["id"])

    fail_stage_index = 2  # third stage ("operators") fails on its first item
    fail_stage = PIPELINE_STAGES[fail_stage_index]
    fail_script = fail_stage["items"][0]["script"]

    def fake_run_command(cmd, **kwargs):  # noqa: ARG001
        script = cmd[1] if len(cmd) > 1 else ""
        rc = 1 if script == fail_script else 0
        return {"returncode": rc, "dry_run": False, "message": f"rc={rc}"}

    monkeypatch.setattr(bridge, "run_command", fake_run_command)

    resp = _deploy_job(client, admin_headers, env["id"])
    assert resp.status_code == 201, resp.text
    job = resp.json()
    assert job["status"] == "failed"
    assert fail_stage["id"] in job["error"]
    assert fail_stage["items"][0]["name"] in job["error"]

    log_text = _job_log(client, admin_headers, job["id"])
    assert "[deploy] FAILED at" in log_text
    # Later stages never started
    later = PIPELINE_STAGES[fail_stage_index + 1]
    assert (
        f"=== stage {fail_stage_index + 2}/{len(PIPELINE_STAGES)}: {later['id']}"
        not in log_text
    )

    # Audit records how far the pipeline got: stages before the failing one
    audit = client.get(
        "/api/v1/audit",
        headers=admin_headers,
        params={"environment_id": env["id"], "action": "env.deploy"},
    )
    entries = [e for e in audit.json() if e["action"] == "env.deploy"]
    assert entries, "expected an env.deploy audit entry"
    details = entries[0]["details"]
    assert details["stages_completed"] == fail_stage_index
    assert details["stages_total"] == len(PIPELINE_STAGES)
    assert (
        details["failed_at"] == f"{fail_stage['id']}/{fail_stage['items'][0]['name']}"
    )
    assert entries[0]["success"] is False


def test_deploy_skip_push_runs_pipeline_without_push(client, admin_headers, tmp_path):
    # No config doc and no config dir at all — only possible with skip_push.
    env = _create_env(client, admin_headers)

    resp = _deploy_job(client, admin_headers, env["id"], params={"skip_push": True})
    assert resp.status_code == 201, resp.text
    job = resp.json()
    assert job["status"] == "success", job["error"]

    log_text = _job_log(client, admin_headers, job["id"])
    assert "[deploy] skip_push=true" in log_text
    assert "[deploy] phase 1" not in log_text
    assert "[dry-run] would write" not in log_text
    assert "[deploy] phase 2" in log_text


def test_deploy_job_operator_forbidden(
    client, admin_headers, operator_headers, tmp_path
):
    config_dir = tmp_path / "etc-genestack"
    config_dir.mkdir()
    env = _create_env(client, admin_headers, genestack_config_dir=str(config_dir))

    resp = _deploy_job(client, operator_headers, env["id"])
    assert resp.status_code == 403


def test_deploy_job_admin_queued_async(client, admin_headers, tmp_path):
    config_dir = tmp_path / "etc-genestack"
    config_dir.mkdir()
    env = _create_env(client, admin_headers, genestack_config_dir=str(config_dir))

    resp = _deploy_job(client, admin_headers, env["id"], run_sync=False)
    assert resp.status_code == 201, resp.text
    assert resp.json()["status"] == "queued"


def test_deploy_conflict_409(client, admin_headers, tmp_path):
    """A queued mutating job blocks a deploy for the same env."""
    config_dir = tmp_path / "etc-genestack"
    config_dir.mkdir()
    env = _create_env(client, admin_headers, genestack_config_dir=str(config_dir))
    _put_doc(client, admin_headers, env["id"])

    first = _deploy_job(client, admin_headers, env["id"], run_sync=False)
    assert first.status_code == 201, first.text
    assert first.json()["status"] == "queued"

    second = _deploy_job(client, admin_headers, env["id"])
    assert second.status_code == 409, second.text


def _capture_commands(monkeypatch, rc_for=None):
    """Capture every run_command call; rc_for maps a script path to a returncode."""
    captured: list[list[str]] = []

    def fake_run_command(cmd, **kwargs):  # noqa: ARG001
        argv = [str(c) for c in cmd]
        captured.append(argv)
        script = argv[1] if len(argv) > 1 else ""
        rc = (rc_for or {}).get(script, 0)
        return {"returncode": rc, "dry_run": False, "message": f"rc={rc}"}

    monkeypatch.setattr(bridge, "run_command", fake_run_command)
    return captured


def test_deploy_success_runs_credentials_setup(
    client, admin_headers, tmp_path, monkeypatch
):
    """A non-dry-run deploy finishes with bin/setup-openstack-rc.sh."""
    config_dir = tmp_path / "etc-genestack"
    config_dir.mkdir()
    env = _create_env(
        client, admin_headers, genestack_config_dir=str(config_dir), dry_run=False
    )
    _put_doc(client, admin_headers, env["id"])
    captured = _capture_commands(monkeypatch)

    resp = _deploy_job(client, admin_headers, env["id"])
    assert resp.status_code == 201, resp.text
    job = resp.json()
    assert job["status"] == "success", job["error"]

    assert ["bash", "bin/setup-openstack-rc.sh"] in captured
    # Credentials setup runs after every pipeline stage item
    last_pipeline = max(
        i for i, argv in enumerate(captured) if argv[1] != "bin/setup-openstack-rc.sh"
    )
    assert captured.index(["bash", "bin/setup-openstack-rc.sh"]) > last_pipeline

    log_text = _job_log(client, admin_headers, job["id"])
    assert (
        "credentials: wrote ~/.config/openstack/clouds.yaml on deploy host" in log_text
    )


def test_deploy_credentials_failure_does_not_fail_deploy(
    client, admin_headers, tmp_path, monkeypatch
):
    """setup-openstack-rc.sh failing is a warning, not a deploy failure."""
    config_dir = tmp_path / "etc-genestack"
    config_dir.mkdir()
    env = _create_env(
        client, admin_headers, genestack_config_dir=str(config_dir), dry_run=False
    )
    _put_doc(client, admin_headers, env["id"])
    _capture_commands(monkeypatch, rc_for={"bin/setup-openstack-rc.sh": 1})

    resp = _deploy_job(client, admin_headers, env["id"])
    assert resp.status_code == 201, resp.text
    job = resp.json()
    assert job["status"] == "success", job["error"]

    log_text = _job_log(client, admin_headers, job["id"])
    assert "WARNING credentials setup failed (rc=1)" in log_text


def test_deploy_dry_run_skips_credentials_setup(
    client, admin_headers, tmp_path, monkeypatch
):
    """Global test config is dry_run=True: would-be line logged, never executed."""
    config_dir = tmp_path / "etc-genestack"
    config_dir.mkdir()
    env = _create_env(client, admin_headers, genestack_config_dir=str(config_dir))
    _put_doc(client, admin_headers, env["id"])
    captured = _capture_commands(monkeypatch)

    resp = _deploy_job(client, admin_headers, env["id"])
    assert resp.status_code == 201, resp.text
    job = resp.json()
    assert job["status"] == "success", job["error"]

    assert ["bash", "bin/setup-openstack-rc.sh"] not in captured
    log_text = _job_log(client, admin_headers, job["id"])
    assert "$ bash bin/setup-openstack-rc.sh" in log_text
    assert "[dry-run] credentials setup skipped" in log_text
    assert "credentials: wrote" not in log_text


DOC_COMPONENTS_DISABLED = """\
provider: kubespray
components:
  keystone: true
  barbican: false
  cinder: false
"""

DOC_NO_COMPONENTS = """\
provider: kubespray
"""

DOC_STAGE_EMPTIED = """\
provider: kubespray
components:
  tempest: false
"""

DOC_WITH_CONTROL_PLANE = """\
provider: kubespray
components:
  keystone: true
servers:
  cp1:
    ip: 10.0.0.11
    roles: [k8s_control_plane]
"""

ADMIN_CONF = """\
apiVersion: v1
clusters:
- cluster:
    certificate-authority-data: ZmFrZQ==
    server: https://127.0.0.1:6443
  name: cluster.local
"""


def _deploy_audit_details(client, headers, env_id) -> dict:
    audit = client.get(
        "/api/v1/audit",
        headers=headers,
        params={"environment_id": env_id, "action": "env.deploy"},
    )
    entries = [e for e in audit.json() if e["action"] == "env.deploy"]
    assert entries, "expected an env.deploy audit entry"
    return entries[0]["details"]


def test_deploy_from_stage_invalid_fails(client, admin_headers, tmp_path):
    config_dir = tmp_path / "etc-genestack"
    config_dir.mkdir()
    env = _create_env(client, admin_headers, genestack_config_dir=str(config_dir))
    _put_doc(client, admin_headers, env["id"])

    resp = _deploy_job(client, admin_headers, env["id"], params={"from_stage": "bogus"})
    assert resp.status_code == 201, resp.text
    job = resp.json()
    assert job["status"] == "failed"
    assert "Unknown from_stage 'bogus'" in job["error"]
    # The valid stage ids are listed in the error
    for stage in PIPELINE_STAGES:
        assert stage["id"] in job["error"]

    log_text = _job_log(client, admin_headers, job["id"])
    # Validation fails before the push phase runs
    assert "[deploy] phase 1" not in log_text


def test_deploy_from_stage_starts_at_requested_stage(client, admin_headers, tmp_path):
    config_dir = tmp_path / "etc-genestack"
    config_dir.mkdir()
    env = _create_env(client, admin_headers, genestack_config_dir=str(config_dir))
    _put_doc(client, admin_headers, env["id"])

    resp = _deploy_job(client, admin_headers, env["id"], params={"from_stage": "core"})
    assert resp.status_code == 201, resp.text
    job = resp.json()
    assert job["status"] == "success", job["error"]

    log_text = _job_log(client, admin_headers, job["id"])
    assert "[deploy] from_stage=core" in log_text
    # The push phase still runs before the (shortened) pipeline
    assert "[deploy] phase 1: push config" in log_text
    total = len(PIPELINE_STAGES)
    core_index = [s["id"] for s in PIPELINE_STAGES].index("core") + 1
    for index, stage in enumerate(PIPELINE_STAGES, start=1):
        marker = f"[deploy] === stage {index}/{total}: {stage['id']}"
        if index < core_index:
            assert marker not in log_text, f"stage ran before from_stage: {marker}"
        else:
            assert marker in log_text, f"missing stage marker: {marker}"

    details = _deploy_audit_details(client, admin_headers, env["id"])
    assert details["from_stage"] == "core"


def test_deploy_until_stage_stops_at_control_point(client, admin_headers, tmp_path):
    config_dir = tmp_path / "etc-genestack"
    config_dir.mkdir()
    env = _create_env(client, admin_headers, genestack_config_dir=str(config_dir))
    _put_doc(client, admin_headers, env["id"])

    resp = _deploy_job(
        client,
        admin_headers,
        env["id"],
        params={"from_stage": "core", "until_stage": "core"},
    )
    assert resp.status_code == 201, resp.text
    job = resp.json()
    assert job["status"] == "success", job["error"]
    log_text = _job_log(client, admin_headers, job["id"])
    assert "[deploy] until_stage=core" in log_text
    assert "stopped at this control point" in log_text
    assert "=== stage" in log_text
    assert ": core" in log_text
    assert ": compute-network" not in log_text
    assert ": testing" not in log_text


def test_deploy_from_testing_runs_tempest_stage(client, admin_headers, tmp_path):
    config_dir = tmp_path / "etc-genestack"
    config_dir.mkdir()
    env = _create_env(client, admin_headers, genestack_config_dir=str(config_dir))
    _put_doc(client, admin_headers, env["id"])

    resp = _deploy_job(
        client, admin_headers, env["id"], params={"from_stage": "testing"}
    )
    assert resp.status_code == 201, resp.text
    job = resp.json()
    assert job["status"] == "success", job["error"]
    log_text = _job_log(client, admin_headers, job["id"])
    assert "run Tempest separately" not in log_text
    assert "$ bash bin/install-tempest.sh" in log_text


def test_deploy_optional_item_failure_continues(
    client, admin_headers, tmp_path, monkeypatch
):
    config_dir = tmp_path / "etc-genestack"
    config_dir.mkdir()
    env = _create_env(
        client, admin_headers, genestack_config_dir=str(config_dir), dry_run=False
    )
    _put_doc(client, admin_headers, env["id"])

    def fake_run_command(cmd, **kwargs):  # noqa: ARG001
        script = cmd[1] if len(cmd) > 1 else ""
        rc = 1 if script == "bin/install-horizon.sh" else 0
        return {"returncode": rc, "dry_run": False, "message": f"rc={rc}"}

    monkeypatch.setattr(bridge, "run_command", fake_run_command)
    resp = _deploy_job(
        client, admin_headers, env["id"], params={"from_stage": "platform-extras"}
    )
    assert resp.status_code == 201, resp.text
    job = resp.json()
    assert job["status"] == "success", job["error"]
    log_text = _job_log(client, admin_headers, job["id"])
    assert "WARNING optional platform-extras/horizon" in log_text
    assert "[deploy] FAILED at" not in log_text


def test_deploy_component_filter_skips_disabled_services(
    client, admin_headers, tmp_path
):
    """components: <svc> false drops that stage item; scripts always run."""
    config_dir = tmp_path / "etc-genestack"
    config_dir.mkdir()
    env = _create_env(client, admin_headers, genestack_config_dir=str(config_dir))
    _put_doc(client, admin_headers, env["id"], doc=DOC_COMPONENTS_DISABLED)

    resp = _deploy_job(client, admin_headers, env["id"])
    assert resp.status_code == 201, resp.text
    job = resp.json()
    assert job["status"] == "success", job["error"]

    log_text = _job_log(client, admin_headers, job["id"])
    assert "[pipeline] skipping barbican (disabled in config doc)" in log_text
    assert "[pipeline] skipping cinder (disabled in config doc)" in log_text
    # Disabled services never run
    assert "$ bash bin/install-barbican.sh" not in log_text
    assert "$ bash bin/install-cinder.sh" not in log_text
    # Non-install scripts always run
    assert "$ bash bin/setup-hosts.sh" in log_text
    assert "$ bash bin/setup-infrastructure.sh" in log_text
    # Enabled/absent components still run
    assert "$ bash bin/install-keystone.sh" in log_text
    assert "$ bash bin/install-nova.sh" in log_text


def test_deploy_doc_without_components_runs_everything(client, admin_headers, tmp_path):
    config_dir = tmp_path / "etc-genestack"
    config_dir.mkdir()
    env = _create_env(client, admin_headers, genestack_config_dir=str(config_dir))
    _put_doc(client, admin_headers, env["id"], doc=DOC_NO_COMPONENTS)

    resp = _deploy_job(client, admin_headers, env["id"])
    assert resp.status_code == 201, resp.text
    job = resp.json()
    assert job["status"] == "success", job["error"]

    log_text = _job_log(client, admin_headers, job["id"])
    assert "[pipeline] skipping" not in log_text
    assert "$ bash bin/install-barbican.sh" in log_text


def test_deploy_stage_empty_after_filtering_marked_complete(
    client, admin_headers, tmp_path
):
    """A stage whose only item is disabled completes with a note, not a failure."""
    config_dir = tmp_path / "etc-genestack"
    config_dir.mkdir()
    env = _create_env(client, admin_headers, genestack_config_dir=str(config_dir))
    _put_doc(client, admin_headers, env["id"], doc=DOC_STAGE_EMPTIED)

    resp = _deploy_job(client, admin_headers, env["id"])
    assert resp.status_code == 201, resp.text
    job = resp.json()
    assert job["status"] == "success", job["error"]

    log_text = _job_log(client, admin_headers, job["id"])
    assert "run Tempest separately" in log_text
    assert "$ bash bin/install-tempest.sh" not in log_text


def _fake_kubeconfig_run_command(monkeypatch, admin_conf=ADMIN_CONF):
    """run_command stub: serves admin.conf over the ssh cat, rc 0 elsewhere."""
    calls: list[list[str]] = []

    def fake_run_command(cmd, **kwargs):
        argv = [str(c) for c in cmd]
        calls.append(argv)
        if argv[:2] == ["cat", "/etc/kubernetes/admin.conf"]:
            assert kwargs.get("ssh_target") == "10.0.0.11"
            assert kwargs.get("log") is None
            return {
                "returncode": 0 if admin_conf else 1,
                "stdout": admin_conf or "",
                "dry_run": False,
                "message": "ok",
            }
        return {"returncode": 0, "dry_run": False, "message": "ok"}

    monkeypatch.setattr(bridge, "run_command", fake_run_command)
    return calls


def test_deploy_fetches_kubeconfig_after_hosts_stage(
    client, admin_headers, tmp_path, monkeypatch
):
    """hosts done + no kubeconfig -> ssh cat admin.conf, rewrite server, 0600."""
    config_dir = tmp_path / "etc-genestack"
    config_dir.mkdir()
    env = _create_env(
        client, admin_headers, genestack_config_dir=str(config_dir), dry_run=False
    )
    _put_doc(client, admin_headers, env["id"], doc=DOC_WITH_CONTROL_PLANE)
    seen: dict[str, str] = {}
    real_fetch = deploy_service._fetch_kubeconfig

    def wrapped(*args, **kwargs):
        real_fetch(*args, **kwargs)
        kubeconfig = config_dir / "inventory" / "artifacts" / "admin.conf"
        assert kubeconfig.is_file()
        text = kubeconfig.read_text(encoding="utf-8")
        assert "server: https://10.0.0.11:6443" in text
        assert "127.0.0.1" not in text
        assert (kubeconfig.stat().st_mode & 0o777) == 0o600
        seen["text"] = text

    monkeypatch.setattr(deploy_service, "_fetch_kubeconfig", wrapped)
    calls = _fake_kubeconfig_run_command(monkeypatch)

    resp = _deploy_job(client, admin_headers, env["id"])
    assert resp.status_code == 201, resp.text
    job = resp.json()
    assert job["status"] == "success", job["error"]

    kubeconfig = config_dir / "inventory" / "artifacts" / "admin.conf"
    assert ["cat", "/etc/kubernetes/admin.conf"] in calls
    # The job created the file, then removed it when it finished.
    assert not kubeconfig.exists()
    assert seen["text"]

    log_text = _job_log(client, admin_headers, job["id"])
    assert "[kubeconfig] wrote" in log_text
    # The fetched content (cluster credentials) is never echoed to the job log
    assert "certificate-authority-data" not in log_text
    assert "ZmFrZQ==" not in log_text
    db = SessionLocal()
    try:
        row = db.get(Environment, env["id"])
        plain = decrypt_secret(row.kubeconfig_data)
    finally:
        db.close()
    assert plain == seen["text"]
    assert "server: https://10.0.0.11:6443" in plain


def test_deploy_kubeconfig_fetch_skipped_when_present(
    client, admin_headers, tmp_path, monkeypatch
):
    config_dir = tmp_path / "etc-genestack"
    kubeconfig = config_dir / "inventory" / "artifacts" / "admin.conf"
    kubeconfig.parent.mkdir(parents=True)
    kubeconfig.write_text("original\n", encoding="utf-8")
    env = _create_env(
        client, admin_headers, genestack_config_dir=str(config_dir), dry_run=False
    )
    _put_doc(client, admin_headers, env["id"], doc=DOC_WITH_CONTROL_PLANE)
    calls = _fake_kubeconfig_run_command(monkeypatch)

    resp = _deploy_job(client, admin_headers, env["id"])
    assert resp.status_code == 201, resp.text
    job = resp.json()
    assert job["status"] == "success", job["error"]

    assert ["cat", "/etc/kubernetes/admin.conf"] not in calls
    assert kubeconfig.read_text(encoding="utf-8") == "original\n"
    log_text = _job_log(client, admin_headers, job["id"])
    assert "already exists — skipping fetch" in log_text


def test_deploy_kubeconfig_fetch_dry_run_logs_would_be(client, admin_headers, tmp_path):
    """Global test config is dry_run=True: would-be line logged, nothing fetched."""
    config_dir = tmp_path / "etc-genestack"
    config_dir.mkdir()
    env = _create_env(client, admin_headers, genestack_config_dir=str(config_dir))
    _put_doc(client, admin_headers, env["id"], doc=DOC_WITH_CONTROL_PLANE)

    resp = _deploy_job(client, admin_headers, env["id"])
    assert resp.status_code == 201, resp.text
    job = resp.json()
    assert job["status"] == "success", job["error"]

    log_text = _job_log(client, admin_headers, job["id"])
    assert "[dry-run] would fetch /etc/kubernetes/admin.conf from 10.0.0.11" in log_text
    assert not (config_dir / "inventory" / "artifacts" / "admin.conf").exists()


def test_pipeline_run_component_filter_skips_disabled(client, admin_headers, tmp_path):
    """genestack.pipeline.run gets the same config-doc component filter."""
    config_dir = tmp_path / "etc-genestack"
    config_dir.mkdir()
    env = _create_env(client, admin_headers, genestack_config_dir=str(config_dir))
    _put_doc(client, admin_headers, env["id"], doc=DOC_COMPONENTS_DISABLED)

    resp = client.post(
        f"/api/v1/environments/{env['id']}/jobs",
        headers=admin_headers,
        json={
            "operation": "genestack.pipeline.run",
            "params": {"stage": "platform-extras"},
            "run_sync": True,
        },
    )
    assert resp.status_code == 201, resp.text
    job = resp.json()
    assert job["status"] == "success", job["error"]

    log_text = _job_log(client, admin_headers, job["id"])
    assert "[pipeline] skipping barbican (disabled in config doc)" in log_text
    assert "[pipeline] skipping cinder (disabled in config doc)" in log_text
    assert "$ bash bin/install-barbican.sh" not in log_text
    assert "$ bash bin/install-heat.sh" in log_text


DOC_TALOS_PROVIDER = """\
provider: talos
components:
  keystone: true
servers:
  cp1:
    ip: 10.0.0.11
    roles: [k8s_control_plane]
  worker1:
    ip: 10.0.0.21
    roles: [compute]
"""


def test_deploy_ovh_talos_uses_factory_default_image_url(
    client, admin_headers, tmp_path, monkeypatch
):
    """OVH + talos deploy falls back to the factory qcow2 when image_url is unset."""
    from tests.test_ovh import (
        _bind_env_to_account,
        _create_ovh_account,
        _mock_transport,
        _ovh_byoi_servers,
        _patch_client_factory,
        _store_account_key,
    )

    _patch_client_factory(
        monkeypatch,
        _mock_transport(servers=_ovh_byoi_servers(), ids=["ns-byoi-1", "ns-sku-2"]),
    )
    acc = _create_ovh_account(client, admin_headers, f"dep-ovh-{_suffix()}")
    config_dir = tmp_path / "etc-genestack"
    config_dir.mkdir()
    env = _create_env(client, admin_headers, genestack_config_dir=str(config_dir))
    _bind_env_to_account(client, admin_headers, env["id"], acc["id"])
    _store_account_key(client, admin_headers, acc["id"])
    _put_doc(
        client,
        admin_headers,
        env["id"],
        doc="""\
provider: talos
servers:
  ns-byoi-1:
    source: ovh
    ip: 198.51.100.21
    roles: [k8s_control_plane]
""",
    )
    from app.services.talos import DEFAULT_TALOS_IMAGE_URL

    resp = _deploy_job(client, admin_headers, env["id"])
    assert resp.status_code == 201, resp.text
    job = resp.json()
    hay = f"{job.get('error') or ''}\n{_job_log(client, admin_headers, job['id'])}"
    assert DEFAULT_TALOS_IMAGE_URL in hay
    assert "image_url is required" not in hay


def test_deploy_talos_provider_runs_talos_flow_not_setup_hosts(
    client, admin_headers, tmp_path
):
    """provider=talos: hosts stage runs the talosctl flow, never setup-hosts.sh."""
    config_dir = tmp_path / "etc-genestack"
    config_dir.mkdir()
    env = _create_env(client, admin_headers, genestack_config_dir=str(config_dir))
    _put_doc(client, admin_headers, env["id"], doc=DOC_TALOS_PROVIDER)

    resp = _deploy_job(client, admin_headers, env["id"])
    assert resp.status_code == 201, resp.text
    job = resp.json()
    assert job["status"] == "success", job["error"]

    log_text = _job_log(client, admin_headers, job["id"])
    assert "provider=talos — using talosctl flow" in log_text
    assert "$ bash bin/setup-hosts.sh" not in log_text
    # The talos flow commands run in docs/k8s-talos.md order
    expected = [
        "talosctl gen config",
        "talosctl config endpoints 10.0.0.11 --talosconfig=./talosconfig",
        "talosctl apply-config --insecure --nodes 10.0.0.11 --file controlplane.yaml",
        "talosctl apply-config --insecure --nodes 10.0.0.21 --file worker.yaml",
        "talosctl bootstrap --nodes 10.0.0.11 --talosconfig=./talosconfig",
        "talosctl kubeconfig",
    ]
    positions = []
    for line in expected:
        pos = log_text.find(line)
        assert pos != -1, f"missing log line: {line}"
        positions.append(pos)
    assert positions == sorted(positions), "talos commands logged out of order"
    # Later pipeline stages are unaffected
    assert "$ bash bin/setup-infrastructure.sh" in log_text
    assert "$ bash bin/install-keystone.sh" in log_text


def _capture_talos_deploy_commands(monkeypatch, fail_worker=False):
    """run_command stub: talosctl kubeconfig writes its target; all rc 0 (or fail worker)."""
    captured: list[list[str]] = []

    def fake_run_command(cmd, **kwargs):  # noqa: ARG001
        argv = [str(c) for c in cmd]
        captured.append(argv)
        rc = 0
        head = argv[0]
        is_talos = head == "talosctl" or head.endswith("/talosctl")
        if (
            is_talos
            and len(argv) > 1
            and argv[1] == "apply-config"
            and "worker.yaml" in argv
            and fail_worker
        ):
            rc = 1
        elif is_talos and len(argv) > 1 and argv[1] == "kubeconfig":
            target = Path(argv[2])
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("talos-kubeconfig\n", encoding="utf-8")
        return {"returncode": rc, "dry_run": False, "message": f"rc={rc}"}

    monkeypatch.setattr(bridge, "run_command", fake_run_command)
    return captured


def test_deploy_talos_skips_kubeconfig_autofetch(
    client, admin_headers, tmp_path, monkeypatch
):
    """The talos flow fetched the kubeconfig, so the ssh-cat fallback never runs."""
    config_dir = tmp_path / "etc-genestack"
    config_dir.mkdir()
    env = _create_env(
        client, admin_headers, genestack_config_dir=str(config_dir), dry_run=False
    )
    _put_doc(client, admin_headers, env["id"], doc=DOC_TALOS_PROVIDER)
    captured = _capture_talos_deploy_commands(monkeypatch)

    resp = _deploy_job(client, admin_headers, env["id"])
    assert resp.status_code == 201, resp.text
    job = resp.json()
    assert job["status"] == "success", job["error"]

    assert ["bash", "bin/setup-hosts.sh"] not in captured
    assert any(
        (argv[0] == "talosctl" or str(argv[0]).endswith("/talosctl"))
        and argv[1:3] == ["gen", "config"]
        for argv in captured
    )
    # talosctl wrote the kubeconfig during the job, so the ssh cat never ran.
    # The job then removed that file and kept the text on the environment.
    assert ["cat", "/etc/kubernetes/admin.conf"] not in captured
    kubeconfig = config_dir / "inventory" / "artifacts" / "admin.conf"
    assert not kubeconfig.exists()
    log_text = _job_log(client, admin_headers, job["id"])
    assert "skipping fetch" in log_text
    assert "talos-kubeconfig" not in log_text
    db = SessionLocal()
    try:
        row = db.get(Environment, env["id"])
        plain = decrypt_secret(row.kubeconfig_data)
    finally:
        db.close()
    assert plain == "talos-kubeconfig\n"


def test_deploy_talos_phase_failure_stops_pipeline(
    client, admin_headers, tmp_path, monkeypatch
):
    config_dir = tmp_path / "etc-genestack"
    config_dir.mkdir()
    env = _create_env(
        client, admin_headers, genestack_config_dir=str(config_dir), dry_run=False
    )
    _put_doc(client, admin_headers, env["id"], doc=DOC_TALOS_PROVIDER)
    captured = _capture_talos_deploy_commands(monkeypatch, fail_worker=True)

    resp = _deploy_job(client, admin_headers, env["id"])
    assert resp.status_code == 201, resp.text
    job = resp.json()
    assert job["status"] == "failed"
    assert "stage 'hosts'" in job["error"]
    assert "apply-worker" in job["error"]

    # The pipeline stopped at hosts: later stages never ran
    assert ["bash", "bin/setup-infrastructure.sh"] not in captured
    log_text = _job_log(client, admin_headers, job["id"])
    assert "[deploy] FAILED at hosts/talos-apply-worker rc=1" in log_text

    details = _deploy_audit_details(client, admin_headers, env["id"])
    assert details["failed_at"] == "hosts/talos-apply-worker"
    assert details["stages_completed"] == 0


def test_deploy_kubespray_provider_unchanged(client, admin_headers, tmp_path):
    """provider=kubespray: hosts stage still runs setup-hosts.sh, no talosctl."""
    config_dir = tmp_path / "etc-genestack"
    config_dir.mkdir()
    env = _create_env(client, admin_headers, genestack_config_dir=str(config_dir))
    _put_doc(client, admin_headers, env["id"])  # DOC is provider: kubespray

    resp = _deploy_job(client, admin_headers, env["id"])
    assert resp.status_code == 201, resp.text
    job = resp.json()
    assert job["status"] == "success", job["error"]

    log_text = _job_log(client, admin_headers, job["id"])
    assert "$ bash bin/setup-hosts.sh" in log_text
    assert "provider=talos" not in log_text
    assert "$ talosctl" not in log_text


# ---------------------------------------------------------------------------
# Deploy via the agent channel
# ---------------------------------------------------------------------------


def test_deploy_dry_run_shows_via_agent(client, admin_headers, tmp_path):
    """Kubespray dry-run deploy with a connected agent logs the agent executor."""
    config_dir = tmp_path / "etc-genestack"
    config_dir.mkdir()
    env = _create_env(client, admin_headers, genestack_config_dir=str(config_dir))
    _put_doc(client, admin_headers, env["id"])
    token = _create_token(client, admin_headers, env["id"])
    url = f"/api/v1/agents/connect?token={token['token']}"

    with client.websocket_connect(url) as ws:
        _handshake(ws, token["token"])
        resp = _deploy_job(client, admin_headers, env["id"])
        assert resp.status_code == 201, resp.text
        job = resp.json()
        assert job["status"] == "success", job["error"]

    log_text = _job_log(client, admin_headers, job["id"])
    assert "via agent" in log_text
    # Dry run: the plan is logged, nothing was written or dispatched
    assert "[dry-run] would write" in log_text
    assert "[dry-run] command not executed" in log_text
    assert list(config_dir.rglob("*")) == []


def test_deploy_via_agent_runs_pipeline_through_agent(client, admin_headers, tmp_path):
    """Non-dry-run kubespray deploy: push ships file_write rows, stages run as
    command frames through the connected agent."""
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
            resp = _deploy_job(client, admin_headers, env["id"])
            assert resp.status_code == 201, resp.text
            job = resp.json()
            assert job["status"] == "success", job["error"]
    finally:
        stop.set()

    commands = [f for f in frames if f.get("type") == "command"]
    writes = [f for f in frames if f.get("type") == "file_write"]
    # Phase 1 pushed the rendered files as file_write frames
    assert writes, "expected file_write frames for the config push"
    # Phase 2 ran every pipeline item as a command frame, incl. setup-hosts
    cmd_lines = [" ".join(str(c) for c in f["cmd"]) for f in commands]
    assert any("bin/setup-hosts.sh" in line for line in cmd_lines)
    # Phase 3 credentials setup also went through the agent
    assert any("bin/setup-openstack-rc.sh" in line for line in cmd_lines)

    # The fake agent executed the push writes locally
    assert (config_dir / "provider").read_text(encoding="utf-8") == "kubespray\n"

    log_text = _job_log(client, admin_headers, job["id"])
    assert "via agent" in log_text
    assert "pushed config version=" in log_text


# ---------------------------------------------------------------------------
# ovh_byoi_reinstall_for_env — direct service-level coverage
# ---------------------------------------------------------------------------


def test_ovh_byoi_reinstall_service_dry_run(client, admin_headers, monkeypatch):
    """Dry run: ok=True, one dry_run entry per source:ovh server, no reinstall POST."""
    eid = _byoi_env_setup(client, admin_headers, monkeypatch)
    with SessionLocal() as db:
        env = db.get(Environment, eid)
        result = deploy_service.ovh_byoi_reinstall_for_env(
            db, env, operating_system="byoi:debian-12", dry_run=True
        )
    assert result["ok"] is True
    assert result["count"] == 2
    assert result["dry_run"] is True
    hosts = {s["hostname"]: s for s in result["servers"]}
    assert hosts["ns-byoi-1"]["service_name"] == "ns-byoi-1"
    assert hosts["ns-sku-2"]["service_name"] == "ns-sku-2"
    assert all(s["dry_run"] for s in result["servers"])
    assert all(s["task_id"] is None for s in result["servers"])
    # the static node is never selected
    assert "other-node" not in hosts


def test_ovh_byoi_reinstall_service_live_continues_on_failure(
    client, admin_headers, monkeypatch
):
    """Live: a failing server is reported per-server without aborting the rest."""
    eid = _byoi_env_setup(
        client,
        admin_headers,
        monkeypatch,
        reinstall_fail={"ns-sku-2": "server is busy"},
    )
    with SessionLocal() as db:
        env = db.get(Environment, eid)
        result = deploy_service.ovh_byoi_reinstall_for_env(
            db, env, operating_system="byoi:debian-12", dry_run=False
        )
    assert result["ok"] is False
    assert result["count"] == 2
    assert result["dry_run"] is False
    hosts = {s["hostname"]: s for s in result["servers"]}
    assert hosts["ns-byoi-1"]["task_id"] == "task-ns-byoi-1"
    assert hosts["ns-byoi-1"]["error"] is None
    assert hosts["ns-sku-2"]["task_id"] is None
    assert "denied" in hosts["ns-sku-2"]["error"].lower()
    assert "1/2" in result["message"]


def test_ovh_byoi_reinstall_service_unbound_env(client, admin_headers, monkeypatch):
    """Unbound env: clear error, no OVH calls, no traceback."""
    ts = int(time.time() * 1000)
    _patch_client_factory(monkeypatch, _mock_transport(servers=_ovh_byoi_servers()))
    env = _create_env(client, admin_headers, name=f"ovh-unbound-{ts}")
    with SessionLocal() as db:
        env_obj = db.get(Environment, env["id"])
        result = deploy_service.ovh_byoi_reinstall_for_env(
            db, env_obj, operating_system="byoi:debian-12"
        )
    assert result["ok"] is False
    assert result["count"] == 0
    assert result["servers"] == []
    assert "not bound" in result["error"]
