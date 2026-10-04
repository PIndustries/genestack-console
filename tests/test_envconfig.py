"""Env config document service + endpoints (Phase 6, Milestone B)."""

from __future__ import annotations

import base64
import subprocess
import uuid
from pathlib import Path

import pytest
import yaml
from sqlalchemy import select

from app.db import SessionLocal
from app.models import EnvConfigVersion, Environment
from app.services import envconfig as envconfig_service
from app.services.crypto import FERNET_PREFIX, decrypt_secret

FULL_DOC = """\
provider: kubespray
deploy:
  ssh_host: deployer.example.com
  ssh_user: deploy
maas:
  url: http://maas.example.com:5240
  api_key: ck:tk:ts
servers:
  ctrl-1:
    system_id: abc123
    source: maas
    roles: [control, network]
    ip: 10.20.0.11
  cmp-1:
    system_id: def456
    source: maas
    roles: [compute, storage]
    ip: 10.20.0.21
network:
  gateway_domain: cluster.example.com
  metallb_pools:
    - name: default
      addresses: [10.20.0.200-10.20.0.220]
components:
  keystone: true
  glance: false
chart_versions:
  keystone: 2026.1.8+db238e7c3
helm_overrides:
  keystone:
    replicas: 3
  global:
    openstack:
      region: lab
kustomize_patches:
  keystone:
    - patch: |
        - op: replace
          path: /spec/replicas
          value: 3
"""

PARTIAL_DOC = """\
components:
  keystone: true
"""


def _suffix() -> str:
    return uuid.uuid4().hex[:8]


