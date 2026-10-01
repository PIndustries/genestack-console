"""Environment-facing Kubernetes day-2 operations.

Never raises to the router: missing kubeconfig and API failures become
``ok: false`` dicts. Dry-run mutates return without calling kube.
"""

from __future__ import annotations

from typing import Any, Callable

from app.config import Settings, get_settings
from app.models import Environment
from app.services.envcontext import build_context
from app.services.k8sclient import (
    CLUSTER_DESCRIBE_KINDS,
    DELETE_WORKLOAD_KINDS,
    DESCRIBE_KINDS,
    NAME_RE,
    NS_RE,
    RESTART_KINDS,
    SCALE_KINDS,
    K8sClient,
    K8sError,
    parse_yaml_docs,
    validate_chart,
    validate_label,
    validate_taint,
)


def validate_namespace(value: str, *, allow_empty: bool = False) -> str | None:
    text = str(value or "").strip()
    if not text:
        if allow_empty:
            return None
        return "invalid namespace: empty"
    if not NS_RE.match(text):
        return f"invalid namespace {text!r}"
    return None


def validate_name(value: str, *, field: str = "name") -> str | None:
    text = str(value or "").strip()
    if not text or not NAME_RE.match(text):
        return f"invalid {field} {text!r}"
    return None


def _empty_workloads(namespace: str, error: str | None) -> dict[str, Any]:
    return {
        "ok": False,
        "namespace": namespace,
        "deployments": [],
        "statefulsets": [],
        "daemonsets": [],
        "pods": [],
        "error": error,
    }


def list_workloads(
    env: Environment,
    settings: Settings | None = None,
    *,
    namespace: str = "",
    pods_only: bool = False,
) -> dict[str, Any]:
    """List deployments/STS/DS/pods. Never raises."""
    settings = settings or get_settings()
    ns = str(namespace or "").strip()
    ctx = build_context(env, settings)
    try:
        kube = ctx.kubeconfig
        if not kube:
            return _empty_workloads(ns, "no kubeconfig")
        with K8sClient(kube) as client:
            listed_ns = ns or None
            if pods_only:
                return {
                    "ok": True,
                    "namespace": ns,
                    "deployments": [],
                    "statefulsets": [],
                    "daemonsets": [],
                    "pods": client.list_pods(listed_ns),
                    "error": None,
                }
            return {
                "ok": True,
                "namespace": ns,
                "deployments": client.list_deployments(listed_ns),
                "statefulsets": client.list_statefulsets(listed_ns),
                "daemonsets": client.list_daemonsets(listed_ns),
                "pods": client.list_pods(listed_ns),
                "error": None,
            }
    except Exception as exc:  # noqa: BLE001 — read path must never raise
        return _empty_workloads(ns, str(exc)[:200])
    finally:
        ctx.cleanup()


def _read(
    env: Environment,
    settings: Settings | None,
    fn: Callable[[K8sClient], dict[str, Any]],
    empty: dict[str, Any],
) -> dict[str, Any]:
    settings = settings or get_settings()
    ctx = build_context(env, settings)
    try:
        kube = ctx.kubeconfig
        if not kube:
            out = dict(empty)
            out["ok"] = False
            out["error"] = "no kubeconfig"
            return out
        with K8sClient(kube) as client:
            payload = fn(client)
        result: dict[str, Any] = {"ok": True, "error": None}
        result.update(payload)
        result["ok"] = True
        result["error"] = None
        return result
    except Exception as exc:  # noqa: BLE001
        out = dict(empty)
        out["ok"] = False
        out["error"] = str(exc)[:200]
        return out
    finally:
        ctx.cleanup()


def _listed_ns(namespace: str) -> str | None:
    text = str(namespace or "").strip()
    return text or None


def list_events(
    env: Environment, settings: Settings | None = None, *, namespace: str = ""
) -> dict[str, Any]:
    ns = str(namespace or "").strip()
    return _read(
        env,
        settings,
        lambda client: {"events": client.list_events(_listed_ns(ns)), "namespace": ns},
        {"events": [], "namespace": ns},
    )


