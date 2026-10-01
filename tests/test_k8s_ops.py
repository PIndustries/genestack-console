"""Kubernetes ops wrappers and router tests."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import httpx

from app.main import app
from app.models import Environment
from app.routers import k8s as k8s_router
from app.services import k8s_ops
from app.services.k8sclient import K8sClient
from tests.test_k8sclient import APISERVER, RouterTransport, _kubeconfig

if not any(getattr(r, "path", "").endswith("/k8s/workloads") for r in app.routes):
    app.include_router(k8s_router.router)


def _env(**kwargs) -> Environment:
    kwargs.setdefault("id", "env-k8s-1")
    kwargs.setdefault("name", "env-k8s-1")
    kwargs.setdefault("dry_run", False)
    return Environment(**kwargs)


def _attach_transport(monkeypatch, tmp_path) -> tuple[Path, RouterTransport]:
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
    return kc, transport


def test_ops_list_gateways_httproutes_metallb(tmp_path, monkeypatch):
    kc, _transport = _attach_transport(monkeypatch, tmp_path)
    env = _env(kubeconfig_path=str(kc))
    gateways = k8s_ops.list_gateways(env, namespace="envoy-gateway")
    assert gateways["ok"] is True
    assert gateways["namespace"] == "envoy-gateway"
    assert gateways["gateways"][0]["name"] == "flex-gateway"
    routes = k8s_ops.list_httproutes(env)
    assert routes["ok"] is True
    assert routes["httproutes"][0]["urls"] == ["https://horizon.example.com"]
    pools = k8s_ops.list_metallb_pools(env)
    assert pools["ok"] is True
    assert pools["pools"][0]["auto_assign"] is False
    empty = k8s_ops.list_gateways(_env())
    assert empty["ok"] is False
    assert empty["gateways"] == []


def test_ops_list_workloads_parses_via_client(tmp_path, monkeypatch):
    kc, _transport = _attach_transport(monkeypatch, tmp_path)
    env = _env(kubeconfig_path=str(kc))
    result = k8s_ops.list_workloads(env)
    assert result["ok"] is True
    assert result["error"] is None
    assert result["deployments"][0]["name"] == "web"
    assert result["statefulsets"][0]["name"] == "db"
    assert result["daemonsets"][0]["name"] == "kindnet"
    assert result["pods"][0]["name"] in {"web-abc", "web-old"}


def test_ops_list_workloads_missing_kubeconfig():
    result = k8s_ops.list_workloads(_env())
    assert result["ok"] is False
    assert result["deployments"] == []
    assert result["statefulsets"] == []
    assert result["daemonsets"] == []
    assert result["pods"] == []
    assert "kubeconfig" in (result["error"] or "")


def test_ops_list_workloads_api_failure_is_ok_false(tmp_path, monkeypatch):
    kc, _transport = _attach_transport(monkeypatch, tmp_path)

    def boom(*_a, **_k):
        raise RuntimeError("apiserver down")

    monkeypatch.setattr(K8sClient, "list_deployments", boom)
    env = _env(kubeconfig_path=str(kc))
    result = k8s_ops.list_workloads(env)
    assert result["ok"] is False
    assert result["deployments"] == []
    assert "apiserver down" in (result["error"] or "")


def test_ops_scale_dry_run_skips_kube(tmp_path, monkeypatch):
    kc, transport = _attach_transport(monkeypatch, tmp_path)
    env = _env(kubeconfig_path=str(kc), dry_run=True)
    result = k8s_ops.scale_workload(
        env, kind="deployments", namespace="default", name="web", replicas=4
    )
    assert result["ok"] is True
    assert result["dry_run"] is True
    assert transport.calls == []


def test_ops_scale_sends_patch(tmp_path, monkeypatch):
    kc, transport = _attach_transport(monkeypatch, tmp_path)
    env = _env(kubeconfig_path=str(kc), dry_run=False)
    result = k8s_ops.scale_workload(
        env, kind="deployments", namespace="default", name="web", replicas=4
    )
    assert result["ok"] is True
    assert {"spec": {"replicas": 4}} in transport.bodies


def test_ops_restart_dry_run(tmp_path, monkeypatch):
    kc, transport = _attach_transport(monkeypatch, tmp_path)
    env = _env(kubeconfig_path=str(kc), dry_run=True)
    result = k8s_ops.restart_workload(
        env, kind="statefulsets", namespace="default", name="db"
    )
    assert result == {
        "ok": True,
        "dry_run": True,
        "error": None,
        "message": "restart statefulsets/default/db",
    }
    assert transport.calls == []


def test_ops_delete_pod_404(tmp_path, monkeypatch):
    kc, transport = _attach_transport(monkeypatch, tmp_path)
    transport.delete_pod_status = 404
    env = _env(kubeconfig_path=str(kc), dry_run=False)
    result = k8s_ops.delete_pod(env, namespace="default", name="web-abc")
    assert result["ok"] is True
    assert result["message"] == "already gone"


def test_ops_cordon(tmp_path, monkeypatch):
    kc, transport = _attach_transport(monkeypatch, tmp_path)
    env = _env(kubeconfig_path=str(kc), dry_run=False)
    result = k8s_ops.set_node_schedulable(env, name="node-1", unschedulable=True)
    assert result["ok"] is True
    assert {"spec": {"unschedulable": True}} in transport.bodies


def test_ops_drain_skips_daemonsets(tmp_path, monkeypatch):
    kc, _transport = _attach_transport(monkeypatch, tmp_path)
    env = _env(kubeconfig_path=str(kc), dry_run=False)
    result = k8s_ops.drain_node(env, name="node-1")
    assert result["cordoned"] is True
    assert "kube-system/kindnet-node-1" in result["skipped"]
    assert "default/web-abc" in result["evicted"]


def test_ops_drain_invalid_name_does_not_call_kube():
    with patch.object(k8s_ops, "K8sClient") as mock_client:
        result = k8s_ops.drain_node(_env(dry_run=False), name="../etc/passwd")
    assert result["ok"] is False
    assert "invalid" in (result["error"] or "")
    mock_client.assert_not_called()


def test_ops_describe_missing_kubeconfig():
    result = k8s_ops.describe(_env(), kind="pods", namespace="default", name="web-abc")
    assert result["ok"] is False
    assert result["object"] is None
    assert result["events"] == []
    assert "kubeconfig" in (result["error"] or "")


def test_router_invalid_names_rejected(client, admin_headers):
    env = client.post(
        "/api/v1/environments", headers=admin_headers, json={"name": "k8s-bad-name"}
    ).json()
    eid = env["id"]
    resp = client.post(
        f"/api/v1/environments/{eid}/k8s/workloads/deployments/Default/web/scale",
        headers=admin_headers,
        json={"replicas": 1},
    )
    assert resp.status_code == 400, resp.text
    resp = client.post(
        f"/api/v1/environments/{eid}/k8s/workloads/deployments/default/Not_Valid/scale",
        headers=admin_headers,
        json={"replicas": 1},
    )
    assert resp.status_code == 400, resp.text
    resp = client.post(
        f"/api/v1/environments/{eid}/k8s/nodes/../etc/passwd/cordon",
        headers=admin_headers,
    )
    assert resp.status_code in (400, 404)
    resp = client.post(
        f"/api/v1/environments/{eid}/k8s/nodes/BAD_NODE/drain",
        headers=admin_headers,
        json={},
    )
    assert resp.status_code == 400
    resp = client.get(
        f"/api/v1/environments/{eid}/k8s/describe",
        headers=admin_headers,
        params={"kind": "pods", "namespace": "UPPER", "name": "web"},
    )
    assert resp.status_code == 400
    resp = client.post(
        f"/api/v1/environments/{eid}/k8s/workloads/daemonsets/kube-system/kindnet/scale",
        headers=admin_headers,
        json={"replicas": 1},
    )
    assert resp.status_code == 400


def test_router_missing_kubeconfig_workloads_200(client, admin_headers):
    env = client.post(
        "/api/v1/environments",
        headers=admin_headers,
        json={"name": "k8s-no-kube"},
    ).json()
    resp = client.get(
        f"/api/v1/environments/{env['id']}/k8s/workloads",
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is False
    assert body["deployments"] == []
    assert body["statefulsets"] == []
    assert body["daemonsets"] == []
    assert body["pods"] == []
    assert body["error"]


def test_router_workloads_ok_with_kube(client, admin_headers, tmp_path, monkeypatch):
    kc, _transport = _attach_transport(monkeypatch, tmp_path)
    env = client.post(
        "/api/v1/environments",
        headers=admin_headers,
        json={"name": "k8s-live", "kubeconfig_path": str(kc), "dry_run": False},
    ).json()
    resp = client.get(
        f"/api/v1/environments/{env['id']}/k8s/workloads",
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["deployments"][0]["name"] == "web"


def test_router_scale_and_restart(client, admin_headers, tmp_path, monkeypatch):
    kc, transport = _attach_transport(monkeypatch, tmp_path)
    env = client.post(
        "/api/v1/environments",
        headers=admin_headers,
        json={"name": "k8s-mutate", "kubeconfig_path": str(kc), "dry_run": False},
    ).json()
    eid = env["id"]
    resp = client.post(
        f"/api/v1/environments/{eid}/k8s/workloads/deployments/default/web/scale",
        headers=admin_headers,
        json={"replicas": 7},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True
    assert {"spec": {"replicas": 7}} in transport.bodies

    resp = client.post(
        f"/api/v1/environments/{eid}/k8s/workloads/deployments/default/web/restart",
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True

    resp = client.delete(
        f"/api/v1/environments/{eid}/k8s/pods/default/web-abc",
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True

    resp = client.post(
        f"/api/v1/environments/{eid}/k8s/nodes/node-1/cordon",
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True


def test_router_viewer_cannot_scale(client, admin_headers, viewer_headers):
    env = client.post(
        "/api/v1/environments",
        headers=admin_headers,
        json={"name": "k8s-viewer-deny"},
    ).json()
    resp = client.post(
        f"/api/v1/environments/{env['id']}/k8s/workloads/deployments/default/web/scale",
        headers=viewer_headers,
        json={"replicas": 1},
    )
    assert resp.status_code == 403


def test_validate_helpers():
    assert k8s_ops.validate_namespace("kube-system") is None
    assert k8s_ops.validate_namespace("") is not None
    assert k8s_ops.validate_namespace("", allow_empty=True) is None
    assert k8s_ops.validate_namespace("Default") is not None
    assert k8s_ops.validate_name("server-1.example.com") is None
    assert k8s_ops.validate_name("../etc/passwd") is not None
    assert k8s_ops.validate_name("Not_Valid") is not None


def _live_env(client, admin_headers, tmp_path, monkeypatch, name="k8s-day2"):
    kc, transport = _attach_transport(monkeypatch, tmp_path)
    env = client.post(
        "/api/v1/environments",
        headers=admin_headers,
        json={"name": name, "kubeconfig_path": str(kc), "dry_run": False},
    ).json()
    return env["id"], transport


def test_router_lists_and_secrets_have_no_data(
    client, admin_headers, tmp_path, monkeypatch
):
    eid, _transport = _live_env(
        client, admin_headers, tmp_path, monkeypatch, "k8s-lists"
    )
    reads = [
        "events",
        "namespaces",
        "services",
        "ingresses",
        "gateways",
        "httproutes",
        "persistentvolumeclaims",
        "persistentvolumes",
        "storageclasses",
        "configmaps",
        "secrets",
        "jobs",
        "nodes",
    ]
    for name in reads:
        resp = client.get(
            f"/api/v1/environments/{eid}/k8s/{name}", headers=admin_headers
        )
        assert resp.status_code == 200, f"{name}: {resp.text}"
        body = resp.json()
        assert body["ok"] is True, name
        assert "data" not in body
    pools_resp = client.get(
        f"/api/v1/environments/{eid}/k8s/metallb/pools", headers=admin_headers
    )
    assert pools_resp.status_code == 200, pools_resp.text
    pools_body = pools_resp.json()
    assert pools_body["ok"] is True
    assert pools_body["pools"][0]["name"] == "primary"
    secrets_resp = client.get(
        f"/api/v1/environments/{eid}/k8s/secrets", headers=admin_headers
    )
    secrets = secrets_resp.json()
    assert secrets["secrets"]
    blob = secrets_resp.text + json.dumps(secrets)
    assert "c2VjcmV0" not in blob
    assert "should-never-return" not in blob
    assert "THIS_IS_A_SECRET_BLOB" not in blob
    for row in secrets["secrets"]:
        assert "data" not in row
        assert "stringData" not in row
        assert "c2VjcmV0" not in str(row)


def test_router_viewer_forbidden_on_mutates(
    client, admin_headers, viewer_headers, tmp_path, monkeypatch
):
    eid, _transport = _live_env(
        client, admin_headers, tmp_path, monkeypatch, "k8s-viewer-mut"
    )
    mutates = [
        ("POST", f"/api/v1/environments/{eid}/k8s/namespaces", {"name": "apps"}),
        ("DELETE", f"/api/v1/environments/{eid}/k8s/namespaces/apps", None),
        (
            "POST",
            f"/api/v1/environments/{eid}/k8s/nodes/node-1/taint",
            {"key": "dedicated", "value": "gpu", "effect": "NoSchedule"},
        ),
        (
            "DELETE",
            f"/api/v1/environments/{eid}/k8s/nodes/node-1/taint",
            {"key": "dedicated", "effect": "NoSchedule"},
        ),
        (
            "POST",
            f"/api/v1/environments/{eid}/k8s/nodes/node-1/label",
            {"key": "role", "value": "worker"},
        ),
        (
            "POST",
            f"/api/v1/environments/{eid}/k8s/apply",
            {"yaml": "apiVersion: v1\nkind: Namespace\nmetadata:\n  name: demo\n"},
        ),
        ("POST", f"/api/v1/environments/{eid}/k8s/helm/openstack/nova/rollback", {}),
        (
            "POST",
            f"/api/v1/environments/{eid}/k8s/helm/openstack/nova/upgrade",
            {"chart": "nova"},
        ),
        (
            "DELETE",
            f"/api/v1/environments/{eid}/k8s/workloads/deployments/default/web",
            None,
        ),
        ("DELETE", f"/api/v1/environments/{eid}/k8s/services/default/web", None),
        (
            "DELETE",
            f"/api/v1/environments/{eid}/k8s/persistentvolumeclaims/default/data",
            None,
        ),
    ]
    for method, url, body in mutates:
        resp = client.request(method, url, headers=viewer_headers, json=body)
        assert (
            resp.status_code == 403
        ), f"{method} {url}: {resp.status_code} {resp.text}"


def test_router_viewer_can_read(
    client, admin_headers, viewer_headers, tmp_path, monkeypatch
):
    eid, _transport = _live_env(
        client, admin_headers, tmp_path, monkeypatch, "k8s-viewer-read"
    )
    resp = client.get(f"/api/v1/environments/{eid}/k8s/secrets", headers=viewer_headers)
    assert resp.status_code == 200
    assert "c2VjcmV0" not in resp.text
    resp = client.get(f"/api/v1/environments/{eid}/k8s/nodes", headers=viewer_headers)
    assert resp.status_code == 200
    assert resp.json()["nodes"][0]["name"] == "node-1"


def test_router_apply_and_namespace(client, admin_headers, tmp_path, monkeypatch):
    eid, transport = _live_env(
        client, admin_headers, tmp_path, monkeypatch, "k8s-apply"
    )
    yaml_text = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: demo\n  namespace: default\n"
    resp = client.post(
        f"/api/v1/environments/{eid}/k8s/apply",
        headers=admin_headers,
        json={"yaml": yaml_text, "run_sync": True},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["operation"] == "k8s.apply"
    assert body["job_id"]
    resp = client.post(
        f"/api/v1/environments/{eid}/k8s/namespaces",
        headers=admin_headers,
        json={"name": "apps"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True
    resp = client.delete(
        f"/api/v1/environments/{eid}/k8s/workloads/deployments/default/web",
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text
    assert any(m == "DELETE" and "/deployments/web" in u for m, u in transport.calls)


def test_router_apply_dry_run_skips_when_no_kube(client, admin_headers):
    env = client.post(
        "/api/v1/environments",
        headers=admin_headers,
        json={"name": "k8s-apply-dry", "dry_run": True},
    ).json()
    resp = client.post(
        f"/api/v1/environments/{env['id']}/k8s/apply",
        headers=admin_headers,
        json={
            "yaml": "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: demo\n  namespace: default\n",
            "run_sync": True,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["operation"] == "k8s.apply"
    assert body["job_id"]
    assert body["status"] == "success"
    assert body["dry_run"] is True


def test_router_taint_label(client, admin_headers, tmp_path, monkeypatch):
    eid, _transport = _live_env(
        client, admin_headers, tmp_path, monkeypatch, "k8s-taint"
    )
    resp = client.post(
        f"/api/v1/environments/{eid}/k8s/nodes/node-1/taint",
        headers=admin_headers,
        json={"key": "spot", "value": "true", "effect": "NoSchedule"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True
    resp = client.request(
        "DELETE",
        f"/api/v1/environments/{eid}/k8s/nodes/node-1/taint",
        headers=admin_headers,
        json={"key": "dedicated", "effect": "NoSchedule"},
    )
    assert resp.status_code == 200, resp.text
    resp = client.post(
        f"/api/v1/environments/{eid}/k8s/nodes/node-1/label",
        headers=admin_headers,
        json={"key": "role", "value": "worker"},
    )
    assert resp.status_code == 200, resp.text


def test_router_helm_mocked(client, admin_headers, tmp_path, monkeypatch):
    def fake_helm(_kube, args, **_kwargs):
        if args[0] == "list":
            return json.dumps(
                [
                    {
                        "name": "nova",
                        "namespace": "openstack",
                        "revision": "2",
                        "status": "deployed",
                        "chart": "nova-2025.1.0",
                        "app_version": "2025.1",
                    }
                ]
            )
        if args[0] == "history":
            return json.dumps(
                [
                    {
                        "revision": 1,
                        "status": "superseded",
                        "chart": "nova-2025.1.0",
                        "description": "Install",
                    }
                ]
            )
        if args[0] == "status":
            return json.dumps(
                {
                    "name": "nova",
                    "namespace": "openstack",
                    "version": 2,
                    "info": {"status": {"status": "deployed"}, "description": "ok"},
                    "chart": {"metadata": {"name": "nova", "version": "2025.1.0"}},
                    "config": {"password": "NOPE"},
                }
            )
        if args[0] in {"rollback", "upgrade", "get"}:
            if args[0] == "get":
                return json.dumps({"chart": "nova"})
            return ""
        return "[]"

    monkeypatch.setattr("app.services.k8sclient.run_helm", fake_helm)
    eid, _transport = _live_env(
        client, admin_headers, tmp_path, monkeypatch, "k8s-helm"
    )
    resp = client.get(f"/api/v1/environments/{eid}/k8s/helm", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["releases"][0]["name"] == "nova"
    assert body["releases"][0]["revision"] == 2
    resp = client.get(
        f"/api/v1/environments/{eid}/k8s/helm/openstack/nova", headers=admin_headers
    )
    assert resp.status_code == 200, resp.text
    assert "NOPE" not in resp.text
    assert "password" not in resp.text
    resp = client.get(
        f"/api/v1/environments/{eid}/k8s/helm/openstack/nova/history",
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["history"][0]["revision"] == 1
    resp = client.post(
        f"/api/v1/environments/{eid}/k8s/helm/openstack/nova/rollback",
        headers=admin_headers,
        json={"revision": 1},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True
    resp = client.post(
        f"/api/v1/environments/{eid}/k8s/helm/openstack/nova/upgrade",
        headers=admin_headers,
        json={"chart": "nova"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True


def test_ops_apply_dry_run_parses_yaml():
    env = _env(dry_run=True)
    result = k8s_ops.apply_yaml(
        env,
        text="apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: demo\n  namespace: default\n",
    )
    assert result["ok"] is True
    assert result["dry_run"] is True
    assert result["applied"][0]["kind"] == "ConfigMap"
