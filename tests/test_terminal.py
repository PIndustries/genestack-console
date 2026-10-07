"""Deploy-host terminal tests: WS auth, guardrails, pty bridge, audit.

The pty bridge never touches a real host here: the
``settings.terminal_command_override`` hook points the spawned command at
``/bin/cat``, which echoes everything back through the pty.
"""

from __future__ import annotations

import threading
import time
import uuid

import pytest
from starlette.websockets import WebSocketDisconnect

from app.config import get_settings
from app.db import SessionLocal
from app.models import AuditLog
from app.routers import terminal


def _suffix() -> str:
    return uuid.uuid4().hex[:8]


@pytest.fixture(autouse=True)
def _reset_sessions():
    yield
    terminal._reset_sessions()


@pytest.fixture
def cat_override():
    """Point the terminal's spawned command at /bin/cat (no real ssh)."""
    settings = get_settings()
    old = settings.terminal_command_override
    settings.terminal_command_override = "/bin/cat"
    try:
        yield
    finally:
        settings.terminal_command_override = old


def _create_env(client, headers, deployer=True) -> dict:
    body = {"name": f"term-env-{_suffix()}"}
    if deployer:
        body["deployer_ssh_host"] = "deployer.example.com"
        body["deployer_ssh_user"] = "ubuntu"
    resp = client.post("/api/v1/environments", headers=headers, json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _ticket(client, headers: dict) -> str:
    """Mint a single-use terminal ticket for the given auth headers."""
    resp = client.post("/api/v1/auth/ticket", headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["ticket"]


def _ws_url(env_id: str, ticket: str | None = None) -> str:
    url = f"/api/v1/terminal?environment_id={env_id}"
    if ticket is not None:
        url += f"&ticket={ticket}"
    return url


def _login_token(client, username: str, password: str) -> str:
    resp = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


@pytest.fixture
def db(client):  # noqa: ARG001 - client ensures the app/tables exist
    from app.db import SessionLocal

    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def _collect_until(ws, marker: str, timeout: float = 10.0) -> str:
    """Receive output frames (in a thread so a broken bridge can't hang the
    test) until the accumulated text contains ``marker``."""
    acc: list[str] = []
    done = threading.Event()

    def _pump() -> None:
        try:
            while not done.is_set():
                frame = ws.receive_json()
                if frame.get("type") == "output":
                    acc.append(frame.get("data") or "")
                    if marker in "".join(acc):
                        done.set()
                        return
        except Exception:  # noqa: BLE001 — disconnect ends the pump
            done.set()

    thread = threading.Thread(target=_pump, daemon=True)
    thread.start()
    done.wait(timeout)
    return "".join(acc)


def _audit_entries(env_id: str, action: str) -> list[AuditLog]:
    db = SessionLocal()
    try:
        return (
            db.query(AuditLog)
            .filter(AuditLog.environment_id == env_id, AuditLog.action == action)
            .all()
        )
    finally:
        db.close()


def _wait_for_audit(env_id: str, action: str, timeout: float = 5.0) -> list[AuditLog]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        rows = _audit_entries(env_id, action)
        if rows:
            return rows
        time.sleep(0.05)
    return []


# ---------------------------------------------------------------------------
# Auth / guardrail rejections
# ---------------------------------------------------------------------------


def test_ws_rejects_missing_credentials(client, admin_headers):
    env = _create_env(client, admin_headers)
    with client.websocket_connect(_ws_url(env["id"])) as ws:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            ws.receive_json()
    assert exc_info.value.code == terminal.CLOSE_AUTH_FAILED


def test_ws_rejects_raw_token_query_param(client, admin_headers):
    # Raw credentials in the URL are no longer accepted (access-log leak).
    env = _create_env(client, admin_headers)
    url = f"/api/v1/terminal?environment_id={env['id']}&token=dev-operator-key"
    with client.websocket_connect(url) as ws:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            ws.receive_json()
    assert exc_info.value.code == terminal.CLOSE_AUTH_FAILED


def test_ws_rejects_bad_ticket(client, admin_headers):
    env = _create_env(client, admin_headers)
    with client.websocket_connect(_ws_url(env["id"], "gst_garbage")) as ws:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            ws.receive_json()
    assert exc_info.value.code == terminal.CLOSE_AUTH_FAILED


def test_ws_rejects_reused_ticket(client, admin_headers, cat_override):
    env = _create_env(client, admin_headers)
    url = _ws_url(env["id"], _ticket(client, admin_headers))
    with client.websocket_connect(url) as ws:
        ws.send_json({"type": "input", "data": "first"})
    # Same ticket again: consumed on first connect, so auth now fails.
    with client.websocket_connect(url) as ws:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            ws.receive_json()
    assert exc_info.value.code == terminal.CLOSE_AUTH_FAILED


def test_ws_rejects_viewer_role(client, admin_headers, viewer_headers):
    env = _create_env(client, admin_headers)
    with client.websocket_connect(_ws_url(env["id"], _ticket(client, viewer_headers))) as ws:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            ws.receive_json()
    assert exc_info.value.code == terminal.CLOSE_FORBIDDEN


def test_ws_rejects_operator_role(client, admin_headers, operator_headers):
    """The deploy-host shell now requires admin; operators are rejected."""
    env = _create_env(client, admin_headers)
    with client.websocket_connect(_ws_url(env["id"], _ticket(client, operator_headers))) as ws:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            ws.receive_json()
    assert exc_info.value.code == terminal.CLOSE_FORBIDDEN


def test_ws_allows_tenant_admin_session(client, admin_headers, db, cat_override):
    """A non-platform user with a tenant-admin membership gets a shell."""
    from app.models import Membership, Tenant, UserRole
    from app.services import accounts

    tenant = Tenant(name=f"term-tenant-{_suffix()}")
    db.add(tenant)
    db.flush()
    user = accounts.create_user(db, f"term-admin-{_suffix()}", "pw")
    db.add(Membership(user_id=user.id, tenant_id=tenant.id, role=UserRole.admin))
    db.commit()

    token = _login_token(client, user.username, "pw")
    resp = client.post(
        "/api/v1/environments",
        headers=admin_headers,
        json={
            "name": f"term-env-{_suffix()}",
            "tenant_id": tenant.id,
            "deployer_ssh_host": "deployer.example.com",
            "deployer_ssh_user": "ubuntu",
        },
    )
    assert resp.status_code == 201, resp.text
    env = resp.json()

    ticket = _ticket(client, {"Authorization": f"Bearer {token}"})
    with client.websocket_connect(_ws_url(env["id"], ticket)) as ws:
        marker = f"tenant-admin-{_suffix()}"
        ws.send_json({"type": "input", "data": marker + "\r"})
        assert marker in _collect_until(ws, marker)


def test_ws_rejects_unknown_environment(client, admin_headers):
    url = _ws_url(uuid.uuid4().hex, _ticket(client, admin_headers))
    with client.websocket_connect(url) as ws:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            ws.receive_json()
    assert exc_info.value.code == terminal.CLOSE_NOT_FOUND


def test_ws_rejects_unset_deployer(client, admin_headers):
    env = _create_env(client, admin_headers, deployer=False)
    with client.websocket_connect(_ws_url(env["id"], _ticket(client, admin_headers))) as ws:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            ws.receive_json()
    assert exc_info.value.code == terminal.CLOSE_NO_DEPLOYER


# ---------------------------------------------------------------------------
# pty bridge (stubbed ssh via /bin/cat)
# ---------------------------------------------------------------------------


def test_pty_bridge_roundtrip(client, admin_headers, cat_override):
    env = _create_env(client, admin_headers)
    url = _ws_url(env["id"], _ticket(client, admin_headers))
    with client.websocket_connect(url) as ws:
        marker = f"hello-terminal-{_suffix()}"
        ws.send_json({"type": "resize", "cols": 100, "rows": 40})
        ws.send_json({"type": "input", "data": marker + "\r"})
        received = _collect_until(ws, marker)
        assert marker in received


def test_pty_killed_on_ws_close(client, admin_headers, cat_override):
    env = _create_env(client, admin_headers)
    with client.websocket_connect(_ws_url(env["id"], _ticket(client, admin_headers))) as ws:
        ws.send_json({"type": "input", "data": "x"})
    # After the context exits the server finishes the session: registry empty.
    deadline = time.time() + 5
    while terminal._sessions and time.time() < deadline:
        time.sleep(0.05)
    assert terminal._sessions == {}


def test_second_connection_replaces_first(client, admin_headers, cat_override):
    env = _create_env(client, admin_headers)
    url1 = _ws_url(env["id"], _ticket(client, admin_headers))
    with client.websocket_connect(url1) as ws1:
        ws1.send_json({"type": "input", "data": "first"})
        url2 = _ws_url(env["id"], _ticket(client, admin_headers))
        with client.websocket_connect(url2) as ws2:
            ws2.send_json({"type": "input", "data": "second"})
        with pytest.raises(WebSocketDisconnect) as exc_info:
            while True:
                ws1.receive_json()
        assert exc_info.value.code == terminal.CLOSE_REPLACED


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


def test_audit_open_and_close(client, admin_headers, cat_override):
    env = _create_env(client, admin_headers)
    with client.websocket_connect(_ws_url(env["id"], _ticket(client, admin_headers))) as ws:
        ws.send_json({"type": "input", "data": "ping\r"})

    opened = _wait_for_audit(env["id"], "env.terminal.open")
    assert opened, "no env.terminal.open audit entry"
    entry = opened[0]
    assert entry.actor.startswith("admin:")
    # Non-reversible label: no substring of the API key may leak into the actor.
    assert "dev-admin-key" not in entry.actor
    assert "dev-adm" not in entry.actor
    assert entry.resource_type == "environment"
    assert entry.resource_id == env["id"]
    assert entry.details.get("target") == "ubuntu@deployer.example.com"

    closed = _wait_for_audit(env["id"], "env.terminal.close")
    assert closed, "no env.terminal.close audit entry"
    assert closed[0].actor.startswith("admin:")
    assert closed[0].details.get("target") == "ubuntu@deployer.example.com"


# ---------------------------------------------------------------------------
# No local-shell fallback.
# ---------------------------------------------------------------------------


def test_target_argv_never_falls_back_to_local_shell():
    """Missing deploy host must raise — never synthesize /bin/bash."""
    with pytest.raises(ValueError, match="local-shell fallback removed"):
        terminal._target_argv("", "ubuntu")
    with pytest.raises(ValueError, match="local-shell fallback removed"):
        terminal._target_argv("   ", None)
    argv = terminal._target_argv("deployer.example.com", "ubuntu")
    assert argv[0] == "ssh"
    assert "/bin/bash" not in argv
    assert "bash" not in argv


def test_ws_rejects_unset_deployer_never_spawns_bash(client, admin_headers, monkeypatch):
    """Regression: CLOSE_NO_DEPLOYER path must not spawn a local shell."""
    spawned: list[list[str]] = []

    def _capture_spawn(self, argv):
        spawned.append(list(argv))
        raise AssertionError("spawn must not run when deploy host is unset")

    monkeypatch.setattr(terminal.TerminalSession, "spawn", _capture_spawn)
    env = _create_env(client, admin_headers, deployer=False)
    with client.websocket_connect(_ws_url(env["id"], _ticket(client, admin_headers))) as ws:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            ws.receive_json()
    assert exc_info.value.code == terminal.CLOSE_NO_DEPLOYER
    assert spawned == []


def test_ws_rejects_unknown_machine(client, admin_headers, monkeypatch):
    """A hostname that is not in inventory never spawns a shell."""
    spawned: list[list[str]] = []

    def _capture_spawn(self, argv):
        spawned.append(list(argv))
        raise AssertionError("spawn must not run for an unknown machine")

    monkeypatch.setattr(terminal.TerminalSession, "spawn", _capture_spawn)
    env = _create_env(client, admin_headers)
    url = _ws_url(env["id"], _ticket(client, admin_headers)) + "&machine=not-in-inventory"
    with client.websocket_connect(url) as ws:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            ws.receive_json()
    assert exc_info.value.code == terminal.CLOSE_NOT_FOUND
    assert spawned == []


def test_inventory_machine_session_does_not_replace_deploy_host(
    client, admin_headers, cat_override
):
    env = _create_env(client, admin_headers)
    added = client.post(
        f"/api/v1/environments/{env['id']}/servers/static",
        headers=admin_headers,
        json={
            "hostname": "compute-01",
            "ip": "10.30.0.8",
            "private_ip": "10.40.0.8",
            "ssh_user": "ops",
        },
    )
    assert added.status_code == 201, added.text
    deploy_url = _ws_url(env["id"], _ticket(client, admin_headers))
    machine_url = (
        _ws_url(env["id"], _ticket(client, admin_headers)) + "&machine=compute-01"
    )
    with client.websocket_connect(deploy_url) as deploy:
        deploy.send_json({"type": "input", "data": "deploy"})
        with client.websocket_connect(machine_url) as machine:
            machine.send_json({"type": "input", "data": "machine"})
            assert len(terminal._sessions) == 2
            deploy.send_json({"type": "input", "data": "still-open"})
    opened = _wait_for_audit(env["id"], "env.terminal.open")
    targets = {row.details.get("target") for row in opened}
    assert "ubuntu@deployer.example.com" in targets
    assert "ops@10.40.0.8" in targets


def test_ubuntu_install_login_defaults_to_ubuntu(client, admin_headers, db):
    """A Genestack Ubuntu install with no recorded login uses ubuntu."""
    from app.models import BaremetalNode, Environment
    from app.services.crypto import encrypt_secret

    env = _create_env(client, admin_headers)
    added = client.post(
        f"/api/v1/environments/{env['id']}/servers/static",
        headers=admin_headers,
        json={"hostname": "compute-01", "ip": "10.30.0.8", "private_ip": "10.40.0.8"},
    )
    assert added.status_code == 201, added.text
    row = db.get(Environment, env["id"])
    assert row is not None
    row.deployer_ssh_user = "root"
    db.add(
        BaremetalNode(
            environment_id=env["id"],
            name="compute-01",
            bmc_host="bmc.example.com",
            bmc_username="root",
            bmc_password=encrypt_secret("secret"),
            boot_stage="ubuntu",
        )
    )
    db.commit()
    host, user = terminal._inventory_target(db, row, "compute-01")
    assert host == "10.40.0.8"
    assert user == "ubuntu"


def test_explicit_ssh_user_wins_over_ubuntu_install(client, admin_headers, db):
    from app.models import BaremetalNode, Environment
    from app.services.crypto import encrypt_secret

    env = _create_env(client, admin_headers)
    added = client.post(
        f"/api/v1/environments/{env['id']}/servers/static",
        headers=admin_headers,
        json={
            "hostname": "compute-01",
            "ip": "10.30.0.8",
            "private_ip": "10.40.0.8",
            "ssh_user": "ops",
        },
    )
    assert added.status_code == 201, added.text
    row = db.get(Environment, env["id"])
    db.add(
        BaremetalNode(
            environment_id=env["id"],
            name="compute-01",
            bmc_host="bmc.example.com",
            bmc_username="root",
            bmc_password=encrypt_secret("secret"),
            boot_stage="ubuntu",
        )
    )
    db.commit()
    _host, user = terminal._inventory_target(db, row, "compute-01")
    assert user == "ops"


def test_target_argv_passes_environment_key(monkeypatch):
    settings = get_settings()
    old = settings.terminal_command_override
    settings.terminal_command_override = ""
    monkeypatch.setattr(terminal, "_known_hosts_option", lambda: None)
    try:
        argv = terminal._target_argv("10.1.1.1", "ubuntu", "/tmp/gsc-not-a-real-key")
    finally:
        settings.terminal_command_override = old
    assert argv[-1] == "ubuntu@10.1.1.1"
    assert argv[argv.index("-i") + 1] == "/tmp/gsc-not-a-real-key"
    assert "BatchMode=yes" in argv


def test_override_ignores_identity_file(cat_override):
    argv = terminal._target_argv("10.1.1.1", "ubuntu", "/tmp/gsc-not-a-real-key")
    assert argv == ["/bin/cat"]


def test_ensure_server_ssh_user_records_ubuntu_once(client, admin_headers, db):
    from app.models import Environment
    from app.services import envconfig

    env = _create_env(client, admin_headers)
    added = client.post(
        f"/api/v1/environments/{env['id']}/servers/static",
        headers=admin_headers,
        json={"hostname": "compute-01", "ip": "10.30.0.8", "private_ip": "10.40.0.8"},
    )
    assert added.status_code == 201, added.text
    row = db.get(Environment, env["id"])
    assert envconfig.ensure_server_ssh_user(
        db, row, "test", hostname="compute-01", ssh_user="ubuntu"
    )
    db.commit()
    listed = client.get(
        f"/api/v1/environments/{env['id']}/servers", headers=admin_headers
    )
    assert listed.status_code == 200, listed.text
    match = next(s for s in listed.json()["servers"] if s["hostname"] == "compute-01")
    assert match["ssh_user"] == "ubuntu"
    assert (
        envconfig.ensure_server_ssh_user(
            db, row, "test", hostname="compute-01", ssh_user="ubuntu"
        )
        is False
    )
