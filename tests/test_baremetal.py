"""Bare-metal ops tests — register/power/pxe_boot/provision via the jobs API."""

from __future__ import annotations

import uuid

import yaml
from sqlalchemy import select


def _suffix() -> str:
    return uuid.uuid4().hex[:8]


def _create_env(client, headers, **fields):
    body = {"name": f"baremetal-env-{_suffix()}", **fields}
    resp = client.post("/api/v1/environments", headers=headers, json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _submit(client, headers, env_id, operation, params=None):
    resp = client.post(
        f"/api/v1/environments/{env_id}/jobs",
        headers=headers,
        json={"operation": operation, "params": params or {}, "run_sync": True},
    )
    assert resp.status_code in (200, 201), resp.text
    return resp.json()


def _create_tenant(client, admin_headers):
    resp = client.post(
        "/api/v1/tenants", headers=admin_headers, json={"name": f"tenant-{_suffix()}"}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _create_user(client, admin_headers, memberships=None):
    username = f"user-{_suffix()}"
    resp = client.post(
        "/api/v1/users",
        headers=admin_headers,
        json={"username": username, "password": "pw", "memberships": memberships or []},
    )
    assert resp.status_code == 201, resp.text
    return username


def _login_headers(client, username):
    resp = client.post(
        "/api/v1/auth/login", json={"username": username, "password": "pw"}
    )
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


def _node_row(env_id, name):
    """Fetch one BaremetalNode row as a plain dict (fresh session)."""
    from app.db import SessionLocal
    from app.models import BaremetalNode

    db = SessionLocal()
    try:
        node = db.scalar(
            select(BaremetalNode).where(
                BaremetalNode.environment_id == env_id,
                BaremetalNode.name == name,
            )
        )
        if node is None:
            return None
        return {
            "id": node.id,
            "name": node.name,
            "bmc_host": node.bmc_host,
            "bmc_username": node.bmc_username,
            "bmc_password": node.bmc_password,
            "pxe_mac": node.pxe_mac,
            "expected_ip": node.expected_ip,
            "state": node.state,
        }
    finally:
        db.close()


def _make_node(env_id, name=None, **fields):
    """Insert a BaremetalNode row directly; returns its id."""
    from app.db import SessionLocal
    from app.models import BaremetalNode
    from app.services.crypto import encrypt_secret

    db = SessionLocal()
    try:
        node = BaremetalNode(
            environment_id=env_id,
            name=name or f"bm-{_suffix()}",
            bmc_host="bmc1.example.com",
            bmc_username="root",
            bmc_password=encrypt_secret("calvin"),
            **fields,
        )
        db.add(node)
        db.commit()
        db.refresh(node)
        return node.id, node.name
    finally:
        db.close()


REGISTER_PARAMS = {
    "name": None,  # filled per test
    "bmc_host": "bmc1.example.com",
    "bmc_username": "root",
    "bmc_password": "calvin",
}


def _register_params(name, **extra):
    return {**REGISTER_PARAMS, "name": name, **extra}


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------


def test_baremetal_ops_in_catalog(client, admin_headers):
    resp = client.get("/api/v1/operations", headers=admin_headers)
    assert resp.status_code == 200
    ops = {op["id"]: op for op in resp.json()}
    expected = {
        "baremetal.node.register": ("operator", True, None),
        "baremetal.nodes.list": ("viewer", False, None),
        "baremetal.node.power": ("operator", True, 120),
        "baremetal.node.pxe_boot": ("operator", True, 300),
        "baremetal.node.next_boot": ("operator", True, 300),
        "baremetal.node.provision": ("admin", True, 1800),
    }
    for op_id, (role, mutating, timeout) in expected.items():
        op = ops[op_id]
        assert op["required_role"] == role, op_id
        assert op["mutating"] is mutating, op_id
        assert op["timeout_seconds"] == timeout, op_id
        assert op["backend"] == "baremetal", op_id


# ---------------------------------------------------------------------------
# baremetal.node.register
# ---------------------------------------------------------------------------


def test_register_dry_run_logs_without_persisting(client, admin_headers, monkeypatch):
    """Global test config is dry_run=True: no DB row, no redfish probe."""
    from app.services import redfish

    calls = []
    monkeypatch.setattr(redfish, "system_macs", lambda *a: calls.append(a) or [])

    env = _create_env(client, admin_headers)
    name = f"bm-{_suffix()}"
    job = _submit(
        client,
        admin_headers,
        env["id"],
        "baremetal.node.register",
        _register_params(name),
    )
    assert job["status"] == "success", job
    assert "[dry-run]" in job["log_text"]
    assert calls == [], "dry-run must not probe redfish"
    assert _node_row(env["id"], name) is None


def test_register_encrypts_password_and_autofills_mac(
    client, admin_headers, monkeypatch
):
    from app.services import redfish
    from app.services.crypto import decrypt_secret

    monkeypatch.setattr(
        redfish, "system_macs", lambda *a: ["aa:bb:cc:dd:ee:01", "aa:bb:cc:dd:ee:02"]
    )

    env = _create_env(client, admin_headers, dry_run=False)
    name = f"bm-{_suffix()}"
    job = _submit(
        client,
        admin_headers,
        env["id"],
        "baremetal.node.register",
        _register_params(name),
    )
    assert job["status"] == "success", job

    row = _node_row(env["id"], name)
    assert row is not None
    assert row["state"] == "registered"
    assert row["bmc_password"].startswith(
        "fernet:"
    ), "password must be encrypted at rest"
    assert decrypt_secret(row["bmc_password"]) == "calvin"
    # First probed MAC wins
    assert row["pxe_mac"] == "aa:bb:cc:dd:ee:01"
    assert "autofilled pxe_mac" in job["log_text"]


def test_register_explicit_mac_skips_probe(client, admin_headers, monkeypatch):
    from app.services import redfish

    calls = []
    monkeypatch.setattr(redfish, "system_macs", lambda *a: calls.append(a) or [])

    env = _create_env(client, admin_headers, dry_run=False)
    name = f"bm-{_suffix()}"
    job = _submit(
        client,
        admin_headers,
        env["id"],
        "baremetal.node.register",
        _register_params(name, pxe_mac="11:22:33:44:55:66"),
    )
    assert job["status"] == "success", job
    assert calls == [], "explicit pxe_mac must skip the redfish probe"
    assert _node_row(env["id"], name)["pxe_mac"] == "11:22:33:44:55:66"


def test_register_probe_failure_still_registers(client, admin_headers, monkeypatch):
    from app.services import redfish
    from app.services.redfish import RedfishError

    def _boom(*a):
        raise RedfishError(
            "redfish GET /redfish/v1/Systems/1/EthernetInterfaces: HTTP 503"
        )

    monkeypatch.setattr(redfish, "system_macs", _boom)

    env = _create_env(client, admin_headers, dry_run=False)
    name = f"bm-{_suffix()}"
    job = _submit(
        client,
        admin_headers,
        env["id"],
        "baremetal.node.register",
        _register_params(name),
    )
    assert job["status"] == "success", job
    assert "mac autofill probe failed" in job["log_text"]
    row = _node_row(env["id"], name)
    assert row is not None
    assert row["pxe_mac"] is None


def test_register_upserts_on_same_name(client, admin_headers, monkeypatch):
    from app.services import redfish

    monkeypatch.setattr(redfish, "system_macs", lambda *a: [])

    env = _create_env(client, admin_headers, dry_run=False)
    name = f"bm-{_suffix()}"
    first = _submit(
        client,
        admin_headers,
        env["id"],
        "baremetal.node.register",
        _register_params(name),
    )
    assert first["status"] == "success", first
    second = _submit(
        client,
        admin_headers,
        env["id"],
        "baremetal.node.register",
        _register_params(name, bmc_host="bmc2.example.com"),
    )
    assert second["status"] == "success", second
    row = _node_row(env["id"], name)
    assert row["bmc_host"] == "bmc2.example.com"


def test_register_missing_password_fails_validation(client, admin_headers):
    env = _create_env(client, admin_headers)
    params = _register_params(f"bm-{_suffix()}")
    del params["bmc_password"]
    job = _submit(client, admin_headers, env["id"], "baremetal.node.register", params)
    assert job["status"] == "failed", job
    assert "Missing required parameter: bmc_password" in job["error"]


def test_register_invalid_name_fails(client, admin_headers):
    env = _create_env(client, admin_headers, dry_run=False)
    job = _submit(
        client,
        admin_headers,
        env["id"],
        "baremetal.node.register",
        _register_params("bad name!"),
    )
    assert job["status"] == "failed", job
    assert "invalid name" in job["error"]


# ---------------------------------------------------------------------------
# baremetal.nodes.list
# ---------------------------------------------------------------------------


def test_list_op_returns_nodes(client, admin_headers):
    env = _create_env(client, admin_headers)
    _make_node(env["id"], name=f"bm-{_suffix()}", state="booting")
    job = _submit(client, admin_headers, env["id"], "baremetal.nodes.list")
    assert job["status"] == "success", job
    assert "1 bare-metal nodes" in job["log_text"]


# ---------------------------------------------------------------------------
# baremetal.node.power
# ---------------------------------------------------------------------------


def test_power_dry_run_logs_without_redfish(client, admin_headers, monkeypatch):
    from app.services import redfish

    calls = []
    monkeypatch.setattr(redfish, "power", lambda *a: calls.append(a))

    env = _create_env(client, admin_headers)
    node_id, name = _make_node(env["id"])
    job = _submit(
        client,
        admin_headers,
        env["id"],
        "baremetal.node.power",
        {"node_id": node_id, "action": "off"},
    )
    assert job["status"] == "success", job
    assert "[dry-run]" in job["log_text"]
    assert calls == []


def test_power_sends_reset_type(client, admin_headers, monkeypatch):
    from app.services import redfish

    calls = []
    monkeypatch.setattr(redfish, "power", lambda *a: calls.append(a) or "ForceOff")

    env = _create_env(client, admin_headers, dry_run=False)
    node_id, name = _make_node(env["id"])
    job = _submit(
        client,
        admin_headers,
        env["id"],
        "baremetal.node.power",
        {"node_id": node_id, "action": "off"},
    )
    assert job["status"] == "success", job
    host, user, password, action = calls[0]
    assert (host, user, action) == ("bmc1.example.com", "root", "off")
    assert password == "calvin", "BMC password must be decrypted for the redfish call"


def test_power_unknown_node_fails(client, admin_headers):
    env = _create_env(client, admin_headers)
    job = _submit(
        client,
        admin_headers,
        env["id"],
        "baremetal.node.power",
        {"node_id": str(uuid.uuid4()), "action": "on"},
    )
    assert job["status"] == "failed", job
    assert "unknown node id" in job["error"]


def test_power_invalid_action_fails(client, admin_headers):
    env = _create_env(client, admin_headers, dry_run=False)
    node_id, _name = _make_node(env["id"])
    job = _submit(
        client,
        admin_headers,
        env["id"],
        "baremetal.node.power",
        {"node_id": node_id, "action": "nuke"},
    )
    assert job["status"] == "failed", job
    assert "unknown power action" in job["error"]


# ---------------------------------------------------------------------------
# baremetal.node.pxe_boot
# ---------------------------------------------------------------------------


def test_pxe_boot_sets_override_and_restarts(client, admin_headers, monkeypatch):
    from app.services import redfish

    calls = []
    monkeypatch.setattr(redfish, "set_pxe_boot", lambda *a: calls.append(("pxe",) + a))
    monkeypatch.setattr(redfish, "boot_override", lambda *a: "Pxe")
    monkeypatch.setattr(
        redfish, "power", lambda *a: calls.append(("power",) + a) or "ForceRestart"
    )

    env = _create_env(client, admin_headers, dry_run=False)
    node_id, name = _make_node(env["id"])
    job = _submit(
        client,
        admin_headers,
        env["id"],
        "baremetal.node.pxe_boot",
        {"node_id": node_id},
    )
    assert job["status"] == "success", job
    assert [c[0] for c in calls] == ["pxe", "power"], "pxe override before restart"
    assert calls[1][4] == "restart"
    assert "readback Pxe" in (job.get("log_text") or "")
    assert _node_row(env["id"], name)["state"] == "booting"


def test_pxe_boot_dry_run_changes_nothing(client, admin_headers, monkeypatch):
    from app.services import redfish

    calls = []
    monkeypatch.setattr(redfish, "set_pxe_boot", lambda *a: calls.append(a))
    monkeypatch.setattr(redfish, "power", lambda *a: calls.append(a))

    env = _create_env(client, admin_headers)
    node_id, name = _make_node(env["id"])
    job = _submit(
        client,
        admin_headers,
        env["id"],
        "baremetal.node.pxe_boot",
        {"node_id": node_id},
    )
    assert job["status"] == "success", job
    assert "[dry-run]" in job["log_text"]
    assert calls == []
    assert _node_row(env["id"], name)["state"] == "registered"


# ---------------------------------------------------------------------------
# baremetal.node.provision
# ---------------------------------------------------------------------------


def _patch_provision_path(monkeypatch, ready=True):
    """Mock redfish, the commission wait, and PXE prep for provision tests.

    A ready path pretends the RAM disk already posted an accepted wipe so the
    test does not wait out the job timeout or download Alpine.
    """
    from datetime import datetime, timezone

    from app.services import baremetal
    from app.services import redfish

    monkeypatch.setattr(redfish, "set_pxe_boot", lambda *a: None)
    monkeypatch.setattr(redfish, "boot_override", lambda *a: "Pxe")
    monkeypatch.setattr(redfish, "power", lambda *a: "ForceRestart")
    monkeypatch.setattr(baremetal, "talos_api_ready", lambda ip, log=None: ready)
    monkeypatch.setattr(
        baremetal, "_prepare_pxe", lambda db, env, settings, log: {"ok": True}
    )
    if not ready:
        return

    def fake_wait(db, node, predicate, **kwargs):
        node.boot_stage = "commissioned"
        node.wiped_at = datetime.now(timezone.utc)
        node.next_boot = "talos"
        node.commission_report = {
            "wipe": True,
            "serial": "fixture",
            "product": "fixture",
            "disks": [{"name": "sda", "wiped": True}],
            "nics": [],
        }
        db.add(node)
        db.flush()
        return True

    monkeypatch.setattr(baremetal, "_wait_until", fake_wait)


def test_provision_dry_run_logs_the_plan(client, admin_headers):
    env = _create_env(client, admin_headers)
    node_id, name = _make_node(env["id"], expected_ip="10.9.0.11")
    job = _submit(
        client,
        admin_headers,
        env["id"],
        "baremetal.node.provision",
        {"node_id": node_id, "roles": ["k8s_control_plane"]},
    )
    assert job["status"] == "success", job
    assert "[dry-run]" in job["log_text"]
    assert "10.9.0.11" in job["log_text"]
    assert _node_row(env["id"], name)["state"] == "registered"


def test_provision_zero_touch_happy_path(client, admin_headers, monkeypatch):
    """pxe boot -> talos API ready -> state talos-ready -> doc servers upsert."""
    _patch_provision_path(monkeypatch, ready=True)

    env = _create_env(client, admin_headers, dry_run=False)
    node_id, name = _make_node(
        env["id"], expected_ip="10.9.0.12", pxe_mac="aa:bb:cc:dd:ee:21"
    )
    job = _submit(
        client,
        admin_headers,
        env["id"],
        "baremetal.node.provision",
        {"node_id": node_id, "roles": ["compute"]},
    )
    assert job["status"] == "success", job
    assert "talos-ready" in job["log_text"]

    row = _node_row(env["id"], name)
    assert row["state"] == "talos-ready"

    config = client.get(
        f"/api/v1/environments/{env['id']}/config", headers=admin_headers
    )
    assert config.status_code == 200, config.text
    doc = yaml.safe_load(config.json()["yaml"])
    entry = doc["servers"][name]
    assert entry["source"] == "baremetal"
    assert entry["system_id"] is None
    assert entry["ip"] == "10.9.0.12"
    assert entry["roles"] == ["compute"]


def test_provision_requires_expected_ip(client, admin_headers, monkeypatch):
    _patch_provision_path(monkeypatch, ready=True)

    env = _create_env(client, admin_headers, dry_run=False)
    node_id, _name = _make_node(env["id"])
    job = _submit(
        client,
        admin_headers,
        env["id"],
        "baremetal.node.provision",
        {"node_id": node_id},
    )
    assert job["status"] == "failed", job
    assert "no expected_ip" in job["error"]


def test_provision_timeout_marks_node_failed(client, admin_headers, monkeypatch):
    """Service-level: poll deadline expires -> state failed, never raises."""
    _patch_provision_path(monkeypatch, ready=False)

    from app.db import SessionLocal
    from app.models import BaremetalNode, Environment
    from app.services import baremetal

    env = _create_env(client, admin_headers, dry_run=False)
    node_id, name = _make_node(
        env["id"], expected_ip="10.9.0.13", pxe_mac="aa:bb:cc:dd:ee:22"
    )

    logs = []
    db = SessionLocal()
    try:
        result = baremetal.provision(
            db,
            db.get(Environment, env["id"]),
            db.get(BaremetalNode, node_id),
            roles=[],
            actor="tester",
            dry_run=False,
            log=logs.append,
            timeout_seconds=0,
            poll_interval=0,
        )
        db.commit()
    finally:
        db.close()
    assert result["ok"] is False
    assert "timed out" in result["error"]
    assert _node_row(env["id"], name)["state"] == "failed"


def test_provision_stop_after_commission_does_not_serve_talos(
    client, admin_headers, monkeypatch
):
    _patch_provision_path(monkeypatch, ready=True)
    from app.db import SessionLocal
    from app.models import BaremetalNode, Environment
    from app.services import baremetal

    def _boom(*_a, **_k):
        raise AssertionError("Talos was served")

    monkeypatch.setattr(baremetal, "_serve_talos", _boom)
    env = _create_env(client, admin_headers, dry_run=False)
    node_id, _name = _make_node(
        env["id"], expected_ip="10.9.0.31", pxe_mac="aa:bb:cc:dd:ee:31"
    )
    db = SessionLocal()
    try:
        result = baremetal.provision(
            db,
            db.get(Environment, env["id"]),
            db.get(BaremetalNode, node_id),
            roles=["compute"],
            actor="tester",
            dry_run=False,
            log=lambda *_: None,
            timeout_seconds=5,
            poll_interval=0,
            stop_after="commission",
        )
    finally:
        db.close()
    assert result["ok"] is True, result
    assert result["stopped_after"] == "commission"


def test_provision_stop_after_talos_does_not_update_inventory(
    client, admin_headers, monkeypatch
):
    _patch_provision_path(monkeypatch, ready=True)
    from app.db import SessionLocal
    from app.models import BaremetalNode, Environment
    from app.services import baremetal

    def _boom(*_a, **_k):
        raise AssertionError("inventory was updated")

    monkeypatch.setattr("app.services.envconfig.assign_server", _boom)
    env = _create_env(client, admin_headers, dry_run=False)
    node_id, _name = _make_node(
        env["id"], expected_ip="10.9.0.32", pxe_mac="aa:bb:cc:dd:ee:32"
    )
    db = SessionLocal()
    try:
        result = baremetal.provision(
            db,
            db.get(Environment, env["id"]),
            db.get(BaremetalNode, node_id),
            roles=["compute"],
            actor="tester",
            dry_run=False,
            log=lambda *_: None,
            timeout_seconds=5,
            poll_interval=0,
            stop_after="talos",
        )
        db.commit()
    finally:
        db.close()
    assert result["ok"] is True, result
    assert result["stopped_after"] == "talos"
    assert result["state"] == "talos-ready"


def test_next_boot_talos_without_a_wipe_is_refused(client, admin_headers, monkeypatch):
    from app.services import redfish

    calls = []
    monkeypatch.setattr(redfish, "set_pxe_boot", lambda *a: calls.append(a))
    monkeypatch.setattr(redfish, "power", lambda *a: calls.append(a))
    env = _create_env(client, admin_headers, dry_run=False)
    node_id, name = _make_node(
        env["id"], expected_ip="10.9.0.33", pxe_mac="aa:bb:cc:dd:ee:33"
    )
    job = _submit(
        client,
        admin_headers,
        env["id"],
        "baremetal.node.next_boot",
        {"node_id": node_id, "next_boot": "talos", "boot_now": True},
    )
    assert job["status"] == "failed", job
    assert "no accepted commission wipe" in (job.get("error") or "")
    assert calls == []
    assert _node_row(env["id"], name)["state"] == "registered"


def test_next_boot_dry_run_does_not_power_the_machine(client, admin_headers, monkeypatch):
    from app.services import redfish

    calls = []
    monkeypatch.setattr(redfish, "set_pxe_boot", lambda *a: calls.append(a))
    monkeypatch.setattr(redfish, "power", lambda *a: calls.append(a))
    env = _create_env(client, admin_headers)
    node_id, name = _make_node(env["id"], pxe_mac="aa:bb:cc:dd:ee:34")
    job = _submit(
        client,
        admin_headers,
        env["id"],
        "baremetal.node.next_boot",
        {"node_id": node_id, "next_boot": "commission", "boot_now": True},
    )
    assert job["status"] == "success", job
    assert "[dry-run]" in (job.get("log_text") or "")
    assert calls == []
    assert _node_row(env["id"], name)["state"] == "registered"


def test_provision_unknown_role_fails(client, admin_headers):
    env = _create_env(client, admin_headers, dry_run=False)
    node_id, _name = _make_node(env["id"], expected_ip="10.9.0.14")
    job = _submit(
        client,
        admin_headers,
        env["id"],
        "baremetal.node.provision",
        {"node_id": node_id, "roles": ["compute", "bogus"]},
    )
    assert job["status"] == "failed", job
    assert "unknown role" in job["error"]


def test_prepare_pxe_guard_logs_when_module_missing(monkeypatch):
    """The pxe sidecar is built separately; its absence is a logged no-op."""
    import builtins

    from app.services import baremetal

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        fromlist = kwargs.get("fromlist") or (args[2] if len(args) > 2 else ())
        if name == "app.services" and "pxe" in fromlist:
            raise ImportError("No module named 'app.services.pxe'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    logs = []
    baremetal._prepare_pxe(None, None, None, logs.append)
    # Product message: "[pxe] pxe module not present"
    assert any("pxe module not present" in m for m in logs)


# ---------------------------------------------------------------------------
# REST endpoint
# ---------------------------------------------------------------------------


def test_rest_list_returns_nodes_without_password(client, admin_headers):
    env = _create_env(client, admin_headers)
    _node_id, name = _make_node(env["id"], state="talos-ready", expected_ip="10.9.0.20")
    resp = client.get(
        f"/api/v1/environments/{env['id']}/baremetal", headers=admin_headers
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["count"] == 1
    node = body["nodes"][0]
    assert node["name"] == name
    assert node["state"] == "talos-ready"
    assert node["expected_ip"] == "10.9.0.20"
    assert node["next_boot"] == "disk"
    assert node["boot_stage"] == "new"
    assert "bmc_password" not in node
    assert "commission_token" not in node


def test_rest_list_unknown_env_404(client, admin_headers):
    resp = client.get(
        f"/api/v1/environments/{uuid.uuid4()}/baremetal", headers=admin_headers
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Tenancy / roles
# ---------------------------------------------------------------------------


def test_baremetal_ops_tenant_scoping(client, admin_headers):
    """Cross-tenant operator gets 403; a viewer cannot run mutating ops."""
    tenant_a = _create_tenant(client, admin_headers)
    tenant_b = _create_tenant(client, admin_headers)
    env_a = _create_env(client, admin_headers, tenant_id=tenant_a["id"])
    env_b = _create_env(client, admin_headers, tenant_id=tenant_b["id"])
    node_id, _name = _make_node(env_a["id"])

    operator = _create_user(
        client, admin_headers, [{"tenant_id": tenant_a["id"], "role": "operator"}]
    )
    viewer = _create_user(
        client, admin_headers, [{"tenant_id": tenant_a["id"], "role": "viewer"}]
    )
    operator_headers = _login_headers(client, operator)
    viewer_headers = _login_headers(client, viewer)

    for op, params in (
        ("baremetal.node.register", _register_params(f"bm-{_suffix()}")),
        ("baremetal.node.power", {"node_id": node_id, "action": "on"}),
        ("baremetal.node.pxe_boot", {"node_id": node_id}),
        ("baremetal.node.next_boot", {"node_id": node_id, "next_boot": "disk"}),
        ("baremetal.node.provision", {"node_id": node_id}),
    ):
        resp = client.post(
            f"/api/v1/environments/{env_b['id']}/jobs",
            headers=operator_headers,
            json={"operation": op, "params": params, "run_sync": True},
        )
        assert resp.status_code == 403, (op, resp.text)
        resp = client.post(
            f"/api/v1/environments/{env_a['id']}/jobs",
            headers=viewer_headers,
            json={"operation": op, "params": params, "run_sync": True},
        )
        assert resp.status_code == 403, (op, resp.text)

    # The REST list endpoint is viewer-readable in-tenant, 403 cross-tenant
    resp = client.get(
        f"/api/v1/environments/{env_a['id']}/baremetal", headers=viewer_headers
    )
    assert resp.status_code == 200, resp.text
    resp = client.get(
        f"/api/v1/environments/{env_b['id']}/baremetal", headers=viewer_headers
    )
    assert resp.status_code == 403, resp.text
