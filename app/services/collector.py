"""Periodic cluster snapshot collector.

Probes every environment's cluster with kubectl/helm (using the staged
kubeconfig from envcontext), persists a ClusterSnapshot, publishes
fleet/env events, and feeds the alerts and metrics services. A failed
probe is still a snapshot (probe_ok=False, health='down') — collection
never raises, and a broken alerts/metrics module never kills it.

Each probe also runs a config-drift check (check_config_drift): the core
rendered artifacts of the env's current config version are hashed and
compared against the on-host files, one ConfigDrift row per artifact
updated in place. Drift checks never fail a probe either.

Unlike app.services.cluster this module does not preflight shutil.which:
probe errors (missing binary, timeout, non-zero exit) are captured by
_run_probe either way, and tests mock that single helper.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shlex
import subprocess
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import ClusterSnapshot, ConfigDrift, Environment
from app.services import envconfig, events
from app.services import genestack_bridge as bridge
from app.services.envcontext import EnvContext, build_context
from app.services.executors import pick_executor

log = logging.getLogger(__name__)

# Core rendered artifacts drift-checked on every probe (config-dir-relative).
DRIFT_ARTIFACTS = ("openstack-components.yaml", "provider", "inventory/inventory.yaml")

# Artifact statuses that count toward an env being "drifted" ('unknown' does
# not — an unreadable file is not proof of drift).
DRIFTED_STATUSES = frozenset({"drift", "missing"})


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _kube_env(kubeconfig_path: str | None) -> dict[str, str] | None:
    if not kubeconfig_path:
        return None
    return {**os.environ, "KUBECONFIG": kubeconfig_path}


def _run_probe(
    cmd: list[str], *, env: dict[str, str] | None = None, timeout: int
) -> tuple[str | None, str | None]:
    """Run a probe command. Returns (stdout, error)."""
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            check=False,
        )
    except FileNotFoundError:
        return None, f"executable not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return None, f"timed out after {timeout}s"
    except OSError as exc:
        return None, str(exc)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        return None, (detail[:200] or f"exit code {proc.returncode}")
    return proc.stdout or "", None


def _empty_summary() -> dict[str, Any]:
    return {
        "nodes_ready": 0,
        "nodes_total": 0,
        "pods_running": 0,
        "pods_pending": 0,
        "pods_failed": 0,
        "crashlooping": [],
    }


def _parse_nodes(data: dict[str, Any]) -> list[dict[str, Any]]:
    nodes = []
    for item in data.get("items") or []:
        meta = item.get("metadata") or {}
        status = item.get("status") or {}
        ready = False
        for cond in status.get("conditions") or []:
            if cond.get("type") == "Ready":
                ready = cond.get("status") == "True"
        labels = meta.get("labels") or {}
        roles = sorted(
            key.removeprefix("node-role.kubernetes.io/")
            for key in labels
            if key.startswith("node-role.kubernetes.io/")
        )
        nodes.append(
            {
                "name": meta.get("name"),
                "ready": ready,
                "roles": roles,
                "kubelet_version": (status.get("nodeInfo") or {}).get("kubeletVersion"),
            }
        )
    return nodes


def _parse_pods(data: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """Parse pods; also return the crashlooping 'ns/name' list.

    Crashlooping = any container waiting on CrashLoopBackOff, or total
    restarts >= 5.
    """
    pods = []
    crashlooping = []
    for item in data.get("items") or []:
        meta = item.get("metadata") or {}
        spec = item.get("spec") or {}
        status = item.get("status") or {}
        containers = status.get("containerStatuses") or []
        restarts = sum(int(c.get("restartCount") or 0) for c in containers)
        waiting_reasons = [
            reason
            for reason in (
                ((c.get("state") or {}).get("waiting") or {}).get("reason")
                for c in containers
            )
            if reason
        ]
        ns = meta.get("namespace")
        name = meta.get("name")
        if restarts >= 5 or any(r == "CrashLoopBackOff" for r in waiting_reasons):
            crashlooping.append(f"{ns}/{name}")
        pods.append(
            {
                "ns": ns,
                "name": name,
                "phase": status.get("phase"),
                "ready": bool(containers)
                and all(bool(c.get("ready")) for c in containers),
                "restarts": restarts,
                "node": spec.get("nodeName"),
                "waiting_reason": waiting_reasons[0] if waiting_reasons else None,
            }
        )
    return pods, sorted(crashlooping)


def _parse_helm(raw: str | None) -> list[dict[str, Any]]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    return [
        {
            "name": rel.get("name"),
            "ns": rel.get("namespace"),
            "status": rel.get("status"),
            "chart": rel.get("chart"),
            "version": rel.get("app_version"),
        }
        for rel in data
        if isinstance(rel, dict)
    ]


def _failed_snapshot(env: Environment, error: str) -> ClusterSnapshot:
    return ClusterSnapshot(
        environment_id=env.id,
        probe_ok=False,
        error=error[:500],
        nodes=[],
        pods=[],
        helm=[],
        summary=_empty_summary(),
        health="down",
    )


def _probe(
    env: Environment, kube_env: dict[str, str] | None, timeout: int
) -> ClusterSnapshot:
    """Run the kubectl/helm probes and build an (unpersisted) snapshot."""
    raw_nodes, err = _run_probe(
        ["kubectl", "get", "nodes", "-o", "json"], env=kube_env, timeout=timeout
    )
    if err is None:
        raw_pods, err = _run_probe(
            ["kubectl", "get", "pods", "-A", "-o", "json"],
            env=kube_env,
            timeout=timeout,
        )
    if err is not None:
        return _failed_snapshot(env, err)

    try:
        nodes_data = json.loads(raw_nodes or "{}")
        pods_data = json.loads(raw_pods or "{}")
    except json.JSONDecodeError as exc:
        return _failed_snapshot(env, f"invalid kubectl output: {exc}")

    nodes = _parse_nodes(nodes_data)
    pods, crashlooping = _parse_pods(pods_data)

    # helm is optional: a missing/failing helm yields an empty release list,
    # never a probe failure.
    raw_helm, helm_err = _run_probe(
        ["helm", "list", "-A", "-o", "json"], env=kube_env, timeout=timeout
    )
    if helm_err:
        log.info("env %s: helm probe failed (ignored): %s", env.id, helm_err)
    helm = [] if helm_err else _parse_helm(raw_helm)

    summary = {
        "nodes_ready": sum(1 for n in nodes if n["ready"]),
        "nodes_total": len(nodes),
        "pods_running": sum(1 for p in pods if p["phase"] == "Running"),
        "pods_pending": sum(1 for p in pods if p["phase"] == "Pending"),
        "pods_failed": sum(1 for p in pods if p["phase"] == "Failed"),
        "crashlooping": crashlooping,
    }
    if any(not n["ready"] for n in nodes) or crashlooping or summary["pods_failed"] > 0:
        health = "degraded"
    else:
        health = "healthy"

    return ClusterSnapshot(
        environment_id=env.id,
        probe_ok=True,
        error=None,
        nodes=nodes,
        pods=pods,
        helm=helm,
        summary=summary,
        health=health,
    )


def _run_hooks(
    db: Session, env: Environment, snapshot: ClusterSnapshot, settings: Settings
) -> None:
    """Feed alerts/metrics; a broken or missing module never kills collection."""
    try:
        from app.services import alerts

        alerts.evaluate_snapshot(db, env, snapshot)
    except Exception as exc:  # noqa: BLE001
        log.warning("alerts.evaluate_snapshot failed for env %s: %s", env.id, exc)
    try:
        from app.services import metrics

        metrics.collect_for_environment(db, env, settings)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "metrics.collect_for_environment failed for env %s: %s", env.id, exc
        )


# ---------------------------------------------------------------------------
# Config drift detection
# ---------------------------------------------------------------------------


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _read_host_artifact(ctx: EnvContext, target: Path) -> tuple[str | None, str | None]:
    """Read one config-dir file from the env's deploy host.

    Returns ``(content, None)`` on success, ``(None, None)`` when the file is
    absent, and ``(None, error)`` when it cannot be read (permissions,
    unreachable host). Uses the same transport as push: a direct read for
    local envs, a remote ``cat`` through the ssh bridge for remote ones.
    Content never goes to the log (may hold secrets).
    """
    if ctx.ssh_target:
        quoted = shlex.quote(str(target))
        # exit 3 marks "absent" so it is not confused with an unreadable file
        result = bridge.run_command(
            ["bash", "-c", f"if [ -f {quoted} ]; then cat {quoted}; else exit 3; fi"],
            dry_run=False,
            ssh_target=ctx.ssh_target,
            agent_env_id=pick_executor(ctx.environment, ctx).agent_env_id,
            timeout=30,
            log=None,
        )
        returncode = result.get("returncode")
        if returncode == 0:
            return result.get("stdout") or "", None
        if returncode == 3:
            return None, None
        detail = (result.get("stderr") or result.get("message") or "").strip()
        return None, (detail or f"read failed (rc={returncode})")[:300]
    try:
        if not target.is_file():
            return None, None
        return target.read_text(encoding="utf-8"), None
    except OSError as exc:
        return None, str(exc)[:300]


def _classify_artifact(
    ctx: EnvContext, target: Path, expected: str
) -> tuple[str, str | None, str | None]:
    """Classify one artifact: match / drift / missing / unknown."""
    expected_sha = _sha256(expected)
    content, error = _read_host_artifact(ctx, target)
    if error is not None:
        return "unknown", error, None
    if content is None:
        return "missing", "file not present on host", None
    actual_sha = _sha256(content)
    if actual_sha == expected_sha:
        return "match", None, actual_sha
    return "drift", "on-host content differs from rendered config", actual_sha


def _check_config_drift(
    db: Session, env: Environment, settings: Settings
) -> dict[str, str] | None:
    current = envconfig.get_current(db, env)
    if current is None:
        return None  # no config document yet — nothing rendered, nothing to check
    doc, _row = current
    files = envconfig.render_to_files(doc, env, settings)
    expected = {name: files[name] for name in DRIFT_ARTIFACTS if name in files}
    if not expected:
        return None

    ctx = build_context(env, settings)
    try:
        if ctx.config_dir is None:
            return None  # no config dir — nowhere to compare against
        existing = {
            row.artifact: row
            for row in db.scalars(
                select(ConfigDrift).where(ConfigDrift.environment_id == env.id)
            ).all()
        }
        prev_drifted = any(row.status in DRIFTED_STATUSES for row in existing.values())

        statuses: dict[str, str] = {}
        checked_at = _utcnow()
        for name, expected_text in expected.items():
            try:
                status, detail, actual_sha = _classify_artifact(
                    ctx, ctx.config_dir / name, expected_text
                )
            except (
                Exception
            ) as exc:  # noqa: BLE001 — one bad artifact never fails the rest
                status, detail, actual_sha = (
                    "unknown",
                    f"check failed: {exc}"[:300],
                    None,
                )
            row = existing.get(name)
            if row is None:
                row = ConfigDrift(environment_id=env.id, artifact=name)
                db.add(row)
            row.checked_at = checked_at
            row.status = status
            row.expected_sha256 = _sha256(expected_text)
            row.actual_sha256 = actual_sha
            row.detail = detail
            statuses[name] = status
        db.commit()
    finally:
        ctx.cleanup()

    drifted = any(status in DRIFTED_STATUSES for status in statuses.values())
    if drifted != prev_drifted:
        events.publish_sync(
            "fleet",
            {
                "type": "drift",
                "environment_id": env.id,
                "drifted": drifted,
                "artifacts": statuses,
            },
        )
    return statuses


def check_config_drift(
    db: Session, env: Environment, settings: Settings
) -> dict[str, str] | None:
    """Upsert per-artifact drift rows for an env's current config version.

    Returns the artifact -> status map, or None when there is nothing to
    check (no config doc / no config dir). Never raises — drift detection
    must never fail a probe.
    """
    try:
        return _check_config_drift(db, env, settings)
    except Exception as exc:  # noqa: BLE001
        log.warning("config drift check failed for env %s: %s", env.id, exc)
        db.rollback()
        return None


def probe_environment(
    db: Session, env: Environment, settings: Settings
) -> ClusterSnapshot:
    """Probe one environment, persist the snapshot, publish events, run hooks."""
    timeout = settings.collector_probe_timeout_seconds
    if not (env.kubeconfig_path or env.kubeconfig_data):
        # No kubeconfig: skip the kubectl/helm spawn entirely. Without
        # KUBECONFIG kubectl's legacy default target is
        # http://127.0.0.1:8080 — the console itself.
        snapshot = _failed_snapshot(
            env, "No kubeconfig is configured for this environment."
        )
    else:
        ctx = build_context(env, settings)
        try:
            snapshot = _probe(env, _kube_env(ctx.kubeconfig), timeout)
        except (
            Exception
        ) as exc:  # noqa: BLE001 — a failed context/probe is a down snapshot
            snapshot = _failed_snapshot(env, str(exc))
        finally:
            ctx.cleanup()

    db.add(snapshot)
    db.commit()
    db.refresh(snapshot)

    taken_at = (
        snapshot.taken_at.isoformat() if snapshot.taken_at else _utcnow().isoformat()
    )
    events.publish_sync(
        f"env:{env.id}",
        {
            "type": "snapshot",
            "environment_id": env.id,
            "health": snapshot.health,
            "taken_at": taken_at,
            "summary": snapshot.summary,
        },
    )
    events.publish_sync(
        "fleet",
        {
            "type": "fleet",
            "environment_id": env.id,
            "health": snapshot.health,
            "taken_at": taken_at,
        },
    )

    _run_hooks(db, env, snapshot, settings)
    check_config_drift(db, env, settings)
    return snapshot


def prune_old_snapshots(db: Session, settings: Settings) -> int:
    """Delete snapshots older than retention; never an env's newest. Returns count."""
    # SQLite stores taken_at naive; compare against a naive UTC cutoff.
    cutoff = _utcnow().replace(tzinfo=None) - timedelta(
        hours=settings.collector_retention_hours
    )
    newest_ids = set(
        db.scalars(
            select(func.max(ClusterSnapshot.id)).group_by(
                ClusterSnapshot.environment_id
            )
        ).all()
    )
    stmt = select(ClusterSnapshot).where(ClusterSnapshot.taken_at < cutoff)
    deleted = 0
    for snap in db.scalars(stmt).all():
        if snap.id in newest_ids:
            continue
        db.delete(snap)
        deleted += 1
    db.commit()
    return deleted


