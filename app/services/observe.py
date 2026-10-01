"""Observe dashboards: plane stats, time series, and live "now" counts.

``probe_live`` / ``live_tile`` stay the collector contract (metrics gauges).
The HTTP payloads add ``plane`` / ``now`` / ``{t,v}`` series so the Console
Observe tab can render without an iframe. Live probes never raise; a failed
source degrades to snapshot/DB zeros so the rest of the bundle still returns
200. Fleet Observe is snapshot + jobs + alerts — never N kubectl calls.
"""

from __future__ import annotations

__all__ = [
    "observe_environment",
    "observe_fleet",
    "observe_logs",
    "probe_live",
    "live_tile",
]

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models import AlertEvent, ClusterSnapshot, Environment, Job, JobStatus
from app.schemas import Principal
from app.services import livestate, openstack_ops, platform
from app.services.envcontext import build_context
from app.services.fleet import _visible_environments
from app.services.metrics import (
    names_for_environment,
    names_for_environments,
    series_for_environment,
    series_for_environments,
)

PREFERRED_SERIES = (
    "node.cpu.cores",
    "node.memory.bytes",
    "pod.cpu.cores",
    "cluster.nodes.ready",
    "cloud.servers.active",
)

_EMPTY_TALOS = {"machines": 0, "reachable": 0, "versions": []}
_EMPTY_K8S = {"health": "unknown", "nodes": 0, "ready": 0, "pods": 0}
_EMPTY_CLOUD = {
    "available": False,
    "servers": 0,
    "servers_active": 0,
    "volumes": 0,
    "networks": 0,
}
_EMPTY_JOBS = {"running": 0, "failed": 0}
_EMPTY_ALERTS = {"firing": 0}
_EMPTY_NOW = {
    "problem_pods": 0,
    "helm_failed": 0,
    "volume_errors": 0,
    "problems": [],
}
_HELM_OK = frozenset({"deployed", ""})
_VOLUME_ERROR = frozenset(
    {"error", "error_deleting", "error_extending", "error_restoring"}
)
_ACTIVE_JOBS = (JobStatus.queued, JobStatus.running)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def bucket_minutes_for(hours: int) -> int:
    if hours <= 1:
        return 5
    if hours <= 6:
        return 15
    if hours <= 24:
        return 30
    return 180


def _node_ready(node: dict[str, Any]) -> bool:
    return str(node.get("status") or "").lower().replace(" ", "") == "ready"


def _server_active(row: dict[str, Any]) -> bool:
    return str(row.get("status") or "").upper() == "ACTIVE"


def _talos_from_platform(overview: dict[str, Any]) -> dict[str, Any]:
    cluster = (
        overview.get("cluster") if isinstance(overview.get("cluster"), dict) else {}
    )
    versions = cluster.get("talos_versions") or []
    if not isinstance(versions, list):
        versions = []
    return {
        "machines": _int(cluster.get("machines")),
        "reachable": _int(cluster.get("talos_reachable")),
        "versions": [str(v) for v in versions if v],
    }


def _k8s_from_cluster(overview: dict[str, Any]) -> dict[str, Any]:
    nodes = overview.get("nodes") if isinstance(overview.get("nodes"), list) else []
    ready = sum(1 for n in nodes if isinstance(n, dict) and _node_ready(n))
    pods = overview.get("pods") if isinstance(overview.get("pods"), dict) else {}
    return {
        "health": str(overview.get("health") or "unknown"),
        "nodes": len(nodes),
        "ready": ready,
        "pods": _int(pods.get("running")),
    }


def _cloud_from_inventory(inv: dict[str, Any]) -> dict[str, Any]:
    servers = inv.get("servers") if isinstance(inv.get("servers"), list) else []
    volumes = inv.get("volumes") if isinstance(inv.get("volumes"), list) else []
    networks = inv.get("networks") if isinstance(inv.get("networks"), list) else []
    active = sum(1 for s in servers if isinstance(s, dict) and _server_active(s))
    return {
        "available": bool(inv.get("available")),
        "servers": len(servers),
        "servers_active": active,
        "volumes": len(volumes),
        "networks": len(networks),
    }


