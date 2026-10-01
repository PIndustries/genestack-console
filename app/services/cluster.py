"""Cluster probes via kubectl/helm — always return a dict, never raise."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Any

PROBE_TIMEOUT = 5

NO_KUBECONFIG = "No kubeconfig is configured for this environment."


def _kube_env(kubeconfig_path: str | None) -> dict[str, str] | None:
    if not kubeconfig_path:
        return None
    return {**os.environ, "KUBECONFIG": kubeconfig_path}


def _run(
    cmd: list[str], *, env: dict[str, str] | None = None
) -> tuple[str | None, str | None]:
    """Run a probe command. Returns (stdout, error)."""
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT,
            env=env,
            check=False,
        )
    except FileNotFoundError:
        return None, f"executable not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return None, f"timed out after {PROBE_TIMEOUT}s"
    except OSError as exc:
        return None, str(exc)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        return None, (detail[:200] or f"exit code {proc.returncode}")
    return proc.stdout or "", None


def _parse_json_list(raw: str | None) -> list[Any]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def _node_status(node: dict[str, Any]) -> str:
    for cond in (node.get("status") or {}).get("conditions") or []:
        if cond.get("type") == "Ready":
            return "Ready" if cond.get("status") == "True" else "NotReady"
    return "Unknown"


def _node_roles(node: dict[str, Any]) -> str:
    labels = (node.get("metadata") or {}).get("labels") or {}
    roles = sorted(
        key.removeprefix("node-role.kubernetes.io/")
        for key in labels
        if key.startswith("node-role.kubernetes.io/")
    )
    return ",".join(roles) if roles else "<none>"


def cluster_status(kubeconfig_path: str | None) -> dict[str, Any]:
    """Probe cluster reachability, nodes, and namespaces via kubectl."""
    result: dict[str, Any] = {
        "reachable": False,
        "nodes": [],
        "namespaces": [],
        "kubeconfig": kubeconfig_path or "default",
        "error": None,
    }

    if not kubeconfig_path:
        # Without a KUBECONFIG, kubectl's legacy default target is
        # http://127.0.0.1:8080 — which, inside this container, is the
        # console itself. Skip the probe instead of self-polling.
        result["error"] = NO_KUBECONFIG
        return result

    kubectl = shutil.which("kubectl")
    if not kubectl:
        result["error"] = "kubectl not found on PATH"
        return result

    env = _kube_env(kubeconfig_path)
    raw_nodes, err = _run([kubectl, "get", "nodes", "-o", "json"], env=env)
    if err:
        result["error"] = err
        return result
    raw_ns, err = _run([kubectl, "get", "ns", "-o", "json"], env=env)
    if err:
        result["error"] = err
        return result

    try:
        nodes_data = json.loads(raw_nodes or "{}")
        ns_data = json.loads(raw_ns or "{}")
    except json.JSONDecodeError as exc:
        result["error"] = f"invalid kubectl output: {exc}"
        return result

    result["reachable"] = True
    result["nodes"] = [
        {
            "name": (n.get("metadata") or {}).get("name"),
            "status": _node_status(n),
            "roles": _node_roles(n),
            "version": ((n.get("status") or {}).get("nodeInfo") or {}).get(
                "kubeletVersion"
            ),
        }
        for n in nodes_data.get("items") or []
    ]
    result["namespaces"] = [
        (ns.get("metadata") or {}).get("name") for ns in ns_data.get("items") or []
    ]
    return result


def _pod_counts(pods: dict[str, Any], release: str, namespace: str) -> dict[str, int]:
    ready = 0
    total = 0
    for pod in pods.get("items") or []:
        meta = pod.get("metadata") or {}
        name = meta.get("name") or ""
        if meta.get("namespace") != namespace or not (
            name == release or name.startswith(f"{release}-")
        ):
            continue
        containers = (pod.get("status") or {}).get("containerStatuses") or []
        total += len(containers)
        ready += sum(1 for c in containers if c.get("ready"))
    return {"ready": ready, "total": total}


def services_status(kubeconfig_path: str | None) -> dict[str, Any]:
    """List helm releases joined with pod readiness from kubectl."""
    result: dict[str, Any] = {"reachable": False, "releases": [], "error": None}
    if not kubeconfig_path:
        result["error"] = NO_KUBECONFIG
        return result

    helm = shutil.which("helm")
    if not helm:
        result["error"] = "helm not found on PATH"
        return result
    kubectl = shutil.which("kubectl")
    if not kubectl:
        result["error"] = "kubectl not found on PATH"
        return result

    env = _kube_env(kubeconfig_path)
    raw_releases, err = _run([helm, "list", "-A", "-o", "json"], env=env)
    if err:
        result["error"] = err
        return result
    raw_pods, err = _run([kubectl, "get", "pods", "-A", "-o", "json"], env=env)
    if err:
        result["error"] = err
        return result

    try:
        pods_data = json.loads(raw_pods or "{}")
    except json.JSONDecodeError:
        pods_data = {}
    releases = _parse_json_list(raw_releases)

    result["reachable"] = True
    result["releases"] = [
        {
            "name": rel.get("name"),
            "namespace": rel.get("namespace"),
            "revision": rel.get("revision"),
            "status": rel.get("status"),
            "chart": rel.get("chart"),
            "pods": _pod_counts(
                pods_data, str(rel.get("name") or ""), str(rel.get("namespace") or "")
            ),
        }
        for rel in releases
    ]
    return result
