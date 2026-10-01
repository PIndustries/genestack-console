"""Live read-only cluster/OpenStack views via kubectl/helm — never raise.

Used by the /api/v1/environments/{id}/cluster and /openstack endpoints.
OpenStack data comes from the in-cluster ``openstack-admin-client`` pod
(docs/openstack-keystone.md); if the pod is missing it is created from
``<config_dir>/manifests/utils/utils-openstack-client-admin.yaml``.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import yaml

from app.services.cluster import _kube_env, _node_roles, _node_status, _parse_json_list
from app.services.envcontext import EnvContext
from app.services.logredact import redact_secret_line

PROBE_TIMEOUT = 10
EXEC_TIMEOUT = 15
POD_WAIT_TIMEOUT = 60
MAX_PROBLEM_PODS = 25
MAX_WARNINGS = 20
LOG_TIMEOUT = 15
LOG_TAIL_DEFAULT = 200
LOG_TAIL_MAX = 2000

# Observability extras (and tempest). Failed status must not flip cluster health.
OPTIONAL_HELM = frozenset(
    {
        "kube-prometheus-stack",
        "prometheus",
        "prometheus-operator",
        "grafana",
        "loki",
        "tempo",
        "prometheus-pushgateway",
        "fluentbit",
        "openstack-exporter",
        "opentelemetry-kube-stack",
        "barbican-exporter",
        "tempest",
        "alertmanager",
    }
)

# RFC 1123 subdomain (pod names may contain dots).
_K8S_NAME_RE = re.compile(
    r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*$"
)
_K8S_NS_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")

OPENSTACK_NAMESPACE = "openstack"
ADMIN_CLIENT_POD = "openstack-admin-client"
ADMIN_CLIENT_MANIFEST = "manifests/utils/utils-openstack-client-admin.yaml"


def _run(
    cmd: list[str],
    *,
    env: dict[str, str] | None = None,
    timeout: int = PROBE_TIMEOUT,
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


# ------------------------------------------------------------ cluster view


def _pod_problem(pod: dict[str, Any]) -> dict[str, Any] | None:
    """Problem row for a pod, or None when the pod is Running-ready/Completed."""
    meta = pod.get("metadata") or {}
    status = pod.get("status") or {}
    phase = status.get("phase") or "Unknown"
    if phase == "Succeeded":
        return None
    reason: str | None = None
    unready = False
    for cs in status.get("containerStatuses") or []:
        state = cs.get("state") or {}
        waiting = (state.get("waiting") or {}).get("reason")
        if waiting:
            reason = reason or waiting
        terminated = state.get("terminated") or {}
        if terminated.get("reason") and terminated["reason"] != "Completed":
            reason = reason or terminated["reason"]
        if not cs.get("ready"):
            unready = True
    if phase == "Running" and not unready and reason is None:
        return None
    return {
        "namespace": meta.get("namespace"),
        "name": meta.get("name"),
        "status": phase,
        "reason": reason or ("ContainersNotReady" if phase == "Running" else phase),
    }


def _pods_summary(pods_data: dict[str, Any]) -> dict[str, Any]:
    items = pods_data.get("items") or []
    running = 0
    problems: list[dict[str, Any]] = []
    for pod in items:
        if ((pod.get("status") or {}).get("phase") or "Unknown") == "Running":
            running += 1
        problem = _pod_problem(pod)
        if problem is not None and len(problems) < MAX_PROBLEM_PODS:
            problems.append(problem)
    return {"total": len(items), "running": running, "problems": problems}


def _kubeconfig_server(path: Path) -> str | None:
    """Return clusters[0].cluster.server from a kubeconfig, or None."""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(data, dict):
        return None
    clusters = data.get("clusters")
    if not isinstance(clusters, list) or not clusters:
        return None
    first = clusters[0] if isinstance(clusters[0], dict) else {}
    cluster = first.get("cluster") if isinstance(first.get("cluster"), dict) else {}
    server = cluster.get("server")
    return str(server) if server else None


def _horizon_url_from_files(ctx: EnvContext) -> str | None:
    """https://horizon.<domain> from the env overlay HTTPRoute, if present."""
    if ctx.config_dir is None:
        return None
    route = (
        ctx.config_dir / "gateway-api" / "routes" / "custom-horizon-gateway-route.yaml"
    )
    if not route.is_file():
        return None
    try:
        data = yaml.safe_load(route.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(data, dict):
        return None
    hosts = (data.get("spec") or {}).get("hostnames") or []
    if not hosts:
        return None
    host = str(hosts[0]).strip()
    if not host:
        return None
    return f"https://{host}"


def _cluster_access(ctx: EnvContext) -> dict[str, Any]:
    kube_path = Path(ctx.kubeconfig) if ctx.kubeconfig else None
    kube_exists = bool(kube_path is not None and kube_path.is_file())
    talos_path = (
        ctx.config_dir / "talos" / "talosconfig" if ctx.config_dir is not None else None
    )
    api_server = (
        _kubeconfig_server(kube_path) if kube_path is not None and kube_exists else None
    )
    return {
        "api_server": api_server,
        "kubeconfig": kube_exists,
        "talosconfig": bool(talos_path is not None and talos_path.is_file()),
        "gateway": None,
        "horizon": _horizon_url_from_files(ctx),
    }


def _warnings_from_events(data: dict[str, Any]) -> list[dict[str, Any]]:
    items = [ev for ev in (data.get("items") or []) if ev.get("type") == "Warning"]
    items.sort(key=lambda ev: ev.get("lastTimestamp") or "", reverse=True)
    warnings: list[dict[str, Any]] = []
    for ev in items[:MAX_WARNINGS]:
        meta = ev.get("metadata") or {}
        inv = ev.get("involvedObject") or {}
        kind = inv.get("kind")
        name = inv.get("name")
        warnings.append(
            {
                "namespace": meta.get("namespace"),
                "name": meta.get("name"),
                "message": ev.get("message"),
                "count": ev.get("count"),
                "last_seen": ev.get("lastTimestamp"),
                "object": f"{kind}/{name}" if kind and name else None,
            }
        )
    return warnings


def _mem_to_gi(raw: Any) -> float | None:
    """Parse a Kubernetes memory quantity into GiB, or None."""
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        if text.endswith("Ki"):
            return round(int(text[:-2]) / 1024 / 1024, 1)
        if text.endswith("Mi"):
            return round(int(text[:-2]) / 1024, 1)
        if text.endswith("Gi"):
            return round(float(text[:-2]), 1)
        if text.endswith("Ti"):
            return round(float(text[:-2]) * 1024, 1)
        return round(int(text) / (1024**3), 1)
    except (TypeError, ValueError):
        return None


def _is_optional_helm(rel: dict[str, Any]) -> bool:
    name = str(rel.get("name") or "").lower()
    chart = str(rel.get("chart") or "").lower()
    if name in OPTIONAL_HELM:
        return True
    return any(chart == opt or chart.startswith(f"{opt}-") for opt in OPTIONAL_HELM)


def _cluster_health(result: dict[str, Any], *, has_kubeconfig: bool) -> tuple[str, str]:
    if not has_kubeconfig:
        return "unknown", "no kubeconfig"
    if not result.get("reachable"):
        return "down", str(result.get("error") or "unreachable")
    problems = (result.get("pods") or {}).get("problems") or []
    if problems:
        n = len(problems)
        return "degraded", f"{n} problem pod{'s' if n != 1 else ''}"
    not_deployed = [
        rel
        for rel in (result.get("releases") or [])
        if (rel.get("status") or "").lower() != "deployed"
    ]
    required = [rel for rel in not_deployed if not _is_optional_helm(rel)]
    if required:
        names = ", ".join(str(rel.get("name") or "?") for rel in required[:3])
        extra = f" +{len(required) - 3}" if len(required) > 3 else ""
        return "degraded", f"helm not deployed: {names}{extra}"
    return "healthy", ""


def _optional_helm_warnings(releases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = []
    for rel in releases:
        if (rel.get("status") or "").lower() == "deployed":
            continue
        if not _is_optional_helm(rel):
            continue
        name = rel.get("name") or "?"
        warnings.append(
            {
                "namespace": rel.get("namespace"),
                "name": name,
                "message": (
                    f"optional helm release not deployed (status={rel.get('status')})"
                ),
                "count": 1,
                "last_seen": None,
                "object": f"HelmRelease/{name}",
            }
        )
    return warnings


def _empty_cluster(ctx: EnvContext, *, error: str | None = None) -> dict[str, Any]:
    has_kubeconfig = bool(ctx.kubeconfig)
    health, reason = _cluster_health(
        {"reachable": False, "error": error, "pods": {}, "releases": []},
        has_kubeconfig=has_kubeconfig,
    )
    return {
        "reachable": False,
        "nodes": [],
        "pods": {"total": 0, "running": 0, "problems": []},
        "releases": [],
        "error": error,
        "health": health,
        "health_reason": reason,
        "warnings": [],
        "access": _cluster_access(ctx),
        "resources": {"cpu": 0, "memory_gi": 0.0},
    }


def _cluster_overview(ctx: EnvContext) -> dict[str, Any]:
    result = _empty_cluster(ctx)

    if not ctx.kubeconfig:
        # No kubeconfig for this env; without KUBECONFIG kubectl would
        # default to http://127.0.0.1:8080 (the console itself).
        result["error"] = "No kubeconfig is configured for this environment."
        return result

    kubectl = shutil.which("kubectl")
    if not kubectl:
        result["error"] = "kubectl not found on PATH"
        return result

    env = _kube_env(ctx.kubeconfig)
    raw_nodes, err = _run([kubectl, "get", "nodes", "-o", "json"], env=env)
    if err:
        result["error"] = err
        return result
    try:
        nodes_data = json.loads(raw_nodes or "{}")
    except json.JSONDecodeError as exc:
        result["error"] = f"invalid kubectl output: {exc}"
        return result

    result["reachable"] = True
    nodes: list[dict[str, Any]] = []
    cpu_total = 0
    mem_total = 0.0
    for n in nodes_data.get("items") or []:
        cpu = ((n.get("status") or {}).get("capacity") or {}).get("cpu")
        mem = ((n.get("status") or {}).get("capacity") or {}).get("memory")
        mem_gi = _mem_to_gi(mem)
        row: dict[str, Any] = {
            "name": (n.get("metadata") or {}).get("name"),
            "roles": _node_roles(n),
            "status": _node_status(n),
            "unschedulable": bool((n.get("spec") or {}).get("unschedulable")),
            "version": ((n.get("status") or {}).get("nodeInfo") or {}).get(
                "kubeletVersion"
            ),
            "cpu_capacity": cpu,
            "mem_capacity": mem,
            "mem_gi": mem_gi,
        }
        nodes.append(row)
        try:
            cpu_total += int(str(cpu).split()[0])
        except (TypeError, ValueError):
            pass
        if mem_gi is not None:
            mem_total += mem_gi
    result["nodes"] = nodes
    result["resources"] = {"cpu": cpu_total, "memory_gi": round(mem_total, 1)}

    # Pod/helm/event failures degrade their section only; the cluster is still reachable.
    raw_pods, err = _run([kubectl, "get", "pods", "-A", "-o", "json"], env=env)
    if not err:
        try:
            result["pods"] = _pods_summary(json.loads(raw_pods or "{}"))
        except json.JSONDecodeError:
            pass

    helm = shutil.which("helm")
    if helm:
        raw_releases, err = _run([helm, "list", "-A", "-o", "json"], env=env)
        if not err:
            result["releases"] = [
                {
                    "name": rel.get("name"),
                    "namespace": rel.get("namespace"),
                    "status": rel.get("status"),
                    "chart": rel.get("chart"),
                    "version": rel.get("app_version"),
                }
                for rel in _parse_json_list(raw_releases)
            ]

    raw_events, err = _run([kubectl, "get", "events", "-A", "-o", "json"], env=env)
    if not err:
        try:
            payload = json.loads(raw_events or "{}")
        except json.JSONDecodeError:
            payload = {}
        if isinstance(payload, dict):
            result["warnings"] = _warnings_from_events(payload)

    helm_warn = _optional_helm_warnings(result.get("releases") or [])
    if helm_warn:
        result["warnings"] = (helm_warn + (result.get("warnings") or []))[:MAX_WARNINGS]

    raw_gw, err = _run(
        [
            kubectl,
            "-n",
            "envoy-gateway",
            "get",
            "gateway",
            "flex-gateway",
            "-o",
            "json",
        ],
        env=env,
    )
    if not err:
        try:
            gw = json.loads(raw_gw or "{}")
        except json.JSONDecodeError:
            gw = {}
        addrs = (gw.get("status") or {}).get("addresses") or []
        if addrs and isinstance(addrs[0], dict) and addrs[0].get("value"):
            result["access"]["gateway"] = str(addrs[0]["value"])
    if not result["access"].get("horizon"):
        raw_rt, err = _run(
            [
                kubectl,
                "-n",
                "openstack",
                "get",
                "httproute",
                "custom-horizon-gateway-route",
                "-o",
                "json",
            ],
            env=env,
        )
        if not err:
            try:
                rt = json.loads(raw_rt or "{}")
            except json.JSONDecodeError:
                rt = {}
            hosts = (rt.get("spec") or {}).get("hostnames") or []
            if hosts:
                result["access"]["horizon"] = f"https://{hosts[0]}"

    health, reason = _cluster_health(result, has_kubeconfig=True)
    result["health"] = health
    result["health_reason"] = reason
    return result


def cluster_overview(ctx: EnvContext) -> dict[str, Any]:
    """Live nodes/pods/helm view for one environment. Never raises."""
    try:
        return _cluster_overview(ctx)
    except Exception as exc:  # noqa: BLE001
        return _empty_cluster(ctx, error=str(exc))


def _valid_k8s_name(value: str, *, namespace: bool = False) -> bool:
    text = str(value or "")
    if namespace:
        return bool(text) and len(text) <= 63 and _K8S_NS_RE.match(text) is not None
    return bool(text) and len(text) <= 253 and _K8S_NAME_RE.match(text) is not None


def cluster_pod_logs(
    ctx: EnvContext,
    *,
    namespace: str,
    pod: str,
    container: str | None = None,
    tail: int = LOG_TAIL_DEFAULT,
    previous: bool = False,
) -> dict[str, Any]:
    """Fetch kubectl logs. Never raises."""
    result: dict[str, Any] = {
        "namespace": namespace,
        "pod": pod,
        "container": container,
        "tail": tail,
        "previous": previous,
        "text": "",
        "error": None,
    }
    try:
        try:
            tail_n = int(tail)
        except (TypeError, ValueError):
            tail_n = LOG_TAIL_DEFAULT
        tail_n = max(1, min(LOG_TAIL_MAX, tail_n))
        result["tail"] = tail_n

        if not _valid_k8s_name(namespace, namespace=True) or not _valid_k8s_name(pod):
            result["error"] = "invalid namespace/pod name"
            return result
        if container and not _valid_k8s_name(container):
            result["error"] = "invalid container name"
            return result

        if not ctx.kubeconfig:
            result["error"] = "No kubeconfig is configured for this environment."
            return result

        kubectl = shutil.which("kubectl")
        if not kubectl:
            result["error"] = "kubectl not found on PATH"
            return result

        cmd = [kubectl, "logs", "-n", namespace, pod, f"--tail={tail_n}"]
        if container:
            cmd.extend(["-c", container])
        if previous:
            cmd.append("--previous")
        stdout, err = _run(cmd, env=_kube_env(ctx.kubeconfig), timeout=LOG_TIMEOUT)
        if err:
            result["error"] = err
            return result
        lines = [
            (redact_secret_line(line) if line else line)
            for line in (stdout or "").splitlines()
        ]
        result["text"] = "\n".join(lines)
        return result
    except Exception as exc:  # noqa: BLE001
        result["error"] = str(exc)
        return result


# ------------------------------------------------------------ openstack view


def _field(item: dict[str, Any], *names: str) -> Any:
    """Case-insensitive lookup across alternate openstack CLI column names."""
    lowered = {str(key).lower(): value for key, value in item.items()}
    for name in names:
        if name.lower() in lowered:
            return lowered[name.lower()]
    return None


def _admin_client_ready(kubectl: str, env: dict[str, str] | None) -> bool:
    raw, err = _run(
        [
            kubectl,
            "-n",
            OPENSTACK_NAMESPACE,
            "get",
            "pod",
            ADMIN_CLIENT_POD,
            "-o",
            "json",
        ],
        env=env,
    )
    if err:
        return False
    try:
        pod = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return False
    status = pod.get("status") or {}
    if status.get("phase") != "Running":
        return False
    for cond in status.get("conditions") or []:
        if cond.get("type") == "Ready":
            return cond.get("status") == "True"
    return False


def _ensure_admin_client(
    kubectl: str, env: dict[str, str] | None, ctx: EnvContext
) -> str | None:
    """Apply the admin client manifest and wait for Ready. Returns an error or None."""
    manifest = None
    if ctx.config_dir is not None:
        candidate = ctx.config_dir / ADMIN_CLIENT_MANIFEST
        if candidate.is_file():
            manifest = candidate
    if manifest is None:
        return (
            f"admin client pod '{ADMIN_CLIENT_POD}' not ready in namespace "
            f"'{OPENSTACK_NAMESPACE}' and no {ADMIN_CLIENT_MANIFEST} manifest available"
        )
    _, err = _run([kubectl, "apply", "-f", str(manifest)], env=env)
    if err:
        return f"failed to create admin client pod: {err}"
    _, err = _run(
        [
            kubectl,
            "-n",
            OPENSTACK_NAMESPACE,
            "wait",
            "--for=condition=Ready",
            f"pod/{ADMIN_CLIENT_POD}",
            f"--timeout={POD_WAIT_TIMEOUT}s",
        ],
        env=env,
        timeout=POD_WAIT_TIMEOUT + 10,
    )
    if err:
        return f"admin client pod did not become Ready: {err}"
    return None


def _openstack_exec(
    kubectl: str, env: dict[str, str] | None, args: list[str]
) -> list[Any]:
    """Run one openstack CLI command in the admin client pod; [] on any failure."""
    raw, err = _run(
        [
            kubectl,
            "-n",
            OPENSTACK_NAMESPACE,
            "exec",
            ADMIN_CLIENT_POD,
            "--",
            "openstack",
            *args,
            "-f",
            "json",
        ],
        env=env,
        timeout=EXEC_TIMEOUT,
    )
    if err:
        return []
    return _parse_json_list(raw)


def _openstack_overview(ctx: EnvContext) -> dict[str, Any]:
    result: dict[str, Any] = {
        "available": False,
        "source": "admin-client-pod",
        "users": [],
        "compute_services": [],
        "network_agents": [],
        "images": [],
        "error": None,
    }

    kubectl = shutil.which("kubectl")
    if not kubectl:
        result["error"] = "kubectl not found on PATH"
        return result

    env = _kube_env(ctx.kubeconfig)
    if not _admin_client_ready(kubectl, env):
        err = _ensure_admin_client(kubectl, env, ctx)
        if err is not None:
            result["error"] = err
            return result

    result["available"] = True
    result["users"] = [
        {"name": _field(u, "Name")}
        for u in _openstack_exec(kubectl, env, ["user", "list"])
    ]
    result["compute_services"] = [
        {
            "name": _field(s, "Binary", "Name"),
            "host": _field(s, "Host"),
            "zone": _field(s, "Zone"),
            "status": _field(s, "Status"),
            "state": _field(s, "State"),
        }
        for s in _openstack_exec(kubectl, env, ["compute", "service", "list"])
    ]
    result["network_agents"] = [
        {
            "type": _field(a, "Agent Type", "Binary"),
            "host": _field(a, "Host"),
            "alive": _field(a, "Alive"),
            "state": _field(a, "State"),
        }
        for a in _openstack_exec(kubectl, env, ["network", "agent", "list"])
    ]
    result["images"] = [
        {"name": _field(i, "Name"), "status": _field(i, "Status")}
        for i in _openstack_exec(kubectl, env, ["image", "list"])
    ]
    return result


def openstack_overview(ctx: EnvContext) -> dict[str, Any]:
    """Live OpenStack view via the in-cluster admin client pod. Never raises."""
    try:
        return _openstack_overview(ctx)
    except Exception as exc:  # noqa: BLE001
        return {
            "available": False,
            "source": "admin-client-pod",
            "users": [],
            "compute_services": [],
            "network_agents": [],
            "images": [],
            "error": str(exc),
        }