def _now_from_cluster(overview: dict[str, Any]) -> dict[str, Any]:
    pods = overview.get("pods") if isinstance(overview.get("pods"), dict) else {}
    problems = pods.get("problems") if isinstance(pods.get("problems"), list) else []
    releases = (
        overview.get("releases") if isinstance(overview.get("releases"), list) else []
    )
    helm_failed = sum(
        1
        for rel in releases
        if isinstance(rel, dict)
        and str(rel.get("status") or "").lower() not in _HELM_OK
    )
    return {
        "problem_pods": len(problems),
        "problems": [p for p in problems[:12] if isinstance(p, dict)],
        "helm_failed": helm_failed,
        "volume_errors": 0,
    }


def _volume_errors(inv: dict[str, Any]) -> int:
    volumes = inv.get("volumes") if isinstance(inv.get("volumes"), list) else []
    n = 0
    for vol in volumes:
        if not isinstance(vol, dict):
            continue
        if str(vol.get("status") or "").lower() in _VOLUME_ERROR:
            n += 1
    return n


def _jobs_for_env(db: Session, env_id: str) -> dict[str, int]:
    running = db.scalar(
        select(func.count())
        .select_from(Job)
        .where(Job.environment_id == env_id, Job.status.in_(_ACTIVE_JOBS))
    )
    failed = db.scalar(
        select(func.count())
        .select_from(Job)
        .where(Job.environment_id == env_id, Job.status == JobStatus.failed)
    )
    return {"running": int(running or 0), "failed": int(failed or 0)}


def _alerts_for_env(db: Session, env_id: str) -> dict[str, int]:
    firing = db.scalar(
        select(func.count())
        .select_from(AlertEvent)
        .where(AlertEvent.environment_id == env_id, AlertEvent.status == "firing")
    )
    return {"firing": int(firing or 0)}


def probe_live(db: Session, env: Environment, settings: Settings) -> dict[str, Any]:
    """Live counts for one environment. Never raises; missing sources are zeros."""
    live: dict[str, Any] = {
        "talos": dict(_EMPTY_TALOS),
        "kubernetes": dict(_EMPTY_K8S),
        "openstack": dict(_EMPTY_CLOUD),
        "jobs": dict(_EMPTY_JOBS),
        "alerts": dict(_EMPTY_ALERTS),
        "now": dict(_EMPTY_NOW),
    }
    try:
        live["talos"] = _talos_from_platform(
            platform.platform_overview(env, settings, db)
        )
    except Exception:  # noqa: BLE001
        pass
    kube = False
    try:
        ctx = build_context(env, settings)
        try:
            kube = bool(ctx.kubeconfig)
            cluster = livestate.cluster_overview(ctx)
            live["kubernetes"] = _k8s_from_cluster(cluster)
            live["now"] = _now_from_cluster(cluster)
        finally:
            ctx.cleanup()
    except Exception:  # noqa: BLE001
        pass
    if kube:
        try:
            inv = openstack_ops.cloud_inventory(env, settings)
            live["openstack"] = _cloud_from_inventory(inv)
            now = (
                live.get("now")
                if isinstance(live.get("now"), dict)
                else dict(_EMPTY_NOW)
            )
            now["volume_errors"] = _volume_errors(inv)
            live["now"] = now
        except Exception:  # noqa: BLE001
            pass
    try:
        live["jobs"] = _jobs_for_env(db, env.id)
    except Exception:  # noqa: BLE001
        pass
    try:
        live["alerts"] = _alerts_for_env(db, env.id)
    except Exception:  # noqa: BLE001
        pass
    return live


def live_tile(live: dict[str, Any]) -> dict[str, Any]:
    """Public live-tile shape (no internal gauge-only fields)."""
    cloud = live.get("openstack") if isinstance(live.get("openstack"), dict) else {}
    return {
        "talos": live.get("talos") or dict(_EMPTY_TALOS),
        "kubernetes": live.get("kubernetes") or dict(_EMPTY_K8S),
        "openstack": {
            "available": bool(cloud.get("available")),
            "servers": _int(cloud.get("servers")),
            "volumes": _int(cloud.get("volumes")),
            "networks": _int(cloud.get("networks")),
        },
        "jobs": live.get("jobs") or dict(_EMPTY_JOBS),
        "alerts": live.get("alerts") or dict(_EMPTY_ALERTS),
    }


