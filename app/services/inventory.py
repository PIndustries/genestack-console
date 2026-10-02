"""Build inventory dictionaries from environment hosts.

Doc server roles map onto the group layout the parent genestack repo consumes
(see ansible/inventory/genestack/inventory.yaml.example):

    k8s_control_plane -> kube_control_plane + kube_node   (kubespray)
    etcd              -> etcd                             (kubespray; NOT kube_node)
    control           -> openstack_control_plane + kube_node
    compute           -> openstack_compute_nodes + kube_node
    network           -> ovn_network_nodes + kube_node
    storage           -> storage_nodes.children.longhorn_storage_nodes + kube_node
    storage-ceph      -> storage_nodes.children.ceph_storage_nodes + kube_node
    storage-cinder    -> storage_nodes.children.cinder_storage_nodes + kube_node

All of these groups nest under ``all.children.k8s_cluster.children`` (storage
one level deeper, under ``storage_nodes.children``), matching the example —
the nesting matters because hosts then inherit ``k8s_cluster`` vars such as
``kube_ovn_central_hosts``. Longhorn is the genestack default storage target
(``storage``); ``storage-ceph`` (rook-managed ceph) and ``storage-cinder``
(e.g. netapp backends) are the alternative storage sub-roles the example's
storage_nodes.children layout defines. ``k8s_cluster`` gets
``vars.cluster_name: cluster.local`` when (and only when) any role lands a
host in one of its groups.
"""

from __future__ import annotations

from typing import Any

from app.models import Environment

# Doc servers: role -> inventory groups (see services/envconfig.py)
SERVER_ROLE_GROUPS: dict[str, tuple[str, ...]] = {
    "k8s_control_plane": ("kube_control_plane", "kube_node"),
    "etcd": ("etcd",),
    "control": ("openstack_control_plane", "kube_node"),
    "compute": ("openstack_compute_nodes", "kube_node"),
    "network": ("ovn_network_nodes", "kube_node"),
    "storage": ("longhorn_storage_nodes", "kube_node"),
    "storage-ceph": ("ceph_storage_nodes", "kube_node"),
    "storage-cinder": ("cinder_storage_nodes", "kube_node"),
}

# Groups that nest under all.children.k8s_cluster.children — kubespray groups
# AND genestack groups, matching ansible/inventory/genestack/inventory.yaml.example
# (nesting matters: hosts inherit k8s_cluster vars like kube_ovn_central_hosts)
_K8S_CLUSTER_GROUPS = frozenset(
    {
        "kube_control_plane",
        "etcd",
        "kube_node",
        "openstack_control_plane",
        "openstack_compute_nodes",
        "ovn_network_nodes",
    }
)

# Groups that nest under all.children.k8s_cluster.children.storage_nodes.children
_STORAGE_GROUPS = frozenset(
    {"longhorn_storage_nodes", "ceph_storage_nodes", "cinder_storage_nodes"}
)

def _add_to_group(children: dict[str, Any], group: str, name: str) -> None:
    """Place host ``name`` in ``group``, creating the nesting genestack expects."""
    if group in _K8S_CLUSTER_GROUPS:
        k8s = children.setdefault(
            "k8s_cluster",
            {"vars": {"cluster_name": "cluster.local"}, "children": {}},
        )
        k8s["children"].setdefault(group, {"hosts": {}})["hosts"][name] = {}
    elif group in _STORAGE_GROUPS:
        k8s = children.setdefault(
            "k8s_cluster",
            {"vars": {"cluster_name": "cluster.local"}, "children": {}},
        )
        storage = k8s["children"].setdefault("storage_nodes", {"children": {}})
        storage["children"].setdefault(group, {"hosts": {}})["hosts"][name] = {}
    else:
        children.setdefault(group, {"hosts": {}})["hosts"][name] = {}


