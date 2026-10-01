"""agent.install job tests — push-install the agent onto a host over ssh."""

from __future__ import annotations

import hashlib
import re
import shlex
import uuid

import pytest

from app.config import get_settings
from app.db import SessionLocal
from app.services import agents
from app.services import genestack_bridge as bridge
from app.services.catalog import get_operation

ADVERTISE = "http://192.0.2.1:8080"
RAW_TOKEN_RE = re.compile(r"gsca_[A-Za-z0-9_-]{8,}")


def _suffix() -> str:
    return uuid.uuid4().hex[:8]


@pytest.fixture
def advertise_url(monkeypatch):
    monkeypatch.setattr(get_settings(), "hub_advertise_url", ADVERTISE)
    return ADVERTISE


def _create_env(client, headers, **fields) -> dict:
    resp = client.post(
        "/api/v1/environments",
        headers=headers,
        json={"name": f"env-agent-install-{_suffix()}", **fields},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _install_job(client, headers, env_id, params):
    resp = client.post(
        f"/api/v1/environments/{env_id}/jobs",
        headers=headers,
        json={"operation": "agent.install", "params": params, "run_sync": True},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _capture_commands(monkeypatch, rc_rules=None):
    """Capture every run_command call; rc_rules maps a cmd substring to a returncode.

    Like the real bridge, the fake logs the command through the provided log
    callback — so a token left unmasked WOULD land in the persisted job log.
    """
    captured: list[dict] = []

    def fake_run_command(cmd, **kwargs):
        display = shlex.join([str(c) for c in cmd])
        log = kwargs.get("log")
        if log:
            log(f"$ {display}")
        rc = 0
        for needle, code in (rc_rules or {}).items():
            if needle in display:
                rc = code
                break
        captured.append(
            {"cmd": [str(c) for c in cmd], "display": display, "kwargs": kwargs}
        )
        return {"returncode": rc, "dry_run": False, "message": f"rc={rc}"}

    monkeypatch.setattr(bridge, "run_command", fake_run_command)
    return captured


def _fresh_token_from(captured) -> str:
    """The raw token the install command carried (must be masked in logs)."""
    match = RAW_TOKEN_RE.search(captured[0]["display"])
    assert match, "expected a raw gsca_ token in the ssh command"
    return match.group(0)


# ---------------------------------------------------------------------------
# Catalog / settings shape
# ---------------------------------------------------------------------------


def test_agent_install_op_in_catalog():
    op = get_operation("agent.install")
    assert op is not None
    assert op.required_role == "admin"
    assert op.mutating is True
    assert op.backend == "agent"
    assert op.handler == "agent_install"
    assert op.timeout_seconds == 900
    params = {p.name: p for p in op.params}
    assert params["host"].required is True
    assert params["ssh_user"].required is False
    assert params["ssh_port"].required is False
    assert params["name"].required is False


def test_token_endpoint_uses_advertise_url(client, admin_headers, advertise_url):
    """With hub.advertise_url set, instructions/hub URLs use it, not headers."""
    env = _create_env(client, admin_headers)
    resp = client.post(
        f"/api/v1/environments/{env['id']}/agent/token",
        headers=admin_headers,
        json={"name": "default"},
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["hub_url"] == "ws://192.0.2.1:8080/api/v1/agents/connect"
    assert data["instructions"].startswith(f"curl -fsSL {ADVERTISE}/agent")
    assert " | bash -s -- --hub ws://192.0.2.1:8080 " in data["instructions"]
    assert f"GSC_HUB_URL={data['hub_url']}" in data["docker_run"]


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------


def test_agent_install_requires_advertise_url(client, admin_headers):
    env = _create_env(client, admin_headers, dry_run=False)
    job = _install_job(client, admin_headers, env["id"], {"host": "10.0.0.5"})
    assert job["status"] == "failed"
    assert "hub.advertise_url" in (job["error"] or "")


def test_agent_install_requires_environment(client, admin_headers, advertise_url):
    resp = client.post(
        "/api/v1/jobs",
        headers=admin_headers,
        json={
            "operation": "agent.install",
            "params": {"host": "10.0.0.5"},
            "run_sync": True,
        },
    )
    assert resp.status_code == 201, resp.text
    job = resp.json()
    assert job["status"] == "failed"
    assert "no environment assigned" in (job["error"] or "")
    assert "POST /api/v1/environments/<id>/jobs" in (job["error"] or "")


def test_agent_install_missing_host_rejected(client, admin_headers, advertise_url):
    env = _create_env(client, admin_headers)
    job = _install_job(client, admin_headers, env["id"], {})
    assert job["status"] == "failed"
    assert "host" in (job["error"] or "")


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------


def test_agent_install_dry_run_logs_both_commands_masked(
    client, admin_headers, advertise_url, monkeypatch
):
    """Global test config is dry_run=True: both ssh commands logged, token masked."""

    def boom(*args, **kwargs):  # noqa: ARG001
        raise AssertionError("subprocess.run must not be called in dry-run")

    monkeypatch.setattr(bridge.subprocess, "run", boom)

    env = _create_env(client, admin_headers)
    job = _install_job(client, admin_headers, env["id"], {"host": "10.0.0.5"})
    assert job["status"] == "success", job["error"]
    assert job["dry_run"] is True

    log_text = job["log_text"]
    # curl-pipe primary, over ssh to the target
    assert f"curl -fsSL {ADVERTISE}/agent" in log_text
    assert "--hub ws://192.0.2.1:8080" in log_text
    assert "root@10.0.0.5" in log_text
    # stdin fallback is planned too (packaged script piped to bash -s)
    assert "bash -s --" in log_text
    assert "bytes to stdin" in log_text
    # token only ever appears masked (shell-quoted by shlex in the display)
    assert "--token" in log_text
    assert "gsca_***" in log_text
    assert not RAW_TOKEN_RE.search(log_text)


# ---------------------------------------------------------------------------
# Credential rotation + ssh execution
# ---------------------------------------------------------------------------


def test_agent_install_rotates_credential_and_masks_token(
    client, admin_headers, advertise_url, monkeypatch
):
    env = _create_env(client, admin_headers, dry_run=False)
    resp = client.post(
        f"/api/v1/environments/{env['id']}/agent/token",
        headers=admin_headers,
        json={"name": "old"},
    )
    assert resp.status_code == 201, resp.text
    old = resp.json()
    old_hash = hashlib.sha256(old["token"].encode()).hexdigest()

    captured = _capture_commands(monkeypatch)
    job = _install_job(
        client, admin_headers, env["id"], {"host": "10.0.0.5", "name": "edge-agent"}
    )
    assert job["status"] == "success", job["log_text"]

    # A fresh credential replaced the old one (replace-on-create).
    db = SessionLocal()
    try:
        cred = agents.credential_for_env(db, env["id"])
        assert cred is not None
        assert cred.id != old["agent_id"]
        assert cred.token_hash != old_hash
        assert cred.name == "edge-agent"
    finally:
        db.close()

    # The ssh command carried the fresh raw token; the persisted log never does.
    raw = _fresh_token_from(captured)
    assert hashlib.sha256(raw.encode()).hexdigest() == cred.token_hash
    assert "--token gsca_***" in job["log_text"]
    assert not RAW_TOKEN_RE.search(job["log_text"])

    # Audit records the host, never the token.
    audit = client.get(
        "/api/v1/audit",
        headers=admin_headers,
        params={"environment_id": env["id"], "action": "env.agent.install"},
    )
    entries = [e for e in audit.json() if e["action"] == "env.agent.install"]
    assert entries, "expected an env.agent.install audit entry"
    details = entries[0]["details"]
    assert details["host"] == "10.0.0.5"
    assert "token" not in details
    assert not RAW_TOKEN_RE.search(str(details))


def test_agent_install_curl_primary_then_stdin_fallback(
    client, admin_headers, advertise_url, monkeypatch
):
    env = _create_env(client, admin_headers, dry_run=False)
    captured = _capture_commands(monkeypatch, rc_rules={"curl -fsSL": 1})

    job = _install_job(
        client,
        admin_headers,
        env["id"],
        {"host": "10.0.0.5", "ssh_user": "ubuntu", "name": "edge"},
    )
    assert job["status"] == "success", job["log_text"]

    # Primary, stdin fallback, then the post-install docker inspect probe.
    assert len(captured) == 3
    curl_call, stdin_call, verify_call = captured
    # Primary: curl-pipe over the bridge ssh wrapping
    assert curl_call["kwargs"]["ssh_target"] == "ubuntu@10.0.0.5"
    assert f"curl -fsSL {ADVERTISE}/agent | bash -s --" in curl_call["display"]
    # Fallback: packaged install.sh piped over ssh stdin into bash -s
    assert stdin_call["cmd"][:4] == ["bash", "-s", "--", "--hub"]
    assert "--name" in stdin_call["cmd"]
    assert "edge" in stdin_call["cmd"]
    script = stdin_call["kwargs"]["input_text"]
    assert "Genestack Console agent installer" in script
    # Post-install verification: docker inspect of the named container
    assert "docker inspect" in verify_call["display"]
    assert "edge" in verify_call["display"]
    assert "trying fallback" in job["log_text"]


def test_agent_install_curl_success_skips_fallback(
    client, admin_headers, advertise_url, monkeypatch
):
    env = _create_env(client, admin_headers, dry_run=False)
    captured = _capture_commands(monkeypatch)

    job = _install_job(client, admin_headers, env["id"], {"host": "10.0.0.5"})
    assert job["status"] == "success", job["log_text"]
    # curl-pipe primary + post-install docker inspect probe; no stdin fallback
    assert len(captured) == 2
    assert "curl -fsSL" in captured[0]["display"]
    assert "docker inspect" in captured[1]["display"]
    for call in captured:
        assert call["kwargs"].get("input_text") is None


def test_agent_install_both_methods_failing_fails_job(
    client, admin_headers, advertise_url, monkeypatch
):
    env = _create_env(client, admin_headers, dry_run=False)
    captured = _capture_commands(monkeypatch, rc_rules={"": 1})  # everything rc=1

    job = _install_job(client, admin_headers, env["id"], {"host": "10.0.0.5"})
    assert job["status"] == "failed"
    assert "rc=1" in (job["error"] or "")
    assert len(captured) == 2  # curl tried, then stdin fallback tried


def test_agent_install_custom_ssh_port(
    client, admin_headers, advertise_url, monkeypatch
):
    env = _create_env(client, admin_headers, dry_run=False)
    captured = _capture_commands(monkeypatch)

    job = _install_job(
        client, admin_headers, env["id"], {"host": "10.0.0.5", "ssh_port": 2222}
    )
    assert job["status"] == "success", job["log_text"]
    argv = captured[0]["cmd"]
    assert argv[0] == "ssh"
    assert "-p" in argv and "2222" in argv
    assert "root@10.0.0.5" in argv
    assert captured[0]["kwargs"].get("ssh_target") is None


# ---------------------------------------------------------------------------
# Authz
# ---------------------------------------------------------------------------


def test_agent_install_operator_forbidden(client, admin_headers, operator_headers):
    env = _create_env(client, admin_headers)
    resp = client.post(
        f"/api/v1/environments/{env['id']}/jobs",
        headers=operator_headers,
        json={
            "operation": "agent.install",
            "params": {"host": "10.0.0.5"},
            "run_sync": True,
        },
    )
    assert resp.status_code == 403


def test_agent_install_cross_tenant_forbidden(client, admin_headers):
    """A tenant admin of tenant A cannot push-install an agent into B's env."""
    tenant_a = client.post(
        "/api/v1/tenants", headers=admin_headers, json={"name": f"t-a-{_suffix()}"}
    ).json()
    tenant_b = client.post(
        "/api/v1/tenants", headers=admin_headers, json={"name": f"t-b-{_suffix()}"}
    ).json()
    resp = client.post(
        "/api/v1/environments",
        headers=admin_headers,
        json={"name": f"env-b-{_suffix()}", "tenant_id": tenant_b["id"]},
    )
    assert resp.status_code == 201, resp.text
    env_b = resp.json()

    username = f"admin-a-{_suffix()}"
    resp = client.post(
        "/api/v1/users",
        headers=admin_headers,
        json={
            "username": username,
            "password": "pw",
            "memberships": [{"tenant_id": tenant_a["id"], "role": "admin"}],
        },
    )
    assert resp.status_code == 201, resp.text
    login = client.post(
        "/api/v1/auth/login", json={"username": username, "password": "pw"}
    )
    headers = {"Authorization": f"Bearer {login.json()['token']}"}

    resp = client.post(
        f"/api/v1/environments/{env_b['id']}/jobs",
        headers=headers,
        json={
            "operation": "agent.install",
            "params": {"host": "10.0.0.5"},
            "run_sync": True,
        },
    )
    assert resp.status_code == 403
