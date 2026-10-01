"""Fleet status board endpoint tests (GET /api/v1/fleet)."""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timedelta, timezone

STEP_ORDER = ["connect", "inventory", "config", "push", "deploy", "operate"]

FULL_ROLES_DOC = """\
provider: kubespray
servers:
  cp-1:
    ip: 10.0.0.11
    roles: [k8s_control_plane, etcd, control]
  worker-1:
    ip: 10.0.0.12
    roles: [compute]
  net-1:
    ip: 10.0.0.13
    roles: [network]
  store-1:
    ip: 10.0.0.14
    roles: [storage]
"""


def _suffix() -> str:
    return uuid.uuid4().hex[:8]


def _create_tenant(client, admin_headers, name=None):
    resp = client.post(
        "/api/v1/tenants",
        headers=admin_headers,
        json={"name": name or f"tenant-{_suffix()}"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _create_user(client, admin_headers, memberships=None, username=None):
    resp = client.post(
        "/api/v1/users",
        headers=admin_headers,
        json={
            "username": username or f"user-{_suffix()}",
            "password": "pw",
            "memberships": memberships or [],
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _login_headers(client, username):
    resp = client.post(
        "/api/v1/auth/login", json={"username": username, "password": "pw"}
    )
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


def _create_env(client, headers, tenant_id=None, **fields):
    body = {"name": f"env-fleet-{_suffix()}", **fields}
    if tenant_id:
        body["tenant_id"] = tenant_id
    resp = client.post("/api/v1/environments", headers=headers, json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _put_doc(client, headers, env_id, doc=FULL_ROLES_DOC):
    resp = client.put(
        f"/api/v1/environments/{env_id}/config",
        headers=headers,
        json={"yaml_text": doc},
    )
    assert resp.status_code == 201, resp.text


def _seed_job(*, operation, status, environment_id, created_at=None, finished_at=None):
    from app.db import SessionLocal
    from app.models import Job, JobStatus

    db = SessionLocal()
    try:
        job = Job(
            environment_id=environment_id,
            operation=operation,
            params={},
            status=JobStatus(status),
            log_text="",
            created_by="fleet-test",
            created_at=created_at or datetime.now(timezone.utc),
            finished_at=finished_at,
        )
        db.add(job)
        db.commit()
        return job.id
    finally:
        db.close()


def _fleet(client, headers) -> dict:
    resp = client.get("/api/v1/fleet", headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# Visibility
# ---------------------------------------------------------------------------


def test_fleet_platform_admin_sees_all_envs_with_step_states(client, admin_headers):
    tenant = _create_tenant(client, admin_headers)
    env = _create_env(
        client, admin_headers, tenant_id=tenant["id"], region="lab", tier="dev"
    )

    body = _fleet(client, admin_headers)
    rows = {e["id"]: e for e in body["environments"]}
    assert env["id"] in rows

    row = rows[env["id"]]
    assert row["name"] == env["name"]
    assert row["region"] == "lab"
    assert row["tier"] == "dev"
    assert row["tenant_id"] == tenant["id"]
    assert row["tenant_name"] == tenant["name"]
    assert row["dry_run"] is True  # global test config is dry_run=True
    assert list(row["steps"].keys()) == STEP_ORDER
    assert set(row["steps"].values()) <= {"done", "attention", "pending"}
    assert row["steps"]["connect"] == "done"  # local hub when no config dir
    assert row["steps"]["operate"] == "pending"  # never probed from /fleet
    assert row["deploy"] is None  # never deployed


def test_fleet_session_user_sees_only_own_tenant_envs(client, admin_headers):
    tenant_a = _create_tenant(client, admin_headers)
    tenant_b = _create_tenant(client, admin_headers)
    env_a = _create_env(client, admin_headers, tenant_id=tenant_a["id"])
    env_b = _create_env(client, admin_headers, tenant_id=tenant_b["id"])

    user = _create_user(
        client,
        admin_headers,
        memberships=[{"tenant_id": tenant_a["id"], "role": "viewer"}],
    )
    headers = _login_headers(client, user["username"])

    body = _fleet(client, headers)
    env_ids = {e["id"] for e in body["environments"]}
    assert env_a["id"] in env_ids
    assert env_b["id"] not in env_ids

    # tenants = the caller's memberships, with their role in each
    assert body["tenants"] == [
        {"id": tenant_a["id"], "name": tenant_a["name"], "role": "viewer"}
    ]


def test_fleet_platform_admin_tenants_have_null_role(client, admin_headers):
    tenant = _create_tenant(client, admin_headers)

    body = _fleet(client, admin_headers)
    by_id = {t["id"]: t for t in body["tenants"]}
    assert tenant["id"] in by_id
    assert by_id[tenant["id"]] == {
        "id": tenant["id"],
        "name": tenant["name"],
        "role": None,
    }
    assert all(t["role"] is None for t in body["tenants"])


def test_fleet_requires_auth(client):
    resp = client.get("/api/v1/fleet")
    assert resp.status_code in (401, 403)


# ---------------------------------------------------------------------------
# current_step logic
# ---------------------------------------------------------------------------


def test_fleet_current_step_first_not_done(client, admin_headers):
    env = _create_env(
        client, admin_headers
    )  # connect: done (local hub when no config dir)

    row = {e["id"]: e for e in _fleet(client, admin_headers)["environments"]}[env["id"]]
    assert row["steps"]["connect"] == "done"
    assert row["current_step"] == "inventory"  # first step not done


def test_fleet_current_step_operate_when_everything_else_done(
    client, admin_headers, tmp_path
):
    config_dir = tmp_path / "etc-genestack"
    config_dir.mkdir()
    env = _create_env(client, admin_headers, genestack_config_dir=str(config_dir))
    _put_doc(client, admin_headers, env["id"])
    base = datetime.now(timezone.utc) - timedelta(minutes=5)
    deploy_id = _seed_job(
        operation="genestack.deploy",
        status="success",
        environment_id=env["id"],
        created_at=base,
        finished_at=base + timedelta(seconds=30),
    )

    row = {e["id"]: e for e in _fleet(client, admin_headers)["environments"]}[env["id"]]
    assert row["steps"] == {
        "connect": "done",
        "inventory": "done",
        "config": "done",
        "push": "done",  # a deploy also counts as a push
        "deploy": "done",
        "operate": "pending",  # not probed from /fleet
    }
    assert row["current_step"] == "operate"
    assert row["deploy"] == {
        "job_id": deploy_id,
        "status": "success",
        "stages_completed": None,
        "stages_total": None,
        "dry_run": None,
    }


def test_fleet_current_step_null_when_all_done(client, admin_headers, monkeypatch):
    from app.services import fleet as fleet_service

    env = _create_env(client, admin_headers)

    def all_done_workflow(db, env, settings, include_operate_probe=True):
        return {
            "environment": {},
            "steps": [
                {"id": step_id, "state": "done", "summary": "", "details": {}}
                for step_id in STEP_ORDER
            ],
        }

    monkeypatch.setattr(fleet_service, "build_workflow", all_done_workflow)
    row = {e["id"]: e for e in _fleet(client, admin_headers)["environments"]}[env["id"]]
    assert row["steps"] == {step_id: "done" for step_id in STEP_ORDER}
    assert row["current_step"] is None
    assert row["deploy"] is None


# ---------------------------------------------------------------------------
# Operate step is never probed from /fleet
# ---------------------------------------------------------------------------


def test_fleet_does_not_probe_clusters(client, admin_headers):
    """Fleet endpoint uses include_operate_probe=False, resulting in operate='pending'."""
    env = _create_env(client, admin_headers)

    row = {e["id"]: e for e in _fleet(client, admin_headers)["environments"]}[env["id"]]
    # When include_operate_probe=False, operate step is "pending" (not probed)
    assert row["steps"]["operate"] == "pending"


def test_build_workflow_without_operate_probe(client, admin_headers):
    from app.db import SessionLocal
    from app.services.workflow import build_workflow

    env = _create_env(client, admin_headers)
    db = SessionLocal()
    try:
        from app.models import Environment

        row = db.get(Environment, env["id"])
        body = build_workflow(db, row, include_operate_probe=False)
    finally:
        db.close()

    operate = next(s for s in body["steps"] if s["id"] == "operate")
    assert operate["state"] == "pending"
    assert operate["summary"] == "not checked"
    assert operate["details"] == {}


# ---------------------------------------------------------------------------
# One broken environment never breaks the board
# ---------------------------------------------------------------------------


def test_fleet_broken_env_does_not_break_others(client, admin_headers, monkeypatch):
    from app.services import fleet as fleet_service

    good = _create_env(client, admin_headers)
    bad = _create_env(client, admin_headers)

    real_build = fleet_service.build_workflow

    def flaky_build(db, env, settings, include_operate_probe=True):
        if env.id == bad["id"]:
            raise RuntimeError("boom")
        return real_build(db, env, settings, include_operate_probe)

    monkeypatch.setattr(fleet_service, "build_workflow", flaky_build)

    body = _fleet(client, admin_headers)
    rows = {e["id"]: e for e in body["environments"]}

    broken = rows[bad["id"]]
    assert broken["error"] == "boom"
    assert broken["steps"] == {step_id: "pending" for step_id in STEP_ORDER}
    assert broken["current_step"] == "connect"
    assert broken["deploy"] is None

    healthy = rows[good["id"]]
    assert "error" not in healthy
    assert healthy["steps"]["connect"] == "done"  # local hub when no config dir


# ---------------------------------------------------------------------------
# Agent connectivity field
# ---------------------------------------------------------------------------


def _fleet_agent(client, headers, env_id) -> dict:
    row = {e["id"]: e for e in _fleet(client, headers)["environments"]}[env_id]
    return row["agent"]


def test_fleet_agent_field_not_enrolled(client, admin_headers):
    env = _create_env(client, admin_headers)
    assert _fleet_agent(client, admin_headers, env["id"]) == {
        "enrolled": False,
        "connected": False,
    }


def test_fleet_agent_field_enrolled_not_connected(client, admin_headers):
    env = _create_env(client, admin_headers)
    resp = client.post(
        f"/api/v1/environments/{env['id']}/agent/token",
        headers=admin_headers,
        json={"name": "default"},
    )
    assert resp.status_code == 201, resp.text
    assert _fleet_agent(client, admin_headers, env["id"]) == {
        "enrolled": True,
        "connected": False,
    }


def test_fleet_agent_field_connected(client, admin_headers):
    from app.services import agents

    env = _create_env(client, admin_headers)
    resp = client.post(
        f"/api/v1/environments/{env['id']}/agent/token",
        headers=admin_headers,
        json={"name": "default"},
    )
    assert resp.status_code == 201, resp.text
    token = resp.json()["token"]

    with client.websocket_connect(f"/api/v1/agents/connect?token={token}") as ws:
        challenge = ws.receive_json()
        assert challenge["type"] == "challenge"
        ws.send_json(
            {"type": "proof", "hmac": agents.proof_for(token, challenge["nonce"])}
        )
        welcome = ws.receive_json()
        assert welcome["type"] == "welcome"
        ws.send_json(
            {
                "type": "hello",
                "agent_id": welcome["agent_id"],
                "version": "1.0.0",
                "hostname": "fl-node",
                "caps": [],
            }
        )

        deadline = time.time() + 5
        agent = {"connected": False}
        while time.time() < deadline:
            agent = _fleet_agent(client, admin_headers, env["id"])
            if agent["connected"]:
                break
            time.sleep(0.05)
        assert agent == {"enrolled": True, "connected": True}