def list_namespaces(
    env: Environment, settings: Settings | None = None
) -> dict[str, Any]:
    return _read(
        env,
        settings,
        lambda client: {"namespaces": client.list_namespaces()},
        {"namespaces": []},
    )


def list_services(
    env: Environment, settings: Settings | None = None, *, namespace: str = ""
) -> dict[str, Any]:
    ns = str(namespace or "").strip()
    return _read(
        env,
        settings,
        lambda client: {
            "services": client.list_services(_listed_ns(ns)),
            "namespace": ns,
        },
        {"services": [], "namespace": ns},
    )


def list_ingresses(
    env: Environment, settings: Settings | None = None, *, namespace: str = ""
) -> dict[str, Any]:
    ns = str(namespace or "").strip()
    return _read(
        env,
        settings,
        lambda client: {
            "ingresses": client.list_ingresses(_listed_ns(ns)),
            "namespace": ns,
        },
        {"ingresses": [], "namespace": ns},
    )


def list_gateways(
    env: Environment, settings: Settings | None = None, *, namespace: str = ""
) -> dict[str, Any]:
    ns = str(namespace or "").strip()
    return _read(
        env,
        settings,
        lambda client: {
            "gateways": client.list_gateways(_listed_ns(ns)),
            "namespace": ns,
        },
        {"gateways": [], "namespace": ns},
    )


def list_httproutes(
    env: Environment, settings: Settings | None = None, *, namespace: str = ""
) -> dict[str, Any]:
    ns = str(namespace or "").strip()
    return _read(
        env,
        settings,
        lambda client: {
            "httproutes": client.list_httproutes(_listed_ns(ns)),
            "namespace": ns,
        },
        {"httproutes": [], "namespace": ns},
    )


def list_metallb_pools(
    env: Environment, settings: Settings | None = None
) -> dict[str, Any]:
    return _read(
        env,
        settings,
        lambda client: {"pools": client.list_metallb_pools()},
        {"pools": []},
    )


def list_storage(
    env: Environment, settings: Settings | None = None, *, namespace: str = ""
) -> dict[str, Any]:
    ns = str(namespace or "").strip()
    empty = {
        "persistentvolumeclaims": [],
        "persistentvolumes": [],
        "storageclasses": [],
        "namespace": ns,
    }

    def _load(client: K8sClient) -> dict[str, Any]:
        return {
            "persistentvolumeclaims": client.list_pvcs(_listed_ns(ns)),
            "persistentvolumes": client.list_pvs(),
            "storageclasses": client.list_storageclasses(),
            "namespace": ns,
        }

    return _read(env, settings, _load, empty)


def list_pvs(env: Environment, settings: Settings | None = None) -> dict[str, Any]:
    return _read(
        env,
        settings,
        lambda client: {"persistentvolumes": client.list_pvs()},
        {"persistentvolumes": []},
    )


def list_storageclasses(
    env: Environment, settings: Settings | None = None
) -> dict[str, Any]:
    return _read(
        env,
        settings,
        lambda client: {"storageclasses": client.list_storageclasses()},
        {"storageclasses": []},
    )


def list_configmaps(
    env: Environment, settings: Settings | None = None, *, namespace: str = ""
) -> dict[str, Any]:
    ns = str(namespace or "").strip()
    return _read(
        env,
        settings,
        lambda client: {
            "configmaps": client.list_configmaps(_listed_ns(ns)),
            "namespace": ns,
        },
        {"configmaps": [], "namespace": ns},
    )


def list_secrets(
    env: Environment, settings: Settings | None = None, *, namespace: str = ""
) -> dict[str, Any]:
    """Secret names/types/keys only — never values."""
    ns = str(namespace or "").strip()
    return _read(
        env,
        settings,
        lambda client: {
            "secrets": client.list_secrets(_listed_ns(ns)),
            "namespace": ns,
        },
        {"secrets": [], "namespace": ns},
    )


