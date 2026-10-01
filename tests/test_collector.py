"""Collector tests: probe parsing, health classification, retention, hooks, runner."""

from __future__ import annotations

import json
import subprocess
import sys
import types
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.db import SessionLocal, init_db
from app.models import ClusterSnapshot, Environment, Job, JobStatus
from app.services import collector, events


@pytest.fixture(autouse=True, scope="module")
def _ensure_tables():
    """Create tables so the file also passes when run standalone."""
    init_db()


@pytest.fixture(autouse=True)
def _stub_hooks(monkeypatch):
    """Default no-op alerts/metrics stubs (built in parallel by other agents)."""
    _install_hook_stubs(monkeypatch)


def _install_hook_stubs(monkeypatch, alerts=None, metrics=None):
    import app.services as services_pkg

    fake_alerts = alerts or types.SimpleNamespace(
        seed_default_rules=lambda db: None,
        evaluate_snapshot=lambda db, env, snapshot: None,
    )
    fake_metrics = metrics or types.SimpleNamespace(
        collect_for_environment=lambda db, env, settings: None,
    )
    monkeypatch.setitem(sys.modules, "app.services.alerts", fake_alerts)
    monkeypatch.setitem(sys.modules, "app.services.metrics", fake_metrics)
    monkeypatch.setattr(services_pkg, "alerts", fake_alerts, raising=False)
    monkeypatch.setattr(services_pkg, "metrics", fake_metrics, raising=False)


# ---------------------------------------------------------------- fixtures


def _node(name, ready=True, roles=("control-plane",), version="v1.30.2"):
    return {
        "metadata": {
            "name": name,
            "labels": {f"node-role.kubernetes.io/{r}": "" for r in roles},
        },
        "status": {
            "conditions": [{"type": "Ready", "status": "True" if ready else "False"}],
            "nodeInfo": {"kubeletVersion": version},
        },
    }


def _pod(
    ns, name, phase="Running", ready=True, restarts=0, node="node-1", waiting=None
):
    container = {"ready": ready, "restartCount": restarts, "state": {}}
    if waiting:
        container["state"] = {"waiting": {"reason": waiting}}
    return {
        "metadata": {"name": name, "namespace": ns},
        "spec": {"nodeName": node},
        "status": {"phase": phase, "containerStatuses": [container]},
    }


HELM_RELEASES = [
    {
        "name": "keystone",
        "namespace": "openstack",
        "revision": "3",
        "status": "deployed",
        "chart": "keystone-1.2.3",
        "app_version": "2024.1",
    }
]


def _patch_run_probe(monkeypatch, responses=None, exc=None):
    """Replace collector._run_probe; responses maps 'nodes'/'pods'/'helm' to (stdout, error)."""

    def fake_run_probe(cmd, *, env=None, timeout=0):  # noqa: ARG001
        if exc is not None:
            raise exc
        if cmd[0] == "helm":
            key = "helm"
        elif "nodes" in cmd:
            key = "nodes"
        elif "pods" in cmd:
            key = "pods"
        else:  # pragma: no cover - guard against unexpected commands
            return None, f"unexpected command: {cmd}"
        return (responses or {}).get(key, (None, f"no fixture for {key}"))

    monkeypatch.setattr(collector, "_run_probe", fake_run_probe)


def _healthy_responses():
    return {
        "nodes": (
            json.dumps(
                {"items": [_node("node-1"), _node("node-2", roles=("worker",))]}
            ),
            None,
        ),
        "pods": (
            json.dumps(
                {
                    "items": [
                        _pod("default", "web-abc"),
                        _pod("openstack", "keystone-xyz", node="node-2"),
                    ]
                }
            ),
            None,
        ),
        "helm": (json.dumps(HELM_RELEASES), None),
    }