def _plane_from_live(live: dict[str, Any]) -> dict[str, Any]:
    talos = live.get("talos") if isinstance(live.get("talos"), dict) else {}
    k8s = live.get("kubernetes") if isinstance(live.get("kubernetes"), dict) else {}
    cloud = live.get("openstack") if isinstance(live.get("openstack"), dict) else {}
    jobs = live.get("jobs") if isinstance(live.get("jobs"), dict) else {}
    alerts = live.get("alerts") if isinstance(live.get("alerts"), dict) else {}
    instances = cloud.get("servers_active")
    if instances is None:
        instances = cloud.get("servers")
    return {
        "talos": {
            "reachable": talos.get("reachable"),
            "machines": _int(talos.get("machines")),
        },
        "kubernetes": {
            "ready": _int(k8s.get("ready")),
            "nodes": _int(k8s.get("nodes")),
        },
        "openstack": {"instances": _int(instances)},
        "jobs": {"running": _int(jobs.get("running"))},
        "alerts": {"firing": _int(alerts.get("firing"))},
    }


def _latest_snapshot(db: Session, env_id: str) -> ClusterSnapshot | None:
    return db.scalar(
        select(ClusterSnapshot)
        .where(ClusterSnapshot.environment_id == env_id)
        .order_by(ClusterSnapshot.taken_at.desc())
        .limit(1)
    )


def _latest_snapshots(db: Session, env_ids: list[str]) -> dict[str, ClusterSnapshot]:
    if not env_ids:
        return {}
    latest = (
        select(
            ClusterSnapshot.environment_id.label("environment_id"),
            func.max(ClusterSnapshot.taken_at).label("max_taken_at"),
        )
        .where(ClusterSnapshot.environment_id.in_(env_ids))
        .group_by(ClusterSnapshot.environment_id)
        .subquery()
    )
    stmt = select(ClusterSnapshot).join(
        latest,
        (ClusterSnapshot.environment_id == latest.c.environment_id)
        & (ClusterSnapshot.taken_at == latest.c.max_taken_at),
    )
    return {s.environment_id: s for s in db.scalars(stmt).all()}


def _pod_problems_from_snap(snap: ClusterSnapshot) -> list[dict[str, Any]]:
    problems: list[dict[str, Any]] = []
    for pod in snap.pods or []:
        if not isinstance(pod, dict):
            continue
        phase = str(pod.get("phase") or "Unknown")
        if phase == "Succeeded":
            continue
        waiting = pod.get("waiting_reason")
        ready = pod.get("ready")
        if phase == "Running" and ready and not waiting:
            continue
        problems.append(
            {
                "namespace": pod.get("ns") or pod.get("namespace"),
                "name": pod.get("name"),
                "status": phase,
                "reason": waiting or phase,
            }
        )
        if len(problems) >= 12:
            break
    if problems:
        return problems
    crash = (snap.summary or {}).get("crashlooping") or []
    out: list[dict[str, Any]] = []
    for item in crash[:12]:
        text = str(item)
        ns, _, name = text.partition("/")
        out.append(
            {
                "namespace": ns if name else None,
                "name": name or text,
                "status": "CrashLoopBackOff",
                "reason": "CrashLoopBackOff",
            }
        )
    return out


def _overlay_snapshot(
    plane: dict[str, Any], now: dict[str, Any], snap: ClusterSnapshot | None
) -> str:
    """Fill zeros from the latest snapshot. Returns snapshot health."""
    if snap is None:
        return "unknown"
    summary = snap.summary if isinstance(snap.summary, dict) else {}
    ready = summary.get("nodes_ready")
    total = summary.get("nodes_total")
    if ready is None or total is None:
        nodes = [n for n in (snap.nodes or []) if isinstance(n, dict)]
        total = len(nodes)
        ready = sum(1 for n in nodes if n.get("ready") is True)
    ready_n = _int(ready)
    total_n = _int(total)
    if not plane["kubernetes"]["nodes"] and total_n:
        plane["kubernetes"]["ready"] = ready_n
        plane["kubernetes"]["nodes"] = total_n
    if not plane["talos"]["machines"] and total_n:
        plane["talos"]["machines"] = total_n
    if not now.get("problem_pods"):
        problems = _pod_problems_from_snap(snap)
        now["problems"] = problems
        now["problem_pods"] = len(problems)
        if not problems:
            now["problem_pods"] = _int(summary.get("pods_failed")) + len(
                summary.get("crashlooping") or []
            )
    if not now.get("helm_failed"):
        now["helm_failed"] = sum(
            1
            for rel in (snap.helm or [])
            if isinstance(rel, dict)
            and str(rel.get("status") or "").lower() not in _HELM_OK
        )
    return str(snap.health or "unknown")


