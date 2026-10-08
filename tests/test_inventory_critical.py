"""Test inventory — critical path functions.

Focused unit tests for build_inventory_from_environment:
key auth servers, password auth servers, deployer inclusion.

Tests the inventory builder directly without DB or API endpoints.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

# The inventory module uses `from services.crypto import decrypt_secret`
# (relative import from app/services/). Ensure this resolves at runtime.
_console_root = Path(__file__).resolve().parents[0].parent
if str(_console_root / "app") not in sys.path:
    sys.path.insert(0, str(_console_root / "app"))

from app.services.crypto import encrypt_secret  # noqa: E402
from app.services.inventory import (  # noqa: E402
    build_inventory_from_environment,
    inventory_host_list,
)


def _make_env(**kwargs):
    """Create a mock Environment with sensible defaults."""
    env = MagicMock()
    env.id = "test-env-id"
    env.name = "test-env"
    env.region = "lab"
    env.tier = "dev"
    # Use kwargs.get directly to allow None values to pass through
    env.deployer_ssh_host = kwargs.get("deployer_ssh_host", "deployer.example.com")
    env.deployer_ssh_user = kwargs.get("deployer_ssh_user", "ubuntu")
    env.genestack_path = kwargs.get("genestack_path", "/opt/genestack")
    env.kubeconfig_path = kwargs.get("kubeconfig_path", "/etc/genestack/kubeconfig")
    env.ssh_private_key_encrypted = kwargs.get("ssh_private_key_encrypted", "")
    env.ssh_public_key = kwargs.get("ssh_public_key", "")
    env.metadata_json = kwargs.get("metadata_json") or {}
    return env


class TestKeyAuthServers:
    """Servers with key auth get ansible_ssh_private_key_file."""

    def test_key_auth_server_gets_private_key_file(self):
        env = _make_env()
        servers = {
            "node01": {
                "ip": "10.0.0.1",
                "ssh_auth_method": "key",
                "roles": ["compute"],
                "source": "static",
            },
        }
        inv = build_inventory_from_environment(
            env, servers=servers, include_deployer=False
        )
        host = inv["all"]["hosts"]["node01"]
        assert host["ansible_ssh_private_key_file"] == "/etc/genestack/.ssh/id_ed25519"

    def test_default_auth_is_key(self):
        env = _make_env()
        servers = {
            "node01": {
                "ip": "10.0.0.1",
                "roles": ["compute"],
                "source": "static",
            },
        }
        inv = build_inventory_from_environment(
            env, servers=servers, include_deployer=False
        )
        host = inv["all"]["hosts"]["node01"]
        assert "ansible_ssh_private_key_file" in host
        assert "ansible_password" not in host


class TestPasswordAuthServers:
    """Servers with password auth get ansible_password, not private key file."""

    def test_password_auth_server_gets_ansible_password(self):
        env = _make_env()
        encrypted_pwd = encrypt_secret("mypass")
        servers = {
            "node01": {
                "ip": "10.0.0.1",
                "ssh_auth_method": "password",
                "ssh_password": encrypted_pwd,
                "roles": ["compute"],
                "source": "static",
            },
        }
        inv = build_inventory_from_environment(
            env, servers=servers, include_deployer=False
        )
        host = inv["all"]["hosts"]["node01"]
        assert "ansible_password" in host

    def test_password_auth_server_no_private_key_file(self):
        env = _make_env()
        encrypted_pwd = encrypt_secret("mypass")
        servers = {
            "node01": {
                "ip": "10.0.0.1",
                "ssh_auth_method": "password",
                "ssh_password": encrypted_pwd,
                "roles": ["compute"],
                "source": "static",
            },
        }
        inv = build_inventory_from_environment(
            env, servers=servers, include_deployer=False
        )
        host = inv["all"]["hosts"]["node01"]
        assert "ansible_ssh_private_key_file" not in host

    def test_password_auth_no_password_value(self):
        env = _make_env()
        servers = {
            "node01": {
                "ip": "10.0.0.1",
                "ssh_auth_method": "password",
                "roles": ["compute"],
                "source": "static",
            },
        }
        inv = build_inventory_from_environment(
            env, servers=servers, include_deployer=False
        )
        host = inv["all"]["hosts"]["node01"]
        assert "ansible_ssh_private_key_file" not in host


class TestDeployer:
    """Deployer inclusion/exclusion in inventory."""

    def test_deployer_included_when_true(self):
        env = _make_env()
        inv = build_inventory_from_environment(env, include_deployer=True)
        deployer_name = env.deployer_ssh_host
        assert deployer_name in inv["all"]["hosts"]
        assert "deployers" in inv["all"]["children"]

    def test_deployer_excluded_when_false(self):
        env = _make_env()
        inv = build_inventory_from_environment(env, include_deployer=False)
        deployer_name = env.deployer_ssh_host
        assert deployer_name not in inv["all"]["hosts"]

    def test_deployer_uses_key_auth(self):
        env = _make_env()
        inv = build_inventory_from_environment(env, include_deployer=True)
        deployer = inv["all"]["hosts"][env.deployer_ssh_host]
        assert (
            deployer["ansible_ssh_private_key_file"] == "/etc/genestack/.ssh/id_ed25519"
        )
        assert (
            deployer["ansible_ssh_common_args"]
            == '"-o StrictHostKeyChecking=accept-new"'
        )

    def test_deployer_no_host(self):
        env = _make_env(deployer_ssh_host=None)
        inv = build_inventory_from_environment(env, include_deployer=True)
        # No deployer host entry when deployer_ssh_host is None
        assert "deployers" not in inv["all"]["children"]


class TestRoleGrouping:
    """Server roles map to correct ansible groups."""

    def test_k8s_control_plane_grouping(self):
        env = _make_env()
        servers = {
            "cp01": {
                "ip": "10.0.0.1",
                "roles": ["k8s_control_plane"],
                "source": "static",
            },
        }
        inv = build_inventory_from_environment(
            env, servers=servers, include_deployer=False
        )
        children = inv["all"]["children"]
        k8s = children["k8s_cluster"]["children"]
        assert "cp01" in k8s["kube_control_plane"]["hosts"]
        assert "cp01" in k8s["kube_node"]["hosts"]

    def test_etcd_not_in_kube_node(self):
        env = _make_env()
        servers = {
            "etcd01": {
                "ip": "10.0.0.1",
                "roles": ["etcd"],
                "source": "static",
            },
        }
        inv = build_inventory_from_environment(
            env, servers=servers, include_deployer=False
        )
        children = inv["all"]["children"]
        k8s = children["k8s_cluster"]["children"]
        assert "etcd01" in k8s["etcd"]["hosts"]
        assert "etcd01" not in k8s.get("kube_node", {}).get("hosts", {})

    def test_worker_is_a_kube_node_only(self):
        env = _make_env()
        servers = {
            "work01": {
                "ip": "10.0.0.1",
                "roles": ["worker"],
                "source": "static",
            },
        }
        inv = build_inventory_from_environment(
            env, servers=servers, include_deployer=False
        )
        children = inv["all"]["children"]
        k8s = children["k8s_cluster"]["children"]
        assert "work01" in k8s["kube_node"]["hosts"]
        assert "openstack_compute_nodes" not in k8s
        assert "storage_nodes" not in k8s

    def test_compute_grouping(self):
        env = _make_env()
        servers = {
            "comp01": {
                "ip": "10.0.0.1",
                "roles": ["compute"],
                "source": "static",
            },
        }
        inv = build_inventory_from_environment(
            env, servers=servers, include_deployer=False
        )
        children = inv["all"]["children"]
        k8s = children["k8s_cluster"]["children"]
        assert "comp01" in k8s["openstack_compute_nodes"]["hosts"]
        assert "comp01" in k8s["kube_node"]["hosts"]

    def test_storage_grouping(self):
        env = _make_env()
        servers = {
            "stor01": {
                "ip": "10.0.0.1",
                "roles": ["storage"],
                "source": "static",
            },
        }
        inv = build_inventory_from_environment(
            env, servers=servers, include_deployer=False
        )
        children = inv["all"]["children"]
        k8s = children["k8s_cluster"]["children"]
        storage = k8s["storage_nodes"]["children"]
        assert "stor01" in storage["longhorn_storage_nodes"]["hosts"]

    def test_multiple_roles(self):
        env = _make_env()
        servers = {
            "multi01": {
                "ip": "10.0.0.1",
                "roles": ["k8s_control_plane", "etcd", "control"],
                "source": "static",
            },
        }
        inv = build_inventory_from_environment(
            env, servers=servers, include_deployer=False
        )
        children = inv["all"]["children"]
        k8s = children["k8s_cluster"]["children"]
        assert "multi01" in k8s["kube_control_plane"]["hosts"]
        assert "multi01" in k8s["etcd"]["hosts"]
        assert "multi01" in k8s["openstack_control_plane"]["hosts"]


class TestInventoryHostList:
    """inventory_host_list utility function."""

    def test_returns_sorted_host_names(self):
        env = _make_env()
        servers = {
            "bravo": {"ip": "10.0.0.2", "roles": ["compute"], "source": "static"},
            "alpha": {"ip": "10.0.0.1", "roles": ["compute"], "source": "static"},
            "charlie": {"ip": "10.0.0.3", "roles": ["compute"], "source": "static"},
        }
        inv = build_inventory_from_environment(
            env, servers=servers, include_deployer=False
        )
        hosts = inventory_host_list(inv)
        assert hosts == ["alpha", "bravo", "charlie"]

    def test_empty_inventory(self):
        hosts = inventory_host_list({})
        assert hosts == []


class TestStaticHosts:
    """Static hosts from env metadata."""

    def test_static_hosts_included(self):
        env = _make_env(metadata_json={"hosts": ["10.0.0.99"]})
        inv = build_inventory_from_environment(env, include_deployer=False)
        assert "10.0.0.99" in inv["all"]["hosts"]
        host = inv["all"]["hosts"]["10.0.0.99"]
        assert host["ansible_host"] == "10.0.0.99"

    def test_static_host_dict(self):
        env = _make_env(
            metadata_json={
                "hosts": [
                    {
                        "hostname": "static-host",
                        "ip": "10.0.0.5",
                        "groups": ["compute"],
                    }
                ]
            }
        )
        inv = build_inventory_from_environment(env, include_deployer=False)
        assert "static-host" in inv["all"]["hosts"]


class TestSavedServerSource:
    """An older saved server still renders from the document."""

    def test_older_source_renders_without_live_merge(self):
        env = _make_env()
        servers = {
            "node-a": {
                "system_id": "abc123",
                "ip": "10.0.0.1",
                "roles": ["compute"],
                "source": "maas",
            }
        }
        inv = build_inventory_from_environment(
            env, servers=servers, include_deployer=False
        )
        assert inv["all"]["hosts"]["node-a"]["ansible_host"] == "10.0.0.1"
        assert "maas_system_id" not in inv["all"]["hosts"]["node-a"]
        k8s = inv["all"]["children"]["k8s_cluster"]["children"]
        assert "node-a" in k8s["openstack_compute_nodes"]["hosts"]