def _make_env(
    db, prefix="collector-env", kubeconfig: str | None = "/tmp/fake-kubeconfig"
) -> Environment:
    # Probe tests exercise the kubectl/helm path, so the env needs a
    # kubeconfig (a missing one short-circuits the probe as 'down').
    env = Environment(
        name=f"{prefix}-{uuid.uuid4().hex[:8]}",
        description=f"{prefix} test",
        kubeconfig_path=kubeconfig,
    )
    db.add(env)
    db.commit()
    return env


def _settings():
    from app.config import get_settings

    return get_settings()


# ------------------------------------------------------------------- probes


def test_probe_healthy_cluster(monkeypatch):
    published = []
    monkeypatch.setattr(
        events,
        "publish_sync",
        lambda topic, payload: published.append((topic, payload)),
    )
    _patch_run_probe(monkeypatch, _healthy_responses())

    db = SessionLocal()
    try:
        env = _make_env(db, "healthy")
        snap = collector.probe_environment(db, env, _settings())

        assert snap.probe_ok is True
        assert snap.error is None
        assert snap.health == "healthy"
        assert snap.nodes == [
            {
                "name": "node-1",
                "ready": True,
                "roles": ["control-plane"],
                "kubelet_version": "v1.30.2",
            },
            {
                "name": "node-2",
                "ready": True,
                "roles": ["worker"],
                "kubelet_version": "v1.30.2",
            },
        ]
        assert snap.pods[0] == {
            "ns": "default",
            "name": "web-abc",
            "phase": "Running",
            "ready": True,
            "restarts": 0,
            "node": "node-1",
            "waiting_reason": None,
        }
        assert snap.helm == [
            {
                "name": "keystone",
                "ns": "openstack",
                "status": "deployed",
                "chart": "keystone-1.2.3",
                "version": "2024.1",
            }
        ]
        assert snap.summary == {
            "nodes_ready": 2,
            "nodes_total": 2,
            "pods_running": 2,
            "pods_pending": 0,
            "pods_failed": 0,
            "crashlooping": [],
        }
    finally:
        db.close()

    env_topic, env_payload = next(t for t in published if t[0].startswith("env:"))
    assert env_topic == f"env:{env.id}"
    assert env_payload["type"] == "snapshot"
    assert env_payload["environment_id"] == env.id
    assert env_payload["health"] == "healthy"
    assert env_payload["summary"]["nodes_total"] == 2
    assert env_payload["taken_at"]

    fleet_topic, fleet_payload = next(t for t in published if t[0] == "fleet")
    assert fleet_payload["type"] == "fleet"
    assert fleet_payload["environment_id"] == env.id
    assert fleet_payload["health"] == "healthy"


def test_probe_notready_node_is_degraded(monkeypatch):
    responses = _healthy_responses()
    responses["nodes"] = (
        json.dumps(
            {
                "items": [
                    _node("node-1"),
                    _node("node-2", ready=False, roles=("worker",)),
                ]
            }
        ),
        None,
    )
    _patch_run_probe(monkeypatch, responses)

    db = SessionLocal()
    try:
        env = _make_env(db, "notready")
        snap = collector.probe_environment(db, env, _settings())
        assert snap.probe_ok is True
        assert snap.health == "degraded"
        assert snap.summary["nodes_ready"] == 1
        assert snap.summary["nodes_total"] == 2
        assert snap.nodes[1]["ready"] is False
    finally:
        db.close()


def test_probe_crashloop_pod_is_degraded(monkeypatch):
    responses = _healthy_responses()
    responses["pods"] = (
        json.dumps(
            {
                "items": [
                    _pod("default", "web-abc"),
                    _pod(
                        "openstack",
                        "neutron-bad",
                        waiting="CrashLoopBackOff",
                        ready=False,
                        restarts=12,
                    ),
                    _pod("openstack", "nova-flaky", restarts=7),  # restarts >= 5
                ]
            }
        ),
        None,
    )
    _patch_run_probe(monkeypatch, responses)

    db = SessionLocal()
    try:
        env = _make_env(db, "crashloop")
        snap = collector.probe_environment(db, env, _settings())
        assert snap.probe_ok is True
        assert snap.health == "degraded"
        assert snap.summary["crashlooping"] == [
            "openstack/neutron-bad",
            "openstack/nova-flaky",
        ]
        bad = next(p for p in snap.pods if p["name"] == "neutron-bad")
        assert bad["waiting_reason"] == "CrashLoopBackOff"
        assert bad["restarts"] == 12
        assert bad["ready"] is False
    finally:
        db.close()


