"""Component reconcile engine — converge the cloud to the desired state.

Desired state is the env config document's ``components:`` block (loaded via
envconfig.get_current, same as the envconfig router/pipeline filter). Actual
state is ``helm list -A -o json`` executed through the env's bridge context
(local or ssh deploy host), parsed the same way collector._parse_helm does.

Component -> helm release naming: genestack releases are typically named
exactly like the component (nova, keystone, glance...) in namespace
``openstack`` — the same assumption cluster.services_status makes when it
joins releases to pods. ``release_for_component`` centralizes the mapping:
``COMPONENT_RELEASE_MAP`` holds the exceptions and the default is identity;
a component whose name is not a valid helm/service name has no determinable
mapping and is planned as ``unknown`` (never acted on).

plan_reconcile is pure computation (DB read + helm read only, no mutations).
run_reconcile logs the plan and, only with apply=True and no env/global
dry_run, executes it: enables go through the exact genestack.service.enable
code path (bridge.enable_service), disables through ``helm uninstall``.
Disabling anything in PROTECTED (core identity/infra) is always refused —
accidentally uninstalling keystone/mariadb is how clouds die.
"""

from __future__ import annotations

import json
from typing import Any, Callable

from sqlalchemy.orm import Session

from app.config import Settings
from app.models import Environment
from app.services import envconfig as envconfig_service
from app.services import genestack_bridge as bridge
from app.services.envcontext import EnvContext, build_context
from app.services.executors import pick_executor
from app.services.service_registry import SERVICE_NAME_RE

LogFn = Callable[[str], None]

# Core identity/infra components reconcile will never uninstall, even when the
# config doc says components.<name>: false.
PROTECTED = frozenset(
    {
        "keystone",
        "placement",
        "nova",
        "neutron",
        "glance",
        "memcached",
        "mariadb-operator",
        "rabbitmq-cluster-operator",
    }
)

# Component -> helm release name exceptions. Default is identity (release
# named exactly like the component); add an entry here when a chart installs
# under a different release name. A None value means "no known release" and
# plans the component as unknown.
COMPONENT_RELEASE_MAP: dict[str, str | None] = {}

# Fallback namespace for helm uninstall when the deployed release's namespace
# cannot be determined (normally it comes from helm list itself).
DEFAULT_NAMESPACE = "openstack"

# helm release statuses that count as "deployed" for diff purposes.
_DEPLOYED_STATUSES = frozenset(
    {"deployed", "failed", "pending-install", "pending-upgrade", "pending-rollback"}
)


def _log(log: LogFn | None, msg: str) -> None:
    if log:
        log(msg)


def release_for_component(
    component: str,
    overrides: dict[str, str | None] | None = None,
) -> str | None:
    """Map a component name to its helm release name (None = unknown).

    ``overrides`` (per-call) wins over the module-level COMPONENT_RELEASE_MAP;
    either may map a component to None to mark it explicitly unmappable.
    Unmapped names default to identity when they are valid service/helm names.
    """
    name = str(component).strip().lower()
    if overrides is not None and name in overrides:
        return overrides[name]
    if name in COMPONENT_RELEASE_MAP:
        return COMPONENT_RELEASE_MAP[name]
    return name if SERVICE_NAME_RE.match(name) else None


def _parse_helm_list(raw: str | None) -> list[dict[str, Any]]:
    """Parse ``helm list -A -o json`` output (mirrors collector._parse_helm)."""
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
            "namespace": rel.get("namespace"),
            "status": rel.get("status"),
            "chart": rel.get("chart"),
        }
        for rel in data
        if isinstance(rel, dict) and rel.get("name")
    ]


