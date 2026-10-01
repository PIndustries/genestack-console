"""Alert service and router tests (/api/v1/alerts)."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.db import Base, SessionLocal, engine
from app.models import (
    AlertEvent,
    AlertRule,
    ClusterSnapshot,
    Environment,
    Membership,
    Tenant,
    UserRole,
)
from app.services import accounts, alerts


@pytest.fixture(scope="module", autouse=True)
def _create_tables():
    # The app normally does this via init_db(); the service-level tests here
    # use sessions directly without the FastAPI client fixture.
    Base.metadata.create_all(bind=engine)


@pytest.fixture(autouse=True)
def _clean_alert_rows(_create_tables):
    """Each test starts with empty alert_rules/alert_events tables."""
    db = SessionLocal()
    try:
        db.query(AlertEvent).delete()
        db.query(AlertRule).delete()
        db.commit()
    finally:
        db.close()


def _suffix() -> str:
    return uuid.uuid4().hex[:8]


def _make_env(name: str | None = None, tenant_id: str | None = None) -> Environment:
    db = SessionLocal()
    try:
        env = Environment(name=name or f"env-alerts-{_suffix()}", tenant_id=tenant_id)
        db.add(env)
        db.commit()
        db.refresh(env)
        return env
    finally:
        db.close()


def _make_tenant() -> Tenant:
    db = SessionLocal()
    try:
        tenant = Tenant(name=f"tenant-alerts-{_suffix()}")
        db.add(tenant)
        db.commit()
        db.refresh(tenant)
        return tenant
    finally:
        db.close()


def _make_rule(db, **fields) -> int:
    """Insert an AlertRule on the given session; returns the rule id.

    Returns the id (not the ORM object) so later commits on the same session
    cannot expire the attributes callers rely on.
    """
    rule = AlertRule(
        name=fields.pop("name", f"rule-{_suffix()}"),
        condition=fields.pop("condition", "pod_crashloop"),
        **fields,
    )
    db.add(rule)
    db.commit()
    db.refresh(rule)
    return rule.id


def _healthy_snapshot(env_id: str, **overrides) -> ClusterSnapshot:
    """A transient (unpersisted) healthy snapshot; fields overridable."""
    data = {
        "environment_id": env_id,
        "probe_ok": True,
        "error": None,
        "nodes": [
            {
                "name": "n1",
                "ready": True,
                "roles": ["control-plane"],
                "kubelet_version": "v1.30",
            }
        ],
        "pods": [],
        "helm": [
            {
                "name": "openstack-keystone",
                "ns": "openstack",
                "status": "deployed",
                "chart": "keystone-1.0.0",
                "version": "1.0.0",
            },
        ],
        "summary": {
            "nodes_ready": 1,
            "nodes_total": 1,
            "pods_running": 1,
            "pods_pending": 0,
            "pods_failed": 0,
            "crashlooping": [],
        },
        "health": "healthy",
    }
    data.update(overrides)
    return ClusterSnapshot(**data)


def _evaluate(env: Environment, snapshot: ClusterSnapshot) -> list[AlertEvent]:
    db = SessionLocal()
    try:
        return alerts.evaluate_snapshot(db, db.get(Environment, env.id), snapshot)
    finally:
        db.close()


def _events(db, rule_id: int | None = None) -> list[AlertEvent]:
    from sqlalchemy import select

    stmt = select(AlertEvent)
    if rule_id is not None:
        stmt = stmt.where(AlertEvent.rule_id == rule_id)
    return list(db.scalars(stmt).all())


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------


def test_seed_default_rules_creates_four_global_rules():
    db = SessionLocal()
    try:
        assert alerts.seed_default_rules(db) == 4
        rules = _events(db)  # no events; sanity
        assert rules == []
        from sqlalchemy import select

        rows = list(db.scalars(select(AlertRule)).all())
        assert len(rows) == 4
        by_name = {r.name: r for r in rows}
        assert by_name["node-not-ready"].condition == "node_not_ready"
        assert by_name["node-not-ready"].severity == "critical"
        assert by_name["pod-crashloop"].condition == "pod_crashloop"
        assert by_name["pod-crashloop"].severity == "warning"
        assert by_name["probe-failed"].condition == "probe_failed"
        assert by_name["probe-failed"].severity == "critical"
        assert by_name["core-service-down"].condition == "service_down"
        assert by_name["core-service-down"].severity == "critical"
        for rule in rows:
            assert rule.environment_id is None  # global
            assert rule.enabled is True
            assert rule.threshold == 1.0
    finally:
        db.close()


def test_seed_default_rules_idempotent():
    db = SessionLocal()
    try:
        assert alerts.seed_default_rules(db) == 4
        assert alerts.seed_default_rules(db) == 0
        # Any existing rule (even a custom one) suppresses seeding.
    finally:
        db.close()

    db = SessionLocal()
    try:
        _make_rule(db, condition="probe_failed")
        assert alerts.seed_default_rules(db) == 0
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Conditions
# ---------------------------------------------------------------------------


def test_probe_failed_fires_and_healthy_does_not():
    env = _make_env()
    db = SessionLocal()
    try:
        rule_id = _make_rule(db, condition="probe_failed", severity="critical")
    finally:
        db.close()

    snap = _healthy_snapshot(
        env.id, probe_ok=False, error="connection refused", health="down"
    )
    changed = _evaluate(env, snap)
    assert len(changed) == 1
    event = changed[0]
    assert event.rule_id == rule_id
    assert event.status == "firing"
    assert event.details == {"error": "connection refused"}

    # A healthy probe never fires.
    assert _evaluate(_make_env(), _healthy_snapshot(env.id)) == []


def test_node_not_ready_fires_with_node_names():
    env = _make_env()
    db = SessionLocal()
    try:
        _make_rule(db, condition="node_not_ready")
    finally:
        db.close()

    snap = _healthy_snapshot(
        env.id,
        nodes=[
            {"name": "n1", "ready": True},
            {"name": "n2", "ready": False},
        ],
        summary={
            "nodes_ready": 1,
            "nodes_total": 2,
            "pods_running": 1,
            "pods_pending": 0,
            "pods_failed": 0,
            "crashlooping": [],
        },
        health="degraded",
    )
    changed = _evaluate(env, snap)
    assert len(changed) == 1
    assert changed[0].details == {"nodes": ["n2"]}

    # All nodes ready: no alert.
    healthy = _healthy_snapshot(env.id)
    assert _evaluate(_make_env(), healthy) == []


def test_pod_crashloop_fires_with_crashlooping_list():
    env = _make_env()
    db = SessionLocal()
    try:
        _make_rule(db, condition="pod_crashloop")
    finally:
        db.close()

    crashing = ["openstack/nova-compute-0"]
    snap = _healthy_snapshot(
        env.id,
        summary={
            "nodes_ready": 1,
            "nodes_total": 1,
            "pods_running": 1,
            "pods_pending": 0,
            "pods_failed": 0,
            "crashlooping": crashing,
        },
        health="degraded",
    )
    changed = _evaluate(env, snap)
    assert len(changed) == 1
    assert changed[0].details == {"crashlooping": crashing}

    assert _evaluate(_make_env(), _healthy_snapshot(env.id)) == []


def test_service_down_matches_core_services_by_substring():
    env = _make_env()
    db = SessionLocal()
    try:
        _make_rule(db, condition="service_down")
    finally:
        db.close()

    helm = [
        {"name": "openstack-mariadb", "ns": "openstack", "status": "failed"},
        {"name": "openstack-keystone", "ns": "openstack", "status": "deployed"},
        # Down but not a core service: must not count.
        {"name": "custom-app", "ns": "apps", "status": "failed"},
    ]
    snap = _healthy_snapshot(env.id, helm=helm, health="degraded")
    changed = _evaluate(env, snap)
    assert len(changed) == 1
    assert changed[0].details == {
        "services": [
            {"name": "openstack-mariadb", "ns": "openstack", "status": "failed"}
        ]
    }

    # All core services deployed: no alert.
    assert _evaluate(_make_env(), _healthy_snapshot(env.id)) == []


def test_threshold_respected():
    env = _make_env()
    db = SessionLocal()
    try:
        _make_rule(db, condition="pod_crashloop", threshold=2.0)
    finally:
        db.close()

    one = _healthy_snapshot(
        env.id,
        summary={
            "nodes_ready": 1,
            "nodes_total": 1,
            "pods_running": 1,
            "pods_pending": 0,
            "pods_failed": 0,
            "crashlooping": ["a/p1"],
        },
    )
    assert _evaluate(env, one) == []

    two = _healthy_snapshot(
        env.id,
        summary={
            "nodes_ready": 1,
            "nodes_total": 1,
            "pods_running": 1,
            "pods_pending": 0,
            "pods_failed": 0,
            "crashlooping": ["a/p1", "a/p2"],
        },
    )
    assert len(_evaluate(env, two)) == 1


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------


def test_no_duplicate_firing_events_while_condition_stays_true():
    env = _make_env()
    db = SessionLocal()
    try:
        rule_id = _make_rule(db, condition="probe_failed")
    finally:
        db.close()

    snap = _healthy_snapshot(env.id, probe_ok=False, error="first")
    assert len(_evaluate(env, snap)) == 1

    # Still failing: no new event; details are refreshed in place.
    snap2 = _healthy_snapshot(env.id, probe_ok=False, error="second")
    assert _evaluate(env, snap2) == []

    db = SessionLocal()
    try:
        events = _events(db, rule_id)
        assert len(events) == 1
        assert events[0].status == "firing"
        assert events[0].details == {"error": "second"}
    finally:
        db.close()


def test_resolve_transition_sets_resolved_at():
    env = _make_env()
    db = SessionLocal()
    try:
        rule_id = _make_rule(db, condition="probe_failed")
    finally:
        db.close()

    _evaluate(env, _healthy_snapshot(env.id, probe_ok=False, error="boom"))
    changed = _evaluate(env, _healthy_snapshot(env.id))
    assert len(changed) == 1
    assert changed[0].status == "resolved"
    assert changed[0].resolved_at is not None

    db = SessionLocal()
    try:
        events = _events(db, rule_id)
        assert len(events) == 1
        assert events[0].status == "resolved"
        assert events[0].resolved_at is not None
    finally:
        db.close()


def test_webhook_called_on_fire_only(monkeypatch):
    calls = []
    monkeypatch.setattr(
        alerts, "fire_webhook", lambda url, payload: calls.append((url, payload))
    )

    env = _make_env()
    url = "http://hooks.example/alert"
    db = SessionLocal()
    try:
        _make_rule(db, condition="probe_failed", webhook_url=url)
    finally:
        db.close()

    _evaluate(env, _healthy_snapshot(env.id, probe_ok=False, error="boom"))
    assert len(calls) == 1
    assert calls[0][0] == url
    assert calls[0][1]["type"] == "alert_fired"

    # Still firing: no second webhook. Resolve: no webhook either.
    _evaluate(env, _healthy_snapshot(env.id, probe_ok=False, error="boom"))
    _evaluate(env, _healthy_snapshot(env.id))
    assert len(calls) == 1


def test_events_published_on_fire_and_resolve(monkeypatch):
    published = []
    monkeypatch.setattr(
        alerts,
        "publish_sync",
        lambda topic, payload: published.append((topic, payload)),
    )

    env = _make_env()
    db = SessionLocal()
    try:
        _make_rule(db, condition="probe_failed")
    finally:
        db.close()

    _evaluate(env, _healthy_snapshot(env.id, probe_ok=False, error="boom"))
    assert [(t, p["type"]) for t, p in published] == [("alerts", "alert_fired")]
    assert published[0][1]["environment_id"] == env.id

    _evaluate(env, _healthy_snapshot(env.id))
    assert [(t, p["type"]) for t, p in published] == [
        ("alerts", "alert_fired"),
        ("alerts", "alert_resolved"),
    ]


def test_env_scoped_rule_applies_only_to_its_env():
    env_a = _make_env()
    env_b = _make_env()
    db = SessionLocal()
    try:
        _make_rule(db, condition="probe_failed", environment_id=env_a.id)
    finally:
        db.close()

    snap = _healthy_snapshot(env_b.id, probe_ok=False, error="boom")
    assert _evaluate(env_b, snap) == []

    snap_a = _healthy_snapshot(env_a.id, probe_ok=False, error="boom")
    assert len(_evaluate(env_a, snap_a)) == 1


def test_disabled_rule_never_fires():
    env = _make_env()
    db = SessionLocal()
    try:
        _make_rule(db, condition="probe_failed", enabled=False)
    finally:
        db.close()

    snap = _healthy_snapshot(env.id, probe_ok=False, error="boom")
    assert _evaluate(env, snap) == []


def test_seeded_rules_fire_end_to_end():
    env = _make_env()
    db = SessionLocal()
    try:
        alerts.seed_default_rules(db)
    finally:
        db.close()

    changed = _evaluate(env, _healthy_snapshot(env.id, probe_ok=False, error="boom"))
    assert len(changed) == 1
    assert changed[0].rule_id is not None


# ---------------------------------------------------------------------------
# Router tests (minimal app: integration wires the router into app.main)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.routers import alerts as alerts_router

    app = FastAPI()
    app.include_router(alerts_router.router)
    with TestClient(app) as c:
        yield c


def _session_headers(memberships, username=None):
    """Session-token headers for a new user with the given memberships.

    memberships: list of (tenant_id, role) tuples.
    """
    db = SessionLocal()
    try:
        user = accounts.create_user(db, username or f"user-alerts-{_suffix()}", "pw")
        for tenant_id, role in memberships:
            db.add(
                Membership(user_id=user.id, tenant_id=tenant_id, role=UserRole(role))
            )
        db.flush()
        token = accounts.create_session(db, user)
        db.commit()
        return {"Authorization": f"Bearer {token.token}"}
    finally:
        db.close()


def _make_event(rule_id: int, env_id: str, status="firing", fired_at=None) -> int:
    db = SessionLocal()
    try:
        event = AlertEvent(
            rule_id=rule_id,
            environment_id=env_id,
            status=status,
            fired_at=fired_at or datetime.now(timezone.utc),
        )
        db.add(event)
        db.commit()
        return event.id
    finally:
        db.close()


def test_rules_crud_as_platform_admin(client, operator_headers, admin_headers):
    resp = client.post(
        "/api/v1/alerts/rules",
        headers=operator_headers,
        json={
            "name": "t-crashloop",
            "condition": "pod_crashloop",
            "threshold": 3,
            "severity": "warning",
        },
    )
    assert resp.status_code == 201, resp.text
    rule = resp.json()
    assert rule["environment_id"] is None
    assert rule["threshold"] == 3
    assert rule["enabled"] is True

    rules = client.get("/api/v1/alerts/rules", headers=admin_headers).json()
    assert rule["id"] in {r["id"] for r in rules}

    resp = client.patch(
        f"/api/v1/alerts/rules/{rule['id']}",
        headers=operator_headers,
        json={"threshold": 5, "enabled": False},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["threshold"] == 5
    assert resp.json()["enabled"] is False

    resp = client.delete(f"/api/v1/alerts/rules/{rule['id']}", headers=operator_headers)
    assert resp.status_code == 204, resp.text
    rules = client.get("/api/v1/alerts/rules", headers=admin_headers).json()
    assert rule["id"] not in {r["id"] for r in rules}

    resp = client.patch(
        f"/api/v1/alerts/rules/{rule['id']}",
        headers=operator_headers,
        json={"threshold": 1},
    )
    assert resp.status_code == 404


def test_viewer_cannot_create_rule(client, viewer_headers):
    resp = client.post(
        "/api/v1/alerts/rules",
        headers=viewer_headers,
        json={"name": "nope", "condition": "probe_failed"},
    )
    assert resp.status_code == 403


def test_invalid_condition_rejected(client, operator_headers):
    resp = client.post(
        "/api/v1/alerts/rules",
        headers=operator_headers,
        json={"name": "bad", "condition": "bogus"},
    )
    assert resp.status_code == 422


def test_global_rule_forbidden_for_tenant_operator(client):
    tenant = _make_tenant()
    headers = _session_headers([(tenant.id, "operator")])

    resp = client.post(
        "/api/v1/alerts/rules",
        headers=headers,
        json={"name": "global-nope", "condition": "probe_failed"},
    )
    assert resp.status_code == 403


def test_env_rule_tenant_scoping(client):
    tenant_a = _make_tenant()
    tenant_b = _make_tenant()
    env_a = _make_env(tenant_id=tenant_a.id)
    operator_a = _session_headers([(tenant_a.id, "operator")])
    operator_b = _session_headers([(tenant_b.id, "operator")])

    resp = client.post(
        "/api/v1/alerts/rules",
        headers=operator_a,
        json={
            "name": "a-rule",
            "condition": "probe_failed",
            "environment_id": env_a.id,
        },
    )
    assert resp.status_code == 201, resp.text
    rule_id = resp.json()["id"]

    # Operator from another tenant cannot see/edit/delete it.
    assert (
        client.patch(
            f"/api/v1/alerts/rules/{rule_id}", headers=operator_b, json={"threshold": 2}
        ).status_code
        == 403
    )
    assert (
        client.delete(f"/api/v1/alerts/rules/{rule_id}", headers=operator_b).status_code
        == 403
    )
    visible = client.get("/api/v1/alerts/rules", headers=operator_b).json()
    assert rule_id not in {r["id"] for r in visible}

    # The owning tenant's operator can edit it.
    resp = client.patch(
        f"/api/v1/alerts/rules/{rule_id}", headers=operator_a, json={"threshold": 2}
    )
    assert resp.status_code == 200, resp.text


def test_events_list_filters_order_and_scoping(client, admin_headers):
    tenant_a = _make_tenant()
    tenant_b = _make_tenant()
    env_a = _make_env(tenant_id=tenant_a.id)
    env_b = _make_env(tenant_id=tenant_b.id)

    db = SessionLocal()
    try:
        rule_a_id = _make_rule(db, condition="probe_failed", environment_id=env_a.id)
        rule_b_id = _make_rule(db, condition="pod_crashloop", environment_id=env_b.id)
    finally:
        db.close()

    base = datetime.now(timezone.utc)
    old = _make_event(rule_a_id, env_a.id, fired_at=base - timedelta(minutes=5))
    new = _make_event(rule_a_id, env_a.id, fired_at=base)
    resolved = _make_event(
        rule_a_id, env_a.id, status="resolved", fired_at=base - timedelta(minutes=10)
    )
    other = _make_event(rule_b_id, env_b.id, fired_at=base)

    events = client.get("/api/v1/alerts/events", headers=admin_headers).json()
    ids = [e["id"] for e in events]
    assert ids.index(new) < ids.index(old)  # newest first

    firing = client.get(
        "/api/v1/alerts/events?status=firing", headers=admin_headers
    ).json()
    firing_ids = {e["id"] for e in firing}
    assert {old, new, other} <= firing_ids
    assert resolved not in firing_ids

    by_env = client.get(
        f"/api/v1/alerts/events?environment_id={env_a.id}", headers=admin_headers
    ).json()
    assert {e["id"] for e in by_env} == {old, new, resolved}

    # Tenant-scoped viewer: only own tenant's events, other env is 404.
    viewer_a = _session_headers([(tenant_a.id, "viewer")])
    scoped = client.get("/api/v1/alerts/events", headers=viewer_a).json()
    assert {e["environment_id"] for e in scoped} == {env_a.id}
    resp = client.get(
        f"/api/v1/alerts/events?environment_id={env_b.id}", headers=viewer_a
    )
    assert resp.status_code == 404


def test_ack_flow(client, admin_headers):
    tenant = _make_tenant()
    env = _make_env(tenant_id=tenant.id)
    db = SessionLocal()
    try:
        rule_id = _make_rule(db, condition="probe_failed", environment_id=env.id)
    finally:
        db.close()
    event_id = _make_event(rule_id, env.id)

    viewer = _session_headers([(tenant.id, "viewer")])
    assert (
        client.post(f"/api/v1/alerts/events/{event_id}/ack", headers=viewer).status_code
        == 403
    )

    operator = _session_headers([(tenant.id, "operator")])
    resp = client.post(f"/api/v1/alerts/events/{event_id}/ack", headers=operator)
    assert resp.status_code == 200, resp.text
    assert resp.json()["acknowledged"] is True

    resp = client.post("/api/v1/alerts/events/999999/ack", headers=admin_headers)
    assert resp.status_code == 404


def test_summary_shape(client, admin_headers):
    tenant = _make_tenant()
    env = _make_env(tenant_id=tenant.id)
    db = SessionLocal()
    try:
        rule_crit_id = _make_rule(
            db, condition="probe_failed", severity="critical", environment_id=env.id
        )
        rule_warn_id = _make_rule(
            db, condition="pod_crashloop", severity="warning", environment_id=env.id
        )
    finally:
        db.close()
    _make_event(rule_crit_id, env.id)
    _make_event(rule_warn_id, env.id)
    _make_event(rule_warn_id, env.id, status="resolved")  # resolved: not counted

    resp = client.get("/api/v1/alerts/summary", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["firing"] == 2
    assert body["by_severity"] == {"critical": 1, "warning": 1}
    assert body["by_environment"] == {env.id: 2}

    # Tenant scoping: a viewer in an unrelated tenant sees nothing.
    other_tenant = _make_tenant()
    viewer = _session_headers([(other_tenant.id, "viewer")])
    body = client.get("/api/v1/alerts/summary", headers=viewer).json()
    assert body == {"firing": 0, "by_severity": {}, "by_environment": {}}


def test_alerts_require_auth(client):
    assert client.get("/api/v1/alerts/rules").status_code == 401
    assert client.get("/api/v1/alerts/events").status_code == 401
    assert client.get("/api/v1/alerts/summary").status_code == 401