def collect_all(
    db_factory: Callable[[], Session],
    settings: Settings,
    executor: ThreadPoolExecutor,
) -> dict[str, Any]:
    """Probe every environment (all tenants) in parallel; never raises."""
    result: dict[str, Any] = {
        "probed": 0,
        "healthy": 0,
        "degraded": 0,
        "down": 0,
        "unknown": 0,
        "errors": [],
    }
    db = db_factory()
    try:
        env_ids = list(db.scalars(select(Environment.id)).all())
    finally:
        db.close()

    def _probe_one(env_id: str) -> str:
        session = db_factory()
        try:
            env = session.get(Environment, env_id)
            if env is None:
                return "unknown"
            from app.services.demo import is_demo_env

            if is_demo_env(env):
                return "healthy"
            return probe_environment(session, env, settings).health
        finally:
            session.close()

    futures: dict[Future, str] = {
        executor.submit(_probe_one, env_id): env_id for env_id in env_ids
    }
    for future in as_completed(futures):
        env_id = futures[future]
        try:
            health = future.result()
        except Exception as exc:  # noqa: BLE001
            log.warning("collector: probe of env %s raised: %s", env_id, exc)
            result["errors"].append({"environment_id": env_id, "error": str(exc)})
            continue
        result["probed"] += 1
        result[health] = result.get(health, 0) + 1
    return result
