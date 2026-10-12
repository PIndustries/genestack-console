"""Facts that break a later deploy step, named before that step runs.

``bin/setup-infrastructure.sh`` reads these with ``set -e``. An empty value
either stops on a prompt or exits after kube-ovn, Longhorn, or the gateway
have already been attempted. The next-action strip and ``genestack.deploy``
both use this list, so the job and the screen say the same thing.
"""

from __future__ import annotations

import re
from typing import Any

from app.services import talos as talos_service
from app.services.envconfig import doc_env

_LEGACY_KERNEL_IFACE = re.compile(r"^eth\d+$")
_NODE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")
_UNROUTABLE_SUFFIX = (
    ".local",
    ".localhost",
    ".invalid",
    ".test",
    ".example",
    ".internal",
)
_VLAN_KEYS = (
    "OVN_EXTERNAL_VLAN_INTERFACE",
    "OVN_EXTERNAL_VLAN_PARENT",
    "OVN_EXTERNAL_VLAN_ID",
    "OVN_EXTERNAL_VLAN_MTU",
)
_STORAGE_ROLES = frozenset({"storage", "storage-ceph", "storage-cinder"})
_WORKER_ROLES = frozenset(
    {"worker", "compute", "network", "storage", "storage-ceph", "storage-cinder"}
)
_ROLE_LABELS: dict[str, tuple[tuple[str, str], ...]] = {
    "control": (("openstack-control-plane", "enabled"),),
    "compute": (("openstack-compute-node", "enabled"),),
    "network": (("openstack-network-node", "enabled"),),
    "storage": (
        ("openstack-storage-node", "enabled"),
        ("longhorn.io/storage-node", "enabled"),
    ),
    "storage-ceph": (
        ("openstack-storage-node", "enabled"),
        ("ceph-storage-node", "enabled"),
    ),
    "storage-cinder": (("openstack-storage-node", "enabled"),),
}
_WORKER_LABEL = ("node-role.kubernetes.io/worker", "worker")


def _need(
    code: str,
    *,
    blocks: bool,
    missing: str,
    detail: str,
    stage: str = "infrastructure",
) -> dict[str, Any]:
    return {
        "code": code,
        "blocks": blocks,
        "missing": missing,
        "detail": detail,
        "stage": stage,
    }


def _doc(doc: dict[str, Any] | None) -> dict[str, Any]:
    return doc if isinstance(doc, dict) else {}


def _network(doc: dict[str, Any]) -> dict[str, Any]:
    network = doc.get("network")
    return network if isinstance(network, dict) else {}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _hyperconverged(network: dict[str, Any]) -> bool:
    value = network.get("hyperconverged")
    if isinstance(value, bool):
        return value
    return _text(value).lower() == "true"


def _provider(doc: dict[str, Any]) -> str:
    return _text(doc.get("provider")).lower()


def _servers(doc: dict[str, Any]) -> list[tuple[str, set[str]]]:
    raw = doc.get("servers")
    if not isinstance(raw, dict):
        return []
    found: list[tuple[str, set[str]]] = []
    for name, entry in raw.items():
        host = _text(name)
        roles: set[str] = set()
        if isinstance(entry, dict):
            listed = entry.get("roles") or []
            if isinstance(listed, str):
                listed = [listed]
            if isinstance(listed, (list, tuple, set)):
                roles = {_text(role).lower() for role in listed if _text(role)}
        if host:
            found.append((host, roles))
    return found


def _has_role(servers: list[tuple[str, set[str]]], role: str) -> bool:
    return any(role in roles for _host, roles in servers)


def _unroutable(domain: str) -> bool:
    name = domain.lower().rstrip(".")
    if not name or "." not in name or name == "cluster.local":
        return True
    return any(name.endswith(suffix) for suffix in _UNROUTABLE_SUFFIX)


