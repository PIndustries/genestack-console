"""Metrics service tests: collect, parse, prune, series bucketing."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.config import get_settings
from app.db import Base, SessionLocal, engine
from app.models import Environment, MetricSample
from app.services import metrics


@pytest.fixture(scope="module", autouse=True)
def _create_tables():
    Base.metadata.create_all(bind=engine)


def _make_env(db, name: str) -> Environment:
    env = Environment(name=name, description="metrics service test")
    db.add(env)
    db.commit()
    return env


def _settings(**overrides):
    return get_settings().model_copy(update=overrides)


NODES_FIXTURE = """\
node-1   250m    6%    1024Mi   12%
node-2   1250m   14%   1Gi      28%
node-3   2       20%   512Ki    1%
"""

PODS_FIXTURE = """\
default       web-abc        125m   64Mi
kube-system   coredns-xyz    12m    24Mi
openstack     keystone-0     1      1Gi
"""


def test_collect_disabled_returns_zero_and_no_subprocess(monkeypatch):
    calls = []

    def spy(*args, **kwargs):
        calls.append((args, kwargs))
        return "", None

    monkeypatch.setattr(metrics, "_run_kubectl", spy)
    with SessionLocal() as db:
        env = _make_env(db, "metrics-disabled-env")
        settings = _settings(metrics_enabled=False)
        assert metrics.collect_for_environment(db, env, settings) == 0
    assert calls == []


def test_parse_cpu_and_memory_units():
    assert metrics._parse_cpu_cores("250m") == 0.25
    assert metrics._parse_cpu_cores("1250m") == 1.25
    assert metrics._parse_cpu_cores("2") == 2.0
    assert metrics._parse_cpu_cores("bogus") is None
    assert metrics._parse_memory_bytes("512Ki") == 512 * 1024
    assert metrics._parse_memory_bytes("64Mi") == 64 * 1024**2
    assert metrics._parse_memory_bytes("1Gi") == float(1024**3)
    assert metrics._parse_memory_bytes("bogus") is None


def test_parse_top_nodes_fixture():
    samples = metrics.parse_top_nodes(NODES_FIXTURE)
    by_key = {(s["name"], s["labels"]["node"]): s["value"] for s in samples}
    assert by_key[("node.cpu.cores", "node-1")] == 0.25
    assert by_key[("node.cpu.cores", "node-2")] == 1.25
    assert by_key[("node.cpu.cores", "node-3")] == 2.0
    assert by_key[("node.memory.bytes", "node-1")] == 1024 * 1024**2
    assert by_key[("node.memory.bytes", "node-2")] == float(1024**3)
    assert by_key[("node.memory.bytes", "node-3")] == 512 * 1024
    assert all(set(s["labels"]) == {"node"} for s in samples)


def test_parse_top_pods_fixture():
    samples = metrics.parse_top_pods(PODS_FIXTURE)
    cpu = {
        (s["labels"]["ns"], s["labels"]["pod"]): s["value"]
        for s in samples
        if s["name"] == "pod.cpu.cores"
    }
    mem = {
        (s["labels"]["ns"], s["labels"]["pod"]): s["value"]
        for s in samples
        if s["name"] == "pod.memory.bytes"
    }
    assert cpu[("default", "web-abc")] == 0.125
    assert cpu[("kube-system", "coredns-xyz")] == 0.012
    assert cpu[("openstack", "keystone-0")] == 1.0
    assert mem[("default", "web-abc")] == 64 * 1024**2
    assert mem[("openstack", "keystone-0")] == float(1024**3)
    assert all(set(s["labels"]) == {"ns", "pod"} for s in samples)


def test_parse_top_pods_with_node_column():
    line = "default   web-abc   100m   32Mi   node-9\n"
    samples = metrics.parse_top_pods(line)
    assert samples[0]["labels"] == {"ns": "default", "pod": "web-abc", "node": "node-9"}


def test_collect_kubectl_failure_returns_zero(monkeypatch):
    def fail(*args, **kwargs):
        return None, "metrics-server unavailable"

    monkeypatch.setattr(metrics, "_run_kubectl", fail)
    monkeypatch.setattr(metrics, "_collect_live_gauges", lambda *a, **k: [])
    with SessionLocal() as db:
        env = _make_env(db, "metrics-fail-env")
        settings = _settings(metrics_enabled=True)
        assert metrics.collect_for_environment(db, env, settings) == 0
        count = (
            db.execute(
                select(MetricSample).where(MetricSample.environment_id == env.id)
            )
            .scalars()
            .all()
        )
        assert count == []


def test_collect_success_inserts_and_publishes(monkeypatch):
    def fake_run(args, *, env, timeout):
        if args[:2] == ["top", "nodes"]:
            return NODES_FIXTURE, None
        if args[:2] == ["top", "pods"]:
            return PODS_FIXTURE, None
        return None, f"unexpected args: {args}"

    published = []

    def fake_publish(topic, payload):
        published.append((topic, payload))

    monkeypatch.setattr(metrics, "_run_kubectl", fake_run)
    monkeypatch.setattr(metrics, "_collect_live_gauges", lambda *a, **k: [])
    monkeypatch.setattr(metrics.events, "publish_sync", fake_publish)

    with SessionLocal() as db:
        env = _make_env(db, "metrics-ok-env")
        settings = _settings(metrics_enabled=True)
        # 3 nodes * 2 + 3 pods * 2 = 12 samples
        assert metrics.collect_for_environment(db, env, settings) == 12

        rows = (
            db.execute(
                select(MetricSample).where(MetricSample.environment_id == env.id)
            )
            .scalars()
            .all()
        )
        assert len(rows) == 12
        sample = next(
            r
            for r in rows
            if r.name == "pod.cpu.cores" and r.labels["pod"] == "web-abc"
        )
        assert sample.value == 0.125
        assert sample.labels == {"ns": "default", "pod": "web-abc"}
        node_sample = next(
            r
            for r in rows
            if r.name == "node.memory.bytes" and r.labels["node"] == "node-2"
        )
        assert node_sample.value == float(1024**3)

    assert published == [
        ("metrics", {"type": "metrics", "environment_id": env.id, "samples": 12})
    ]


def test_prune_deletes_only_old_rows():
    with SessionLocal() as db:
        env = _make_env(db, "metrics-prune-env")
        now = datetime.now(timezone.utc)
        old = MetricSample(
            environment_id=env.id,
            ts=now - timedelta(hours=100),
            name="node.cpu.cores",
            labels={"node": "n1"},
            value=1.0,
        )
        fresh = MetricSample(
            environment_id=env.id,
            ts=now - timedelta(hours=1),
            name="node.cpu.cores",
            labels={"node": "n1"},
            value=2.0,
        )
        db.add_all([old, fresh])
        db.commit()
        fresh_id = fresh.id

        settings = _settings(metrics_retention_hours=72)
        assert metrics.prune_old_metrics(db, settings) == 1

        remaining = (
            db.execute(
                select(MetricSample).where(MetricSample.environment_id == env.id)
            )
            .scalars()
            .all()
        )
        assert [r.id for r in remaining] == [fresh_id]


def test_series_bucketing_math():
    with SessionLocal() as db:
        env = _make_env(db, "metrics-series-env")
        # Fixed timestamps inside the default 24h window.
        base = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        rows = [
            # bucket A: base+5min and base+20min (same 30-min bucket)
            MetricSample(
                environment_id=env.id,
                ts=base + timedelta(minutes=5),
                name="pod.cpu.cores",
                labels={},
                value=10.0,
            ),
            MetricSample(
                environment_id=env.id,
                ts=base + timedelta(minutes=20),
                name="pod.cpu.cores",
                labels={},
                value=20.0,
            ),
            # bucket B: base+40min (next bucket)
            MetricSample(
                environment_id=env.id,
                ts=base + timedelta(minutes=40),
                name="pod.cpu.cores",
                labels={},
                value=7.0,
            ),
            # different metric name: must be excluded
            MetricSample(
                environment_id=env.id,
                ts=base + timedelta(minutes=5),
                name="pod.memory.bytes",
                labels={},
                value=99.0,
            ),
        ]
        db.add_all(rows)
        db.commit()

        series = metrics.series_for_environment(
            db, env.id, "pod.cpu.cores", hours=24, bucket_minutes=30
        )
        assert len(series) == 2
        first, second = series
        assert first["count"] == 2
        assert first["avg"] == 15.0
        assert first["min"] == 10.0
        assert first["max"] == 20.0
        assert second["count"] == 1
        assert second["avg"] == 7.0
        assert second["min"] == 7.0
        assert second["max"] == 7.0
        # Bucket starts align to 30-minute boundaries and are ISO strings.
        start_a = datetime.fromisoformat(first["bucket_start_iso"])
        start_b = datetime.fromisoformat(second["bucket_start_iso"])
        assert start_a.minute in (0, 30) and start_a.second == 0
        assert (start_b - start_a) == timedelta(minutes=30)
        assert start_a <= base + timedelta(minutes=5) < start_a + timedelta(minutes=30)