def list_jobs(
    env: Environment, settings: Settings | None = None, *, namespace: str = ""
) -> dict[str, Any]:
    ns = str(namespace or "").strip()
    return _read(
        env,
        settings,
        lambda client: {"jobs": client.list_jobs(_listed_ns(ns)), "namespace": ns},
        {"jobs": [], "namespace": ns},
    )


def list_helm(env: Environment, settings: Settings | None = None) -> dict[str, Any]:
    return _read(
        env,
        settings,
        lambda client: {"releases": client.list_helm_releases()},
        {"releases": []},
    )


def list_nodes(env: Environment, settings: Settings | None = None) -> dict[str, Any]:
    return _read(
        env,
        settings,
        lambda client: {"nodes": client.list_nodes()},
        {"nodes": []},
    )


def helm_history(
    env: Environment,
    settings: Settings | None = None,
    *,
    namespace: str,
    name: str,
) -> dict[str, Any]:
    return _read(
        env,
        settings,
        lambda client: {
            "history": client.helm_history(namespace, name),
            "namespace": namespace,
            "name": name,
        },
        {"history": [], "namespace": namespace, "name": name},
    )


def helm_status(
    env: Environment,
    settings: Settings | None = None,
    *,
    namespace: str,
    name: str,
) -> dict[str, Any]:
    return _read(
        env,
        settings,
        lambda client: {
            "status": client.helm_status(namespace, name),
            "namespace": namespace,
            "name": name,
        },
        {"status": None, "namespace": namespace, "name": name},
    )


def _mutate(
    env: Environment,
    settings: Settings | None,
    fn: Callable[[K8sClient], Any],
    *,
    message: str,
) -> dict[str, Any]:
    settings = settings or get_settings()
    ctx = build_context(env, settings)
    try:
        if ctx.dry_run:
            return {"ok": True, "dry_run": True, "error": None, "message": message}
        kube = ctx.kubeconfig
        if not kube:
            return {
                "ok": False,
                "dry_run": False,
                "error": "no kubeconfig",
                "message": None,
            }
        with K8sClient(kube) as client:
            payload = fn(client)
        result: dict[str, Any] = {
            "ok": True,
            "dry_run": False,
            "error": None,
            "message": message,
        }
        if isinstance(payload, dict):
            result.update(payload)
            result["dry_run"] = False
            result.setdefault("ok", True)
            result.setdefault("message", message)
            result.setdefault("error", None)
        return result
    except K8sError as exc:
        return {"ok": False, "dry_run": False, "error": str(exc)[:200], "message": None}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "dry_run": False, "error": str(exc)[:200], "message": None}
    finally:
        ctx.cleanup()


def scale_workload(
    env: Environment,
    settings: Settings | None = None,
    *,
    kind: str,
    namespace: str,
    name: str,
    replicas: int,
) -> dict[str, Any]:
    return _mutate(
        env,
        settings,
        lambda client: client.scale(kind, namespace, name, replicas),
        message=f"scale {kind}/{namespace}/{name} to {replicas}",
    )


def restart_workload(
    env: Environment,
    settings: Settings | None = None,
    *,
    kind: str,
    namespace: str,
    name: str,
) -> dict[str, Any]:
    return _mutate(
        env,
        settings,
        lambda client: client.restart(kind, namespace, name),
        message=f"restart {kind}/{namespace}/{name}",
    )


def delete_pod(
    env: Environment,
    settings: Settings | None = None,
    *,
    namespace: str,
    name: str,
) -> dict[str, Any]:
    return _mutate(
        env,
        settings,
        lambda client: client.delete_pod(namespace, name),
        message=f"delete pod {namespace}/{name}",
    )


def delete_workload(
    env: Environment,
    settings: Settings | None = None,
    *,
    kind: str,
    namespace: str,
    name: str,
) -> dict[str, Any]:
    return _mutate(
        env,
        settings,
        lambda client: client.delete_workload(kind, namespace, name),
        message=f"delete {kind}/{namespace}/{name}",
    )


