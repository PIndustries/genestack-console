"""Telemetry foundation tests: models, event bus, config defaults."""

from __future__ import annotations

import asyncio

import pytest

from app.db import Base, SessionLocal, create_db_engine, engine
from app.models import AlertEvent, AlertRule, ClusterSnapshot, Environment, MetricSample
from app.services import events


@pytest.fixture(scope="module", autouse=True)
def _create_tables():
    # The app normally does this via init_db(); the module-level tests here
    # use sessions directly without the FastAPI client fixture.
    Base.metadata.create_all(bind=engine)


def _make_env(db, name: str) -> Environment:
    env = Environment(name=name, description="telemetry foundation test")
    db.add(env)
    db.commit()
    return env


def test_new_tables_created_on_fresh_db(tmp_path):
    """A fresh SQLite DB via the app's engine machinery gets all 4 tables."""
    from sqlalchemy import inspect

    fresh = create_db_engine(f"sqlite:///{tmp_path}/fresh.db")
    Base.metadata.create_all(bind=fresh)
    tables = set(inspect(fresh).get_table_names())
    assert {
        "cluster_snapshots",
        "metric_samples",
        "alert_rules",
        "alert_events",
    } <= tables


def test_cluster_snapshot_persists_and_cascades():
    with SessionLocal() as db:
        env = _make_env(db, "telemetry-snap-env")
        snap = ClusterSnapshot(
            environment_id=env.id,
            nodes=[
                {
                    "name": "n1",
                    "ready": True,
                    "roles": ["control-plane"],
                    "kubelet_version": "v1.30",
                }
            ],
            pods=[
                {
                    "ns": "default",
                    "name": "p1",
                    "phase": "Running",
                    "ready": True,
                    "restarts": 0,
                    "node": "n1",
                    "waiting_reason": None,
                }
            ],
            helm=[
                {
                    "name": "keystone",
                    "ns": "openstack",
                    "status": "deployed",
                    "chart": "keystone-1.0.0",
                    "version": "1.0.0",
                }
            ],
            summary={
                "nodes_ready": 1,
                "nodes_total": 1,
                "pods_running": 1,
                "pods_pending": 0,
                "pods_failed": 0,
                "crashlooping": [],
            },
            health="healthy",
        )
        db.add(snap)
        db.commit()
        snap_id = snap.id

        loaded = db.get(ClusterSnapshot, snap_id)
        assert loaded is not None
        assert loaded.environment_id == env.id
        assert loaded.probe_ok is True
        assert loaded.error is None
        assert loaded.nodes[0]["name"] == "n1"
        assert loaded.summary["crashlooping"] == []
        assert loaded.health == "healthy"
        assert loaded.taken_at is not None

        # Cascade: deleting the environment removes its snapshots.
        db.delete(env)
        db.commit()
        assert db.get(ClusterSnapshot, snap_id) is None


def test_metric_sample_persists():
    with SessionLocal() as db:
        env = _make_env(db, "telemetry-metric-env")
        sample = MetricSample(
            environment_id=env.id,
            name="node_cpu_usage_percent",
            labels={"node": "n1"},
            value=42.5,
        )
        db.add(sample)
        db.commit()

        loaded = db.get(MetricSample, sample.id)
        assert loaded is not None
        assert loaded.environment_id == env.id
        assert loaded.name == "node_cpu_usage_percent"
        assert loaded.labels == {"node": "n1"}
        assert loaded.value == 42.5
        assert loaded.ts is not None


def test_alert_rule_and_event_persist():
    with SessionLocal() as db:
        env = _make_env(db, "telemetry-alert-env")
        rule = AlertRule(
            environment_id=None,  # applies to all environments
            name="any crashloop",
            condition="pod_crashloop",
        )
        db.add(rule)
        db.commit()
        assert rule.threshold == 1.0
        assert rule.severity == "warning"
        assert rule.enabled is True
        assert rule.webhook_url is None
        assert rule.created_at is not None

        event = AlertEvent(
            rule_id=rule.id,
            environment_id=env.id,
            details={"pod": "default/p1", "restarts": 7},
        )
        db.add(event)
        db.commit()

        loaded = db.get(AlertEvent, event.id)
        assert loaded is not None
        assert loaded.rule_id == rule.id
        assert loaded.environment_id == env.id
        assert loaded.status == "firing"
        assert loaded.resolved_at is None
        assert loaded.acknowledged is False
        assert loaded.details == {"pod": "default/p1", "restarts": 7}
        assert loaded.fired_at is not None


def test_event_bus_publish_subscribe_unsubscribe():
    async def run():
        queue = events.subscribe(["alerts"])
        try:
            await events.publish("alerts", {"rule": "any crashloop"})
            topic, payload = await asyncio.wait_for(queue.get(), timeout=1.0)
            assert topic == "alerts"
            assert payload == {"rule": "any crashloop"}

            # Other topics are not delivered to this subscriber.
            await events.publish("metrics", {"name": "cpu"})
            assert queue.empty()
        finally:
            events.unsubscribe(queue)

        # After unsubscribe, delivery stops.
        await events.publish("alerts", {"rule": "late"})
        assert queue.empty()

    asyncio.run(run())


def test_event_bus_publish_with_zero_subscribers():
    assert events.subscriber_count() == 0
    asyncio.run(events.publish("fleet", {"ok": True}))


def test_event_bus_publish_sync_noops_without_loop():
    # init() has not been called: must silently no-op, not raise.
    events.publish_sync("alerts", {"rule": "noop"})


def test_config_telemetry_defaults():
    from app.config import get_settings

    settings = get_settings()
    assert settings.collector_enabled is True
    assert settings.collector_interval_seconds == 60
    assert settings.collector_probe_timeout_seconds == 15
    assert settings.collector_retention_hours == 168
    assert settings.metrics_enabled is False
    assert settings.metrics_retention_hours == 72
    assert settings.stream_max_subscribers == 100
