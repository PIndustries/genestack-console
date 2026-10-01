"""Cloud mutation routes: viewer 403, dry_run skips OpenStack, native REST wrappers."""

from __future__ import annotations

import uuid
from unittest.mock import patch

from app.services import openstack_ops
from tests.test_openstack_ops import (
    VALID_ID,
    VALID_ID_2,
    _FakeOSClient,
    _create_env,
    _run_result,
)

OK = {"ok": True, "returncode": 0, "message": "accepted"}


def test_new_cloud_routes_viewer_403(client, admin_headers, viewer_headers):
    env = _create_env(client, admin_headers)
    base = f"/api/v1/environments/{env['id']}/cloud"
    cases = [
        (
            "POST",
            f"{base}/images",
            {"name": "cirros", "url": "https://example.invalid/a.img"},
        ),
        ("DELETE", f"{base}/images/{VALID_ID}", None),
        ("PATCH", f"{base}/images/{VALID_ID}", {"visibility": "public"}),
        (
            "POST",
            f"{base}/flavors",
            {"name": "m1.tiny", "vcpus": 1, "ram": 512, "disk": 1},
        ),
        ("DELETE", f"{base}/flavors/{VALID_ID}", None),
        ("POST", f"{base}/keypairs", {"name": "laptop"}),
        ("DELETE", f"{base}/keypairs/laptop", None),
        ("POST", f"{base}/security-groups", {"name": "web"}),
        ("DELETE", f"{base}/security-groups/{VALID_ID}", None),
        ("DELETE", f"{base}/floating-ips/{VALID_ID}", None),
        ("POST", f"{base}/floating-ips/disassociate", {"address": "203.0.113.10"}),
        ("POST", f"{base}/volumes/{VALID_ID}/extend", {"size": 20}),
        ("POST", f"{base}/volumes/{VALID_ID}/snapshot", {"name": "snap-1"}),
        ("DELETE", f"{base}/volume-snapshots/{VALID_ID}", None),
        ("POST", f"{base}/servers/{VALID_ID}/resize", {"flavor": "m1.small"}),
        ("POST", f"{base}/servers/{VALID_ID}/rebuild", {"image": "cirros"}),
        ("POST", f"{base}/servers/{VALID_ID}/snapshot", {"name": "vm-snap"}),
        ("POST", f"{base}/servers/{VALID_ID}/security-groups", {"name": "default"}),
        ("DELETE", f"{base}/servers/{VALID_ID}/security-groups/default", None),
        ("POST", f"{base}/servers/{VALID_ID}/pause", None),
        ("DELETE", f"{base}/networks/{VALID_ID}", None),
        (
            "POST",
            f"{base}/subnets",
            {"network": VALID_ID, "cidr": "10.0.0.0/24", "name": "s1"},
        ),
        ("DELETE", f"{base}/subnets/{VALID_ID}", None),
        ("DELETE", f"{base}/routers/{VALID_ID}", None),
        ("DELETE", f"{base}/routers/{VALID_ID}/interfaces/{VALID_ID_2}", None),
        ("POST", f"{base}/projects", {"name": "demo", "enabled": True}),
        ("PATCH", f"{base}/projects/{VALID_ID}", {"enabled": False}),
        ("POST", f"{base}/users", {"name": "alice", "password": "supersecret"}),
        ("PATCH", f"{base}/users/{VALID_ID}", {"enabled": False}),
        ("POST", f"{base}/load-balancers", {"name": "lb-1", "vip_subnet_id": VALID_ID}),
        ("DELETE", f"{base}/load-balancers/{VALID_ID}", None),
        ("POST", f"{base}/dns-zones", {"name": "example.com.", "email": "a@b.example"}),
        ("DELETE", f"{base}/dns-zones/{VALID_ID}", None),
        ("POST", f"{base}/secrets", {"name": "tok", "payload": "hidden"}),
        ("DELETE", f"{base}/secrets/{VALID_ID}", None),
    ]
    for method, url, body in cases:
        resp = client.request(method, url, headers=viewer_headers, json=body)
        assert resp.status_code == 403, (method, url, resp.status_code, resp.text)


