"""Observe dashboard endpoints: env + fleet, snapshots, metrics, tenant scope."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.db import SessionLocal
from app.models import (
    AlertEvent,
    AlertRule,
    ClusterSnapshot,
    Job,
    JobStatus,
    MetricSample,
)
from app.routers import observe as observe_router
from app.services import metrics
from app.services import observe as observe_service


@pytest.fixture(scope="module", autouse=True)
def _wire_router(app):
    paths = {getattr(r, "path", None) for r in app.routes}
    if "/api/v1/environments/{environment_id}/observe" not in paths:
        app.include_router(observe_router.router)


@pytest.fixture(autouse=True)
def _skip_live(monkeypatch):
    monkeypatch.setattr(
        "app.services.observe.probe_live",
        lambda *a, **k: {
            "talos": {"machines": 0, "reachable": 0, "versions": []},
            "kubernetes": {"health": "unknown", "nodes": 0, "ready": 0, "pods": 0},
            "openstack": {
                "available": False,
                "servers": 0,
                "servers_active": 0,
                "volumes": 0,
                "networks": 0,
            },
            "jobs": {"running": 0, "failed": 0},
            "alerts": {"firing": 0},
            "now": {
                "problem_pods": 0,
                "helm_failed": 0,
                "volume_errors": 0,
                "problems": [],
            },
        },
    )


def _suffix() -> str:
    return uuid.uuid4().hex[:8]


def _create_env(client, admin_headers, tenant_id=None, name=None):
    body = {"name": name or f"env-ob-{_suffix()}"}
    if tenant_id:
        body["tenant_id"] = tenant_id
    resp = client.post("/api/v1/environments", headers=admin_headers, json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _add_snapshot(
    env_id: str, *, summary=None, pods=None, helm=None, health="healthy", taken_at=None
):
    with SessionLocal() as db:
        snap = ClusterSnapshot(
            environment_id=env_id,
            taken_at=taken_at or datetime.now(timezone.utc),
            probe_ok=True,
            nodes=[{"name": "n1", "ready": True}, {"name": "n2", "ready": False}],
            pods=pods or [],
            helm=helm or [],
            summary=summary
            or {
                "nodes_ready": 2,
                "nodes_total": 3,
                "pods_failed": 0,
                "crashlooping": [],
            },
            health=health,
        )
        db.add(snap)
        db.commit()


def _add_sample(
    env_id: str, name: str, value: float, ts: datetime | None = None
) -> None:
    with SessionLocal() as db:
        db.add(
            MetricSample(
                environment_id=env_id,
                ts=ts or datetime.now(timezone.utc),
                name=name,
                labels={},
                value=value,
            )
        )
        db.commit()


def _add_job(env_id: str, status: JobStatus = JobStatus.running) -> None:
    with SessionLocal() as db:
        db.add(Job(environment_id=env_id, operation="noop", status=status, log_text=""))
        db.commit()


def _add_alert(env_id: str) -> None:
    with SessionLocal() as db:
        rule = AlertRule(
            name=f"rule-{_suffix()}", condition="pod_crashloop", severity="warning"
        )
        db.add(rule)
        db.flush()
        db.add(AlertEvent(rule_id=rule.id, environment_id=env_id, status="firing"))
        db.commit()


def test_observe_empty_env_returns_200(client, admin_headers):
    env = _create_env(client, admin_headers)
    resp = client.get(
        f"/api/v1/environments/{env['id']}/observe?hours=24", headers=admin_headers
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["environment_id"] == env["id"]
    assert body["hours"] == 24
    assert "metrics_enabled" in body
    assert "generated_at" in body
    assert body["live"]["talos"]["machines"] == 0
    assert body["live"]["kubernetes"]["nodes"] == 0
    assert body["live"]["jobs"]["running"] == 0
    assert body["live"]["alerts"]["firing"] == 0
    assert body["plane"]["talos"]["machines"] == 0
    assert body["plane"]["kubernetes"]["nodes"] == 0
    assert body["plane"]["jobs"]["running"] == 0
    assert body["plane"]["alerts"]["firing"] == 0
    assert body["now"]["problem_pods"] == 0
    assert "node.cpu.cores" in body["series"]
    assert body["series"]["node.cpu.cores"] == []
    assert body["series"]["cluster.nodes.ready"] == []


def test_observe_from_snapshot_metrics_jobs_alerts(client, admin_headers):
    env = _create_env(client, admin_headers)
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    _add_snapshot(
        env["id"],
        summary={
            "nodes_ready": 2,
            "nodes_total": 3,
            "pods_failed": 1,
            "crashlooping": ["openstack/nova-0"],
        },
        pods=[
            {
                "ns": "openstack",
                "name": "nova-0",
                "phase": "Running",
                "ready": False,
                "waiting_reason": "CrashLoopBackOff",
            },
            {"ns": "kube-system", "name": "coredns", "phase": "Running", "ready": True},
        ],
        helm=[
            {"name": "keystone", "status": "deployed"},
            {"name": "nova", "status": "failed"},
        ],
        taken_at=now,
    )
    _add_sample(env["id"], "node.cpu.cores", 1.5, now + timedelta(minutes=2))
    _add_job(env["id"], JobStatus.running)
    _add_job(env["id"], JobStatus.success)
    _add_alert(env["id"])

    resp = client.get(
        f"/api/v1/environments/{env['id']}/observe?hours=6", headers=admin_headers
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["hours"] == 6
    assert body["plane"]["kubernetes"]["ready"] == 2
    assert body["plane"]["kubernetes"]["nodes"] == 3
    assert body["plane"]["talos"]["machines"] == 3
    assert body["plane"]["jobs"]["running"] == 1
    assert body["plane"]["alerts"]["firing"] == 1
    assert body["now"]["problem_pods"] >= 1
    assert body["now"]["helm_failed"] == 1
    cpu = body["series"]["node.cpu.cores"]
    assert len(cpu) >= 1
    assert cpu[0]["v"] == 1.5
    assert body["series"]["cluster.nodes.ready"]
    assert body["series"]["cluster.nodes.ready"][-1]["v"] == 2.0


def test_observe_hours_validation(client, admin_headers):
    env = _create_env(client, admin_headers)
    url = f"/api/v1/environments/{env['id']}/observe"
    assert client.get(f"{url}?hours=0", headers=admin_headers).status_code == 422
    assert client.get(f"{url}?hours=169", headers=admin_headers).status_code == 422
    ok = client.get(f"{url}?hours=168", headers=admin_headers)
    assert ok.status_code == 200
    assert ok.json()["hours"] == 168


def test_fleet_observe_lists_env_stats(client, admin_headers):
    env = _create_env(client, admin_headers)
    _add_snapshot(env["id"], summary={"nodes_ready": 4, "nodes_total": 4})
    _add_job(env["id"], JobStatus.queued)
    resp = client.get("/api/v1/fleet/observe", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    rows = {r["environment_id"]: r for r in body["environments"]}
    assert env["id"] in rows
    card = rows[env["id"]]
    assert card["name"] == env["name"]
    assert card["plane"]["kubernetes"]["ready"] == 4
    assert card["plane"]["kubernetes"]["nodes"] == 4
    assert card["plane"]["jobs"]["running"] == 1
    assert card["health"] == "healthy"


def test_observe_tenant_isolation(client, admin_headers):
    suffix = _suffix()
    tenant_a = client.post(
        "/api/v1/tenants", headers=admin_headers, json={"name": f"ta-{suffix}"}
    ).json()
    tenant_b = client.post(
        "/api/v1/tenants", headers=admin_headers, json={"name": f"tb-{suffix}"}
    ).json()
    env_a = _create_env(client, admin_headers, tenant_id=tenant_a["id"])
    env_b = _create_env(client, admin_headers, tenant_id=tenant_b["id"])
    _add_snapshot(env_b["id"])

    user = client.post(
        "/api/v1/users",
        headers=admin_headers,
        json={
            "username": f"viewer-{suffix}",
            "password": "pw",
            "memberships": [{"tenant_id": tenant_a["id"], "role": "viewer"}],
        },
    ).json()
    login = client.post(
        "/api/v1/auth/login", json={"username": user["username"], "password": "pw"}
    )
    headers = {"Authorization": f"Bearer {login.json()['token']}"}

    own = client.get(f"/api/v1/environments/{env_a['id']}/observe", headers=headers)
    assert own.status_code == 200, own.text
    assert "live" in own.json()
    assert (
        client.get(
            f"/api/v1/environments/{env_b['id']}/observe", headers=headers
        ).status_code
        == 403
    )
    fleet = client.get("/api/v1/fleet/observe", headers=headers)
    assert fleet.status_code == 200
    ids = {r["environment_id"] for r in fleet.json()["environments"]}
    assert env_a["id"] in ids
    assert env_b["id"] not in ids


GAUGE_LIVE = {
    "talos": {"machines": 3, "reachable": 2, "versions": ["v1.10.4"]},
    "kubernetes": {"health": "healthy", "nodes": 3, "ready": 3, "pods": 12},
    "openstack": {
        "available": True,
        "servers": 4,
        "servers_active": 3,
        "volumes": 5,
        "networks": 2,
    },
    "jobs": {"running": 1, "failed": 2},
    "alerts": {"firing": 0},
}

GAUGE_NAMES = {
    "cluster.nodes.ready",
    "cluster.nodes.total",
    "cluster.pods.running",
    "talos.nodes.reachable",
    "talos.nodes.total",
    "cloud.servers.active",
    "cloud.volumes",
    "jobs.running",
    "jobs.failed",
    "alerts.firing",
}


def test_collect_writes_gauges_when_kubectl_top_fails(monkeypatch):
    monkeypatch.setattr(
        metrics, "_run_kubectl", lambda *a, **k: (None, "metrics-server unavailable")
    )
    monkeypatch.setattr(observe_service, "probe_live", lambda *a, **k: GAUGE_LIVE)
    monkeypatch.setattr(metrics.events, "publish_sync", lambda *a, **k: None)

    from app.config import get_settings
    from app.models import Environment

    with SessionLocal() as db:
        env = Environment(name=f"obs-gauge-{_suffix()}", description="observe collect")
        db.add(env)
        db.commit()
        settings = get_settings().model_copy(update={"metrics_enabled": True})
        written = metrics.collect_for_environment(db, env, settings)
        assert written == len(GAUGE_NAMES)
        rows = (
            db.execute(
                select(MetricSample).where(MetricSample.environment_id == env.id)
            )
            .scalars()
            .all()
        )
        by_name = {r.name: r.value for r in rows}
        assert set(by_name) == GAUGE_NAMES
        assert by_name["cluster.nodes.ready"] == 3.0
        assert by_name["talos.nodes.reachable"] == 2.0
        assert by_name["cloud.servers.active"] == 3.0
        assert by_name["jobs.failed"] == 2.0
        assert by_name["alerts.firing"] == 0.0
