"""Lifecycle workflow endpoint tests (GET /api/v1/environments/{id}/workflow)."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

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

PARTIAL_ROLES_DOC = """\
provider: kubespray
servers:
  cp-1:
    ip: 10.0.0.11
    roles: [k8s_control_plane]
"""

OVH_NO_VRACK_DOC = """\
provider: talos
servers:
  cp-1:
    source: ovh
    ip: 10.10.0.11
    private_ip: 10.10.0.11
    roles: [k8s_control_plane, etcd, control]
  worker-1:
    source: ovh
    ip: 10.10.0.12
    private_ip: 10.10.0.12
    roles: [compute]
  net-1:
    source: ovh
    ip: 10.10.0.13
    private_ip: 10.10.0.13
    roles: [network]
  store-1:
    source: ovh
    ip: 10.10.0.14
    private_ip: 10.10.0.14
    roles: [storage]
"""

OVH_READY_DOC = """\
provider: talos
ovh:
  vrack: pn-example
  vlan_id: 100
  private_cidr: 10.10.0.0/24
servers:
  cp-1:
    source: ovh
    ip: 10.10.0.11
    private_ip: 10.10.0.11
    roles: [k8s_control_plane, etcd, control]
  worker-1:
    source: ovh
    ip: 10.10.0.12
    private_ip: 10.10.0.12
    roles: [compute]
  net-1:
    source: ovh
    ip: 10.10.0.13
    private_ip: 10.10.0.13
    roles: [network]
  store-1:
    source: ovh
    ip: 10.10.0.14
    private_ip: 10.10.0.14
    roles: [storage]