def _role_needs(
    servers: list[tuple[str, set[str]]],
) -> list[dict[str, Any]]:
    """Labels setup-infrastructure.sh requires before it installs kube-ovn.

    Kubespray applies these during host setup. A Talos deploy does not run
    that play, so a saved role with no node still exits at the top of the
    infrastructure script.
    """
    needs: list[dict[str, Any]] = []
    checks = (
        (
            "role-control",
            _has_role(servers, "control"),
            "No machine has the control role.",
            (
                "No machine has the control role. setup-infrastructure.sh exits "
                "before kube-ovn because no node can be labeled "
                "openstack-control-plane."
            ),
        ),
        (
            "role-compute",
            _has_role(servers, "compute"),
            "No machine has the compute role.",
            (
                "No machine has the compute role. setup-infrastructure.sh exits "
                "before kube-ovn because no node can be labeled "
                "openstack-compute-node."
            ),
        ),
        (
            "role-network",
            _has_role(servers, "network"),
            "No machine has the network role.",
            (
                "No machine has the network role. setup-infrastructure.sh exits "
                "before kube-ovn because no node can be labeled "
                "openstack-network-node."
            ),
        ),
        (
            "role-storage",
            any(_has_role(servers, role) for role in _STORAGE_ROLES),
            "No machine has a storage role.",
            (
                "No machine has a storage role. setup-infrastructure.sh exits "
                "before kube-ovn because no node can be labeled "
                "openstack-storage-node."
            ),
        ),
        (
            "role-worker",
            any(_has_role(servers, role) for role in _WORKER_ROLES),
            "No machine can take the worker label.",
            (
                "No machine has a worker, compute, network, or storage role. "
                "setup-infrastructure.sh exits before kube-ovn because no node "
                "can be labeled node-role.kubernetes.io/worker."
            ),
        ),
    )
    for code, present, missing, detail in checks:
        if present:
            continue
        needs.append(_need(code, blocks=True, missing=missing, detail=detail))
    return needs


def _provider_notice(
    doc: dict[str, Any], exported: dict[str, str]
) -> dict[str, Any] | None:
    network = _network(doc)
    ovn = network.get("ovn")
    raw = ""
    if isinstance(ovn, dict):
        raw = _text(ovn.get("external_interface"))
    vlan_count = sum(1 for key in _VLAN_KEYS if exported.get(key))
    if vlan_count == 4 or exported.get("OVN_EXTERNAL_INTERFACE"):
        return None
    if _provider(doc) == "talos" and _LEGACY_KERNEL_IFACE.fullmatch(raw):
        return _need(
            "provider-port",
            blocks=False,
            missing=f"Provider port {raw} is not used.",
            detail=(
                f"Provider port {raw} is a kernel name a Talos image renames. "
                "It is not plugged into br-ex. Neutron's external network has "
                "no port until a bond or PCI name is saved."
            ),
        )
    return _need(
        "provider-port",
        blocks=False,
        missing="No provider port is saved.",
        detail=(
            "No provider port is saved. setup-infrastructure.sh leaves br-ex "
            "with no physical port, so Neutron's external network has nowhere "
            "to send traffic."
        ),
    )


