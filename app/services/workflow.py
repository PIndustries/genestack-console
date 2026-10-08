"""Guided lifecycle workflow view for one environment (read-only).

Aggregates everything the console UI needs to render the "where is this
environment in its lifecycle" stepper: six steps (connect, inventory,
config, push, deploy, operate), each with a traffic-light state
(``done`` / ``attention`` / ``pending``), a one-line plain-English
summary, and a details mapping for the UI.

Like the descriptor, this module never raises: every step builder runs
behind :func:`_safe_step`, so a failing probe degrades that step to
``pending`` with an ``error`` note in details instead of 500ing the
endpoint.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models import AuditLog, Environment, Job, JobStatus
from app.services import cluster as cluster_probe
from app.services import descriptor as descriptor_service
from app.services import envconfig as envconfig_service
from app.services.envcontext import build_context
from app.services.service_registry import PIPELINE_STAGES, filter_stage_items
from app.services.deploy_timing import attach_stage_times, parse_timings

# Roles that must each appear on at least one host for a deployable topology
REQUIRED_ROLES = ("k8s_control_plane", "etcd", "control")

# Roles reported in the inventory step details
TRACKED_ROLES = (
    "k8s_control_plane",
    "etcd",
    "control",
    "compute",
    "network",
    "storage",
    "worker",
)

# Operations that count as "pushing config to the deploy host"
PUSH_OPERATIONS = ("genestack.config.push", "genestack.deploy")

DEPLOY_OPERATION = "genestack.deploy"
DEPLOY_OPERATIONS = ("genestack.deploy", "genestack.greenfield")

# Operations that count as "verifying the environment works"
VERIFY_OPERATIONS = ("genestack.verify",)

# Operation that prepares the deploy host (clone + bootstrap)
PREPARE_OPERATION = "genestack.host_prepare"

# Helm/chart name fragments that mean a pipeline stage has landed on the cluster.
# Console is the source of truth: the workflow uses this to offer "Continue from".
_STAGE_RELEASE_HINTS: dict[str, tuple[str, ...]] = {
    "infrastructure": ("kube-ovn",),
    # kube-ovn is its own control point; detect it so Continue is not stuck on CNI.
    "cni": ("kube-ovn",),
    "operators": (
        "cert-manager",
        "mariadb-operator",
        "mariadb",
        "postgres-operator",
        "redis-operator",
        "memcached",
        "sealed-secrets",
        "metallb",
        "longhorn",
        "envoy-gateway",
        "rabbitmq",
    ),
    "core": ("keystone", "placement", "glance"),
    "compute-network": ("nova", "neutron", "libvirt"),
    "platform-extras": (
        "barbican",
        "cinder",
        "heat",
        "horizon",
        "skyline",
        "octavia",
        "manila",
        "magnum",
    ),
    "observability": (
        "grafana",
        "loki",
        "tempo",
        "kube-prometheus-stack",
        "prometheus",
        "fluentbit",
        "openstack-exporter",
    ),
    "testing": ("tempest",),
}


def _step(
    step_id: str, state: str, summary: str, details: dict[str, Any]
) -> dict[str, Any]:
    return {"id": step_id, "state": state, "summary": summary, "details": details}


def _safe_step(step_id: str, fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """Run one step builder; turn any unexpected failure into a pending step."""
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 — the workflow view must never raise
        return _step(step_id, "pending", "probe failed", {"error": str(exc)})


def _resolved_dry_run(env: Environment, settings: Settings) -> bool:
    return bool(env.dry_run) if env.dry_run is not None else bool(settings.dry_run)


def _kubeconfig_source(env: Environment) -> str:
    if env.kubeconfig_data:
        return "blob"
    if env.kubeconfig_path:
        return "path"
    return "default"


def _latest_job(
    db: Session, env: Environment, operations: tuple[str, ...]
) -> Job | None:
    return db.scalar(
        select(Job)
        .where(Job.environment_id == env.id, Job.operation.in_(operations))
        .order_by(Job.created_at.desc())
        .limit(1)
    )


def _failed_stage_id(failed_at: Any) -> str | None:
    """Parse ``infrastructure/setup-infrastructure.sh`` or job error text."""
    text = str(failed_at or "").strip()
    if not text:
        return None
    if "/" in text:
        head = text.split("/", 1)[0].strip()
        if head:
            return head
    match = re.search(r"stage ['\"]([a-z0-9-]+)['\"]", text, re.I)
    return match.group(1) if match else None


def _release_blob(releases: list[Any]) -> str:
    parts: list[str] = []
    for rel in releases:
        if not isinstance(rel, dict):
            continue
        status = str(rel.get("status") or "deployed").lower()
        if status not in ("deployed", "superseded"):
            continue
        parts.append(str(rel.get("name") or ""))
        parts.append(str(rel.get("chart") or ""))
    return " ".join(parts).lower()


def _stage_detected(stage_id: str, blob: str, cluster: dict[str, Any]) -> bool:
    if stage_id == "hosts":
        return bool(cluster.get("reachable") and (cluster.get("nodes") or []))
    hints = _STAGE_RELEASE_HINTS.get(stage_id) or ()
    if not hints:
        return False
    # Core / compute must actually be installed, not a leftover helm name
    # (failed keystone still matches "keystone"; libvirt must not mark nova done).
    if stage_id in ("core", "compute-network"):
        return all(hint in blob for hint in hints)
    # Barbican/cinder/heat can land while horizon helm is still failed.
    # Continue must stay on extras until a dashboard release exists.
    if stage_id == "platform-extras":
        return "horizon" in blob or "skyline" in blob
    return any(hint in blob for hint in hints)


def parse_deploy_log(log_text: str | None) -> dict[str, Any]:
    """Current stage/item/service from a deploy or greenfield job log."""
    stage = ""
    item = ""
    service = ""
    pxe = False
    talos = False
    for line in str(log_text or "").splitlines():
        match = re.search(r"===\s*stage\s+\d+\s*/\s*\d+\s*:\s*([\w-]+)", line)
        if match:
            stage = match.group(1)
        match = re.search(r"\$\s+bash\s+(bin/\S+)", line)
        if match:
            item = match.group(1)
        match = re.search(r"FAILED at\s+([\w-]+)/(\S+)", line)
        if match:
            stage = match.group(1)
            item = match.group(2)
        match = re.search(r"SERVICE_NAME=([A-Za-z0-9_-]+)", line)
        if match:
            service = match.group(1)
        if re.search(
            r"\[greenfield\].*(PXE|ISO|BYOI)|pxe_boot|iso_boot|virtual CD", line, re.I
        ):
            pxe = True
        if re.search(r"talosctl|maintenance API|:50000", line, re.I):
            talos = True
    return {
        "stage": stage,
        "item": item,
        "service": service,
        "pxe": pxe,
        "talos": talos,
    }


def _item_name(item: dict[str, Any]) -> str:
    name = str(item.get("name") or "").strip()
    if name:
        return name.replace("bin/", "").replace("install-", "").removesuffix(".sh")
    script = str(item.get("script") or "")
    return (
        script.replace("bin/", "").replace("install-", "").removesuffix(".sh") or script
    )


def _item_matches_blob(name: str, blob: str) -> bool:
    key = str(name or "").strip().lower()
    if not key or not blob:
        return False
    if key in {"setup-hosts", "setup-hosts.sh"}:
        return False
    if key in {"setup-infrastructure", "setup-infrastructure.sh"}:
        return "kube-ovn" in blob
    token = key.replace("_", "-")
    return token in blob


def _failed_item_name(failed_at: Any) -> str | None:
    text = str(failed_at or "")
    if "/" not in text:
        return None
    tail = text.split("/", 1)[1].strip()
    return tail.replace("bin/", "").replace("install-", "").removesuffix(".sh") or None


def pipeline_progress(
    db: Session, env: Environment, settings: Settings | None = None
) -> dict[str, Any]:
    """Where the genestack pipeline actually is, from live cluster + last job.

    ``next_stage`` is the first unfinished PIPELINE_STAGES id (or the stage
    the last deploy failed on). The UI uses it for Continue — never helm/kubectl.
    Each stage includes per-item state so Overview can show what is done,
    running, blocked, and still left without extra clicks.
    """
    from app.services.demo import canned_pipeline, is_demo_env

    if is_demo_env(env):
        return canned_pipeline()
    settings = settings or get_settings()
    job = _latest_job(db, env, DEPLOY_OPERATIONS)
    running = bool(
        job is not None and job.status in (JobStatus.running, JobStatus.queued)
    )
    failed_at = None
    if job is not None and job.status == JobStatus.failed:
        action = (
            "env.greenfield"
            if job.operation == "genestack.greenfield"
            else "env.deploy"
        )
        audit = _latest_audit_details(db, env, action)
        if not audit:
            audit = _latest_audit_details(db, env, "env.deploy")
        failed_at = audit.get("failed_at") or job.error

    cluster: dict[str, Any] = {"reachable": False, "nodes": []}
    services: dict[str, Any] = {"releases": []}
    # helm list shares a lock with in-flight `helm upgrade`. Snapshot/Overview
    # must still report running/failed_at from the job table when a deploy is live.
    if env.kubeconfig_path or env.kubeconfig_data:
        ctx = build_context(env, settings)
        try:
            cluster = cluster_probe.cluster_status(ctx.kubeconfig)
            if not running:
                services = cluster_probe.services_status(ctx.kubeconfig)
        finally:
            ctx.cleanup()
    blob = "" if running else _release_blob(services.get("releases") or [])

    components = None
    try:
        current = envconfig_service.get_current(db, env)
        doc = current[0] if current else {}
        if isinstance(doc, dict) and isinstance(doc.get("components"), dict):
            components = doc.get("components")
    except Exception:  # noqa: BLE001 — progress must still return stages
        components = None

    activity = parse_deploy_log(job.log_text if job is not None else "")
    params = job.params if job is not None and isinstance(job.params, dict) else {}
    from_stage = str(params.get("from_stage") or "").strip()
    log_stage = str(activity.get("stage") or "").strip()
    current_stage = log_stage or (from_stage if running else "")
    if (
        running
        and job is not None
        and job.operation == "genestack.greenfield"
        and not log_stage
    ):
        current_stage = "hosts"
    current_item = str(activity.get("service") or activity.get("item") or "").strip()
    current_item_key = (
        current_item.replace("bin/", "").replace("install-", "").removesuffix(".sh")
    )
    failed_item = _failed_item_name(failed_at)
    failed_stage = _failed_stage_id(failed_at)

    stages: list[dict[str, Any]] = []
    next_stage: str | None = None
    seen_current = False
    remaining: list[dict[str, str]] = []
    current_label = ""
    for spec in PIPELINE_STAGES:
        sid = str(spec["id"])
        detected = _stage_detected(sid, blob, cluster)
        state = "done" if detected else "pending"
        if running and not detected and next_stage is None:
            state = "running"
        if not detected and next_stage is None:
            next_stage = sid
        items_out: list[dict[str, Any]] = []
        spec_items = filter_stage_items(spec, components, None)
        in_current_stage = bool(running and current_stage == sid)
        if in_current_stage:
            seen_current = True
        passed_current_item = not in_current_stage
        if running and current_stage and not seen_current and sid != current_stage:
            # Stages before the live stage are already underway or done.
            if not detected:
                state = "done"
        for raw in spec_items:
            name = _item_name(raw)
            item_state = "pending"
            if (
                failed_stage == sid
                and failed_item
                and failed_item in {name, raw.get("name")}
            ):
                item_state = "failed"
                state = "failed"
            elif running and in_current_stage:
                matched = bool(current_item_key) and (
                    current_item_key == name
                    or current_item.endswith(name)
                    or name in current_item_key
                )
                if matched:
                    item_state = "running"
                    passed_current_item = True
                    current_label = name
                elif not passed_current_item and current_item_key:
                    item_state = "done"
                elif not passed_current_item:
                    item_state = "running"
                    passed_current_item = True
                    current_label = name
                else:
                    item_state = "pending"
            elif detected or (not running and _item_matches_blob(name, blob)):
                item_state = "done"
            elif running and current_stage and not seen_current:
                item_state = "done"
            items_out.append(
                {
                    "name": name,
                    "script": str(raw.get("script") or ""),
                    "state": item_state,
                }
            )
            if item_state in {"pending", "running", "failed"}:
                remaining.append({"stage": sid, "item": name, "state": item_state})
        if in_current_stage:
            state = (
                "failed"
                if any(i["state"] == "failed" for i in items_out)
                else "running"
            )
        stages.append(
            {
                "id": sid,
                "name": spec["name"],
                "description": spec.get("description") or "",
                "state": state,
                "required": bool(spec.get("required", True)),
                "control": str(spec.get("control") or "helm"),
                "items": items_out,
            }
        )
    if running and job is not None:
        if from_stage and not log_stage:
            next_stage = from_stage
            for item in stages:
                if item["id"] == from_stage:
                    item["state"] = "running"
                elif item["state"] == "running" and item["id"] != from_stage:
                    item["state"] = "pending"
        elif current_stage:
            next_stage = current_stage

    ids = {item["id"] for item in stages}
    if failed_stage in ids:
        for item in stages:
            if item["id"] != failed_stage:
                continue
            # Last deploy failed here — Continue must resume this stage even
            # when a helm name fragment already marked it "done" (e.g.
            # mariadb-operator present but redis-replication play missing).
            item["state"] = "failed"
            next_stage = failed_stage
            break

    total = sum(len(s.get("items") or []) for s in stages)
    done_n = sum(
        1 for s in stages for it in (s.get("items") or []) if it.get("state") == "done"
    )
    current = None
    if running:
        current = {
            "stage": current_stage or next_stage,
            "item": current_item_key or current_item,
            "service": activity.get("service") or "",
            "label": current_label or current_item_key or current_stage,
            "pxe": bool(activity.get("pxe")),
            "talos": bool(activity.get("talos")),
            "operation": job.operation if job is not None else "",
        }

    parsed = parse_timings(job.log_text if job is not None else "")
    attach_stage_times(stages, parsed)
    elapsed_s = _job_elapsed_s(job)
    if elapsed_s is not None and parsed.get("total_s") is None:
        parsed["total_s"] = elapsed_s

    return {
        "stages": stages,
        "next_stage": next_stage,
        "can_continue": bool(next_stage) and not running,
        "running": running,
        "failed_at": failed_at,
        "release_count": len(services.get("releases") or []),
        "node_count": len(cluster.get("nodes") or []),
        "cluster_reachable": bool(cluster.get("reachable")),
        "current": current,
        "remaining": remaining,
        "done_count": done_n,
        "total_count": total,
        "elapsed_s": elapsed_s,
        "timings": parsed,
    }


def _job_elapsed_s(job: Job | None) -> float | None:
    if job is None or job.started_at is None:
        return None
    start = job.started_at
    end = job.finished_at
    if end is None:
        end = datetime.now(timezone.utc)
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    return max(0.0, (end - start).total_seconds())


def _latest_audit_details(db: Session, env: Environment, action: str) -> dict[str, Any]:
    row = db.scalar(
        select(AuditLog)
        .where(AuditLog.environment_id == env.id, AuditLog.action == action)
        .order_by(AuditLog.id.desc())
        .limit(1)
    )
    return dict(row.details) if row and isinstance(row.details, dict) else {}


# ---------------------------------------------------------------------------
# Step builders
# ---------------------------------------------------------------------------


def _ovh_has_private_ip(assignment: dict[str, Any]) -> bool:
    """True when the host has a private_ip, or an ip already in 10.0.0.0/8."""
    if assignment.get("private_ip"):
        return True
    first = str(assignment.get("ip") or "").strip().split(".", 1)[0]
    return first == "10"


def _connect_step(db: Session, env: Environment, settings: Settings) -> dict[str, Any]:
    details: dict[str, Any] = {
        "genestack_config_dir": env.genestack_config_dir,
        "deployer_ssh_host": env.deployer_ssh_host,
        "deployer_ssh_user": env.deployer_ssh_user,
        "kubeconfig_source": _kubeconfig_source(env),
        "dry_run": _resolved_dry_run(env, settings),
    }
    # Host readiness comes from the latest genestack.host_prepare job only —
    # never a live probe of the remote host on every workflow call.
    prepare_job = _latest_job(db, env, (PREPARE_OPERATION,))
    if prepare_job is None:
        details["prepared"] = None
    else:
        details["prepared"] = prepare_job.status == JobStatus.success
        details["prepare_job_id"] = prepare_job.id
    if env.genestack_config_dir:
        summary = "deploy target configured"
        if prepare_job is not None and prepare_job.status != JobStatus.success:
            summary += " (host prepare not verified)"
        return _step("connect", "done", summary, details)
    # Community OVH: jobs run on this console. A remote config dir / agent is
    # optional — missing one is not a failure.
    details["local_hub"] = True
    return _step("connect", "done", "this console is the fleet hub", details)


def _inventory_step(db: Session, env: Environment) -> dict[str, Any]:
    current = envconfig_service.get_current(db, env)
    doc = current[0] if current else {}
    servers = (doc.get("servers") or {}) if isinstance(doc, dict) else {}
    if not isinstance(servers, dict):
        servers = {}
    roles = {role: 0 for role in TRACKED_ROLES}
    ovh_hosts: list[str] = []
    missing_private: list[str] = []
    private_assigned = 0
    for hostname, assignment in servers.items():
        if not isinstance(assignment, dict):
            continue
        assigned = {str(r).lower() for r in (assignment.get("roles") or [])}
        for role in TRACKED_ROLES:
            if role in assigned:
                roles[role] += 1
        if str(assignment.get("source") or "").lower() != "ovh":
            continue
        ovh_hosts.append(str(hostname))
        if _ovh_has_private_ip(assignment):
            private_assigned += 1
        else:
            missing_private.append(str(hostname))
    ovh = doc.get("ovh") if isinstance(doc, dict) else {}
    if not isinstance(ovh, dict):
        ovh = {}
    vrack = ovh.get("vrack")
    if isinstance(vrack, str):
        vrack = vrack.strip() or None
    elif vrack is not None:
        vrack = str(vrack)
    vlan_id = ovh.get("vlan_id")
    details: dict[str, Any] = {
        "host_count": len(servers),
        "roles": roles,
        "source": "doc" if servers else "none",
        "ovh_bound": bool(ovh_hosts),
    }
    if ovh_hosts:
        details["vrack"] = vrack
        details["vlan_id"] = vlan_id
        details["private_ips_assigned"] = private_assigned
        details["hosts_missing_private_ip"] = missing_private
    if not servers:
        return _step("inventory", "pending", "no servers in config document", details)
    missing = [role for role in REQUIRED_ROLES if roles[role] == 0]
    if missing:
        return _step(
            "inventory",
            "attention",
            f"missing required roles: {', '.join(missing)}",
            details,
        )
    if ovh_hosts:
        if not vrack:
            return _step(
                "inventory",
                "attention",
                "vRack not set — pick it on Platform → OVH",
                details,
            )
        if vlan_id is None or vlan_id == "":
            return _step(
                "inventory",
                "attention",
                "VLAN not set — pick it on Platform → OVH",
                details,
            )
        if missing_private:
            n = len(missing_private)
            return _step(
                "inventory",
                "attention",
                f"missing private IPs on {n} OVH host(s)",
                details,
            )
        return _step(
            "inventory",
            "done",
            f"{len(ovh_hosts)} OVH host(s), VLAN {vlan_id}, vRack {vrack}, roles covered",
            details,
        )
    return _step(
        "inventory",
        "done",
        f"{len(servers)} host(s) with all required roles",
        details,
    )


def _config_step(db: Session, env: Environment) -> dict[str, Any]:
    current = envconfig_service.get_current(db, env)
    if current is None:
        details = {"version": None, "updated_at": None, "updated_by": None}
        return _step("config", "pending", "no config document yet", details)
    _doc, row = current
    details = {
        "version": row.version,
        "updated_at": row.created_at,
        "updated_by": row.created_by,
    }
    return _step("config", "done", f"config version {row.version} saved", details)


def _job_state_and_summary(job: Job, *, kind: str) -> tuple[str, str]:
    """Map a latest push/deploy job onto (state, summary)."""
    if job.status == JobStatus.success:
        return "done", "config pushed" if kind == "push" else "deployed"
    if job.status == JobStatus.failed:
        return "attention", f"last {kind} failed"
    if job.status == JobStatus.running:
        return "pending", f"{kind} in progress"
    return "pending", f"{kind} queued"


def _push_step(db: Session, env: Environment, settings: Settings) -> dict[str, Any]:
    job = _latest_job(db, env, PUSH_OPERATIONS)
    if job is None:
        details = {
            "job_id": None,
            "status": None,
            "finished_at": None,
            "dry_run": _resolved_dry_run(env, settings),
        }
        return _step("push", "pending", "never pushed", details)
    # Version is not persisted on the job; recover it from the audit trail
    # when available (env.config.push / env.deploy both record it).
    action = "env.deploy" if job.operation == DEPLOY_OPERATION else "env.config.push"
    audit = _latest_audit_details(db, env, action)
    details: dict[str, Any] = {
        "job_id": job.id,
        "status": job.status.value,
        "finished_at": job.finished_at,
        "dry_run": audit.get("dry_run", _resolved_dry_run(env, settings)),
    }
    if audit.get("version") is not None:
        details["version"] = audit["version"]
    state, summary = _job_state_and_summary(job, kind="push")
    return _step("push", state, summary, details)


def _deploy_step(db: Session, env: Environment, settings: Settings) -> dict[str, Any]:
    job = _latest_job(db, env, (DEPLOY_OPERATION,))
    pipe = pipeline_progress(db, env, settings)
    details: dict[str, Any] = {
        "job_id": job.id if job is not None else None,
        "status": job.status.value if job is not None else None,
        "finished_at": job.finished_at if job is not None else None,
        "dry_run": job.dry_run if job is not None else None,
        "pipeline": pipe,
    }
    audit = _latest_audit_details(db, env, "env.deploy")
    if audit.get("stages_completed") is not None:
        details["stages_completed"] = audit["stages_completed"]
        details["stages_total"] = audit.get("stages_total")
    if job is None:
        if pipe.get("cluster_reachable"):
            nxt = pipe.get("next_stage") or "infrastructure"
            return _step(
                "deploy",
                "attention",
                f"Kubernetes is up; continue from {nxt}",
                details,
            )
        return _step("deploy", "pending", "not deployed yet", details)
    state, summary = _job_state_and_summary(job, kind="deploy")
    nxt = pipe.get("next_stage")
    if job.status == JobStatus.failed and nxt:
        summary = "last deploy failed"
    elif job.status == JobStatus.success and nxt and nxt != "hosts":
        # Job reported success but live helm/cluster shows later stages missing
        # (e.g. Talos is up, OpenStack not). Offer Continue instead of "done".
        state = "attention"
        summary = f"deployed through earlier stages; continue from {nxt}"
    return _step("deploy", state, summary, details)


def _operate_step(db: Session, env: Environment, settings: Settings) -> dict[str, Any]:
    ctx = build_context(env, settings)
    try:
        cluster = cluster_probe.cluster_status(ctx.kubeconfig)
        services = cluster_probe.services_status(ctx.kubeconfig)
        # Reuse the descriptor's doc-vs-on-disk inventory drift check
        # (None when either side is missing; never raises).
        drift = descriptor_service._config_drift(env, settings, ctx.config_dir)
    finally:
        ctx.cleanup()
    reachable = bool(cluster.get("reachable"))
    nodes = cluster.get("nodes") or []
    releases = services.get("releases") or []
    details = {
        "reachable": reachable,
        "node_count": len(nodes),
        "release_count": len(releases),
        "error": cluster.get("error") or services.get("error"),
    }
    if drift is not None:
        details["drift"] = drift
    verify_job = _latest_job(db, env, VERIFY_OPERATIONS)
    if verify_job is None:
        details["verify"] = None
    else:
        details["verify"] = {
            "job_id": verify_job.id,
            "status": verify_job.status.value,
            "level": (verify_job.params or {}).get("level") or "standard",
            "finished_at": verify_job.finished_at,
        }
    current = envconfig_service.get_current(db, env)
    network = (current[0].get("network") or {}) if current else {}
    if isinstance(network, dict) and network.get("gateway_domain"):
        domain = network["gateway_domain"]
        details["skyline_url"] = f"https://skyline.{domain}"
        details["horizon_url"] = f"https://horizon.{domain}"
    if not reachable:
        return _step("operate", "pending", "cluster unreachable", details)
    if not releases:
        return _step(
            "operate",
            "attention",
            "cluster reachable but no helm releases",
            details,
        )
    return _step(
        "operate",
        "done",
        f"cluster reachable, {len(releases)} helm release(s)",
        details,
    )


def _operate_unchecked_step() -> dict[str, Any]:
    """Operate step without live probes — used by fleet views where probing
    every cluster (5s timeout each) would make the endpoint unusable."""
    return _step("operate", "pending", "not checked", {})


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


def build_workflow(
    db: Session,
    env: Environment,
    settings: Settings | None = None,
    include_operate_probe: bool = True,
) -> dict[str, Any]:
    """Assemble the read-only lifecycle workflow view for one environment.

    With ``include_operate_probe=False`` the operate step skips the live
    cluster probes and reports "not checked" instead.
    """
    settings = settings or get_settings()
    operate = (
        _operate_step
        if include_operate_probe
        else lambda _db, _e, _s: _operate_unchecked_step()
    )
    return {
        "environment": {
            "id": env.id,
            "name": env.name,
            "region": env.region,
            "tier": env.tier,
            "dry_run": _resolved_dry_run(env, settings),
            "tenant_id": env.tenant_id,
        },
        "steps": [
            _safe_step("connect", lambda: _connect_step(db, env, settings)),
            _safe_step("inventory", lambda: _inventory_step(db, env)),
            _safe_step("config", lambda: _config_step(db, env)),
            _safe_step("push", lambda: _push_step(db, env, settings)),
            _safe_step("deploy", lambda: _deploy_step(db, env, settings)),
            _safe_step("operate", lambda: operate(db, env, settings)),
        ],
    }