def delete_service(
    env: Environment,
    settings: Settings | None = None,
    *,
    namespace: str,
    name: str,
) -> dict[str, Any]:
    return _mutate(
        env,
        settings,
        lambda client: client.delete_service(namespace, name),
        message=f"delete service {namespace}/{name}",
    )


def delete_pvc(
    env: Environment,
    settings: Settings | None = None,
    *,
    namespace: str,
    name: str,
) -> dict[str, Any]:
    return _mutate(
        env,
        settings,
        lambda client: client.delete_pvc(namespace, name),
        message=f"delete pvc {namespace}/{name}",
    )


def create_namespace(
    env: Environment,
    settings: Settings | None = None,
    *,
    name: str,
    labels: dict[str, str] | None = None,
) -> dict[str, Any]:
    return _mutate(
        env,
        settings,
        lambda client: client.create_namespace(name, labels),
        message=f"create namespace {name}",
    )


def delete_namespace(
    env: Environment,
    settings: Settings | None = None,
    *,
    name: str,
) -> dict[str, Any]:
    return _mutate(
        env,
        settings,
        lambda client: client.delete_namespace(name),
        message=f"delete namespace {name}",
    )


def add_taint(
    env: Environment,
    settings: Settings | None = None,
    *,
    name: str,
    key: str,
    value: str,
    effect: str,
) -> dict[str, Any]:
    err = validate_taint(key, value, effect)
    if err:
        return {"ok": False, "dry_run": False, "error": err, "message": None}
    return _mutate(
        env,
        settings,
        lambda client: client.add_taint(name, key, value, effect),
        message=f"taint node {name} {key}:{effect}",
    )


def remove_taint(
    env: Environment,
    settings: Settings | None = None,
    *,
    name: str,
    key: str,
    effect: str,
) -> dict[str, Any]:
    err = validate_taint(key, "", effect, require_value=False)
    if err:
        return {"ok": False, "dry_run": False, "error": err, "message": None}
    return _mutate(
        env,
        settings,
        lambda client: client.remove_taint(name, key, effect),
        message=f"untaint node {name} {key}:{effect}",
    )


def set_label(
    env: Environment,
    settings: Settings | None = None,
    *,
    name: str,
    key: str,
    value: str,
) -> dict[str, Any]:
    err = validate_label(key, value)
    if err:
        return {"ok": False, "dry_run": False, "error": err, "message": None}
    return _mutate(
        env,
        settings,
        lambda client: client.set_label(name, key, value),
        message=f"label node {name} {key}",
    )


def apply_yaml(
    env: Environment,
    settings: Settings | None = None,
    *,
    text: str,
) -> dict[str, Any]:
    settings = settings or get_settings()
    ctx = build_context(env, settings)
    try:
        kube = ctx.kubeconfig
        if ctx.dry_run and not kube:
            docs = parse_yaml_docs(text)
            applied = [
                {
                    "kind": d.get("kind"),
                    "namespace": ((d.get("metadata") or {}).get("namespace") or ""),
                    "name": ((d.get("metadata") or {}).get("name") or ""),
                }
                for d in docs
            ]
            return {
                "ok": True,
                "dry_run": True,
                "error": None,
                "message": f"apply yaml ({len(applied)} object(s))",
                "applied": applied,
            }
        if not kube:
            return {
                "ok": False,
                "dry_run": False,
                "error": "no kubeconfig",
                "message": None,
            }
        with K8sClient(kube) as client:
            payload = client.apply_yaml(text, dry_run=ctx.dry_run)
        result = {
            "ok": True,
            "dry_run": ctx.dry_run,
            "error": None,
            "message": "apply yaml",
        }
        if isinstance(payload, dict):
            result.update(payload)
            result["dry_run"] = ctx.dry_run
        return result
    except K8sError as exc:
        return {
            "ok": False,
            "dry_run": bool(ctx.dry_run),
            "error": str(exc)[:200],
            "message": None,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "dry_run": bool(ctx.dry_run),
            "error": str(exc)[:200],
            "message": None,
        }
    finally:
        ctx.cleanup()