def test_new_cloud_routes_operator_ok(client, admin_headers):
    env = _create_env(client, admin_headers)
    base = f"/api/v1/environments/{env['id']}/cloud"
    patches = {
        "image_create": OK,
        "image_delete": OK,
        "image_patch": OK,
        "flavor_create": OK,
        "flavor_delete": OK,
        "keypair_create": {**OK, "keypair": {"name": "laptop"}},
        "keypair_delete": OK,
        "security_group_create": OK,
        "security_group_delete": OK,
        "floating_ip_delete": OK,
        "floating_ip_disassociate": OK,
        "volume_extend": OK,
        "volume_snapshot_create": OK,
        "volume_snapshot_delete": OK,
        "server_resize": OK,
        "server_rebuild": OK,
        "server_snapshot": OK,
        "server_security_group_add": OK,
        "server_security_group_remove": OK,
        "server_action": OK,
        "network_delete": OK,
        "subnet_create": OK,
        "subnet_delete": OK,
        "router_delete": OK,
        "router_remove_interface": OK,
        "project_create": OK,
        "project_update": OK,
        "user_create": OK,
        "user_update": OK,
        "load_balancer_create": OK,
        "load_balancer_delete": OK,
        "dns_zone_create": OK,
        "dns_zone_delete": OK,
        "secret_create": OK,
        "secret_delete": OK,
    }
    with patch.multiple(
        openstack_ops, **{k: (lambda *a, _v=v, **kw: _v) for k, v in patches.items()}
    ):
        assert (
            client.post(
                f"{base}/images",
                headers=admin_headers,
                json={"name": "cirros", "url": "https://example.invalid/a.img"},
            ).status_code
            == 200
        )
        assert (
            client.delete(
                f"{base}/images/{VALID_ID}", headers=admin_headers
            ).status_code
            == 200
        )
        assert (
            client.patch(
                f"{base}/images/{VALID_ID}",
                headers=admin_headers,
                json={"visibility": "public"},
            ).status_code
            == 200
        )
        assert (
            client.post(
                f"{base}/flavors",
                headers=admin_headers,
                json={"name": "m1.tiny", "vcpus": 1, "ram": 512, "disk": 1},
            ).status_code
            == 200
        )
        assert (
            client.delete(
                f"{base}/flavors/{VALID_ID}", headers=admin_headers
            ).status_code
            == 200
        )
        assert (
            client.post(
                f"{base}/keypairs", headers=admin_headers, json={"name": "laptop"}
            ).status_code
            == 200
        )
        assert (
            client.delete(f"{base}/keypairs/laptop", headers=admin_headers).status_code
            == 200
        )
        assert (
            client.post(
                f"{base}/security-groups", headers=admin_headers, json={"name": "web"}
            ).status_code
            == 200
        )
        assert (
            client.delete(
                f"{base}/security-groups/{VALID_ID}", headers=admin_headers
            ).status_code
            == 200
        )
        assert (
            client.delete(
                f"{base}/floating-ips/{VALID_ID}", headers=admin_headers
            ).status_code
            == 200
        )
        assert (
            client.post(
                f"{base}/floating-ips/disassociate",
                headers=admin_headers,
                json={"address": "203.0.113.10"},
            ).status_code
            == 200
        )
        assert (
            client.post(
                f"{base}/volumes/{VALID_ID}/extend",
                headers=admin_headers,
                json={"size": 20},
            ).status_code
            == 200
        )
        assert (
            client.post(
                f"{base}/volumes/{VALID_ID}/snapshot",
                headers=admin_headers,
                json={"name": "snap-1"},
            ).status_code
            == 200
        )
        assert (
            client.delete(
                f"{base}/volume-snapshots/{VALID_ID}", headers=admin_headers
            ).status_code
            == 200
        )
        assert (
            client.post(
                f"{base}/servers/{VALID_ID}/resize",
                headers=admin_headers,
                json={"flavor": "m1.small"},
            ).status_code
            == 200
        )
        assert (
            client.post(
                f"{base}/servers/{VALID_ID}/rebuild",
                headers=admin_headers,
                json={"image": "cirros"},
            ).status_code
            == 200
        )
        assert (
            client.post(
                f"{base}/servers/{VALID_ID}/snapshot",
                headers=admin_headers,
                json={"name": "vm-snap"},
            ).status_code
            == 200
        )
        assert (
            client.post(
                f"{base}/servers/{VALID_ID}/security-groups",
                headers=admin_headers,
                json={"name": "default"},
            ).status_code
            == 200
        )
        assert (
            client.delete(
                f"{base}/servers/{VALID_ID}/security-groups/default",
                headers=admin_headers,
            ).status_code
            == 200
        )
        assert (
            client.post(
                f"{base}/servers/{VALID_ID}/pause", headers=admin_headers
            ).status_code
            == 200
        )
        assert (
            client.post(
                f"{base}/servers/{VALID_ID}/shelve", headers=admin_headers
            ).status_code
            == 200
        )
        assert (
            client.delete(
                f"{base}/networks/{VALID_ID}", headers=admin_headers
            ).status_code
            == 200
        )
        assert (
            client.post(
                f"{base}/subnets",
                headers=admin_headers,
                json={"network": VALID_ID, "cidr": "10.0.0.0/24", "name": "s1"},
            ).status_code
            == 200
        )
        assert (
            client.delete(
                f"{base}/subnets/{VALID_ID}", headers=admin_headers
            ).status_code
            == 200
        )
        assert (
            client.delete(
                f"{base}/routers/{VALID_ID}", headers=admin_headers
            ).status_code
            == 200
        )
        assert (
            client.delete(
                f"{base}/routers/{VALID_ID}/interfaces/{VALID_ID_2}",
                headers=admin_headers,
            ).status_code
            == 200
        )
        assert (
            client.post(
                f"{base}/projects",
                headers=admin_headers,
                json={"name": "demo", "description": "d", "enabled": True},
            ).status_code
            == 200
        )
        assert (
            client.patch(
                f"{base}/projects/{VALID_ID}",
                headers=admin_headers,
                json={"enabled": False},
            ).status_code
            == 200
        )
        assert (
            client.post(
                f"{base}/users",
                headers=admin_headers,
                json={"name": "alice", "password": "supersecret"},
            ).status_code
            == 200
        )
        assert (
            client.patch(
                f"{base}/users/{VALID_ID}",
                headers=admin_headers,
                json={"enabled": False},
            ).status_code
            == 200
        )
        assert (
            client.post(
                f"{base}/load-balancers",
                headers=admin_headers,
                json={"name": "lb-1", "vip_subnet_id": VALID_ID},
            ).status_code
            == 200
        )
        assert (
            client.delete(
                f"{base}/load-balancers/{VALID_ID}", headers=admin_headers
            ).status_code
            == 200
        )
        assert (
            client.post(
                f"{base}/dns-zones",
                headers=admin_headers,
                json={"name": "example.com.", "email": "a@b.example"},
            ).status_code
            == 200
        )
        assert (
            client.delete(
                f"{base}/dns-zones/{VALID_ID}", headers=admin_headers
            ).status_code
            == 200
        )
        assert (
            client.post(
                f"{base}/secrets",
                headers=admin_headers,
                json={"name": "tok", "payload": "hidden"},
            ).status_code
            == 200
        )
        assert (
            client.delete(
                f"{base}/secrets/{VALID_ID}", headers=admin_headers
            ).status_code
            == 200
        )


