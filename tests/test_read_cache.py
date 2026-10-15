"""Short-lived singleflight for live Kubernetes and OpenStack reads."""

from __future__ import annotations

import threading
import time
import uuid
from types import SimpleNamespace

import pytest

from app.services import k8s_ops, openstack_ops, read_cache
from app.services.read_cache import cached_read


def test_cached_read_second_call_skips_loader():
    calls = {"n": 0}

    def loader():
        calls["n"] += 1
        return {"items": ["a"], "n": calls["n"]}

    first = cached_read("read-cache-reuse", loader)
    second = cached_read("read-cache-reuse", loader)
    assert calls["n"] == 1
    assert second == first
    assert second is not first
    assert second == {"items": ["a"], "n": 1}


def test_cached_read_caller_cannot_mutate_cached_object():
    calls = {"n": 0}

    def loader():
        calls["n"] += 1
        return {"items": ["a"]}

    first = cached_read("read-cache-mutate", loader)
    first["items"].append("from-leader")
    second = cached_read("read-cache-mutate", loader)
    assert second["items"] == ["a"]
    assert second is not first
    second["items"].append("from-hit")
    third = cached_read("read-cache-mutate", loader)
    assert third["items"] == ["a"]
    assert calls["n"] == 1


def test_cached_read_refresh_runs_loader_again():
    calls = {"n": 0}

    def loader():
        calls["n"] += 1
        return {"items": ["a"], "n": calls["n"]}

    first = cached_read("read-cache-refresh", loader)
    cached_read("read-cache-refresh", loader)
    refreshed = cached_read("read-cache-refresh", loader, refresh=True)
    assert calls["n"] == 2
    assert refreshed == {"items": ["a"], "n": 2}
    assert refreshed is not first
    again = cached_read("read-cache-refresh", loader)
    assert again["n"] == 2
    assert calls["n"] == 2


def test_cached_read_raising_loader_is_not_cached():
    calls = {"n": 0}

    def loader():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("dial failed")
        return {"ok": True}

    with pytest.raises(RuntimeError, match="dial failed"):
        cached_read("read-cache-raise", loader)
    assert "read-cache-raise" not in read_cache._inflight
    got = cached_read("read-cache-raise", loader, wait=0.2)
    assert got == {"ok": True}
    assert calls["n"] == 2


def test_cached_read_empty_key_does_not_cache():
    calls = {"n": 0}

    def loader():
        calls["n"] += 1
        return {"n": calls["n"]}

    assert cached_read("", loader)["n"] == 1
    assert cached_read("", loader)["n"] == 2


def test_cached_read_concurrent_miss_runs_loader_once(monkeypatch):
    calls = {"n": 0}
    claims = {"n": 0}
    release = threading.Event()
    in_loader = threading.Event()
    real_claim = read_cache._claim

    def counting_claim(key: str):
        claims["n"] += 1
        return real_claim(key)

    monkeypatch.setattr(read_cache, "_claim", counting_claim)

    def loader():
        calls["n"] += 1
        in_loader.set()
        assert release.wait(5)
        return {"items": [1]}

    results: list[dict] = []
    barrier = threading.Barrier(2)

    def worker():
        barrier.wait(5)
        results.append(cached_read("read-cache-flight", loader))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    try:
        assert in_loader.wait(5)
        deadline = _timed_out()
        while claims["n"] < 2 and not deadline():
            threading.Event().wait(0.01)
        assert claims["n"] == 2
        assert calls["n"] == 1
    finally:
        release.set()
        for thread in threads:
            thread.join(5)
    assert calls["n"] == 1
    assert results == [{"items": [1]}, {"items": [1]}]
    assert not any(thread.is_alive() for thread in threads)


def _timed_out(seconds: float = 5.0):
    end = time.monotonic() + seconds
    return lambda: time.monotonic() >= end


