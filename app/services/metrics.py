"""Cluster metrics collection via ``kubectl top`` plus live-API gauges.

The collector calls :func:`collect_for_environment` after each snapshot,
inside try/except; it must never raise. ``kubectl top`` failures
(metrics-server absent, kubectl missing, timeouts) skip those samples and
collection continues with gauges from cluster / Talos / cloud / jobs /
alerts. Disabled collection is a no-op (0 samples).

:func:`prune_old_metrics` deletes samples older than
``settings.metrics_retention_hours``; wire it into the same hourly pruning
pass used for snapshots (safe to call on any cadence).

:func:`series_for_environment` downsamples stored samples into fixed time
buckets for chart endpoints, computed in Python to stay SQLite-friendly.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import Environment, MetricSample
from app.services import events
from app.services.envcontext import build_context

log = logging.getLogger(__name__)

_MEM_UNITS = {
    "Ki": 1024,
    "Mi": 1024**2,
    "Gi": 1024**3,
    "Ti": 1024**4,
}


def _parse_cpu_cores(raw: str) -> float | None:
    """Parse kubectl CPU quantity: '250m' -> 0.25, '2' -> 2.0."""
    raw = raw.strip()
    if not raw:
        return None
    try:
        if raw.endswith("m"):
            return float(raw[:-1]) / 1000.0
        return float(raw)
    except ValueError:
        return None


def _parse_memory_bytes(raw: str) -> float | None:
    """Parse kubectl memory quantity: '1024Mi' -> 1073741824.0 bytes."""
    raw = raw.strip()
    if not raw:
        return None
    for suffix, multiplier in _MEM_UNITS.items():
        if raw.endswith(suffix):
            try:
                return float(raw[: -len(suffix)]) * multiplier
            except ValueError:
                return None
    try:
        return float(raw)
    except ValueError:
        return None


def parse_top_nodes(text: str) -> list[dict[str, Any]]:
    """Parse 'kubectl top nodes --no-headers' output into sample dicts.

    Columns: NAME CPU(cores) CPU% MEMORY(bytes) MEMORY%
    """
    samples: list[dict[str, Any]] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 5:
            continue
        labels = {"node": parts[0]}
        cpu = _parse_cpu_cores(parts[1])
        if cpu is not None:
            samples.append({"name": "node.cpu.cores", "labels": labels, "value": cpu})
        mem = _parse_memory_bytes(parts[3])
        if mem is not None:
            samples.append(
                {"name": "node.memory.bytes", "labels": labels, "value": mem}
            )
    return samples


def parse_top_pods(text: str) -> list[dict[str, Any]]:
    """Parse 'kubectl top pods -A --no-headers' output into sample dicts.

    Columns: NAMESPACE NAME CPU(cores) MEMORY(bytes) [NODE]
    """
    samples: list[dict[str, Any]] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        labels = {"ns": parts[0], "pod": parts[1]}
        if len(parts) >= 5:
            labels["node"] = parts[4]
        cpu = _parse_cpu_cores(parts[2])
        if cpu is not None:
            samples.append({"name": "pod.cpu.cores", "labels": labels, "value": cpu})
        mem = _parse_memory_bytes(parts[3])
        if mem is not None:
            samples.append({"name": "pod.memory.bytes", "labels": labels, "value": mem})
    return samples


def _run_kubectl(
    args: list[str], *, env: dict[str, str] | None, timeout: int
) -> tuple[str | None, str | None]:
    """Run a kubectl command. Returns (stdout, error) — never raises."""
    kubectl = shutil.which("kubectl")
    if not kubectl:
        return None, "kubectl not found on PATH"
    try:
        proc = subprocess.run(
            [kubectl, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            check=False,
        )
    except FileNotFoundError:
        return None, "executable not found: kubectl"
    except subprocess.TimeoutExpired:
        return None, f"timed out after {timeout}s"
    except OSError as exc:
        return None, str(exc)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        return None, detail[:200] or f"exit code {proc.returncode}"
    return proc.stdout or "", None


def gauges_from_live(live: dict[str, Any]) -> list[dict[str, Any]]:
    """Turn an observe live-tile payload into gauge samples (dots in names)."""
    k8s = live.get("kubernetes") if isinstance(live.get("kubernetes"), dict) else {}
    talos = live.get("talos") if isinstance(live.get("talos"), dict) else {}
    cloud = live.get("openstack") if isinstance(live.get("openstack"), dict) else {}
    jobs = live.get("jobs") if isinstance(live.get("jobs"), dict) else {}
    alerts = live.get("alerts") if isinstance(live.get("alerts"), dict) else {}
    active = cloud.get("servers_active")
    if active is None:
        active = cloud.get("servers")
    pairs = (
        ("cluster.nodes.ready", k8s.get("ready")),
        ("cluster.nodes.total", k8s.get("nodes")),
        ("cluster.pods.running", k8s.get("pods")),
        ("talos.nodes.reachable", talos.get("reachable")),
        ("talos.nodes.total", talos.get("machines")),
        ("cloud.servers.active", active),
        ("cloud.volumes", cloud.get("volumes")),
        ("jobs.running", jobs.get("running")),
        ("jobs.failed", jobs.get("failed")),
        ("alerts.firing", alerts.get("firing")),
    )
    samples: list[dict[str, Any]] = []
    for name, value in pairs:
        if value is None:
            continue
        try:
            samples.append({"name": name, "labels": {}, "value": float(value)})
        except (TypeError, ValueError):
            continue
    return samples


def _collect_live_gauges(
    db: Session, env: Environment, settings: Settings
) -> list[dict[str, Any]]:
    """Gauge samples from live APIs. Never raises — a failed probe is skipped."""
    try:
        from app.services import observe

        live = observe.probe_live(db, env, settings)
        return gauges_from_live(live)
    except Exception as exc:  # noqa: BLE001
        log.info("metrics: live gauges skipped for env %s: %s", env.id, exc)
        return []


def collect_for_environment(db: Session, env: Environment, settings: Settings) -> int:
    """Collect kubectl top usage plus live-API gauges; returns samples written.

    No-op (returns 0) when settings.metrics_enabled is False. ``kubectl top``
    failures skip those samples and collection continues with gauges. Never
    raises.
    """
    if not settings.metrics_enabled:
        return 0

    parsed: list[dict[str, Any]] = []
    ctx = build_context(env, settings)
    try:
        subprocess_env = ctx.subprocess_env()
        timeout = settings.collector_probe_timeout_seconds
        raw_nodes, err = _run_kubectl(
            ["top", "nodes", "--no-headers"], env=subprocess_env, timeout=timeout
        )
        if err:
            log.info("metrics: kubectl top nodes failed for env %s: %s", env.id, err)
        else:
            parsed.extend(parse_top_nodes(raw_nodes or ""))
        raw_pods, err = _run_kubectl(
            ["top", "pods", "-A", "--no-headers"], env=subprocess_env, timeout=timeout
        )
        if err:
            log.info("metrics: kubectl top pods failed for env %s: %s", env.id, err)
        else:
            parsed.extend(parse_top_pods(raw_pods or ""))
    finally:
        ctx.cleanup()

    parsed.extend(_collect_live_gauges(db, env, settings))
    if not parsed:
        return 0

    ts = datetime.now(timezone.utc)
    rows = [
        MetricSample(
            environment_id=env.id,
            ts=ts,
            name=sample["name"],
            labels=sample.get("labels") or {},
            value=sample["value"],
        )
        for sample in parsed
    ]
    db.add_all(rows)
    db.commit()
    count = len(rows)
    events.publish_sync(
        "metrics", {"type": "metrics", "environment_id": env.id, "samples": count}
    )
    return count


def prune_old_metrics(db: Session, settings: Settings) -> int:
    """Delete samples older than settings.metrics_retention_hours."""
    cutoff = datetime.now(timezone.utc) - timedelta(
        hours=settings.metrics_retention_hours
    )
    result = db.execute(
        delete(MetricSample)
        .where(MetricSample.ts < cutoff)
        .execution_options(synchronize_session=False)
    )
    db.commit()
    return result.rowcount or 0


def names_for_environments(
    db: Session,
    env_ids: list[str],
    hours: int,
) -> list[dict[str, Any]]:
    """Distinct metric names with samples in the window: [{name, samples, latest_ts}]."""
    if not env_ids:
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max(1, hours))
    stmt = (
        select(
            MetricSample.name,
            func.count().label("samples"),
            func.max(MetricSample.ts).label("latest_ts"),
        )
        .where(
            MetricSample.environment_id.in_(env_ids),
            MetricSample.ts >= cutoff,
        )
        .group_by(MetricSample.name)
        .order_by(MetricSample.name)
    )
    return [
        {"name": name, "samples": samples, "latest_ts": latest_ts}
        for name, samples, latest_ts in db.execute(stmt).all()
    ]


def names_for_environment(db: Session, env_id: str, hours: int) -> list[dict[str, Any]]:
    """Distinct metric names for one environment in the window."""
    return names_for_environments(db, [env_id], hours)


def _downsample_series(
    db: Session,
    env_ids: list[str],
    name: str,
    hours: int = 24,
    bucket_minutes: int = 30,
) -> list[dict[str, Any]]:
    if not env_ids:
        return []
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    rows = (
        db.execute(
            select(MetricSample)
            .where(
                MetricSample.environment_id.in_(env_ids),
                MetricSample.name == name,
                MetricSample.ts >= since,
            )
            .order_by(MetricSample.ts)
        )
        .scalars()
        .all()
    )
    bucket_seconds = max(1, bucket_minutes) * 60
    buckets: dict[int, list[float]] = {}
    for row in rows:
        ts = row.ts
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        bucket = int(ts.timestamp() // bucket_seconds)
        buckets.setdefault(bucket, []).append(row.value)
    return [
        {
            "bucket_start_iso": datetime.fromtimestamp(
                bucket * bucket_seconds, tz=timezone.utc
            ).isoformat(),
            "avg": sum(values) / len(values),
            "min": min(values),
            "max": max(values),
            "count": len(values),
        }
        for bucket, values in sorted(buckets.items())
    ]


def series_for_environment(
    db: Session,
    env_id: str,
    name: str,
    hours: int = 24,
    bucket_minutes: int = 30,
) -> list[dict[str, Any]]:
    """Downsampled series for charting: [{bucket_start_iso, avg, min, max, count}]."""
    return _downsample_series(db, [env_id], name, hours, bucket_minutes)


def series_for_environments(
    db: Session,
    env_ids: list[str],
    name: str,
    hours: int = 24,
    bucket_minutes: int = 30,
) -> list[dict[str, Any]]:
    """Downsampled series rolled up across environments (same bucket math)."""
    return _downsample_series(db, env_ids, name, hours, bucket_minutes)
