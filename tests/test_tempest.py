"""genestack.tempest job tests — OpenStack conformance via the tempest helm chart."""

from __future__ import annotations

import uuid

from app.db import SessionLocal
from app.models import Environment, JobStatus
from app.services import genestack_bridge as bridge
from app.services.catalog import get_operation
from app.services.job_runner import JobRunner


def _tempest_job(client, headers, params=None):
    body = {"operation": "genestack.tempest", "params": params or {}, "run_sync": True}
    return client.post("/api/v1/jobs", headers=headers, json=body)


def _job_log(client, headers, job_id) -> str:
    resp = client.get(f"/api/v1/jobs/{job_id}", headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()["log_text"]


def test_tempest_op_in_catalog():
    op = get_operation("genestack.tempest")
    assert op is not None
    assert op.name == "Tempest conformance suite"
    assert op.required_role == "operator"
    assert op.mutating is True
    assert op.handler == "genestack_tempest"
    assert op.timeout_seconds == 7200
    action = next(p for p in op.params if p.name == "action")
    assert action.required is False
    suite = next(p for p in op.params if p.name == "suite")
    assert suite.required is False


def test_tempest_dry_run_logs_both_commands(client, operator_headers):
    """Default action install-run: both commands logged, nothing executed."""
    resp = _tempest_job(client, operator_headers)
    assert resp.status_code == 201, resp.text
    job = resp.json()
    assert job["status"] == "success", job["error"]

    log_text = _job_log(client, operator_headers, job["id"])
    assert "bash bin/install-tempest.sh" in log_text
    assert "manifests.job_run_tests=true" in log_text
    assert "[dry-run] command not executed" in log_text


def test_tempest_install_only(client, operator_headers, monkeypatch):
    captured: list[list[str]] = []

    def fake_run_command(cmd, **kwargs):  # noqa: ARG001
        captured.append([str(c) for c in cmd])
        return {"returncode": 0, "dry_run": False, "message": "ok"}

    monkeypatch.setattr(bridge, "run_command", fake_run_command)

    resp = _tempest_job(client, operator_headers, params={"action": "install"})
    assert resp.status_code == 201, resp.text
    job = resp.json()
    assert job["status"] == "success", job["error"]
    assert captured == [["bash", "bin/install-tempest.sh"]]


def test_tempest_run_only(client, operator_headers, monkeypatch):
    captured: list[list[str]] = []

    def fake_run_command(cmd, **kwargs):  # noqa: ARG001
        captured.append([str(c) for c in cmd])
        return {"returncode": 0, "dry_run": False, "message": "ok"}

    monkeypatch.setattr(bridge, "run_command", fake_run_command)

    resp = _tempest_job(client, operator_headers, params={"action": "run"})
    assert resp.status_code == 201, resp.text
    job = resp.json()
    assert job["status"] == "success", job["error"]
    # Product runs install-tempest.sh with manifests.job_run_tests=true
    assert captured == [
        ["bash", "bin/install-tempest.sh", "--set", "manifests.job_run_tests=true"]
    ]


def test_tempest_invalid_action_fails(client, operator_headers):
    resp = _tempest_job(client, operator_headers, params={"action": "bogus"})
    assert resp.status_code == 201, resp.text
    job = resp.json()
    assert job["status"] == "failed"
    assert "Invalid tempest action 'bogus'" in job["error"]
    assert "install, run, install-run" in job["error"]

    log_text = _job_log(client, operator_headers, job["id"])
    assert "[denied]" in log_text
    assert "install-tempest.sh" not in log_text


def test_tempest_install_rc_names_phase(client, operator_headers, monkeypatch):
    captured: list[list[str]] = []

    def fake_run_command(cmd, **kwargs):  # noqa: ARG001
        captured.append([str(c) for c in cmd])
        return {"returncode": 3, "dry_run": False, "message": "failed rc=3"}

    monkeypatch.setattr(bridge, "run_command", fake_run_command)

    resp = _tempest_job(client, operator_headers)  # install-run: install fails first
    assert resp.status_code == 201, resp.text
    job = resp.json()
    assert job["status"] == "failed"
    assert "tempest install failed rc=3" in job["error"]
    # test phase never attempted after a failed install
    assert captured == [["bash", "bin/install-tempest.sh"]]


def test_tempest_run_rc_names_phase(client, operator_headers, monkeypatch):
    def fake_run_command(cmd, **kwargs):  # noqa: ARG001
        return {"returncode": 1, "dry_run": False, "message": "failed rc=1"}

    monkeypatch.setattr(bridge, "run_command", fake_run_command)

    resp = _tempest_job(client, operator_headers, params={"action": "run"})
    assert resp.status_code == 201, resp.text
    job = resp.json()
    assert job["status"] == "failed"
    assert "tempest test failed rc=1" in job["error"]


_SUITE_SCRIPT_SET = (
    "--set",
    "conf.script=tempest run --include-list /etc/tempest/test-whitelist "
    "--exclude-list /etc/tempest/test-blacklist --config-file "
    "/etc/tempest/tempest.conf -w 4",
)


def test_tempest_suite_reaches_install_command(client, operator_headers, monkeypatch):
    """suite → install phase gets BOTH chart values: the whitelist and a
    rewritten conf.script that actually consumes it via --include-list
    (the deployed script only references --exclude-list/--smoke, so the
    whitelist file alone would be mounted but ignored)."""
    captured: list[list[str]] = []

    def fake_run_command(cmd, **kwargs):  # noqa: ARG001
        captured.append([str(c) for c in cmd])
        return {"returncode": 0, "dry_run": False, "message": "ok"}

    monkeypatch.setattr(bridge, "run_command", fake_run_command)

    suite = "tempest\\.api\\.identity"
    resp = _tempest_job(
        client,
        operator_headers,
        params={"action": "install", "suite": suite},
    )
    assert resp.status_code == 201, resp.text
    job = resp.json()
    assert job["status"] == "success", job["error"]
    assert captured == [
        [
            "bash",
            "bin/install-tempest.sh",
            "--set",
            f"conf.whitelist[0]={suite}",
            *_SUITE_SCRIPT_SET,
        ]
    ]

    log_text = _job_log(client, operator_headers, job["id"])
    assert f"conf.whitelist[0]={suite}" in log_text
    assert "--include-list /etc/tempest/test-whitelist" in log_text
    assert "not wired in v1" not in log_text


def test_tempest_suite_install_run_threads_value_and_test_phase(
    client, operator_headers, monkeypatch
):
    captured: list[list[str]] = []

    def fake_run_command(cmd, **kwargs):  # noqa: ARG001
        captured.append([str(c) for c in cmd])
        return {"returncode": 0, "dry_run": False, "message": "ok"}

    monkeypatch.setattr(bridge, "run_command", fake_run_command)

    resp = _tempest_job(
        client,
        operator_headers,
        params={"action": "install-run", "suite": "smoke-suite"},
    )
    assert resp.status_code == 201, resp.text
    job = resp.json()
    assert job["status"] == "success", job["error"]
    assert captured == [
        [
            "bash",
            "bin/install-tempest.sh",
            "--set",
            "conf.whitelist[0]=smoke-suite",
            *_SUITE_SCRIPT_SET,
        ],
        [
            "bash",
            "bin/install-tempest.sh",
            "--set",
            "conf.whitelist[0]=smoke-suite",
            *_SUITE_SCRIPT_SET,
            "--set",
            "manifests.job_run_tests=true",
        ],
    ]

    log_text = _job_log(client, operator_headers, job["id"])
    assert "--include-list /etc/tempest/test-whitelist" in log_text
    assert "not wired in v1" not in log_text


def test_tempest_suite_full_keeps_chart_default(client, operator_headers, monkeypatch):
    """suite=full means: no whitelist override, chart default runs."""
    captured: list[list[str]] = []

    def fake_run_command(cmd, **kwargs):  # noqa: ARG001
        captured.append([str(c) for c in cmd])
        return {"returncode": 0, "dry_run": False, "message": "ok"}

    monkeypatch.setattr(bridge, "run_command", fake_run_command)

    resp = _tempest_job(
        client,
        operator_headers,
        params={"action": "install", "suite": "full"},
    )
    assert resp.status_code == 201, resp.text
    job = resp.json()
    assert job["status"] == "success", job["error"]
    assert captured == [["bash", "bin/install-tempest.sh"]]

    log_text = _job_log(client, operator_headers, job["id"])
    assert "chart default" in log_text


def test_tempest_suite_with_run_only_works(client, operator_headers, monkeypatch):
    """The run action now uses bash bin/install-tempest.sh with suite --set
    flags, so suite+action=run works (re-installs with the suite settings)."""
    captured: list[list[str]] = []

    def fake_run_command(cmd, **kwargs):  # noqa: ARG001
        captured.append([str(c) for c in cmd])
        return {"returncode": 0, "dry_run": False, "message": "ok"}

    monkeypatch.setattr(bridge, "run_command", fake_run_command)

    resp = _tempest_job(
        client,
        operator_headers,
        params={"action": "run", "suite": "smoke-suite"},
    )
    assert resp.status_code == 201, resp.text
    job = resp.json()
    assert job["status"] == "success"
    assert captured == [
        [
            "bash",
            "bin/install-tempest.sh",
            "--set",
            "conf.whitelist[0]=smoke-suite",
            *_SUITE_SCRIPT_SET,
            "--set",
            "manifests.job_run_tests=true",
        ],
    ]


def test_tempest_job_threads_ssh_target(genestack_root):
    """Env deploy host: both phases are ssh-wrapped (dry-run log check)."""
    db = SessionLocal()
    try:
        env = Environment(
            name=f"env-tempest-ssh-{uuid.uuid4().hex[:8]}",
            deployer_ssh_host="deployer.example.com",
            deployer_ssh_user="deploy",
            genestack_path=str(genestack_root),
        )
        db.add(env)
        db.commit()

        runner = JobRunner(db)
        job = runner.create_job(
            operation="genestack.tempest",
            params={"action": "install-run"},
            environment_id=env.id,
            created_by="tempest-ssh-test",
        )
        db.commit()
        job = runner.run_job(job)

        assert job.status == JobStatus.success, job.error
        log_text = job.log_text or ""
        assert "deploy@deployer.example.com" in log_text
        lines = log_text.splitlines()
        assert any("ssh" in line and "install-tempest.sh" in line for line in lines)
        assert any(
            "ssh" in line and "manifests.job_run_tests=true" in line for line in lines
        )
    finally:
        db.close()


def test_tempest_viewer_forbidden(client, viewer_headers):
    resp = _tempest_job(client, viewer_headers)
    assert resp.status_code == 403