def _points(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        ts = row.get("t") or row.get("bucket_start_iso")
        val = row.get("v") if row.get("v") is not None else row.get("avg")
        if ts is None or val is None:
            continue
        try:
            point = {"t": str(ts), "v": float(val)}
        except (TypeError, ValueError):
            continue
        point.update(row)
        out.append(point)
    return out


def _ready_series_from_snapshots(
    db: Session, env_id: str, hours: int, bucket_minutes: int
) -> list[dict[str, Any]]:
    cutoff = _utcnow() - timedelta(hours=hours)
    snaps = list(
        db.scalars(
            select(ClusterSnapshot)
            .where(
                ClusterSnapshot.environment_id == env_id,
                ClusterSnapshot.taken_at >= cutoff,
            )
            .order_by(ClusterSnapshot.taken_at)
        ).all()
    )
    if not snaps:
        return []
    bucket_seconds = max(1, bucket_minutes) * 60
    buckets: dict[int, list[float]] = {}
    for snap in snaps:
        ts = snap.taken_at
        if ts is None:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        summary = snap.summary if isinstance(snap.summary, dict) else {}
        ready = summary.get("nodes_ready")
        if ready is None:
            ready = sum(
                1 for n in (snap.nodes or []) if isinstance(n, dict) and n.get("ready")
            )
        bucket = int(ts.timestamp() // bucket_seconds)
        buckets.setdefault(bucket, []).append(float(ready or 0))
    return [
        {
            "t": datetime.fromtimestamp(
                bucket * bucket_seconds, tz=timezone.utc
            ).isoformat(),
            "v": sum(values) / len(values),
            "bucket_start_iso": datetime.fromtimestamp(
                bucket * bucket_seconds, tz=timezone.utc
            ).isoformat(),
            "avg": sum(values) / len(values),
            "count": len(values),
        }
        for bucket, values in sorted(buckets.items())
    ]


def _series_map(
    db: Session, env_id: str, hours: int
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    bucket = bucket_minutes_for(hours)
    names = names_for_environment(db, env_id, hours)
    have = {row["name"] for row in names if row.get("name")}
    series: dict[str, Any] = {}
    for row in names:
        name = row.get("name")
        if not name:
            continue
        series[name] = _points(
            series_for_environment(db, env_id, name, hours=hours, bucket_minutes=bucket)
        )
    for name in PREFERRED_SERIES:
        if name not in series:
            series[name] = []
    if not series.get("cluster.nodes.ready"):
        series["cluster.nodes.ready"] = _ready_series_from_snapshots(
            db, env_id, hours, bucket
        )
        if series["cluster.nodes.ready"] and "cluster.nodes.ready" not in have:
            names = list(names) + [
                {
                    "name": "cluster.nodes.ready",
                    "samples": len(series["cluster.nodes.ready"]),
                    "latest_ts": series["cluster.nodes.ready"][-1]["t"],
                }
            ]
    return names, series


def observe_environment(
    db: Session,
    env: Environment,
    settings: Settings | None = None,
    *,
    hours: int = 24,
) -> dict[str, Any]:
    """Dashboard bundle for one environment (viewer)."""
    settings = settings or get_settings()
    hours = max(1, min(168, int(hours or 24)))
    live = probe_live(db, env, settings)
    plane = _plane_from_live(live)
    now = live.get("now") if isinstance(live.get("now"), dict) else dict(_EMPTY_NOW)
    now = {
        "problem_pods": _int(now.get("problem_pods")),
        "helm_failed": _int(now.get("helm_failed")),
        "volume_errors": _int(now.get("volume_errors")),
        "problems": list(now.get("problems") or []),
    }
    snap = _latest_snapshot(db, env.id)
    health = _overlay_snapshot(plane, now, snap)
    # DB jobs/alerts win over a patched or empty live probe.
    jobs = _jobs_for_env(db, env.id)
    alerts = _alerts_for_env(db, env.id)
    plane["jobs"]["running"] = jobs["running"]
    plane["alerts"]["firing"] = alerts["firing"]
    live["jobs"] = jobs
    live["alerts"] = alerts
    names, series = _series_map(db, env.id, hours)
    live_pub = live_tile(live)
    live_pub["talos"] = {
        "machines": plane["talos"]["machines"] or live_pub["talos"]["machines"],
        "reachable": (
            plane["talos"]["reachable"]
            if plane["talos"]["reachable"] is not None
            else live_pub["talos"]["reachable"]
        ),
        "versions": live_pub["talos"].get("versions") or [],
    }
    pods = live_pub["kubernetes"].get("pods") or 0
    if not pods and snap is not None:
        summary = snap.summary if isinstance(snap.summary, dict) else {}
        pods = _int(summary.get("pods_running"))
    k8s_health = live_pub["kubernetes"].get("health") or "unknown"
    if k8s_health == "unknown" and health != "unknown":
        k8s_health = health
    live_pub["kubernetes"] = {
        "health": k8s_health,
        "nodes": plane["kubernetes"]["nodes"] or live_pub["kubernetes"]["nodes"],
        "ready": plane["kubernetes"]["ready"] or live_pub["kubernetes"]["ready"],
        "pods": pods,
    }
    live_pub["jobs"] = jobs
    live_pub["alerts"] = alerts
    return {
        "environment_id": env.id,
        "name": env.name,
        "generated_at": _utcnow(),
        "hours": hours,
        "health": health,
        "metrics_enabled": bool(settings.metrics_enabled),
        "live": live_pub,
        "plane": plane,
        "now": now,
        "series": series,
        "names": names,
    }


def observe_fleet(
    db: Session,
    principal: Principal,
    settings: Settings | None = None,
    *,
    hours: int = 24,
) -> dict[str, Any]:
    """Per-environment plane tiles from snapshots + jobs + alerts (viewer)."""
    settings = settings or get_settings()
    hours = max(1, min(168, int(hours or 24)))
    from app.models import Tenant

    tenant_names = {t.id: t.name for t in db.scalars(select(Tenant)).all()}
    envs = _visible_environments(db, principal)
    env_ids = [env.id for env in envs]
    snaps = _latest_snapshots(db, env_ids)
    tiles: list[dict[str, Any]] = []
    for env in envs:
        snap = snaps.get(env.id)
        jobs = _jobs_for_env(db, env.id)
        alerts = _alerts_for_env(db, env.id)
        plane = {
            "talos": {"reachable": None, "machines": 0},
            "kubernetes": {"ready": 0, "nodes": 0},
            "openstack": {"instances": 0},
            "jobs": {"running": jobs["running"]},
            "alerts": {"firing": alerts["firing"]},
        }
        now = dict(_EMPTY_NOW)
        health = _overlay_snapshot(plane, now, snap)
        live = {
            "talos": {
                "machines": plane["talos"]["machines"],
                "reachable": plane["talos"]["reachable"] or 0,
                "versions": [],
            },
            "kubernetes": {
                "health": health,
                "nodes": plane["kubernetes"]["nodes"],
                "ready": plane["kubernetes"]["ready"],
                "pods": 0,
            },
            "openstack": {
                "available": False,
                "servers": plane["openstack"]["instances"],
                "volumes": 0,
                "networks": 0,
            },
            "jobs": jobs,
            "alerts": alerts,
        }
        tiles.append(
            {
                "id": env.id,
                "environment_id": env.id,
                "name": env.name,
                "region": env.region,
                "tier": env.tier,
                "tenant_id": env.tenant_id,
                "tenant_name": (
                    tenant_names.get(env.tenant_id) if env.tenant_id else None
                ),
                "health": health,
                "live": live_tile(live),
                "plane": plane,
                "now": now,
            }
        )
    names = names_for_environments(db, env_ids, hours)
    bucket = bucket_minutes_for(hours)
    series = {
        row["name"]: _points(
            series_for_environments(
                db, env_ids, row["name"], hours=hours, bucket_minutes=bucket
            )
        )
        for row in names
        if row.get("name")
    }
    return {
        "generated_at": _utcnow(),
        "hours": hours,
        "metrics_enabled": bool(settings.metrics_enabled),
        "environments": tiles,
        "series": series,
        "names": names,
    }


def observe_logs(
    env: Environment,
    settings: Settings,
    *,
    query: str | None = None,
    namespace: str | None = None,
    pod: str | None = None,
    since: str = "15m",
    limit: int = 200,
) -> dict[str, Any]:
    """Read Loki through this environment's Kubernetes API service proxy.

    Uses the monitoring/loki-gateway service installed by Genestack and used
    by its Grafana/OTel defaults. Never falls back to another cluster, public
    URL or pod logs. Missing service/RBAC is an explicit unavailable result.
    """
    import json
    import re
    import shutil
    from urllib.parse import urlencode

    from app.services.cluster import _kube_env
    from app.services.logredact import redact_secret_line

    result: dict[str, Any] = {
        "ok": False,
        "error": None,
        "query": "",
        "namespace": namespace,
        "pod": pod,
        "since": since,
        "lines": [],
        "count": 0,
    }
    duration = re.fullmatch(r"([1-9][0-9]*)([smhd])", since)
    if not duration or len(since) > 8:
        result["error"] = "since must be a positive duration such as 15m or 1h"
        return result
    seconds = int(duration[1]) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[duration[2]]
    if seconds > 7 * 86400 or not 1 <= limit <= 2000:
        result["error"] = (
            "Log window must be at most seven days and limit between 1 and 2000"
        )
        return result
    if query and (namespace or pod):
        result["error"] = "Use either a LogQL query or namespace/pod filters"
        return result
    if query and len(query) > 8192:
        result["error"] = "LogQL query exceeds 8192 characters"
        return result
    selectors = []
    for label, value in (("namespace", namespace), ("pod", pod)):
        if value:
            if not livestate._valid_k8s_name(value, namespace=label == "namespace"):
                result["error"] = "Invalid namespace or pod name"
                return result
            selectors.append(f"{label}={json.dumps(value)}")
    expression = query or (
        "{" + ",".join(selectors) + "}" if selectors else '{namespace=~".+"}'
    )
    # Do not echo query text: LogQL can contain sensitive literal filters.
    result["query"] = ""
    ctx = None
    try:
        ctx = build_context(env, settings)
        if not ctx.kubeconfig:
            result["error"] = "No kubeconfig is configured for this environment"
            return result
        kubectl = shutil.which("kubectl")
        if not kubectl:
            result["error"] = "kubectl not found on PATH"
            return result
        end = _utcnow()
        params = urlencode(
            {
                "query": expression,
                "start": int((end - timedelta(seconds=seconds)).timestamp() * 1e9),
                "end": int(end.timestamp() * 1e9),
                "limit": limit,
                "direction": "backward",
            }
        )
        path = (
            "/api/v1/namespaces/monitoring/services/http:loki-gateway:80/proxy/loki/api/v1/query_range?"
            + params
        )
        raw, error = livestate._run(
            [kubectl, "get", "--raw", path],
            env=_kube_env(ctx.kubeconfig),
            timeout=livestate.LOG_TIMEOUT,
        )
        if error:
            result["error"] = (
                "Loki query unavailable; check the environment service and Kubernetes access"
            )
            return result
        data = json.loads(raw or "{}")
        if (
            data.get("status") != "success"
            or data.get("data", {}).get("resultType") != "streams"
        ):
            result["error"] = "Loki did not return log streams"
            return result
        entries = []
        for stream in data["data"].get("result", []):
            for entry in stream.get("values", []):
                if (
                    isinstance(entry, list)
                    and len(entry) == 2
                    and isinstance(entry[1], str)
                ):
                    entries.append((int(entry[0]), redact_secret_line(entry[1])))
        entries.sort(key=lambda item: item[0], reverse=True)
        result["lines"] = [line for _, line in entries[:limit]]
        result["count"] = len(result["lines"])
        result["ok"] = True
        return result
    except Exception:  # noqa: BLE001 - provider errors may contain credentials
        result["error"] = "Loki log query failed"
        return result
    finally:
        if ctx is not None:
            ctx.cleanup()