def build_inventory_from_environment(
    env: Environment,
    servers: dict[str, Any] | None = None,
    include_deployer: bool = True,
) -> dict[str, Any]:
    """
    Build a simple ansible-style inventory dict for an environment.

    ``servers`` optional doc servers section (hostname -> {system_id, ip,
    ssh_user, roles, source}) from the environment config document. Roles
    map to groups via SERVER_ROLE_GROUPS. Static and terraform entries
    (source "static"/"terraform") get ansible_host=ip and
    ansible_user=ssh_user (default "ubuntu"). An older record whose source
    is "maas" still renders. Legacy system_id-keyed entries are still
    understood.

    ``include_deployer`` adds the env's deploy host under a console-side
    ``deployers`` group; the rendered /etc/genestack inventory passes False
    (the deployer runs the playbooks, it is not a deployment target).
    """
    meta = env.metadata_json or {}
    static_hosts = meta.get("hosts") or meta.get("inventory_hosts") or []

    inventory: dict[str, Any] = {
        "all": {
            "hosts": {},
            "vars": {
                "environment_id": env.id,
                "environment_name": env.name,
                "region": env.region,
                "tier": env.tier,
                "genestack_path": env.genestack_path,
                "kubeconfig_path": env.kubeconfig_path,
                "genestack_ssh_private_key_file": "/etc/genestack/.ssh/id_ed25519",
                "genestack_ssh_public_key": env.ssh_public_key or "",
            },
            "children": {},
        },
    }

    # Deployer from environment fields (console-side only, never rendered)
    if include_deployer and env.deployer_ssh_host:
        deployer_name = env.deployer_ssh_host
        deployer_vars: dict[str, Any] = {
            "ansible_host": env.deployer_ssh_host,
            "ansible_user": env.deployer_ssh_user or "ubuntu",
            "role": "deployer",
            "ansible_ssh_private_key_file": "/etc/genestack/.ssh/id_ed25519",
            "ansible_ssh_common_args": '"-o StrictHostKeyChecking=accept-new"',
        }
        inventory["all"]["hosts"][deployer_name] = deployer_vars
        _add_to_group(inventory["all"]["children"], "deployers", deployer_name)

    # Static hosts from metadata
    for h in static_hosts:
        if isinstance(h, str):
            name = h
            entry: dict[str, Any] = {"ansible_host": h}
            groups: list[str] = []
        else:
            name = (
                h.get("hostname")
                or h.get("name")
                or h.get("ansible_host")
                or h.get("ip")
            )
            if not name:
                continue
            entry = {
                "ansible_host": h.get("ansible_host") or h.get("ip") or name,
                "ansible_user": h.get("ansible_user") or h.get("user"),
            }
            if h.get("vars"):
                entry.update(h["vars"])
            groups = h.get("groups") or h.get("roles") or []

        inventory["all"]["hosts"][name] = {
            k: v for k, v in entry.items() if v is not None
        }
        host_vars = inventory["all"]["hosts"][name]
        host_vars["ansible_ssh_private_key_file"] = "/etc/genestack/.ssh/id_ed25519"
        host_vars["ansible_ssh_common_args"] = '"-o StrictHostKeyChecking=accept-new"'
        for g in groups:
            _add_to_group(inventory["all"]["children"], str(g), name)

    # Doc servers section: explicit role assignments keyed by hostname.
    servers = servers or {}
    for key, server in servers.items():
        if not isinstance(server, dict):
            continue
        if (
            "source" not in server
            and "system_id" not in server
            and server.get("hostname")
        ):
            # Legacy keying: the mapping key is a system id.
            name = str(server["hostname"])
        else:
            name = str(server.get("hostname") or key)
        source = server.get("source") or "static"
        entry = {"ansible_host": server.get("ip") or name}
        if source in ("static", "terraform"):
            entry["ansible_user"] = server.get("ssh_user") or "ubuntu"
        inventory["all"]["hosts"][name] = {
            k: v for k, v in entry.items() if v is not None
        }
        host_vars = inventory["all"]["hosts"][name]
        auth_method = (server.get("ssh_auth_method") or "key").lower()
        if auth_method == "password":
            pwd = server.get("ssh_password")
            if pwd:
                from app.services.crypto import decrypt_secret

                host_vars["ansible_password"] = decrypt_secret(pwd)
            host_vars["ansible_ssh_pass"] = decrypt_secret(pwd) if pwd else None
        else:
            host_vars["ansible_ssh_private_key_file"] = "/etc/genestack/.ssh/id_ed25519"
        host_vars["ansible_ssh_common_args"] = '"-o StrictHostKeyChecking=accept-new"'
        children = inventory["all"]["children"]
        for role in server.get("roles") or []:
            for group in SERVER_ROLE_GROUPS.get(str(role).lower()) or ():
                _add_to_group(children, group, name)

    return inventory


def inventory_host_list(inventory: dict[str, Any]) -> list[str]:
    """Flat list of host names from inventory."""
    return sorted((inventory.get("all") or {}).get("hosts") or {}.keys())
