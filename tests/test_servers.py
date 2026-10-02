"""Servers endpoints (saved inventory) and drift."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
import yaml

from app.models import Environment
from app.services.inventory import build_inventory_from_environment


def _suffix() -> str:
    return uuid.uuid4().hex[:8]


def _create_env(client, headers, **fields) -> dict:
    resp = client.post(
        "/api/v1/environments",
        headers=headers,
        json={"name": f"env-srv-{_suffix()}", **fields},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _assign(client, headers, env_id, **body):
    return client.post(
        f"/api/v1/environments/{env_id}/servers/assign", headers=headers, json=body
    )


def _static(client, headers, env_id, **body):
    return client.post(
        f"/api/v1/environments/{env_id}/servers/static", headers=headers, json=body
    )


def _remove(client, headers, env_id, **body):
    return client.post(
        f"/api/v1/environments/{env_id}/servers/remove", headers=headers, json=body
    )


def _write_disk_inventory(config_dir: Path, host_names: list[str]) -> None:
    inventory_dir = config_dir / "inventory"
    inventory_dir.mkdir(parents=True, exist_ok=True)
    (inventory_dir / "inventory.yaml").write_text(
        yaml.safe_dump(
            {
                "all": {
                    "children": {
                        "controllers": {"hosts": {h: {} for h in host_names}},
                    }
                }
            }
        ),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# List: saved hosts only
# ---------------------------------------------------------------------------


def test_servers_list_returns_saved_hosts(client, admin_headers):
    env = _create_env(client, admin_headers)
    resp = client.get(
        f"/api/v1/environments/{env['id']}/servers", headers=admin_headers
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "maas_configured" not in body
    assert "mock" not in body
    assert body["servers"] == []

    assert (
        _static(
            client, admin_headers, env["id"], hostname="bare-01", ip="10.30.0.5"
        ).status_code
        == 201
    )
    body = client.get(
        f"/api/v1/environments/{env['id']}/servers", headers=admin_headers
    ).json()
    assert "mock" not in body
    assert body["ovh_bound"] is False
    assert [s["hostname"] for s in body["servers"]] == ["bare-01"]


# ---------------------------------------------------------------------------
# Assign
# ---------------------------------------------------------------------------


def test_assign_upserts_doc(client, admin_headers):
    env = _create_env(client, admin_headers)

    resp = _assign(
        client,
        admin_headers,
        env["id"],
        system_id="abc123",
        hostname="gs-control-01",
        roles=["control"],
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["version"] == 1
    assert resp.json()["server"]["assigned"] is True
    assert resp.json()["server"]["source"] == "static"

    resp = _assign(
        client,
        admin_headers,
        env["id"],
        system_id="def456",
        hostname="gs-compute-01",
        roles=["compute", "storage"],
        ip="10.20.0.21",
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["version"] == 2  # assignments are versioned doc edits

    servers = client.get(
        f"/api/v1/environments/{env['id']}/servers", headers=admin_headers
    )
    by_hostname = {s["hostname"]: s for s in servers.json()["servers"]}
    assert by_hostname["gs-control-01"]["assigned"] is True
    assert by_hostname["gs-control-01"]["roles"] == ["control"]
    assert by_hostname["gs-control-01"]["source"] == "static"
    assert by_hostname["gs-control-01"]["ip"] in (None, "")
    assert by_hostname["gs-compute-01"]["assigned"] is True
    assert by_hostname["gs-compute-01"]["roles"] == ["compute", "storage"]
    assert by_hostname["gs-compute-01"]["ip"] == "10.20.0.21"
    assert "gs-storage-01" not in by_hostname

    config = client.get(
        f"/api/v1/environments/{env['id']}/config", headers=admin_headers
    )
    doc = yaml.safe_load(config.json()["yaml"])
    assert doc["servers"]["gs-control-01"]["system_id"] == "abc123"
    assert doc["servers"]["gs-control-01"]["source"] == "static"
    assert doc["servers"]["gs-control-01"]["roles"] == ["control"]
    assert doc["servers"]["gs-compute-01"]["ip"] == "10.20.0.21"


def test_assign_reassignment_upserts(client, admin_headers):
    env = _create_env(client, admin_headers)
    assert (
        _assign(
            client,
            admin_headers,
            env["id"],
            system_id="abc123",
            hostname="ctrl-1",
            roles=["control"],
        ).status_code
        == 201
    )
    resp = _assign(
        client,
        admin_headers,
        env["id"],
        system_id="abc123",
        hostname="ctrl-1",
        roles=["network"],
    )
    assert resp.status_code == 201, resp.text

    config = client.get(
        f"/api/v1/environments/{env['id']}/config", headers=admin_headers
    )
    doc = yaml.safe_load(config.json()["yaml"])
    assert doc["servers"]["ctrl-1"]["roles"] == ["network"]


def test_assign_invalid_role_422(client, admin_headers):
    env = _create_env(client, admin_headers)
    resp = _assign(
        client, admin_headers, env["id"], system_id="abc123", roles=["bogus"]
    )
    assert resp.status_code == 422


def test_assign_unknown_machine_still_listed(client, admin_headers):
    """An assignment is saved from the posted fields. Nothing else is merged in."""
    env = _create_env(client, admin_headers)
    resp = _assign(
        client,
        admin_headers,
        env["id"],
        system_id="zzz999",
        hostname="retired-01",
        roles=["storage"],
    )
    assert resp.status_code == 201, resp.text

    servers = client.get(
        f"/api/v1/environments/{env['id']}/servers", headers=admin_headers
    )
    by_hostname = {s["hostname"]: s for s in servers.json()["servers"]}
    assert by_hostname["retired-01"]["assigned"] is True
    assert by_hostname["retired-01"]["system_id"] == "zzz999"
    assert by_hostname["retired-01"]["source"] == "static"
    assert by_hostname["retired-01"]["power_state"] is None


# ---------------------------------------------------------------------------
# Static hosts
# ---------------------------------------------------------------------------


def test_static_add_update_remove(client, admin_headers):
    env = _create_env(client, admin_headers)

    resp = _static(
        client,
        admin_headers,
        env["id"],
        hostname="bare-01",
        ip="10.30.0.5",
        roles=["compute"],
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["version"] == 1
    assert resp.json()["server"]["source"] == "static"
    assert resp.json()["server"]["assigned"] is True

    body = client.get(
        f"/api/v1/environments/{env['id']}/servers", headers=admin_headers
    ).json()
    entry = {s["hostname"]: s for s in body["servers"]}["bare-01"]
    assert entry["source"] == "static"
    assert entry["assigned"] is True
    assert entry["system_id"] is None
    assert entry["ssh_user"] is None
    assert entry["power_state"] is None
    assert entry["status"] is None
    assert entry["roles"] == ["compute"]

    # Re-adding the same hostname updates it (new doc version)
    resp = _static(
        client,
        admin_headers,
        env["id"],
        hostname="bare-01",
        ssh_user="ops",
        roles=["storage"],
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["version"] == 2
    config = client.get(
        f"/api/v1/environments/{env['id']}/config", headers=admin_headers
    )
    doc = yaml.safe_load(config.json()["yaml"])
    assert doc["servers"]["bare-01"]["roles"] == ["storage"]
    assert doc["servers"]["bare-01"]["ssh_user"] == "ops"
    assert doc["servers"]["bare-01"]["ip"] == "10.30.0.5"  # preserved

    resp = _remove(client, admin_headers, env["id"], hostname="bare-01")
    assert resp.status_code == 200, resp.text
    assert resp.json()["removed"] == "bare-01"
    body = client.get(
        f"/api/v1/environments/{env['id']}/servers", headers=admin_headers
    ).json()
    assert body["servers"] == []

    # Removing a missing host is a 404
    assert (
        _remove(client, admin_headers, env["id"], hostname="bare-01").status_code == 404
    )


def test_static_invalid_role_422(client, admin_headers):
    env = _create_env(client, admin_headers)
    resp = _static(
        client, admin_headers, env["id"], hostname="bare-01", roles=["bogus"]
    )
    assert resp.status_code == 422


def test_static_invalid_hostname_422(client, admin_headers):
    env = _create_env(client, admin_headers)
    resp = _static(
        client, admin_headers, env["id"], hostname="bad host!", roles=["compute"]
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Roles -> inventory groups
# ---------------------------------------------------------------------------


def test_roles_land_in_rendered_inventory_groups(client, admin_headers):
    env = _create_env(client, admin_headers)
    _assign(
        client,
        admin_headers,
        env["id"],
        system_id="abc123",
        hostname="ctrl-1",
        roles=["control", "network"],
    )
    _assign(
        client,
        admin_headers,
        env["id"],
        system_id="def456",
        hostname="cmp-1",
        roles=["compute"],
    )

    render = client.get(
        f"/api/v1/environments/{env['id']}/config/render", headers=admin_headers
    )
    inventory = yaml.safe_load(render.json()["files"]["inventory/inventory.yaml"])
    children = inventory["all"]["children"]
    k8s_children = children["k8s_cluster"]["children"]
    assert k8s_children["openstack_control_plane"]["hosts"] == {"ctrl-1": {}}
    assert k8s_children["ovn_network_nodes"]["hosts"] == {"ctrl-1": {}}
    assert k8s_children["openstack_compute_nodes"]["hosts"] == {"cmp-1": {}}
    assert k8s_children["kube_node"]["hosts"] == {
        "ctrl-1": {},
        "cmp-1": {},
    }


def test_storage_sub_roles_land_in_rendered_inventory_groups(client, admin_headers):
    env = _create_env(client, admin_headers)
    resp = _assign(
        client,
        admin_headers,
        env["id"],
        system_id="abc123",
        hostname="ceph-1",
        roles=["storage-ceph"],
    )
    assert resp.status_code == 201, resp.text
    resp = _static(
        client,
        admin_headers,
        env["id"],
        hostname="cin-1",
        ip="10.30.0.7",
        roles=["storage-cinder"],
    )
    assert resp.status_code == 201, resp.text

    render = client.get(
        f"/api/v1/environments/{env['id']}/config/render", headers=admin_headers
    )
    inventory = yaml.safe_load(render.json()["files"]["inventory/inventory.yaml"])
    k8s_children = inventory["all"]["children"]["k8s_cluster"]["children"]
    storage_children = k8s_children["storage_nodes"]["children"]
    assert storage_children["ceph_storage_nodes"]["hosts"] == {"ceph-1": {}}
    assert storage_children["cinder_storage_nodes"]["hosts"] == {"cin-1": {}}
    assert k8s_children["kube_node"]["hosts"] == {"ceph-1": {}, "cin-1": {}}


def test_static_hosts_in_rendered_inventory(client, admin_headers):
    env = _create_env(client, admin_headers)
    _static(
        client,
        admin_headers,
        env["id"],
        hostname="bare-01",
        ip="10.30.0.5",
        roles=["compute"],
    )
    _static(
        client,
        admin_headers,
        env["id"],
        hostname="bare-02",
        ip="10.30.0.6",
        ssh_user="ops",
        roles=["storage"],
    )
    _assign(
        client,
        admin_headers,
        env["id"],
        system_id="abc123",
        hostname="ctrl-1",
        roles=["control"],
    )

    render = client.get(
        f"/api/v1/environments/{env['id']}/config/render", headers=admin_headers
    )
    inventory = yaml.safe_load(render.json()["files"]["inventory/inventory.yaml"])
    hosts = inventory["all"]["hosts"]
    children = inventory["all"]["children"]
    k8s_children = children["k8s_cluster"]["children"]
    # Static hosts: ansible_host=ip, ansible_user=ssh_user (default ubuntu)
    assert hosts["bare-01"]["ansible_host"] == "10.30.0.5"
    assert hosts["bare-01"]["ansible_user"] == "ubuntu"
    assert hosts["bare-02"]["ansible_host"] == "10.30.0.6"
    assert hosts["bare-02"]["ansible_user"] == "ops"
    assert k8s_children["openstack_compute_nodes"]["hosts"] == {"bare-01": {}}
    assert k8s_children["storage_nodes"]["children"]["longhorn_storage_nodes"][
        "hosts"
    ] == {"bare-02": {}}
    # An assign with no address uses the hostname. Source is static, so the
    # default ansible user is written.
    assert hosts["ctrl-1"]["ansible_host"] == "ctrl-1"
    assert hosts["ctrl-1"]["ansible_user"] == "ubuntu"
    assert "maas_system_id" not in hosts["ctrl-1"]
    assert k8s_children["openstack_control_plane"]["hosts"] == {"ctrl-1": {}}


def test_saved_servers_group_by_role():
    """build_inventory_from_environment groups the saved document only."""
    env = Environment(id="env-x", name="env-x")
    servers = {
        "gs-control-01": {
            "system_id": "abc123",
            "roles": ["control"],
            "ip": "10.20.0.11",
            "source": "maas",
        },
        "gs-compute-01": {
            "roles": ["compute"],
            "ip": "10.20.0.21",
            "source": "maas",
        },
    }
    inventory = build_inventory_from_environment(env, servers=servers)
    children = inventory["all"]["children"]
    k8s_children = children["k8s_cluster"]["children"]
    assert k8s_children["openstack_control_plane"]["hosts"] == {"gs-control-01": {}}
    assert k8s_children["openstack_compute_nodes"]["hosts"] == {"gs-compute-01": {}}
    assert "ungrouped_maas" not in children
    assert "maas_system_id" not in inventory["all"]["hosts"]["gs-control-01"]


# ---------------------------------------------------------------------------
# Descriptor drift
# ---------------------------------------------------------------------------


def _put_servers_doc(client, headers, env_id):
    resp = client.put(
        f"/api/v1/environments/{env_id}/config",
        headers=headers,
        json={
            "yaml_text": (
                "servers:\n"
                "  ctrl-1:\n"
                "    system_id: abc123\n"
                "    source: maas\n"
                "    roles: [control]\n"
                "    ip: 10.20.0.11\n"
            )
        },
    )
    assert resp.status_code == 201, resp.text


def test_descriptor_drift_true_when_doc_differs_from_disk(
    client, admin_headers, tmp_path
):
    config_dir = tmp_path / "etc-genestack"
    config_dir.mkdir()
    _write_disk_inventory(config_dir, ["other-host"])
    env = _create_env(client, admin_headers, genestack_config_dir=str(config_dir))
    _put_servers_doc(client, admin_headers, env["id"])

    resp = client.get(
        f"/api/v1/environments/{env['id']}/descriptor", headers=admin_headers
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["topology"]["drift"] is True


def test_descriptor_drift_false_when_doc_matches_disk(client, admin_headers, tmp_path):
    config_dir = tmp_path / "etc-genestack"
    config_dir.mkdir()
    env = _create_env(client, admin_headers, genestack_config_dir=str(config_dir))
    _put_servers_doc(client, admin_headers, env["id"])

    # Write exactly the rendered inventory to disk
    render = client.get(
        f"/api/v1/environments/{env['id']}/config/render", headers=admin_headers
    )
    rendered = render.json()["files"]["inventory/inventory.yaml"]
    inventory_dir = config_dir / "inventory"
    inventory_dir.mkdir(parents=True, exist_ok=True)
    (inventory_dir / "inventory.yaml").write_text(rendered, encoding="utf-8")

    resp = client.get(
        f"/api/v1/environments/{env['id']}/descriptor", headers=admin_headers
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["topology"]["drift"] is False


def test_descriptor_drift_null_without_doc_or_disk(client, admin_headers, tmp_path):
    # No config document at all -> null even with an on-disk inventory
    config_dir = tmp_path / "etc-genestack"
    config_dir.mkdir()
    _write_disk_inventory(config_dir, ["other-host"])
    env = _create_env(client, admin_headers, genestack_config_dir=str(config_dir))
    resp = client.get(
        f"/api/v1/environments/{env['id']}/descriptor", headers=admin_headers
    )
    assert resp.json()["topology"]["drift"] is None

    # Doc present but no on-disk inventory -> null
    config_dir2 = tmp_path / "etc-genestack-2"
    config_dir2.mkdir()
    env2 = _create_env(client, admin_headers, genestack_config_dir=str(config_dir2))
    _put_servers_doc(client, admin_headers, env2["id"])
    resp = client.get(
        f"/api/v1/environments/{env2['id']}/descriptor", headers=admin_headers
    )
    assert resp.json()["topology"]["drift"] is None


# ---------------------------------------------------------------------------
# Tenant scoping
# ---------------------------------------------------------------------------


@pytest.fixture
def two_tenants(client, admin_headers):
    from tests.test_tenants import _create_tenant, _create_user, _login_headers

    tenant_a = _create_tenant(client, admin_headers)
    tenant_b = _create_tenant(client, admin_headers)
    env_a = _create_env(client, admin_headers, tenant_id=tenant_a["id"])
    env_b = _create_env(client, admin_headers, tenant_id=tenant_b["id"])
    operator = _create_user(
        client,
        admin_headers,
        memberships=[{"tenant_id": tenant_a["id"], "role": "operator"}],
    )
    return {
        "env_a": env_a,
        "env_b": env_b,
        "operator_headers": _login_headers(client, operator["username"]),
    }


def test_servers_endpoints_cross_tenant_403(client, two_tenants):
    env_b = two_tenants["env_b"]
    headers = two_tenants["operator_headers"]

    assert (
        client.get(
            f"/api/v1/environments/{env_b['id']}/servers", headers=headers
        ).status_code
        == 403
    )
    resp = _assign(client, headers, env_b["id"], system_id="abc123", roles=["control"])
    assert resp.status_code == 403
    resp = _static(client, headers, env_b["id"], hostname="bare-01", roles=["compute"])
    assert resp.status_code == 403
    resp = _remove(client, headers, env_b["id"], hostname="bare-01")
    assert resp.status_code == 403


def test_servers_endpoints_own_tenant_ok(client, two_tenants):
    env_a = two_tenants["env_a"]
    headers = two_tenants["operator_headers"]

    assert (
        client.get(
            f"/api/v1/environments/{env_a['id']}/servers", headers=headers
        ).status_code
        == 200
    )
    resp = _assign(client, headers, env_a["id"], system_id="abc123", roles=["control"])
    assert resp.status_code == 201, resp.text
    resp = _static(client, headers, env_a["id"], hostname="bare-01", roles=["compute"])
    assert resp.status_code == 201, resp.text
    resp = _remove(client, headers, env_a["id"], hostname="bare-01")
    assert resp.status_code == 200, resp.text