def test_k8s_services_reuses_a_recent_result(client, admin_headers, monkeypatch):
    env = client.post(
        "/api/v1/environments", headers=admin_headers, json={"name": "k8s-cache-svc"}
    ).json()
    calls = {"n": 0}

    def fake_list_services(env, settings=None, *, namespace=""):  # noqa: ARG001
        calls["n"] += 1
        return {
            "ok": True,
            "services": [{"name": f"svc-{calls['n']}"}],
            "namespace": namespace,
            "error": None,
        }

    monkeypatch.setattr(k8s_ops, "list_services", fake_list_services)
    url = f"/api/v1/environments/{env['id']}/k8s/services"
    first = client.get(url, headers=admin_headers)
    second = client.get(url, headers=admin_headers)
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert calls["n"] == 1
    assert second.json() == first.json()
    assert first.json()["services"] == [{"name": "svc-1"}]
    third = client.get(f"{url}?refresh=1", headers=admin_headers)
    assert third.status_code == 200, third.text
    assert calls["n"] == 2
    assert third.json()["services"] == [{"name": "svc-2"}]


def test_cloud_inventory_singleflight_and_hit_skips_context(monkeypatch):
    calls = {"api": 0, "build": 0}
    claims = {"n": 0}
    release = threading.Event()
    in_loader = threading.Event()
    real_claim = openstack_ops._inventory_claim

    def counting_claim(key: str):
        claims["n"] += 1
        return real_claim(key)

    class Ctx:
        def cleanup(self):
            return None

    def fake_build(env, settings=None):  # noqa: ARG001
        calls["build"] += 1
        return Ctx()

    def fake_api(ctx):  # noqa: ARG001
        calls["api"] += 1
        if calls["api"] == 1:
            in_loader.set()
            assert release.wait(5)
        return {
            "available": True,
            "source": "openstack-api",
            "error": None,
            "cached": False,
            "servers": [{"name": "a"}],
        }

    monkeypatch.setattr(openstack_ops, "_inventory_claim", counting_claim)
    monkeypatch.setattr(openstack_ops, "build_context", fake_build)
    monkeypatch.setattr(openstack_ops, "_inventory_via_api", fake_api)
    env = SimpleNamespace(id=f"inv-flight-{uuid.uuid4().hex[:8]}")
    openstack_ops.invalidate_cloud_cache(env.id)

    results: list[dict] = []
    barrier = threading.Barrier(2)

    def worker():
        barrier.wait(5)
        results.append(openstack_ops.cloud_inventory(env))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    try:
        assert in_loader.wait(5)
        deadline = _timed_out()
        while claims["n"] < 2 and not deadline():
            threading.Event().wait(0.01)
        assert claims["n"] == 2
        assert calls == {"api": 1, "build": 1}
    finally:
        release.set()
        for thread in threads:
            thread.join(5)
    assert not any(thread.is_alive() for thread in threads)
    assert calls == {"api": 1, "build": 1}
    assert len(results) == 2
    assert {row["cached"] for row in results} == {False, True}
    assert all(row["servers"] == [{"name": "a"}] for row in results)

    hit = openstack_ops.cloud_inventory(env)
    assert calls == {"api": 1, "build": 1}
    assert hit["cached"] is True
    hit["servers"].append({"name": "mutated"})
    again = openstack_ops.cloud_inventory(env)
    assert again["servers"] == [{"name": "a"}]
    assert again["cached"] is True
    assert calls == {"api": 1, "build": 1}

    openstack_ops.invalidate_cloud_cache(env.id)
    refreshed = openstack_ops.cloud_inventory(env)
    assert calls == {"api": 2, "build": 2}
    assert refreshed["cached"] is False
    assert refreshed["servers"] == [{"name": "a"}]


def test_cloud_inventory_does_not_cache_unavailable(monkeypatch):
    calls = {"n": 0}

    class Ctx:
        def cleanup(self):
            return None

    def fake_api(ctx):  # noqa: ARG001
        calls["n"] += 1
        return {
            "available": False,
            "source": "unavailable",
            "error": "down",
            "cached": False,
        }

    monkeypatch.setattr(
        openstack_ops, "build_context", lambda env, settings=None: Ctx()
    )
    monkeypatch.setattr(openstack_ops, "_inventory_via_api", fake_api)
    env = SimpleNamespace(id=f"inv-down-{uuid.uuid4().hex[:8]}")
    openstack_ops.invalidate_cloud_cache(env.id)
    first = openstack_ops.cloud_inventory(env)
    assert env.id not in openstack_ops._inventory_inflight
    second = openstack_ops.cloud_inventory(env)
    assert first["available"] is False
    assert second.get("cached") is not True
    assert calls["n"] == 2
