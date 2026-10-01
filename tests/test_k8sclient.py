"""Native Kubernetes REST client tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import yaml

from app.services.k8sclient import (
    APPLY_CONTENT_TYPE,
    APPLY_FIELD_MANAGER,
    K8sClient,
    K8sError,
    MERGE_PATCH_CONTENT_TYPE,
    PATCH_CONTENT_TYPE,
    RESTART_ANNOTATION,
    _gateway_row,
    _httproute_row,
    _metallb_pool_row,
    _pod_row,
    run_helm,
)

APISERVER = "https://kube.example:6443"

DEPLOYMENT = {
    "kind": "Deployment",
    "metadata": {
        "name": "web",
        "namespace": "default",
        "creationTimestamp": "2026-01-01T00:00:00Z",
    },
    "spec": {
        "replicas": 3,
        "template": {"spec": {"containers": [{"name": "app", "image": "nginx:1.25"}]}},
    },
    "status": {"readyReplicas": 2, "availableReplicas": 2, "updatedReplicas": 3},
}
STATEFULSET = {
    "kind": "StatefulSet",
    "metadata": {
        "name": "db",
        "namespace": "default",
        "creationTimestamp": "2026-01-02T00:00:00Z",
    },
    "spec": {
        "replicas": 1,
        "template": {"spec": {"containers": [{"name": "pg", "image": "postgres:16"}]}},
    },
    "status": {"readyReplicas": 1, "availableReplicas": 1, "updatedReplicas": 1},
}
DAEMONSET = {
    "kind": "DaemonSet",
    "metadata": {
        "name": "kindnet",
        "namespace": "kube-system",
        "creationTimestamp": "2026-01-03T00:00:00Z",
    },
    "spec": {
        "template": {"spec": {"containers": [{"name": "net", "image": "kindnet:1"}]}},
    },
    "status": {
        "desiredNumberScheduled": 2,
        "numberReady": 2,
        "numberAvailable": 2,
        "updatedNumberScheduled": 2,
    },
}
POD = {
    "metadata": {
        "name": "web-abc",
        "namespace": "default",
        "creationTimestamp": "2026-01-01T00:00:00Z",
        "ownerReferences": [{"kind": "ReplicaSet", "name": "web-xyz"}],
    },
    "spec": {"nodeName": "node-1", "containers": [{"name": "app"}]},
    "status": {
        "phase": "Running",
        "containerStatuses": [
            {"ready": True, "restartCount": 1, "state": {"running": {}}},
        ],
    },
}
TERMINATING_POD = {
    "metadata": {
        "name": "web-old",
        "namespace": "default",
        "creationTimestamp": "2026-01-01T00:00:00Z",
        "deletionTimestamp": "2026-01-05T00:00:00Z",
        "ownerReferences": [{"kind": "ReplicaSet", "name": "web-xyz"}],
    },
    "spec": {"nodeName": "node-1", "containers": [{"name": "app"}]},
    "status": {
        "phase": "Running",
        "containerStatuses": [{"ready": False, "restartCount": 0}],
    },
}

DS_POD = {
    "metadata": {
        "name": "kindnet-node-1",
        "namespace": "kube-system",
        "ownerReferences": [
            {"kind": "DaemonSet", "name": "kindnet", "controller": True}
        ],
        "creationTimestamp": "2026-01-01T00:00:00Z",
    },
    "spec": {"nodeName": "node-1", "containers": [{"name": "net"}]},
    "status": {
        "phase": "Running",
        "containerStatuses": [{"ready": True, "restartCount": 0}],
    },
}
MIRROR_POD = {
    "metadata": {
        "name": "kube-apiserver-node-1",
        "namespace": "kube-system",
        "annotations": {"kubernetes.io/config.mirror": "abc"},
        "creationTimestamp": "2026-01-01T00:00:00Z",
    },
    "spec": {"nodeName": "node-1", "containers": [{"name": "kube-apiserver"}]},
    "status": {
        "phase": "Running",
        "containerStatuses": [{"ready": True, "restartCount": 0}],
    },
}
APP_POD = {
    "metadata": {
        "name": "web-abc",
        "namespace": "default",
        "ownerReferences": [{"kind": "ReplicaSet", "name": "web-xyz"}],
        "creationTimestamp": "2026-01-01T00:00:00Z",
    },
    "spec": {"nodeName": "node-1", "containers": [{"name": "app"}]},
    "status": {
        "phase": "Running",
        "containerStatuses": [{"ready": True, "restartCount": 0}],
    },
}
PDB_POD = {
    "metadata": {
        "name": "locked",
        "namespace": "default",
        "creationTimestamp": "2026-01-01T00:00:00Z",
    },
    "spec": {"nodeName": "node-1", "containers": [{"name": "app"}]},
    "status": {
        "phase": "Running",
        "containerStatuses": [{"ready": True, "restartCount": 0}],
    },
}
NODE = {
    "kind": "Node",
    "metadata": {
        "name": "node-1",
        "creationTimestamp": "2026-01-01T00:00:00Z",
        "labels": {
            "kubernetes.io/hostname": "node-1",
            "node-role.kubernetes.io/control-plane": "",
        },
    },
    "spec": {
        "unschedulable": False,
        "taints": [{"key": "dedicated", "value": "gpu", "effect": "NoSchedule"}],
    },
    "status": {
        "conditions": [
            {
                "type": "Ready",
                "status": "True",
                "reason": "KubeletReady",
                "message": "ok",
            }
        ],
        "addresses": [
            {"type": "InternalIP", "address": "10.200.0.51"},
            {"type": "Hostname", "address": "node-1"},
        ],
        "capacity": {"cpu": "8", "memory": "16Gi", "pods": "110"},
        "allocatable": {"cpu": "7", "memory": "15Gi", "pods": "110"},
        "nodeInfo": {"kubeletVersion": "v1.31.0"},
    },
}
NAMESPACE = {
    "metadata": {
        "name": "default",
        "creationTimestamp": "2026-01-01T00:00:00Z",
        "labels": {},
    },
    "status": {"phase": "Active"},
}
SERVICE = {
    "metadata": {
        "name": "web",
        "namespace": "default",
        "creationTimestamp": "2026-01-01T00:00:00Z",
    },
    "spec": {
        "type": "ClusterIP",
        "clusterIP": "10.96.0.10",
        "selector": {"app": "web"},
        "ports": [{"name": "http", "port": 80, "protocol": "TCP", "targetPort": 8080}],
    },
    "status": {"loadBalancer": {}},
}
INGRESS = {
    "metadata": {
        "name": "web",
        "namespace": "default",
        "creationTimestamp": "2026-01-01T00:00:00Z",
    },
    "spec": {
        "ingressClassName": "nginx",
        "tls": [{"hosts": ["web.example"]}],
        "rules": [{"host": "web.example"}],
    },
    "status": {"loadBalancer": {"ingress": [{"ip": "1.2.3.4"}]}},
}
GATEWAY = {
    "metadata": {
        "name": "flex-gateway",
        "namespace": "envoy-gateway",
        "creationTimestamp": "2026-01-01T00:00:00Z",
    },
    "spec": {
        "gatewayClassName": "eg",
        "listeners": [
            {"name": "http", "protocol": "HTTP", "port": 80},
            {"name": "horizon-https", "protocol": "HTTPS", "port": 443},
        ],
    },
    "status": {"addresses": [{"type": "IPAddress", "value": "10.10.0.200"}]},
}
HTTPROUTE = {
    "metadata": {
        "name": "custom-horizon-gateway-route",
        "namespace": "openstack",
        "creationTimestamp": "2026-01-01T00:00:00Z",
    },
    "spec": {
        "parentRefs": [
            {
                "name": "flex-gateway",
                "namespace": "envoy-gateway",
                "sectionName": "horizon-https",
            }
        ],
        "hostnames": ["horizon.example.com"],
        "rules": [{"backendRefs": [{"name": "horizon-int", "port": 80}]}],
    },
}
METALLB_POOL = {
    "metadata": {
        "name": "primary",
        "namespace": "metallb-system",
        "creationTimestamp": "2026-01-01T00:00:00Z",
    },
    "spec": {"addresses": ["10.234.0.0/24"], "autoAssign": False},
}
PVC = {
    "metadata": {
        "name": "data",
        "namespace": "default",
        "creationTimestamp": "2026-01-01T00:00:00Z",
    },
    "spec": {
        "storageClassName": "general",
        "accessModes": ["ReadWriteOnce"],
        "volumeName": "pv-1",
        "resources": {"requests": {"storage": "10Gi"}},
    },
    "status": {"phase": "Bound", "capacity": {"storage": "10Gi"}},
}
PV = {
    "metadata": {"name": "pv-1", "creationTimestamp": "2026-01-01T00:00:00Z"},
    "spec": {
        "storageClassName": "general",
        "persistentVolumeReclaimPolicy": "Retain",
        "capacity": {"storage": "10Gi"},
        "accessModes": ["ReadWriteOnce"],
        "claimRef": {"namespace": "default", "name": "data"},
    },
    "status": {"phase": "Bound"},
}
STORAGECLASS = {
    "metadata": {
        "name": "general",
        "creationTimestamp": "2026-01-01T00:00:00Z",
        "annotations": {"storageclass.kubernetes.io/is-default-class": "true"},
    },
    "provisioner": "driver.example",
    "reclaimPolicy": "Delete",
    "volumeBindingMode": "WaitForFirstConsumer",
    "allowVolumeExpansion": True,
}
CONFIGMAP = {
    "metadata": {
        "name": "app-config",
        "namespace": "default",
        "creationTimestamp": "2026-01-01T00:00:00Z",
    },
    "data": {"foo": "bar", "hello": "world"},
}
SECRET = {
    "metadata": {
        "name": "app-secret",
        "namespace": "default",
        "creationTimestamp": "2026-01-01T00:00:00Z",
        "labels": {"app": "web"},
    },
    "type": "Opaque",
    "data": {"password": "c2VjcmV0", "token": "dG9rZW4="},
    "stringData": {"raw": "should-never-return"},
}
HELM_SECRET = {
    "type": "helm.sh/release.v1",
    "metadata": {
        "name": "sh.helm.release.v1.nova.v3",
        "namespace": "openstack",
        "creationTimestamp": "2026-01-01T00:00:00Z",
        "labels": {
            "owner": "helm",
            "name": "nova",
            "status": "deployed",
            "version": "3",
        },
    },
    "data": {"release": "THIS_IS_A_SECRET_BLOB_DO_NOT_RETURN"},
}
JOB = {
    "metadata": {
        "name": "backup",
        "namespace": "default",
        "creationTimestamp": "2026-01-01T00:00:00Z",
    },
    "spec": {"completions": 1, "parallelism": 1},
    "status": {"succeeded": 1, "failed": 0, "active": 0},
}
CORE_EVENT = {
    "type": "Warning",
    "reason": "FailedScheduling",
    "message": "0/1 nodes available",
    "count": 3,
    "lastTimestamp": "2026-01-01T00:00:02Z",
    "metadata": {"namespace": "default", "name": "web.2"},
    "involvedObject": {"kind": "Pod", "name": "web-abc"},
}


def _kubeconfig(path: Path) -> Path:
    doc = {
        "apiVersion": "v1",
        "clusters": [
            {
                "name": "fake",
                "cluster": {
                    "server": APISERVER,
                    "insecure-skip-tls-verify": True,
                },
            }
        ],
        "users": [{"name": "fake", "user": {"token": "k8s-token"}}],
        "contexts": [{"name": "fake", "context": {"cluster": "fake", "user": "fake"}}],
        "current-context": "fake",
    }
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return path


class RouterTransport(httpx.BaseTransport):
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.bodies: list[Any] = []
        self.content_types: list[str | None] = []
        self.node_pods: list[dict[str, Any]] = [DS_POD, MIRROR_POD, APP_POD]
        self.evict_status: dict[str, int] = {}
        self.delete_pod_status = 200
        self.gateway_api = True
        self.gateway_api_v1 = True
        self.metallb = True

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        method = request.method
        parsed = urlparse(url)
        path = parsed.path
        qs = parse_qs(parsed.query)
        self.calls.append((method, url))
        self.content_types.append(request.headers.get("content-type"))
        body: Any = None
        if request.content:
            try:
                body = json.loads(request.content)
            except (json.JSONDecodeError, UnicodeDecodeError):
                body = request.content
        self.bodies.append(body)

        if method == "GET" and path.endswith("/apis/apps/v1/deployments"):
            return httpx.Response(200, json={"items": [DEPLOYMENT]})
        if method == "GET" and path.endswith(
            "/apis/apps/v1/namespaces/default/deployments"
        ):
            return httpx.Response(200, json={"items": [DEPLOYMENT]})
        if method == "GET" and path.endswith("/apis/apps/v1/statefulsets"):
            return httpx.Response(200, json={"items": [STATEFULSET]})
        if method == "GET" and path.endswith(
            "/apis/apps/v1/namespaces/default/statefulsets"
        ):
            return httpx.Response(200, json={"items": [STATEFULSET]})
        if method == "GET" and path.endswith("/apis/apps/v1/daemonsets"):
            return httpx.Response(200, json={"items": [DAEMONSET]})
        if method == "GET" and path.endswith(
            "/apis/apps/v1/namespaces/kube-system/daemonsets"
        ):
            return httpx.Response(200, json={"items": [DAEMONSET]})
        if method == "GET" and path.endswith("/api/v1/pods"):
            selector = (qs.get("fieldSelector") or [""])[0]
            if selector.startswith("spec.nodeName="):
                return httpx.Response(200, json={"items": list(self.node_pods)})
            return httpx.Response(200, json={"items": [POD, TERMINATING_POD]})
        if method == "GET" and path.endswith("/api/v1/namespaces/default/pods"):
            return httpx.Response(200, json={"items": [POD]})
        if method == "GET" and path.endswith("/api/v1/nodes"):
            return httpx.Response(200, json={"items": [NODE]})
        if method == "GET" and path.endswith(
            "/apis/apps/v1/namespaces/default/deployments/web"
        ):
            return httpx.Response(200, json=DEPLOYMENT)
        if method == "GET" and path.endswith("/api/v1/nodes/node-1"):
            return httpx.Response(200, json=NODE)
        if method == "GET" and path.endswith("/api/v1/namespaces"):
            return httpx.Response(200, json={"items": [NAMESPACE]})
        if method == "GET" and path.endswith("/api/v1/services"):
            return httpx.Response(200, json={"items": [SERVICE]})
        if method == "GET" and path.endswith("/api/v1/namespaces/default/services"):
            return httpx.Response(200, json={"items": [SERVICE]})
        if method == "GET" and path.endswith("/apis/networking.k8s.io/v1/ingresses"):
            return httpx.Response(200, json={"items": [INGRESS]})
        if method == "GET" and "/apis/gateway.networking.k8s.io/" in path:
            if not self.gateway_api:
                return httpx.Response(
                    404, text="the server could not find the requested resource"
                )
            if "/v1/" in path and "/v1beta1/" not in path and not self.gateway_api_v1:
                return httpx.Response(
                    404, text="the server could not find the requested resource"
                )
            if path.rstrip("/").endswith("/gateways"):
                return httpx.Response(200, json={"items": [GATEWAY]})
            if path.rstrip("/").endswith("/httproutes"):
                return httpx.Response(200, json={"items": [HTTPROUTE]})
            return httpx.Response(404, text=f"no mock for {method} {url}")
        if method == "GET" and path.endswith("/apis/metallb.io/v1beta1/ipaddresspools"):
            if not self.metallb:
                return httpx.Response(
                    404, text="the server could not find the requested resource"
                )
            return httpx.Response(200, json={"items": [METALLB_POOL]})
        if method == "GET" and path.endswith("/api/v1/persistentvolumeclaims"):
            return httpx.Response(200, json={"items": [PVC]})
        if method == "GET" and path.endswith("/api/v1/persistentvolumes"):
            return httpx.Response(200, json={"items": [PV]})
        if method == "GET" and path.endswith("/apis/storage.k8s.io/v1/storageclasses"):
            return httpx.Response(200, json={"items": [STORAGECLASS]})
        if method == "GET" and path.endswith("/api/v1/configmaps"):
            return httpx.Response(200, json={"items": [CONFIGMAP]})
        if method == "GET" and path.endswith("/api/v1/secrets"):
            return httpx.Response(200, json={"items": [SECRET, HELM_SECRET]})
        if method == "GET" and path.endswith("/apis/batch/v1/jobs"):
            return httpx.Response(200, json={"items": [JOB]})
        if method == "GET" and path.endswith(
            "/api/v1/namespaces/default/configmaps/demo"
        ):
            return httpx.Response(
                200, json={"kind": "ConfigMap", "metadata": {"name": "demo"}}
            )
        if method == "GET" and path.endswith(
            "/api/v1/namespaces/default/secrets/app-secret"
        ):
            return httpx.Response(200, json=SECRET)
        if method == "POST" and path.endswith("/api/v1/namespaces"):
            return httpx.Response(
                201,
                json={
                    "metadata": {"name": (body or {}).get("metadata", {}).get("name")}
                },
            )
        if method == "DELETE" and "/apis/apps/v1/namespaces/" in path:
            return httpx.Response(200, json={"status": "Success"})
        if (
            method == "DELETE"
            and "/api/v1/namespaces/" in path
            and "/services/" in path
        ):
            return httpx.Response(200, json={"status": "Success"})
        if method == "DELETE" and "/persistentvolumeclaims/" in path:
            return httpx.Response(200, json={"status": "Success"})
        if (
            method == "DELETE"
            and "/api/v1/namespaces/" in path
            and path.strip("/").count("/") == 3
            and "/pods/" not in path
            and "/services/" not in path
        ):
            return httpx.Response(200, json={"status": "Success"})
        if method == "GET" and path.endswith("/api/v1/events"):
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "type": "Normal",
                            "reason": "ScalingReplicaSet",
                            "message": "Scaled up",
                            "count": 1,
                            "lastTimestamp": "2026-01-01T00:00:01Z",
                            "metadata": {"namespace": "default", "name": "web.1"},
                            "involvedObject": {"kind": "Deployment", "name": "web"},
                        },
                        CORE_EVENT,
                    ]
                },
            )
        if method == "GET" and "/events" in path:
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "type": "Normal",
                            "reason": "ScalingReplicaSet",
                            "message": "Scaled up",
                            "count": 1,
                            "lastTimestamp": "2026-01-01T00:00:01Z",
                            "metadata": {"namespace": "default", "name": "web.1"},
                            "involvedObject": {"kind": "Deployment", "name": "web"},
                        }
                    ]
                },
            )
        if method == "PATCH" and APPLY_CONTENT_TYPE in (
            request.headers.get("content-type") or ""
        ):
            return httpx.Response(
                200, json={"kind": "ConfigMap", "metadata": {"name": "demo"}}
            )
        if method == "PATCH" and "/apis/apps/v1/namespaces/" in path:
            return httpx.Response(200, json={"status": "patched"})
        if method == "PATCH" and "/api/v1/nodes/" in path:
            return httpx.Response(
                200,
                json={
                    "spec": (body or {}).get("spec") if isinstance(body, dict) else {}
                },
            )
        if method == "DELETE" and "/api/v1/namespaces/" in path and "/pods/" in path:
            return httpx.Response(self.delete_pod_status)
        if method == "POST" and path.endswith("/eviction"):
            # /api/v1/namespaces/{ns}/pods/{pod}/eviction
            parts = path.strip("/").split("/")
            ns = parts[3] if len(parts) > 5 else ""
            pod = parts[5] if len(parts) > 5 else ""
            key = f"{ns}/{pod}"
            status = self.evict_status.get(key, 201)
            return httpx.Response(
                status,
                json={"kind": "Eviction"} if status < 400 else {"status": "Failure"},
            )
        return httpx.Response(404, text=f"no mock for {method} {url}")


@pytest.fixture
def k8s_client(tmp_path, monkeypatch):
    kc = _kubeconfig(tmp_path / "kubeconfig")
    transport = RouterTransport()
    real_client = httpx.Client

    def fake_client(*args, **kwargs):
        kwargs.pop("verify", None)
        kwargs.pop("cert", None)
        kwargs["transport"] = transport
        kwargs.setdefault("base_url", APISERVER)
        return real_client(*args, **kwargs)

    monkeypatch.setattr("app.services.osclient.httpx.Client", fake_client)
    client = K8sClient(str(kc))
    client._http = httpx.Client(transport=transport, base_url=APISERVER)
    client._apiserver = APISERVER
    client._cleanup = []
    yield client, transport
    client.close()


def test_list_workloads_parses_items(k8s_client):
    client, _transport = k8s_client
    deps = client.list_deployments()
    assert deps[0]["kind"] == "Deployment"
    assert deps[0]["name"] == "web"
    assert deps[0]["namespace"] == "default"
    assert deps[0]["replicas"] == 3
    assert deps[0]["ready"] == 2
    assert deps[0]["available"] == 2
    assert deps[0]["updated"] == 3
    assert deps[0]["images"] == ["nginx:1.25"]
    assert deps[0]["age"] == "2026-01-01T00:00:00Z"

    sts = client.list_statefulsets("default")
    assert sts[0]["kind"] == "StatefulSet"
    assert sts[0]["replicas"] == 1
    assert sts[0]["images"] == ["postgres:16"]

    dss = client.list_daemonsets()
    assert dss[0]["kind"] == "DaemonSet"
    assert dss[0]["replicas"] == 2
    assert dss[0]["ready"] == 2

    pods = client.list_pods()
    by_name = {p["name"]: p for p in pods}
    assert by_name["web-abc"]["phase"] == "Running"
    assert by_name["web-abc"]["ready"] == "1/1"
    assert by_name["web-abc"]["restarts"] == 1
    assert by_name["web-abc"]["controllers"] == ["ReplicaSet/web-xyz"]
    assert by_name["web-abc"]["node"] == "node-1"
    assert "deletion_timestamp" not in by_name["web-abc"]
    assert by_name["web-old"]["deletion_timestamp"] == "2026-01-05T00:00:00Z"


def test_scale_sends_strategic_merge_patch(k8s_client):
    client, transport = k8s_client
    client.scale("deployments", "default", "web", 5)
    assert any(
        m == "PATCH"
        and u.split("?", 1)[0].endswith(
            "/apis/apps/v1/namespaces/default/deployments/web"
        )
        for m, u in transport.calls
    )
    assert {"spec": {"replicas": 5}} in transport.bodies
    assert PATCH_CONTENT_TYPE in transport.content_types


def test_restart_sets_restarted_at(k8s_client):
    client, transport = k8s_client
    client.restart("deployments", "default", "web")
    patched = [
        b
        for b in transport.bodies
        if isinstance(b, dict) and RESTART_ANNOTATION in str(b)
    ]
    assert patched
    stamp = patched[0]["spec"]["template"]["metadata"]["annotations"][
        RESTART_ANNOTATION
    ]
    assert stamp.endswith("Z")
    assert "T" in stamp
    assert PATCH_CONTENT_TYPE in transport.content_types
    assert any(
        m == "PATCH"
        and u.split("?", 1)[0].endswith(
            "/apis/apps/v1/namespaces/default/deployments/web"
        )
        for m, u in transport.calls
    )


def test_delete_pod_404_is_ok(k8s_client):
    client, transport = k8s_client
    transport.delete_pod_status = 404
    result = client.delete_pod("default", "web-abc")
    assert result == {"ok": True, "message": "already gone"}


def test_delete_pod_success(k8s_client):
    client, _transport = k8s_client
    result = client.delete_pod("default", "web-abc")
    assert result["ok"] is True
    assert "deleted" in result["message"]


def test_cordon_patch_unschedulable_true(k8s_client):
    client, transport = k8s_client
    client.set_unschedulable("node-1", True)
    assert {"spec": {"unschedulable": True}} in transport.bodies
    assert any(m == "PATCH" and "/api/v1/nodes/node-1" in u for m, u in transport.calls)
    assert PATCH_CONTENT_TYPE in transport.content_types


def test_uncordon_patch_unschedulable_false(k8s_client):
    client, transport = k8s_client
    client.set_unschedulable("node-1", False)
    assert {"spec": {"unschedulable": False}} in transport.bodies


def test_drain_skips_daemonset_and_mirror_evicts_others(k8s_client):
    client, transport = k8s_client
    result = client.drain_node("node-1")
    assert result["cordoned"] is True
    assert result["ok"] is True
    assert "kube-system/kindnet-node-1" in result["skipped"]
    assert "kube-system/kube-apiserver-node-1" in result["skipped"]
    assert "default/web-abc" in result["evicted"]
    assert result["blocked"] == []
    assert any(
        m == "POST" and u.endswith("/namespaces/default/pods/web-abc/eviction")
        for m, u in transport.calls
    )
    assert not any("kindnet-node-1/eviction" in u for _, u in transport.calls)
    cordon = [
        b
        for b in transport.bodies
        if isinstance(b, dict) and (b.get("spec") or {}).get("unschedulable") is True
    ]
    assert cordon
    evictions = [
        b
        for b in transport.bodies
        if isinstance(b, dict) and b.get("kind") == "Eviction"
    ]
    assert evictions
    assert evictions[0]["apiVersion"] == "policy/v1"
    assert evictions[0]["metadata"]["name"] == "web-abc"
    assert evictions[0]["metadata"]["namespace"] == "default"


def test_drain_pdb_429_is_blocked(k8s_client):
    client, transport = k8s_client
    transport.node_pods = [APP_POD, PDB_POD]
    transport.evict_status["default/locked"] = 429
    result = client.drain_node("node-1")
    assert result["cordoned"] is True
    assert "default/web-abc" in result["evicted"]
    assert any("default/locked" in b and "PDB blocked" in b for b in result["blocked"])
    assert result["ok"] is False


def test_drain_rejects_invalid_name(k8s_client):
    client, _transport = k8s_client
    with pytest.raises(K8sError, match="invalid node name"):
        client.drain_node("../etc/passwd")


def test_describe_returns_object_and_events(k8s_client):
    client, _transport = k8s_client
    result = client.describe("deployments", "default", "web")
    assert result["ok"] is True
    assert result["object"]["metadata"]["name"] == "web"
    assert result["events"]
    assert result["events"][0]["reason"] == "ScalingReplicaSet"
    assert result["error"] is None


def test_scale_rejects_daemonsets(k8s_client):
    client, _transport = k8s_client
    with pytest.raises(K8sError, match="cannot scale"):
        client.scale("daemonsets", "kube-system", "kindnet", 1)


def test_list_nodes_includes_labels_taints_capacity(k8s_client):
    client, _transport = k8s_client
    nodes = client.list_nodes()
    assert nodes[0]["name"] == "node-1"
    assert nodes[0]["unschedulable"] is False
    assert nodes[0]["status"] == "Ready"
    assert nodes[0]["internal_ip"] == "10.200.0.51"
    assert "control-plane" in nodes[0]["roles"]
    assert nodes[0]["labels"]["kubernetes.io/hostname"] == "node-1"
    assert nodes[0]["taints"][0]["key"] == "dedicated"
    assert nodes[0]["capacity"]["cpu"] == "8"
    assert nodes[0]["conditions"][0]["type"] == "Ready"


def test_list_secrets_strips_data(k8s_client):
    client, _transport = k8s_client
    secrets = client.list_secrets()
    by_name = {s["name"]: s for s in secrets}
    row = by_name["app-secret"]
    assert "data" not in row
    assert "stringData" not in row
    assert "string_data" not in row
    assert row["keys"] == ["password", "token"]
    assert row["type"] == "Opaque"
    blob = json.dumps(row)
    assert "c2VjcmV0" not in blob
    assert "should-never-return" not in blob
    assert "THIS_IS_A_SECRET_BLOB_DO_NOT_RETURN" not in json.dumps(secrets)


def test_describe_secret_strips_data(k8s_client):
    client, _transport = k8s_client
    result = client.describe("secrets", "default", "app-secret")
    obj = result["object"]
    assert "data" not in obj
    assert "stringData" not in obj
    assert "c2VjcmV0" not in json.dumps(result)


def test_gateway_httproute_metallb_rows():
    gw = _gateway_row(GATEWAY)
    assert gw["namespace"] == "envoy-gateway"
    assert gw["name"] == "flex-gateway"
    assert gw["class"] == "eg"
    assert gw["addresses"] == ["10.10.0.200"]
    assert gw["address"] == "10.10.0.200"
    assert gw["listeners"] == ["HTTP/80/http", "HTTPS/443/horizon-https"]
    assert gw["age"] == "2026-01-01T00:00:00Z"

    route = _httproute_row(HTTPROUTE)
    assert route["namespace"] == "openstack"
    assert route["name"] == "custom-horizon-gateway-route"
    assert route["hosts"] == ["horizon.example.com"]
    assert route["parent_refs"] == [
        {"namespace": "envoy-gateway", "name": "flex-gateway", "kind": "Gateway"}
    ]
    assert route["backends"] == [
        {"namespace": "openstack", "name": "horizon-int", "kind": "Service", "port": 80}
    ]
    assert route["urls"] == ["https://horizon.example.com"]
    assert route["tls"] is False
    assert route["age"] == "2026-01-01T00:00:00Z"

    pool = _metallb_pool_row(METALLB_POOL)
    assert pool["name"] == "primary"
    assert pool["addresses"] == ["10.234.0.0/24"]
    assert pool["auto_assign"] is False
    assert pool["age"] == "2026-01-01T00:00:00Z"


def test_list_gateways_fallback_and_missing_crd(k8s_client):
    client, transport = k8s_client
    transport.gateway_api_v1 = False
    gws = client.list_gateways()
    assert gws[0]["name"] == "flex-gateway"
    routes = client.list_httproutes("openstack")
    assert routes[0]["name"] == "custom-horizon-gateway-route"
    transport.gateway_api = False
    transport.metallb = False
    assert client.list_gateways() == []
    assert client.list_httproutes() == []
    assert client.list_metallb_pools() == []


def test_list_day2_collections(k8s_client):
    client, _transport = k8s_client
    events = client.list_events()
    assert any(e["reason"] == "FailedScheduling" for e in events)
    nss = client.list_namespaces()
    assert nss[0]["name"] == "default"
    svcs = client.list_services()
    assert svcs[0]["cluster_ip"] == "10.96.0.10"
    ings = client.list_ingresses()
    assert ings[0]["hosts"] == ["web.example"]
    assert ings[0]["tls"] is True
    gws = client.list_gateways()
    assert gws[0]["name"] == "flex-gateway"
    assert gws[0]["addresses"] == ["10.10.0.200"]
    routes = client.list_httproutes()
    assert routes[0]["hosts"] == ["horizon.example.com"]
    pools = client.list_metallb_pools()
    assert pools[0]["name"] == "primary"
    pvcs = client.list_pvcs()
    assert pvcs[0]["phase"] == "Bound"
    pvs = client.list_pvs()
    assert pvs[0]["claim"] == "default/data"
    scs = client.list_storageclasses()
    assert scs[0]["default"] is True
    cms = client.list_configmaps()
    assert cms[0]["keys"] == ["foo", "hello"]
    assert "bar" not in json.dumps(cms)
    jobs = client.list_jobs()
    assert jobs[0]["succeeded"] == 1


def test_apply_yaml_server_side_apply(k8s_client):
    client, transport = k8s_client
    text = (
        "apiVersion: v1\n"
        "kind: ConfigMap\n"
        "metadata:\n"
        "  name: demo\n"
        "  namespace: default\n"
        "data:\n"
        "  foo: bar\n"
    )
    result = client.apply_yaml(text)
    assert result["ok"] is True
    assert result["applied"][0]["name"] == "demo"
    assert any(
        m == "PATCH" and "fieldManager=" + APPLY_FIELD_MANAGER in u
        for m, u in transport.calls
    )
    assert APPLY_CONTENT_TYPE in transport.content_types


def test_taint_and_label_node(k8s_client):
    client, transport = k8s_client
    result = client.add_taint("node-1", "spot", "true", "NoSchedule")
    assert result["ok"] is True
    assert any(t["key"] == "spot" for t in result["taints"])
    assert MERGE_PATCH_CONTENT_TYPE in transport.content_types
    removed = client.remove_taint("node-1", "dedicated", "NoSchedule")
    assert removed["ok"] is True
    labeled = client.set_label("node-1", "role", "worker")
    assert labeled["ok"] is True
    assert {"metadata": {"labels": {"role": "worker"}}} in transport.bodies


def test_create_and_delete_namespace(k8s_client):
    client, _transport = k8s_client
    created = client.create_namespace("apps")
    assert created["ok"] is True
    deleted = client.delete_namespace("apps")
    assert deleted["ok"] is True
    with pytest.raises(K8sError, match="protected"):
        client.delete_namespace("kube-system")


def test_delete_workload_service_pvc(k8s_client):
    client, _transport = k8s_client
    assert client.delete_workload("deployments", "default", "web")["ok"] is True
    assert client.delete_service("default", "web")["ok"] is True
    assert client.delete_pvc("default", "data")["ok"] is True


def test_helm_list_from_cli(k8s_client, monkeypatch):
    client, _transport = k8s_client

    def fake_helm(_kube, args, **_kwargs):
        assert args[:2] == ["list", "-A"]
        return json.dumps(
            [
                {
                    "name": "nova",
                    "namespace": "openstack",
                    "revision": "4",
                    "status": "deployed",
                    "chart": "nova-2025.1.0",
                    "app_version": "2025.1",
                    "updated": "2026-01-01",
                }
            ]
        )

    monkeypatch.setattr("app.services.k8sclient.run_helm", fake_helm)
    rows = client.list_helm_releases()
    assert rows[0]["name"] == "nova"
    assert rows[0]["revision"] == 4
    assert rows[0]["chart"] == "nova-2025.1.0"


def test_helm_list_falls_back_to_secrets(k8s_client, monkeypatch):
    client, _transport = k8s_client

    def fake_helm(*_a, **_k):
        raise K8sError("helm not found on PATH")

    monkeypatch.setattr("app.services.k8sclient.run_helm", fake_helm)
    rows = client.list_helm_releases()
    assert rows[0]["name"] == "nova"
    assert rows[0]["namespace"] == "openstack"
    assert rows[0]["revision"] == 3
    assert "THIS_IS_A_SECRET_BLOB" not in json.dumps(rows)
    assert all("data" not in r for r in rows)


def test_helm_status_strips_values(k8s_client, monkeypatch):
    client, _transport = k8s_client

    def fake_helm(_kube, args, **_kwargs):
        assert args[0] == "status"
        return json.dumps(
            {
                "name": "nova",
                "namespace": "openstack",
                "version": 4,
                "info": {
                    "status": {"status": "deployed"},
                    "description": "Upgrade complete",
                    "last_deployed": "2026-01-01T00:00:00Z",
                },
                "chart": {
                    "metadata": {
                        "name": "nova",
                        "version": "2025.1.0",
                        "appVersion": "2025.1",
                    }
                },
                "config": {"password": "SUPERSECRET"},
                "manifest": "kind: Secret\ndata:\n  x: y\n",
            }
        )

    monkeypatch.setattr("app.services.k8sclient.run_helm", fake_helm)
    status = client.helm_status("openstack", "nova")
    blob = json.dumps(status)
    assert "SUPERSECRET" not in blob
    assert "manifest" not in status
    assert "config" not in status
    assert status["chart"] == "nova"
    assert status["revision"] == 4


def test_helm_rollback_and_upgrade(k8s_client, monkeypatch):
    client, _transport = k8s_client
    calls: list[list[str]] = []

    def fake_helm(_kube, args, **_kwargs):
        calls.append(list(args))
        if args[0] == "get":
            return json.dumps({"chart": "nova", "name": "nova"})
        return ""

    monkeypatch.setattr("app.services.k8sclient.run_helm", fake_helm)
    rb = client.helm_rollback("openstack", "nova", 2)
    assert rb["ok"] is True
    up = client.helm_upgrade("openstack", "nova", values={"replicaCount": 3})
    assert up["ok"] is True
    assert any(c[0] == "rollback" and "2" in c for c in calls)
    upgrade = [c for c in calls if c[0] == "upgrade"][0]
    assert "--reuse-values" in upgrade
    assert "-f" in upgrade
    assert "SUPERSECRET" not in json.dumps(calls)


def test_run_helm_not_found(monkeypatch, tmp_path):
    monkeypatch.setattr("app.services.k8sclient.shutil.which", lambda _n: None)
    with pytest.raises(K8sError, match="not found"):
        run_helm(str(tmp_path / "kube"), ["list"])


def test_pod_row_ignores_laststate_error_when_ready():
    row = _pod_row(
        {
            "metadata": {"name": "kube-ovn-pinger-x", "namespace": "kube-system"},
            "spec": {"nodeName": "n1", "containers": [{"name": "pinger"}]},
            "status": {
                "phase": "Running",
                "containerStatuses": [
                    {
                        "ready": True,
                        "restartCount": 3,
                        "state": {"running": {"startedAt": "2026-09-04T00:00:00Z"}},
                        "lastState": {"terminated": {"reason": "Error", "exitCode": 1}},
                    }
                ],
            },
        }
    )
    assert row["ready"] == "1/1"
    assert row["reason"] is None


def test_pod_row_reports_crashloop_waiting():
    row = _pod_row(
        {
            "metadata": {"name": "cinder-volume-x", "namespace": "openstack"},
            "spec": {"nodeName": "n1", "containers": [{"name": "cinder-volume"}]},
            "status": {
                "phase": "Running",
                "containerStatuses": [
                    {
                        "ready": False,
                        "restartCount": 8,
                        "state": {"waiting": {"reason": "CrashLoopBackOff"}},
                        "lastState": {"terminated": {"reason": "Error", "exitCode": 1}},
                    }
                ],
            },
        }
    )
    assert row["ready"] == "0/1"
    assert row["reason"] == "CrashLoopBackOff"