def collect(
    doc: dict[str, Any] | None,
    *,
    stages: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Needs for the stages that will run.

    ``stages`` None means the whole pipeline. A need whose stage is not in
    the set is omitted, so a continue from core does not repeat an
    infrastructure fact.
    """
    body = _doc(doc)
    if stages is not None and "infrastructure" not in stages:
        return []
    network = _network(body)
    exported = doc_env(body)
    needs: list[dict[str, Any]] = []

    domain = _text(network.get("gateway_domain"))
    email = _text(network.get("acme_email"))
    if not domain:
        needs.append(
            _need(
                "gateway-domain",
                blocks=True,
                missing="Gateway domain is not saved.",
                detail=(
                    "Gateway domain is not saved. setup-infrastructure.sh "
                    "stops on a prompt before it installs kube-ovn."
                ),
            )
        )
    container = _text(network.get("container_interface"))
    compute = _text(network.get("compute_interface"))
    if not container or not compute:
        needs.append(
            _need(
                "interfaces",
                blocks=True,
                missing="Container and compute interfaces are not both saved.",
                detail=(
                    "Container interface or compute interface is not saved. "
                    "setup-infrastructure.sh guesses NICs on the console host "
                    "and exits when those guesses are the same. Save both, "
                    "and keep them different, before Deploy."
                ),
            )
        )
    elif container == compute:
        needs.append(
            _need(
                "interfaces",
                blocks=True,
                missing="Compute and container interfaces are the same.",
                detail=(
                    f"Compute interface and container interface are both "
                    f"{container}. setup-infrastructure.sh exits before "
                    "kube-ovn."
                ),
            )
        )

    vlan_count = sum(1 for key in _VLAN_KEYS if exported.get(key))
    if vlan_count not in (0, 4):
        needs.append(
            _need(
                "provider-vlan",
                blocks=True,
                missing="The provider VLAN is only partly saved.",
                detail=(
                    "The provider VLAN is only partly saved. "
                    "setup-infrastructure.sh exits before kube-ovn unless "
                    "the interface, parent, id, and mtu are all set, or "
                    "all cleared."
                ),
            )
        )

    if not _hyperconverged(network):
        needs.extend(_role_needs(_servers(body)))

    if _provider(body) == "talos":
        image = talos_service.install_image_from_doc(body)
        if talos_service.image_has_longhorn_extensions(image):
            needs.append(
                _need(
                    "talos-reboot",
                    blocks=False,
                    missing="Some nodes may reboot before Longhorn.",
                    detail=(
                        "Deploy reboots each control plane and storage node "
                        "that is missing ext-iscsid, one at a time, before "
                        "Longhorn. A node that already has the service is "
                        "skipped."
                    ),
                )
            )
        else:
            needs.append(
                _need(
                    "talos-iscsi",
                    blocks=True,
                    missing="The Talos install image has no iSCSI tools.",
                    detail=(
                        "The Talos install image has no iSCSI tools. "
                        "Longhorn's pre-upgrade check fails later because "
                        "iscsiadm is not on the node. Save a factory image "
                        "that includes iscsi-tools before Deploy."
                    ),
                )
            )

    if email and domain and _unroutable(domain):
        needs.append(
            _need(
                "acme-domain",
                blocks=False,
                missing=(
                    f"Gateway domain {domain} cannot complete a public certificate."
                ),
                detail=(
                    f"Gateway domain is {domain} and an ACME email is saved. "
                    "setup-envoy-gateway.sh requests a public certificate for "
                    "that name. That name cannot complete the http-01 check, "
                    "so the gateway certificate stays pending."
                ),
            )
        )
    elif not email:
        needs.append(
            _need(
                "acme-email",
                blocks=False,
                missing="No ACME email is saved.",
                detail=(
                    "No ACME email is saved. The gateway will not request a "
                    "certificate, so Horizon and Skyline stay without a "
                    "public certificate."
                ),
            )
        )

    notice = _provider_notice(body, exported)
    if notice is not None:
        needs.append(notice)
    return needs


def blocking(
    doc: dict[str, Any] | None,
    *,
    stages: set[str] | None = None,
) -> list[dict[str, Any]]:
    return [item for item in collect(doc, stages=stages) if item["blocks"]]


def node_label_args(doc: dict[str, Any] | None) -> list[tuple[str, list[str]]]:
    """``(node, ['key=value', ...])`` for the infrastructure label check.

    Hyperconverged installs label every node themselves. An unsafe node
    name is skipped.
    """
    body = _doc(doc)
    if _hyperconverged(_network(body)):
        return []
    planned: list[tuple[str, list[str]]] = []
    for host, roles in _servers(body):
        if not _NODE_NAME.fullmatch(host):
            continue
        pairs: list[str] = []
        seen: set[str] = set()
        for role in sorted(roles):
            for key, value in _ROLE_LABELS.get(role, ()):
                token = f"{key}={value}"
                if token not in seen:
                    seen.add(token)
                    pairs.append(token)
        if roles & _WORKER_ROLES:
            token = f"{_WORKER_LABEL[0]}={_WORKER_LABEL[1]}"
            if token not in seen:
                pairs.append(token)
        if pairs:
            planned.append((host, pairs))
    return planned