def test_probe_failed_pods_is_degraded(monkeypatch):
    responses = _healthy_responses()
    responses["pods"] = (
        json.dumps(
            {
                "items": [
                    _pod("default", "web-abc"),
                    _pod("batch", "job-1", phase="Failed", ready=False),
                ]
            }
        ),
        None,
    )
    _patch_run_probe(monkeypatch, responses)

    db = SessionLocal()
    try:
        env = _make_env(db, "failedpod")
        snap = collector.probe_environment(db, env, _settings())
        assert snap.health == "degraded"
        assert snap.summary["pods_failed"] == 1
    finally:
        db.close()


def test_probe_unreachable_cluster_is_down(monkeypatch):
    _patch_run_probe(
        monkeypatch, exc=subprocess.TimeoutExpired(cmd="kubectl", timeout=15)
    )

    db = SessionLocal()
    try:
        env = _make_env(db, "unreachable")
        snap = collector.probe_environment(db, env, _settings())
        assert snap.probe_ok is False
        assert snap.error
        assert snap.health == "down"
        assert snap.nodes == []
        assert snap.pods == []
        assert snap.helm == []
        assert snap.summary == {
            "nodes_ready": 0,
            "nodes_total": 0,
            "pods_running": 0,
            "pods_pending": 0,
            "pods_failed": 0,
            "crashlooping": [],
        }
        # Persisted, not just returned.
        loaded = db.get(ClusterSnapshot, snap.id)
        assert loaded is not None and loaded.probe_ok is False
    finally:
        db.close()


def test_probe_without_kubeconfig_is_down_without_spawn(monkeypatch):
    # An env with no kubeconfig must not spawn kubectl/helm at all —
    # without KUBECONFIG kubectl defaults to http://127.0.0.1:8080,
    # which inside the container is the console itself.
    def boom(*_a, **_k):  # noqa: ARG001
        raise AssertionError("_run_probe must not be called without a kubeconfig")

    monkeypatch.setattr(collector, "_run_probe", boom)
    db = SessionLocal()
    try:
        env = _make_env(db, "no-kc", kubeconfig=None)
        snap = collector.probe_environment(db, env, _settings())
        assert snap.probe_ok is False
        assert "kubeconfig" in (snap.error or "")
        assert snap.health == "down"
        loaded = db.get(ClusterSnapshot, snap.id)
        assert loaded is not None and loaded.probe_ok is False
    finally:
        db.close()


def test_probe_kubectl_error_is_down(monkeypatch):
    _patch_run_probe(
        monkeypatch,
        {"nodes": (None, "The connection to the server localhost:8080 was refused")},
    )

    db = SessionLocal()
    try:
        env = _make_env(db, "refused")
        snap = collector.probe_environment(db, env, _settings())
        assert snap.probe_ok is False
        assert "refused" in snap.error
        assert snap.health == "down"
    finally:
        db.close()


def test_probe_helm_failure_is_not_a_probe_failure(monkeypatch):
    responses = _healthy_responses()
    responses["helm"] = (None, "executable not found: helm")
    _patch_run_probe(monkeypatch, responses)

    db = SessionLocal()
    try:
        env = _make_env(db, "nohelm")
        snap = collector.probe_environment(db, env, _settings())
        assert snap.probe_ok is True
        assert snap.health == "healthy"
        assert snap.helm == []
    finally:
        db.close()


