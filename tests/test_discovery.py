"""Discovery inbox tests: event ingestion, endpoints, claim/creds, bmc scan op."""

from __future__ import annotations

import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from starlette.websockets import WebSocketDisconnect

from app.db import SessionLocal
from app.models import AuditLog, DiscoveredBmc, DiscoveredNode, Environment
from app.services import agents
from app.services.crypto import decrypt_secret
from tests.test_agent_relay import _start_relay_pump


def _suffix() -> str:
    return uuid.uuid4().hex[:8]


@pytest.fixture(autouse=True)
def _reset_registry():
    yield
    agents.registry.reset()


def _create_env(client, headers, dry_run=None) -> dict:
    body = {"name": f"discovery-env-{_suffix()}"}
    if dry_run is not None:
        body["dry_run"] = dry_run
    resp = client.post("/api/v1/environments", headers=headers, json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _create_token(client, headers, env_id: str) -> dict:
    resp = client.post(
        f"/api/v1/environments/{env_id}/agent/token",
        headers=headers,
        json={"name": "default"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _handshake(ws, token: str) -> str:
    challenge = ws.receive_json()
    assert challenge["type"] == "challenge"
    ws.send_json({"type": "proof", "hmac": agents.proof_for(token, challenge["nonce"])})
    welcome = ws.receive_json()
    assert welcome["type"] == "welcome"
    return welcome["agent_id"]


def _wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def _submit(client, headers, env_id, operation, params=None):
    resp = client.post(
        f"/api/v1/environments/{env_id}/jobs",
        headers=headers,
        json={"operation": operation, "params": params or {}, "run_sync": True},
    )
    assert resp.status_code in (200, 201), resp.text
    return resp.json()


def _nodes(env_id: str) -> list[DiscoveredNode]:
    db = SessionLocal()
    try:
        return list(
            db.scalars(
                select(DiscoveredNode).where(DiscoveredNode.environment_id == env_id)
            ).all()
        )
    finally:
        db.close()


def _bmcs(env_id: str) -> list[DiscoveredBmc]:
    db = SessionLocal()
    try:
        return list(
            db.scalars(
                select(DiscoveredBmc).where(DiscoveredBmc.environment_id == env_id)
            ).all()
        )
    finally:
        db.close()


def _ingest_node(env_id: str, mac: str, **extra) -> None:
    payload = {"mac": mac, **extra}
    assert agents.handle_agent_event(
        env_id, {"kind": "pxe_request", "payload": payload}
    )


def _ingest_bmc(env_id: str, ip: str, **extra) -> None:
    payload = {"ip": ip, **extra}
    assert agents.handle_agent_event(env_id, {"kind": "bmc_found", "payload": payload})


# ---------------------------------------------------------------------------
# Event ingestion
# ---------------------------------------------------------------------------


def test_pxe_request_upsert_and_last_seen_bump(client, admin_headers):
    env = _create_env(client, admin_headers)
    _ingest_node(env["id"], "AA:BB:CC:DD:EE:01", ip="10.0.0.11", hostname="node-1")

    rows = _nodes(env["id"])
    assert len(rows) == 1
    node = rows[0]
    assert node.mac == "aa:bb:cc:dd:ee:01"  # normalized
    assert node.ip == "10.0.0.11"
    assert node.hostname == "node-1"
    assert node.state == "discovered"
    assert node.first_seen is not None and node.last_seen is not None

    # Re-sighting: same row, last_seen bumps, ip/hostname refresh
    db = SessionLocal()
    try:
        stale = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1)
        db.query(DiscoveredNode).filter_by(id=node.id).update({"last_seen": stale})
        db.commit()
        first_seen = db.get(DiscoveredNode, node.id).first_seen
    finally:
        db.close()

    _ingest_node(env["id"], "aa:bb:cc:dd:ee:01", ip="10.0.0.12")
    rows = _nodes(env["id"])
    assert len(rows) == 1
    assert rows[0].id == node.id
    assert rows[0].last_seen > stale
    assert rows[0].first_seen == first_seen
    assert rows[0].ip == "10.0.0.12"


def test_bmc_found_upsert_and_last_seen_bump(client, admin_headers):
    env = _create_env(client, admin_headers)
    _ingest_bmc(env["id"], "10.0.0.21", vendor="Dell", model="iDRAC9", title="BMC")

    rows = _bmcs(env["id"])
    assert len(rows) == 1
    bmc = rows[0]
    assert bmc.ip == "10.0.0.21"
    assert bmc.vendor == "Dell"
    assert bmc.model == "iDRAC9"
    assert bmc.state == "new"

    db = SessionLocal()
    try:
        stale = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1)
        db.query(DiscoveredBmc).filter_by(id=bmc.id).update({"last_seen": stale})
        db.commit()
    finally:
        db.close()

    _ingest_bmc(env["id"], "10.0.0.21", vendor="Dell Inc.")
    rows = _bmcs(env["id"])
    assert len(rows) == 1
    assert rows[0].id == bmc.id
    assert rows[0].last_seen > stale
    assert rows[0].vendor == "Dell Inc."


def test_invalid_event_payloads_ignored(client, admin_headers):
    env = _create_env(client, admin_headers)
    cases = [
        {"kind": "pxe_request", "payload": {"ip": "10.0.0.1"}},  # missing mac
        {"kind": "pxe_request", "payload": {"mac": "  "}},  # blank mac
        {"kind": "bmc_found", "payload": {"vendor": "Dell"}},  # missing ip
        {"kind": "bmc_found", "payload": {"ip": "not-an-ip"}},  # bad ip
        {"kind": "bmc_found", "payload": "oops"},  # non-dict payload
        {"kind": "disk_found", "payload": {"ip": "10.0.0.5"}},  # unknown kind
        {"payload": {"mac": "aa:bb:cc:dd:ee:ff"}},  # missing kind
    ]
    for frame in cases:
        assert agents.handle_agent_event(env["id"], frame) is False
    assert _nodes(env["id"]) == []
    assert _bmcs(env["id"]) == []


def test_ws_event_frame_ingested(client, admin_headers):
    """End-to-end: an event frame on the agent channel lands in the inbox."""
    env = _create_env(client, admin_headers)
    data = _create_token(client, admin_headers, env["id"])
    url = f"/api/v1/agents/connect?token={data['token']}"
    with client.websocket_connect(url) as ws:
        _handshake(ws, data["token"])
        ws.send_json(
            {
                "type": "event",
                "kind": "pxe_request",
                "payload": {"mac": "aa:bb:cc:dd:ee:02", "ip": "10.0.0.31"},
            }
        )
        ws.send_json(
            {
                "type": "event",
                "kind": "bmc_found",
                "payload": {"ip": "10.0.0.41", "vendor": "HPE"},
            }
        )
        assert _wait_for(lambda: len(_nodes(env["id"])) == 1)
        assert _wait_for(lambda: len(_bmcs(env["id"])) == 1)

    assert _nodes(env["id"])[0].mac == "aa:bb:cc:dd:ee:02"
    assert _bmcs(env["id"])[0].vendor == "HPE"


# ---------------------------------------------------------------------------
# GET /discovery
# ---------------------------------------------------------------------------


def test_get_discovery_shape_and_scoping(client, admin_headers, viewer_headers):
    env = _create_env(client, admin_headers)
    other = _create_env(client, admin_headers)
    _ingest_node(env["id"], "aa:bb:cc:dd:ee:10", ip="10.1.0.10")
    _ingest_node(env["id"], "aa:bb:cc:dd:ee:11", ip="10.1.0.11")
    _ingest_bmc(env["id"], "10.1.0.20", vendor="Dell")
    _ingest_node(other["id"], "aa:bb:cc:dd:ee:99")
    _ingest_bmc(other["id"], "10.9.9.9")

    resp = client.get(
        f"/api/v1/environments/{env['id']}/discovery", headers=viewer_headers
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert set(data) == {"hosts", "bmcs"}
    assert len(data["hosts"]) == 2
    assert len(data["bmcs"]) == 1

    host = data["hosts"][0]
    assert {
        "id",
        "environment_id",
        "mac",
        "ip",
        "hostname",
        "state",
        "first_seen",
        "last_seen",
    } <= set(host)
    # Newest-first by last_seen; only this env's rows
    macs = [h["mac"] for h in data["hosts"]]
    assert macs == ["aa:bb:cc:dd:ee:11", "aa:bb:cc:dd:ee:10"]
    assert all(h["environment_id"] == env["id"] for h in data["hosts"])
    assert data["bmcs"][0]["ip"] == "10.1.0.20"


# ---------------------------------------------------------------------------
# POST /discovery/claim
# ---------------------------------------------------------------------------


def test_claim_marks_node_and_updates_config_doc(
    client, admin_headers, operator_headers
):
    env = _create_env(client, admin_headers)
    _ingest_node(env["id"], "aa:bb:cc:dd:ee:20", ip="10.2.0.20")

    resp = client.post(
        f"/api/v1/environments/{env['id']}/discovery/claim",
        headers=operator_headers,
        json={"mac": "AA:BB:CC:DD:EE:20", "name": "compute-01", "roles": ["compute"]},
    )
    assert resp.status_code == 200, resp.text
    result = resp.json()
    assert result["ok"] is True
    assert result["node"]["state"] == "claimed"
    assert result["config_version"] == 1

    from app.services import envconfig as envconfig_service

    db = SessionLocal()
    try:
        env_row = db.get(Environment, env["id"])
        current = envconfig_service.get_current(db, env_row)
        assert current is not None
        doc, _row = current
        entry = doc["servers"]["compute-01"]
        assert entry["source"] == "baremetal"
        assert entry["roles"] == ["compute"]
        assert entry["ip"] == "10.2.0.20"
    finally:
        db.close()


def test_claim_unknown_mac_404(client, admin_headers, operator_headers):
    env = _create_env(client, admin_headers)
    resp = client.post(
        f"/api/v1/environments/{env['id']}/discovery/claim",
        headers=operator_headers,
        json={"mac": "aa:bb:cc:dd:ee:ff", "name": "node-x"},
    )
    assert resp.status_code == 404, resp.text


def test_claim_invalid_role_400(client, admin_headers, operator_headers):
    env = _create_env(client, admin_headers)
    _ingest_node(env["id"], "aa:bb:cc:dd:ee:21")
    resp = client.post(
        f"/api/v1/environments/{env['id']}/discovery/claim",
        headers=operator_headers,
        json={"mac": "aa:bb:cc:dd:ee:21", "name": "node-y", "roles": ["bogus"]},
    )
    assert resp.status_code == 400, resp.text
    assert "unknown role" in resp.json()["detail"]
    # The sighting stays unclaimed
    assert _nodes(env["id"])[0].state == "discovered"


# ---------------------------------------------------------------------------
# POST /discovery/bmc-creds
# ---------------------------------------------------------------------------


def test_bmc_creds_creates_baremetal_node(client, admin_headers, operator_headers):
    env = _create_env(client, admin_headers)
    _ingest_bmc(env["id"], "10.3.0.30", vendor="Dell")
    bmc = _bmcs(env["id"])[0]

    resp = client.post(
        f"/api/v1/environments/{env['id']}/discovery/bmc-creds",
        headers=operator_headers,
        json={
            "bmc_id": bmc.id,
            "name": "bm-01",
            "username": "root",
            "password": "calvin",
        },
    )
    assert resp.status_code == 200, resp.text
    result = resp.json()
    assert result["ok"] is True
    assert result["created"] is True
    assert result["bmc"]["state"] == "registered"
    node = result["node"]
    assert node["bmc_host"] == "10.3.0.30"
    assert node["bmc_username"] == "root"
    assert "bmc_password" not in node  # never serialized

    from app.models import BaremetalNode

    db = SessionLocal()
    try:
        row = db.get(BaremetalNode, node["id"])
        assert row is not None
        assert row.bmc_password.startswith("fernet:")
        assert row.bmc_password != "calvin"
        assert decrypt_secret(row.bmc_password) == "calvin"
        assert db.get(DiscoveredBmc, bmc.id).state == "registered"
    finally:
        db.close()

    # Second POST with the same name updates the existing node
    resp = client.post(
        f"/api/v1/environments/{env['id']}/discovery/bmc-creds",
        headers=operator_headers,
        json={
            "bmc_id": bmc.id,
            "name": "bm-01",
            "username": "admin",
            "password": "hunter2",
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["created"] is False
    db = SessionLocal()
    try:
        row = db.get(BaremetalNode, node["id"])
        assert row.bmc_username == "admin"
        assert decrypt_secret(row.bmc_password) == "hunter2"
    finally:
        db.close()


def test_bmc_creds_unknown_bmc_404(client, admin_headers, operator_headers):
    env = _create_env(client, admin_headers)
    resp = client.post(
        f"/api/v1/environments/{env['id']}/discovery/bmc-creds",
        headers=operator_headers,
        json={
            "bmc_id": str(uuid.uuid4()),
            "name": "bm-x",
            "username": "root",
            "password": "calvin",
        },
    )
    assert resp.status_code == 404, resp.text


# ---------------------------------------------------------------------------
# baremetal.bmc_scan op
# ---------------------------------------------------------------------------


def _fake_scan_agent_loop(ws, found: int = 2) -> None:
    """Answer scan_bmc command frames with one log line and a result payload."""
    import anyio

    try:
        while True:
            frame = ws.receive_json()
            if frame.get("type") == "command" and "scan_bmc" in frame:
                subnet = frame["scan_bmc"]["subnet"]
                assert subnet
                ws.send_json(
                    {"type": "log", "id": frame["id"], "line": f"sweeping {subnet}"}
                )
                ws.send_json(
                    {"type": "result", "id": frame["id"], "rc": 0, "found": found}
                )
    except (WebSocketDisconnect, anyio.EndOfStream):
        pass


def test_bmc_scan_in_catalog(client, admin_headers):
    resp = client.get("/api/v1/operations", headers=admin_headers)
    assert resp.status_code == 200
    ops = {op["id"]: op for op in resp.json()}
    op = ops["baremetal.bmc_scan"]
    assert op["required_role"] == "operator"
    assert op["mutating"] is True
    assert op["timeout_seconds"] == 900
    assert op["backend"] == "baremetal"
    assert op["params"][0]["name"] == "subnet"
    assert op["params"][0]["required"] is True


def test_bmc_scan_dry_run(client, admin_headers):
    # Global test config is dry_run=True; a default env inherits it
    env = _create_env(client, admin_headers)
    job = _submit(
        client,
        admin_headers,
        env["id"],
        "baremetal.bmc_scan",
        {"subnet": "10.4.0.0/24"},
    )
    assert job["status"] == "success", job["log_text"]
    assert job["dry_run"] is True
    assert "[dry-run] would scan 10.4.0.0/24" in job["log_text"]


def test_bmc_scan_invalid_cidr_fails(client, admin_headers):
    env = _create_env(client, admin_headers)
    job = _submit(
        client, admin_headers, env["id"], "baremetal.bmc_scan", {"subnet": "bogus"}
    )
    assert job["status"] == "failed"
    assert "invalid CIDR" in (job["error"] or "")


def test_bmc_scan_missing_subnet_rejected(client, admin_headers, operator_headers):
    env = _create_env(client, admin_headers)
    resp = client.post(
        f"/api/v1/environments/{env['id']}/jobs",
        headers=operator_headers,
        json={"operation": "baremetal.bmc_scan", "params": {}, "run_sync": True},
    )
    assert resp.status_code in (200, 201), resp.text
    job = resp.json()
    assert job["status"] == "failed"
    assert "subnet" in (job["error"] or "")


def test_bmc_scan_no_agent_fails_cleanly(client, admin_headers):
    env = _create_env(client, admin_headers, dry_run=False)
    job = _submit(
        client,
        admin_headers,
        env["id"],
        "baremetal.bmc_scan",
        {"subnet": "10.4.0.0/24"},
    )
    assert job["status"] == "failed"
    assert "no agent connected for this env" in (job["error"] or "")


def test_bmc_scan_routed_to_agent(client, admin_headers):
    env = _create_env(client, admin_headers, dry_run=False)
    data = _create_token(client, admin_headers, env["id"])
    url = f"/api/v1/agents/connect?token={data['token']}"
    stop = _start_relay_pump()
    try:
        with client.websocket_connect(url) as ws:
            _handshake(ws, data["token"])
            agent_thread = threading.Thread(
                target=_fake_scan_agent_loop,
                args=(ws,),
                kwargs={"found": 3},
                daemon=True,
            )
            agent_thread.start()

            job = _submit(
                client,
                admin_headers,
                env["id"],
                "baremetal.bmc_scan",
                {"subnet": "10.5.0.0/24"},
            )
            agent_thread.join(timeout=5)

        assert job["status"] == "success", job["log_text"]
        assert "sweeping 10.5.0.0/24" in job["log_text"]
        assert "3 found" in job["log_text"]
    finally:
        stop.set()

    # Audit carries the subnet and found count — never credentials
    db = SessionLocal()
    try:
        entry = db.scalar(
            select(AuditLog)
            .where(AuditLog.action == "env.bmc_scan")
            .where(AuditLog.environment_id == env["id"])
            .order_by(AuditLog.id.desc())
        )
        assert entry is not None
        assert entry.details == {"subnet": "10.5.0.0/24", "found": 3, "dry_run": False}
        assert entry.success is True
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------


def test_discovery_viewer_cannot_claim(client, admin_headers, viewer_headers):
    env = _create_env(client, admin_headers)
    _ingest_node(env["id"], "aa:bb:cc:dd:ee:30")
    resp = client.post(
        f"/api/v1/environments/{env['id']}/discovery/claim",
        headers=viewer_headers,
        json={"mac": "aa:bb:cc:dd:ee:30", "name": "node-z"},
    )
    assert resp.status_code == 403, resp.text
    resp = client.post(
        f"/api/v1/environments/{env['id']}/discovery/bmc-creds",
        headers=viewer_headers,
        json={"bmc_id": "x", "name": "bm-z", "username": "root", "password": "calvin"},
    )
    assert resp.status_code == 403, resp.text


def test_discovery_cross_tenant_forbidden(client, admin_headers):
    tenant_a = client.post(
        "/api/v1/tenants", headers=admin_headers, json={"name": f"disc-a-{_suffix()}"}
    ).json()
    tenant_b = client.post(
        "/api/v1/tenants", headers=admin_headers, json={"name": f"disc-b-{_suffix()}"}
    ).json()
    resp = client.post(
        "/api/v1/environments",
        headers=admin_headers,
        json={"name": f"disc-env-b-{_suffix()}", "tenant_id": tenant_b["id"]},
    )
    assert resp.status_code == 201, resp.text
    env_b = resp.json()

    username = f"disc-user-{_suffix()}"
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

    resp = client.get(f"/api/v1/environments/{env_b['id']}/discovery", headers=headers)
    assert resp.status_code == 403
    resp = client.post(
        f"/api/v1/environments/{env_b['id']}/discovery/claim",
        headers=headers,
        json={"mac": "aa:bb:cc:dd:ee:40", "name": "node-t"},
    )
    assert resp.status_code == 403
