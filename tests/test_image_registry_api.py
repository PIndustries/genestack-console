"""Registry cache read API. The warm job stays registry.mirror."""

from __future__ import annotations

import uuid

from app.services import image_registry
from app.services.catalog import get_operation


def test_helm_charts_marks_oci_ahead_of_http_repos():
    rows = image_registry.helm_charts()
    names = [row["name"] for row in rows]
    assert names.index("cert-manager") < names.index("keystone")
    by_name = {row["name"]: row for row in rows}
    assert by_name["cert-manager"]["oci"] is True
    assert by_name["cert-manager"]["registry"] == "quay.io"
    assert by_name["keystone"]["oci"] is False
    assert by_name["keystone"]["url"].startswith("https://")


def test_warm_cache_uses_the_job_route_not_the_missing_ops_path(client, admin_headers):
    """Warm image cache queues registry.mirror. The old ops URL 404s."""
    name = f"env-warm-{uuid.uuid4().hex[:10]}"
    create = client.post("/api/v1/environments", headers=admin_headers, json={"name": name})
    assert create.status_code in (200, 201), create.text
    eid = create.json()["id"]

    missing = client.post(
        f"/api/v1/ops/environments/{eid}/registry/mirror",
        headers=admin_headers,
        json={"dry_run": False},
    )
    assert missing.status_code == 404

    queued = client.post(
        f"/api/v1/environments/{eid}/jobs",
        headers=admin_headers,
        json={"operation": "registry.mirror", "params": {}},
    )
    assert queued.status_code == 201, queued.text
    body = queued.json()
    assert body["operation"] == "registry.mirror"
    assert body["environment_id"] == eid
    assert body["status"] == "queued"


def test_registry_mirror_is_an_admin_job():
    op = get_operation("registry.mirror")
    assert op is not None
    assert op.required_role == "admin"
    assert op.id == "registry.mirror"


def test_registry_status_is_readable(client, admin_headers, viewer_headers, monkeypatch):
    """GET returns caches and charts. It does not ask Docker."""

    def fake(db, env, settings=None):
        return {
            "environment_id": env.id,
            "environment_name": env.name,
            "bind": "10.200.0.50",
            "running": False,
            "ready": False,
            "caches": [
                {
                    "registry": "docker.io",
                    "running": False,
                    "port": 5001,
                    "endpoint": "http://10.200.0.50:5001",
                    "images": 0,
                    "repositories": [],
                }
            ],
            "ready_count": 0,
            "cache_count": 1,
            "image_count": 0,
            "last_mirror": None,
            "charts": [
                {
                    "name": "cert-manager",
                    "oci": True,
                    "registry": "quay.io",
                    "url": "oci://quay.io/jetstack/cert-manager",
                    "repo": "charts",
                }
            ],
        }

    monkeypatch.setattr(image_registry, "for_environment", fake)
    name = f"env-reg-{uuid.uuid4().hex[:10]}"
    create = client.post(
        "/api/v1/environments",
        headers=admin_headers,
        json={"name": name},
    )
    assert create.status_code in (200, 201), create.text
    eid = create.json()["id"]

    got = client.get(f"/api/v1/environments/{eid}/registry", headers=viewer_headers)
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["environment_id"] == eid
    assert body["bind"] == "10.200.0.50"
    assert body["caches"][0]["registry"] == "docker.io"
    assert body["charts"][0]["name"] == "cert-manager"

    missing = client.get(
        "/api/v1/environments/does-not-exist/registry",
        headers=viewer_headers,
    )
    assert missing.status_code == 404


def _one(name, remote, port, enabled=True):
    return {"name": name, "remote": remote, "port": port, "enabled": enabled}


def test_registry_host_prefers_saved_address_over_pxe(monkeypatch):
    monkeypatch.setattr(image_registry, "console_address", lambda: "172.16.10.7")
    doc = {
        "pxe": {"next_server": "10.8.8.8"},
        "registry": {"host": "10.7.7.7"},
    }
    assert image_registry.registry_host_from_doc(doc) == "10.7.7.7"
    assert image_registry.host_source(doc) == "registry"
    bare = {"pxe": {"next_server": "10.8.8.8"}}
    assert image_registry.registry_host_from_doc(bare) == "10.8.8.8"
    assert image_registry.host_source(bare) == "pxe"
    assert image_registry.registry_host_from_doc({}) == "172.16.10.7"
    assert image_registry.host_source({}) == "console"
    assert image_registry.registry_host_from_doc({"pxe": {"gateway": "0.0.0.0"}}) == "172.16.10.7"
    monkeypatch.setattr(image_registry, "console_address", lambda: "")
    assert image_registry.registry_host_from_doc({}) == ""
    assert image_registry.host_source({}) == ""
    assert "10.200.0.50" not in image_registry.talos_registry_patch_yaml({})


def test_console_address_uses_the_default_route(monkeypatch):
    class Sock:
        def connect(self, _addr):
            return None

        def getsockname(self):
            return ("172.16.10.7", 9)

        def close(self):
            return None

    monkeypatch.setattr(image_registry.socket, "socket", lambda *_a, **_k: Sock())
    assert image_registry.console_address() == "172.16.10.7"


