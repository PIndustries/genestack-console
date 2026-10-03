"""Talos Linux bootstrap — genestack's ``provider: talos`` path.

Implements genestack's documented talos flow (docs/k8s-talos.md) natively,
driven from the env config document's ``servers:`` section:

  1. talosctl gen config <cluster> https://<cp-ip>:6443 --install-disk <disk>
  2. talosctl apply-config --insecure --nodes <cp-ip> --file controlplane.yaml
  3. talosctl apply-config --insecure --nodes <worker-ip> --file worker.yaml
     (once per worker)
  4. talosctl config endpoints <apply-ip> --talosconfig=./talosconfig
  5. talosctl bootstrap --nodes <apply-ip> --talosconfig=./talosconfig   (once)
  6. talosctl kubeconfig <path> --nodes <apply-ip> --talosconfig=./talosconfig

All commands run from ``<config_dir>/talos`` on the deploy host (local or over
the ssh executor) via the genestack bridge. Control-plane nodes are doc
servers whose roles contain ``k8s_control_plane``; every other server is a
worker for talos purposes. Bootstrap and endpoints target the first
control-plane node (docs/k8s-talos.md: bootstrap runs ONCE on a single cp).

Two doc notes are surfaced in the op logs, never enforced:
  - pin kube-ovn to v1.14.10 in helm-chart-versions.yaml for talos
  - nodes boot a Talos Image Factory image carrying the siderolabs/iscsi-tools
    and siderolabs/util-linux-tools extensions (longhorn needs them)
"""

from __future__ import annotations

import base64
import ipaddress
import os
import re
import shlex
from pathlib import Path
from typing import Any, Callable

import yaml

from app.models import Environment
from app.services import genestack_bridge as bridge
from app.services import secret_lease
from app.services.envconfig import ConfigValidationError

LogFn = Callable[[str], None]

DEFAULT_INSTALL_DISK = "/dev/sda"

# Same deploy-host-relative kubeconfig location deploy.py's kubespray
# auto-fetch uses (kept here to avoid a circular import).
KUBECONFIG_FALLBACK_RELPATH = "inventory/artifacts/admin.conf"

# Advisory notes from docs/k8s-talos.md — logged, not enforced.
KUBE_OVN_TALOS_PIN = "v1.14.10"
FACTORY_IMAGE_EXTENSIONS = ("siderolabs/iscsi-tools", "siderolabs/util-linux-tools")

# Image Factory schematic: official iscsi-tools + util-linux-tools (Longhorn).
# metal-amd64.qcow2 is what OVH BYOI expects (not the raw.xz installer).
DEFAULT_TALOS_VERSION = "v1.13.9"
DEFAULT_FACTORY_SCHEMATIC = (
    "613e1592b2da41ae5e265e8789429f22e121aab91cb4deb6bc3c0b6262961245"
)
DEFAULT_TALOS_IMAGE_URL = (
    f"https://factory.talos.dev/image/{DEFAULT_FACTORY_SCHEMATIC}/"
    f"{DEFAULT_TALOS_VERSION}/metal-amd64.qcow2"
)
# Installer image used by `talosctl gen config --install-image`. Must match
# the factory schematic BYOI already wrote, otherwise apply-config reinstalls
# vanilla Talos and drops the Longhorn extensions.
DEFAULT_TALOS_INSTALL_IMAGE = (
    f"factory.talos.dev/installer/{DEFAULT_FACTORY_SCHEMATIC}:{DEFAULT_TALOS_VERSION}"
)

# Dual-NIC (OVH Rise 1): cluster fabric is the private/vRack CIDRs. Public
# NIC stays up for egress + later ingress, but Talos/etcd/kubelet bind only
# inside these subnets and the host firewall default-denies the edge.
DEFAULT_CLUSTER_CIDRS = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
# Greenfield Rise fabric is 802.1q VLAN 100. Must match
# app.services.ovh_fabric.DEFAULT_VLAN_ID (importing ovh_fabric here cycles:
# ovh_fabric → deploy → talos). Explicit ovh.vlan_id: 0 stays untagged.
DEFAULT_VLAN_ID = 100
# When ovh.private_cidr is set, do not punch the whole RFC1918 space on the
# public NIC. Include the Genestack kube-ovn ranges that actually exist.
DEFAULT_POD_CIDR = "10.244.0.0/16"
DEFAULT_SERVICE_CIDR = "10.96.0.0/12"
GENESTACK_OVN_POD_CIDR = "10.236.0.0/14"
KUBE_OVN_JOIN_CIDR = "100.64.0.0/16"
NETWORK_PATCH_FILENAME = "network-patch.yaml"
ETCD_PATCH_FILENAME = "etcd-patch.yaml"
FIREWALL_FILENAME = "firewall.yaml"
# PKI-bearing artifacts written by `talosctl gen config`. Presence of either
# means the workdir already holds live cluster identity — never --force gen.
TALOS_PKI_MARKER_FILES = ("secrets.yaml", "talosconfig")
# Dual-NIC: kube-apiserver (6443) and the Talos API (50000) may stay reachable
# on the public NIC *from listed management CIDRs only* — never 0.0.0.0/0.
DEFAULT_PUBLIC_MANAGEMENT_PORTS = (6443, 50000)
_KUBE_SERVER_RE = re.compile(r"^(\s*server:\s*)\S+.*$", re.MULTILINE)
_IMAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{7,199}$")
_HOST_RE = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$"
)
_CLUSTER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")
_DISK_RE = re.compile(r"^/dev/[A-Za-z0-9/._+-]+$")