def _fetch_releases(
    env: Environment,
    settings: Settings,
    ctx: EnvContext | None = None,
    log: LogFn | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    """Run ``helm list -A -o json`` for the env. Returns (releases, error).

    Read-only, so it always executes (dry_run=False) even on dry-run envs —
    same rule as envconfig's remote file reads.
    """
    own_ctx = ctx is None
    ctx = ctx or build_context(env, settings)
    try:
        result = bridge.run_command(
            ["helm", "list", "-A", "-o", "json"],
            timeout=30,
            dry_run=False,
            extra_env=ctx.subprocess_env(),
            ssh_target=ctx.ssh_target,
            remote_env=ctx.remote_env(),
            agent_env_id=pick_executor(env, ctx).agent_env_id,
            log=log,
        )
    finally:
        if own_ctx:
            ctx.cleanup()
    if result.get("returncode") != 0:
        error = (
            result.get("stderr") or result.get("message") or "helm list failed"
        ).strip()
        return [], error[:300]
    return _parse_helm_list(result.get("stdout")), None


def _desired_components(
    db: Session, env: Environment
) -> tuple[dict[str, Any] | None, str | None]:
    """The env doc's components: block, or (None, note) when undeterminable."""
    current = envconfig_service.get_current(db, env)
    if current is None:
        return None, "no config document yet — nothing to reconcile"
    components = current[0].get("components")
    if not isinstance(components, dict):
        return None, "config doc has no components: block — nothing to reconcile"
    return components, None


def plan_reconcile(
    db: Session,
    env: Environment,
    settings: Settings,
    *,
    releases: list[dict[str, Any]] | None = None,
    release_map: dict[str, str | None] | None = None,
    ctx: EnvContext | None = None,
    log: LogFn | None = None,
) -> dict[str, Any]:
    """Diff desired (config doc components:) vs actual (helm releases).

    Pure computation — no side effects beyond the helm read. Pass ``releases``
    (parsed helm list rows) to unit-test without a cluster; when omitted the
    live list is fetched through the env's bridge context.
    """
    summary = {"to_enable": 0, "to_disable": 0, "in_sync": 0, "unknown": 0}
    plan: dict[str, Any] = {"actions": [], "summary": summary}

    components, note = _desired_components(db, env)
    if components is None:
        plan["note"] = note
        return plan

    helm_error: str | None = None
    if releases is None:
        releases, helm_error = _fetch_releases(env, settings, ctx=ctx, log=log)
        if helm_error:
            plan["helm_error"] = helm_error
            plan["note"] = (
                f"helm list failed: {helm_error} — treating nothing as deployed"
            )

    deployed: dict[str, dict[str, Any]] = {}
    for rel in releases:
        status = str(rel.get("status") or "").lower()
        if status and status not in _DEPLOYED_STATUSES:
            continue
        deployed.setdefault(str(rel["name"]), rel)

    for component, desired_raw in components.items():
        name = str(component).strip().lower()
        desired = bool(desired_raw)
        release = release_for_component(name, overrides=release_map)
        if release is None:
            summary["unknown"] += 1
            plan["actions"].append(
                {
                    "component": name,
                    "desired": desired,
                    "deployed": False,
                    "action": "none",
                    "release": None,
                    "reason": "unknown release mapping",
                }
            )
            continue
        rel = deployed.get(release)
        is_deployed = rel is not None
        namespace = str(rel.get("namespace")) if rel else None
        if desired and not is_deployed:
            action, reason = "enable", "desired but not deployed"
            summary["to_enable"] += 1
        elif not desired and is_deployed:
            action, reason = "disable", "deployed but not desired"
            summary["to_disable"] += 1
        else:
            action = "none"
            reason = "in sync" if desired else "absent as desired"
            summary["in_sync"] += 1
        plan["actions"].append(
            {
                "component": name,
                "desired": desired,
                "deployed": is_deployed,
                "action": action,
                "release": release,
                "namespace": namespace,
                "reason": reason,
            }
        )
    return plan


def _log_plan(plan: dict[str, Any], log: LogFn | None) -> None:
    if plan.get("note"):
        _log(log, f"[reconcile] note: {plan['note']}")
    for action in plan["actions"]:
        _log(
            log,
            f"[reconcile] plan: {action['component']} desired={action['desired']} "
            f"deployed={action['deployed']} release={action['release']} "
            f"-> {action['action']} ({action['reason']})",
        )
    s = plan["summary"]
    _log(
        log,
        f"[reconcile] summary: to_enable={s['to_enable']} to_disable={s['to_disable']} "
        f"in_sync={s['in_sync']} unknown={s['unknown']}",
    )


def run_reconcile(
    db: Session,
    env: Environment | None,
    settings: Settings,
    apply: bool = False,
    log: LogFn | None = None,
    *,
    timeout: int | None = None,
) -> dict[str, Any]:
    """Plan and (optionally) apply component reconciliation for an env.

    Default (apply=False) is plan-only: the plan is logged and returned, no
    executions. apply=True enables missing components via the same code path
    as the genestack.service.enable op (bridge.enable_service) and uninstalls
    undesired releases via helm — stopping on the first failure. Env/global
    dry_run forces plan-only even with apply=True. Components in PROTECTED
    are never uninstalled.
    """
    if env is None:
        return {
            "ok": False,
            "error": "genestack.components.reconcile requires an environment",
            "returncode": 2,
        }
    timeout = timeout or settings.job_timeout_seconds
    ctx = build_context(env, settings)
    try:
        plan = plan_reconcile(db, env, settings, ctx=ctx, log=log)
        _log_plan(plan, log)

        dry = ctx.dry_run
        if not apply:
            _log(log, "[reconcile] DRY PLAN — re-run with apply=true to execute")
            return {
                **plan,
                "ok": True,
                "applied": 0,
                "dry_run": True,
                "message": "plan-only (apply=false)",
            }
        if dry:
            _log(
                log,
                "[reconcile] dry-run: env/global dry_run is set — forcing plan-only "
                "even though apply=true",
            )
            return {
                **plan,
                "ok": True,
                "applied": 0,
                "dry_run": True,
                "message": "plan-only (dry_run forced)",
            }
        if plan.get("helm_error"):
            msg = f"refusing to apply: helm list failed ({plan['helm_error']})"
            _log(log, f"[reconcile] {msg}")
            return {**plan, "ok": False, "error": msg, "applied": 0, "returncode": 1}

        # Same execution context the genestack_service_enable handler gets:
        # subprocess/remote env plus doc-derived vars (GATEWAY_DOMAIN et al).
        extra_env = ctx.subprocess_env()
        remote_env = ctx.remote_env()
        agent_env_id = pick_executor(env, ctx).agent_env_id
        current = envconfig_service.get_current(db, env)
        if current is not None:
            doc_vars = envconfig_service.doc_env(current[0])
            extra_env.update(doc_vars)
            remote_env.update(doc_vars)

        applied = 0
        results: list[dict[str, Any]] = []
        for action in plan["actions"]:
            kind = action["action"]
            if kind == "none":
                continue
            component = action["component"]
            if kind == "enable":
                _log(
                    log,
                    f"[reconcile] enable {component} (genestack.service.enable path)",
                )
                result = bridge.enable_service(
                    component,
                    ctx.genestack_root,
                    dry_run=False,
                    timeout=timeout,
                    extra_env=extra_env,
                    ssh_target=ctx.ssh_target,
                    remote_env=remote_env,
                    agent_env_id=agent_env_id,
                    log=log,
                )
            else:  # disable
                if component in PROTECTED:
                    _log(log, f"[reconcile] {component}: refused: protected component")
                    results.append({**action, "refused": True})
                    continue
                namespace = action.get("namespace") or DEFAULT_NAMESPACE
                release = action["release"]
                _log(
                    log,
                    f"[reconcile] disable {component}: helm uninstall {release} -n {namespace}",
                )
                result = bridge.run_command(
                    ["helm", "uninstall", release, "-n", namespace],
                    timeout=timeout,
                    dry_run=False,
                    extra_env=extra_env,
                    ssh_target=ctx.ssh_target,
                    remote_env=remote_env,
                    agent_env_id=agent_env_id,
                    log=log,
                )
            ok = result.get("returncode") == 0 and result.get("ok", True)
            results.append({**action, "returncode": result.get("returncode"), "ok": ok})
            if not ok:
                msg = (
                    f"reconcile {kind} {component} failed "
                    f"(rc={result.get('returncode')}): {result.get('error') or result.get('message')}"
                )
                _log(log, f"[reconcile] FAILED — stopping: {msg}")
                return {
                    **plan,
                    "ok": False,
                    "error": msg,
                    "applied": applied,
                    "results": results,
                    "returncode": result.get("returncode") or 1,
                }
            applied += 1
        _log(log, f"[reconcile] applied {applied} action(s)")
        return {
            **plan,
            "ok": True,
            "applied": applied,
            "results": results,
            "dry_run": False,
            "message": f"applied {applied} action(s)",
        }
    finally:
        ctx.cleanup()