def _create_env(client, headers, **fields) -> dict:
    resp = client.post(
        "/api/v1/environments",
        headers=headers,
        json={"name": f"env-cfg-{_suffix()}", **fields},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _put_doc(client, headers, env_id, yaml_text=FULL_DOC):
    resp = client.put(
        f"/api/v1/environments/{env_id}/config",
        headers=headers,
        json={"yaml_text": yaml_text},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# Version CRUD via the API
# ---------------------------------------------------------------------------


def test_config_empty_state(client, admin_headers):
    env = _create_env(client, admin_headers)
    resp = client.get(f"/api/v1/environments/{env['id']}/config", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "version": None,
        "yaml": None,
        "supports_compare_and_swap": True,
    }

    versions = client.get(
        f"/api/v1/environments/{env['id']}/config/versions", headers=admin_headers
    )
    assert versions.status_code == 200
    assert versions.json() == []

    render = client.get(
        f"/api/v1/environments/{env['id']}/config/render", headers=admin_headers
    )
    assert render.status_code == 200
    assert render.json() == {"version": None, "files": {}}


def test_put_get_history_versions(client, admin_headers):
    env = _create_env(client, admin_headers)

    first = _put_doc(
        client, admin_headers, env["id"], "components:\n  keystone: true\n"
    )
    assert first["version"] == 1
    assert first["warnings"] == []

    second = _put_doc(client, admin_headers, env["id"], FULL_DOC)
    assert second["version"] == 2

    current = client.get(
        f"/api/v1/environments/{env['id']}/config", headers=admin_headers
    )
    assert current.status_code == 200
    assert current.json()["version"] == 2
    # A leftover maas block is dropped on save and omitted on read.
    got_doc = yaml.safe_load(current.json()["yaml"])
    expected_doc, _warnings = envconfig_service.parse_document(FULL_DOC)
    expected_doc.pop("maas", None)
    assert "maas" not in got_doc
    assert got_doc == expected_doc
    assert current.json()["created_by"]

    history = client.get(
        f"/api/v1/environments/{env['id']}/config/versions", headers=admin_headers
    )
    assert [row["version"] for row in history.json()] == [2, 1]

    v1 = client.get(
        f"/api/v1/environments/{env['id']}/config/versions/1", headers=admin_headers
    )
    assert v1.status_code == 200
    assert v1.json()["yaml"] == "components:\n  keystone: true\n"

    missing = client.get(
        f"/api/v1/environments/{env['id']}/config/versions/99", headers=admin_headers
    )
    assert missing.status_code == 404


def test_put_invalid_yaml_422(client, admin_headers):
    env = _create_env(client, admin_headers)
    resp = client.put(
        f"/api/v1/environments/{env['id']}/config",
        headers=admin_headers,
        json={"yaml_text": "key: [unclosed\n  bad indent"},
    )
    assert resp.status_code == 422

    # A scalar top level is not a valid document either
    resp = client.put(
        f"/api/v1/environments/{env['id']}/config",
        headers=admin_headers,
        json={"yaml_text": "just a string"},
    )
    assert resp.status_code == 422

    # Nothing was stored
    current = client.get(
        f"/api/v1/environments/{env['id']}/config", headers=admin_headers
    )
    assert current.json() == {
        "version": None,
        "yaml": None,
        "supports_compare_and_swap": True,
    }


def test_put_unknown_top_level_keys_warn_not_reject(client, admin_headers):
    env = _create_env(client, admin_headers)
    resp = client.put(
        f"/api/v1/environments/{env['id']}/config",
        headers=admin_headers,
        json={"yaml_text": "provider: kubespray\nbogus_key: 1\n"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["version"] == 1
    assert any("bogus_key" in w for w in body["warnings"])


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def test_render_full_doc_file_mapping(client, admin_headers):
    env = _create_env(client, admin_headers)
    _put_doc(client, admin_headers, env["id"])

    resp = client.get(
        f"/api/v1/environments/{env['id']}/config/render", headers=admin_headers
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["version"] == 1
    files = body["files"]

    # The env's auto-generated SSH key pair is always rendered for host access
    assert set(files) == {
        "provider",
        "inventory/inventory.yaml",
        "openstack-components.yaml",
        "helm-chart-versions.yaml",
        "helm-configs/keystone/console-rendered.yaml",
        "helm-configs/global_overrides/console-rendered.yaml",
        "kustomize/keystone/overlay/patches.yaml",
        "kustomize/keystone/overlay/kustomization.yaml",
        ".ssh/id_ed25519",
        ".ssh/id_ed25519.pub",
    }

    assert files["provider"] == "kubespray\n"

    inventory = yaml.safe_load(files["inventory/inventory.yaml"])
    children = inventory["all"]["children"]
    k8s_children = children["k8s_cluster"]["children"]
    assert k8s_children["openstack_control_plane"]["hosts"] == {"ctrl-1": {}}
    assert k8s_children["ovn_network_nodes"]["hosts"] == {"ctrl-1": {}}
    assert k8s_children["openstack_compute_nodes"]["hosts"] == {"cmp-1": {}}
    assert k8s_children["storage_nodes"]["children"]["longhorn_storage_nodes"][
        "hosts"
    ] == {"cmp-1": {}}
    # Every openstack role also joins the k8s cluster as a kube_node
    assert k8s_children["kube_node"]["hosts"] == {"ctrl-1": {}, "cmp-1": {}}
    assert children["k8s_cluster"]["vars"] == {"cluster_name": "cluster.local"}
    assert inventory["all"]["hosts"]["ctrl-1"]["ansible_host"] == "10.20.0.11"
    assert inventory["all"]["hosts"]["cmp-1"]["ansible_host"] == "10.20.0.21"

    components = yaml.safe_load(files["openstack-components.yaml"])
    assert components == {"components": {"keystone": True, "glance": False}}

    charts = yaml.safe_load(files["helm-chart-versions.yaml"])
    assert charts == {"charts": {"keystone": "2026.1.8+db238e7c3"}}

    assert yaml.safe_load(files["helm-configs/keystone/console-rendered.yaml"]) == {
        "replicas": 3
    }
    assert yaml.safe_load(
        files["helm-configs/global_overrides/console-rendered.yaml"]
    ) == {"openstack": {"region": "lab"}}

    patches = list(yaml.safe_load_all(files["kustomize/keystone/overlay/patches.yaml"]))
    assert patches == [{"patch": "- op: replace\n  path: /spec/replicas\n  value: 3\n"}]

    kustomization = yaml.safe_load(
        files["kustomize/keystone/overlay/kustomization.yaml"]
    )
    assert kustomization == {
        "apiVersion": "kustomize.config.k8s.io/v1beta1",
        "kind": "Kustomization",
        "resources": ["../base"],
        "patches": [{"path": "patches.yaml"}],
    }


def test_render_partial_doc_only_present_sections(client, admin_headers):
    env = _create_env(client, admin_headers)
    _put_doc(client, admin_headers, env["id"], PARTIAL_DOC)

    resp = client.get(
        f"/api/v1/environments/{env['id']}/config/render", headers=admin_headers
    )
    assert resp.status_code == 200, resp.text
    files = resp.json()["files"]
    assert set(files) == {
        "openstack-components.yaml",
        ".ssh/id_ed25519",
        ".ssh/id_ed25519.pub",
    }
    assert yaml.safe_load(files["openstack-components.yaml"]) == {
        "components": {"keystone": True}
    }


def test_talos_provider_renders_kube_ovn_writable_hostpaths():
    env = Environment(id="env-talos", name="env-talos")
    files = envconfig_service.render_to_files({"provider": "talos"}, env)
    path = "helm-configs/kube-ovn/console-rendered.yaml"
    assert path in files
    body = yaml.safe_load(files[path])
    assert body["OPENVSWITCH_DIR"] == "/var/lib/openvswitch"
    assert body["OVN_DIR"] == "/var/lib/ovn"
    assert body["DISABLE_MODULES_MANAGEMENT"] is True
    assert body["cni_conf"]["MOUNT_LOCAL_BIN_DIR"] is False


def test_render_to_files_service_level_partial():
    """Absent sections produce no files (never wipe existing ones)."""
    env = Environment(id="env-x", name="env-x")
    files = envconfig_service.render_to_files(
        {
            "servers": {
                "sid1": {"hostname": "h1", "roles": ["control"], "ip": "10.0.0.1"}
            }
        },
        env,
    )
    assert set(files) == {"inventory/inventory.yaml"}
    inventory = yaml.safe_load(files["inventory/inventory.yaml"])
    children = inventory["all"]["children"]
    k8s_children = children["k8s_cluster"]["children"]
    assert k8s_children["openstack_control_plane"]["hosts"] == {"h1": {}}
    assert k8s_children["kube_node"]["hosts"] == {"h1": {}}
    assert "openstack_compute_nodes" not in k8s_children


def test_render_inventory_matches_genestack_group_shape():
    """All six roles land in the genestack/kubespray group layout."""
    env = Environment(id="env-x", name="env-x")
    files = envconfig_service.render_to_files(
        {
            "servers": {
                "k8s-01": {"roles": ["k8s_control_plane"], "ip": "10.0.0.1"},
                "etcd-01": {"roles": ["etcd"], "ip": "10.0.0.2"},
                "ctrl-1": {"roles": ["control"], "ip": "10.0.0.3"},
                "cmp-1": {"roles": ["compute"], "ip": "10.0.0.4"},
                "net-1": {"roles": ["network"], "ip": "10.0.0.5"},
                "sto-1": {"roles": ["storage"], "ip": "10.0.0.6"},
            }
        },
        env,
    )
    inventory = yaml.safe_load(files["inventory/inventory.yaml"])
    children = inventory["all"]["children"]

    # All groups (kubespray + genestack) nest under k8s_cluster.children,
    # matching ansible/inventory/genestack/inventory.yaml.example
    assert set(children) == {"k8s_cluster"}
    k8s = children["k8s_cluster"]
    assert k8s["vars"] == {"cluster_name": "cluster.local"}
    assert set(k8s["children"]) == {
        "kube_control_plane",
        "etcd",
        "kube_node",
        "openstack_control_plane",
        "openstack_compute_nodes",
        "ovn_network_nodes",
        "storage_nodes",
    }
    assert k8s["children"]["openstack_control_plane"]["hosts"] == {"ctrl-1": {}}
    assert k8s["children"]["openstack_compute_nodes"]["hosts"] == {"cmp-1": {}}
    assert k8s["children"]["ovn_network_nodes"]["hosts"] == {"net-1": {}}
    assert k8s["children"]["storage_nodes"]["children"]["longhorn_storage_nodes"][
        "hosts"
    ] == {"sto-1": {}}

    assert k8s["children"]["kube_control_plane"]["hosts"] == {"k8s-01": {}}
    assert k8s["children"]["etcd"]["hosts"] == {"etcd-01": {}}
    # k8s_control_plane/control/compute/network/storage all join kube_node;
    # etcd does not (matches inventory.yaml.example)
    assert k8s["children"]["kube_node"]["hosts"] == {
        "k8s-01": {},
        "ctrl-1": {},
        "cmp-1": {},
        "net-1": {},
        "sto-1": {},
    }


def test_render_inventory_no_k8s_cluster_without_k8s_roles():
    """No k8s roles -> no k8s_cluster group (and no cluster_name vars)."""
    env = Environment(id="env-x", name="env-x")
    files = envconfig_service.render_to_files(
        {"servers": {"etcd-01": {"roles": ["etcd"], "ip": "10.0.0.2"}}},
        env,
    )
    inventory = yaml.safe_load(files["inventory/inventory.yaml"])
    # etcd is itself a kubespray group, so k8s_cluster still exists here
    assert "k8s_cluster" in inventory["all"]["children"]

    files = envconfig_service.render_to_files(
        {"servers": {"bare-01": {"roles": [], "ip": "10.0.0.9"}}},
        env,
    )
    inventory = yaml.safe_load(files["inventory/inventory.yaml"])
    assert inventory["all"]["children"] == {}


def test_render_inventory_excludes_deployer():
    """The env's deploy host never lands in the rendered inventory."""
    env = Environment(
        id="env-x",
        name="env-x",
        deployer_ssh_host="deployer.example.com",
        deployer_ssh_user="deploy",
    )
    files = envconfig_service.render_to_files(
        {"servers": {"ctrl-1": {"roles": ["control"], "ip": "10.0.0.3"}}},
        env,
    )
    inventory = yaml.safe_load(files["inventory/inventory.yaml"])
    assert "deployer.example.com" not in inventory["all"]["hosts"]
    assert "deployers" not in inventory["all"]["children"]


def test_render_rejects_unsafe_service_names():
    env = Environment(id="env-x", name="env-x")
    with pytest.raises(envconfig_service.ConfigValidationError):
        envconfig_service.render_to_files(
            {"helm_overrides": {"../escape": {"a": 1}}}, env
        )


# ---------------------------------------------------------------------------
# group_vars section (ansible inventory group_vars)
# ---------------------------------------------------------------------------

GROUP_VARS_DOC = """\
group_vars:
  all:
    cloud_provider: openstack
  k8s_cluster:
    kube_version: v1.34.3
    kube_ovn_iface: bond0
"""


def test_render_group_vars_paths_and_content():
    """A group_vars-only doc renders one console-rendered.yml per group."""
    env = Environment(id="env-x", name="env-x")
    files = envconfig_service.render_to_files(yaml.safe_load(GROUP_VARS_DOC), env)
    # Partial doc: only the group_vars section produces files
    assert set(files) == {
        "inventory/group_vars/all/console-rendered.yml",
        "inventory/group_vars/k8s_cluster/console-rendered.yml",
    }
    assert yaml.safe_load(files["inventory/group_vars/all/console-rendered.yml"]) == {
        "cloud_provider": "openstack"
    }
    assert yaml.safe_load(
        files["inventory/group_vars/k8s_cluster/console-rendered.yml"]
    ) == {
        "kube_version": "v1.34.3",
        "kube_ovn_iface": "bond0",
    }


def test_parse_document_rejects_invalid_group_vars():
    with pytest.raises(envconfig_service.ConfigValidationError):
        envconfig_service.parse_document("group_vars: [all]\n")
    with pytest.raises(envconfig_service.ConfigValidationError):
        envconfig_service.parse_document("group_vars:\n  'Bad Group':\n    a: 1\n")
    with pytest.raises(envconfig_service.ConfigValidationError):
        envconfig_service.parse_document("group_vars:\n  9lives:\n    a: 1\n")
    with pytest.raises(envconfig_service.ConfigValidationError):
        envconfig_service.parse_document("group_vars:\n  all: not-a-mapping\n")


def test_render_rejects_invalid_group_vars_names():
    env = Environment(id="env-x", name="env-x")
    with pytest.raises(envconfig_service.ConfigValidationError):
        envconfig_service.render_to_files({"group_vars": {"../escape": {"a": 1}}}, env)
    with pytest.raises(envconfig_service.ConfigValidationError):
        envconfig_service.render_to_files({"group_vars": {"all": "scalar"}}, env)


def test_render_endpoint_includes_group_vars_files(client, admin_headers):
    env = _create_env(client, admin_headers)
    _put_doc(client, admin_headers, env["id"], GROUP_VARS_DOC)

    resp = client.get(
        f"/api/v1/environments/{env['id']}/config/render", headers=admin_headers
    )
    assert resp.status_code == 200, resp.text
    files = resp.json()["files"]
    assert yaml.safe_load(files["inventory/group_vars/all/console-rendered.yml"]) == {
        "cloud_provider": "openstack"
    }
    assert yaml.safe_load(
        files["inventory/group_vars/k8s_cluster/console-rendered.yml"]
    ) == {
        "kube_version": "v1.34.3",
        "kube_ovn_iface": "bond0",
    }


# ---------------------------------------------------------------------------
# Storage backends: storage-ceph/storage-cinder roles + storage: section
# ---------------------------------------------------------------------------


def test_render_inventory_storage_sub_roles():
    """storage-ceph/storage-cinder land in their storage_nodes children + kube_node."""
    env = Environment(id="env-x", name="env-x")
    files = envconfig_service.render_to_files(
        {
            "servers": {
                "sto-1": {"roles": ["storage"], "ip": "10.0.0.6"},
                "ceph-1": {"roles": ["storage-ceph"], "ip": "10.0.0.7"},
                "cin-1": {"roles": ["storage-cinder"], "ip": "10.0.0.8"},
            }
        },
        env,
    )
    inventory = yaml.safe_load(files["inventory/inventory.yaml"])
    k8s_children = inventory["all"]["children"]["k8s_cluster"]["children"]
    storage_children = k8s_children["storage_nodes"]["children"]
    assert storage_children["longhorn_storage_nodes"]["hosts"] == {"sto-1": {}}
    assert storage_children["ceph_storage_nodes"]["hosts"] == {"ceph-1": {}}
    assert storage_children["cinder_storage_nodes"]["hosts"] == {"cin-1": {}}
    # All storage sub-roles also join the k8s cluster as kube_node
    assert k8s_children["kube_node"]["hosts"] == {
        "sto-1": {},
        "ceph-1": {},
        "cin-1": {},
    }


def test_parse_document_accepts_storage_sub_roles():
    doc, _warnings = envconfig_service.parse_document(
        "servers:\n  ceph-1:\n    roles: [storage-ceph, storage-cinder]\n"
    )
    assert doc["servers"]["ceph-1"]["roles"] == ["storage-ceph", "storage-cinder"]


STORAGE_DOC = """\
storage:
  cinder_backend_name: netapp-iscsi-1
  cinder_worker_name: netapp
  ceph:
    enabled: true
"""


def test_render_storage_cinder_vars_to_group_vars():
    """storage.cinder_* vars render the cinder_storage_nodes group vars file."""
    env = Environment(id="env-x", name="env-x")
    files = envconfig_service.render_to_files(yaml.safe_load(STORAGE_DOC), env)
    assert set(files) == {
        "inventory/group_vars/cinder_storage_nodes/console-rendered.yml",
        # storage.ceph: {enabled: true} renders the rook kustomize overlay
        "kustomize/rook-ceph/overlay/kustomization.yaml",
    }
    assert yaml.safe_load(
        files["inventory/group_vars/cinder_storage_nodes/console-rendered.yml"]
    ) == {
        "cinder_backend_name": "netapp-iscsi-1",
        "cinder_worker_name": "netapp",
    }
    assert yaml.safe_load(files["kustomize/rook-ceph/overlay/kustomization.yaml"]) == {
        "apiVersion": "kustomize.config.k8s.io/v1beta1",
        "kind": "Kustomization",
        "resources": [
            "../rook-operator/base",
            "../rook-defaults/base",
            "../rook-cluster/base",
        ],
    }


def test_render_storage_merges_with_explicit_group_vars():
    """Explicit group_vars.cinder_storage_nodes entries win over storage.cinder_*."""
    env = Environment(id="env-x", name="env-x")
    doc = yaml.safe_load(STORAGE_DOC)
    doc["group_vars"] = {
        "cinder_storage_nodes": {"cinder_backend_name": "netapp-nfs-1", "extra": 1},
        "all": {"cloud_provider": "openstack"},
    }
    files = envconfig_service.render_to_files(doc, env)
    assert yaml.safe_load(
        files["inventory/group_vars/cinder_storage_nodes/console-rendered.yml"]
    ) == {
        "cinder_backend_name": "netapp-nfs-1",
        "cinder_worker_name": "netapp",
        "extra": 1,
    }
    assert yaml.safe_load(files["inventory/group_vars/all/console-rendered.yml"]) == {
        "cloud_provider": "openstack"
    }


def test_parse_document_rejects_invalid_storage():
    with pytest.raises(envconfig_service.ConfigValidationError):
        envconfig_service.parse_document("storage: [cinder]\n")
    with pytest.raises(envconfig_service.ConfigValidationError):
        envconfig_service.parse_document("storage:\n  ceph: true\n")


def test_put_unknown_storage_keys_warn_not_reject(client, admin_headers):
    env = _create_env(client, admin_headers)
    resp = client.put(
        f"/api/v1/environments/{env['id']}/config",
        headers=admin_headers,
        json={
            "yaml_text": "storage:\n  cinder_backend_name: netapp-iscsi-1\n  bogus_store: 1\n"
        },
    )
    assert resp.status_code == 201, resp.text
    assert any("bogus_store" in w for w in resp.json()["warnings"])

    # Known storage keys do not warn
    resp = client.put(
        f"/api/v1/environments/{env['id']}/config",
        headers=admin_headers,
        json={"yaml_text": STORAGE_DOC},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["warnings"] == []


def test_render_endpoint_includes_storage_files(client, admin_headers):
    env = _create_env(client, admin_headers)
    _put_doc(client, admin_headers, env["id"], STORAGE_DOC)

    resp = client.get(
        f"/api/v1/environments/{env['id']}/config/render", headers=admin_headers
    )
    assert resp.status_code == 200, resp.text
    files = resp.json()["files"]
    assert yaml.safe_load(
        files["inventory/group_vars/cinder_storage_nodes/console-rendered.yml"]
    ) == {
        "cinder_backend_name": "netapp-iscsi-1",
        "cinder_worker_name": "netapp",
    }
    # The enabled ceph section renders the rook kustomize overlay too
    assert yaml.safe_load(files["kustomize/rook-ceph/overlay/kustomization.yaml"]) == {
        "apiVersion": "kustomize.config.k8s.io/v1beta1",
        "kind": "Kustomization",
        "resources": [
            "../rook-operator/base",
            "../rook-defaults/base",
            "../rook-cluster/base",
        ],
    }


# ---------------------------------------------------------------------------
# Servers section: normalization + validation
# ---------------------------------------------------------------------------

OLD_SERVERS_DOC = """\
servers:
  abc123:
    hostname: ctrl-1
    roles: [control, network]
    ip: 10.20.0.11
"""


def test_parse_document_normalizes_legacy_servers():
    doc, _warnings = envconfig_service.parse_document(OLD_SERVERS_DOC)
    servers = doc["servers"]
    assert list(servers) == ["ctrl-1"]
    entry = servers["ctrl-1"]
    assert entry["system_id"] == "abc123"
    assert entry["source"] == "maas"
    assert entry["roles"] == ["control", "network"]
    assert entry["ip"] == "10.20.0.11"
    assert entry.get("ssh_user") in (None, "")


def test_parse_document_infers_source_when_absent():
    doc, _warnings = envconfig_service.parse_document(
        "servers:\n"
        "  ctrl-1:\n"
        "    system_id: abc123\n"
        "    roles: [control]\n"
        "  bare-01:\n"
        "    ip: 10.30.0.5\n"
        "    roles: [compute]\n"
    )
    assert doc["servers"]["ctrl-1"]["source"] == "maas"
    assert doc["servers"]["bare-01"]["source"] == "static"
    assert not doc["servers"]["bare-01"].get("system_id")


def test_parse_document_rejects_invalid_hostname():
    with pytest.raises(envconfig_service.ConfigValidationError):
        envconfig_service.parse_document(
            "servers:\n  'bad host!':\n    roles: [control]\n"
        )


def test_parse_document_rejects_unknown_role():
    with pytest.raises(envconfig_service.ConfigValidationError):
        envconfig_service.parse_document("servers:\n  ctrl-1:\n    roles: [bogus]\n")


def test_parse_document_rejects_unknown_source():
    with pytest.raises(envconfig_service.ConfigValidationError):
        envconfig_service.parse_document(
            "servers:\n  ctrl-1:\n    source: webhook\n    roles: [control]\n"
        )


def test_legacy_servers_doc_normalized_via_api(client, admin_headers):
    env = _create_env(client, admin_headers)
    _put_doc(client, admin_headers, env["id"], OLD_SERVERS_DOC)

    render = client.get(
        f"/api/v1/environments/{env['id']}/config/render", headers=admin_headers
    )
    inventory = yaml.safe_load(render.json()["files"]["inventory/inventory.yaml"])
    children = inventory["all"]["children"]
    k8s_children = children["k8s_cluster"]["children"]
    assert k8s_children["openstack_control_plane"]["hosts"] == {"ctrl-1": {}}
    assert inventory["all"]["hosts"]["ctrl-1"]["ansible_host"] == "10.20.0.11"

    servers = client.get(
        f"/api/v1/environments/{env['id']}/servers", headers=admin_headers
    )
    entry = {s["hostname"]: s for s in servers.json()["servers"]}["ctrl-1"]
    assert entry["system_id"] == "abc123"
    assert entry["source"] == "maas"
    assert entry["assigned"] is True


def test_render_to_files_static_server_ansible_user():
    env = Environment(id="env-x", name="env-x")
    files = envconfig_service.render_to_files(
        {
            "servers": {
                "bare-01": {
                    "system_id": None,
                    "source": "static",
                    "roles": ["compute"],
                    "ip": "10.30.0.5",
                    "ssh_user": None,
                }
            }
        },
        env,
    )
    inventory = yaml.safe_load(files["inventory/inventory.yaml"])
    host = inventory["all"]["hosts"]["bare-01"]
    assert host["ansible_host"] == "10.30.0.5"
    assert host["ansible_user"] == "ubuntu"
    assert inventory["all"]["children"]["k8s_cluster"]["children"][
        "openstack_compute_nodes"
    ]["hosts"] == {"bare-01": {}}


# ---------------------------------------------------------------------------
# Doc-derived environment (network -> setup-infrastructure.sh env vars)
# ---------------------------------------------------------------------------


def test_doc_env_extracts_gateway_domain():
    assert envconfig_service.doc_env(
        {"network": {"gateway_domain": "cluster.example.com"}}
    ) == {"GATEWAY_DOMAIN": "cluster.example.com"}
    assert envconfig_service.doc_env({"network": {"metallb_pools": []}}) == {}
    assert envconfig_service.doc_env({}) == {}


def test_doc_env_maps_all_network_keys():
    doc = {
        "network": {
            "gateway_domain": "cluster.example.com",
            "acme_email": "ops@example.com",
            "hyperconverged": True,
            "container_interface": "bond0",
            "compute_interface": "bond1",
        }
    }
    assert envconfig_service.doc_env(doc) == {
        "GATEWAY_DOMAIN": "cluster.example.com",
        "ACME_EMAIL": "ops@example.com",
        "HYPERCONVERGED": "true",
        "CONTAINER_INTERFACE": "bond0",
        "COMPUTE_INTERFACE": "bond1",
    }


def test_doc_env_omits_absent_network_keys():
    assert envconfig_service.doc_env(
        {"network": {"acme_email": "ops@example.com"}}
    ) == {"ACME_EMAIL": "ops@example.com"}
    assert envconfig_service.doc_env({"network": {}}) == {}
    assert envconfig_service.doc_env({"network": None}) == {}


def test_doc_env_hyperconverged_bool_mapping():
    assert envconfig_service.doc_env({"network": {"hyperconverged": False}}) == {
        "HYPERCONVERGED": "false"
    }
    assert envconfig_service.doc_env({"network": {"hyperconverged": True}}) == {
        "HYPERCONVERGED": "true"
    }


def test_doc_env_ovn_mapping_prefixes_keys():
    doc = {
        "network": {
            "ovn": {
                "external_interface": "bond0.126",
                "vlans": ["vlan10:bond0:10:1500", "vlan20:bond0:20:1500"],
                "external_vlan_id": 126,
            }
        }
    }
    assert envconfig_service.doc_env(doc) == {
        "OVN_EXTERNAL_INTERFACE": "bond0.126",
        "OVN_VLANS": "vlan10:bond0:10:1500,vlan20:bond0:20:1500",
        "OVN_EXTERNAL_VLAN_ID": "126",
    }
    assert envconfig_service.doc_env({"network": {"ovn": {}}}) == {}


def test_doc_env_empty_ovn_external_does_not_become_compute_interface():
    """compute_interface is not an OVN br-ex fallback.

    Dual-NIC OVH Talos lost :6443/:50000 when public eno1np0 was enslaved
    into br-ex via COMPUTE_INTERFACE. Leave OVN_EXTERNAL_INTERFACE unset.
    """
    env = envconfig_service.doc_env(
        {
            "network": {
                "container_interface": "enp3934127.100",
                "compute_interface": "eno1np0",
                "ovn": {},
            }
        }
    )
    assert env["CONTAINER_INTERFACE"] == "enp3934127.100"
    assert env["COMPUTE_INTERFACE"] == "eno1np0"
    assert "OVN_EXTERNAL_INTERFACE" not in env


def test_setup_infrastructure_empty_ovn_external_does_not_become_compute_interface():
    """setup-infrastructure.sh must not plug COMPUTE_INTERFACE into br-ex.

    That script lives in the Genestack checkout. This repository does not
    vendor it. The test runs only when a copy is present at bin/.
    """
    script = Path(__file__).resolve().parents[1] / "bin" / "setup-infrastructure.sh"
    if not script.is_file():
        pytest.skip("bin/setup-infrastructure.sh is not in this repository")
    text = script.read_text(encoding="utf-8")
    collapsed = text.replace(" ", "")
    assert "OVN_EXTERNAL_INTERFACE=${COMPUTE_INTERFACE}" not in collapsed
    assert 'OVN_EXTERNAL_INTERFACE="${COMPUTE_INTERFACE}"' not in collapsed
    assert "ovn.openstack.org/ports-" in text
    assert "OPENVSWITCH_DIR: /var/lib/openvswitch" in text

    start = text.index('if [ -z "${OVN_EXTERNAL_INTERFACE}" ]; then')
    end = text.index(
        'if [ "${COMPUTE_INTERFACE}" = "${CONTAINER_INTERFACE}" ]; then', start
    )
    block = text[start:end]
    proc = subprocess.run(
        [
            "bash",
            "-c",
            "ip() { return 1; }\n"
            "COMPUTE_INTERFACE=eno1np0\n"
            "OVN_EXTERNAL_INTERFACE=\n"
            f"{block}"
            'printf "OVN=%s\\n" "${OVN_EXTERNAL_INTERFACE-}"\n',
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert proc.stdout.rstrip().endswith("OVN=")
    combined = proc.stdout + proc.stderr
    assert "leaving br-ex with no physical port" in combined


def test_put_unknown_network_keys_warn_not_reject(client, admin_headers):
    env = _create_env(client, admin_headers)
    resp = client.put(
        f"/api/v1/environments/{env['id']}/config",
        headers=admin_headers,
        json={
            "yaml_text": "network:\n  gateway_domain: cluster.example.com\n  bogus_net: 1\n"
        },
    )
    assert resp.status_code == 201, resp.text
    assert any("bogus_net" in w for w in resp.json()["warnings"])
    # Known network keys do not warn
    resp = client.put(
        f"/api/v1/environments/{env['id']}/config",
        headers=admin_headers,
        json={
            "yaml_text": (
                "network:\n  gateway_domain: cluster.example.com\n"
                "  acme_email: ops@example.com\n  hyperconverged: true\n"
                "  container_interface: bond0\n  compute_interface: bond1\n"
                "  ovn:\n    external_interface: bond0.126\n"
                "  metallb_pools: []\n  ovn_bridge_mappings: []\n"
            )
        },
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["warnings"] == []


def test_gateway_domain_flows_to_job_env(client, admin_headers, monkeypatch):
    """network.gateway_domain lands in the job subprocess env as GATEWAY_DOMAIN."""
    from app.services import genestack_bridge as bridge

    env = _create_env(client, admin_headers)
    _put_doc(
        client,
        admin_headers,
        env["id"],
        "network:\n  gateway_domain: cluster.example.com\n",
    )

    captured: list[dict] = []

    def fake_run_command(cmd, **kwargs):
        captured.append(kwargs.get("extra_env") or {})
        return {"ok": True, "dry_run": True, "returncode": None, "message": "dry"}

    monkeypatch.setattr(bridge, "run_command", fake_run_command)

    resp = client.post(
        f"/api/v1/environments/{env['id']}/jobs",
        headers=admin_headers,
        json={
            "operation": "genestack.pipeline.run",
            "params": {"stage": "core"},
            "run_sync": True,
        },
    )
    assert resp.status_code in (200, 201), resp.text
    assert captured, "pipeline ran no commands"
    assert all(
        extra.get("GATEWAY_DOMAIN") == "cluster.example.com" for extra in captured
    )


# ---------------------------------------------------------------------------
# secrets: section — encrypted at rest, masked on read, rendered genestack-native
# ---------------------------------------------------------------------------

SECRETS_DOC = """\
secrets:
  netapp-cinder-backend:
    namespace: openstack
    data:
      username: admin
      password: s3cret
  api-token:
    data:
      token: abc123
"""


def _stored_yaml(env_id) -> str:
    db = SessionLocal()
    try:
        row = db.scalar(
            select(EnvConfigVersion)
            .where(EnvConfigVersion.environment_id == env_id)
            .order_by(EnvConfigVersion.version.desc())
            .limit(1)
        )
        return row.yaml_text
    finally:
        db.close()


def _stored_secrets(env_id) -> dict:
    doc, _warnings = envconfig_service.parse_document(_stored_yaml(env_id))
    return doc["secrets"]


def _b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


def test_put_secrets_encrypted_at_rest(client, admin_headers):
    env = _create_env(client, admin_headers)
    _put_doc(client, admin_headers, env["id"], SECRETS_DOC)

    stored = _stored_yaml(env["id"])
    assert FERNET_PREFIX in stored
    # Plaintext never touches the DB
    assert "s3cret" not in stored
    assert "abc123" not in stored

    secrets = _stored_secrets(env["id"])
    entry = secrets["netapp-cinder-backend"]
    assert entry["namespace"] == "openstack"
    assert entry["data"]["username"].startswith(FERNET_PREFIX)
    assert decrypt_secret(entry["data"]["password"]) == "s3cret"
    assert decrypt_secret(secrets["api-token"]["data"]["token"]) == "abc123"


def test_get_config_masks_secret_values(client, admin_headers):
    env = _create_env(client, admin_headers)
    _put_doc(client, admin_headers, env["id"], SECRETS_DOC)

    current = client.get(
        f"/api/v1/environments/{env['id']}/config", headers=admin_headers
    )
    assert current.status_code == 200
    masked = current.json()["yaml"]
    assert "s3cret" not in masked
    assert FERNET_PREFIX not in masked
    doc = yaml.safe_load(masked)
    assert doc["secrets"]["netapp-cinder-backend"]["data"] == {
        "username": envconfig_service.SECRET_MASK,
        "password": envconfig_service.SECRET_MASK,
    }
    assert doc["secrets"]["api-token"]["data"] == {
        "token": envconfig_service.SECRET_MASK
    }

    # Historical versions are masked too
    v1 = client.get(
        f"/api/v1/environments/{env['id']}/config/versions/1", headers=admin_headers
    )
    assert "s3cret" not in v1.json()["yaml"]
    assert FERNET_PREFIX not in v1.json()["yaml"]


def test_reput_masked_sentinel_keeps_stored_values(client, admin_headers):
    env = _create_env(client, admin_headers)
    _put_doc(client, admin_headers, env["id"], SECRETS_DOC)

    masked = client.get(
        f"/api/v1/environments/{env['id']}/config", headers=admin_headers
    ).json()["yaml"]
    resp = client.put(
        f"/api/v1/environments/{env['id']}/config",
        headers=admin_headers,
        json={"yaml_text": masked},
    )
    assert resp.status_code == 201, resp.text

    # Sentinel round-trip leaves the original values in place
    secrets = _stored_secrets(env["id"])
    assert (
        decrypt_secret(secrets["netapp-cinder-backend"]["data"]["password"]) == "s3cret"
    )
    assert decrypt_secret(secrets["api-token"]["data"]["token"]) == "abc123"

    # A new value alongside sentinels changes only that key
    doc = yaml.safe_load(masked)
    doc["secrets"]["api-token"]["data"]["token"] = "rotated456"
    resp = client.put(
        f"/api/v1/environments/{env['id']}/config",
        headers=admin_headers,
        json={"yaml_text": yaml.safe_dump(doc)},
    )
    assert resp.status_code == 201, resp.text
    secrets = _stored_secrets(env["id"])
    assert decrypt_secret(secrets["api-token"]["data"]["token"]) == "rotated456"
    assert (
        decrypt_secret(secrets["netapp-cinder-backend"]["data"]["password"]) == "s3cret"
    )


def test_render_secrets_kubesecrets_yaml(client, admin_headers):
    """Stored (encrypted) values render as base64 plaintext Secret manifests."""
    env = _create_env(client, admin_headers)
    _put_doc(client, admin_headers, env["id"], SECRETS_DOC)

    db = SessionLocal()
    try:
        row = db.get(Environment, env["id"])
        current = envconfig_service.get_current(db, row)
        files = envconfig_service.render_to_files(current[0], row)
    finally:
        db.close()

    text = files["kubesecrets.yaml"]
    assert text.startswith("---\n")  # create-secrets.sh shape: every doc led by ---
    docs = list(yaml.safe_load_all(text))
    assert [d["metadata"]["name"] for d in docs] == [
        "netapp-cinder-backend",
        "api-token",
    ]
    for doc in docs:
        assert doc["apiVersion"] == "v1"
        assert doc["kind"] == "Secret"
        assert doc["type"] == "Opaque"
    assert docs[0]["metadata"]["namespace"] == "openstack"
    # namespace defaults to "openstack" when omitted
    assert docs[1]["metadata"]["namespace"] == "openstack"
    b64 = _b64
    assert docs[0]["data"] == {"username": b64("admin"), "password": b64("s3cret")}
    assert docs[1]["data"] == {"token": b64("abc123")}
    assert base64.b64decode(docs[0]["data"]["password"]).decode() == "s3cret"


def test_render_preview_masks_kubesecrets(client, admin_headers):
    env = _create_env(client, admin_headers)
    _put_doc(client, admin_headers, env["id"], SECRETS_DOC)

    resp = client.get(
        f"/api/v1/environments/{env['id']}/config/render", headers=admin_headers
    )
    assert resp.status_code == 200, resp.text
    text = resp.json()["files"]["kubesecrets.yaml"]
    assert "s3cret" not in text
    assert base64.b64encode(b"s3cret").decode() not in text
    docs = list(yaml.safe_load_all(text))
    assert docs[0]["data"] == {
        "username": envconfig_service.SECRET_MASK,
        "password": envconfig_service.SECRET_MASK,
    }


def test_render_to_files_plaintext_secrets_service_level():
    """Service-level render works on a plaintext doc (no encryption involved)."""
    env = Environment(id="env-x", name="env-x")
    files = envconfig_service.render_to_files(
        {"secrets": {"db-pw": {"data": {"password": "plain"}}}}, env
    )
    docs = list(yaml.safe_load_all(files["kubesecrets.yaml"]))
    assert docs == [
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": "db-pw", "namespace": "openstack"},
            "type": "Opaque",
            "data": {"password": base64.b64encode(b"plain").decode()},
        }
    ]


def test_put_secrets_validation_errors(client, admin_headers):
    env = _create_env(client, admin_headers)
    base = f"/api/v1/environments/{env['id']}/config"

    def put(yaml_text):
        return client.put(base, headers=admin_headers, json={"yaml_text": yaml_text})

    # secrets must be a mapping
    assert put("secrets: [not, a, mapping]\n").status_code == 422
    # secret names are dns-1123
    assert put("secrets:\n  Bad_Name:\n    data: {a: b}\n").status_code == 422
    assert put("secrets:\n  '-leading-dash':\n    data: {a: b}\n").status_code == 422
    # entry and data must be mappings of string values
    assert put("secrets:\n  ok-name: not-a-mapping\n").status_code == 422
    assert put("secrets:\n  ok-name:\n    data: not-a-mapping\n").status_code == 422
    assert put("secrets:\n  ok-name:\n    data:\n      num: 42\n").status_code == 422
    assert (
        put("secrets:\n  ok-name:\n    namespace: 42\n    data: {a: b}\n").status_code
        == 422
    )

    # Nothing was stored
    current = client.get(base, headers=admin_headers)
    assert current.json() == {
        "version": None,
        "yaml": None,
        "supports_compare_and_swap": True,
    }


def test_put_secrets_unknown_entry_key_warns(client, admin_headers):
    env = _create_env(client, admin_headers)
    resp = client.put(
        f"/api/v1/environments/{env['id']}/config",
        headers=admin_headers,
        json={"yaml_text": "secrets:\n  ok-name:\n    bogus: 1\n    data: {a: b}\n"},
    )
    assert resp.status_code == 201, resp.text
    assert any("bogus" in w for w in resp.json()["warnings"])


# ---------------------------------------------------------------------------
# Tenant scoping
# ---------------------------------------------------------------------------


@pytest.fixture
def two_tenants(client, admin_headers):
    """Two tenants with one env each, plus an operator session user in tenant A."""
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
    viewer = _create_user(
        client,
        admin_headers,
        memberships=[{"tenant_id": tenant_a["id"], "role": "viewer"}],
    )
    return {
        "env_a": env_a,
        "env_b": env_b,
        "operator_headers": _login_headers(client, operator["username"]),
        "viewer_headers": _login_headers(client, viewer["username"]),
    }


def test_config_endpoints_cross_tenant_403(client, two_tenants):
    env_b = two_tenants["env_b"]
    headers = two_tenants["operator_headers"]
    base = f"/api/v1/environments/{env_b['id']}"

    assert client.get(f"{base}/config", headers=headers).status_code == 403
    assert (
        client.put(
            f"{base}/config", headers=headers, json={"yaml_text": "a: 1"}
        ).status_code
        == 403
    )
    assert client.get(f"{base}/config/versions", headers=headers).status_code == 403
    assert client.get(f"{base}/config/versions/1", headers=headers).status_code == 403
    assert client.get(f"{base}/config/render", headers=headers).status_code == 403


def test_config_put_requires_operator_role(client, two_tenants):
    env_a = two_tenants["env_a"]
    viewer = two_tenants["viewer_headers"]
    operator = two_tenants["operator_headers"]

    # Viewer can read but not write
    assert (
        client.get(
            f"/api/v1/environments/{env_a['id']}/config", headers=viewer
        ).status_code
        == 200
    )
    resp = client.put(
        f"/api/v1/environments/{env_a['id']}/config",
        headers=viewer,
        json={"yaml_text": "provider: kubespray\n"},
    )
    assert resp.status_code == 403

    resp = client.put(
        f"/api/v1/environments/{env_a['id']}/config",
        headers=operator,
        json={"yaml_text": "provider: kubespray\n"},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["version"] == 1


def _stored_doc(env_id) -> dict:
    doc, _warnings = envconfig_service.parse_document(_stored_yaml(env_id))
    return doc


# ---------------------------------------------------------------------------
# A leftover maas block is accepted and not stored
# ---------------------------------------------------------------------------

MAAS_DOC = """\
provider: kubespray
maas:
  url: http://maas.example.com:5240
  api_key: consumer:token:secret
"""


def test_leftover_maas_block_is_dropped(client, admin_headers):
    env = _create_env(client, admin_headers)
    _put_doc(client, admin_headers, env["id"], MAAS_DOC)

    stored = _stored_yaml(env["id"])
    assert "consumer:token:secret" not in stored
    assert "maas:" not in stored

    current = client.get(
        f"/api/v1/environments/{env['id']}/config", headers=admin_headers
    )
    assert current.status_code == 200
    doc = yaml.safe_load(current.json()["yaml"])
    assert "maas" not in doc
    assert doc["provider"] == "kubespray"


# ---------------------------------------------------------------------------
# deploy.ssh_password — encrypted at rest, masked on read
# ---------------------------------------------------------------------------

DEPLOY_PASS_DOC = """\
provider: kubespray
deploy:
  ssh_host: deployer.example.com
  ssh_user: deploy
  ssh_password: node:pass:secret
"""


def test_put_deploy_ssh_password_encrypted_at_rest(client, admin_headers):
    env = _create_env(client, admin_headers)
    _put_doc(client, admin_headers, env["id"], DEPLOY_PASS_DOC)

    stored = _stored_yaml(env["id"])
    assert "node:pass:secret" not in stored
    deploy = _stored_doc(env["id"])["deploy"]
    assert deploy["ssh_password"].startswith(FERNET_PREFIX)
    assert decrypt_secret(deploy["ssh_password"]) == "node:pass:secret"


def test_get_config_masks_deploy_ssh_password(client, admin_headers):
    env = _create_env(client, admin_headers)
    _put_doc(client, admin_headers, env["id"], DEPLOY_PASS_DOC)

    current = client.get(
        f"/api/v1/environments/{env['id']}/config", headers=admin_headers
    )
    assert current.status_code == 200
    masked = current.json()["yaml"]
    assert "node:pass:secret" not in masked
    assert FERNET_PREFIX not in masked
    doc = yaml.safe_load(masked)
    assert doc["deploy"]["ssh_password"] == envconfig_service.SECRET_MASK
    assert doc["deploy"]["ssh_user"] == "deploy"

    # The provider view masks it too
    got = client.get(
        f"/api/v1/environments/{env['id']}/config/provider", headers=admin_headers
    )
    assert got.json()["deploy"]["ssh_password"] == envconfig_service.SECRET_MASK


def test_reput_masked_deploy_ssh_password_keeps_stored_value(client, admin_headers):
    env = _create_env(client, admin_headers)
    _put_doc(client, admin_headers, env["id"], DEPLOY_PASS_DOC)

    masked = client.get(
        f"/api/v1/environments/{env['id']}/config", headers=admin_headers
    ).json()["yaml"]
    resp = client.put(
        f"/api/v1/environments/{env['id']}/config",
        headers=admin_headers,
        json={"yaml_text": masked},
    )
    assert resp.status_code == 201, resp.text
    deploy = _stored_doc(env["id"])["deploy"]
    assert decrypt_secret(deploy["ssh_password"]) == "node:pass:secret"


def test_set_provider_encrypts_deploy_ssh_password(db_session):
    db, env = db_session
    row, _warnings = envconfig_service.set_provider(
        db,
        env,
        "tester",
        provider="kubespray",
        deploy={"ssh_user": "deploy", "ssh_password": "node:pass:secret"},
    )
    db.commit()
    assert "node:pass:secret" not in row.yaml_text
    doc, _ = envconfig_service.parse_document(row.yaml_text)
    assert doc["deploy"]["ssh_password"].startswith(FERNET_PREFIX)
    assert decrypt_secret(doc["deploy"]["ssh_password"]) == "node:pass:secret"


# ---------------------------------------------------------------------------
# Provider-aware config: ovh source, service_name, set_provider/get_provider
# ---------------------------------------------------------------------------


@pytest.fixture
def db_session():
    """A fresh Environment row + an open session for service-level calls.

    Yields ``(db, env)``; no commit happens until a test explicitly commits, so
    service functions (which only flush) behave like the router endpoints.
    """
    db = SessionLocal()
    suffix = _suffix()
    env = Environment(id=f"env-provider-{suffix}", name=f"env-provider-{suffix}")
    db.add(env)
    db.commit()
    try:
        yield db, env
    finally:
        db.rollback()
        db.delete(env)
        db.commit()
        db.close()


def test_normalize_servers_accepts_ovh_source_and_service_name():
    doc, warnings = envconfig_service.parse_document(
        "servers:\n"
        "  ovh-01:\n"
        "    source: ovh\n"
        "    service_name: ns2030113.ip-203-0-113-20.us\n"
        "    ip: 10.30.0.5\n"
        "    roles: [compute]\n"
    )
    entry = doc["servers"]["ovh-01"]
    assert entry["source"] == "ovh"
    assert entry["service_name"] == "ns2030113.ip-203-0-113-20.us"
    assert not entry.get("system_id")
    assert warnings == []


def test_normalize_servers_warns_on_non_string_service_name():
    doc, warnings = envconfig_service.parse_document(
        "servers:\n" "  ovh-01:\n" "    source: ovh\n" "    service_name: 42\n"
    )
    assert doc["servers"]["ovh-01"]["service_name"] == 42
    assert any("service_name" in w for w in warnings)


def test_normalize_servers_missing_service_name_defaults_to_none():
    doc, _warnings = envconfig_service.parse_document(
        "servers:\n  static-01:\n    ip: 10.30.0.5\n    roles: [compute]\n"
    )
    assert not doc["servers"]["static-01"].get("service_name")


def test_upsert_static_server_persists_source_and_service_name(db_session):
    db, env = db_session
    hostname = f"ovh-{_suffix()}"
    row, warnings = envconfig_service.upsert_static_server(
        db,
        env,
        "tester",
        hostname=hostname,
        ip="10.30.0.5",
        ssh_user="root",
        roles=["compute"],
        source="ovh",
        service_name="ns2030113.ip-203-0-113-20.us",
    )
    db.commit()
    assert warnings == []
    doc, _ = envconfig_service.parse_document(row.yaml_text)
    entry = doc["servers"][hostname]
    assert entry["source"] == "ovh"
    assert entry["service_name"] == "ns2030113.ip-203-0-113-20.us"

    # Role-only save (source omitted) must not rewrite OVH identity back to static.
    row2, _ = envconfig_service.upsert_static_server(
        db,
        env,
        "tester",
        hostname=hostname,
        ip="10.30.0.5",
        roles=["compute", "storage"],
    )
    db.commit()
    doc2, _ = envconfig_service.parse_document(row2.yaml_text)
    kept = doc2["servers"][hostname]
    assert kept["source"] == "ovh"
    assert kept["service_name"] == "ns2030113.ip-203-0-113-20.us"
    assert kept["roles"] == ["compute", "storage"]

    # Invalid source is rejected
    with pytest.raises(envconfig_service.ConfigValidationError):
        envconfig_service.upsert_static_server(
            db,
            env,
            "tester",
            hostname=f"bad-{_suffix()}",
            source="webhook",
        )


def test_set_provider_stores_talos_and_merges(db_session):
    db, env = db_session
    v1, _warnings = envconfig_service.set_provider(
        db,
        env,
        "tester",
        provider="talos",
        talos={"cluster_name": "gs1", "install_disk": "/dev/sda"},
    )
    db.commit()
    assert v1.version == 1
    doc, _ = envconfig_service.parse_document(v1.yaml_text)
    assert doc["provider"] == "talos"
    assert doc["talos"]["cluster_name"] == "gs1"
    assert doc["talos"]["install_disk"] == "/dev/sda"
    assert doc["talos"]["image_url"].startswith("https://factory.talos.dev/image/")

    # Second call merges non-None keys onto the existing section
    v2, _warnings = envconfig_service.set_provider(
        db,
        env,
        "tester",
        provider="talos",
        talos={"image_url": "https://mirror.talos.dev/image/v1.9.0"},
        deploy={"ssh_host": "deployer.example.com", "ssh_user": "deploy"},
    )
    db.commit()
    assert v2.version == 2
    doc, _ = envconfig_service.parse_document(v2.yaml_text)
    assert doc["talos"] == {
        "cluster_name": "gs1",
        "install_disk": "/dev/sda",
        "image_url": "https://mirror.talos.dev/image/v1.9.0",
    }
    assert doc["deploy"] == {"ssh_host": "deployer.example.com", "ssh_user": "deploy"}


def test_set_provider_rejects_unknown_provider(db_session):
    db, env = db_session
    with pytest.raises(envconfig_service.ConfigValidationError):
        envconfig_service.set_provider(db, env, "tester", provider="openshift")


def test_set_provider_warns_on_unknown_talos_key(db_session):
    db, env = db_session
    row, warnings = envconfig_service.set_provider(
        db,
        env,
        "tester",
        provider="talos",
        talos={"cluster_name": "gs1", "bogus_key": "x"},
    )
    db.commit()
    assert any("bogus_key" in w for w in warnings)
    doc, _ = envconfig_service.parse_document(row.yaml_text)
    # Loose validation: the unknown key is kept, not rejected
    assert doc["talos"]["bogus_key"] == "x"


def test_set_provider_kubespray_path(db_session):
    db, env = db_session
    row, warnings = envconfig_service.set_provider(
        db,
        env,
        "tester",
        provider="kubespray",
        deploy={"dry_run": False},
    )
    db.commit()
    assert warnings == []
    doc, _ = envconfig_service.parse_document(row.yaml_text)
    assert doc["provider"] == "kubespray"
    assert doc["deploy"] == {"dry_run": False}
    assert "talos" not in doc


def test_set_provider_creates_version_when_absent(db_session):
    db, env = db_session
    row, _warnings = envconfig_service.set_provider(db, env, "tester", provider="talos")
    db.commit()
    assert row.version == 1
    doc, _ = envconfig_service.parse_document(row.yaml_text)
    assert doc["provider"] == "talos"


def test_get_provider_defaults_without_version(db_session):
    db, env = db_session
    got = envconfig_service.get_provider(db, env)
    assert got["provider"] == "talos"
    assert got["talos"] == {}
    assert got["deploy"] == {}
    assert got["ovh"] == {}
    assert got["infra"] is None
    assert got["ovh_account_id"] is None
    assert got["default_image_url"].startswith("https://factory.talos.dev/image/")
    assert got["default_iso_url"].endswith("/metal-amd64.iso")
    assert "v1.13.9" in got["default_iso_url"]
    # No version was created
    assert envconfig_service.get_current(db, env) is None


def test_get_provider_reads_doc(db_session):
    db, env = db_session
    envconfig_service.set_provider(
        db,
        env,
        "tester",
        provider="talos",
        talos={"cluster_name": "gs1"},
        deploy={"ssh_host": "deployer.example.com"},
    )
    db.commit()
    got = envconfig_service.get_provider(db, env)
    assert got["provider"] == "talos"
    assert got["talos"]["cluster_name"] == "gs1"
    assert got["talos"]["image_url"].startswith("https://factory.talos.dev/image/")
    assert got["deploy"] == {"ssh_host": "deployer.example.com"}
    assert got["ovh"] == {}
    assert got["infra"] is None
    assert got["default_image_url"].startswith("https://factory.talos.dev/image/")


def test_set_ovh_fabric_persists_vrack_vlan_cidr(db_session):
    db, env = db_session
    row, warnings = envconfig_service.set_ovh_fabric(
        db,
        env,
        "tester",
        vrack="pn-lab",
        vlan_id=10,
        private_cidr="10.10.0.0/24",
    )
    db.commit()
    assert row.version == 1
    assert warnings == []
    provider = envconfig_service.get_provider(db, env)
    assert provider["ovh"] == {
        "vrack": "pn-lab",
        "vlan_id": 10,
        "private_cidr": "10.10.0.0/24",
    }
    envconfig_service.set_ovh_fabric(db, env, "tester", vlan_id=0)
    db.commit()
    assert envconfig_service.get_provider(db, env)["ovh"]["vlan_id"] == 0


def test_ovh_vlan_id_out_of_range_rejected(db_session):
    db, env = db_session
    with pytest.raises(envconfig_service.ConfigValidationError, match="vlan_id"):
        envconfig_service.put_version(db, env, "ovh:\n  vlan_id: 4001\n", "tester")


# ---------------------------------------------------------------------------
# Provider endpoints (PUT/GET /config/provider) + /servers service_name
# ---------------------------------------------------------------------------


def test_put_config_provider_endpoint(client, admin_headers):
    env = _create_env(client, admin_headers)
    base = f"/api/v1/environments/{env['id']}"

    # GET on empty state returns defaults (OVH/Talos is the community path)
    resp = client.get(f"{base}/config/provider", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["provider"] == "talos"
    assert body["talos"] == {}
    assert body["deploy"] == {}
    assert body["ovh"] == {}
    assert body["infra"] is None
    assert body["ovh_account_id"] is None
    assert body["default_image_url"].startswith("https://factory.talos.dev/image/")

    # PUT talos with a full block
    resp = client.put(
        f"{base}/config/provider",
        headers=admin_headers,
        json={
            "provider": "talos",
            "talos": {"cluster_name": "gs1", "install_disk": "/dev/sda"},
            "deploy": {"ssh_host": "deployer.example.com", "ssh_user": "deploy"},
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {"version": 1, "warnings": [], "provider": "talos"}

    got = client.get(f"{base}/config/provider", headers=admin_headers).json()
    assert got["provider"] == "talos"
    assert got["talos"]["cluster_name"] == "gs1"
    assert got["talos"]["install_disk"] == "/dev/sda"
    assert got["talos"]["image_url"].startswith("https://factory.talos.dev/image/")
    assert got["deploy"] == {"ssh_host": "deployer.example.com", "ssh_user": "deploy"}
    assert got["default_image_url"].startswith("https://factory.talos.dev/image/")

    # Second PUT merges onto the existing sections
    resp = client.put(
        f"{base}/config/provider",
        headers=admin_headers,
        json={
            "provider": "talos",
            "talos": {"image_url": "https://mirror.example.com/v1"},
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["version"] == 2
    got = client.get(f"{base}/config/provider", headers=admin_headers)
    assert got.json()["talos"] == {
        "cluster_name": "gs1",
        "install_disk": "/dev/sda",
        "image_url": "https://mirror.example.com/v1",
    }
    assert got.json()["deploy"] == {
        "ssh_host": "deployer.example.com",
        "ssh_user": "deploy",
    }

    # The audit trail recorded the provider change
    audit = client.get(
        "/api/v1/audit",
        params={"environment_id": env["id"], "action": "env.config.provider"},
        headers=admin_headers,
    )
    assert audit.status_code == 200, audit.text
    rows = audit.json()
    assert rows and rows[0]["details"]["provider"] == "talos"


def test_put_config_provider_invalid_422(client, admin_headers):
    env = _create_env(client, admin_headers)
    resp = client.put(
        f"/api/v1/environments/{env['id']}/config/provider",
        headers=admin_headers,
        json={"provider": "openshift"},
    )
    assert resp.status_code == 422, resp.text

    # Unknown talos key warns but still stores (200)
    resp = client.put(
        f"/api/v1/environments/{env['id']}/config/provider",
        headers=admin_headers,
        json={"provider": "talos", "talos": {"bogus": "x"}},
    )
    assert resp.status_code == 200, resp.text
    assert any("bogus" in w for w in resp.json()["warnings"])


def test_put_config_provider_requires_operator(client, two_tenants):
    env_a = two_tenants["env_a"]
    viewer = two_tenants["viewer_headers"]
    resp = client.put(
        f"/api/v1/environments/{env_a['id']}/config/provider",
        headers=viewer,
        json={"provider": "talos"},
    )
    assert resp.status_code == 403
    # Viewer can still read
    assert (
        client.get(
            f"/api/v1/environments/{env_a['id']}/config/provider", headers=viewer
        ).status_code
        == 200
    )


def test_servers_static_endpoint_persists_source_and_service_name(
    client, admin_headers
):
    env = _create_env(client, admin_headers)
    base = f"/api/v1/environments/{env['id']}"
    hostname = f"ovh-{_suffix()}"

    resp = client.post(
        f"{base}/servers/static",
        headers=admin_headers,
        json={
            "hostname": hostname,
            "ip": "10.30.0.5",
            "ssh_user": "root",
            "roles": ["compute"],
            "source": "ovh",
            "service_name": "ns2030113.ip-203-0-113-20.us",
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["server"]["source"] == "ovh"
    assert body["server"]["service_name"] == "ns2030113.ip-203-0-113-20.us"

    # GET /servers reflects the doc entry with source + service_name
    servers = client.get(f"{base}/servers", headers=admin_headers)
    assert servers.status_code == 200
    entry = {s["hostname"]: s for s in servers.json()["servers"]}[hostname]
    assert entry["source"] == "ovh"
    assert entry["service_name"] == "ns2030113.ip-203-0-113-20.us"
    assert entry["assigned"] is True

    # Invalid source is rejected
    resp = client.post(
        f"{base}/servers/static",
        headers=admin_headers,
        json={"hostname": f"bad-{_suffix()}", "source": "webhook"},
    )
    assert resp.status_code == 422, resp.text
