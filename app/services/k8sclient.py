"""Native Kubernetes REST from the console process (kube-apiserver).

Same mTLS path as OpenStackClient: ``load_kube_http`` builds an httpx client
with SSLContext.load_cert_chain. Do not pass verify=(ca, cert) — that 401s.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import yaml

from app.services.envcontext import EnvContext
from app.services.logredact import redact_secret_line
from app.services.osclient import load_kube_http

RESTART_ANNOTATION = "kubectl.kubernetes.io/restartedAt"
MIRROR_ANNOTATION = "kubernetes.io/config.mirror"
PATCH_CONTENT_TYPE = "application/strategic-merge-patch+json"
MERGE_PATCH_CONTENT_TYPE = "application/merge-patch+json"
APPLY_CONTENT_TYPE = "application/apply-patch+yaml"
APPLY_FIELD_MANAGER = "genestack-console"
HELM_RELEASE_TYPE = "helm.sh/release.v1"
HELM_TIMEOUT = 90
HELM_MUTATE_TIMEOUT = 180
EVENTS_LIMIT = 250

SCALE_KINDS = frozenset({"deployments", "statefulsets"})
RESTART_KINDS = frozenset({"deployments", "statefulsets", "daemonsets"})
DELETE_WORKLOAD_KINDS = frozenset({"deployments", "statefulsets", "daemonsets"})
CLUSTER_DESCRIBE_KINDS = frozenset(
    {"nodes", "namespaces", "persistentvolumes", "storageclasses"}
)
DESCRIBE_KINDS = frozenset(
    {
        "pods",
        "nodes",
        "deployments",
        "statefulsets",
        "daemonsets",
        "services",
        "namespaces",
        "ingresses",
        "persistentvolumeclaims",
        "persistentvolumes",
        "storageclasses",
        "configmaps",
        "secrets",
        "jobs",
    }
)
APPS_KINDS = frozenset({"deployments", "statefulsets", "daemonsets"})
TAINT_EFFECTS = frozenset({"NoSchedule", "PreferNoSchedule", "NoExecute"})
PROTECTED_NAMESPACES = frozenset(
    {"default", "kube-system", "kube-public", "kube-node-lease"}
)

# RFC 1123 DNS label (namespaces) / subdomain (names, node FQDNs).
NS_RE = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")
NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?$")
LABEL_NAME_RE = re.compile(r"^[A-Za-z0-9]([-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?$")
LABEL_PREFIX_RE = re.compile(
    r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?(\.[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?)*$"
)
LABEL_VALUE_RE = re.compile(r"^([A-Za-z0-9]([-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?)?$")
CHART_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,251}$")

KIND_RESOURCE = {
    "Namespace": "namespaces",
    "Node": "nodes",
    "Pod": "pods",
    "Service": "services",
    "ConfigMap": "configmaps",
    "Secret": "secrets",
    "PersistentVolumeClaim": "persistentvolumeclaims",
    "PersistentVolume": "persistentvolumes",
    "Endpoints": "endpoints",
    "EndpointSlice": "endpointslices",
    "ServiceAccount": "serviceaccounts",
    "Event": "events",
    "LimitRange": "limitranges",
    "ResourceQuota": "resourcequotas",
    "ReplicationController": "replicationcontrollers",
    "Deployment": "deployments",
    "StatefulSet": "statefulsets",
    "DaemonSet": "daemonsets",
    "ReplicaSet": "replicasets",
    "Job": "jobs",
    "CronJob": "cronjobs",
    "Ingress": "ingresses",
    "IngressClass": "ingressclasses",
    "NetworkPolicy": "networkpolicies",
    "StorageClass": "storageclasses",
    "CSIDriver": "csidrivers",
    "CSINode": "csinodes",
    "VolumeAttachment": "volumeattachments",
    "PodDisruptionBudget": "poddisruptionbudgets",
    "HorizontalPodAutoscaler": "horizontalpodautoscalers",
    "Role": "roles",
    "RoleBinding": "rolebindings",
    "ClusterRole": "clusterroles",
    "ClusterRoleBinding": "clusterrolebindings",
    "CustomResourceDefinition": "customresourcedefinitions",
    "Lease": "leases",
    "PriorityClass": "priorityclasses",
    "RuntimeClass": "runtimeclasses",
    "ValidatingWebhookConfiguration": "validatingwebhookconfigurations",
    "MutatingWebhookConfiguration": "mutatingwebhookconfigurations",
    "APIService": "apiservices",
    "ControllerRevision": "controllerrevisions",
    "CertificateSigningRequest": "certificatesigningrequests",
}

CLUSTER_SCOPED_KINDS = frozenset(
    {
        "Node",
        "Namespace",
        "PersistentVolume",
        "StorageClass",
        "ClusterRole",
        "ClusterRoleBinding",
        "CustomResourceDefinition",
        "CSIDriver",
        "CSINode",
        "VolumeAttachment",
        "PriorityClass",
        "RuntimeClass",
        "ValidatingWebhookConfiguration",
        "MutatingWebhookConfiguration",
        "APIService",
        "IngressClass",
        "CertificateSigningRequest",
        "ComponentStatus",
        "FlowSchema",
        "PriorityLevelConfiguration",
    }
)


class K8sError(RuntimeError):
    """Kubernetes API call failed."""


class K8sClient:
    """kube-apiserver REST client (mTLS / bearer from kubeconfig)."""

    def __init__(self, kubeconfig_path: str) -> None:
        try:
            self._http, self._apiserver, self._cleanup = load_kube_http(kubeconfig_path)
        except Exception as exc:  # noqa: BLE001 — surface as K8sError
            raise K8sError(str(exc)) from exc
        self._kubeconfig = kubeconfig_path
        self._gvk_cache: dict[tuple[str, str], tuple[str, bool]] = {}

    def close(self) -> None:
        try:
            self._http.close()
        finally:
            for p in self._cleanup:
                try:
                    Path(p).unlink(missing_ok=True)
                except OSError:
                    pass

    def __enter__(self) -> K8sClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        if not path.startswith("/"):
            path = "/" + path
        return self._http.request(method, f"{self._apiserver}{path}", **kwargs)

    def json(
        self,
        method: str,
        path: str,
        *,
        json_body: Any | None = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        ok: tuple[int, ...] = (200, 201, 202, 204),
    ) -> Any:
        kwargs: dict[str, Any] = {}
        if json_body is not None:
            kwargs["json"] = json_body
        if params is not None:
            kwargs["params"] = params
        if headers is not None:
            kwargs["headers"] = headers
        resp = self.request(method, path, **kwargs)
        return self._decode(resp, method, path, ok)

    def patch(
        self, path: str, body: dict[str, Any], *, ok: tuple[int, ...] = (200, 201)
    ) -> Any:
        # httpx ``json=`` forces application/json; kube requires strategic-merge.
        resp = self.request(
            "PATCH",
            path,
            content=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": PATCH_CONTENT_TYPE},
        )
        return self._decode(resp, "PATCH", path, ok)

    def merge_patch(
        self, path: str, body: dict[str, Any], *, ok: tuple[int, ...] = (200, 201)
    ) -> Any:
        resp = self.request(
            "PATCH",
            path,
            content=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": MERGE_PATCH_CONTENT_TYPE},
        )
        return self._decode(resp, "PATCH", path, ok)

    def _decode(
        self,
        resp: httpx.Response,
        method: str,
        path: str,
        ok: tuple[int, ...],
    ) -> Any:
        if resp.status_code not in ok:
            detail = redact_secret_line((resp.text or "")[:200])
            raise K8sError(f"{method} {path} HTTP {resp.status_code}: {detail}")
        if resp.status_code == 204 or not resp.content:
            return {}
        try:
            return resp.json()
        except json.JSONDecodeError as exc:
            raise K8sError(f"invalid json from {method} {path}: {exc}") from exc

    def list_deployments(self, namespace: str | None = None) -> list[dict[str, Any]]:
        data = self.json("GET", _collection("deployments", namespace))
        return [
            _workload_row("Deployment", item, daemonset=False)
            for item in data.get("items") or []
        ]

    def list_statefulsets(self, namespace: str | None = None) -> list[dict[str, Any]]:
        data = self.json("GET", _collection("statefulsets", namespace))
        return [
            _workload_row("StatefulSet", item, daemonset=False)
            for item in data.get("items") or []
        ]

    def list_daemonsets(self, namespace: str | None = None) -> list[dict[str, Any]]:
        data = self.json("GET", _collection("daemonsets", namespace))
        return [
            _workload_row("DaemonSet", item, daemonset=True)
            for item in data.get("items") or []
        ]

    def list_pods(
        self,
        namespace: str | None = None,
        *,
        field_selector: str | None = None,
    ) -> list[dict[str, Any]]:
        params = {"fieldSelector": field_selector} if field_selector else None
        data = self.json("GET", _collection("pods", namespace), params=params)
        return [_pod_row(item) for item in data.get("items") or []]

    def list_nodes(self) -> list[dict[str, Any]]:
        data = self.json("GET", "/api/v1/nodes")
        return [_node_row(item) for item in _items(data)]

    def list_events(self, namespace: str | None = None) -> list[dict[str, Any]]:
        path = (
            f"/api/v1/namespaces/{namespace}/events" if namespace else "/api/v1/events"
        )
        data = self.json("GET", path)
        rows = [_event_row(item) for item in _items(data)]
        rows.sort(key=lambda r: str(r.get("last_timestamp") or ""), reverse=True)
        return rows[:EVENTS_LIMIT]

    def list_namespaces(self) -> list[dict[str, Any]]:
        data = self.json("GET", "/api/v1/namespaces")
        return [_namespace_row(item) for item in _items(data)]

    def list_services(self, namespace: str | None = None) -> list[dict[str, Any]]:
        data = self.json("GET", _collection("services", namespace))
        return [_service_row(item) for item in _items(data)]

    def list_ingresses(self, namespace: str | None = None) -> list[dict[str, Any]]:
        data = self.json(
            "GET", _group_collection("networking.k8s.io/v1", "ingresses", namespace)
        )
        return [_ingress_row(item) for item in _items(data)]

    def list_gateways(self, namespace: str | None = None) -> list[dict[str, Any]]:
        items = self._json_group_items(
            ("gateway.networking.k8s.io/v1", "gateway.networking.k8s.io/v1beta1"),
            "gateways",
            namespace,
        )
        return [_gateway_row(item) for item in items]

    def list_httproutes(self, namespace: str | None = None) -> list[dict[str, Any]]:
        items = self._json_group_items(
            ("gateway.networking.k8s.io/v1", "gateway.networking.k8s.io/v1beta1"),
            "httproutes",
            namespace,
        )
        return [_httproute_row(item) for item in items]

    def list_metallb_pools(self) -> list[dict[str, Any]]:
        items = self._json_group_items(("metallb.io/v1beta1",), "ipaddresspools")
        return [_metallb_pool_row(item) for item in items]

    def _json_group_items(
        self,
        api_versions: tuple[str, ...],
        resource: str,
        namespace: str | None = None,
    ) -> list[dict[str, Any]]:
        """GET a CRD collection via json(). Missing API/CRD (404) is empty."""
        for api_version in api_versions:
            try:
                data = self.json(
                    "GET", _group_collection(api_version, resource, namespace)
                )
            except K8sError as exc:
                if " HTTP 404:" not in str(exc):
                    raise
                continue
            return _items(data)
        return []

    def list_pvcs(self, namespace: str | None = None) -> list[dict[str, Any]]:
        data = self.json("GET", _collection("persistentvolumeclaims", namespace))
        return [_pvc_row(item) for item in _items(data)]

    def list_pvs(self) -> list[dict[str, Any]]:
        data = self.json("GET", "/api/v1/persistentvolumes")
        return [_pv_row(item) for item in _items(data)]

    def list_storageclasses(self) -> list[dict[str, Any]]:
        data = self.json("GET", "/apis/storage.k8s.io/v1/storageclasses")
        return [_storageclass_row(item) for item in _items(data)]

    def list_configmaps(self, namespace: str | None = None) -> list[dict[str, Any]]:
        data = self.json("GET", _collection("configmaps", namespace))
        return [_configmap_row(item) for item in _items(data)]

    def list_secrets(self, namespace: str | None = None) -> list[dict[str, Any]]:
        """List secret metadata only. Never returns data or stringData."""
        data = self.json("GET", _collection("secrets", namespace))
        return [_secret_row(item) for item in _items(data)]

    def list_jobs(self, namespace: str | None = None) -> list[dict[str, Any]]:
        data = self.json("GET", _group_collection("batch/v1", "jobs", namespace))
        return [_job_row(item) for item in _items(data)]

    def list_helm_releases(self) -> list[dict[str, Any]]:
        try:
            raw = run_helm(self._kubeconfig, ["list", "-A", "-o", "json"])
        except K8sError as exc:
            if "not found" in str(exc).lower():
                return self._helm_from_secrets()
            raise
        parsed = _parse_json_list(raw)
        return [_helm_release_row(item) for item in parsed if isinstance(item, dict)]

    def scale(self, kind: str, ns: str, name: str, replicas: int) -> Any:
        kind = kind.lower()
        if kind not in SCALE_KINDS:
            raise K8sError(f"cannot scale {kind}")
        return self.patch(
            f"/apis/apps/v1/namespaces/{ns}/{kind}/{name}",
            {"spec": {"replicas": replicas}},
        )

    def restart(self, kind: str, ns: str, name: str) -> Any:
        kind = kind.lower()
        if kind not in RESTART_KINDS:
            raise K8sError(f"cannot restart {kind}")
        stamp = _rfc3339_now()
        return self.patch(
            f"/apis/apps/v1/namespaces/{ns}/{kind}/{name}",
            {
                "spec": {
                    "template": {
                        "metadata": {
                            "annotations": {RESTART_ANNOTATION: stamp},
                        }
                    }
                }
            },
        )

    def delete_pod(self, ns: str, name: str) -> dict[str, Any]:
        path = f"/api/v1/namespaces/{ns}/pods/{name}"
        resp = self.request("DELETE", path)
        if resp.status_code in (200, 202):
            return {"ok": True, "message": f"pod {ns}/{name} deleted"}
        if resp.status_code == 404:
            return {"ok": True, "message": "already gone"}
        raise K8sError(f"DELETE {path} HTTP {resp.status_code}: {resp.text[:200]}")

    def set_unschedulable(self, name: str, value: bool) -> Any:
        if not NAME_RE.match(name):
            raise K8sError(f"invalid node name {name!r}")
        return self.patch(f"/api/v1/nodes/{name}", {"spec": {"unschedulable": value}})

    def drain_node(
        self,
        name: str,
        *,
        ignore_daemonsets: bool = True,
        delete_emptydir: bool = False,
        grace_period: int = 30,
        timeout: int = 90,
    ) -> dict[str, Any]:
        if not NAME_RE.match(name):
            raise K8sError(f"invalid node name {name!r}")
        out: dict[str, Any] = {
            "ok": False,
            "cordoned": False,
            "evicted": [],
            "skipped": [],
            "blocked": [],
            "error": None,
        }
        try:
            self.set_unschedulable(name, True)
            out["cordoned"] = True
            params = {"fieldSelector": f"spec.nodeName={name}"}
            data = self.json("GET", "/api/v1/pods", params=params)
        except K8sError as exc:
            out["error"] = str(exc)[:200]
            return out
        items = [p for p in (data.get("items") or []) if isinstance(p, dict)]

        to_evict: list[dict[str, Any]] = []
        for pod in items:
            skip = _skip_reason(
                pod,
                ignore_daemonsets=ignore_daemonsets,
                delete_emptydir=delete_emptydir,
            )
            key = _pod_key(pod)
            if skip:
                out["skipped"].append(key)
            else:
                to_evict.append(pod)

        deadline = time.monotonic() + max(int(timeout), 0)
        for pod in to_evict:
            if time.monotonic() >= deadline:
                out["error"] = "drain timed out"
                break
            key = _pod_key(pod)
            meta = pod.get("metadata") or {}
            ns = str(meta.get("namespace") or "")
            pname = str(meta.get("name") or "")
            body: dict[str, Any] = {
                "apiVersion": "policy/v1",
                "kind": "Eviction",
                "metadata": {"name": pname, "namespace": ns},
            }
            if grace_period is not None:
                body["deleteOptions"] = {"gracePeriodSeconds": int(grace_period)}
            resp = self.request(
                "POST",
                f"/api/v1/namespaces/{ns}/pods/{pname}/eviction",
                json=body,
            )
            if resp.status_code in (200, 201, 202, 404):
                out["evicted"].append(key)
            elif resp.status_code == 429:
                out["blocked"].append(f"{key}: PDB blocked")
            else:
                out["blocked"].append(f"{key}: HTTP {resp.status_code}")

        if out["error"] is None and not out["blocked"]:
            out["ok"] = True
        elif out["error"] is None:
            out["error"] = (
                "PDB blocked"
                if any("PDB blocked" in b for b in out["blocked"])
                else None
            )
        return out

    def describe(self, kind: str, ns: str | None, name: str) -> dict[str, Any]:
        kind = (kind or "").lower()
        if kind not in DESCRIBE_KINDS:
            raise K8sError(f"unknown kind {kind!r}")
        if kind not in CLUSTER_DESCRIBE_KINDS and not ns:
            raise K8sError("namespace is required")
        path = _object_path(kind, ns, name)
        obj = self.json("GET", path)
        obj = _sanitize_described(kind, obj)
        events = self._events_for(kind, ns, name)
        return {"ok": True, "object": obj, "events": events, "error": None}

    def create_namespace(
        self, name: str, labels: dict[str, str] | None = None
    ) -> dict[str, Any]:
        if not NS_RE.match(name):
            raise K8sError(f"invalid namespace {name!r}")
        body: dict[str, Any] = {
            "apiVersion": "v1",
            "kind": "Namespace",
            "metadata": {"name": name},
        }
        if labels:
            body["metadata"]["labels"] = labels
        self.json("POST", "/api/v1/namespaces", json_body=body)
        return {"ok": True, "message": f"namespace {name} created"}

    def delete_namespace(self, name: str) -> dict[str, Any]:
        if not NS_RE.match(name):
            raise K8sError(f"invalid namespace {name!r}")
        if name in PROTECTED_NAMESPACES:
            raise K8sError(f"refusing to delete protected namespace {name}")
        return self._delete(f"/api/v1/namespaces/{name}", f"namespace {name}")

    def add_taint(self, name: str, key: str, value: str, effect: str) -> dict[str, Any]:
        _check_node_name(name)
        err = validate_taint(key, value, effect)
        if err:
            raise K8sError(err)
        node = self.json("GET", f"/api/v1/nodes/{name}")
        taints = _taint_list(node)
        replaced = False
        for taint in taints:
            if taint.get("key") == key and taint.get("effect") == effect:
                taint["value"] = value
                replaced = True
                break
        if not replaced:
            entry: dict[str, Any] = {"key": key, "effect": effect}
            if value:
                entry["value"] = value
            taints.append(entry)
        self.merge_patch(f"/api/v1/nodes/{name}", {"spec": {"taints": taints}})
        return {
            "ok": True,
            "message": f"taint {key}:{effect} on {name}",
            "taints": taints,
        }

    def remove_taint(self, name: str, key: str, effect: str) -> dict[str, Any]:
        _check_node_name(name)
        err = validate_taint(key, "", effect, require_value=False)
        if err:
            raise K8sError(err)
        node = self.json("GET", f"/api/v1/nodes/{name}")
        taints = [
            t
            for t in _taint_list(node)
            if not (t.get("key") == key and t.get("effect") == effect)
        ]
        self.merge_patch(f"/api/v1/nodes/{name}", {"spec": {"taints": taints}})
        return {
            "ok": True,
            "message": f"removed taint {key}:{effect} from {name}",
            "taints": taints,
        }

    def set_label(self, name: str, key: str, value: str) -> dict[str, Any]:
        _check_node_name(name)
        err = validate_label(key, value)
        if err:
            raise K8sError(err)
        self.patch(f"/api/v1/nodes/{name}", {"metadata": {"labels": {key: value}}})
        return {"ok": True, "message": f"label {key}={value} on {name}"}

    def apply_yaml(self, text: str, *, dry_run: bool = False) -> dict[str, Any]:
        docs = parse_yaml_docs(text)
        if not docs:
            raise K8sError("no YAML documents")
        applied: list[dict[str, Any]] = []
        for doc in docs:
            kind = str(doc.get("kind") or "")
            api_version = str(doc.get("apiVersion") or "")
            meta = doc.get("metadata") or {}
            name = str(meta.get("name") or "").strip()
            ns = str(meta.get("namespace") or "").strip() or None
            if not kind or not api_version or not name:
                raise K8sError("each document needs apiVersion, kind, metadata.name")
            if not NAME_RE.match(name) and not NS_RE.match(name):
                raise K8sError(f"invalid object name {name!r}")
            path = self._apply_path(api_version, kind, ns, name)
            params: dict[str, Any] = {"fieldManager": APPLY_FIELD_MANAGER}
            if dry_run:
                params["dryRun"] = "All"
            body = yaml.safe_dump(doc, sort_keys=False)
            resp = self.request(
                "PATCH",
                path,
                content=body.encode("utf-8"),
                headers={"Content-Type": APPLY_CONTENT_TYPE},
                params=params,
            )
            self._decode(resp, "PATCH", path, (200, 201))
            applied.append({"kind": kind, "namespace": ns or "", "name": name})
        return {
            "ok": True,
            "message": f"applied {len(applied)} object(s)",
            "applied": applied,
            "dry_run": dry_run,
        }

    def delete_workload(self, kind: str, ns: str, name: str) -> dict[str, Any]:
        kind = kind.lower()
        if kind not in DELETE_WORKLOAD_KINDS:
            raise K8sError(f"cannot delete {kind}")
        return self._delete(
            f"/apis/apps/v1/namespaces/{ns}/{kind}/{name}",
            f"{kind.rstrip('s')} {ns}/{name}",
        )

    def delete_service(self, ns: str, name: str) -> dict[str, Any]:
        return self._delete(
            f"/api/v1/namespaces/{ns}/services/{name}", f"service {ns}/{name}"
        )

    def delete_pvc(self, ns: str, name: str) -> dict[str, Any]:
        return self._delete(
            f"/api/v1/namespaces/{ns}/persistentvolumeclaims/{name}",
            f"pvc {ns}/{name}",
        )

    def helm_history(self, ns: str, name: str) -> list[dict[str, Any]]:
        raw = run_helm(self._kubeconfig, ["history", name, "-n", ns, "-o", "json"])
        rows = []
        for item in _parse_json_list(raw):
            if not isinstance(item, dict):
                continue
            rows.append(
                {
                    "revision": _as_int(item.get("revision")),
                    "updated": item.get("updated") or item.get("updated_at") or "",
                    "status": item.get("status") or "",
                    "chart": item.get("chart") or "",
                    "app_version": item.get("app_version") or "",
                    "description": item.get("description") or "",
                }
            )
        return rows

    def helm_status(self, ns: str, name: str) -> dict[str, Any]:
        raw = run_helm(self._kubeconfig, ["status", name, "-n", ns, "-o", "json"])
        try:
            data = json.loads(raw or "{}")
        except json.JSONDecodeError as exc:
            raise K8sError(f"invalid helm status json: {exc}") from exc
        if not isinstance(data, dict):
            data = {}
        return _helm_status_row(data, namespace=ns, name=name)

    def helm_rollback(
        self, ns: str, name: str, revision: int | None = None
    ) -> dict[str, Any]:
        args = ["rollback", name]
        if revision is not None:
            args.append(str(int(revision)))
        args.extend(["-n", ns, "--wait=false"])
        run_helm(self._kubeconfig, args, timeout=HELM_MUTATE_TIMEOUT)
        rev = f" revision {revision}" if revision is not None else " previous revision"
        return {"ok": True, "message": f"rolled back helm {ns}/{name}{rev}"}

    def helm_upgrade(
        self,
        ns: str,
        name: str,
        *,
        chart: str | None = None,
        values: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        chart_ref = (chart or "").strip() or self._chart_for_release(ns, name)
        err = validate_chart(chart_ref)
        if err:
            raise K8sError(err)
        args = ["upgrade", name, chart_ref, "-n", ns, "--reuse-values", "--wait=false"]
        values_path: str | None = None
        try:
            if values:
                values_path = _write_values_file(values)
                args.extend(["-f", values_path])
            run_helm(self._kubeconfig, args, timeout=HELM_MUTATE_TIMEOUT)
        finally:
            if values_path:
                try:
                    Path(values_path).unlink(missing_ok=True)
                except OSError:
                    pass
        return {"ok": True, "message": f"upgraded helm {ns}/{name}"}

    def _chart_for_release(self, ns: str, name: str) -> str:
        raw = run_helm(
            self._kubeconfig, ["get", "metadata", name, "-n", ns, "-o", "json"]
        )
        try:
            meta = json.loads(raw or "{}")
        except json.JSONDecodeError:
            meta = {}
        chart = str((meta or {}).get("chart") or "").strip()
        if not chart:
            raise K8sError(f"chart is required to upgrade {ns}/{name}")
        return chart

    def _helm_from_secrets(self) -> list[dict[str, Any]]:
        data = self.json(
            "GET",
            "/api/v1/secrets",
            params={"labelSelector": "owner=helm"},
        )
        best: dict[tuple[str, str], dict[str, Any]] = {}
        for item in _items(data):
            meta = item.get("metadata") or {}
            labels = meta.get("labels") or {}
            if item.get("type") != HELM_RELEASE_TYPE and labels.get("owner") != "helm":
                continue
            ns = str(meta.get("namespace") or "")
            rel = str(labels.get("name") or "")
            if not rel:
                continue
            revision = _as_int(labels.get("version"))
            key = (ns, rel)
            prev = best.get(key)
            if prev is None or revision >= int(prev.get("revision") or 0):
                best[key] = {
                    "name": rel,
                    "namespace": ns,
                    "revision": revision,
                    "status": labels.get("status") or "",
                    "chart": "",
                    "app_version": "",
                    "updated": meta.get("creationTimestamp") or "",
                }
        return list(best.values())

    def _apply_path(
        self, api_version: str, kind: str, ns: str | None, name: str
    ) -> str:
        resource, namespaced = self._discover(api_version, kind)
        prefix = _api_prefix(api_version)
        if namespaced:
            namespace = ns or "default"
            if not NS_RE.match(namespace):
                raise K8sError(f"invalid namespace {namespace!r}")
            return f"{prefix}/namespaces/{namespace}/{resource}/{name}"
        return f"{prefix}/{resource}/{name}"

    def _discover(self, api_version: str, kind: str) -> tuple[str, bool]:
        key = (api_version, kind)
        cached = self._gvk_cache.get(key)
        if cached:
            return cached
        resource = KIND_RESOURCE.get(kind)
        if resource:
            namespaced = kind not in CLUSTER_SCOPED_KINDS
            self._gvk_cache[key] = (resource, namespaced)
            return resource, namespaced
        data = self.json("GET", _api_prefix(api_version))
        for res in data.get("resources") or []:
            if not isinstance(res, dict):
                continue
            if res.get("kind") != kind:
                continue
            rname = str(res.get("name") or "")
            if not rname or "/" in rname:
                continue
            namespaced = bool(res.get("namespaced"))
            self._gvk_cache[key] = (rname, namespaced)
            return rname, namespaced
        raise K8sError(f"unknown kind {kind} ({api_version})")

    def _delete(self, path: str, what: str) -> dict[str, Any]:
        resp = self.request("DELETE", path)
        if resp.status_code in (200, 202):
            return {"ok": True, "message": f"{what} deleted"}
        if resp.status_code == 404:
            return {"ok": True, "message": "already gone"}
        detail = redact_secret_line((resp.text or "")[:200])
        raise K8sError(f"DELETE {path} HTTP {resp.status_code}: {detail}")

    def _events_for(self, kind: str, ns: str | None, name: str) -> list[dict[str, Any]]:
        if kind in CLUSTER_DESCRIBE_KINDS:
            path = "/api/v1/events"
        else:
            path = f"/api/v1/namespaces/{ns}/events"
        try:
            data = self.json(
                "GET",
                path,
                params={"fieldSelector": f"involvedObject.name={name}"},
            )
        except K8sError:
            return []
        rows: list[dict[str, Any]] = []
        for ev in data.get("items") or []:
            inv = ev.get("involvedObject") or {}
            if inv.get("name") and inv.get("name") != name:
                continue
            rows.append(_event_row(ev))
        return rows


def client_from_context(ctx: EnvContext) -> K8sClient:
    kube = ctx.kubeconfig
    if not kube:
        raise K8sError("no kubeconfig")
    return K8sClient(kube)


def run_helm(kubeconfig: str, args: list[str], *, timeout: int = HELM_TIMEOUT) -> str:
    """Run helm against kubeconfig. Never logs values files or secret output."""
    helm = shutil.which("helm")
    if not helm:
        raise K8sError("helm not found on PATH")
    env = {**os.environ, "KUBECONFIG": kubeconfig, "HELM_DRIVER": "secret"}
    try:
        proc = subprocess.run(
            [helm, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            check=False,
        )
    except FileNotFoundError as exc:
        raise K8sError("helm not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise K8sError(f"helm timed out after {timeout}s") from exc
    except OSError as exc:
        raise K8sError(str(exc)[:200]) from exc
    if proc.returncode != 0:
        detail = redact_secret_line((proc.stderr or proc.stdout or "").strip())[:200]
        raise K8sError(detail or f"helm exit {proc.returncode}")
    return proc.stdout or ""


def validate_label_key(key: str) -> str | None:
    text = str(key or "").strip()
    if not text or len(text) > 253:
        return f"invalid label key {text!r}"
    if "/" in text:
        prefix, name = text.split("/", 1)
        if (
            not prefix
            or len(prefix) > 253
            or not LABEL_PREFIX_RE.match(prefix)
            or not name
            or not LABEL_NAME_RE.match(name)
        ):
            return f"invalid label key {text!r}"
        return None
    if not LABEL_NAME_RE.match(text):
        return f"invalid label key {text!r}"
    return None


def validate_label(key: str, value: str) -> str | None:
    err = validate_label_key(key)
    if err:
        return err
    val = str(value or "")
    if len(val) > 63 or not LABEL_VALUE_RE.match(val):
        return f"invalid label value {val!r}"
    return None


def validate_taint(
    key: str, value: str, effect: str, *, require_value: bool = False
) -> str | None:
    err = validate_label_key(key)
    if err:
        return err.replace("label key", "taint key")
    if effect not in TAINT_EFFECTS:
        return f"effect must be one of {', '.join(sorted(TAINT_EFFECTS))}"
    val = str(value or "")
    if require_value and not val:
        return "taint value is required"
    if val and (len(val) > 63 or not LABEL_VALUE_RE.match(val)):
        return f"invalid taint value {val!r}"
    return None


def validate_chart(chart: str) -> str | None:
    text = str(chart or "").strip()
    if not text or ".." in text or text.startswith("/") or "\\" in text:
        return f"invalid chart {text!r}"
    if text.startswith("oci://"):
        rest = text[6:]
        if not rest or ".." in rest:
            return f"invalid chart {text!r}"
        return None
    if not CHART_RE.match(text):
        return f"invalid chart {text!r}"
    return None


def _rfc3339_now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _collection(resource: str, namespace: str | None) -> str:
    if resource in APPS_KINDS:
        if namespace:
            return f"/apis/apps/v1/namespaces/{namespace}/{resource}"
        return f"/apis/apps/v1/{resource}"
    if namespace:
        return f"/api/v1/namespaces/{namespace}/{resource}"
    return f"/api/v1/{resource}"


def _group_collection(api_version: str, resource: str, namespace: str | None) -> str:
    prefix = _api_prefix(api_version)
    if namespace:
        return f"{prefix}/namespaces/{namespace}/{resource}"
    return f"{prefix}/{resource}"


def _api_prefix(api_version: str) -> str:
    ver = (api_version or "").strip()
    if ver == "v1":
        return "/api/v1"
    return f"/apis/{ver}"


def _object_path(kind: str, ns: str | None, name: str) -> str:
    if kind == "nodes":
        return f"/api/v1/nodes/{name}"
    if kind == "namespaces":
        return f"/api/v1/namespaces/{name}"
    if kind == "persistentvolumes":
        return f"/api/v1/persistentvolumes/{name}"
    if kind == "storageclasses":
        return f"/apis/storage.k8s.io/v1/storageclasses/{name}"
    if kind in APPS_KINDS:
        return f"/apis/apps/v1/namespaces/{ns}/{kind}/{name}"
    if kind == "jobs":
        return f"/apis/batch/v1/namespaces/{ns}/jobs/{name}"
    if kind == "ingresses":
        return f"/apis/networking.k8s.io/v1/namespaces/{ns}/ingresses/{name}"
    if kind == "persistentvolumeclaims":
        return f"/api/v1/namespaces/{ns}/persistentvolumeclaims/{name}"
    return f"/api/v1/namespaces/{ns}/{kind}/{name}"


def _images(obj: dict[str, Any]) -> list[str]:
    spec = ((obj.get("spec") or {}).get("template") or {}).get("spec") or {}
    images: list[str] = []
    seen: set[str] = set()
    for key in ("initContainers", "containers"):
        for container in spec.get(key) or []:
            img = container.get("image")
            if img and img not in seen:
                seen.add(img)
                images.append(str(img))
    return images


def _workload_row(kind: str, obj: dict[str, Any], *, daemonset: bool) -> dict[str, Any]:
    meta = obj.get("metadata") or {}
    spec = obj.get("spec") or {}
    status = obj.get("status") or {}
    if daemonset:
        replicas = status.get("desiredNumberScheduled") or 0
        ready = status.get("numberReady") or 0
        available = status.get("numberAvailable") or 0
        updated = status.get("updatedNumberScheduled") or 0
    else:
        replicas = spec.get("replicas") if spec.get("replicas") is not None else 0
        ready = status.get("readyReplicas") or 0
        available = status.get("availableReplicas") or 0
        updated = status.get("updatedReplicas") or 0
    return {
        "kind": kind,
        "namespace": meta.get("namespace"),
        "name": meta.get("name"),
        "replicas": replicas,
        "ready": ready,
        "available": available,
        "updated": updated,
        "images": _images(obj),
        "age": meta.get("creationTimestamp") or "",
    }


def _pod_row(obj: dict[str, Any]) -> dict[str, Any]:
    meta = obj.get("metadata") or {}
    spec = obj.get("spec") or {}
    status = obj.get("status") or {}
    containers = spec.get("containers") or []
    desired = len(containers)
    css = status.get("containerStatuses") or []
    init_css = status.get("initContainerStatuses") or []
    ready_n = sum(1 for c in css if c.get("ready"))
    restarts = sum(int(c.get("restartCount") or 0) for c in css)
    reason = status.get("reason") or ""
    stale = False
    for cs in list(init_css) + list(css):
        cur = cs.get("state") or {}
        waiting = cur.get("waiting") or {}
        terminated = cur.get("terminated") or {}
        last = (cs.get("lastState") or {}).get("terminated") or {}
        wr = str(waiting.get("reason") or "")
        tr = str(terminated.get("reason") or "")
        lr = str(last.get("reason") or "")
        if (
            wr == "ContainerStatusUnknown"
            or tr == "ContainerStatusUnknown"
            or lr == "ContainerStatusUnknown"
        ):
            stale = True
            reason = reason or "ContainerStatusUnknown"
        if wr:
            reason = wr
            continue
        if tr and tr != "Completed":
            reason = tr
            continue
        # lastState.terminated.reason is often "Error" on a healthy restart.
        # Never promote that onto a currently running/ready container.
        running = bool(cur.get("running"))
        if running and cs.get("ready"):
            continue
        if lr and lr not in ("Completed", "Error") and not reason:
            reason = lr
    controllers: list[str] = []
    for ref in meta.get("ownerReferences") or []:
        kind = ref.get("kind")
        rname = ref.get("name")
        if kind and rname:
            controllers.append(f"{kind}/{rname}")
    row: dict[str, Any] = {
        "namespace": meta.get("namespace"),
        "name": meta.get("name"),
        "node": spec.get("nodeName") or "",
        "phase": status.get("phase") or "Unknown",
        "ready": f"{ready_n}/{desired}",
        "restarts": restarts,
        "reason": reason or None,
        "stale": stale,
        "age": meta.get("creationTimestamp") or "",
        "controllers": controllers,
        "containers": [str(c.get("name") or "") for c in containers if c.get("name")],
    }
    deletion = meta.get("deletionTimestamp")
    if deletion:
        row["deletion_timestamp"] = deletion
    return row


def _pod_key(pod: dict[str, Any]) -> str:
    meta = pod.get("metadata") or {}
    return f"{meta.get('namespace') or ''}/{meta.get('name') or ''}"


def _skip_reason(
    pod: dict[str, Any],
    *,
    ignore_daemonsets: bool,
    delete_emptydir: bool,
) -> str | None:
    meta = pod.get("metadata") or {}
    spec = pod.get("spec") or {}
    anns = meta.get("annotations") or {}
    if anns.get(MIRROR_ANNOTATION):
        return "mirror"
    if ignore_daemonsets:
        for ref in meta.get("ownerReferences") or []:
            if ref.get("kind") == "DaemonSet":
                return "daemonset"
    if not delete_emptydir:
        for vol in spec.get("volumes") or []:
            if not isinstance(vol, dict):
                continue
            if "emptyDir" in vol and vol.get("emptyDir") is not None:
                return "emptydir"
    return None


def _event_row(ev: dict[str, Any]) -> dict[str, Any]:
    meta = ev.get("metadata") or {}
    inv = ev.get("involvedObject") or {}
    kind = inv.get("kind")
    name = inv.get("name")
    return {
        "type": ev.get("type"),
        "reason": ev.get("reason"),
        "message": ev.get("message"),
        "count": ev.get("count"),
        "namespace": meta.get("namespace"),
        "first_timestamp": ev.get("firstTimestamp") or ev.get("eventTime"),
        "last_timestamp": ev.get("lastTimestamp") or ev.get("eventTime"),
        "involved_object": f"{kind}/{name}" if kind and name else name,
    }


def _items(data: Any) -> list[dict[str, Any]]:
    if not isinstance(data, dict):
        return []
    return [item for item in (data.get("items") or []) if isinstance(item, dict)]


def _parse_json_list(raw: str | None) -> list[Any]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _check_node_name(name: str) -> None:
    if not NAME_RE.match(name):
        raise K8sError(f"invalid node name {name!r}")


def _taint_list(node: dict[str, Any]) -> list[dict[str, Any]]:
    spec = node.get("spec") or {}
    taints = spec.get("taints") or []
    out: list[dict[str, Any]] = []
    for taint in taints:
        if not isinstance(taint, dict) or not taint.get("key"):
            continue
        row: dict[str, Any] = {
            "key": taint.get("key"),
            "effect": taint.get("effect") or "",
        }
        if taint.get("value"):
            row["value"] = taint.get("value")
        out.append(row)
    return out


def _data_keys(obj: dict[str, Any], field: str = "data") -> list[str]:
    blob = obj.get(field)
    if isinstance(blob, dict):
        return sorted(str(k) for k in blob)
    return []


def _sanitize_described(kind: str, obj: Any) -> Any:
    if not isinstance(obj, dict):
        return obj
    if kind == "secrets" or obj.get("kind") == "Secret":
        cleaned = dict(obj)
        cleaned.pop("data", None)
        cleaned.pop("stringData", None)
        return cleaned
    return obj


def parse_yaml_docs(text: str) -> list[dict[str, Any]]:
    docs: list[dict[str, Any]] = []
    try:
        loaded = list(yaml.safe_load_all(text or ""))
    except yaml.YAMLError as exc:
        raise K8sError(f"invalid YAML: {exc}") from exc
    for doc in loaded:
        if doc is None:
            continue
        if not isinstance(doc, dict):
            raise K8sError("YAML documents must be mappings")
        if str(doc.get("kind") or "") == "List":
            for item in doc.get("items") or []:
                if isinstance(item, dict):
                    docs.append(item)
            continue
        docs.append(doc)
    return docs


def _write_values_file(values: dict[str, Any]) -> str:
    fd, path = tempfile.mkstemp(prefix="gsc-helm-", suffix=".yaml")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            yaml.safe_dump(values, handle, sort_keys=False)
        os.chmod(path, 0o600)
    except Exception:
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return path


def _namespace_row(obj: dict[str, Any]) -> dict[str, Any]:
    meta = obj.get("metadata") or {}
    status = obj.get("status") or {}
    return {
        "name": meta.get("name"),
        "phase": status.get("phase") or "",
        "labels": meta.get("labels") or {},
        "age": meta.get("creationTimestamp") or "",
    }


def _service_row(obj: dict[str, Any]) -> dict[str, Any]:
    meta = obj.get("metadata") or {}
    spec = obj.get("spec") or {}
    status = obj.get("status") or {}
    ports = []
    for port in spec.get("ports") or []:
        if not isinstance(port, dict):
            continue
        bit = f"{port.get('port')}/{port.get('protocol') or 'TCP'}"
        if port.get("name"):
            bit = f"{port.get('name')}:{bit}"
        if port.get("nodePort"):
            bit = f"{bit}:{port.get('nodePort')}"
        ports.append(bit)
    lb = []
    for ing in (status.get("loadBalancer") or {}).get("ingress") or []:
        if not isinstance(ing, dict):
            continue
        lb.append(ing.get("ip") or ing.get("hostname") or "")
    return {
        "namespace": meta.get("namespace"),
        "name": meta.get("name"),
        "type": spec.get("type") or "ClusterIP",
        "cluster_ip": spec.get("clusterIP") or "",
        "external_ips": spec.get("externalIPs") or [],
        "ports": ports,
        "selector": spec.get("selector") or {},
        "load_balancer": [x for x in lb if x],
        "age": meta.get("creationTimestamp") or "",
    }


def _ingress_row(obj: dict[str, Any]) -> dict[str, Any]:
    meta = obj.get("metadata") or {}
    spec = obj.get("spec") or {}
    status = obj.get("status") or {}
    rules = [r for r in (spec.get("rules") or []) if isinstance(r, dict)]
    hosts = [r.get("host") for r in rules if r.get("host")]
    tls = bool(spec.get("tls"))
    scheme = "https" if tls else "http"
    urls = [f"{scheme}://{h}" for h in hosts]
    ns = meta.get("namespace")
    backends: list[dict[str, Any]] = []
    for rule in rules:
        http = rule.get("http") or {}
        if not isinstance(http, dict):
            continue
        for path in http.get("paths") or []:
            if not isinstance(path, dict):
                continue
            backend = path.get("backend") or {}
            if not isinstance(backend, dict):
                continue
            service = backend.get("service") or {}
            if not isinstance(service, dict) or not service.get("name"):
                continue
            port = service.get("port")
            if isinstance(port, dict):
                port_val = port.get("number")
                if port_val is None:
                    port_val = port.get("name")
            else:
                port_val = port
            backends.append(
                {"namespace": ns, "name": service.get("name"), "port": port_val}
            )
    addrs = []
    for ing in (status.get("loadBalancer") or {}).get("ingress") or []:
        if isinstance(ing, dict):
            addrs.append(ing.get("ip") or ing.get("hostname") or "")
    class_name = spec.get("ingressClassName") or ""
    if not class_name:
        class_name = (meta.get("annotations") or {}).get(
            "kubernetes.io/ingress.class"
        ) or ""
    return {
        "namespace": meta.get("namespace"),
        "name": meta.get("name"),
        "class": class_name,
        "hosts": hosts,
        "urls": urls,
        "backends": backends,
        "address": ", ".join(a for a in addrs if a),
        "tls": tls,
        "age": meta.get("creationTimestamp") or "",
    }


def _gateway_row(obj: dict[str, Any]) -> dict[str, Any]:
    meta = obj.get("metadata") or {}
    spec = obj.get("spec") or {}
    status = obj.get("status") or {}
    addresses: list[str] = []
    for addr in status.get("addresses") or []:
        if not isinstance(addr, dict):
            continue
        value = addr.get("value") or addr.get("ip") or addr.get("hostname") or ""
        if value:
            addresses.append(str(value))
    listeners: list[str] = []
    for listener in spec.get("listeners") or []:
        if not isinstance(listener, dict):
            continue
        protocol = str(listener.get("protocol") or "")
        port = listener.get("port")
        name = str(listener.get("name") or "")
        port_s = "" if port is None else str(port)
        listeners.append(f"{protocol}/{port_s}/{name}")
    return {
        "namespace": meta.get("namespace"),
        "name": meta.get("name"),
        "class": spec.get("gatewayClassName") or "",
        "addresses": addresses,
        "listeners": listeners,
        "address": ", ".join(addresses),
        "age": meta.get("creationTimestamp") or "",
    }


def _httproute_row(obj: dict[str, Any]) -> dict[str, Any]:
    meta = obj.get("metadata") or {}
    spec = obj.get("spec") or {}
    ns = meta.get("namespace")
    raw_hosts = spec.get("hostnames") or []
    if not isinstance(raw_hosts, list):
        raw_hosts = []
    hosts = [str(h) for h in raw_hosts if h]
    parent_refs: list[dict[str, Any]] = []
    for ref in spec.get("parentRefs") or []:
        if not isinstance(ref, dict) or not ref.get("name"):
            continue
        parent_refs.append(
            {
                "namespace": ref.get("namespace") or ns,
                "name": ref.get("name"),
                "kind": ref.get("kind") or "Gateway",
            }
        )
    backends: list[dict[str, Any]] = []
    for rule in spec.get("rules") or []:
        if not isinstance(rule, dict):
            continue
        for backend in rule.get("backendRefs") or []:
            if not isinstance(backend, dict) or not backend.get("name"):
                continue
            backends.append(
                {
                    "namespace": backend.get("namespace") or ns,
                    "name": backend.get("name"),
                    "kind": backend.get("kind") or "Service",
                    "port": backend.get("port"),
                }
            )
    return {
        "namespace": ns,
        "name": meta.get("name"),
        "hosts": hosts,
        "parent_refs": parent_refs,
        "backends": backends,
        "urls": [f"https://{h}" for h in hosts],
        "tls": False,
        "age": meta.get("creationTimestamp") or "",
    }


def _metallb_pool_row(obj: dict[str, Any]) -> dict[str, Any]:
    meta = obj.get("metadata") or {}
    spec = obj.get("spec") or {}
    auto = spec.get("autoAssign")
    return {
        "name": meta.get("name"),
        "addresses": [str(a) for a in (spec.get("addresses") or []) if a],
        "auto_assign": True if auto is None else bool(auto),
        "age": meta.get("creationTimestamp") or "",
    }


def _pvc_row(obj: dict[str, Any]) -> dict[str, Any]:
    meta = obj.get("metadata") or {}
    spec = obj.get("spec") or {}
    status = obj.get("status") or {}
    return {
        "namespace": meta.get("namespace"),
        "name": meta.get("name"),
        "phase": status.get("phase") or "",
        "storage_class": spec.get("storageClassName") or "",
        "access_modes": spec.get("accessModes") or [],
        "volume": spec.get("volumeName") or "",
        "capacity": (status.get("capacity") or {}).get("storage")
        or ((spec.get("resources") or {}).get("requests") or {}).get("storage")
        or "",
        "age": meta.get("creationTimestamp") or "",
    }


def _pv_row(obj: dict[str, Any]) -> dict[str, Any]:
    meta = obj.get("metadata") or {}
    spec = obj.get("spec") or {}
    status = obj.get("status") or {}
    claim = spec.get("claimRef") or {}
    claim_name = ""
    if isinstance(claim, dict) and claim.get("name"):
        claim_name = f"{claim.get('namespace') or ''}/{claim.get('name')}"
    return {
        "name": meta.get("name"),
        "phase": status.get("phase") or "",
        "storage_class": spec.get("storageClassName") or "",
        "reclaim_policy": spec.get("persistentVolumeReclaimPolicy") or "",
        "capacity": ((spec.get("capacity") or {}).get("storage") or ""),
        "access_modes": spec.get("accessModes") or [],
        "claim": claim_name,
        "age": meta.get("creationTimestamp") or "",
    }


def _storageclass_row(obj: dict[str, Any]) -> dict[str, Any]:
    meta = obj.get("metadata") or {}
    anns = meta.get("annotations") or {}
    default = str(
        anns.get("storageclass.kubernetes.io/is-default-class") or ""
    ).lower() in {
        "true",
        "1",
    }
    return {
        "name": meta.get("name"),
        "provisioner": obj.get("provisioner") or "",
        "reclaim_policy": obj.get("reclaimPolicy") or "",
        "volume_binding_mode": obj.get("volumeBindingMode") or "",
        "allow_volume_expansion": bool(obj.get("allowVolumeExpansion")),
        "default": default,
        "age": meta.get("creationTimestamp") or "",
    }


def _configmap_row(obj: dict[str, Any]) -> dict[str, Any]:
    meta = obj.get("metadata") or {}
    return {
        "namespace": meta.get("namespace"),
        "name": meta.get("name"),
        "keys": _data_keys(obj, "data"),
        "age": meta.get("creationTimestamp") or "",
    }


def _secret_row(obj: dict[str, Any]) -> dict[str, Any]:
    meta = obj.get("metadata") or {}
    return {
        "namespace": meta.get("namespace"),
        "name": meta.get("name"),
        "type": obj.get("type") or "Opaque",
        "keys": _data_keys(obj, "data"),
        "labels": meta.get("labels") or {},
        "age": meta.get("creationTimestamp") or "",
    }


def _job_row(obj: dict[str, Any]) -> dict[str, Any]:
    meta = obj.get("metadata") or {}
    spec = obj.get("spec") or {}
    status = obj.get("status") or {}
    return {
        "namespace": meta.get("namespace"),
        "name": meta.get("name"),
        "completions": spec.get("completions"),
        "parallelism": spec.get("parallelism"),
        "succeeded": status.get("succeeded") or 0,
        "failed": status.get("failed") or 0,
        "active": status.get("active") or 0,
        "age": meta.get("creationTimestamp") or "",
    }


def _node_row(obj: dict[str, Any]) -> dict[str, Any]:
    meta = obj.get("metadata") or {}
    spec = obj.get("spec") or {}
    status = obj.get("status") or {}
    labels = meta.get("labels") or {}
    roles = sorted(
        key.removeprefix("node-role.kubernetes.io/")
        for key in labels
        if key.startswith("node-role.kubernetes.io/")
    )
    conditions = []
    ready = "Unknown"
    for cond in status.get("conditions") or []:
        if not isinstance(cond, dict):
            continue
        conditions.append(
            {
                "type": cond.get("type") or "",
                "status": cond.get("status") or "",
                "reason": cond.get("reason") or "",
                "message": cond.get("message") or "",
            }
        )
        if cond.get("type") == "Ready":
            ready = "Ready" if cond.get("status") == "True" else "NotReady"
    taints = _taint_list(obj)
    capacity = status.get("capacity") or {}
    allocatable = status.get("allocatable") or {}
    internal_ip = ""
    for addr in status.get("addresses") or []:
        if isinstance(addr, dict) and str(addr.get("type") or "") == "InternalIP":
            internal_ip = str(addr.get("address") or "").strip()
            break
    return {
        "name": meta.get("name"),
        "internal_ip": internal_ip,
        "labels": labels,
        "taints": taints,
        "unschedulable": bool(spec.get("unschedulable")),
        "conditions": conditions,
        "capacity": {
            "cpu": capacity.get("cpu") or "",
            "memory": capacity.get("memory") or "",
            "pods": capacity.get("pods") or "",
        },
        "allocatable": {
            "cpu": allocatable.get("cpu") or "",
            "memory": allocatable.get("memory") or "",
            "pods": allocatable.get("pods") or "",
        },
        "roles": roles,
        "status": ready,
        "version": ((status.get("nodeInfo") or {}).get("kubeletVersion") or ""),
        "age": meta.get("creationTimestamp") or "",
    }


def _helm_release_row(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": item.get("name") or "",
        "namespace": item.get("namespace") or "",
        "revision": _as_int(item.get("revision")),
        "status": item.get("status") or "",
        "chart": item.get("chart") or "",
        "app_version": item.get("app_version") or item.get("appVersion") or "",
        "updated": item.get("updated") or "",
    }


def _helm_status_row(
    data: dict[str, Any], *, namespace: str, name: str
) -> dict[str, Any]:
    info = data.get("info") if isinstance(data.get("info"), dict) else {}
    status_obj = info.get("status")
    if isinstance(status_obj, dict):
        status = status_obj.get("status") or status_obj.get("code") or ""
    else:
        status = status_obj or ""
    chart = data.get("chart") if isinstance(data.get("chart"), dict) else {}
    chart_meta = (
        chart.get("metadata") if isinstance(chart.get("metadata"), dict) else {}
    )
    return {
        "name": data.get("name") or name,
        "namespace": data.get("namespace") or namespace,
        "revision": _as_int(data.get("version") or data.get("revision")),
        "status": str(status or ""),
        "chart": chart_meta.get("name") or "",
        "chart_version": chart_meta.get("version") or "",
        "app_version": chart_meta.get("appVersion") or "",
        "last_deployed": info.get("last_deployed") or info.get("lastDeployed") or "",
        "description": info.get("description") or "",
    }