def _write_file_argv(filename: str, content: str) -> list[str]:
    """Write a file on the deploy host via a base64 payload (ssh/agent safe)."""
    b64 = base64.b64encode(content.encode("utf-8")).decode("ascii")
    return ["sh", "-c", f"printf '%s' '{b64}' | base64 -d > {filename}"]


def _doc_servers(doc: dict[str, Any]) -> dict[str, Any]:
    servers = doc.get("servers") if isinstance(doc, dict) else None
    return servers if isinstance(servers, dict) else {}


def _clean_ip(value: Any) -> str:
    return str(value or "").split("/", 1)[0].strip()


def valid_install_image(image: str) -> bool:
    """Registry-style installer image for ``talosctl --install-image`` / upgrade."""
    raw = str(image or "").strip()
    if not raw or raw.startswith("-") or ".." in raw or " " in raw or "://" in raw:
        return False
    return _IMAGE_RE.match(raw) is not None


def valid_node_endpoint(value: str) -> bool:
    """IPv4/IPv6 or DNS hostname safe as ``talosctl --nodes``."""
    host = str(value or "").split("/", 1)[0].strip()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if not host or host.startswith("-") or len(host) > 253:
        return False
    if any(c in host for c in " \t\n;|&$`\\\"'"):
        return False
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return bool(_HOST_RE.match(host))


def node_cluster_ip(entry: dict[str, Any], hostname: str = "") -> str:
    """IP Talos/Kubernetes/Genestack use on the node (private NIC preferred)."""
    return _clean_ip(entry.get("private_ip")) or _clean_ip(entry.get("ip")) or hostname


def node_apply_ip(entry: dict[str, Any], hostname: str = "") -> str:
    """IP used to reach the node in Talos maintenance mode.

    Maintenance listens on every NIC, so a console that is not on the vRack
    still applies over the public address. After the firewall patch lands,
    later talosctl (bootstrap, kubeconfig) uses :func:`node_cluster_ip`.
    """
    return (
        _clean_ip(entry.get("public_ip"))
        or _clean_ip(entry.get("ip"))
        or node_cluster_ip(entry, hostname)
    )


def _valid_cidr(value: str) -> bool:
    text = str(value or "").strip()
    if not text or text.startswith("-"):
        return False
    try:
        ipaddress.ip_network(text, strict=False)
    except ValueError:
        return False
    return True


def cluster_cidrs_from_doc(doc: dict[str, Any]) -> list[str]:
    talos_cfg = doc.get("talos") if isinstance(doc, dict) else None
    raw = talos_cfg.get("cluster_cidrs") if isinstance(talos_cfg, dict) else None
    if isinstance(raw, str) and raw.strip():
        return (
            [raw.strip()] if _valid_cidr(raw.strip()) else list(DEFAULT_CLUSTER_CIDRS)
        )
    if isinstance(raw, list):
        out = [str(item).strip() for item in raw if _valid_cidr(str(item).strip())]
        if out:
            return out
    private = private_cidr_from_doc(doc)
    if private and _valid_cidr(private):
        cidrs = [private]
        for extra in (
            DEFAULT_POD_CIDR,
            GENESTACK_OVN_POD_CIDR,
            DEFAULT_SERVICE_CIDR,
            KUBE_OVN_JOIN_CIDR,
        ):
            if extra not in cidrs:
                cidrs.append(extra)
        return cidrs
    return list(DEFAULT_CLUSTER_CIDRS)


def _ovh_map(doc: dict[str, Any]) -> dict[str, Any]:
    section = doc.get("ovh") if isinstance(doc, dict) else None
    return section if isinstance(section, dict) else {}


def vlan_id_from_doc(doc: dict[str, Any]) -> int:
    """Return the private-NIC VLAN. Missing/null/invalid → 100; explicit 0 is untagged."""
    section = _ovh_map(doc)
    if "vlan_id" not in section or section.get("vlan_id") is None:
        return DEFAULT_VLAN_ID
    raw = section.get("vlan_id")
    if raw == "":
        return DEFAULT_VLAN_ID
    try:
        vlan = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_VLAN_ID
    return vlan if 0 <= vlan <= 4000 else DEFAULT_VLAN_ID