def test_console_address_ignores_loopback(monkeypatch):
    class Sock:
        def connect(self, _addr):
            return None

        def getsockname(self):
            return ("127.0.0.1", 0)

        def close(self):
            return None

    monkeypatch.setattr(image_registry.socket, "socket", lambda *_a, **_k: Sock())
    assert image_registry.console_address() == ""


def test_disabled_upstream_drops_out_of_the_talos_mirror():
    doc = {
        "registry": {
            "host": "10.9.9.9",
            "upstreams": [
                _one("docker.io", "https://registry-1.docker.io", 5001, True),
                _one("quay.io", "https://quay.io", 5002, False),
            ],
        }
    }
    text = image_registry.talos_registry_patch_yaml(doc)
    assert "10.9.9.9:5001" in text
    assert "quay.io" not in text
    names = [name for name, _remote, _port in image_registry.active_upstreams(doc)]
    assert names == ["docker.io"]


def test_put_registry_saves_address_and_round_trips(client, operator_headers, viewer_headers):
    name = f"env-cache-{uuid.uuid4().hex[:10]}"
    create = client.post("/api/v1/environments", headers=operator_headers, json={"name": name})
    assert create.status_code in (200, 201), create.text
    eid = create.json()["id"]

    denied = client.put(
        f"/api/v1/environments/{eid}/registry",
        headers=viewer_headers,
        json={"host": "10.9.9.9", "upstreams": [_one("docker.io", "https://registry-1.docker.io", 5001)]},
    )
    assert denied.status_code == 403

    saved = client.put(
        f"/api/v1/environments/{eid}/registry",
        headers=operator_headers,
        json={
            "host": "10.9.9.9",
            "upstreams": [
                _one("docker.io", "https://registry-1.docker.io", 5001, True),
                _one("quay.io", "https://quay.io", 5002, False),
                _one("example.dev", "https://example.dev", 5010, True),
            ],
        },
    )
    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert body["bind"] == "10.9.9.9"
    assert body["host_source"] == "registry"
    assert body["configured_host"] == "10.9.9.9"
    by_name = {row["name"]: row for row in body["upstreams"]}
    assert by_name["quay.io"]["enabled"] is False
    assert by_name["example.dev"]["enabled"] is True
    assert by_name["example.dev"]["builtin"] is False
    assert by_name["docker.io"]["builtin"] is True
    assert any(row["registry"] == "example.dev" and row["endpoint"] == "http://10.9.9.9:5010" for row in body["caches"])
    assert all(row["registry"] != "quay.io" for row in body["caches"])

    again = client.get(f"/api/v1/environments/{eid}/registry", headers=viewer_headers)
    assert again.status_code == 200
    assert again.json()["configured_host"] == "10.9.9.9"


def test_put_registry_rejects_one_port_with_two_upstreams(client, admin_headers):
    name = f"env-cache-bad-{uuid.uuid4().hex[:10]}"
    create = client.post("/api/v1/environments", headers=admin_headers, json={"name": name})
    eid = create.json()["id"]
    bad = client.put(
        f"/api/v1/environments/{eid}/registry",
        headers=admin_headers,
        json={
            "host": "not a host",
            "upstreams": [_one("docker.io", "https://registry-1.docker.io", 5001)],
        },
    )
    assert bad.status_code == 422
    clash = client.put(
        f"/api/v1/environments/{eid}/registry",
        headers=admin_headers,
        json={
            "host": "10.9.9.9",
            "upstreams": [
                _one("docker.io", "https://registry-1.docker.io", 5001),
                _one("quay.io", "https://quay.io", 5001),
            ],
        },
    )
    assert clash.status_code == 422


def test_status_stays_fast_when_docker_does_not_answer(monkeypatch):
    def hung(*_args, **_kwargs):
        raise TimeoutError("docker hung")

    monkeypatch.setattr(image_registry, "_docker", hung)
    monkeypatch.setattr(image_registry, "console_address", lambda: "172.16.10.7")
    payload = image_registry.status({})
    assert payload["bind"] == "172.16.10.7"
    assert payload["host_source"] == "console"
    assert "10.200.0.50" not in str(payload)
    assert any(row["name"] == "docker.io" for row in payload["upstreams"])
    assert payload["caches"]
    assert all(row["running"] is False for row in payload["caches"])


def test_start_only_dry_run_does_not_call_docker(monkeypatch):
    def boom(*_args, **_kwargs):
        raise AssertionError("docker should stay unused")

    monkeypatch.setattr(image_registry, "_docker", boom)

    class Env:
        id = "not-a-real-env"

    logs: list[str] = []
    result = image_registry.mirror_cluster(
        Env(),
        None,
        logs.append,
        dry_run=True,
        start_only=True,
    )
    assert result["ok"] is True
    assert result["dry_run"] is True
    assert any("would start" in line for line in logs)