# ---------------------------------------------------------------- retention


def _seed_snapshot(db, env_id, taken_at):
    snap = ClusterSnapshot(
        environment_id=env_id,
        taken_at=taken_at,
        probe_ok=True,
        nodes=[],
        pods=[],
        helm=[],
        summary={},
        health="healthy",
    )
    db.add(snap)
    db.commit()
    return snap.id


def test_prune_keeps_newest_per_environment():
    settings = _settings()
    old = datetime.now(timezone.utc) - timedelta(
        hours=settings.collector_retention_hours + 24
    )

    db = SessionLocal()
    try:
        env_a = _make_env(db, "prune-a")
        env_b = _make_env(db, "prune-b")
        # Inserted oldest-first so the newest taken_at also has the max id.
        a_oldest = _seed_snapshot(db, env_a.id, old - timedelta(hours=2))
        a_mid = _seed_snapshot(db, env_a.id, old - timedelta(hours=1))
        a_newest = _seed_snapshot(db, env_a.id, old)  # old, but newest for env A
        b_old = _seed_snapshot(db, env_b.id, old)
        b_fresh = _seed_snapshot(db, env_b.id, datetime.now(timezone.utc))

        deleted = collector.prune_old_snapshots(db, settings)
        assert deleted == 3

        remaining = set(
            db.scalars(
                select(ClusterSnapshot.id).where(
                    ClusterSnapshot.environment_id.in_([env_a.id, env_b.id])
                )
            ).all()
        )
        assert remaining == {a_newest, b_fresh}
        assert {a_oldest, a_mid, b_old}.isdisjoint(remaining)

        # Second pass is a no-op: the newest snapshot is never deleted.
        assert collector.prune_old_snapshots(db, settings) == 0
    finally:
        db.close()


# -------------------------------------------------------------------- hooks


def test_alerts_and_metrics_hooks_are_called(monkeypatch):
    calls = {"evaluate": [], "metrics": []}
    _install_hook_stubs(
        monkeypatch,
        alerts=types.SimpleNamespace(
            evaluate_snapshot=lambda db, env, snapshot: calls["evaluate"].append(
                (env.id, snapshot.id)
            ),
        ),
        metrics=types.SimpleNamespace(
            collect_for_environment=lambda db, env, settings: calls["metrics"].append(
                env.id
            ),
        ),
    )
    _patch_run_probe(monkeypatch, _healthy_responses())

    db = SessionLocal()
    try:
        env = _make_env(db, "hooks")
        snap = collector.probe_environment(db, env, _settings())
        assert calls["evaluate"] == [(env.id, snap.id)]
        assert calls["metrics"] == [env.id]
    finally:
        db.close()


def test_hook_exceptions_do_not_break_collection(monkeypatch):
    def _boom(*args, **kwargs):
        raise RuntimeError("boom")

    _install_hook_stubs(
        monkeypatch,
        alerts=types.SimpleNamespace(evaluate_snapshot=_boom),
        metrics=types.SimpleNamespace(collect_for_environment=_boom),
    )
    _patch_run_probe(monkeypatch, _healthy_responses())

    db = SessionLocal()
    try:
        env = _make_env(db, "hookboom")
        snap = collector.probe_environment(db, env, _settings())
        assert snap.probe_ok is True
        assert snap.health == "healthy"
        assert db.get(ClusterSnapshot, snap.id) is not None
    finally:
        db.close()


# ---------------------------------------------------------------- collect_all