def helm_rollback(
    env: Environment,
    settings: Settings | None = None,
    *,
    namespace: str,
    name: str,
    revision: int | None = None,
) -> dict[str, Any]:
    rev = f" to {revision}" if revision is not None else ""
    return _mutate(
        env,
        settings,
        lambda client: client.helm_rollback(namespace, name, revision),
        message=f"helm rollback {namespace}/{name}{rev}",
    )


def helm_upgrade(
    env: Environment,
    settings: Settings | None = None,
    *,
    namespace: str,
    name: str,
    chart: str | None = None,
    values: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if chart:
        err = validate_chart(chart)
        if err:
            return {"ok": False, "dry_run": False, "error": err, "message": None}
    return _mutate(
        env,
        settings,
        lambda client: client.helm_upgrade(namespace, name, chart=chart, values=values),
        message=f"helm upgrade {namespace}/{name}",
    )


def set_node_schedulable(
    env: Environment,
    settings: Settings | None = None,
    *,
    name: str,
    unschedulable: bool,
) -> dict[str, Any]:
    action = "cordon" if unschedulable else "uncordon"
    return _mutate(
        env,
        settings,
        lambda client: client.set_unschedulable(name, unschedulable),
        message=f"{action} node {name}",
    )


def drain_node(
    env: Environment,
    settings: Settings | None = None,
    *,
    name: str,
    ignore_daemonsets: bool = True,
    delete_emptydir: bool = False,
    grace_period: int = 30,
    timeout: int = 90,
) -> dict[str, Any]:
    err = validate_name(name)
    if err:
        return {
            "ok": False,
            "cordoned": False,
            "evicted": [],
            "skipped": [],
            "blocked": [],
            "error": err,
        }
    return _mutate(
        env,
        settings,
        lambda client: client.drain_node(
            name,
            ignore_daemonsets=ignore_daemonsets,
            delete_emptydir=delete_emptydir,
            grace_period=grace_period,
            timeout=timeout,
        ),
        message=f"drain node {name}",
    )


def describe(
    env: Environment,
    settings: Settings | None = None,
    *,
    kind: str,
    namespace: str = "",
    name: str,
) -> dict[str, Any]:
    settings = settings or get_settings()
    empty: dict[str, Any] = {"ok": False, "object": None, "events": [], "error": None}
    ctx = build_context(env, settings)
    try:
        kube = ctx.kubeconfig
        if not kube:
            empty["error"] = "no kubeconfig"
            return empty
        with K8sClient(kube) as client:
            return client.describe(kind, namespace or None, name)
    except Exception as exc:  # noqa: BLE001
        empty["error"] = str(exc)[:200]
        return empty
    finally:
        ctx.cleanup()


__all__ = [
    "CLUSTER_DESCRIBE_KINDS",
    "DELETE_WORKLOAD_KINDS",
    "DESCRIBE_KINDS",
    "RESTART_KINDS",
    "SCALE_KINDS",
    "add_taint",
    "apply_yaml",
    "create_namespace",
    "delete_namespace",
    "delete_pod",
    "delete_pvc",
    "delete_service",
    "delete_workload",
    "describe",
    "drain_node",
    "helm_history",
    "helm_rollback",
    "helm_status",
    "helm_upgrade",
    "list_configmaps",
    "list_events",
    "list_helm",
    "list_ingresses",
    "list_jobs",
    "list_namespaces",
    "list_nodes",
    "list_pvs",
    "list_secrets",
    "list_services",
    "list_storage",
    "list_storageclasses",
    "list_workloads",
    "remove_taint",
    "restart_workload",
    "scale_workload",
    "set_label",
    "set_node_schedulable",
    "validate_name",
    "validate_namespace",
]
