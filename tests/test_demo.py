"""Walkthrough sample tenant/env: default off, idempotent seed, canned reads."""

from __future__ import annotations

from sqlalchemy import func, select

from app.db import SessionLocal
from app.models import BaremetalNode, Environment, Tenant
from app.services.demo import (
    ENV_NAME,
    TENANT_NAME,
    seed_demo,
    seed_demo_if_enabled,
)


def _count_demo_tenants(db) -> int:
    return int(
        db.scalar(
            select(func.count()).select_from(Tenant).where(Tenant.name == TENANT_NAME)
        )
        or 0
    )


def _seed() -> dict:
    db = SessionLocal()
    try:
        return seed_demo(db)
    finally:
        db.close()


def test_settings_seed_demo_defaults_off():
    from app.config import Settings, get_settings

    assert Settings().seed_demo is False
    assert get_settings().seed_demo is False


def test_load_settings_parses_seed_demo(tmp_path):
    import yaml

    from app.config import load_settings

    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump({"seed_demo": True}), encoding="utf-8")
    assert load_settings(cfg).seed_demo is True
    cfg.write_text(yaml.safe_dump({"seed_demo": False}), encoding="utf-8")
    assert load_settings(cfg).seed_demo is False
    cfg.write_text(yaml.safe_dump({}), encoding="utf-8")
    assert load_settings(cfg).seed_demo is False


def test_seed_demo_if_enabled_false_is_noop():
    db = SessionLocal()
    try:
        before = _count_demo_tenants(db)
        result = seed_demo_if_enabled(db)
        after = _count_demo_tenants(db)
        assert result is None
        assert after == before
    finally:
        db.close()


def test_seed_demo_idempotent():
    db = SessionLocal()
    try:
        first = seed_demo(db)
        second = seed_demo(db)
        assert first["environment_id"] == second["environment_id"]
        tenants = list(
            db.scalars(select(Tenant).where(Tenant.name == TENANT_NAME)).all()
        )
        envs = list(
            db.scalars(select(Environment).where(Environment.name == ENV_NAME)).all()
        )
        assert len(tenants) == 1
        assert len(envs) == 1
        nodes = list(
            db.scalars(
                select(BaremetalNode).where(BaremetalNode.environment_id == envs[0].id)
            ).all()
        )
        assert len(nodes) == 4
        assert envs[0].tier == "demo"
        assert (envs[0].metadata_json or {}).get("demo") is True
    finally:
        db.close()


def test_demo_read_apis_return_canned_data(client, admin_headers):
    info = _seed()
    eid = info["environment_id"]

    cluster = client.get(f"/api/v1/environments/{eid}/cluster", headers=admin_headers)
    assert cluster.status_code == 200, cluster.text
    body = cluster.json()
    assert body["reachable"] is True
    assert body.get("demo") is True
    assert body["health"] == "healthy"
    assert len(body["nodes"]) == 4

    workloads = client.get(
        f"/api/v1/environments/{eid}/k8s/workloads?pods_only=1",
        headers=admin_headers,
    )
    assert workloads.status_code == 200, workloads.text
    wl = workloads.json()
    assert wl["ok"] is True
    assert wl.get("demo") is True
    assert len(wl["pods"]) >= 4
    assert all(p.get("phase") == "Running" for p in wl["pods"])

    vms = client.get(f"/api/v1/environments/{eid}/vms", headers=admin_headers)
    assert vms.status_code == 200, vms.text
    vm_body = vms.json()
    assert vm_body.get("demo") is True
    assert len(vm_body["vms"]) >= 1

    metal = client.get(f"/api/v1/environments/{eid}/baremetal", headers=admin_headers)
    assert metal.status_code == 200, metal.text
    assert metal.json()["count"] == 4

    observe = client.get(f"/api/v1/environments/{eid}/observe", headers=admin_headers)
    assert observe.status_code == 200, observe.text
    ob = observe.json()
    assert ob["health"] == "healthy"
    assert ob.get("demo") is True
    assert ob["live"]["kubernetes"]["ready"] == 4

    health = client.get(f"/api/v1/environments/{eid}/health", headers=admin_headers)
    assert health.status_code == 200, health.text
    chips = {c["id"]: c for c in health.json()["chips"]}
    assert chips["k8s"]["state"] == "ok"
    assert chips["identity"]["state"] == "ok"

    plat = client.get(f"/api/v1/environments/{eid}/platform", headers=admin_headers)
    assert plat.status_code == 200, plat.text
    assert plat.json().get("demo") is True
    assert len(plat.json()["nodes"]) == 4


def test_demo_job_create_returns_400(client, admin_headers):
    info = _seed()
    eid = info["environment_id"]
    resp = client.post(
        f"/api/v1/environments/{eid}/jobs",
        headers=admin_headers,
        json={"operation": "internal.health", "params": {}, "run_sync": True},
    )
    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    text = detail if isinstance(detail, str) else str(detail)
    assert "walkthrough is a sample" in text