"""


def _suffix() -> str:
    return uuid.uuid4().hex[:8]


def _create_env(client, headers, **fields) -> dict:
    resp = client.post(
        "/api/v1/environments",
        headers=headers,
        json={"name": f"env-wf-{_suffix()}", **fields},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _put_doc(client, headers, env_id, doc):
    resp = client.put(
        f"/api/v1/environments/{env_id}/config",
        headers=headers,
        json={"yaml_text": doc},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["version"]


def _seed_job(
    *, operation, status, environment_id, created_at=None, finished_at=None, params=None
):
    from app.db import SessionLocal
    from app.models import Job, JobStatus

    db = SessionLocal()
    try:
        job = Job(
            environment_id=environment_id,
            operation=operation,
            params=params or {},
            status=JobStatus(status),
            log_text="",
            created_by="workflow-test",
            created_at=created_at or datetime.now(timezone.utc),
            finished_at=finished_at,
        )
        db.add(job)
        db.commit()
        return job.id
    finally:
        db.close()


def _workflow(client, headers, env_id) -> dict:
    resp = client.get(f"/api/v1/environments/{env_id}/workflow", headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _steps(body: dict) -> dict[str, dict]:
    return {step["id"]: step for step in body["steps"]}


# ---------------------------------------------------------------------------
# Empty environment
# ---------------------------------------------------------------------------


def test_workflow_empty_env(client, admin_headers, viewer_headers, monkeypatch):
    # No cluster in the test env: pin the probe to unreachable so the
    # operate step doesn't depend on whatever the default kubeconfig points at.
    from app.services import cluster as cluster_probe

    monkeypatch.setattr(
        cluster_probe,
        "cluster_status",
        lambda _kc: {"reachable": False, "nodes": [], "error": "no cluster in tests"},
    )
    monkeypatch.setattr(
        cluster_probe,
        "services_status",
        lambda _kc: {
            "reachable": False,
            "releases": [],
            "error": "no cluster in tests",
        },
    )

    env = _create_env(client, admin_headers, region="lab", tier="dev")

    body = _workflow(client, viewer_headers, env["id"])

    assert body["environment"] == {
        "id": env["id"],
        "name": env["name"],
        "region": "lab",
        "tier": "dev",
        "dry_run": True,  # global test config is dry_run=True
        "tenant_id": env["tenant_id"],
    }
    assert [s["id"] for s in body["steps"]] == [
        "connect",
        "inventory",
        "config",
        "push",
        "deploy",
        "operate",
    ]

    steps = _steps(body)
    assert steps["connect"]["state"] == "done"
    assert steps["connect"]["summary"] == "deploy target configured"
    config_dir = steps["connect"]["details"]["genestack_config_dir"]
    assert isinstance(config_dir, str)
    assert config_dir.endswith(f"/environments/{env['name']}/etc-genestack")
    assert steps["connect"]["details"]["kubeconfig_source"] == "default"
    assert steps["connect"]["details"]["dry_run"] is True

    assert steps["inventory"]["state"] == "pending"
    assert steps["inventory"]["details"]["host_count"] == 0
    assert steps["inventory"]["details"]["source"] == "none"
    assert steps["inventory"]["details"]["roles"] == {
        "k8s_control_plane": 0,
        "etcd": 0,
        "control": 0,
        "compute": 0,
        "network": 0,
        "storage": 0,
    }

    assert steps["config"]["state"] == "pending"
    assert steps["config"]["summary"] == "no config document yet"
    assert steps["config"]["details"]["version"] is None

    assert steps["push"]["state"] == "pending"
    assert steps["push"]["summary"] == "never pushed"
    assert steps["push"]["details"]["job_id"] is None

    assert steps["deploy"]["state"] == "pending"
    assert steps["deploy"]["summary"] == "not deployed yet"

    assert steps["operate"]["state"] == "pending"
    assert steps["operate"]["summary"] == "cluster unreachable"
    assert steps["operate"]["details"]["reachable"] is False
    assert steps["operate"]["details"]["verify"] is None
    assert "skyline_url" not in steps["operate"]["details"]


# ---------------------------------------------------------------------------
# Fully configured environment
# ---------------------------------------------------------------------------


def test_workflow_full_env_connect_inventory_config_done(
    client, admin_headers, viewer_headers, tmp_path
):
    config_dir = tmp_path / "etc-genestack"
    config_dir.mkdir()
    env = _create_env(
        client,
        admin_headers,
        genestack_config_dir=str(config_dir),
        deployer_ssh_host="deployer.example.com",
        deployer_ssh_user="ubuntu",
        kubeconfig_path="/etc/genestack/kubeconfig",
    )
    version = _put_doc(client, admin_headers, env["id"], FULL_ROLES_DOC)

    body = _workflow(client, viewer_headers, env["id"])
    steps = _steps(body)

    assert steps["connect"]["state"] == "done"
    assert steps["connect"]["summary"] == "deploy target configured"
    assert steps["connect"]["details"]["genestack_config_dir"] == str(config_dir)
    assert steps["connect"]["details"]["deployer_ssh_host"] == "deployer.example.com"
    assert steps["connect"]["details"]["kubeconfig_source"] == "path"

    inventory = steps["inventory"]
    assert inventory["state"] == "done"
    assert inventory["details"]["host_count"] == 4
    assert inventory["details"]["source"] == "doc"
    assert inventory["details"]["roles"] == {
        "k8s_control_plane": 1,
        "etcd": 1,
        "control": 1,
        "compute": 1,
        "network": 1,
        "storage": 1,
    }

    config = steps["config"]
    assert config["state"] == "done"
    assert config["details"]["version"] == version
    assert config["details"]["updated_at"]
    assert config["details"]["updated_by"]

    # No jobs yet; no cluster in the sandbox
    assert steps["push"]["state"] == "pending"
    assert steps["deploy"]["state"] == "pending"
    assert steps["operate"]["state"] == "pending"


def test_workflow_inventory_missing_required_roles(
    client, admin_headers, viewer_headers, tmp_path
):
    config_dir = tmp_path / "etc-genestack"
    config_dir.mkdir()
    env = _create_env(client, admin_headers, genestack_config_dir=str(config_dir))
    _put_doc(client, admin_headers, env["id"], PARTIAL_ROLES_DOC)

    steps = _steps(_workflow(client, viewer_headers, env["id"]))

    inventory = steps["inventory"]
    assert inventory["state"] == "attention"
    assert inventory["summary"] == "missing required roles: etcd, control"
    assert inventory["details"]["host_count"] == 1
    assert inventory["details"]["roles"]["k8s_control_plane"] == 1
    assert inventory["details"]["roles"]["etcd"] == 0


def test_workflow_inventory_ovh_missing_vrack(
    client, admin_headers, viewer_headers, tmp_path
):
    """OVH-sourced hosts are not inventory-done on roles alone — vRack is required."""
    config_dir = tmp_path / "etc-genestack"
    config_dir.mkdir()
    env = _create_env(client, admin_headers, genestack_config_dir=str(config_dir))
    _put_doc(client, admin_headers, env["id"], OVH_NO_VRACK_DOC)

    inventory = _steps(_workflow(client, viewer_headers, env["id"]))["inventory"]
    assert inventory["state"] == "attention"
    assert inventory["summary"] == "vRack not set — pick it on Platform → OVH"
    assert inventory["details"]["ovh_bound"] is True
    assert inventory["details"]["host_count"] == 4
    assert inventory["details"]["vrack"] is None
    assert inventory["details"]["private_ips_assigned"] == 4
    assert inventory["details"]["hosts_missing_private_ip"] == []


def test_workflow_inventory_ovh_ready(client, admin_headers, viewer_headers, tmp_path):
    """OVH hosts with vRack, VLAN, private IPs, and required roles → inventory done."""
    config_dir = tmp_path / "etc-genestack"
    config_dir.mkdir()
    env = _create_env(client, admin_headers, genestack_config_dir=str(config_dir))
    _put_doc(client, admin_headers, env["id"], OVH_READY_DOC)

    inventory = _steps(_workflow(client, viewer_headers, env["id"]))["inventory"]
    assert inventory["state"] == "done"
    assert (
        inventory["summary"]
        == "4 OVH host(s), VLAN 100, vRack pn-example, roles covered"
    )
    assert inventory["details"]["ovh_bound"] is True
    assert inventory["details"]["vrack"] == "pn-example"
    assert inventory["details"]["vlan_id"] == 100
    assert inventory["details"]["private_ips_assigned"] == 4
    assert inventory["details"]["hosts_missing_private_ip"] == []
    assert inventory["details"]["roles"] == {
        "k8s_control_plane": 1,
        "etcd": 1,
        "control": 1,
        "compute": 1,
        "network": 1,
        "storage": 1,
    }


# ---------------------------------------------------------------------------
# Push / deploy states from seeded jobs
# ---------------------------------------------------------------------------


def test_workflow_push_and_deploy_job_states(client, admin_headers, viewer_headers):
    env = _create_env(client, admin_headers)
    env_id = env["id"]
    base = datetime.now(timezone.utc) - timedelta(minutes=10)

    # Successful push -> push done
    push_id = _seed_job(
        operation="genestack.config.push",
        status="success",
        environment_id=env_id,
        created_at=base,
        finished_at=base + timedelta(seconds=30),
    )
    steps = _steps(_workflow(client, viewer_headers, env_id))
    assert steps["push"]["state"] == "done"
    assert steps["push"]["summary"] == "config pushed"
    assert steps["push"]["details"]["job_id"] == push_id
    assert steps["push"]["details"]["status"] == "success"
    assert steps["push"]["details"]["finished_at"]
    assert steps["deploy"]["state"] == "pending"

    # Later failed push -> attention
    _seed_job(
        operation="genestack.config.push",
        status="failed",
        environment_id=env_id,
        created_at=base + timedelta(minutes=1),
        finished_at=base + timedelta(minutes=1, seconds=30),
    )
    steps = _steps(_workflow(client, viewer_headers, env_id))
    assert steps["push"]["state"] == "attention"
    assert steps["push"]["summary"] == "last push failed"

    # Successful deploy -> deploy done (a deploy also pushes, so push is done too)
    deploy_id = _seed_job(
        operation="genestack.deploy",
        status="success",
        environment_id=env_id,
        created_at=base + timedelta(minutes=2),
        finished_at=base + timedelta(minutes=2, seconds=30),
    )
    steps = _steps(_workflow(client, viewer_headers, env_id))
    assert steps["deploy"]["state"] == "done"
    assert steps["deploy"]["summary"] == "deployed"
    assert steps["deploy"]["details"]["job_id"] == deploy_id
    assert steps["push"]["state"] == "done"

    # Failed deploy -> deploy attention
    _seed_job(
        operation="genestack.deploy",
        status="failed",
        environment_id=env_id,
        created_at=base + timedelta(minutes=3),
        finished_at=base + timedelta(minutes=3, seconds=30),
    )
    steps = _steps(_workflow(client, viewer_headers, env_id))
    assert steps["deploy"]["state"] == "attention"
    assert steps["deploy"]["summary"] == "last deploy failed"

    # Running deploy -> pending "in progress"; queued push -> pending "queued"
    _seed_job(
        operation="genestack.deploy",
        status="running",
        environment_id=env_id,
        created_at=base + timedelta(minutes=4),
    )
    steps = _steps(_workflow(client, viewer_headers, env_id))
    assert steps["deploy"]["state"] == "pending"
    assert steps["deploy"]["summary"] == "deploy in progress"
    assert steps["push"]["state"] == "pending"
    assert steps["push"]["summary"] == "push in progress"

    _seed_job(
        operation="genestack.config.push",
        status="queued",
        environment_id=env_id,
        created_at=base + timedelta(minutes=5),
    )
    steps = _steps(_workflow(client, viewer_headers, env_id))
    assert steps["push"]["state"] == "pending"
    assert steps["push"]["summary"] == "push queued"

    # Jobs of another environment never leak into this view
    other = _create_env(client, admin_headers)
    other_steps = _steps(_workflow(client, viewer_headers, other["id"]))
    assert other_steps["push"]["summary"] == "never pushed"
    assert other_steps["deploy"]["summary"] == "not deployed yet"


# ---------------------------------------------------------------------------
# Operate states (probes monkeypatched — no real cluster in the sandbox)
# ---------------------------------------------------------------------------


def test_workflow_operate_states(client, admin_headers, viewer_headers, monkeypatch):
    from app.services import cluster as cluster_probe

    env = _create_env(client, admin_headers)

    monkeypatch.setattr(
        cluster_probe,
        "cluster_status",
        lambda _kc: {"reachable": True, "nodes": [{"name": "cp-1"}], "error": None},
    )
    monkeypatch.setattr(
        cluster_probe,
        "services_status",
        lambda _kc: {
            "reachable": True,
            "releases": [{"name": "keystone"}],
            "error": None,
        },
    )
    steps = _steps(_workflow(client, viewer_headers, env["id"]))
    assert steps["operate"]["state"] == "done"
    assert steps["operate"]["details"]["node_count"] == 1
    assert steps["operate"]["details"]["release_count"] == 1
    assert steps["operate"]["details"]["error"] is None

    monkeypatch.setattr(
        cluster_probe,
        "services_status",
        lambda _kc: {"reachable": True, "releases": [], "error": None},
    )
    steps = _steps(_workflow(client, viewer_headers, env["id"]))
    assert steps["operate"]["state"] == "attention"
    assert steps["operate"]["summary"] == "cluster reachable but no helm releases"

    monkeypatch.setattr(
        cluster_probe,
        "cluster_status",
        lambda _kc: {"reachable": False, "nodes": [], "error": "connection refused"},
    )
    monkeypatch.setattr(
        cluster_probe,
        "services_status",
        lambda _kc: {"reachable": False, "releases": [], "error": "connection refused"},
    )
    steps = _steps(_workflow(client, viewer_headers, env["id"]))
    assert steps["operate"]["state"] == "pending"
    assert steps["operate"]["summary"] == "cluster unreachable"
    assert steps["operate"]["details"]["error"] == "connection refused"


def test_workflow_operate_verify_job_details(
    client, admin_headers, viewer_headers, monkeypatch
):
    """Latest genestack.verify job surfaces in the operate step details."""
    # Pin the probe to unreachable: the verify-details logic is independent of
    # whether a default kubeconfig happens to point at a live cluster.
    from app.services import cluster as cluster_probe

    monkeypatch.setattr(
        cluster_probe,
        "cluster_status",
        lambda _kc: {"reachable": False, "nodes": [], "error": "no cluster in tests"},
    )
    monkeypatch.setattr(
        cluster_probe,
        "services_status",
        lambda _kc: {
            "reachable": False,
            "releases": [],
            "error": "no cluster in tests",
        },
    )

    env = _create_env(client, admin_headers)
    env_id = env["id"]

    # Never run -> null
    steps = _steps(_workflow(client, viewer_headers, env_id))
    assert steps["operate"]["details"]["verify"] is None

    base = datetime.now(timezone.utc) - timedelta(minutes=5)
    verify_id = _seed_job(
        operation="genestack.verify",
        status="success",
        environment_id=env_id,
        created_at=base,
        finished_at=base + timedelta(seconds=90),
        params={"level": "quick"},
    )
    # An older verify job must not win over the latest one
    _seed_job(
        operation="genestack.verify",
        status="failed",
        environment_id=env_id,
        created_at=base - timedelta(minutes=5),
        finished_at=base - timedelta(minutes=5) + timedelta(seconds=90),
        params={"level": "standard"},
    )

    steps = _steps(_workflow(client, viewer_headers, env_id))
    verify = steps["operate"]["details"]["verify"]
    assert verify["job_id"] == verify_id
    assert verify["status"] == "success"
    assert verify["level"] == "quick"
    assert verify["finished_at"]
    # Operate state logic untouched: no cluster in the sandbox
    assert steps["operate"]["state"] == "pending"


def test_workflow_operate_skyline_url(client, admin_headers, viewer_headers, tmp_path):
    """skyline_url appears when the current config doc sets network.gateway_domain."""
    config_dir = tmp_path / "etc-genestack"
    config_dir.mkdir()
    env = _create_env(client, admin_headers, genestack_config_dir=str(config_dir))
    _put_doc(
        client,
        admin_headers,
        env["id"],
        "provider: kubespray\nnetwork:\n  gateway_domain: lab.example.com\n",
    )

    steps = _steps(_workflow(client, viewer_headers, env["id"]))
    assert (
        steps["operate"]["details"]["skyline_url"] == "https://skyline.lab.example.com"
    )
    assert (
        steps["operate"]["details"]["horizon_url"] == "https://horizon.lab.example.com"
    )

    # No config document -> key omitted entirely
    other = _create_env(client, admin_headers)
    steps = _steps(_workflow(client, viewer_headers, other["id"]))
    assert "skyline_url" not in steps["operate"]["details"]


# ---------------------------------------------------------------------------
# Connect step: host-prepare readiness from seeded jobs
# ---------------------------------------------------------------------------


def test_workflow_connect_prepared_from_prepare_jobs(
    client, admin_headers, viewer_headers, tmp_path
):
    config_dir = tmp_path / "etc-genestack"
    config_dir.mkdir()
    env = _create_env(client, admin_headers, genestack_config_dir=str(config_dir))
    env_id = env["id"]

    # No prepare job ever -> prepared null, no prepare_job_id, summary unchanged
    steps = _steps(_workflow(client, viewer_headers, env_id))
    connect = steps["connect"]
    assert connect["state"] == "done"
    assert connect["summary"] == "deploy target configured"
    assert connect["details"]["prepared"] is None
    assert "prepare_job_id" not in connect["details"]

    # Latest prepare failed -> prepared false, summary flagged, state stays done
    _seed_job(
        operation="genestack.host_prepare",
        status="failed",
        environment_id=env_id,
    )
    steps = _steps(_workflow(client, viewer_headers, env_id))
    connect = steps["connect"]
    assert connect["state"] == "done"
    assert connect["summary"] == "deploy target configured (host prepare not verified)"
    assert connect["details"]["prepared"] is False

    # Newer successful prepare -> prepared true, summary back to plain
    success_id = _seed_job(
        operation="genestack.host_prepare",
        status="success",
        environment_id=env_id,
        created_at=datetime.now(timezone.utc) + timedelta(minutes=1),
    )
    steps = _steps(_workflow(client, viewer_headers, env_id))
    connect = steps["connect"]
    assert connect["state"] == "done"
    assert connect["summary"] == "deploy target configured"
    assert connect["details"]["prepared"] is True
    assert connect["details"]["prepare_job_id"] == success_id

    # Jobs of another environment never leak into this view
    other = _create_env(client, admin_headers, genestack_config_dir=str(config_dir))
    other_steps = _steps(_workflow(client, viewer_headers, other["id"]))
    assert other_steps["connect"]["details"]["prepared"] is None


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


def test_workflow_unknown_environment_404(client, viewer_headers):
    resp = client.get(
        "/api/v1/environments/does-not-exist/workflow", headers=viewer_headers
    )
    assert resp.status_code == 404


def test_workflow_requires_auth(client):
    resp = client.get("/api/v1/environments/does-not-exist/workflow")
    assert resp.status_code in (401, 403)


def test_workflow_cross_tenant_forbidden(client, admin_headers):
    """A session user in tenant A cannot read the workflow of an env in tenant B."""
    tenant_a = client.post(
        "/api/v1/tenants", headers=admin_headers, json={"name": f"wf-a-{_suffix()}"}
    ).json()
    tenant_b = client.post(
        "/api/v1/tenants", headers=admin_headers, json={"name": f"wf-b-{_suffix()}"}
    ).json()
    env_b = _create_env(client, admin_headers, tenant_id=tenant_b["id"])

    username = f"wf-viewer-{_suffix()}"
    resp = client.post(
        "/api/v1/users",
        headers=admin_headers,
        json={
            "username": username,
            "password": "pw",
            "memberships": [{"tenant_id": tenant_a["id"], "role": "viewer"}],
        },
    )
    assert resp.status_code == 201, resp.text
    login = client.post(
        "/api/v1/auth/login", json={"username": username, "password": "pw"}
    )
    assert login.status_code == 200, login.text
    headers = {"Authorization": f"Bearer {login.json()['token']}"}

    resp = client.get(f"/api/v1/environments/{env_b['id']}/workflow", headers=headers)
    assert resp.status_code == 403
