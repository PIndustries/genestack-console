"""Platform fabric join: Talos + Kubernetes + Nova."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.services import platform


def test_match_k8s_by_private_ip():
    k8s = [{"name": "talos-abc", "internal_ip": "10.10.0.11", "external_ip": "1.2.3.4"}]
    assert platform._match_k8s(k8s, "10.10.0.11", "9.9.9.9")["name"] == "talos-abc"
    assert platform._match_k8s(k8s, "", "1.2.3.4")["name"] == "talos-abc"
    assert platform._match_k8s(k8s, "10.9.9.9", "9.9.9.9") is None


def test_match_nova_by_k8s_name():
    computes = [
        {
            "host": "talos-abc",
            "state": "up",
            "status": "enabled",
            "binary": "nova-compute",
        }
    ]
    assert platform._match_nova(computes, "talos-abc")["state"] == "up"
    assert platform._match_nova(computes, "other") is None


def test_cluster_summary_counts_roles_and_versions():
    env = SimpleNamespace(name="lab-1")
    nodes = [
        {
            "name": "cp1",
            "roles": ["k8s_control_plane", "etcd"],
            "talos": {"reachable": True, "version": "v1.13.9"},
            "kubernetes": {
                "status": "Ready",
                "version": "v1.34.1",
                "cpu_capacity": "32",
                "mem_gi": 128,
                "unschedulable": False,
            },
        },
        {
            "name": "w1",
            "roles": ["compute"],
            "talos": {"reachable": True, "version": "v1.13.8"},
            "kubernetes": {"status": "NotReady", "version": "v1.34.1"},
        },
        {
            "name": "cp2",
            "roles": ["K8S_CONTROL_PLANE"],
            "talos": {"reachable": False, "version": None},
            "kubernetes": None,
        },
    ]
    image = "factory.talos.dev/installer/abc12345:v1.13.9"
    summary = platform._cluster_summary(env, nodes, image)
    assert summary["name"] == "lab-1"
    assert summary["machines"] == 3
    assert summary["control_planes"] == 2
    assert summary["workers"] == 1
    assert summary["ready"] == 1
    assert summary["not_ready"] == 1
    assert summary["talos_reachable"] == 2
    assert summary["talos_versions"] == ["v1.13.8", "v1.13.9"]
    assert summary["kubernetes_versions"] == ["v1.34.1"]
    assert summary["install_image"] == image

    empty = platform._cluster_summary(env, [], image)
    assert empty["machines"] == 0
    assert empty["control_planes"] == 0
    assert empty["workers"] == 0
    assert empty["ready"] == 0
    assert empty["not_ready"] == 0
    assert empty["talos_reachable"] == 0
    assert empty["talos_versions"] == []
    assert empty["kubernetes_versions"] == []
    assert empty["install_image"] == image
    assert empty["name"] == "lab-1"


def test_entry_os_ubuntu_is_not_dialed_as_talos():
    assert platform._entry_os({"adopt": "kubespray"}, set(), "cp-1") == "ubuntu"
    assert platform._entry_os({}, {"cp-1"}, "cp-1") == "ubuntu"
    assert platform._entry_os({"adopt": ""}, set(), "cp-1") == "talos"
    assert platform._entry_os({}, {"other"}, "cp-1") == "talos"


def test_platform_endpoint_200(client, admin_headers, monkeypatch):
    env = client.post(
        "/api/v1/environments", headers=admin_headers, json={"name": "pf-1"}
    ).json()
    monkeypatch.setattr(
        platform,
        "platform_overview",
        lambda *a, **k: {
            "nodes": [
                {
                    "name": "ns1",
                    "public_ip": "1.2.3.4",
                    "private_ip": "10.10.0.11",
                    "roles": ["k8s_control_plane"],
                    "talos": {"reachable": True, "version": "v1.13.9", "error": None},
                    "kubernetes": {"name": "talos-abc", "status": "Ready"},
                    "openstack": {
                        "host": "talos-abc",
                        "state": "up",
                        "status": "enabled",
                    },
                }
            ],
            "error": None,
        },
    )
    resp = client.get(
        f"/api/v1/environments/{env['id']}/platform", headers=admin_headers
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["nodes"][0]["kubernetes"]["name"] == "talos-abc"
    assert body["nodes"][0]["openstack"]["state"] == "up"


def test_talos_reboot_invalid_name(client, admin_headers):
    env = client.post(
        "/api/v1/environments", headers=admin_headers, json={"name": "pf-2"}
    ).json()
    resp = client.post(
        f"/api/v1/environments/{env['id']}/platform/nodes/../etc/passwd/reboot",
        headers=admin_headers,
    )
    assert resp.status_code in (400, 404, 422)


def test_talos_dmesg_mocked(client, admin_headers):
    env = client.post(
        "/api/v1/environments", headers=admin_headers, json={"name": "pf-3"}
    ).json()
    with patch.object(
        platform,
        "talos_dmesg",
        return_value={"ok": True, "text": "boot", "error": None},
    ):
        resp = client.get(
            f"/api/v1/environments/{env['id']}/platform/nodes/server-1.example.com/dmesg",
            headers=admin_headers,
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["text"] == "boot"


def test_talos_services_mocked(client, admin_headers):
    env = client.post(
        "/api/v1/environments", headers=admin_headers, json={"name": "pf-svc"}
    ).json()
    payload = {
        "ok": True,
        "error": None,
        "text": "apid Running OK",
        "services": [
            {
                "id": "apid",
                "state": "Running",
                "health": "OK",
                "last_event": "Health check successful",
            }
        ],
        "node": "ns1",
        "ip": "10.10.0.11",
    }
    with patch.object(platform, "talos_services", return_value=payload):
        resp = client.get(
            f"/api/v1/environments/{env['id']}/platform/nodes/ns1/services",
            headers=admin_headers,
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["services"][0]["id"] == "apid"
    assert body["node"] == "ns1"


def test_parse_services_table():
    text = """NODE         ID        STATE     HEALTH   LAST EVENT