def private_cidr_from_doc(doc: dict[str, Any]) -> str:
    return str(_ovh_map(doc).get("private_cidr") or "").strip()


def address_with_prefix(ip: str, cidr: str) -> str:
    addr = str(ip or "").split("/", 1)[0].strip()
    if not addr:
        return ""
    if "/" in str(cidr or ""):
        prefix = str(cidr).split("/", 1)[1].strip()
        if prefix.isdigit():
            return f"{addr}/{prefix}"
    return addr


def public_ingress_ports_from_doc(doc: dict[str, Any]) -> list[int]:
    talos_cfg = doc.get("talos") if isinstance(doc, dict) else None
    raw = talos_cfg.get("public_ingress_ports") if isinstance(talos_cfg, dict) else None
    if not isinstance(raw, list):
        return []
    ports: list[int] = []
    for item in raw:
        try:
            port = int(item)
        except (TypeError, ValueError):
            continue
        if 1 <= port <= 65535 and port not in ports:
            ports.append(port)
    return ports


def public_management_cidrs_from_doc(doc: dict[str, Any]) -> list[str]:
    """Source CIDRs allowed to hit public 6443/50000. Never 0.0.0.0/0."""
    talos_cfg = doc.get("talos") if isinstance(doc, dict) else None
    allow_world = (
        bool(talos_cfg.get("allow_public_world"))
        if isinstance(talos_cfg, dict)
        else False
    )
    raw = (
        talos_cfg.get("public_management_cidrs")
        if isinstance(talos_cfg, dict)
        else None
    )
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        text = str(item).strip()
        if "/" not in text and valid_node_endpoint(text):
            text = f"{text}/32"
        if not _valid_cidr(text):
            continue
        net = ipaddress.ip_network(text, strict=False)
        if net.prefixlen == 0 and not allow_world:
            continue
        cidr = str(net)
        if cidr not in out:
            out.append(cidr)
    return out


def nodeport_bind_cidrs_from_doc(doc: dict[str, Any]) -> list[str]:
    """CIDRs kube-proxy may bind NodePorts on (private fabric only)."""
    private = private_cidr_from_doc(doc)
    if private and _valid_cidr(private):
        return [str(ipaddress.ip_network(private, strict=False))]
    return []


def install_image_from_doc(doc: dict[str, Any]) -> str:
    talos_cfg = doc.get("talos") if isinstance(doc, dict) else None
    raw = talos_cfg.get("install_image") if isinstance(talos_cfg, dict) else None
    image = str(raw or "").strip()
    return image or DEFAULT_TALOS_INSTALL_IMAGE


def _rewrite_kubeconfig_text(text: str, server_url: str) -> str:
    """Return ``text`` with its kubeconfig ``server:`` line pointed at ``server_url``."""
    return _KUBE_SERVER_RE.sub(lambda m: f"{m.group(1)}{server_url}", text)


