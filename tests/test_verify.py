"""genestack.verify job tests — genestack's own test suite as the 'did it work' check."""

from __future__ import annotations

from app.services import genestack_bridge as bridge
from app.services.catalog import get_operation


def _verify_job(client, headers, params=None):
    body = {"operation": "genestack.verify", "params": params or {}, "run_sync": True}
    return client.post("/api/v1/jobs", headers=headers, json=body)


def _job_log(client, headers, job_id) -> str:
    resp = client.get(f"/api/v1/jobs/{job_id}", headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()["log_text"]


def test_verify_op_in_catalog():
    op = get_operation("genestack.verify")
    assert op is not None
    assert op.name == "Verify environment (genestack test suite)"
    assert op.required_role == "operator"
    assert op.mutating is True
    assert op.handler == "genestack_verify"
    assert op.timeout_seconds == 3600
    level = next(p for p in op.params if p.name == "level")
    assert level.required is False


def test_verify_dry_run_logs_run_all_tests_command(client, operator_headers):
    """Global test config is dry_run=True: command logged, nothing executed."""
    resp = _verify_job(client, operator_headers)
    assert resp.status_code == 201, resp.text
    job = resp.json()
    assert job["status"] == "success", job["error"]

    log_text = _job_log(client, operator_headers, job["id"])
    assert "bash scripts/tests/run-all-tests.sh standard" in log_text
    assert "[dry-run] command not executed" in log_text


def test_verify_level_param_forwarded(client, operator_headers, monkeypatch):
    captured: list[list[str]] = []

    def fake_run_command(cmd, **kwargs):  # noqa: ARG001
        captured.append([str(c) for c in cmd])
        return {"returncode": 0, "dry_run": False, "message": "ok"}

    monkeypatch.setattr(bridge, "run_command", fake_run_command)

    resp = _verify_job(client, operator_headers, params={"level": "quick"})
    assert resp.status_code == 201, resp.text
    job = resp.json()
    assert job["status"] == "success", job["error"]
    assert captured == [["bash", "scripts/tests/run-all-tests.sh", "quick"]]


def test_verify_invalid_level_fails(client, operator_headers):
    resp = _verify_job(client, operator_headers, params={"level": "bogus"})
    assert resp.status_code == 201, resp.text
    job = resp.json()
    assert job["status"] == "failed"
    assert "Invalid verify level 'bogus'" in job["error"]
    assert "quick, standard, full" in job["error"]

    log_text = _job_log(client, operator_headers, job["id"])
    assert "[denied]" in log_text
    assert "run-all-tests.sh" not in log_text


def test_verify_nonzero_rc_fails_job(client, operator_headers, monkeypatch):
    def fake_run_command(cmd, **kwargs):  # noqa: ARG001
        return {"returncode": 1, "dry_run": False, "message": "failed rc=1"}

    monkeypatch.setattr(bridge, "run_command", fake_run_command)

    resp = _verify_job(client, operator_headers)
    assert resp.status_code == 201, resp.text
    job = resp.json()
    assert job["status"] == "failed"
    assert "verify standard failed (rc=1)" in job["error"]


def test_verify_viewer_forbidden(client, viewer_headers):
    resp = _verify_job(client, viewer_headers)
    assert resp.status_code == 403