10.10.0.11   apid      Running   OK       Health check successful
10.10.0.11   kubelet   Running   OK       Health check successful
"""
    rows = platform._parse_services(text)
    assert [r["id"] for r in rows] == ["apid", "kubelet"]
    assert rows[0]["state"] == "Running"
    assert rows[0]["health"] == "OK"
    assert rows[0]["last_event"] == "Health check successful"

    no_node = """SERVICE      STATE     HEALTH   LAST EVENT
etcd         Running   OK       Health check successful
machined     Running   ?        Service started
"""
    rows = platform._parse_services(no_node)
    assert rows[0] == {
        "id": "etcd",
        "state": "Running",
        "health": "OK",
        "last_event": "Health check successful",
    }
    assert rows[1]["id"] == "machined"
    assert rows[1]["health"] == "?"
    assert platform._parse_services("") == []
    assert platform._parse_services("NODE ID STATE HEALTH LAST EVENT") == []


def test_talos_upgrade_invalid_name(client, admin_headers):
    env = client.post(
        "/api/v1/environments", headers=admin_headers, json={"name": "pf-4"}
    ).json()
    resp = client.post(
        f"/api/v1/environments/{env['id']}/platform/nodes/../etc/passwd/upgrade",
        headers=admin_headers,
    )
    assert resp.status_code in (400, 404, 422)


def test_talos_upgrade_invalid_image(client, admin_headers):
    env = client.post(
        "/api/v1/environments", headers=admin_headers, json={"name": "pf-5"}
    ).json()
    url = f"/api/v1/environments/{env['id']}/platform/nodes/ns1/upgrade"
    with patch.object(
        platform, "talos_upgrade", return_value={"ok": True, "error": None}
    ):
        for bad in (
            "not a valid image",
            "foo/bar/../../evil",
            "short",
            "img with space",
        ):
            resp = client.post(url, headers=admin_headers, json={"image": bad})
            assert resp.status_code == 400, (bad, resp.text)


def test_talos_upgrade_enqueues_job(client, admin_headers):
    env = client.post(
        "/api/v1/environments", headers=admin_headers, json={"name": "pf-6"}
    ).json()
    image = "factory.talos.dev/installer/abc12345:v1.13.9"
    resp = client.post(
        f"/api/v1/environments/{env['id']}/platform/nodes/ns1/upgrade",
        headers=admin_headers,
        json={"image": image},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["operation"] == "platform.talos.upgrade"
    assert body["job_id"]
    assert body["status"] == "queued"


def test_talos_upgrade_enqueues_no_body(client, admin_headers):
    env = client.post(
        "/api/v1/environments", headers=admin_headers, json={"name": "pf-7"}
    ).json()
    resp = client.post(
        f"/api/v1/environments/{env['id']}/platform/nodes/ns1/upgrade",
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["operation"] == "platform.talos.upgrade"
    assert body["job_id"]


def test_valid_install_image():
    assert platform.valid_install_image("factory.talos.dev/installer/abc12345:v1.13.9")
    assert not platform.valid_install_image("not a valid image")
    assert not platform.valid_install_image("foo/bar/../../evil")
    assert not platform.valid_install_image("short")
    assert not platform.valid_install_image("")
    assert not platform.valid_install_image("-evil")
    assert not platform.valid_install_image("--image=evil")
    assert not platform.valid_install_image("https://evil.example/x")
    assert not platform.valid_install_image("file:///etc/passwd")


def test_node_talos_ip_prefers_private():
    entry = {
        "private_ip": "10.10.0.12",
        "public_ip": "192.0.2.130",
        "ip": "10.10.0.12",
    }
    assert platform._node_talos_ip(entry, "ns1") == "10.10.0.12"
    public_only = {"public_ip": "203.0.113.10"}
    assert platform._node_talos_ip(public_only, "ns1") == "203.0.113.10"


def test_valid_node_endpoint():
    assert platform.valid_node_endpoint("203.0.113.10")
    assert platform.valid_node_endpoint("server-1.example.com")
    assert platform.valid_node_endpoint("10.10.0.11")
    assert not platform.valid_node_endpoint("-e")
    assert not platform.valid_node_endpoint("--nodes")
    assert not platform.valid_node_endpoint("1.2.3.4;id")
    assert not platform.valid_node_endpoint("http://evil")
    assert not platform.valid_node_endpoint("")


def test_talos_upgrade_runs_talosctl(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(
        platform, "_node_public_ip", lambda *a, **k: ("/tmp/talosconfig", "1.2.3.4")
    )
    monkeypatch.setattr(platform.shutil, "which", lambda _name: "/usr/bin/talosctl")

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["timeout"] = kwargs.get("timeout")

        class Proc:
            returncode = 0
            stdout = "ok"
            stderr = ""

        return Proc()

    monkeypatch.setattr(platform.subprocess, "run", fake_run)
    image = "factory.talos.dev/installer/abc12345:v1.13.9"
    result = platform.talos_upgrade(
        object(), "ns1", settings=object(), db=None, image=image
    )
    assert result["ok"] is True
    assert result["image"] == image
    assert result["ip"] == "1.2.3.4"
    argv = captured["argv"]
    assert argv[1] == "upgrade"
    assert "--nodes" in argv and "1.2.3.4" in argv
    assert "--image" in argv and image in argv
    assert "--preserve" in argv
    assert "--wait=false" in argv
    assert captured["timeout"] == platform.UPGRADE_TIMEOUT


def test_talos_upgrade_uses_env_install_image(monkeypatch):
    monkeypatch.setattr(
        platform.envconfig,
        "get_current",
        lambda db, env: (
            {"talos": {"install_image": "factory.talos.dev/installer/fromdoc:v1.13.9"}},
            None,
        ),
    )
    monkeypatch.setattr(
        platform, "_node_public_ip", lambda *a, **k: ("/tmp/talosconfig", "1.2.3.4")
    )
    monkeypatch.setattr(platform.shutil, "which", lambda _name: "/usr/bin/talosctl")
    captured: dict = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv

        class Proc:
            returncode = 0
            stdout = ""
            stderr = ""

        return Proc()

    monkeypatch.setattr(platform.subprocess, "run", fake_run)
    result = platform.talos_upgrade(
        object(), "ns1", settings=object(), db=object(), image=None
    )
    assert result["ok"] is True
    assert result["image"] == "factory.talos.dev/installer/fromdoc:v1.13.9"
    assert "factory.talos.dev/installer/fromdoc:v1.13.9" in captured["argv"]


class _Proc:
    def __init__(self, returncode=0, stdout="ok", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _patch_talos(
    monkeypatch, *, which="/usr/bin/talosctl", handler=None, stdout="ok", returncode=0
):
    captured: dict = {"calls": []}
    monkeypatch.setattr(
        platform, "_node_public_ip", lambda *a, **k: ("/tmp/talosconfig", "10.10.0.11")
    )
    monkeypatch.setattr(platform.shutil, "which", lambda _n: which)

    def fake_run(argv, **kwargs):
        captured["calls"].append(list(argv))
        captured["argv"] = list(argv)
        captured["timeout"] = kwargs.get("timeout")
        if handler is not None:
            return handler(argv, captured)
        return _Proc(returncode=returncode, stdout=stdout)

    monkeypatch.setattr(platform.subprocess, "run", fake_run)
    return captured


def _env(client, admin_headers, name: str) -> dict:
    return client.post(
        "/api/v1/environments", headers=admin_headers, json={"name": name}
    ).json()


def _node_url(env_id: str, suffix: str, node: str = "ns1") -> str:
    return f"/api/v1/environments/{env_id}/platform/nodes/{node}/{suffix}"


def test_talos_health_missing_talosctl(monkeypatch):
    _patch_talos(monkeypatch, which=None)
    result = platform.talos_health(object(), "ns1", settings=object(), db=None)
    assert result["ok"] is False
    assert "talosctl" in (result.get("error") or "").lower()
    assert result["node"] == "ns1"


def test_talos_health_missing_talosctl_http(client, admin_headers, monkeypatch):
    env = _env(client, admin_headers, "pf-health-miss")
    _patch_talos(monkeypatch, which=None)
    resp = client.get(_node_url(env["id"], "health"), headers=admin_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is False
    assert "talosctl" in (body.get("error") or "").lower()


def test_talos_health_happy(monkeypatch):
    captured = _patch_talos(monkeypatch, stdout="waiting for etcd to be healthy: OK")
    result = platform.talos_health(object(), "ns1", settings=object(), db=None)
    assert result["ok"] is True
    assert "OK" in result["text"]
    assert result["ip"] == "10.10.0.11"
    argv = captured["argv"]
    assert argv[1] == "health"
    assert "--wait=false" in argv
    assert "--nodes" in argv and "10.10.0.11" in argv
    assert "--talosconfig" in argv


def test_talos_health_http_happy(client, admin_headers, monkeypatch):
    env = _env(client, admin_headers, "pf-health-ok")
    _patch_talos(monkeypatch, stdout="healthy")
    resp = client.get(_node_url(env["id"], "health"), headers=admin_headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True
    assert resp.json()["text"] == "healthy"


def test_talos_node_invalid_name_400(client, admin_headers):
    env = _env(client, admin_headers, "pf-bad-name")
    for suffix in (
        "health",
        "etcd",
        "machineconfig",
        "disks",
        "logs",
        "resources",
        "events",
        "containers",
    ):
        resp = client.get(
            _node_url(env["id"], suffix, node="--nodes"), headers=admin_headers
        )
        assert resp.status_code == 400, (suffix, resp.text)
    resp = client.post(
        _node_url(env["id"], "shutdown", node="ns1;id"), headers=admin_headers
    )
    assert resp.status_code == 400
    resp = client.post(
        _node_url(env["id"], "reset", node="bad name"), headers=admin_headers
    )
    assert resp.status_code == 400


def test_talos_mutates_viewer_403(client, admin_headers, viewer_headers, monkeypatch):
    env = _env(client, admin_headers, "pf-viewer-403")
    _patch_talos(monkeypatch)
    base = f"/api/v1/environments/{env['id']}/platform/nodes/ns1"
    for method, path, body in (
        ("POST", f"{base}/shutdown", None),
        ("POST", f"{base}/reset", {"graceful": True, "reboot": True, "wipe": True}),
        ("POST", f"{base}/apply-config", {"yaml": "machine: {}", "mode": "auto"}),
        ("POST", f"{base}/service/kubelet/restart", None),
        ("POST", f"{base}/reboot", None),
        (
            "POST",
            f"{base}/upgrade",
            {"image": "factory.talos.dev/installer/abc12345:v1.13.9"},
        ),
    ):
        resp = client.request(method, path, headers=viewer_headers, json=body)
        assert resp.status_code == 403, (path, resp.status_code, resp.text)


def test_talos_reads_viewer_ok(client, admin_headers, viewer_headers, monkeypatch):
    env = _env(client, admin_headers, "pf-viewer-read")
    _patch_talos(monkeypatch, stdout="ok")
    resp = client.get(_node_url(env["id"], "health"), headers=viewer_headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True


def test_talos_etcd_members_and_status(monkeypatch):
    def handler(argv, captured):
        if "members" in argv:
            return _Proc(stdout="MEMBER ID HASH")
        if "status" in argv:
            return _Proc(stdout="STATUS OK")
        return _Proc(returncode=1, stdout="", stderr="unexpected")

    _patch_talos(monkeypatch, handler=handler)
    result = platform.talos_etcd(object(), "ns1", settings=object(), db=None)
    assert result["ok"] is True
    assert "MEMBER" in result["members"]
    assert "STATUS" in result["status"]
    assert "MEMBER" in result["text"]


def test_talos_etcd_status_optional(monkeypatch):
    def handler(argv, captured):
        if "members" in argv:
            return _Proc(stdout="m1")
        return _Proc(returncode=1, stdout="", stderr="etcd status not supported")

    _patch_talos(monkeypatch, handler=handler)
    result = platform.talos_etcd(object(), "ns1", settings=object(), db=None)
    assert result["ok"] is True
    assert result["members"] == "m1"
    assert result["status"] == ""
    assert result.get("status_error")


def test_talos_machineconfig(monkeypatch):
    captured = _patch_talos(monkeypatch, stdout="version: v1alpha1\nmachine: {}\n")
    result = platform.talos_machineconfig(object(), "ns1", settings=object(), db=None)
    assert result["ok"] is True
    assert "machine:" in result["yaml"]
    assert "get" in captured["argv"] and "machineconfig" in captured["argv"]
    assert "-o" in captured["argv"] and "yaml" in captured["argv"]


def test_talos_disks_and_volumes(monkeypatch):
    def handler(argv, captured):
        joined = " ".join(argv)
        if "discoveredvolumes" in joined:
            return _Proc(stdout="volume: /dev/sda")
        if "disks" in joined:
            return _Proc(stdout="disk: /dev/sda")
        return _Proc(returncode=1, stdout="", stderr="no")

    _patch_talos(monkeypatch, handler=handler)
    result = platform.talos_disks(object(), "ns1", settings=object(), db=None)
    assert result["ok"] is True
    assert "disk:" in result["disks"]
    assert "volume:" in result["volumes"]


def test_talos_logs_service(monkeypatch):
    captured = _patch_talos(monkeypatch, stdout="kubelet started")
    result = platform.talos_logs(
        object(), "ns1", settings=object(), db=None, service="kubelet"
    )
    assert result["ok"] is True
    assert result["service"] == "kubelet"
    assert "kubelet started" in result["text"]
    assert captured["argv"][1] == "logs"
    assert "kubelet" in captured["argv"]


def test_talos_logs_invalid_service():
    result = platform.talos_logs(object(), "ns1", service="kubelet;rm")
    assert result["ok"] is False
    assert "invalid" in (result.get("error") or "")


def test_talos_logs_http_invalid_service(client, admin_headers):
    env = _env(client, admin_headers, "pf-logs-bad")
    resp = client.get(
        _node_url(env["id"], "logs"),
        headers=admin_headers,
        params={"service": "bad;svc"},
    )
    assert resp.status_code == 400


def test_talos_resources_combined(monkeypatch):
    captured = _patch_talos(monkeypatch, stdout="meminfo: 1")
    result = platform.talos_resources(object(), "ns1", settings=object(), db=None)
    assert result["ok"] is True
    assert "meminfo" in captured["argv"][2]
    assert result["results"]["resources"]["ok"] is True


def test_talos_resources_degrades(monkeypatch):
    def handler(argv, captured):
        joined = " ".join(argv)
        if "meminfo,cpustat" in joined:
            return _Proc(returncode=1, stdout="", stderr="unknown resource")
        if "get" in argv and "meminfo" in argv:
            return _Proc(returncode=1, stdout="", stderr="no meminfo")
        if "get" in argv and "cpustat" in argv:
            return _Proc(stdout="cpu: 4")
        if "get" in argv and "runtimes" in argv:
            return _Proc(returncode=1, stdout="", stderr="no")
        if "get" in argv and "networkstatus" in argv:
            return _Proc(returncode=1, stdout="", stderr="no")
        if argv[1] == "memory":
            return _Proc(stdout="Mem: 128Gi")
        if argv[1] == "netstat":
            return _Proc(stdout="tcp 80")
        if argv[1] == "interfaces":
            return _Proc(stdout="eth0 up")
        return _Proc(returncode=1, stdout="", stderr="no")

    captured = _patch_talos(monkeypatch, handler=handler)
    result = platform.talos_resources(object(), "ns1", settings=object(), db=None)
    assert result["ok"] is True
    assert result["results"]["cpustat"]["ok"] is True
    assert result["results"]["memory"]["ok"] is True
    assert result["results"]["netstat"]["ok"] is True
    verbs = [c[1] for c in captured["calls"]]
    assert "memory" in verbs
    assert "netstat" in verbs


def test_talos_events_since(monkeypatch):
    captured = _patch_talos(monkeypatch, stdout="event 1")
    result = platform.talos_events(
        object(), "ns1", settings=object(), db=None, since="1h"
    )
    assert result["ok"] is True
    assert "--since" in captured["argv"]
    assert "1h" in captured["argv"]
    assert "--tail" in captured["argv"]


def test_talos_events_fallback_dmesg(monkeypatch):
    def handler(argv, captured):
        if argv[1] == "events":
            return _Proc(returncode=1, stdout="", stderr="unknown command")
        if argv[1] == "dmesg":
            return _Proc(stdout="[    0.000000] Linux")
        return _Proc(returncode=1, stdout="", stderr="no")

    _patch_talos(monkeypatch, handler=handler)
    result = platform.talos_events(object(), "ns1", settings=object(), db=None)
    assert result["ok"] is True
    assert result.get("fallback") == "dmesg"
    assert "Linux" in result["text"]


def test_talos_events_invalid_since_http(client, admin_headers):
    env = _env(client, admin_headers, "pf-events-since")
    resp = client.get(
        _node_url(env["id"], "events"),
        headers=admin_headers,
        params={"since": ";rm"},
    )
    assert resp.status_code == 400


def test_talos_containers_fallback(monkeypatch):
    def handler(argv, captured):
        if argv[1] == "containers":
            return _Proc(returncode=1, stdout="", stderr="unknown")
        if argv[1] == "get" and "containers" in argv:
            return _Proc(stdout="kubelet")
        return _Proc(returncode=1, stdout="", stderr="no")

    captured = _patch_talos(monkeypatch, handler=handler)
    result = platform.talos_containers(object(), "ns1", settings=object(), db=None)
    assert result["ok"] is True
    assert "kubelet" in result["text"]
    assert any(c[1] == "get" for c in captured["calls"])


def test_talos_shutdown_happy(monkeypatch):
    captured = _patch_talos(monkeypatch, stdout="")
    result = platform.talos_shutdown(object(), "ns1", settings=object(), db=None)
    assert result["ok"] is True
    assert "shutdown" in result["message"]
    assert captured["argv"][1] == "shutdown"
    assert "--wait=false" in captured["argv"]


def test_talos_shutdown_http(client, admin_headers, operator_headers, monkeypatch):
    env = _env(client, admin_headers, "pf-shutdown")
    _patch_talos(monkeypatch, stdout="")
    resp = client.post(_node_url(env["id"], "shutdown"), headers=operator_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["operation"] == "platform.talos.shutdown"
    assert body["job_id"]
    assert body["status"] == "queued"


def test_talos_reset_flags(monkeypatch):
    captured = _patch_talos(monkeypatch, stdout="")
    result = platform.talos_reset(
        object(),
        "ns1",
        settings=object(),
        db=None,
        graceful=False,
        reboot=True,
        wipe=False,
    )
    assert result["ok"] is True
    argv = captured["argv"]
    assert argv[1] == "reset"
    assert "--wait=false" in argv
    assert "--graceful=false" in argv
    assert "--reboot" in argv
    assert "--wipe-mode=none" in argv
    assert result["graceful"] is False
    assert result["reboot"] is True
    assert result["wipe"] is False


def test_talos_reset_defaults(monkeypatch):
    captured = _patch_talos(monkeypatch, stdout="")
    result = platform.talos_reset(object(), "ns1", settings=object(), db=None)
    assert result["ok"] is True
    argv = captured["argv"]
    assert "--graceful=true" in argv
    assert "--wipe-mode=system-disk" in argv
    assert "--reboot" not in argv


def test_talos_reset_http(client, admin_headers, monkeypatch):
    env = _env(client, admin_headers, "pf-reset")
    _patch_talos(monkeypatch, stdout="")
    resp = client.post(
        _node_url(env["id"], "reset"),
        headers=admin_headers,
        json={"graceful": True, "reboot": True, "wipe": True},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["operation"] == "platform.talos.reset"
    assert body["job_id"]
    assert body["status"] == "queued"


def test_talos_reset_run_sync_dry_run(client, admin_headers, monkeypatch):
    """run_sync + env dry_run rehearses without calling talosctl mutate."""
    env = _env(client, admin_headers, "pf-reset-sync")
    captured = _patch_talos(monkeypatch, stdout="")
    resp = client.post(
        _node_url(env["id"], "reset"),
        headers=admin_headers,
        json={
            "graceful": True,
            "reboot": True,
            "wipe": True,
            "run_sync": True,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["status"] == "success"
    assert body["dry_run"] is True
    # dry-run path must not invoke talosctl
    assert "argv" not in captured
    assert captured["calls"] == []


def test_talos_apply_config(monkeypatch):
    captured = _patch_talos(monkeypatch, stdout="applied")
    yaml_text = "version: v1alpha1\nmachine: {}\n"
    result = platform.talos_apply_config(
        object(),
        "ns1",
        settings=object(),
        db=None,
        yaml_text=yaml_text,
        mode="no-reboot",
    )
    assert result["ok"] is True
    assert result["mode"] == "no-reboot"
    argv = captured["argv"]
    assert argv[1] == "apply-config"
    assert "--mode" in argv and "no-reboot" in argv
    assert "--file" in argv
    file_arg = argv[argv.index("--file") + 1]
    assert file_arg.endswith(".yaml")
    assert not Path(file_arg).exists()


def test_talos_apply_config_http(client, admin_headers, monkeypatch):
    env = _env(client, admin_headers, "pf-apply")
    _patch_talos(monkeypatch, stdout="")
    resp = client.post(
        _node_url(env["id"], "apply-config"),
        headers=admin_headers,
        json={"yaml": "machine: {}\n", "mode": "staged"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["operation"] == "platform.talos.apply_config"
    assert body["job_id"]


def test_talos_apply_config_invalid_mode_http(client, admin_headers):
    env = _env(client, admin_headers, "pf-apply-mode")
    resp = client.post(
        _node_url(env["id"], "apply-config"),
        headers=admin_headers,
        json={"yaml": "machine: {}\n", "mode": "explode"},
    )
    assert resp.status_code == 422


def test_talos_service_action(monkeypatch):
    captured = _patch_talos(monkeypatch, stdout="")
    result = platform.talos_service_action(
        object(), "ns1", "kubelet", "restart", settings=object(), db=None
    )
    assert result["ok"] is True
    assert result["service"] == "kubelet"
    assert result["action"] == "restart"
    argv = captured["argv"]
    assert argv[1] == "service"
    assert "kubelet" in argv and "restart" in argv


def test_talos_service_action_http(
    client, admin_headers, operator_headers, monkeypatch
):
    env = _env(client, admin_headers, "pf-svc-act")
    _patch_talos(monkeypatch, stdout="")
    resp = client.post(
        _node_url(env["id"], "service/kubelet/restart"), headers=operator_headers
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True


def test_talos_service_invalid_action_http(client, admin_headers):
    env = _env(client, admin_headers, "pf-svc-bad")
    resp = client.post(
        _node_url(env["id"], "service/kubelet/explode"), headers=admin_headers
    )
    assert resp.status_code == 400


def test_talos_service_invalid_id_http(client, admin_headers):
    env = _env(client, admin_headers, "pf-svc-id")
    resp = client.post(
        _node_url(env["id"], "service/--help/restart"), headers=admin_headers
    )
    assert resp.status_code == 400


def test_talos_shutdown_missing_talosctl_not_500(client, admin_headers, monkeypatch):
    env = _env(client, admin_headers, "pf-shut-miss")
    _patch_talos(monkeypatch, which=None)
    # Enqueue path always 200; run_sync surfaces the missing-talosctl failure.
    resp = client.post(
        _node_url(env["id"], "shutdown"),
        headers=admin_headers,
        json={"run_sync": True},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is False
    assert body["status"] == "failed"
    assert "talosctl" in (body.get("error") or "").lower()