def test_collect_all_probes_every_environment(monkeypatch):
    _patch_run_probe(monkeypatch, _healthy_responses())

    db = SessionLocal()
    try:
        env_ids = {_make_env(db, "fleet-a").id, _make_env(db, "fleet-b").id}
    finally:
        db.close()

    executor = ThreadPoolExecutor(max_workers=2)
    try:
        result = collector.collect_all(SessionLocal, _settings(), executor)
    finally:
        executor.shutdown(wait=True)

    assert result["errors"] == []
    assert result["probed"] >= len(env_ids)
    assert result["healthy"] >= len(env_ids)

    db = SessionLocal()
    try:
        for env_id in env_ids:
            snaps = list(
                db.scalars(
                    select(ClusterSnapshot).where(
                        ClusterSnapshot.environment_id == env_id
                    )
                ).all()
            )
            assert snaps and all(s.health == "healthy" for s in snaps)
    finally:
        db.close()


# -------------------------------------------------------------------- runner


def _seed_job(*, operation="internal.health", status="queued", environment_id=None):
    db = SessionLocal()
    try:
        job = Job(
            environment_id=environment_id,
            operation=operation,
            params={},
            status=JobStatus(status),
            log_text="",
            created_by="collector-test",
        )
        db.add(job)
        db.commit()
        return job.id
    finally:
        db.close()


def _get_job_status(job_id: str) -> str:
    db = SessionLocal()
    try:
        return db.get(Job, job_id).status.value
    finally:
        db.close()


def test_no_collector_daemon_still_drains_jobs(monkeypatch):
    """--no-collector: no scheduler is built, but the job loop runs unchanged."""
    from app.worker import runner

    built = []

    class _NoScheduler:
        def __init__(self, *args, **kwargs):
            built.append(True)

    monkeypatch.setattr(runner, "CollectorScheduler", _NoScheduler)
    job_id = _seed_job()

    rc = runner.run_daemon(interval=0, limit=10, collector_enabled=False, max_ticks=1)

    assert rc == 0
    assert built == []
    assert _get_job_status(job_id) == "success"


def test_scheduler_probes_due_envs_once_and_tracks_in_flight(monkeypatch):
    from app.worker import runner

    probed = []

    def fake_probe(db, env, settings):  # noqa: ARG001
        probed.append(env.id)
        return types.SimpleNamespace(health="healthy")

    monkeypatch.setattr(collector, "probe_environment", fake_probe)

    db = SessionLocal()
    try:
        env_id = _make_env(db, "due").id
    finally:
        db.close()

    scheduler = runner.CollectorScheduler(_settings())
    scheduler.tick()
    for future in list(scheduler._in_flight.values()):
        future.result(timeout=10)
    assert env_id in probed

    # Interval (60s) has not elapsed: the env is not re-probed.
    scheduler.tick()
    for future in list(scheduler._in_flight.values()):
        future.result(timeout=10)
    assert probed.count(env_id) == 1


def test_scheduler_startup_seeds_default_rules(monkeypatch):
    from app.worker import runner

    seeded = []
    _install_hook_stubs(
        monkeypatch,
        alerts=types.SimpleNamespace(seed_default_rules=lambda db: seeded.append(True)),
    )
    runner.CollectorScheduler(_settings()).startup()
    assert seeded == [True]


def test_scheduler_startup_tolerates_broken_alerts(monkeypatch):
    from app.worker import runner

    def _boom(db):  # noqa: ARG001
        raise RuntimeError("alerts module broken")

    _install_hook_stubs(
        monkeypatch, alerts=types.SimpleNamespace(seed_default_rules=_boom)
    )
    runner.CollectorScheduler(_settings()).startup()  # must not raise


def test_job_status_events_are_published(monkeypatch):
    from app.worker import runner

    published = []
    monkeypatch.setattr(
        events,
        "publish_sync",
        lambda topic, payload: published.append((topic, payload)),
    )

    job_id = _seed_job()
    assert runner.process_queued_jobs(once=True) >= 1

    job_events = [p for t, p in published if t == "jobs" and p["id"] == job_id]
    statuses = [p["status"] for p in job_events]
    assert "running" in statuses
    assert statuses[-1] == "success"
    assert all(
        p["type"] == "job" and p["operation"] == "internal.health" for p in job_events
    )
