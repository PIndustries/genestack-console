"""Live cluster/OpenState endpoint tests: subprocess mocks, degradation, scoping."""

from __future__ import annotations

import json
import uuid

from app.services import livestate


class _Proc:
    def __init__(self, stdout: str = "", returncode: int = 0, stderr: str = ""):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


def _suffix() -> str:
    return uuid.uuid4().hex[:8]


def _create_env(client, admin_headers, tenant_id=None):
    body = {"name": f"env-live-{_suffix()}"}
    if tenant_id:
        body["tenant_id"] = tenant_id
    resp = client.post("/api/v1/environments", headers=admin_headers, json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


_FAKE_KUBECONFIG = """apiVersion: v1
kind: Config
clusters:
- cluster:
    server: https://127.0.0.1:6443
  name: fake
contexts:
- context:
    cluster: fake
    user: fake
  name: fake
current-context: fake
users:
- name: fake
  user: {}
"""


def _set_env_kubeconfig(env_id: str) -> None:
    # The live cluster probe short-circuits when the env has no kubeconfig
    # (without KUBECONFIG kubectl would target 127.0.0.1:8080, i.e. the
    # console itself); these tests exercise the probe path, so set one.
    from pathlib import Path

    from app.db import SessionLocal
    from app.models import Environment

    path = Path("/tmp/fake-kubeconfig")
    path.write_text(_FAKE_KUBECONFIG, encoding="utf-8")
    with SessionLocal() as db:
        env = db.get(Environment, env_id)
        env.kubeconfig_path = str(path)
        db.commit()


def _patch_probes(monkeypatch, handler, *, kubectl=True, helm=True):
    def fake_which(name):
        found = {"kubectl": kubectl, "helm": helm}.get(name, True)
        return f"/usr/bin/{name}" if found else None

    monkeypatch.setattr(livestate.shutil, "which", fake_which)
    monkeypatch.setattr(livestate.subprocess, "run", handler)


# ------------------------------------------------------------ fixtures: probe payloads

_NODES_JSON = json.dumps(
    {
        "items": [
            {
                "metadata": {
                    "name": "node-1",
                    "labels": {"node-role.kubernetes.io/control-plane": ""},
                },
                "status": {
                    "conditions": [{"type": "Ready", "status": "True"}],
                    "nodeInfo": {"kubeletVersion": "v1.34.3"},
                    "capacity": {"cpu": "8", "memory": "32Gi"},
                },
            },
            {
                "metadata": {"name": "node-2", "labels": {}},
                "status": {
                    "conditions": [{"type": "Ready", "status": "False"}],
                    "nodeInfo": {"kubeletVersion": "v1.34.3"},
                    "capacity": {"cpu": "16", "memory": "64Gi"},
                },
            },
        ]
    }
)

_PODS_JSON = json.dumps(
    {
        "items": [
            {
                "metadata": {"name": "keystone-abc", "namespace": "openstack"},
                "status": {
                    "phase": "Running",
                    "containerStatuses": [{"ready": True, "state": {"running": {}}}],
                },
            },
            {
                "metadata": {"name": "helm-job-done", "namespace": "openstack"},
                "status": {
                    "phase": "Succeeded",
                    "containerStatuses": [
                        {
                            "ready": False,
                            "state": {"terminated": {"reason": "Completed"}},
                        }
                    ],
                },
            },
            {
                "metadata": {"name": "nova-bad", "namespace": "openstack"},
                "status": {
                    "phase": "Running",
                    "containerStatuses": [
                        {
                            "ready": False,
                            "state": {"waiting": {"reason": "CrashLoopBackOff"}},
                        }
                    ],
                },
            },
            {
                "metadata": {"name": "stuck-pod", "namespace": "default"},
                "status": {"phase": "Pending", "containerStatuses": []},
            },
        ]
    }
)

_HELM_JSON = json.dumps(
    [
        {
            "name": "keystone",
            "namespace": "openstack",
            "revision": "3",
            "status": "deployed",
            "chart": "keystone-0.4.1",
            "app_version": "2024.1",
        }
    ]
)

_POD_READY_JSON = json.dumps(
    {
        "metadata": {"name": "openstack-admin-client", "namespace": "openstack"},
        "status": {
            "phase": "Running",
            "conditions": [{"type": "Ready", "status": "True"}],
        },
    }
)

_USERS_JSON = json.dumps([{"ID": "u1", "Name": "admin"}, {"ID": "u2", "Name": "nova"}])
_SERVICES_JSON = json.dumps(
    [
        {
            "ID": 1,
            "Binary": "nova-compute",
            "Host": "node-1",
            "Zone": "nova",
            "Status": "enabled",
            "State": "up",
        }
    ]
)
_AGENTS_JSON = json.dumps(
    [
        {
            "ID": "a1",
            "Agent Type": "OVN Controller Gateway agent",
            "Host": "node-1",
            "Alive": ":-)",
            "State": "UP",
        }
    ]
)
_IMAGES_JSON = json.dumps([{"ID": "i1", "Name": "cirros", "Status": "active"}])

_EVENTS_JSON = json.dumps(
    {
        "items": [
            {
                "type": "Normal",
                "metadata": {"name": "keystone-abc.1", "namespace": "openstack"},
                "message": "Started container",
                "count": 1,
                "lastTimestamp": "2026-08-31T12:00:00Z",
                "involvedObject": {"kind": "Pod", "name": "keystone-abc"},
            },
            {
                "type": "Warning",
                "metadata": {"name": "nova-bad.17c4", "namespace": "openstack"},
                "message": "Back-off restarting failed container nova in pod nova-bad",
                "count": 15,
                "lastTimestamp": "2026-08-31T11:00:00Z",
                "involvedObject": {"kind": "Pod", "name": "nova-bad"},
            },
            {
                "type": "Warning",
                "metadata": {"name": "stuck-pod.abc", "namespace": "default"},
                "message": "FailedScheduling: 0/2 nodes available",
                "count": 3,
                "lastTimestamp": "2026-08-31T10:00:00Z",
                "involvedObject": {"kind": "Pod", "name": "stuck-pod"},
            },
        ]
    }
)


def _cluster_handler(argv, **kwargs):
    if "nodes" in argv:
        return _Proc(_NODES_JSON)
    if "pods" in argv:
        return _Proc(_PODS_JSON)
    if "events" in argv:
        return _Proc(_EVENTS_JSON)
    if argv[0].endswith("helm"):
        return _Proc(_HELM_JSON)
    return _Proc(stderr=f"unexpected argv: {argv}", returncode=1)


def _openstack_handler(argv, **kwargs):
    if "exec" not in argv:
        return _Proc(_POD_READY_JSON)
    if "user" in argv:
        return _Proc(_USERS_JSON)
    if "compute" in argv:
        return _Proc(_SERVICES_JSON)
    if "network" in argv:
        return _Proc(_AGENTS_JSON)
    if "image" in argv:
        return _Proc(_IMAGES_JSON)
    return _Proc(stderr=f"unexpected argv: {argv}", returncode=1)


# ------------------------------------------------------------ cluster endpoint


def test_cluster_happy_path(client, admin_headers, monkeypatch):
    captured = []

    def handler(argv, **kwargs):
        captured.append((argv, kwargs))
        return _cluster_handler(argv, **kwargs)

    _patch_probes(monkeypatch, handler)
    env = _create_env(client, admin_headers)
    _set_env_kubeconfig(env["id"])

    resp = client.get(
        f"/api/v1/environments/{env['id']}/cluster", headers=admin_headers
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["reachable"] is True
    assert body["error"] is None

    assert body["nodes"] == [
        {
            "name": "node-1",
            "roles": "control-plane",
            "status": "Ready",
            "unschedulable": False,
            "version": "v1.34.3",
            "cpu_capacity": "8",
            "mem_capacity": "32Gi",
            "mem_gi": 32.0,
        },
        {
            "name": "node-2",
            "roles": "<none>",
            "status": "NotReady",
            "unschedulable": False,
            "version": "v1.34.3",
            "cpu_capacity": "16",
            "mem_capacity": "64Gi",
            "mem_gi": 64.0,
        },
    ]
    assert body["resources"] == {"cpu": 24, "memory_gi": 96.0}
    assert body["health_reason"] == "2 problem pods"

    pods = body["pods"]
    assert pods["total"] == 4
    assert pods["running"] == 2
    assert pods["problems"] == [
        {
            "namespace": "openstack",
            "name": "nova-bad",
            "status": "Running",
            "reason": "CrashLoopBackOff",
        },
        {
            "namespace": "default",
            "name": "stuck-pod",
            "status": "Pending",
            "reason": "Pending",
        },
    ]

    assert body["releases"] == [
        {
            "name": "keystone",
            "namespace": "openstack",
            "status": "deployed",
            "chart": "keystone-0.4.1",
            "version": "2024.1",
        }
    ]
    assert body["health"] == "degraded"
    assert body["warnings"] == [
        {
            "namespace": "openstack",
            "name": "nova-bad.17c4",
            "message": "Back-off restarting failed container nova in pod nova-bad",
            "count": 15,
            "last_seen": "2026-08-31T11:00:00Z",
            "object": "Pod/nova-bad",
        },
        {
            "namespace": "default",
            "name": "stuck-pod.abc",
            "message": "FailedScheduling: 0/2 nodes available",
            "count": 3,
            "last_seen": "2026-08-31T10:00:00Z",
            "object": "Pod/stuck-pod",
        },
    ]
    assert body["access"]["kubeconfig"] is True
    # kubectl + helm were both invoked
    invoked = {argv[0] for argv, _ in captured}
    assert invoked == {"/usr/bin/kubectl", "/usr/bin/helm"}


def test_cluster_unreachable_degrades(client, admin_headers, monkeypatch):
    def handler(argv, **kwargs):
        return _Proc(stderr="The connection to the server was refused", returncode=1)

    _patch_probes(monkeypatch, handler)
    env = _create_env(client, admin_headers)
    _set_env_kubeconfig(env["id"])

    resp = client.get(
        f"/api/v1/environments/{env['id']}/cluster", headers=admin_headers
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["reachable"] is False
    assert "connection to the server" in body["error"]
    assert body["nodes"] == []
    assert body["pods"] == {"total": 0, "running": 0, "problems": []}
    assert body["releases"] == []


def test_cluster_kubectl_missing_degrades(client, admin_headers, monkeypatch):
    _patch_probes(monkeypatch, lambda argv, **kw: _Proc(), kubectl=False)
    env = _create_env(client, admin_headers)
    _set_env_kubeconfig(env["id"])

    resp = client.get(
        f"/api/v1/environments/{env['id']}/cluster", headers=admin_headers
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["reachable"] is False
    assert body["error"] == "kubectl not found on PATH"


def test_cluster_no_kubeconfig_skips_probes(client, admin_headers, monkeypatch):
    def boom(argv, **kwargs):  # noqa: ARG001
        raise AssertionError("kubectl/helm must not run without a kubeconfig")

    _patch_probes(monkeypatch, boom)
    env = _create_env(client, admin_headers)

    resp = client.get(
        f"/api/v1/environments/{env['id']}/cluster", headers=admin_headers
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["reachable"] is False
    assert "kubeconfig" in body["error"]
    assert body["nodes"] == []
    assert body["releases"] == []


# ------------------------------------------------------------ openstack endpoint


def test_openstack_happy_path(client, admin_headers, monkeypatch):
    timeouts = []

    def handler(argv, **kwargs):
        if "exec" in argv:
            timeouts.append(kwargs.get("timeout"))
        return _openstack_handler(argv, **kwargs)

    _patch_probes(monkeypatch, handler)
    env = _create_env(client, admin_headers)

    resp = client.get(
        f"/api/v1/environments/{env['id']}/openstack", headers=admin_headers
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["available"] is True
    assert body["source"] == "admin-client-pod"
    assert body["error"] is None

    assert body["users"] == [{"name": "admin"}, {"name": "nova"}]
    assert body["compute_services"] == [
        {
            "name": "nova-compute",
            "host": "node-1",
            "zone": "nova",
            "status": "enabled",
            "state": "up",
        }
    ]
    assert body["network_agents"] == [
        {
            "type": "OVN Controller Gateway agent",
            "host": "node-1",
            "alive": ":-)",
            "state": "UP",
        }
    ]
    assert body["images"] == [{"name": "cirros", "status": "active"}]
    # every openstack exec is capped at EXEC_TIMEOUT (15s)
    assert timeouts and all(t == livestate.EXEC_TIMEOUT for t in timeouts)


def test_openstack_pod_missing_apply_fails(
    client, admin_headers, monkeypatch, tmp_path
):
    def handler(argv, **kwargs):
        if "apply" in argv:
            return _Proc(stderr="error: unable to read manifest", returncode=1)
        if "get" in argv and "pod" in argv:
            return _Proc(
                stderr='Error from server (NotFound): pods "x" not found', returncode=1
            )
        return _Proc(stderr=f"unexpected argv: {argv}", returncode=1)

    _patch_probes(monkeypatch, handler)
    env = _create_env(client, admin_headers)
    # point the env at a config dir holding the admin client manifest
    manifest_dir = tmp_path / "manifests" / "utils"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "utils-openstack-client-admin.yaml").write_text(
        "apiVersion: v1\nkind: Pod\n", encoding="utf-8"
    )
    resp = client.patch(
        f"/api/v1/environments/{env['id']}",
        headers=admin_headers,
        json={"genestack_config_dir": str(tmp_path)},
    )
    assert resp.status_code in (200, 204), resp.text

    resp = client.get(
        f"/api/v1/environments/{env['id']}/openstack", headers=admin_headers
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["available"] is False
    assert "failed to create admin client pod" in body["error"]
    assert body["users"] == []
    assert body["compute_services"] == []
    assert body["network_agents"] == []
    assert body["images"] == []


def test_openstack_pod_missing_no_manifest(client, admin_headers, monkeypatch):
    def handler(argv, **kwargs):
        return _Proc(stderr="NotFound", returncode=1)

    _patch_probes(monkeypatch, handler)
    env = _create_env(client, admin_headers)  # no genestack_config_dir

    resp = client.get(
        f"/api/v1/environments/{env['id']}/openstack", headers=admin_headers
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["available"] is False
    assert (
        "no manifests/utils/utils-openstack-client-admin.yaml manifest available"
        in body["error"]
    )


def test_openstack_section_failure_isolation(client, admin_headers, monkeypatch):
    def handler(argv, **kwargs):
        if "exec" in argv and "user" in argv:
            return _Proc(stderr="HTTP 401 Unauthorized", returncode=1)
        return _openstack_handler(argv, **kwargs)

    _patch_probes(monkeypatch, handler)
    env = _create_env(client, admin_headers)

    resp = client.get(
        f"/api/v1/environments/{env['id']}/openstack", headers=admin_headers
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["available"] is True
    assert body["error"] is None
    assert body["users"] == []  # failing section degrades to an empty list
    assert body["compute_services"][0]["name"] == "nova-compute"
    assert body["network_agents"][0]["type"] == "OVN Controller Gateway agent"
    assert body["images"] == [{"name": "cirros", "status": "active"}]


# ------------------------------------------------------------ tenant scoping


def test_livestate_tenant_scoping(client, admin_headers, monkeypatch):
    _patch_probes(monkeypatch, _cluster_handler)
    suffix = _suffix()
    tenant_a = client.post(
        "/api/v1/tenants", headers=admin_headers, json={"name": f"live-ta-{suffix}"}
    ).json()
    tenant_b = client.post(
        "/api/v1/tenants", headers=admin_headers, json={"name": f"live-tb-{suffix}"}
    ).json()
    env_a = _create_env(client, admin_headers, tenant_id=tenant_a["id"])
    env_b = _create_env(client, admin_headers, tenant_id=tenant_b["id"])

    user = client.post(
        "/api/v1/users",
        headers=admin_headers,
        json={
            "username": f"live-viewer-{suffix}",
            "password": "pw",
            "memberships": [{"tenant_id": tenant_a["id"], "role": "viewer"}],
        },
    ).json()
    login = client.post(
        "/api/v1/auth/login", json={"username": user["username"], "password": "pw"}
    )
    assert login.status_code == 200, login.text
    headers = {"Authorization": f"Bearer {login.json()['token']}"}

    # Own tenant's env: readable.
    resp = client.get(f"/api/v1/environments/{env_a['id']}/cluster", headers=headers)
    assert resp.status_code == 200, resp.text

    # Other tenant's env: 403 on both live endpoints.
    for suffix_path in ("cluster", "openstack", "cluster/logs?pod=x&namespace=default"):
        assert (
            client.get(
                f"/api/v1/environments/{env_b['id']}/{suffix_path}", headers=headers
            ).status_code
            == 403
        )
    # Nonexistent env: 404.
    assert (
        client.get(
            "/api/v1/environments/does-not-exist/cluster", headers=headers
        ).status_code
        == 404
    )


# ------------------------------------------------------------ access downloads


def test_kubeconfig_download_200(client, admin_headers):
    env = _create_env(client, admin_headers)
    _set_env_kubeconfig(env["id"])
    resp = client.get(
        f"/api/v1/environments/{env['id']}/access/kubeconfig", headers=admin_headers
    )
    assert resp.status_code == 200, resp.text
    assert "application/yaml" in resp.headers.get("content-type", "")
    assert b"https://127.0.0.1:6443" in resp.content


def test_kubeconfig_download_404(client, admin_headers):
    env = _create_env(client, admin_headers)
    resp = client.get(
        f"/api/v1/environments/{env['id']}/access/kubeconfig", headers=admin_headers
    )
    assert resp.status_code == 404


def test_kubeconfig_download_viewer_403(client, admin_headers, viewer_headers):
    env = _create_env(client, admin_headers)
    _set_env_kubeconfig(env["id"])
    resp = client.get(
        f"/api/v1/environments/{env['id']}/access/kubeconfig", headers=viewer_headers
    )
    assert resp.status_code == 403
    resp = client.get(
        f"/api/v1/environments/{env['id']}/access/talosconfig", headers=viewer_headers
    )
    assert resp.status_code == 403


def test_talosconfig_download_200(client, admin_headers, tmp_path):
    env = _create_env(client, admin_headers)
    talos_dir = tmp_path / "talos"
    talos_dir.mkdir()
    (talos_dir / "talosconfig").write_text("context: fake\n", encoding="utf-8")
    patched = client.patch(
        f"/api/v1/environments/{env['id']}",
        headers=admin_headers,
        json={"genestack_config_dir": str(tmp_path)},
    )
    assert patched.status_code in (200, 204), patched.text
    resp = client.get(
        f"/api/v1/environments/{env['id']}/access/talosconfig", headers=admin_headers
    )
    assert resp.status_code == 200, resp.text
    assert "application/yaml" in resp.headers.get("content-type", "")
    assert b"context: fake" in resp.content


def test_cluster_optional_helm_failed_is_healthy(client, admin_headers, monkeypatch):
    pods_ok = json.dumps(
        {
            "items": [
                {
                    "metadata": {"name": "keystone-abc", "namespace": "openstack"},
                    "status": {
                        "phase": "Running",
                        "containerStatuses": [
                            {"ready": True, "state": {"running": {}}}
                        ],
                    },
                }
            ]
        }
    )
    helm_optional = json.dumps(
        [
            {
                "name": "keystone",
                "namespace": "openstack",
                "status": "deployed",
                "chart": "keystone-0.4.1",
                "app_version": "2024.1",
            },
            {
                "name": "kube-prometheus-stack",
                "namespace": "monitoring",
                "status": "failed",
                "chart": "kube-prometheus-stack-65.1.0",
                "app_version": "v0.75.0",
            },
        ]
    )
    events_empty = json.dumps({"items": []})

    def handler(argv, **kwargs):
        if "nodes" in argv:
            return _Proc(_NODES_JSON)
        if "pods" in argv:
            return _Proc(pods_ok)
        if "events" in argv:
            return _Proc(events_empty)
        if argv[0].endswith("helm"):
            return _Proc(helm_optional)
        return _Proc(stderr=f"unexpected argv: {argv}", returncode=1)

    _patch_probes(monkeypatch, handler)
    env = _create_env(client, admin_headers)
    _set_env_kubeconfig(env["id"])
    resp = client.get(
        f"/api/v1/environments/{env['id']}/cluster", headers=admin_headers
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["reachable"] is True
    assert body["health"] == "healthy"
    assert body["health_reason"] == ""
    objects = [w.get("object") for w in body["warnings"]]
    assert "HelmRelease/kube-prometheus-stack" in objects


def test_cluster_required_helm_failed_is_degraded(client, admin_headers, monkeypatch):
    pods_ok = json.dumps(
        {
            "items": [
                {
                    "metadata": {"name": "keystone-abc", "namespace": "openstack"},
                    "status": {
                        "phase": "Running",
                        "containerStatuses": [
                            {"ready": True, "state": {"running": {}}}
                        ],
                    },
                }
            ]
        }
    )

    def handler(argv, **kwargs):
        if "nodes" in argv:
            return _Proc(_NODES_JSON)
        if "pods" in argv:
            return _Proc(pods_ok)
        if "events" in argv:
            return _Proc(json.dumps({"items": []}))
        if argv[0].endswith("helm"):
            return _Proc(
                json.dumps(
                    [
                        {
                            "name": "keystone",
                            "namespace": "openstack",
                            "status": "failed",
                            "chart": "keystone-0.4.1",
                            "app_version": "2024.1",
                        }
                    ]
                )
            )
        return _Proc(stderr=f"unexpected argv: {argv}", returncode=1)

    _patch_probes(monkeypatch, handler)
    env = _create_env(client, admin_headers)
    _set_env_kubeconfig(env["id"])
    resp = client.get(
        f"/api/v1/environments/{env['id']}/cluster", headers=admin_headers
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["health"] == "degraded"
    assert "helm not deployed: keystone" in body["health_reason"]


def test_cluster_health_optional_only():
    health, reason = livestate._cluster_health(
        {
            "reachable": True,
            "pods": {"problems": []},
            "releases": [
                {
                    "name": "kube-prometheus-stack",
                    "status": "failed",
                    "chart": "kube-prometheus-stack-1.0.0",
                }
            ],
        },
        has_kubeconfig=True,
    )
    assert health == "healthy"
    assert reason == ""


def test_cluster_logs_happy_path(client, admin_headers, monkeypatch):
    captured = []

    def handler(argv, **kwargs):
        captured.append(argv)
        if "logs" in argv:
            return _Proc("line1\nline2\n")
        return _Proc(stderr=f"unexpected argv: {argv}", returncode=1)

    _patch_probes(monkeypatch, handler)
    env = _create_env(client, admin_headers)
    _set_env_kubeconfig(env["id"])
    resp = client.get(
        f"/api/v1/environments/{env['id']}/cluster/logs",
        headers=admin_headers,
        params={"pod": "nova-bad", "namespace": "openstack", "tail": 50},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["error"] is None
    assert "line1" in body["text"]
    assert captured
    argv = captured[0]
    assert "logs" in argv
    assert "-n" in argv and "openstack" in argv
    assert "nova-bad" in argv
    assert "--tail=50" in argv


def test_cluster_logs_container_and_previous(client, admin_headers, monkeypatch):
    captured = []

    def handler(argv, **kwargs):
        captured.append(argv)
        return _Proc("ok\n")

    _patch_probes(monkeypatch, handler)
    env = _create_env(client, admin_headers)
    _set_env_kubeconfig(env["id"])
    resp = client.get(
        f"/api/v1/environments/{env['id']}/cluster/logs",
        headers=admin_headers,
        params={
            "pod": "nova-bad",
            "namespace": "openstack",
            "container": "nova",
            "previous": True,
        },
    )
    assert resp.status_code == 200, resp.text
    argv = captured[0]
    assert "-c" in argv and "nova" in argv
    assert "--previous" in argv


def test_cluster_logs_viewer_allowed(
    client, admin_headers, viewer_headers, monkeypatch
):
    def handler(argv, **kwargs):
        return _Proc("ok\n")

    _patch_probes(monkeypatch, handler)
    env = _create_env(client, admin_headers)
    _set_env_kubeconfig(env["id"])
    resp = client.get(
        f"/api/v1/environments/{env['id']}/cluster/logs",
        headers=viewer_headers,
        params={"pod": "nova-bad", "namespace": "openstack"},
    )
    assert resp.status_code == 200, resp.text


def test_cluster_logs_no_kubeconfig(client, admin_headers, monkeypatch):
    def boom(argv, **kwargs):  # noqa: ARG001
        raise AssertionError("kubectl must not run without a kubeconfig")

    _patch_probes(monkeypatch, boom)
    env = _create_env(client, admin_headers)
    resp = client.get(
        f"/api/v1/environments/{env['id']}/cluster/logs",
        headers=admin_headers,
        params={"pod": "nova-bad", "namespace": "openstack"},
    )
    assert resp.status_code == 200, resp.text
    assert "kubeconfig" in resp.json()["error"]


def test_cluster_logs_rejects_bad_name(client, admin_headers, monkeypatch):
    def boom(argv, **kwargs):  # noqa: ARG001
        raise AssertionError("kubectl must not run for invalid names")

    _patch_probes(monkeypatch, boom)
    env = _create_env(client, admin_headers)
    _set_env_kubeconfig(env["id"])
    resp = client.get(
        f"/api/v1/environments/{env['id']}/cluster/logs",
        headers=admin_headers,
        params={"pod": "../etc/passwd", "namespace": "openstack"},
    )
    assert resp.status_code == 200, resp.text
    assert "invalid" in resp.json()["error"]


def test_cluster_logs_kubectl_error(client, admin_headers, monkeypatch):
    def handler(argv, **kwargs):
        return _Proc(
            stderr='Error from server (NotFound): pods "x" not found', returncode=1
        )

    _patch_probes(monkeypatch, handler)
    env = _create_env(client, admin_headers)
    _set_env_kubeconfig(env["id"])
    resp = client.get(
        f"/api/v1/environments/{env['id']}/cluster/logs",
        headers=admin_headers,
        params={"pod": "missing-pod", "namespace": "openstack"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["text"] == ""
    assert "NotFound" in body["error"] or "not found" in body["error"].lower()


def test_talosconfig_download_404(client, admin_headers):
    env = _create_env(client, admin_headers)
    resp = client.get(
        f"/api/v1/environments/{env['id']}/access/talosconfig", headers=admin_headers
    )
    assert resp.status_code == 404