def rewrite_kubeconfig_server(path: Path, server_url: str) -> bool:
    """Point kubeconfig `server:` at an address the console can actually reach."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    new = _rewrite_kubeconfig_text(text, server_url)
    if new == text:
        return False
    path.write_text(new, encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return True


def fleet_has_private_nic(doc: dict[str, Any]) -> bool:
    for _hostname, entry in _doc_servers(doc).items():
        if isinstance(entry, dict) and _clean_ip(entry.get("private_ip")):
            return True
    return False


def _node_record(hostname: str, entry: dict[str, Any]) -> dict[str, str]:
    cluster_ip = node_cluster_ip(entry, hostname)
    apply_ip = node_apply_ip(entry, hostname)
    return {
        "hostname": str(hostname),
        "ip": cluster_ip,
        "apply_ip": apply_ip,
        "public_ip": _clean_ip(entry.get("public_ip")),
        "private_ip": _clean_ip(entry.get("private_ip")),
    }


def network_patch_yaml(doc: dict[str, Any]) -> str:
    """Strategic merge patch applied to every node: certSANs + kubelet fabric."""
    cidrs = cluster_cidrs_from_doc(doc)
    sans: list[str] = []
    for hostname, entry in _doc_servers(doc).items():
        if not isinstance(entry, dict):
            continue
        for key in ("private_ip", "ip", "public_ip"):
            addr = _clean_ip(entry.get(key))
            if addr and addr not in sans:
                sans.append(addr)
        if hostname and hostname not in sans:
            sans.append(str(hostname))
    kubelet: dict[str, Any] = {
        "nodeIP": {"validSubnets": cidrs},
        "extraArgs": {
            "healthz-bind-address": "127.0.0.1",
            "read-only-port": "0",
        },
    }
    patch: dict[str, Any] = {
        "machine": {
            "certSANs": sans,
            "kubelet": kubelet,
        },
    }
    nodeports = nodeport_bind_cidrs_from_doc(doc)
    if nodeports:
        patch["cluster"] = {
            "proxy": {
                "extraArgs": {
                    "nodeport-addresses": ",".join(nodeports),
                }
            }
        }
    return yaml.safe_dump(patch, sort_keys=False)


def etcd_patch_yaml(doc: dict[str, Any]) -> str:
    """Control-plane-only patch: etcd is invalid on worker machine configs."""
    cidrs = cluster_cidrs_from_doc(doc)
    patch = {
        "cluster": {
            "etcd": {
                "advertisedSubnets": cidrs,
                "listenSubnets": cidrs,
            }
        },
    }
    return yaml.safe_dump(patch, sort_keys=False)


def node_vlan_patch_yaml(
    hostname: str, entry: dict[str, Any], doc: dict[str, Any]
) -> str | None:
    """Per-node patch: 802.1q on the private NIC (vRack) plus its cluster address.

    VLAN 0 is untagged — pin the address on the private MAC. VLAN 1-4000
    puts the cluster address on ``vlans: [{vlanId}]`` and clears the parent
    so we do not keep an untagged IP next to the tagged one.
    Returns None when there is nothing to pin (no MAC, no private IP).
    """
    mac = str(entry.get("private_mac") or "").strip()
    ip = node_cluster_ip(entry, hostname)
    vlan_id = vlan_id_from_doc(doc)
    cidr = private_cidr_from_doc(doc)
    addr = address_with_prefix(ip, cidr) if ip else ""
    if not mac and not (vlan_id > 0 and addr):
        return None
    iface: dict[str, Any] = {"dhcp": False}
    if mac:
        iface["deviceSelector"] = {"hardwareAddr": mac}
    if vlan_id > 0:
        vlan: dict[str, Any] = {"vlanId": vlan_id}
        if addr:
            vlan["addresses"] = [addr]
        iface["vlans"] = [vlan]
        # Parent must not keep the cluster IP untagged. Omit addresses rather
        # than [] — Talos strategic-merge leaves an existing list alone.
        iface.pop("addresses", None)
        kernel = str(entry.get("private_iface") or "").strip()
        if kernel:
            iface["interface"] = kernel
            iface.pop("deviceSelector", None)
    elif addr:
        iface["addresses"] = [addr]
    network: dict[str, Any] = {"hostname": hostname, "interfaces": [iface]}
    return yaml.safe_dump({"machine": {"network": network}}, sort_keys=False)


def firewall_yaml(doc: dict[str, Any]) -> str:
    """Host firewall: default-deny ingress, allow the private fabric.

    Public NIC stays up for outbound (OVH image pull, packages). Dual-NIC
    punches kube-apiserver (6443) and the Talos API (50000) only for
    ``talos.public_management_cidrs`` (the console/fleet host), never
    ``0.0.0.0/0``. Extra ``talos.public_ingress_ports`` merge on top of
    those ports. etcd, kubelet, NodePorts, and the rest stay closed to the
    internet.
    """
    cidrs = cluster_cidrs_from_doc(doc)
    docs: list[dict[str, Any]] = [
        {
            "apiVersion": "v1alpha1",
            "kind": "NetworkDefaultActionConfig",
            "ingress": "block",
        },
        {
            "apiVersion": "v1alpha1",
            "kind": "NetworkRuleConfig",
            "name": "cluster-internal-tcp",
            "portSelector": {"ports": ["1-65535"], "protocol": "tcp"},
            "ingress": [{"subnet": cidr} for cidr in cidrs],
        },
        {
            "apiVersion": "v1alpha1",
            "kind": "NetworkRuleConfig",
            "name": "cluster-internal-udp",
            "portSelector": {"ports": ["1-65535"], "protocol": "udp"},
            "ingress": [{"subnet": cidr} for cidr in cidrs],
        },
    ]
    public_ports = public_ingress_ports_from_doc(doc)
    if fleet_has_private_nic(doc):
        for port in DEFAULT_PUBLIC_MANAGEMENT_PORTS:
            if port not in public_ports:
                public_ports.append(port)
    mgmt = public_management_cidrs_from_doc(doc)
    if public_ports and mgmt:
        docs.append(
            {
                "apiVersion": "v1alpha1",
                "kind": "NetworkRuleConfig",
                "name": "public-ingress-tcp",
                "portSelector": {"ports": public_ports, "protocol": "tcp"},
                "ingress": [{"subnet": cidr} for cidr in mgmt],
            }
        )
    # explicit_start so the first extra document is a new YAML doc when
    # concatenated onto controlplane.yaml / worker.yaml (those files already
    # end with a HostnameConfig document; without --- the kinds merge).
    return yaml.safe_dump_all(docs, sort_keys=False, explicit_start=True)


def talos_pki_markers(workdir: Path) -> list[str]:
    """Return PKI marker filenames that already exist under ``workdir``.

    ``secrets.yaml`` is the cluster secrets bundle; ``talosconfig`` holds the
    matching client credentials. Either file means gen-config --force would
    mint a new identity and orphan the live Talos/etcd PKI.
    """
    root = Path(workdir)
    return [name for name in TALOS_PKI_MARKER_FILES if (root / name).is_file()]


def talos_workdir_bootstrapped(workdir: Path) -> bool:
    """True when ``workdir`` already looks like a bootstrapped Talos cluster."""
    return bool(talos_pki_markers(workdir))


def build_talos_plan(
    doc: dict[str, Any],
    env: Environment,
    settings: Any = None,  # noqa: ARG001 — signature reserved (see render_to_files)
) -> dict[str, Any]:
    """Compute the talos bootstrap plan from the env config document.

    Returns a dict with ``cluster_name``, ``install_disk``, ``workdir``
    (``<config_dir>/talos``), ``kubeconfig`` target path, ``control_planes`` /
    ``workers`` node lists (``{hostname, ip}``), and the ordered ``commands``
    (``{phase, argv}``) for the flow in docs/k8s-talos.md. ``settings`` is
    accepted for symmetry with other services and currently unused.

    Raises :class:`ConfigValidationError` when the doc has no control-plane
    nodes, the install disk is explicitly empty, the env has no
    genestack_config_dir (the talos working dir lives under it), or the
    workdir already holds Talos PKI markers (``secrets.yaml`` /
    ``talosconfig``) — hosts stage must not ``gen config --force`` over live
    cluster identity.
    """
    talos_cfg = doc.get("talos") if isinstance(doc, dict) else None
    if talos_cfg is None:
        talos_cfg = {}
    if not isinstance(talos_cfg, dict):
        raise ConfigValidationError(
            "'talos' must be a mapping of cluster_name/install_disk"
        )

    cluster_name = str(talos_cfg.get("cluster_name") or env.name).strip()
    if not cluster_name:
        raise ConfigValidationError(
            "talos cluster name is empty (set talos.cluster_name)"
        )
    if not _CLUSTER_NAME_RE.match(cluster_name):
        raise ConfigValidationError("talos.cluster_name is invalid")

    install_disk = talos_cfg.get("install_disk")
    if install_disk is None:
        install_disk = DEFAULT_INSTALL_DISK
    install_disk = str(install_disk).strip()
    if not install_disk:
        raise ConfigValidationError(
            "talos.install_disk must not be empty "
            f"(omit it to use the default {DEFAULT_INSTALL_DISK})"
        )
    if ".." in install_disk or not _DISK_RE.match(install_disk):
        raise ConfigValidationError("talos.install_disk is invalid")

    control_planes: list[dict[str, str]] = []
    workers: list[dict[str, str]] = []
    for hostname, entry in _doc_servers(doc).items():
        if not isinstance(entry, dict):
            continue
        node = _node_record(str(hostname), entry)
        roles = [str(role).lower() for role in entry.get("roles") or []]
        if "k8s_control_plane" in roles:
            control_planes.append(node)
        else:
            workers.append(node)
    if not control_planes:
        raise ConfigValidationError(
            "talos bootstrap requires at least one server with role "
            "k8s_control_plane in the config doc servers section"
        )

    if not env.genestack_config_dir:
        raise ConfigValidationError(
            "talos bootstrap requires the env's genestack_config_dir "
            "(working dir <config_dir>/talos)"
        )
    config_dir = Path(env.genestack_config_dir).expanduser()
    workdir = config_dir / "talos"
    if env.kubeconfig_path:
        kubeconfig = Path(env.kubeconfig_path).expanduser()
    else:
        kubeconfig = config_dir / KUBECONFIG_FALLBACK_RELPATH

    # Never `talosctl gen config --force` over an existing cluster.
    # Hosts stage re-entry must not mint new secrets/talosconfig and wipe live PKI.
    existing_pki = talos_pki_markers(workdir)
    if existing_pki:
        markers = ", ".join(existing_pki)
        raise ConfigValidationError(
            "talos workdir already looks bootstrapped "
            f"({markers} under {workdir}); refusing gen-config --force so "
            "hosts stage cannot overwrite live Talos PKI. Remove those files "
            "only if you intentionally want a new cluster identity, or use "
            "talosctl gen config --with-secrets to regenerate machine configs "
            "while preserving secrets"
        )

    first_cp = control_planes[0]["ip"]
    apply_first = control_planes[0]["apply_ip"] or first_cp
    if not valid_node_endpoint(first_cp) or not valid_node_endpoint(apply_first):
        raise ConfigValidationError("talos node address is invalid")
    for node in control_planes + workers:
        ip = node.get("ip") or ""
        apply_ip = node.get("apply_ip") or ip
        if (ip and not valid_node_endpoint(ip)) or (
            apply_ip and not valid_node_endpoint(apply_ip)
        ):
            raise ConfigValidationError(
                f"talos node address for {node.get('hostname') or ip!r} is invalid"
            )
    lock_public = fleet_has_private_nic(doc)
    cidrs = cluster_cidrs_from_doc(doc)
    install_image = install_image_from_doc(doc)
    if not valid_install_image(install_image):
        raise ConfigValidationError("talos.install_image is invalid")
    # talosctl v1.13+ only accepts --nodes/--insecure/--talosconfig AFTER the
    # subcommand (flags before the verb are treated as unknown commands).
    commands: list[dict[str, Any]] = [
        {
            "phase": "write-network-patch",
            "argv": _write_file_argv(NETWORK_PATCH_FILENAME, network_patch_yaml(doc)),
        },
        {
            "phase": "write-etcd-patch",
            "argv": _write_file_argv(ETCD_PATCH_FILENAME, etcd_patch_yaml(doc)),
        },
        {
            "phase": "gen-config",
            "argv": [
                "talosctl",
                "gen",
                "config",
                cluster_name,
                f"https://{first_cp}:6443",
                "--install-disk",
                install_disk,
                "--install-image",
                install_image,
                "--config-patch",
                f"@{NETWORK_PATCH_FILENAME}",
                "--config-patch-control-plane",
                f"@{ETCD_PATCH_FILENAME}",
                "--with-docs=false",
                "--with-examples=false",
                # Never --force. Greenfield workdirs have no files to
                # overwrite; bootstrapped workdirs hard-fail above.
            ],
        },
        {
            # Point talosconfig at an address the console can reach before
            # apply/wait/bootstrap. Cluster endpoint in machine config stays
            # first_cp (private fabric); only the client config uses apply_ip.
            "phase": "endpoints",
            "argv": [
                "talosctl",
                "config",
                "endpoints",
                apply_first,
                "--talosconfig=./talosconfig",
            ],
        },
    ]
    if lock_public:
        commands.append(
            {
                "phase": "write-firewall",
                "argv": _write_file_argv(FIREWALL_FILENAME, firewall_yaml(doc)),
            }
        )
        commands.append(
            {
                "phase": "apply-firewall",
                "argv": [
                    "sh",
                    "-c",
                    "for f in controlplane.yaml worker.yaml; do "
                    'grep -q NetworkDefaultActionConfig "$f" 2>/dev/null '
                    f'|| cat {FIREWALL_FILENAME} >> "$f"; done',
                ],
            }
        )
    servers_doc = _doc_servers(doc)
    vlan_id = vlan_id_from_doc(doc)

    def _apply_for(node: dict[str, str], phase: str, filename: str) -> None:
        entry = servers_doc.get(node["hostname"])
        entry = entry if isinstance(entry, dict) else {}
        patch = node_vlan_patch_yaml(node["hostname"], entry, doc)
        argv = [
            "talosctl",
            "apply-config",
            "--insecure",
            "--nodes",
            node["apply_ip"] or node["ip"],
            "--file",
            filename,
        ]
        if patch:
            patch_name = f"node-{node['hostname']}.yaml"
            commands.append(
                {
                    "phase": "write-node-net",
                    "argv": _write_file_argv(patch_name, patch),
                }
            )
            argv.extend(["--config-patch", f"@{patch_name}"])
        commands.append({"phase": phase, "argv": argv})

    for node in control_planes:
        _apply_for(node, "apply-controlplane", "controlplane.yaml")
    for node in workers:
        _apply_for(node, "apply-worker", "worker.yaml")
    # After apply the node leaves maintenance and reboots. Talk to the public
    # NIC: the console is not on the vRack. talosctl flags must follow the verb.
    apply_q = shlex.quote(apply_first)
    commands.append(
        {
            "phase": "wait-controlplane",
            "argv": [
                "sh",
                "-c",
                "i=0; until talosctl version --nodes "
                f"{apply_q} --talosconfig=./talosconfig >/dev/null 2>&1; do "
                'i=$((i+1)); [ "$i" -gt 90 ] && exit 1; '
                f'echo waiting for Talos API on {apply_q} "($i/90)"; '
                "sleep 10; done",
            ],
        }
    )
    commands.append(
        {
            "phase": "bootstrap",
            "argv": [
                "talosctl",
                "bootstrap",
                "--nodes",
                apply_first,
                "--talosconfig=./talosconfig",
            ],
        }
    )
    commands.append(
        {
            "phase": "kubeconfig",
            "argv": [
                "talosctl",
                "kubeconfig",
                str(kubeconfig),
                "--nodes",
                apply_first,
                "--talosconfig=./talosconfig",
                "--force",
            ],
        }
    )

    return {
        "cluster_name": cluster_name,
        "install_disk": install_disk,
        "workdir": workdir,
        "kubeconfig": kubeconfig,
        "control_planes": control_planes,
        "workers": workers,
        "cluster_cidrs": cidrs,
        "lock_public": lock_public,
        "vlan_id": vlan_id,
        "apply_ip": apply_first,
        "install_image": install_image,
        "commands": commands,
    }


def _executor_file_exists(
    path: Path,
    *,
    ssh_target: str | None,
    agent_env_id: str | None,
) -> bool:
    """True when ``path`` already exists on the executor. Does not log contents."""
    if agent_env_id:
        from app.services import agent_relay

        reply = agent_relay.agent_exec(
            agent_env_id,
            "run_command",
            {"cmd": ["test", "-f", str(path)], "cwd": None, "env": {}, "timeout": 30},
            timeout=30,
            log_cb=None,
        )
        return reply.get("rc") == 0 and not reply.get("error")
    if ssh_target:
        result = bridge.run_command(
            ["test", "-f", str(path)],
            timeout=30,
            dry_run=False,
            ssh_target=ssh_target,
            log=None,
        )
        return result.get("returncode") == 0
    return path.is_file()


def _executor_read_text(
    path: Path,
    *,
    ssh_target: str | None,
    agent_env_id: str | None,
) -> str:
    """Read ``path`` from the executor. The text is not logged."""
    if agent_env_id:
        from app.services import agent_relay

        reply = agent_relay.agent_exec(
            agent_env_id,
            "run_command",
            {"cmd": ["cat", str(path)], "cwd": None, "env": {}, "timeout": 30},
            timeout=30,
            log_cb=None,
        )
        if reply.get("error") or reply.get("rc") not in (0, None):
            return ""
        return str(reply.get("stdout") or "")
    if ssh_target:
        result = bridge.run_command(
            ["cat", str(path)],
            timeout=30,
            dry_run=False,
            ssh_target=ssh_target,
            log=None,
        )
        if result.get("returncode") not in (0, None):
            return ""
        return str(result.get("stdout") or "")
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _retain_created_kubeconfig(
    plan: dict[str, Any],
    env: Environment,
    log: LogFn,
    *,
    ssh_target: str | None,
    agent_env_id: str | None,
    db: Any | None,
) -> None:
    """Store a kubeconfig this run created, then hand the path to the lease.

    A file that already existed is not read and not recorded. The read uses
    no log callback, so the kubeconfig text never enters the job log.
    """
    text = _executor_read_text(
        plan["kubeconfig"],
        ssh_target=ssh_target,
        agent_env_id=agent_env_id,
    )
    apply_ip = str(plan.get("apply_ip") or "")
    cluster_ip = plan["control_planes"][0]["ip"] if plan["control_planes"] else ""
    if text and apply_ip and apply_ip != cluster_ip and (ssh_target or agent_env_id):
        text = _rewrite_kubeconfig_text(text, f"https://{apply_ip}:6443")
    if text.strip():
        try:
            secret_lease.store_kubeconfig(env, text, db)
        except Exception as exc:  # noqa: BLE001 — do not log kubeconfig text
            log(
                "[talos] WARNING could not store kubeconfig on the environment: "
                f"{type(exc).__name__}"
            )
    secret_lease.remember(
        plan["kubeconfig"],
        agent_env_id=agent_env_id,
        ssh_target=ssh_target,
        config_dir=plan["workdir"].parent,
        log_fn=log,
    )


def log_talos_notes(log: LogFn | None) -> None:
    """Surface the docs/k8s-talos.md caveats (advisory, never enforced)."""
    if log is None:
        return
    log(
        f"[talos] note: pin kube-ovn to {KUBE_OVN_TALOS_PIN} in "
        "helm-chart-versions.yaml for talos (docs/k8s-talos.md)"
    )
    log(
        "[talos] note: nodes must boot a Talos Image Factory image with "
        f"{' + '.join(FACTORY_IMAGE_EXTENSIONS)} extensions (longhorn needs them)"
    )


def run_talos_bootstrap(
    doc: dict[str, Any],
    env: Environment,
    log: LogFn,
    *,
    dry_run: bool,
    timeout: int,
    extra_env: dict[str, str] | None = None,
    ssh_target: str | None = None,
    remote_env: dict[str, str] | None = None,
    agent_env_id: str | None = None,
    db: Any | None = None,
) -> dict[str, Any]:
    """Run the talos bootstrap plan over the agent/ssh executor via the bridge.

    Every command runs from the plan's workdir (``<config_dir>/talos``) and
    stops at the first failure, naming the phase. dry_run logs every command
    and executes nothing. Raises :class:`ConfigValidationError` when the plan
    cannot be built (caller maps that to a returncode=2 result).
    """
    plan = build_talos_plan(doc, env)
    cp_count = len(plan["control_planes"])
    worker_count = len(plan["workers"])
    log(
        f"[talos] cluster={plan['cluster_name']} disk={plan['install_disk']} "
        f"control_planes={cp_count} workers={worker_count} dry_run={dry_run}"
    )
    cidrs = plan.get("cluster_cidrs") or list(DEFAULT_CLUSTER_CIDRS)
    log(f"[talos] cluster fabric CIDRs={','.join(cidrs)} (kubelet/etcd bind here)")
    vlan_id = plan.get("vlan_id") or 0
    if vlan_id:
        log(
            f"[talos] private NIC carries 802.1q VLAN {vlan_id} "
            "(vRack interconnect — untagged parent, tagged cluster fabric)"
        )
    if plan.get("lock_public"):
        ports = ",".join(str(p) for p in DEFAULT_PUBLIC_MANAGEMENT_PORTS)
        mgmt = public_management_cidrs_from_doc(doc)
        if mgmt:
            log(
                "[talos] dual-NIC: apply-config uses the public NIC in maintenance "
                "mode; cluster fabric is private. Public ingress default-deny "
                f"except tcp {ports} from {','.join(mgmt)}"
            )
        else:
            log(
                "[talos] dual-NIC: apply-config uses the public NIC in maintenance "
                "mode; cluster fabric is private. Public 6443/50000 stay closed "
                "until talos.public_management_cidrs is set (never 0.0.0.0/0)"
            )
        for node in plan["control_planes"] + plan["workers"]:
            if node.get("private_ip") and node.get("apply_ip") != node.get("ip"):
                log(
                    f"[talos] {node['hostname']}: apply={node.get('apply_ip')} "
                    f"cluster={node.get('ip')} public={node.get('public_ip') or '-'}"
                )
    else:
        log(
            "[talos] no private_ip on inventory — not locking the public NIC. "
            "Set private_ip (vRack) on each host so the cluster fabric is not "
            "exposed to the internet"
        )
    log_talos_notes(log)

    if not dry_run:
        plan["workdir"].mkdir(parents=True, exist_ok=True)
        plan["kubeconfig"].parent.mkdir(parents=True, exist_ok=True)
        log(f"[talos] workdir {plan['workdir']}")
    # A file that already exists belongs to the operator. A file this run
    # creates, including on the deploy host over SSH or the agent, is stored
    # and removed when the job ends.
    kube_existed = True
    if not dry_run:
        kube_existed = _executor_file_exists(
            plan["kubeconfig"],
            ssh_target=ssh_target,
            agent_env_id=agent_env_id,
        )

    base: dict[str, Any] = {
        "cluster_name": plan["cluster_name"],
        "install_disk": plan["install_disk"],
        "control_planes": cp_count,
        "workers": worker_count,
        "dry_run": dry_run,
    }
    phases_completed = 0
    total = len(plan["commands"])
    failed: dict[str, Any] | None = None
    try:
        for command in plan["commands"]:
            result = bridge.run_command(
                [str(a) for a in command["argv"]],
                cwd=plan["workdir"],
                timeout=timeout,
                dry_run=dry_run,
                extra_env=extra_env,
                ssh_target=ssh_target,
                remote_env=remote_env,
                agent_env_id=agent_env_id,
                log=log,
            )
            rc = result.get("returncode")
            if rc not in (0, None) and not result.get("dry_run"):
                phase = command["phase"]
                log(f"[talos] FAILED at phase '{phase}' rc={rc} — stopping")
                failed = {
                    **base,
                    "ok": False,
                    "error": f"talos bootstrap failed at phase '{phase}' (rc={rc})",
                    "returncode": rc,
                    "failed_phase": phase,
                    "phases_completed": phases_completed,
                    "phases_total": total,
                }
                break
            phases_completed += 1

        if failed is None and not dry_run and not agent_env_id and not ssh_target:
            apply_ip = str(plan.get("apply_ip") or "")
            cluster_ip = (
                plan["control_planes"][0]["ip"] if plan["control_planes"] else ""
            )
            if apply_ip and apply_ip != cluster_ip:
                server = f"https://{apply_ip}:6443"
                if rewrite_kubeconfig_server(plan["kubeconfig"], server):
                    log(
                        f"[talos] kubeconfig server rewritten to {server} "
                        "(console is not on the private fabric)"
                    )
    finally:
        if not dry_run and not kube_existed:
            _retain_created_kubeconfig(
                plan,
                env,
                log,
                ssh_target=ssh_target,
                agent_env_id=agent_env_id,
                db=db,
            )
    if failed is not None:
        return failed
    log(
        f"[talos] bootstrap complete: {phases_completed}/{total} phases, "
        f"kubeconfig at {plan['kubeconfig']}"
    )
    return {
        **base,
        "ok": True,
        "returncode": 0,
        "failed_phase": None,
        "phases_completed": phases_completed,
        "phases_total": total,
        "kubeconfig": str(plan["kubeconfig"]),
        "message": (
            f"talos cluster {plan['cluster_name']} bootstrapped "
            f"({cp_count} cp, {worker_count} worker)"
        ),
    }