def test_volume_snapshots_and_optional_lists_viewer_ok(
    client, admin_headers, viewer_headers
):
    env = _create_env(client, admin_headers)
    base = f"/api/v1/environments/{env['id']}/cloud"
    with patch.object(
        openstack_ops,
        "volume_snapshots_list",
        return_value={"snapshots": [], "error": None, "source": "openstack-api"},
    ):
        resp = client.get(f"{base}/volume-snapshots", headers=viewer_headers)
    assert resp.status_code == 200, resp.text
    with patch.object(
        openstack_ops,
        "load_balancers_list",
        return_value={"available": False, "load_balancers": [], "error": None},
    ):
        resp = client.get(f"{base}/load-balancers", headers=viewer_headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["available"] is False
    with patch.object(
        openstack_ops,
        "dns_zones_list",
        return_value={"available": False, "zones": [], "error": None},
    ):
        assert (
            client.get(f"{base}/dns-zones", headers=viewer_headers).json()["available"]
            is False
        )
    with patch.object(
        openstack_ops,
        "secrets_list",
        return_value={"available": False, "secrets": [], "error": None},
    ):
        assert (
            client.get(f"{base}/secrets", headers=viewer_headers).json()["available"]
            is False
        )


def test_ops_dry_run_does_not_call_client():
    env = openstack_ops.Environment(name=f"dry-{uuid.uuid4().hex[:8]}")
    with (
        patch.object(openstack_ops, "OpenStackClient") as mock_client,
        patch.object(
            openstack_ops, "_run_openstack", return_value=_run_result(dry_run=True)
        ),
    ):
        results = [
            openstack_ops.image_create(
                env,
                None,
                name="cirros",
                url="https://example.invalid/a.img",
                dry_run=True,
            ),
            openstack_ops.image_delete(env, None, VALID_ID, dry_run=True),
            openstack_ops.flavor_create(
                env, None, name="m1.tiny", vcpus=1, ram=512, disk=1, dry_run=True
            ),
            openstack_ops.keypair_create(env, None, name="laptop", dry_run=True),
            openstack_ops.security_group_create(env, None, name="web", dry_run=True),
            openstack_ops.floating_ip_delete(env, None, VALID_ID, dry_run=True),
            openstack_ops.floating_ip_disassociate(
                env, None, address="203.0.113.10", dry_run=True
            ),
            openstack_ops.volume_extend(env, None, VALID_ID, size=20, dry_run=True),
            openstack_ops.volume_snapshot_create(
                env, None, VALID_ID, name="snap", dry_run=True
            ),
            openstack_ops.server_resize(
                env, None, VALID_ID, flavor="m1.small", dry_run=True
            ),
            openstack_ops.server_rebuild(
                env, None, VALID_ID, image="cirros", dry_run=True
            ),
            openstack_ops.server_snapshot(
                env, None, VALID_ID, name="vm-snap", dry_run=True
            ),
            openstack_ops.network_delete(env, None, VALID_ID, dry_run=True),
            openstack_ops.subnet_create(
                env, None, network=VALID_ID, cidr="10.0.0.0/24", dry_run=True
            ),
            openstack_ops.router_delete(env, None, VALID_ID, dry_run=True),
            openstack_ops.project_create(env, None, name="demo", dry_run=True),
            openstack_ops.user_create(
                env, None, name="alice", password="supersecret", dry_run=True
            ),
            openstack_ops.load_balancer_create(
                env, None, name="lb", vip_subnet_id=VALID_ID, dry_run=True
            ),
            openstack_ops.dns_zone_create(
                env, None, name="example.com.", email="a@b.example", dry_run=True
            ),
            openstack_ops.secret_create(
                env, None, name="tok", payload="hidden", dry_run=True
            ),
            openstack_ops.server_action(env, None, "pause", VALID_ID, dry_run=True),
        ]
    mock_client.assert_not_called()
    assert all(r.get("ok") and r.get("dry_run") for r in results)


def test_ops_native_image_and_identity():
    fake = _FakeOSClient(
        image_create=lambda **kw: {"id": "i1", **kw},
        image_delete=lambda *_a, **_k: None,
        project_create=lambda **kw: {"id": "p1", **kw},
        user_create=lambda **kw: {"id": "u1", "name": kw.get("name")},
        flavor_create=lambda **kw: {"id": "f1", **kw},
        keypair_create=lambda **kw: {"name": kw.get("name"), "private_key": "BEGIN"},
    )
    env = openstack_ops.Environment(name=f"nat-{uuid.uuid4().hex[:8]}")
    with (
        patch.object(openstack_ops, "_kube_path", return_value="/tmp/kube"),
        patch.object(openstack_ops, "OpenStackClient", return_value=fake),
        patch.object(openstack_ops, "_run_openstack") as mock_run,
    ):
        img = openstack_ops.image_create(
            env, None, name="cirros", url="https://example.invalid/a.img", dry_run=False
        )
        proj = openstack_ops.project_create(env, None, name="demo", dry_run=False)
        user = openstack_ops.user_create(
            env, None, name="alice", password="supersecret", dry_run=False
        )
        flav = openstack_ops.flavor_create(
            env, None, name="m1.tiny", vcpus=1, ram=512, disk=1, dry_run=False
        )
        kp = openstack_ops.keypair_create(env, None, name="laptop", dry_run=False)
    assert img["ok"] is True
    assert img["image"]["name"] == "cirros"
    assert proj["ok"] is True
    assert user["user"]["name"] == "alice"
    assert "password" not in (user.get("user") or {})
    assert flav["flavor"]["vcpus"] == 1
    assert kp["keypair"]["private_key"] == "BEGIN"
    mock_run.assert_not_called()


def test_validation_rejects_before_cloud():
    env = openstack_ops.Environment(name=f"val-{uuid.uuid4().hex[:8]}")
    with patch.object(openstack_ops, "_run_openstack") as mock_run:
        bad = [
            openstack_ops.image_create(
                env, None, name="ok", url="file:///etc/passwd", dry_run=False
            ),
            openstack_ops.keypair_create(
                env, None, name="k", public_key="not-a-key", dry_run=False
            ),
            openstack_ops.user_create(
                env, None, name="alice", password="short", dry_run=False
            ),
            openstack_ops.volume_extend(env, None, "not-uuid", size=20, dry_run=False),
            openstack_ops.subnet_create(
                env, None, network=VALID_ID, cidr="999/99", dry_run=False
            ),
            openstack_ops.secret_create(env, None, name="t", payload="", dry_run=False),
        ]
    mock_run.assert_not_called()
    assert all(r["ok"] is False and r["returncode"] == 2 for r in bad)


def test_pause_action_native():
    called = {}

    def _action(sid, action):
        called["sid"] = sid
        called["action"] = action

    fake = _FakeOSClient(server_action=_action)
    env = openstack_ops.Environment(name=f"act-{uuid.uuid4().hex[:8]}")
    with (
        patch.object(openstack_ops, "_kube_path", return_value="/tmp/kube"),
        patch.object(openstack_ops, "OpenStackClient", return_value=fake),
        patch.object(openstack_ops, "_run_openstack") as mock_run,
    ):
        result = openstack_ops.server_action(
            env, None, "pause", VALID_ID, dry_run=False
        )
    assert result["ok"] is True
    assert called == {"sid": VALID_ID, "action": "pause"}
    mock_run.assert_not_called()
