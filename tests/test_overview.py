"""Overview is one read of the slow sections."""

from __future__ import annotations

import uuid

from app.services import k8s_ops, openstack_ops, platform


def _env(client, admin_headers) -> str:
    name = f"ov-{uuid.uuid4().hex[:10]}"
    create = client.post(
        "/api/v1/environments",
        headers=admin_headers,
        json={"name": name, "region": "lab", "tier": "dev"},
    )
    assert create.status_code in (200, 201), create.text
    return create.json()["id"]


def test_overview_is_one_read_and_reuses_each_cache(client, admin_headers, monkeypatch):
    eid = _env(client, admin_headers)
    calls = {"platform": 0, "cloud": 0, "vms": 0, "ingresses": 0, "workloads": 0}

    def fake_platform(env, settings=None, db=None):
        calls["platform"] += 1
        return {"nodes": [{"name": "n1"}], "error": None, "install_image": "img", "cluster": {}}

    def fake_cloud(env, settings=None, refresh=False):
        calls["cloud"] += 1
        return {"available": True, "servers": [{"id": "s1"}], "networks": [], "projects": [], "floating_ips": []}

    def fake_vms(env, settings=None):
        calls["vms"] += 1
        return {"vms": [{"id": "s1"}], "source": "live"}

    def fake_ingresses(env, settings=None, namespace=""):
        calls["ingresses"] += 1
        return {"ok": True, "ingresses": [{"name": "ing"}]}

    def fake_empty(env, settings=None, namespace=""):
        return {"ok": True}

    def fake_pools(env, settings=None):
        return {"ok": True, "pools": [{"name": "pool"}]}

    def fake_pods(env, settings=None, namespace="", pods_only=False):
        calls["workloads"] += 1
        return {"ok": True, "pods": [{"name": "pod"}]}

    monkeypatch.setattr(platform, "_load_platform_overview", fake_platform)
    monkeypatch.setattr(openstack_ops, "cloud_inventory", fake_cloud)
    monkeypatch.setattr(openstack_ops, "list_servers", fake_vms)
    monkeypatch.setattr(k8s_ops, "list_ingresses", fake_ingresses)
    monkeypatch.setattr(k8s_ops, "list_services", fake_empty)
    monkeypatch.setattr(k8s_ops, "list_gateways", fake_empty)
    monkeypatch.setattr(k8s_ops, "list_httproutes", fake_empty)
    monkeypatch.setattr(k8s_ops, "list_metallb_pools", fake_pools)
    monkeypatch.setattr(k8s_ops, "list_workloads", fake_pods)

    first = client.get(f"/api/v1/environments/{eid}/overview", headers=admin_headers)
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["environment_id"] == eid
    assert body["platform"]["nodes"][0]["name"] == "n1"
    assert body["cloud"]["servers"][0]["id"] == "s1"
    assert body["vms"]["vms"][0]["id"] == "s1"
    assert body["ingresses"]["ingresses"][0]["name"] == "ing"
    assert body["pools"]["pools"][0]["name"] == "pool"
    assert body["workloads"]["pods"][0]["name"] == "pod"
    assert calls == {"platform": 1, "cloud": 1, "vms": 1, "ingresses": 1, "workloads": 1}

    second = client.get(f"/api/v1/environments/{eid}/overview", headers=admin_headers)
    assert second.status_code == 200, second.text
    assert calls["platform"] == 1
    assert calls["ingresses"] == 1
    assert calls["workloads"] == 1
    assert calls["vms"] == 1

    listed = client.get(
        f"/api/v1/environments/{eid}/k8s/ingresses",
        headers=admin_headers,
    )
    assert listed.status_code == 200, listed.text
    assert listed.json()["ingresses"][0]["name"] == "ing"
    assert calls["ingresses"] == 1


def test_overview_keeps_the_other_sections_when_one_fails(client, admin_headers, monkeypatch):
    eid = _env(client, admin_headers)

    def boom(env, settings=None, namespace=""):
        raise RuntimeError("ingress dial failed")

    def fake_platform(env, settings=None, db=None):
        return {"nodes": [{"name": "n1"}], "error": None, "install_image": "img", "cluster": {}}

    monkeypatch.setattr(k8s_ops, "list_ingresses", boom)
    monkeypatch.setattr(platform, "_load_platform_overview", fake_platform)
    monkeypatch.setattr(
        openstack_ops,
        "cloud_inventory",
        lambda env, settings=None, refresh=False: {"available": False, "servers": []},
    )
    monkeypatch.setattr(
        openstack_ops,
        "list_servers",
        lambda env, settings=None: {"vms": [], "source": "unavailable"},
    )
    monkeypatch.setattr(k8s_ops, "list_services", lambda env, settings=None, namespace="": {"services": []})
    monkeypatch.setattr(k8s_ops, "list_gateways", lambda env, settings=None, namespace="": {"gateways": []})
    monkeypatch.setattr(k8s_ops, "list_httproutes", lambda env, settings=None, namespace="": {"httproutes": []})
    monkeypatch.setattr(k8s_ops, "list_metallb_pools", lambda env, settings=None: {"pools": []})
    monkeypatch.setattr(
        k8s_ops,
        "list_workloads",
        lambda env, settings=None, namespace="", pods_only=False: {"pods": []},
    )

    got = client.get(f"/api/v1/environments/{eid}/overview", headers=admin_headers)
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["platform"]["nodes"][0]["name"] == "n1"
    assert body["ingresses"]["ok"] is False
    assert "ingress dial failed" not in str(body["ingresses"])
