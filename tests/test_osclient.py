"""Native OpenStack REST client (kube service proxy) tests."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from app.services.osclient import OpenStackClient, OpenStackError

FAKE_CA = base64.b64encode(b"not-a-real-ca").decode()
TOKEN_BODY = {
    "token": {
        "expires_at": "2099-01-01T00:00:00.000000Z",
        "project": {"id": "proj-1", "name": "admin"},
        "catalog": [],
    }
}
FLAVORS = {
    "flavors": [
        {
            "id": "f1",
            "name": "m1.tiny",
            "vcpus": 1,
            "ram": 512,
            "disk": 1,
            "os-flavor-access:is_public": True,
        }
    ]
}
IMAGES = {"images": [{"id": "i1", "name": "cirros", "status": "active", "size": 1024}]}
SERVERS = {
    "servers": [
        {
            "id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            "name": "demo-1",
            "status": "ACTIVE",
            "OS-EXT-STS:power_state": 1,
            "flavor": {"id": "f1"},
            "image": {"id": "i1"},
            "addresses": {"demo-net": [{"addr": "10.0.0.10", "version": 4}]},
            "OS-EXT-SRV-ATTR:host": "compute-1",
            "created": "2026-01-01T00:00:00Z",
            "tenant_id": "proj-1",
            "key_name": None,
        },
        {
            "id": "bbbbbbbb-bbbb-cccc-dddd-eeeeeeeeeeee",
            "name": "vol-backed",
            "status": "SHUTOFF",
            "flavor": {"id": "missing-flavor"},
            "image": {"id": "missing-image"},
            "addresses": {},
            "created": "2026-01-02T00:00:00Z",
            "tenant_id": "proj-1",
        },
    ]
}
EMPTY = {}


def _kubeconfig(path: Path) -> Path:
    doc = {
        "apiVersion": "v1",
        "clusters": [
            {
                "name": "fake",
                "cluster": {
                    "server": "https://kube.example:6443",
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
        self.catalog: list[dict[str, Any]] = []
        self.nova_quota = {"id": "proj-1", "instances": 10, "cores": 20, "ram": 51200}
        self.neutron_quota = {
            "network": 100,
            "subnet": 100,
            "router": 10,
            "floatingip": 50,
            "security_group": 10,
            "port": 500,
        }

    def handle_request(self, request: httpx.Request) -> httpx.Response:  # noqa: ARG002
        url = str(request.url)
        method = request.method
        path = url.split("?", 1)[0]
        self.calls.append((method, url))
        body: Any = None
        if request.content:
            try:
                body = json.loads(request.content)
            except (json.JSONDecodeError, UnicodeDecodeError):
                body = request.content
        self.bodies.append(body)
        if "/secrets/keystone-admin" in path:
            pw = base64.b64encode(b"secret-pass").decode()
            return httpx.Response(200, json={"data": {"password": pw}})
        if path.endswith("/v3/auth/tokens") and method == "POST":
            token = dict(TOKEN_BODY)
            token["token"] = dict(TOKEN_BODY["token"])
            token["token"]["catalog"] = list(self.catalog)
            return httpx.Response(
                201,
                json=token,
                headers={"X-Subject-Token": "os-token"},
            )
        if "/flavors/detail" in path:
            return httpx.Response(200, json=FLAVORS)
        if method == "GET" and path.endswith("/v2/images"):
            return httpx.Response(200, json=IMAGES)
        if method == "POST" and path.endswith("/v2/images"):
            img = body if isinstance(body, dict) else {}
            return httpx.Response(
                201,
                json={
                    "id": "img-new",
                    "name": img.get("name") or "cirros",
                    "status": "queued",
                    "visibility": img.get("visibility") or "private",
                    "disk_format": img.get("disk_format"),
                    "container_format": img.get("container_format"),
                },
            )
        if method == "POST" and "/v2/images/" in path and path.endswith("/import"):
            return httpx.Response(202, json={})
        if method == "PATCH" and "/v2/images/" in path:
            return httpx.Response(
                200, json={"id": "i1", "name": "renamed", "visibility": "public"}
            )
        if method == "DELETE" and "/v2/images/" in path:
            return httpx.Response(204)
        if method == "POST" and path.endswith("/v2.1/flavors"):
            flav = (body or {}).get("flavor") if isinstance(body, dict) else {}
            return httpx.Response(
                200,
                json={"flavor": {"id": "flav-new", **(flav or {})}},
            )
        if method == "DELETE" and "/v2.1/flavors/" in path:
            return httpx.Response(202)
        if "/servers/detail" in path:
            return httpx.Response(200, json=SERVERS)
        if method == "GET" and "/volumes/detail" in path:
            return httpx.Response(200, json={"volumes": []})
        if method == "GET" and path.endswith("/v2.0/networks"):
            return httpx.Response(200, json={"networks": []})
        if method == "GET" and path.endswith("/v2.0/subnets"):
            return httpx.Response(200, json={"subnets": []})
        if method == "GET" and path.endswith("/v2.0/routers"):
            return httpx.Response(200, json={"routers": []})
        if method == "GET" and path.endswith("/v2.0/floatingips"):
            return httpx.Response(
                200,
                json={
                    "floatingips": [
                        {
                            "id": "fip-1",
                            "floating_ip_address": "203.0.113.10",
                            "fixed_ip_address": None,
                            "port_id": "port-1",
                            "status": "ACTIVE",
                        }
                    ]
                },
            )
        if method == "GET" and path.endswith("/v2.0/security-groups"):
            return httpx.Response(
                200,
                json={
                    "security_groups": [
                        {
                            "id": "g1",
                            "name": "default",
                            "description": "default",
                            "security_group_rules": [
                                {
                                    "id": "rule-1",
                                    "direction": "ingress",
                                    "ethertype": "IPv4",
                                    "protocol": "tcp",
                                    "port_range_min": 22,
                                    "port_range_max": 22,
                                    "remote_ip_prefix": "0.0.0.0/0",
                                    "remote_group_id": None,
                                }
                            ],
                        }
                    ]
                },
            )
        if method == "POST" and path.endswith("/v2.0/security-group-rules"):
            rule = (
                (body or {}).get("security_group_rule")
                if isinstance(body, dict)
                else {}
            )
            payload = {"id": "rule-new", **(rule or {})}
            return httpx.Response(201, json={"security_group_rule": payload})
        if method == "DELETE" and "/v2.0/security-group-rules/" in path:
            return httpx.Response(204)
        if method == "POST" and path.endswith("/v2.0/routers"):
            router = (body or {}).get("router") if isinstance(body, dict) else {}
            return httpx.Response(
                201,
                json={
                    "router": {
                        "id": "r1",
                        "name": (router or {}).get("name") or "router1",
                        "external_gateway_info": (router or {}).get(
                            "external_gateway_info"
                        ),
                    }
                },
            )
        if method == "PUT" and path.endswith("/add_router_interface"):
            return httpx.Response(
                200,
                json={
                    "id": "r1",
                    "subnet_id": (
                        (body or {}).get("subnet_id")
                        if isinstance(body, dict)
                        else None
                    ),
                    "port_id": "port-if-1",
                },
            )
        if "/os-quota-sets/" in path and method == "GET":
            return httpx.Response(200, json={"quota_set": dict(self.nova_quota)})
        if "/os-quota-sets/" in path and method == "PUT":
            qs = (body or {}).get("quota_set") if isinstance(body, dict) else {}
            self.nova_quota.update(qs or {})
            return httpx.Response(200, json={"quota_set": dict(self.nova_quota)})
        if method == "GET" and "/v2.0/quotas/" in path:
            return httpx.Response(200, json={"quota": dict(self.neutron_quota)})
        if method == "PUT" and "/v2.0/quotas/" in path:
            q = (body or {}).get("quota") if isinstance(body, dict) else {}
            self.neutron_quota.update(q or {})
            return httpx.Response(200, json={"quota": dict(self.neutron_quota)})
        if method == "GET" and path.endswith("/v2.0/ports"):
            return httpx.Response(
                200,
                json={
                    "ports": [
                        {
                            "id": "port-1",
                            "device_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                        }
                    ]
                },
            )
        if method == "GET" and path.endswith("/os-keypairs"):
            return httpx.Response(200, json={"keypairs": []})
        if method == "POST" and path.endswith("/os-keypairs"):
            kp = (body or {}).get("keypair") if isinstance(body, dict) else {}
            out = {"name": (kp or {}).get("name") or "k1", "fingerprint": "ab:cd"}
            if not (kp or {}).get("public_key"):
                out["private_key"] = "-----BEGIN OPENSSH PRIVATE KEY-----\nfake\n"
            return httpx.Response(200, json={"keypair": out})
        if method == "DELETE" and "/os-keypairs/" in path:
            return httpx.Response(202)
        if method == "GET" and path.endswith("/v3/projects"):
            return httpx.Response(
                200,
                json={"projects": [{"id": "proj-1", "name": "admin", "enabled": True}]},
            )
        if method == "POST" and path.endswith("/v3/projects"):
            proj = (body or {}).get("project") if isinstance(body, dict) else {}
            return httpx.Response(
                201,
                json={
                    "project": {
                        "id": "proj-new",
                        "name": (proj or {}).get("name"),
                        "enabled": True,
                    }
                },
            )
        if method == "PATCH" and "/v3/projects/" in path:
            proj = (body or {}).get("project") if isinstance(body, dict) else {}
            return httpx.Response(
                200, json={"project": {"id": "proj-1", **(proj or {})}}
            )
        if method == "GET" and path.endswith("/v3/users"):
            return httpx.Response(
                200, json={"users": [{"id": "u1", "name": "admin", "enabled": True}]}
            )
        if method == "POST" and path.endswith("/v3/users"):
            user = (body or {}).get("user") if isinstance(body, dict) else {}
            return httpx.Response(
                201,
                json={
                    "user": {
                        "id": "u-new",
                        "name": (user or {}).get("name"),
                        "enabled": True,
                    }
                },
            )
        if method == "PATCH" and "/v3/users/" in path:
            user = (body or {}).get("user") if isinstance(body, dict) else {}
            cleaned = {k: v for k, v in (user or {}).items() if k != "password"}
            return httpx.Response(200, json={"user": {"id": "u1", **cleaned}})
        if method == "GET" and "/v3/roles" in path:
            return httpx.Response(
                200, json={"roles": [{"id": "role-member", "name": "member"}]}
            )
        if method == "PUT" and "/roles/" in path:
            return httpx.Response(204)
        if method == "POST" and path.endswith("/v2.0/security-groups"):
            sg = (body or {}).get("security_group") if isinstance(body, dict) else {}
            return httpx.Response(
                201,
                json={
                    "security_group": {"id": "sg-new", "name": (sg or {}).get("name")}
                },
            )
        if method == "DELETE" and "/v2.0/security-groups/" in path:
            return httpx.Response(204)
        if method == "DELETE" and "/v2.0/floatingips/" in path:
            return httpx.Response(204)
        if method == "DELETE" and path.endswith("/v2.0/networks/n1"):
            return httpx.Response(204)
        if method == "DELETE" and "/v2.0/networks/" in path:
            return httpx.Response(204)
        if method == "DELETE" and "/v2.0/subnets/" in path:
            return httpx.Response(204)
        if (
            method == "DELETE"
            and "/v2.0/routers/" in path
            and "/remove_router_interface" not in path
        ):
            return httpx.Response(204)
        if method == "PUT" and path.endswith("/remove_router_interface"):
            return httpx.Response(
                200,
                json={
                    "id": "r1",
                    "subnet_id": (
                        (body or {}).get("subnet_id")
                        if isinstance(body, dict)
                        else None
                    ),
                },
            )
        if method == "GET" and "/snapshots/detail" in path:
            return httpx.Response(200, json={"snapshots": []})
        if method == "POST" and path.endswith("/snapshots"):
            snap = (body or {}).get("snapshot") if isinstance(body, dict) else {}
            return httpx.Response(
                202,
                json={
                    "snapshot": {
                        "id": "snap-1",
                        "name": (snap or {}).get("name"),
                        "status": "creating",
                    }
                },
            )
        if method == "DELETE" and "/snapshots/" in path:
            return httpx.Response(202)
        if method == "POST" and "/volumes/" in path and path.endswith("/action"):
            return httpx.Response(202, json={})
        if method == "GET" and path.endswith("/v2/lbaas/loadbalancers"):
            return httpx.Response(
                200,
                json={
                    "loadbalancers": [
                        {
                            "id": "lb-1",
                            "name": "web",
                            "provisioning_status": "ACTIVE",
                            "operating_status": "ONLINE",
                            "vip_address": "10.0.0.20",
                            "vip_subnet_id": "sn1",
                        }
                    ]
                },
            )
        if method == "POST" and path.endswith("/v2/lbaas/loadbalancers"):
            lb = (body or {}).get("loadbalancer") if isinstance(body, dict) else {}
            return httpx.Response(
                201,
                json={"loadbalancer": {"id": "lb-new", "name": (lb or {}).get("name")}},
            )
        if method == "DELETE" and "/v2/lbaas/loadbalancers/" in path:
            return httpx.Response(204)
        if method == "GET" and path.endswith("/v2/zones"):
            return httpx.Response(
                200,
                json={
                    "zones": [
                        {
                            "id": "z1",
                            "name": "example.com.",
                            "status": "ACTIVE",
                            "email": "a@b.c",
                        }
                    ]
                },
            )
        if method == "POST" and path.endswith("/v2/zones"):
            return httpx.Response(
                202,
                json={
                    "id": "z-new",
                    "name": (
                        (body or {}).get("name") if isinstance(body, dict) else None
                    ),
                },
            )
        if method == "DELETE" and "/v2/zones/" in path:
            return httpx.Response(202)
        if method == "GET" and path.endswith("/v1/secrets"):
            return httpx.Response(
                200,
                json={
                    "secrets": [
                        {
                            "name": "api-token",
                            "status": "ACTIVE",
                            "secret_ref": "http://barbican/v1/secrets/sec-1",
                        }
                    ]
                },
            )
        if method == "POST" and path.endswith("/v1/secrets"):
            return httpx.Response(
                201, json={"secret_ref": "http://barbican/v1/secrets/sec-new"}
            )
        if method == "DELETE" and "/v1/secrets/" in path:
            return httpx.Response(204)
        if "/remote-consoles" in path and method == "POST":
            return httpx.Response(
                200,
                json={
                    "remote_console": {
                        "url": "https://novnc.example/vnc",
                        "type": "novnc",
                    }
                },
            )
        if "/os-volume_attachments" in path and method == "POST":
            return httpx.Response(200, json={"volumeAttachment": {"volumeId": "v1"}})
        if "/os-volume_attachments" in path and method == "DELETE":
            return httpx.Response(202)
        if "/servers/" in path and path.endswith("/action") and method == "POST":
            return httpx.Response(202, json={})
        if method == "POST" and path.endswith("/v2.1/servers"):
            return httpx.Response(202, json={"server": {"id": "s1", "name": "demo-1"}})
        if method == "DELETE" and "/v2.1/servers/" in path:
            return httpx.Response(204)
        if method == "POST" and path.endswith("/volumes"):
            vol = (body or {}).get("volume") if isinstance(body, dict) else {}
            return httpx.Response(
                202,
                json={
                    "volume": {
                        "id": "v1",
                        "name": vol.get("name") or "data-1",
                        "size": vol.get("size") or 10,
                        "status": "creating",
                    }
                },
            )
        if method == "DELETE" and "/volumes/" in path:
            return httpx.Response(202)
        if method == "POST" and path.endswith("/v2.0/networks"):
            return httpx.Response(
                201, json={"network": {"id": "n1", "name": "demo-net"}}
            )
        if method == "POST" and path.endswith("/v2.0/subnets"):
            return httpx.Response(201, json={"subnet": {"id": "sn1"}})
        if method == "POST" and path.endswith("/v2.0/floatingips"):
            return httpx.Response(
                201,
                json={
                    "floatingip": {"id": "fip-1", "floating_ip_address": "203.0.113.10"}
                },
            )
        if method == "PUT" and "/v2.0/floatingips/" in path:
            return httpx.Response(
                200, json={"floatingip": {"id": "fip-1", "port_id": "port-1"}}
            )
        return httpx.Response(404, text=f"no mock for {method} {url}")


@pytest.fixture
def os_client(tmp_path, monkeypatch):
    kc = _kubeconfig(tmp_path / "kubeconfig")
    transport = RouterTransport()

    real_client = httpx.Client

    def fake_client(*args, **kwargs):
        kwargs.pop("verify", None)
        kwargs.pop("cert", None)
        kwargs["transport"] = transport
        kwargs.setdefault("base_url", "https://kube.example:6443")
        return real_client(*args, **kwargs)

    monkeypatch.setattr("app.services.osclient.httpx.Client", fake_client)
    client = OpenStackClient(str(kc))
    client._http = httpx.Client(
        transport=transport, base_url="https://kube.example:6443"
    )
    client._apiserver = "https://kube.example:6443"
    client._cleanup = []
    yield client, transport
    client.close()


def test_inventory_uses_keystone_then_nova_glance(os_client):
    client, transport = os_client
    inv = client.inventory()
    assert inv["available"] is True
    assert inv["source"] == "openstack-api"
    assert inv["flavors"][0]["name"] == "m1.tiny"
    assert inv["images"][0]["name"] == "cirros"
    assert inv["servers"][0]["image"] == "cirros"
    assert inv["servers"][0]["flavor"] == "m1.tiny"
    assert inv["servers"][0]["project_id"] == "proj-1"
    assert inv["servers"][0]["project_name"] == "admin"
    assert inv["projects"][0]["name"] == "admin"
    methods_urls = transport.calls
    assert any("/auth/tokens" in u and m == "POST" for m, u in methods_urls)
    assert any("keystone-admin" in u for _, u in methods_urls)
    assert any("nova-api" in u for _, u in methods_urls)
    assert any("glance-api" in u for _, u in methods_urls)


def test_list_servers_resolves_image_and_flavor_names(os_client):
    client, _transport = os_client
    rows = client.list_servers()
    assert rows[0]["image"] == "cirros"
    assert rows[0]["flavor"] == "m1.tiny"
    assert rows[0]["project_id"] == "proj-1"
    assert rows[0]["project_name"] == "admin"
    assert rows[0]["host"] == "compute-1"
    assert rows[0]["addresses"] == {"demo-net": [{"addr": "10.0.0.10", "version": 4}]}
    assert rows[1]["image"] == "missing-image"
    assert rows[1]["flavor"] == "missing-flavor"
    assert rows[1]["project_id"] == "proj-1"
    assert rows[1]["project_name"] == "admin"


def test_server_create_posts_nova(os_client):
    client, transport = os_client
    server = client.server_create(
        name="demo-1", image="i1", flavor="f1", network="n1", key_name="kp"
    )
    assert server["id"] == "s1"
    posted = [
        b["server"]
        for b in transport.bodies
        if isinstance(b, dict)
        and isinstance(b.get("server"), dict)
        and "imageRef" in b["server"]
    ]
    assert posted
    assert posted[0]["imageRef"] == "i1"
    assert posted[0]["flavorRef"] == "f1"
    assert posted[0]["networks"] == [{"uuid": "n1"}]
    assert posted[0]["key_name"] == "kp"
    assert any(
        m == "POST" and u.split("?", 1)[0].endswith("/v2.1/servers")
        for m, u in transport.calls
    )


def test_volume_create_posts_cinder(os_client):
    client, transport = os_client
    vol = client.volume_create(name="data-1", size=10)
    assert vol["id"] == "v1"
    assert vol["size"] == 10
    assert any(
        m == "POST" and u.split("?", 1)[0].endswith("/volumes")
        for m, u in transport.calls
    )
    assert any(
        isinstance(b, dict) and (b.get("volume") or {}).get("size") == 10
        for b in transport.bodies
    )
    assert any("cinder-api:8776" in u for _, u in transport.calls)


def test_server_start_stop_posts_nova_action(os_client):
    client, transport = os_client
    sid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    client.server_action(sid, "start")
    client.server_action(sid, "stop")
    assert {"os-start": None} in transport.bodies
    assert {"os-stop": None} in transport.bodies
    assert any(u.endswith("/action") and m == "POST" for m, u in transport.calls)


def test_server_reboot_posts_nova_action(os_client):
    client, transport = os_client
    client.server_action("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "reboot")
    assert any(u.endswith("/action") and m == "POST" for m, u in transport.calls)
    assert {"reboot": {"type": "SOFT"}} in transport.bodies


def test_network_create_posts_neutron(os_client):
    client, transport = os_client
    client.network_create(name="demo-net", cidr="10.0.0.0/24")
    assert any("/v2.0/networks" in u and m == "POST" for m, u in transport.calls)
    assert any("/v2.0/subnets" in u and m == "POST" for m, u in transport.calls)


def test_proxy_paths_are_kube_service_proxy(os_client):
    client, _transport = os_client
    ident = client._proxy("identity", "/v3/auth/tokens")
    assert ident.endswith(
        "/api/v1/namespaces/openstack/services/http:keystone-api:5000/proxy/v3/auth/tokens"
    )
    assert "services/http:nova-api:8774/proxy" in client._proxy(
        "compute", "/v2.1/servers"
    )
    assert "services/http:neutron-server:9696/proxy" in client._proxy(
        "network", "/v2.0/networks"
    )
    assert "services/http:cinder-api:8776/proxy" in client._proxy(
        "volume", "/v3/volumes"
    )
    assert "services/http:glance-api:9292/proxy" in client._proxy("image", "/v2/images")


def test_load_kube_http_uses_sslcontext_cert_chain(tmp_path, monkeypatch):
    from app.services.osclient import load_kube_http

    loaded: dict[str, str] = {}

    class FakeCtx:
        def load_cert_chain(self, certfile, keyfile):
            loaded["cert"] = certfile
            loaded["key"] = keyfile

    captured: dict[str, Any] = {}

    class Dummy:
        def close(self):
            pass

    monkeypatch.setattr(
        "app.services.osclient.ssl.create_default_context", lambda **_kw: FakeCtx()
    )

    def fake_client(**kwargs):
        captured.update(kwargs)
        return Dummy()

    monkeypatch.setattr("app.services.osclient.httpx.Client", fake_client)
    doc = {
        "apiVersion": "v1",
        "clusters": [
            {
                "name": "c",
                "cluster": {
                    "server": "https://kube.example:6443",
                    "certificate-authority-data": FAKE_CA,
                },
            }
        ],
        "users": [
            {
                "name": "u",
                "user": {
                    "client-certificate-data": FAKE_CA,
                    "client-key-data": FAKE_CA,
                },
            }
        ],
        "contexts": [{"name": "c", "context": {"cluster": "c", "user": "u"}}],
        "current-context": "c",
    }
    path = tmp_path / "admin.conf"
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    _client, server, cleanup = load_kube_http(str(path))
    assert server == "https://kube.example:6443"
    assert isinstance(captured.get("verify"), FakeCtx)
    assert "cert" not in captured
    assert loaded.get("cert") and loaded.get("key")
    for item in cleanup:
        Path(item).unlink(missing_ok=True)


def test_server_action_unknown_raises(os_client):
    client, _transport = os_client
    with pytest.raises(OpenStackError, match="unknown server action"):
        client.server_action("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "explode")


def test_list_security_groups_includes_rules(os_client):
    client, _transport = os_client
    groups = client.list_security_groups()
    assert groups[0]["name"] == "default"
    assert groups[0]["rules"][0]["id"] == "rule-1"
    assert groups[0]["rules"][0]["direction"] == "ingress"
    assert groups[0]["rules"][0]["protocol"] == "tcp"
    assert groups[0]["rules"][0]["port_range_min"] == 22
    assert groups[0]["rules"][0]["remote_ip_prefix"] == "0.0.0.0/0"


def test_security_group_rule_create_posts_neutron(os_client):
    client, transport = os_client
    rule = client.security_group_rule_create(
        sg_id="g1",
        direction="ingress",
        ethertype="IPv4",
        protocol="tcp",
        port_range_min=80,
        port_range_max=80,
        remote_ip_prefix="10.0.0.0/8",
    )
    assert rule["id"] == "rule-new"
    posted = [
        b["security_group_rule"]
        for b in transport.bodies
        if isinstance(b, dict) and isinstance(b.get("security_group_rule"), dict)
    ]
    assert posted
    assert posted[0]["security_group_id"] == "g1"
    assert posted[0]["direction"] == "ingress"
    assert posted[0]["protocol"] == "tcp"
    assert posted[0]["port_range_min"] == 80
    assert posted[0]["remote_ip_prefix"] == "10.0.0.0/8"
    assert any(
        m == "POST" and u.split("?", 1)[0].endswith("/v2.0/security-group-rules")
        for m, u in transport.calls
    )


def test_security_group_rule_delete(os_client):
    client, transport = os_client
    client.security_group_rule_delete("rule-1")
    assert any(
        m == "DELETE" and "/v2.0/security-group-rules/rule-1" in u.split("?", 1)[0]
        for m, u in transport.calls
    )


def test_router_create_posts_neutron(os_client):
    client, transport = os_client
    router = client.router_create(name="edge", external_network="n-ext")
    assert router["id"] == "r1"
    posted = [
        b["router"]
        for b in transport.bodies
        if isinstance(b, dict)
        and isinstance(b.get("router"), dict)
        and "external_gateway_info" in b["router"]
    ]
    assert posted
    assert posted[0]["name"] == "edge"
    assert posted[0]["external_gateway_info"] == {"network_id": "n-ext"}
    assert any(
        m == "POST" and u.split("?", 1)[0].endswith("/v2.0/routers")
        for m, u in transport.calls
    )


def test_router_add_interface_put(os_client):
    client, transport = os_client
    data = client.router_add_interface(router_id="r1", subnet_id="sn1")
    assert data["subnet_id"] == "sn1"
    assert any(
        m == "PUT"
        and u.split("?", 1)[0].endswith("/v2.0/routers/r1/add_router_interface")
        for m, u in transport.calls
    )
    assert any(
        isinstance(b, dict) and b.get("subnet_id") == "sn1" for b in transport.bodies
    )


def test_network_create_external_sets_router_external(os_client):
    client, transport = os_client
    client.network_create(name="public", external=True)
    posted = [
        b["network"]
        for b in transport.bodies
        if isinstance(b, dict)
        and isinstance(b.get("network"), dict)
        and b["network"].get("name") == "public"
    ]
    assert posted
    assert posted[0].get("router:external") is True
    assert any(
        m == "POST" and u.split("?", 1)[0].endswith("/v2.0/networks")
        for m, u in transport.calls
    )
    assert not any(
        m == "POST" and u.split("?", 1)[0].endswith("/v2.0/subnets")
        for m, u in transport.calls
    )


def test_network_create_external_with_cidr_still_makes_subnet(os_client):
    client, transport = os_client
    client.network_create(name="public", cidr="203.0.113.0/24", external=True)
    assert any(
        m == "POST" and u.split("?", 1)[0].endswith("/v2.0/networks")
        for m, u in transport.calls
    )
    assert any(
        m == "POST" and u.split("?", 1)[0].endswith("/v2.0/subnets")
        for m, u in transport.calls
    )


def test_get_and_update_quotas(os_client):
    client, transport = os_client
    client._project_id = "proj-1"
    got = client.get_quotas()
    assert got["compute"] == {"instances": 10, "cores": 20, "ram": 51200}
    assert got["network"]["floatingip"] == 50
    assert got["network"]["security_group"] == 10
    updated = client.quotas_update(
        compute={"instances": 15, "cores": 30}, network={"router": 20}
    )
    assert updated["compute"]["instances"] == 15
    assert any(m == "GET" and "/os-quota-sets/proj-1" in u for m, u in transport.calls)
    assert any(m == "PUT" and "/os-quota-sets/proj-1" in u for m, u in transport.calls)
    assert any(m == "PUT" and "/v2.0/quotas/proj-1" in u for m, u in transport.calls)
    assert any(
        isinstance(b, dict) and (b.get("quota_set") or {}).get("instances") == 15
        for b in transport.bodies
    )
    assert any(
        isinstance(b, dict) and (b.get("quota") or {}).get("router") == 20
        for b in transport.bodies
    )


def test_inventory_includes_quotas_and_sg_rules(os_client):
    client, transport = os_client
    inv = client.inventory()
    assert inv["quotas"]["compute"]["instances"] == 10
    assert inv["quotas"]["network"]["port"] == 500
    assert inv["security_groups"][0]["rules"][0]["protocol"] == "tcp"
    assert inv["load_balancers_available"] is False
    assert inv["dns_zones_available"] is False
    assert inv["secrets_available"] is False
    assert inv["load_balancers"] == []
    assert any("/os-quota-sets/" in u and m == "GET" for m, u in transport.calls)
    assert any("/v2.0/quotas/" in u and m == "GET" for m, u in transport.calls)


def test_image_create_import_and_delete(os_client):
    client, transport = os_client
    img = client.image_create(
        name="cirros",
        disk_format="qcow2",
        url="https://example.invalid/cirros.img",
    )
    assert img["id"] == "img-new"
    assert any(
        m == "POST" and u.split("?", 1)[0].endswith("/v2/images")
        for m, u in transport.calls
    )
    assert any(
        m == "POST" and u.split("?", 1)[0].endswith("/import")
        for m, u in transport.calls
    )
    client.image_delete("img-new")
    assert any(m == "DELETE" and "/v2/images/img-new" in u for m, u in transport.calls)
    patched = client.image_patch("i1", name="renamed", visibility="public")
    assert patched["name"] == "renamed"
    assert any(
        m == "PATCH" and "/v2/images/i1" in u.split("?", 1)[0]
        for m, u in transport.calls
    )


def test_keypair_generate_and_delete(os_client):
    client, transport = os_client
    kp = client.keypair_create(name="laptop")
    assert kp["name"] == "laptop"
    assert "private_key" in kp
    client.keypair_delete("laptop")
    assert any(m == "DELETE" and "/os-keypairs/laptop" in u for m, u in transport.calls)


def test_security_group_create_delete(os_client):
    client, transport = os_client
    sg = client.security_group_create(name="web", description="http")
    assert sg["id"] == "sg-new"
    client.security_group_delete("sg-new")
    assert any(
        m == "DELETE" and "/v2.0/security-groups/sg-new" in u
        for m, u in transport.calls
    )


def test_floating_ip_release_and_disassociate(os_client):
    client, transport = os_client
    client.floating_ip_disassociate(address="203.0.113.10")
    assert any(
        isinstance(b, dict) and (b.get("floatingip") or {}).get("port_id") is None
        for b in transport.bodies
    )
    client.floating_ip_delete("fip-1")
    assert any(
        m == "DELETE" and "/v2.0/floatingips/fip-1" in u for m, u in transport.calls
    )


def test_volume_extend_and_snapshot(os_client):
    client, transport = os_client
    client._project_id = "proj-1"
    client.volume_extend("v1", 20)
    assert any(
        isinstance(b, dict) and (b.get("os-extend") or {}).get("new_size") == 20
        for b in transport.bodies
    )
    snap = client.volume_snapshot_create(volume_id="v1", name="snap-a")
    assert snap["id"] == "snap-1"
    client.volume_snapshot_delete("snap-1")
    assert any(m == "DELETE" and "/snapshots/snap-1" in u for m, u in transport.calls)


def test_server_resize_rebuild_snapshot_and_extra_actions(os_client):
    client, transport = os_client
    sid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    client.server_resize(sid, "f1")
    client.server_rebuild(sid, "i1")
    client.server_snapshot(sid, "snap-vm")
    client.server_add_security_group(sid, "default")
    client.server_remove_security_group(sid, "default")
    client.server_action(sid, "pause")
    client.server_action(sid, "shelve")
    bodies = [b for b in transport.bodies if isinstance(b, dict)]
    assert {"resize": {"flavorRef": "f1"}} in bodies
    assert {"rebuild": {"imageRef": "i1"}} in bodies
    assert {"createImage": {"name": "snap-vm"}} in bodies
    assert {"addSecurityGroup": {"name": "default"}} in bodies
    assert {"removeSecurityGroup": {"name": "default"}} in bodies
    assert {"pause": None} in bodies
    assert {"shelve": None} in bodies


def test_network_subnet_router_delete(os_client):
    client, transport = os_client
    sub = client.subnet_create(network="n1", cidr="10.0.1.0/24", name="n1-sub")
    assert sub["id"] == "sn1"
    client.network_delete("n1")
    client.subnet_delete("sn1")
    client.router_delete("r1")
    client.router_remove_interface(router_id="r1", subnet_id="sn1")
    assert any(m == "DELETE" and "/v2.0/networks/n1" in u for m, u in transport.calls)
    assert any(
        m == "PUT" and u.split("?", 1)[0].endswith("/remove_router_interface")
        for m, u in transport.calls
    )


def test_identity_and_flavor_crud(os_client):
    client, transport = os_client
    proj = client.project_create(name="demo", description="d", enabled=True)
    assert proj["id"] == "proj-new"
    client.project_update("proj-1", enabled=False)
    user = client.user_create(name="alice", password="super-secret", project="proj-1")
    assert user["id"] == "u-new"
    assert "password" not in user
    client.user_update("u1", enabled=False, password="new-secret1")
    flav = client.flavor_create(name="m1.nano", vcpus=1, ram=64, disk=1)
    assert flav["id"] == "flav-new"
    client.flavor_delete("flav-new")
    assert any(
        m == "POST" and u.split("?", 1)[0].endswith("/v2.1/flavors")
        for m, u in transport.calls
    )
    assert any(
        m == "DELETE" and "/v2.1/flavors/flav-new" in u for m, u in transport.calls
    )


def test_optional_services_probe_catalog(os_client):
    client, transport = os_client
    assert client.has_service("load-balancer", "octavia") is False
    with pytest.raises(OpenStackError, match="not in service catalog"):
        client.list_load_balancers()
    transport.catalog = [
        {"type": "load-balancer", "name": "octavia"},
        {"type": "dns", "name": "designate"},
        {"type": "key-manager", "name": "barbican"},
    ]
    client._token = None
    assert client.has_service("load-balancer", "octavia") is True
    lbs = client.list_load_balancers()
    assert lbs[0]["name"] == "web"
    zones = client.list_dns_zones()
    assert zones[0]["name"] == "example.com."
    secrets = client.list_secrets()
    assert secrets[0]["id"] == "sec-1"
    assert "payload" not in secrets[0]
    client.load_balancer_create(name="lb-1", vip_subnet_id="sn1")
    client.dns_zone_create(name="example.com", email="hostmaster@example.com")
    secret = client.secret_create(name="api-token", payload="s3cret")
    assert secret["id"] == "sec-new"
    inv = client.inventory()
    assert inv["load_balancers_available"] is True
    assert inv["dns_zones_available"] is True
    assert inv["secrets_available"] is True
    assert inv["load_balancers"][0]["id"] == "lb-1"
