"""Agent channel hub tests: tokens, WS handshake, registry, ops."""

from __future__ import annotations

import hashlib
import threading
import time
import uuid

import pytest
from starlette.websockets import WebSocketDisconnect

from app.db import SessionLocal
from app.models import AgentCredential
from app.services import agents
from tests.test_agent_relay import _start_relay_pump


def _suffix() -> str:
    return uuid.uuid4().hex[:8]


@pytest.fixture(autouse=True)
def _reset_registry():
    yield
    agents.registry.reset()


def _create_env(client, headers, prefix="agent-env", dry_run=None) -> dict:
    body = {"name": f"{prefix}-{_suffix()}"}
    if dry_run is not None:
        body["dry_run"] = dry_run
    resp = client.post("/api/v1/environments", headers=headers, json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _create_token(client, headers, env_id: str, name: str = "default") -> dict:
    resp = client.post(
        f"/api/v1/environments/{env_id}/agent/token",
        headers=headers,
        json={"name": name},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _handshake(ws, token: str) -> str:
    """Complete the challenge/proof handshake as a fake agent; returns agent_id."""
    challenge = ws.receive_json()
    assert challenge["type"] == "challenge"
    ws.send_json({"type": "proof", "hmac": agents.proof_for(token, challenge["nonce"])})
    welcome = ws.receive_json()
    assert welcome["type"] == "welcome"
    return welcome["agent_id"]


def _fake_agent_loop(ws, rc: int = 0) -> None:
    """Answer command frames with one log line and a result, until disconnect."""
    import anyio

    try:
        while True:
            frame = ws.receive_json()
            if frame.get("type") == "command":
                ws.send_json(
                    {"type": "log", "id": frame["id"], "line": "fake-output-line"}
                )
                ws.send_json(
                    {
                        "type": "result",
                        "id": frame["id"],
                        "rc": rc,
                        "stdout": "fake stdout",
                        "stderr": "",
                    }
                )
    except (WebSocketDisconnect, anyio.EndOfStream):
        pass


def _dying_agent_loop(ws) -> None:
    """Receive one command frame, then drop the socket without answering."""
    import contextlib

    with contextlib.suppress(Exception):
        ws.receive_json()
        ws.close()


def _wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def _get_credential(agent_id: str) -> AgentCredential | None:
    db = SessionLocal()
    try:
        return db.get(AgentCredential, agent_id)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Token creation
# ---------------------------------------------------------------------------


def test_token_create_stores_hash_only(client, admin_headers):
    env = _create_env(client, admin_headers)
    data = _create_token(client, admin_headers, env["id"])

    assert data["token"].startswith(agents.TOKEN_PREFIX)
    assert data["environment_id"] == env["id"]
    assert data["hub_url"].endswith("/api/v1/agents/connect")
    assert data["hub_url"].startswith(("ws://", "wss://"))
    # Curl-pipe one-liner carries the hub base URL + raw token
    assert data["instructions"].startswith("curl -fsSL http://")
    assert " | bash -s -- --hub ws://" in data["instructions"]
    assert f"--token {data['token']}" in data["instructions"]
    # Docker one-liner kept for compatibility
    assert "docker run -d" in data["docker_run"]
    assert data["token"] in data["docker_run"]
    assert f"GSC_HUB_URL={data['hub_url']}" in data["docker_run"]

    cred = _get_credential(data["agent_id"])
    assert cred is not None
    # Only the sha256 hash is stored; the raw token appears nowhere
    assert cred.token_hash == hashlib.sha256(data["token"].encode()).hexdigest()
    assert cred.token_hash != data["token"]
    assert agents.TOKEN_PREFIX not in cred.token_hash


def test_token_instructions_https_maps_to_wss(client, admin_headers):
    """Behind an https reverse proxy the one-liner must use https + wss."""
    env = _create_env(client, admin_headers)
    resp = client.post(
        f"/api/v1/environments/{env['id']}/agent/token",
        headers={**admin_headers, "x-forwarded-proto": "https"},
        json={"name": "default"},
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["instructions"].startswith("curl -fsSL https://")
    assert " | bash -s -- --hub wss://" in data["instructions"]
    assert f"--token {data['token']}" in data["instructions"]
    assert data["hub_url"].startswith("wss://")


def test_agent_install_script_route(client):
    """GET /agent self-hosts the install script, unauthenticated."""
    resp = client.get("/agent")
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/plain")
    assert "Genestack Console agent installer" in resp.text
    assert "--hub <ws(s)://host[:port]>" in resp.text


def test_agent_src_routes(client):
    """GET /agent-src/{file} serves main.py + Containerfile, unauthenticated."""
    for name, ctype in (("main.py", "text/x-python"), ("Containerfile", "text/plain")):
        resp = client.get(f"/agent-src/{name}")
        assert resp.status_code == 200, resp.text
        assert resp.headers["content-type"].startswith(ctype)
        assert resp.text.strip()


def test_agent_src_rejects_other_files(client):
    """Only the allowlisted names are served — no path traversal, no /../ etc."""
    for name in ("../app/config.py", "config.py", "setup.py", "evil%2F.py"):
        resp = client.get(f"/agent-src/{name}")
        assert resp.status_code == 404, f"{name} should 404, got {resp.status_code}"


def test_install_script_derives_src_base_from_hub(client):
    """The served installer no longer hardcodes a placeholder src URL."""
    resp = client.get("/agent")
    assert resp.status_code == 200
    text = resp.text
    assert "get.genestack.dev" not in text
    assert "agent-src" in text


def test_token_create_replaces_previous(client, admin_headers):
    env = _create_env(client, admin_headers)
    first = _create_token(client, admin_headers, env["id"])
    second = _create_token(client, admin_headers, env["id"])
    assert first["agent_id"] != second["agent_id"]
    assert _get_credential(first["agent_id"]) is None
    assert _get_credential(second["agent_id"]) is not None


def test_token_create_multiple_named_credentials(client, admin_headers):
    """Different names add credentials instead of replacing (HA agents)."""
    env = _create_env(client, admin_headers)
    default = _create_token(client, admin_headers, env["id"], name="default")
    second = _create_token(client, admin_headers, env["id"], name="agent-b")
    assert default["agent_id"] != second["agent_id"]
    assert default["name"] == "default"
    assert second["name"] == "agent-b"
    # Both credentials coexist and either token is usable
    assert _get_credential(default["agent_id"]) is not None
    assert _get_credential(second["agent_id"]) is not None


def test_token_create_replaces_only_same_name(client, admin_headers):
    env = _create_env(client, admin_headers)
    first = _create_token(client, admin_headers, env["id"], name="default")
    other = _create_token(client, admin_headers, env["id"], name="agent-b")
    rotated = _create_token(client, admin_headers, env["id"], name="default")
    # The "default" credential was rotated...
    assert rotated["agent_id"] != first["agent_id"]
    assert _get_credential(first["agent_id"]) is None
    assert _get_credential(rotated["agent_id"]) is not None
    # ...and the other named credential is untouched
    assert _get_credential(other["agent_id"]) is not None


def test_token_create_requires_admin(
    client, admin_headers, operator_headers, viewer_headers
):
    env = _create_env(client, admin_headers)
    for headers in (viewer_headers, operator_headers):
        resp = client.post(
            f"/api/v1/environments/{env['id']}/agent/token",
            headers=headers,
            json={"name": "default"},
        )
        assert resp.status_code == 403, resp.text


def test_token_create_cross_tenant_forbidden(client, admin_headers):
    """A tenant admin of tenant A cannot enroll an agent for tenant B's env."""
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
        f"/api/v1/environments/{env_b['id']}/agent/token",
        headers=headers,
        json={"name": "default"},
    )
    assert resp.status_code == 403


def test_status_unenrolled(client, admin_headers, viewer_headers):
    env = _create_env(client, admin_headers)
    resp = client.get(
        f"/api/v1/environments/{env['id']}/agent/status", headers=viewer_headers
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["enrolled"] is False
    assert data["connected"] is False
    assert data["agent_id"] is None


# ---------------------------------------------------------------------------
# WebSocket handshake
# ---------------------------------------------------------------------------


def test_ws_rejects_bad_token(client, admin_headers):
    env = _create_env(client, admin_headers)
    _create_token(client, admin_headers, env["id"])
    with client.websocket_connect("/api/v1/agents/connect?token=gsca_garbage") as ws:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            ws.receive_json()
    assert exc_info.value.code == 4001


def test_ws_rejects_wrong_proof(client, admin_headers):
    env = _create_env(client, admin_headers)
    data = _create_token(client, admin_headers, env["id"])
    url = f"/api/v1/agents/connect?token={data['token']}"
    with client.websocket_connect(url) as ws:
        challenge = ws.receive_json()
        assert challenge["type"] == "challenge"
        ws.send_json({"type": "proof", "hmac": "0" * 64})
        with pytest.raises(WebSocketDisconnect) as exc_info:
            ws.receive_json()
    assert exc_info.value.code == 4001


def test_ws_handshake_accept_and_hello_heartbeat(client, admin_headers):
    env = _create_env(client, admin_headers)
    data = _create_token(client, admin_headers, env["id"])
    url = f"/api/v1/agents/connect?token={data['token']}"
    with client.websocket_connect(url) as ws:
        agent_id = _handshake(ws, data["token"])
        assert agent_id == data["agent_id"]

        ws.send_json(
            {
                "type": "hello",
                "agent_id": agent_id,
                "version": "0.1.0",
                "hostname": "agent-node-1",
                "caps": ["command"],
            }
        )
        ws.send_json({"type": "heartbeat", "ts": time.time()})

        # last_seen/hostname/version are mirrored onto the credential row
        def _db_updated():
            cred = _get_credential(agent_id)
            return (
                cred is not None
                and cred.last_seen is not None
                and cred.hostname == "agent-node-1"
                and cred.version == "0.1.0"
            )

        assert _wait_for(_db_updated), "credential row not updated by hello/heartbeat"

        # status endpoint reports the live connection
        def _connected():
            resp = client.get(
                f"/api/v1/environments/{env['id']}/agent/status", headers=admin_headers
            )
            body = resp.json()
            return body["connected"] and body["hostname"] == "agent-node-1"

        assert _wait_for(_connected), "status endpoint did not report connected"

    # After the socket closes the registry forgets the agent
    assert _wait_for(lambda: agents.registry.record_for_env(env["id"]) is None)


def test_offline_marked_after_silence(client, admin_headers):
    env = _create_env(client, admin_headers)
    data = _create_token(client, admin_headers, env["id"])
    url = f"/api/v1/agents/connect?token={data['token']}"
    with client.websocket_connect(url) as ws:
        agent_id = _handshake(ws, data["token"])
        assert _wait_for(lambda: agents.registry.get(agent_id) is not None)
        record = agents.registry.get(agent_id)
        assert record is not None and record.online()

        # Simulate 45s of silence: the agent now reports offline
        record.last_frame_at = time.monotonic() - (agents.OFFLINE_AFTER_SECONDS + 1)
        resp = client.get(
            f"/api/v1/environments/{env['id']}/agent/status", headers=admin_headers
        )
        assert resp.status_code == 200
        assert resp.json()["connected"] is False


# ---------------------------------------------------------------------------
# Command routing
# ---------------------------------------------------------------------------


def test_command_roundtrip_via_registry(client, admin_headers):
    env = _create_env(client, admin_headers)
    data = _create_token(client, admin_headers, env["id"])
    url = f"/api/v1/agents/connect?token={data['token']}"
    with client.websocket_connect(url) as ws:
        _handshake(ws, data["token"])
        agent_thread = threading.Thread(
            target=_fake_agent_loop, args=(ws,), daemon=True
        )
        agent_thread.start()

        lines: list[str] = []
        result = agents.registry.run_command_sync(
            env["id"], ["uptime"], timeout=10, log_cb=lines.append
        )
        agent_thread.join(timeout=5)
        assert result["rc"] == 0
        assert result["stdout"] == "fake stdout"
        assert lines == ["fake-output-line"]


def test_command_sync_no_agent_raises(client, admin_headers):
    env = _create_env(client, admin_headers)
    with pytest.raises(agents.AgentUnavailableError, match="no agent connected"):
        agents.registry.run_command_sync(env["id"], ["uptime"], timeout=1)


# ---------------------------------------------------------------------------
# HA: several agents per environment
# ---------------------------------------------------------------------------


def test_two_agents_registered_for_one_env(client, admin_headers):
    env = _create_env(client, admin_headers)
    tok_a = _create_token(client, admin_headers, env["id"], name="agent-a")
    tok_b = _create_token(client, admin_headers, env["id"], name="agent-b")
    url_a = f"/api/v1/agents/connect?token={tok_a['token']}"
    url_b = f"/api/v1/agents/connect?token={tok_b['token']}"
    with client.websocket_connect(url_a) as ws_a:
        id_a = _handshake(ws_a, tok_a["token"])
        with client.websocket_connect(url_b) as ws_b:
            id_b = _handshake(ws_b, tok_b["token"])
            assert id_a != id_b

            assert _wait_for(
                lambda: len(agents.registry.records_for_env(env["id"])) == 2
            )
            ids = {r.agent_id for r in agents.registry.records_for_env(env["id"])}
            assert ids == {id_a, id_b}

            # Routing is deterministic round-robin: consecutive picks rotate.
            first = agents.registry.record_for_env(env["id"])
            second = agents.registry.record_for_env(env["id"])
            assert first is not None and second is not None
            assert first.agent_id != second.agent_id

        # After one agent disconnects the other keeps serving the env.
        assert _wait_for(lambda: len(agents.registry.records_for_env(env["id"])) == 1)
        record = agents.registry.record_for_env(env["id"])
        assert record is not None and record.agent_id == id_a


def test_command_failover_to_second_agent(client, admin_headers):
    """The first agent's socket dies mid-command; the second completes it."""
    env = _create_env(client, admin_headers)
    tok_a = _create_token(client, admin_headers, env["id"], name="agent-a")
    tok_b = _create_token(client, admin_headers, env["id"], name="agent-b")
    url_a = f"/api/v1/agents/connect?token={tok_a['token']}"
    url_b = f"/api/v1/agents/connect?token={tok_b['token']}"
    with client.websocket_connect(url_a) as ws_a:
        _handshake(ws_a, tok_a["token"])
        with client.websocket_connect(url_b) as ws_b:
            _handshake(ws_b, tok_b["token"])
            assert _wait_for(
                lambda: len(agents.registry.records_for_env(env["id"])) == 2
            )

            # agent-b answers commands; agent-a takes the frame and drops.
            agent_b_thread = threading.Thread(
                target=_fake_agent_loop, args=(ws_b,), daemon=True
            )
            agent_b_thread.start()
            die_thread = threading.Thread(
                target=_dying_agent_loop, args=(ws_a,), daemon=True
            )
            die_thread.start()

            # Round-robin picks the first-connected agent (agent-a) first.
            result = agents.registry.run_command_sync(env["id"], ["uptime"], timeout=10)
            die_thread.join(timeout=5)
            agent_b_thread.join(timeout=5)

            assert result["rc"] == 0
            assert result["stdout"] == "fake stdout"


def test_status_lists_agents_with_compat_fields(client, admin_headers):
    env = _create_env(client, admin_headers)

    # Unenrolled: empty agent list, zero connected
    resp = client.get(
        f"/api/v1/environments/{env['id']}/agent/status", headers=admin_headers
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["agents"] == []
    assert body["connected_count"] == 0

    tok_a = _create_token(client, admin_headers, env["id"], name="agent-a")
    tok_b = _create_token(client, admin_headers, env["id"], name="agent-b")
    url_a = f"/api/v1/agents/connect?token={tok_a['token']}"
    url_b = f"/api/v1/agents/connect?token={tok_b['token']}"
    with client.websocket_connect(url_a) as ws_a:
        id_a = _handshake(ws_a, tok_a["token"])
        ws_a.send_json(
            {
                "type": "hello",
                "agent_id": id_a,
                "version": "1.0.0",
                "hostname": "node-a",
                "caps": ["command"],
            }
        )
        with client.websocket_connect(url_b) as ws_b:
            id_b = _handshake(ws_b, tok_b["token"])

            def _both_connected():
                data = client.get(
                    f"/api/v1/environments/{env['id']}/agent/status",
                    headers=admin_headers,
                ).json()
                return data["connected_count"] == 2 and any(
                    a.get("hostname") == "node-a" for a in data["agents"]
                )

            assert _wait_for(_both_connected), "status did not report both agents"
            body = client.get(
                f"/api/v1/environments/{env['id']}/agent/status",
                headers=admin_headers,
            ).json()

            # HA list: one entry per agent with live connection state
            assert body["connected_count"] == 2
            assert len(body["agents"]) == 2
            by_name = {a["name"]: a for a in body["agents"]}
            assert set(by_name) == {"agent-a", "agent-b"}
            entry_a = by_name["agent-a"]
            assert entry_a["agent_id"] == id_a
            assert entry_a["connected"] is True
            assert entry_a["hostname"] == "node-a"
            assert entry_a["version"] == "1.0.0"
            assert entry_a["last_seen"] is not None
            assert by_name["agent-b"]["agent_id"] == id_b
            assert by_name["agent-b"]["connected"] is True

            # Backward-compat single-agent fields describe a connected agent
            assert body["connected"] is True
            assert body["enrolled"] is True
            assert body["agent_id"] in {id_a, id_b}
            assert body["credential_name"] in {"agent-a", "agent-b"}
            assert body["hostname"] is not None
            assert body["last_seen"] is not None


# ---------------------------------------------------------------------------
# Ops: agent.command / agent.status jobs
# ---------------------------------------------------------------------------


def _submit_job(client, headers, env_id: str, operation: str, params: dict) -> dict:
    resp = client.post(
        f"/api/v1/environments/{env_id}/jobs",
        headers=headers,
        json={"operation": operation, "params": params, "run_sync": True},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_agent_command_job_end_to_end(client, admin_headers):
    env = _create_env(client, admin_headers, dry_run=False)
    data = _create_token(client, admin_headers, env["id"])
    url = f"/api/v1/agents/connect?token={data['token']}"
    stop = _start_relay_pump()
    try:
        with client.websocket_connect(url) as ws:
            _handshake(ws, data["token"])
            agent_thread = threading.Thread(
                target=_fake_agent_loop, args=(ws,), daemon=True
            )
            agent_thread.start()

            job = _submit_job(
                client, admin_headers, env["id"], "agent.command", {"command": "uptime"}
            )
            agent_thread.join(timeout=5)

            assert job["status"] == "success", job["log_text"]
            assert "fake-output-line" in job["log_text"]
            assert job["dry_run"] is False
    finally:
        stop.set()


def test_agent_command_job_failing_rc_marks_job_failed(client, admin_headers):
    env = _create_env(client, admin_headers, dry_run=False)
    data = _create_token(client, admin_headers, env["id"])
    url = f"/api/v1/agents/connect?token={data['token']}"
    stop = _start_relay_pump()
    try:
        with client.websocket_connect(url) as ws:
            _handshake(ws, data["token"])
            agent_thread = threading.Thread(
                target=_fake_agent_loop, args=(ws,), kwargs={"rc": 3}, daemon=True
            )
            agent_thread.start()

            job = _submit_job(
                client,
                admin_headers,
                env["id"],
                "agent.command",
                {"command": "hostname"},
            )
            agent_thread.join(timeout=5)
            assert job["status"] == "failed"
            assert "rc=3" in (job["error"] or "") or "rc=3" in job["log_text"]
    finally:
        stop.set()


def test_agent_command_job_no_agent_fails_cleanly(client, admin_headers):
    env = _create_env(client, admin_headers, dry_run=False)
    job = _submit_job(
        client, admin_headers, env["id"], "agent.command", {"command": "uptime"}
    )
    assert job["status"] == "failed"
    assert "no agent connected for this env" in (job["error"] or "")


def test_agent_command_rejects_non_allowlisted(client, admin_headers):
    env = _create_env(client, admin_headers, dry_run=False)
    job = _submit_job(
        client, admin_headers, env["id"], "agent.command", {"command": "rm -rf /"}
    )
    assert job["status"] == "failed"
    assert "not allowlisted" in (job["error"] or "")


def test_agent_command_dry_run_needs_no_agent(client, admin_headers):
    # Global test config is dry_run=True; a default env inherits it
    env = _create_env(client, admin_headers)
    job = _submit_job(
        client, admin_headers, env["id"], "agent.command", {"command": "uptime"}
    )
    assert job["status"] == "success", job["log_text"]
    assert job["dry_run"] is True
    assert "[dry-run]" in job["log_text"]


def test_agent_status_op(client, admin_headers):
    env = _create_env(client, admin_headers)
    _create_token(client, admin_headers, env["id"])
    job = _submit_job(client, admin_headers, env["id"], "agent.status", {})
    assert job["status"] == "success", job["log_text"]
    assert "enrolled=True" in job["log_text"]
    assert "connected=False" in job["log_text"]
