"""Environment config document (Phase 6, Milestone B).

Each environment's entire config lives in the console DB as one flat,
versioned YAML document (``EnvConfigVersion`` rows). The document is the
source of truth; ``render_to_files`` maps it onto the deploy host's
``/etc/genestack`` tree and ``push_rendered`` writes those files (locally or
over the ssh executor), backing up pre-existing files first.

Documented top-level schema (loose validation — unknown keys warn, not reject).
A leftover ``maas:`` block in an older file is ignored and dropped on the next save.
Older server rows may still say ``source: maas``; they still load::

    provider: kubespray
    deploy: {ssh_host, ssh_user, ssh_password, dry_run}  # push syncs ssh_host and ssh_user onto the Environment row (ssh_password encrypted, masked on read)
    servers:                                   # role assignments, hostname keyed
      <hostname>: {system_id: <id|null>, ip, ssh_user,
                   roles: [k8s_control_plane|etcd|control|compute|network|
                           storage|storage-ceph|storage-cinder],
                   source: static|baremetal|ovh|terraform,
                   service_name: <ovh internal name|null>,
                   public_ip, private_ip, private_mac, vrack_vni>}
    ovh: {vrack, vlan_id, private_cidr}        # dedicated/Rise fabric (802.1q on private NIC)
    network: {gateway_domain, acme_email, hyperconverged,
              container_interface,  # kube-ovn IFACE; Talos: VLAN iface with the private IP (e.g. enp3934127.100), not the untagged parent
              compute_interface,    # must NOT be the public / default-route NIC
              ovn: {external_interface, ...},  # br-ex uplink; never the public NIC (empty does not fall back to compute_interface)
              metallb_pools: [...], ovn_bridge_mappings: [...]}
    storage: {cinder_backend_name, cinder_worker_name, ceph: {enabled, external_pvc}}
    group_vars:                                # ansible inventory group_vars
      <group>: {<var>: <value>, ...}           # group names: ^[a-z][a-z0-9_]*$
    components: {keystone: true, glance: false, ...}
    chart_versions: {keystone: "2026.1.8+…", ...}
    talos: {cluster_name, install_disk,      # provider=talos bootstrap (services/talos.py)
            image_url, install_image}        # factory image + installer for apply-config
    pxe: {interface, range_start, range_end, # console-owned DHCP/PXE (services/pxe.py);
          netmask, gateway, dns,             # interface/range_start/range_end required
          next_server, http_port, image_url} # when the section is present
    helm_overrides: {<service>: <inline yaml mapping>, global: {...}}
    kustomize_patches: {<service>: [<patch yaml docs>]}
    secrets:                                 # encrypted at rest (fernet:), masked on read
      <secret name>:                         # dns-1123: ^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$
        namespace: openstack                 # optional, default "openstack"
        data: {<key>: <plaintext value>}     # "***" on write keeps the stored value

Rendered-file mapping (only sections present in the doc produce files —
absent sections never wipe existing files):

    provider          -> provider
    servers           -> inventory/inventory.yaml      (genestack/kubespray group layout,
                                                        via inventory.py; deployer excluded)
    components        -> openstack-components.yaml     (wrapped in a top-level components: map)
    chart_versions    -> helm-chart-versions.yaml      (wrapped in a top-level charts: map)
    helm_overrides    -> helm-configs/<svc>/console-rendered.yaml
                         helm-configs/global_overrides/console-rendered.yaml  (the "global" key)
    kustomize_patches -> kustomize/<svc>/overlay/patches.yaml (multi-doc YAML for lists)
                         kustomize/<svc>/overlay/kustomization.yaml
                         Kustomization referencing ../base + patches.yaml.
                         This overwrites the bootstrap.sh-created stub with a
                         superset — safe because the stub is exactly
                         ``resources: [../base]`` (push backs it up first).
    group_vars        -> inventory/group_vars/<group>/console-rendered.yml
                         One file per group. Genestack's own tree
                         (ansible/inventory/genestack/group_vars, copied to
                         /etc/genestack/inventory by bootstrap.sh) has no
                         single per-group filename convention — all/ carries
                         eight files (all.yml, docker.yml, …), k8s_cluster/
                         uses a dashed k8s-cluster.yml, and etcd is a
                         top-level etcd.yml *file*, not a directory — so the
                         the console uses its own name for every group. Safe
                         because ansible loads every *.yml in a group dir
                         (and merges a same-named dir with etcd.yml).
    network           -> nothing rendered; its keys flow to pipeline/install
                         commands as env vars instead (see doc_env).
    pxe               -> nothing rendered here; consumed by app/services/pxe.py
                         and pxe_runtime.py, which serve dnsmasq.conf, boot.ipxe,
                         and Talos assets from <data_dir>/pxe in-process.
     storage           -> the cinder_* keys (cinder_backend_name,
                          cinder_worker_name) merge into
                          inventory/group_vars/cinder_storage_nodes/console-rendered.yml,
                          mirroring the example inventory's cinder_storage_nodes
                          group vars; explicit group_vars.cinder_storage_nodes
                          entries win on a key conflict. ceph: {enabled, …} renders
                          kustomize/rook-ceph/overlay/kustomization.yaml, a
                          kustomize overlay that references the repo's upstream
                          rook trees (base-kustomize/rook-defaults, rook-operator,
                          rook-cluster — the *-external-pvc variants when
                          external_pvc is set), which bootstrap.sh symlinks into
                          the config dir the same way it links keystone/base.
                          ceph.enabled: false (or no ceph section) renders no files.
    secrets           -> kubesecrets.yaml     (multi-doc v1/Secret manifests,
                         matching bin/create-secrets.sh's shape; values
                         base64-encoded plaintext like the generated file)

The ``console-rendered.yaml`` filename keeps this output separate from
hand-maintained files (helm picks up every file in the directory).

``kubesecrets.yaml`` is an exception to the plain-overwrite rule: genestack's
bin/create-secrets.sh refuses to regenerate it ("Reusing existing secrets
file to avoid mass rotation"), so push_rendered merges instead — existing
Secret entries survive, console entries win only on a name conflict.

``helm-chart-versions.yaml`` is the other exception: bootstrap copies the
repo's full chart set (100+ charts) into the config dir and later pipeline
stages extract versions from it, so push_rendered merges the doc's
``chart_versions:`` pins onto the destination file per chart name (doc wins
per key, all other charts preserved). When the destination file is absent,
the doc-only partial render is written with a job-log warning to bootstrap
first. The render preview (GET /config/render) still shows the doc-only
render — the merge happens at push time.
"""

from __future__ import annotations

import base64
import os
import re
import shlex
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import yaml
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import EnvConfigVersion, Environment
from app.services import genestack_bridge as bridge
from app.services import secret_lease
from app.services.crypto import decrypt_secret, encrypt_secret
from app.services.envcontext import EnvContext
from app.services.executors import pick_executor
from app.services.inventory import SERVER_ROLE_GROUPS, build_inventory_from_environment

LogFn = Callable[[str], None]

KNOWN_TOP_LEVEL_KEYS = frozenset(
    {
        "provider",
        "deploy",
        "maas",
        "servers",
        "network",
        "storage",
        "group_vars",
        "components",
        "chart_versions",
        "helm_overrides",
        "kustomize_patches",
        "secrets",
        "talos",
        "pxe",
        "ovh",
    }
)

# network: keys doc_env() maps onto env vars for genestack's
# bin/setup-infrastructure.sh; anything else warns (loose validation).
# container_interface is kube-ovn IFACE (Talos: the VLAN iface that holds
# the private IP, e.g. enp3934127.100, not the untagged parent).
# compute_interface and ovn.external_interface must never be the public /
# default-route NIC — empty OVN_EXTERNAL_INTERFACE leaves br-ex unported
# rather than falling back to COMPUTE_INTERFACE (that enslaved public
# eno1np0 into br-ex and killed Talos :50000 / kube-apiserver :6443).
# metallb_pools / ovn_bridge_mappings are known but rendered elsewhere.
KNOWN_NETWORK_KEYS = frozenset(
    {
        "gateway_domain",
        "acme_email",
        "hyperconverged",
        "container_interface",
        "compute_interface",
        "ovn",
        "metallb_pools",
        "ovn_bridge_mappings",
    }
)

# storage: keys render_to_files() maps onto cinder_storage_nodes group vars
# (cinder_*) or rook kustomize overlays (ceph); anything else warns (loose
# validation).
KNOWN_STORAGE_KEYS = frozenset(
    {
        "cinder_backend_name",
        "cinder_worker_name",
        "ceph",
    }
)

# storage.ceph: known keys; unknown sub-keys warn (loose validation).
# "enabled" turns the rook overlay on/off; "external_pvc" selects the
# *-external-pvc upstream trees (cluster-on-PVC / no host devices) instead of
# the host-device defaults.
KNOWN_CEPH_KEYS = frozenset(
    {
        "enabled",
        "external_pvc",
    }
)

# Rook kustomize overlay rendered under kustomize/rook-ceph/overlay — mirrors
# the kustomize_patches convention: bootstrap.sh symlinks every
# base-kustomize/<svc>/base into <config dir>/kustomize/<svc>/base, so the
# overlay references the upstream trees via ../<svc>/base. The actual tree
# files (rook-operator crds/common/operator, rook-defaults filesystem +
# storageclasses, rook-cluster CephCluster + toolbox) are hand-maintained
# upstream and are never rendered into the doc.
_ROOK_KUSTOMIZE_SERVICE = "rook-ceph"
_ROOK_BASE_TREES = ("rook-operator", "rook-defaults", "rook-cluster")
_ROOK_EXTERNAL_PVC_BASE_TREES = (
    "rook-operator",
    "rook-defaults-external-pvc",
    "rook-cluster-external-pvc",
)


def _rook_overlay_kustomization(external_pvc: bool = False) -> str:
    trees = _ROOK_EXTERNAL_PVC_BASE_TREES if external_pvc else _ROOK_BASE_TREES
    resources = "\n".join(f"  - ../{tree}/base" for tree in trees)
    return (
        "apiVersion: kustomize.config.k8s.io/v1beta1\n"
        "kind: Kustomization\n"
        "resources:\n"
        f"{resources}\n"
    )


# talos: settings for the provider=talos bootstrap flow (app/services/talos.py);
# image_url is the Talos factory image URL (OVH BYOI and the PXE assets);
# efi_bootloader_path is the OVH BYOI EFI path (default \EFI\BOOT\BOOTX64.EFI).
KNOWN_TALOS_KEYS = frozenset(
    {
        "cluster_name",
        "install_disk",
        "image_url",
        "install_image",
        "efi_bootloader_path",
        "cluster_cidrs",
        "public_ingress_ports",
        "public_management_cidrs",
        "allow_public_world",
    }
)

# ovh: vRack fabric for dedicated/Rise dual-NIC (attach + 802.1q on the private NIC).
KNOWN_OVH_KEYS = frozenset({"vrack", "vlan_id", "private_cidr"})
TALOS_LIST_KEYS = frozenset(
    {"cluster_cidrs", "public_ingress_ports", "public_management_cidrs"}
)

# Helm values kube-ovn needs on Talos (read-only /etc). Docs: k8s-cni-kube-ovn.md
# and scripts/hyperconverged-lab-talos.sh. Without these, CNI pods fail with
# mkdir /etc/origin: read-only file system.
TALOS_KUBE_OVN_HELM = {
    "OPENVSWITCH_DIR": "/var/lib/openvswitch",
    "OVN_DIR": "/var/lib/ovn",
    "DISABLE_MODULES_MANAGEMENT": True,
    "cni_conf": {"MOUNT_LOCAL_BIN_DIR": False},
    # Talos kube-apiserver default service CIDR is 10.96.0.0/12. Genestack's
    # kube-ovn base chart uses kubespray 10.233.0.0/18; mismatch breaks DNS.
    "ipv4": {"SVC_CIDR": "10.96.0.0/12"},
}

# pxe: console-owned DHCP/PXE provisioning (app/services/pxe.py and
# pxe_runtime.py, in-process). interface/range_start/range_end are required
# when the section is present; the rest are optional.
# Unknown keys warn (loose validation).
KNOWN_PXE_KEYS = frozenset(
    {
        "interface",
        "range_start",
        "range_end",
        "netmask",
        "gateway",
        "dns",
        "next_server",
        "http_port",
        "image_url",
    }
)
REQUIRED_PXE_KEYS = frozenset({"interface", "range_start", "range_end"})

VALID_SERVER_ROLES = frozenset(SERVER_ROLE_GROUPS)

# Filename used for console-rendered output inside existing config dirs
RENDERED_FILENAME = "console-rendered.yaml"

# group_vars files use the .yml extension, matching genestack's own
# ansible/inventory/genestack/group_vars tree (all.yml, k8s-cluster.yml, …)
GROUP_VARS_RENDERED_FILENAME = "console-rendered.yml"

# Ansible inventory group names (all, k8s_cluster, etcd, …) — also path-safe
_GROUP_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")

# OVN_* env var names derived from network.ovn sub-keys (doc_env). Keys are
# interpolated unquoted into the remote shell string, so only plain
# identifiers are allowed to become OVN_<KEY> variables.
_ENV_VAR_NAME_RE = re.compile(r"[a-z][a-z0-9_]*")

# Overlay kustomization rendered next to patches.yaml. Superset of the stub
# bootstrap.sh creates (resources: [../base] only), so overwriting is safe.
_OVERLAY_KUSTOMIZATION = """\
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - ../base
patches:
  - path: patches.yaml
"""

# Service-name keys become path segments — keep them strictly path-safe
_SERVICE_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")

# Server inventory is keyed by hostname — same path-safe character set
_HOSTNAME_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")

VALID_SERVER_SOURCES = frozenset({"maas", "static", "baremetal", "ovh", "terraform"})

# provider: deployment provider for the env (doc "provider" key)
VALID_PROVIDERS = frozenset({"kubespray", "talos"})

# deploy: push host connection settings (ssh_host/ssh_user/ssh_password/dry_run);
# unknown keys warn (loose validation). ssh_password is encrypted at rest and
# masked on read (see mask_document / put_version).
KNOWN_DEPLOY_KEYS = frozenset({"ssh_host", "ssh_user", "ssh_password", "dry_run"})

# secrets: section — kubernetes Secret names (dns-1123 subdomain)
SECRET_NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$")
KNOWN_SECRET_ENTRY_KEYS = frozenset({"namespace", "data"})
DEFAULT_SECRET_NAMESPACE = "openstack"

# Sentinel returned in place of stored secret values; sending it back on PUT
# means "keep the stored value for this key" (mirrors MASKED_KUBECONFIG_DATA).
SECRET_MASK = "***"

# Rendered filename matching genestack's bin/create-secrets.sh output
KUBESECRETS_FILENAME = "kubesecrets.yaml"

# Rendered filename matching the repo's chart versions file bootstrap copies
# to /etc/genestack (top-level "charts:" mapping of chart name -> version)
CHART_VERSIONS_FILENAME = "helm-chart-versions.yaml"

# Manifest written by push_rendered: the exact set of files the last push
# produced, so the next push can prune what the document no longer renders.
MANIFEST_FILENAME = ".genestack-manifest.yaml"


class ConfigConflictError(ValueError):
    """The expected config version is no longer current."""


class ConfigValidationError(ValueError):
    """The config document is invalid (bad YAML, wrong shape, unsafe keys)."""


def _log(log: LogFn | None, msg: str) -> None:
    if log:
        log(msg)


def _clean_for_dump(data: Any) -> Any:
    """Drop null server fields so the YAML is not a wall of empty keys."""
    if not isinstance(data, dict):
        return data
    out = dict(data)
    servers = out.get("servers")
    if isinstance(servers, dict):
        cleaned: dict[str, Any] = {}
        for host, entry in servers.items():
            if not isinstance(entry, dict):
                cleaned[host] = entry
                continue
            # Keep system_id and ssh_auth_method even when None (required for round-trip)
            cleaned[host] = {
                k: v
                for k, v in entry.items()
                if v is not None or k in ("system_id", "ssh_auth_method")
            }
        out["servers"] = cleaned
    return out


def _dump(data: Any) -> str:
    return yaml.safe_dump(
        _clean_for_dump(data), sort_keys=False, default_flow_style=False
    )


# ---------------------------------------------------------------------------
# Parsing / validation
# ---------------------------------------------------------------------------


def _normalize_servers(servers: dict[Any, Any], warnings: list[str]) -> dict[str, Any]:
    """Normalize the ``servers:`` section to hostname-keyed entries.

    Current shape (per hostname key): ``{system_id, ip, ssh_user, ssh_auth_method,
    ssh_password, roles, source, service_name}`` with source "static",
    "baremetal", "ovh", or "terraform". Older records may still say "maas".
    Legacy documents keyed by system id (entries carrying a ``hostname`` field
    and no ``source``) are rewritten on read: the hostname becomes the key and the
    old key is kept as ``system_id``. When ``source`` is absent and a system_id
    is present, the stored source stays "maas" so an older document still loads.
    """
    normalized: dict[str, Any] = {}
    for key, assignment in servers.items():
        if not isinstance(assignment, dict):
            raise ConfigValidationError(f"servers.{key} must be a mapping")
        entry = dict(assignment)
        if "source" not in entry and "system_id" not in entry and entry.get("hostname"):
            # Legacy keying: the mapping key is a system id.
            hostname = str(entry.pop("hostname"))
            entry["system_id"] = key
            entry["source"] = "maas"
        else:
            hostname = str(entry.pop("hostname", None) or key)
            if "source" not in entry:
                entry["source"] = "maas" if entry.get("system_id") else "static"
        if entry["source"] not in VALID_SERVER_SOURCES:
            raise ConfigValidationError(
                f"servers.{hostname}: source must be one of {', '.join(sorted(VALID_SERVER_SOURCES))}"
            )
        # Optional keys stay absent when unset so the YAML is not a wall of nulls.
        if entry.get("service_name") is not None and not isinstance(
            entry.get("service_name"), str
        ):
            warnings.append(
                f"servers.{hostname}: service_name must be a string (value ignored)"
            )
        if not _HOSTNAME_KEY_RE.match(hostname):
            raise ConfigValidationError(
                f"servers: invalid hostname '{hostname}' "
                f"(must match {_HOSTNAME_KEY_RE.pattern})"
            )
        for role in entry.get("roles") or []:
            if str(role).lower() not in VALID_SERVER_ROLES:
                raise ConfigValidationError(
                    f"servers.{hostname}: unknown role '{role}' "
                    f"(valid: {', '.join(sorted(VALID_SERVER_ROLES))})"
                )
        if hostname in normalized:
            warnings.append(
                f"servers: duplicate hostname '{hostname}' after normalization; keeping first"
            )
            continue
        # Ensure ssh_auth_method is always present (None if unset)
        if "ssh_auth_method" not in entry:
            entry["ssh_auth_method"] = None
        normalized[hostname] = entry
    return normalized


def parse_document(yaml_text: str) -> tuple[dict[str, Any], list[str]]:
    """Parse and loosely validate a config document.

    Returns ``(doc, warnings)``. Raises :class:`ConfigValidationError` on
    unparseable YAML or a non-mapping top level. Unknown top-level keys
    produce warnings, not rejection. The ``servers:`` section is normalized
    to hostname-keyed entries (see :func:`_normalize_servers`). ``group_vars:``
    group names must match ``^[a-z][a-z0-9_]*$`` and hold mappings; unknown
    ``network:`` and ``storage:`` keys warn.
    """
    try:
        data = yaml.safe_load(yaml_text)
    except yaml.YAMLError as exc:
        raise ConfigValidationError(
            f"YAML parse error at {getattr(exc, 'problem_mark', 'unknown location')}: {exc.problem}. "
            f"Check indentation, quotes, and special characters around that location."
        ) from exc
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ConfigValidationError(
            "Config document must be a YAML mapping at the top level"
        )
    warnings = [
        f"Unknown top-level key '{key}' (ignored on render)"
        for key in data
        if key not in KNOWN_TOP_LEVEL_KEYS
    ]
    servers = data.get("servers")
    if servers is not None:
        if not isinstance(servers, dict):
            raise ConfigValidationError(
                "'servers' must be a mapping of hostname -> assignment"
            )
        data["servers"] = _normalize_servers(servers, warnings)
    group_vars = data.get("group_vars")
    if group_vars is not None:
        if not isinstance(group_vars, dict):
            raise ConfigValidationError(
                "'group_vars' must be a mapping of group -> variables"
            )
        for group, variables in group_vars.items():
            name = str(group)
            if not _GROUP_NAME_RE.match(name):
                raise ConfigValidationError(
                    f"group_vars: invalid group name '{name}' "
                    f"(must match {_GROUP_NAME_RE.pattern})"
                )
            if not isinstance(variables, dict):
                raise ConfigValidationError(
                    f"group_vars.{name} must be a mapping of var -> value"
                )
    network = data.get("network")
    if isinstance(network, dict):
        warnings.extend(
            f"network: unknown key '{key}' (ignored)"
            for key in network
            if key not in KNOWN_NETWORK_KEYS
        )
    storage = data.get("storage")
    if storage is not None:
        if not isinstance(storage, dict):
            raise ConfigValidationError(
                "'storage' must be a mapping of backend settings"
            )
        warnings.extend(
            f"storage: unknown key '{key}' (ignored)"
            for key in storage
            if key not in KNOWN_STORAGE_KEYS
        )
        ceph = storage.get("ceph")
        if ceph is not None:
            if not isinstance(ceph, dict):
                raise ConfigValidationError("storage.ceph must be a mapping")
            warnings.extend(
                f"storage.ceph: unknown key '{key}' (ignored)"
                for key in ceph
                if key not in KNOWN_CEPH_KEYS
            )
            for key in KNOWN_CEPH_KEYS:
                if ceph.get(key) is not None and not isinstance(ceph[key], bool):
                    raise ConfigValidationError(f"storage.ceph.{key} must be a boolean")
    talos = data.get("talos")
    if talos is not None:
        if not isinstance(talos, dict):
            raise ConfigValidationError(
                "'talos' must be a mapping of cluster_name/install_disk/image_url"
            )
        warnings.extend(
            f"talos: unknown key '{key}' (ignored)"
            for key in talos
            if key not in KNOWN_TALOS_KEYS
        )
        for key in KNOWN_TALOS_KEYS:
            if talos.get(key) is None:
                continue
            if key in TALOS_LIST_KEYS:
                if not isinstance(talos[key], list):
                    raise ConfigValidationError(f"talos.{key} must be a list")
            elif not isinstance(talos[key], str):
                raise ConfigValidationError(f"talos.{key} must be a string")
    ovh = data.get("ovh")
    if ovh is not None:
        if not isinstance(ovh, dict):
            raise ConfigValidationError(
                "'ovh' must be a mapping of vrack/vlan_id/private_cidr"
            )
        warnings.extend(
            f"ovh: unknown key '{key}' (ignored)"
            for key in ovh
            if key not in KNOWN_OVH_KEYS
        )
        if ovh.get("vrack") is not None and not isinstance(ovh["vrack"], str):
            raise ConfigValidationError("ovh.vrack must be a string")
        if ovh.get("private_cidr") is not None and not isinstance(
            ovh["private_cidr"], str
        ):
            raise ConfigValidationError("ovh.private_cidr must be a string")
        vlan_id = ovh.get("vlan_id")
        if vlan_id is not None:
            if not isinstance(vlan_id, int) or isinstance(vlan_id, bool):
                raise ConfigValidationError(
                    "ovh.vlan_id must be an integer (0 = untagged, 1-4000 = 802.1q)"
                )
            if vlan_id < 0 or vlan_id > 4000:
                raise ConfigValidationError("ovh.vlan_id must be between 0 and 4000")
    pxe = data.get("pxe")
    if pxe is not None:
        if not isinstance(pxe, dict):
            raise ConfigValidationError(
                "'pxe' must be a mapping of interface/range_start/range_end/…"
            )
        missing = [key for key in sorted(REQUIRED_PXE_KEYS) if not pxe.get(key)]
        if missing:
            raise ConfigValidationError(
                f"pxe: missing required key(s): {', '.join(missing)}"
            )
        warnings.extend(
            f"pxe: unknown key '{key}' (ignored)"
            for key in pxe
            if key not in KNOWN_PXE_KEYS
        )
        for key in (
            "interface",
            "range_start",
            "range_end",
            "netmask",
            "gateway",
            "dns",
            "next_server",
            "image_url",
        ):
            if pxe.get(key) is not None and not isinstance(pxe[key], str):
                raise ConfigValidationError(f"pxe.{key} must be a string")
        http_port = pxe.get("http_port")
        if http_port is not None and not isinstance(http_port, int):
            raise ConfigValidationError("pxe.http_port must be an integer")
    secrets = data.get("secrets")
    if secrets is not None:
        if not isinstance(secrets, dict):
            raise ConfigValidationError(
                "'secrets' must be a mapping of secret name -> entry"
            )
        for key, entry in secrets.items():
            name = str(key)
            if not SECRET_NAME_RE.match(name):
                raise ConfigValidationError(
                    f"secrets: invalid secret name '{name}' "
                    f"(must match {SECRET_NAME_RE.pattern})"
                )
            if not isinstance(entry, dict):
                raise ConfigValidationError(f"secrets.{name} must be a mapping")
            warnings.extend(
                f"secrets.{name}: unknown key '{entry_key}' (ignored)"
                for entry_key in entry
                if entry_key not in KNOWN_SECRET_ENTRY_KEYS
            )
            namespace = entry.get("namespace")
            if namespace is not None and not isinstance(namespace, str):
                raise ConfigValidationError(
                    f"secrets.{name}.namespace must be a string"
                )
            secret_data = entry.get("data")
            if not isinstance(secret_data, dict):
                raise ConfigValidationError(
                    f"secrets.{name}.data must be a mapping of key -> value"
                )
            for data_key, value in secret_data.items():
                if not isinstance(value, str):
                    raise ConfigValidationError(
                        f"secrets.{name}.data.{data_key} must be a string"
                    )
    return data, warnings


# ---------------------------------------------------------------------------
# Secrets at rest (encrypt on store, mask on read)
# ---------------------------------------------------------------------------


def _resolve_secret_sentinels(
    doc: dict[str, Any], previous_doc: dict[str, Any] | None
) -> None:
    """Replace SECRET_MASK values in ``doc`` with the previously stored values.

    Per-key mirror of the masked-field pattern in routers/environments.py:
    a data value equal to the sentinel keeps the stored (already encrypted)
    value for that key. A sentinel with no previous value is stored as a
    literal (same tradeoff as the existing masked fields).
    """
    secrets = doc.get("secrets")
    if not isinstance(secrets, dict):
        return
    previous_secrets = (previous_doc or {}).get("secrets") or {}
    for name, entry in secrets.items():
        previous_data = (previous_secrets.get(name) or {}).get("data") or {}
        for data_key, value in (entry.get("data") or {}).items():
            if value == SECRET_MASK and data_key in previous_data:
                entry["data"][data_key] = previous_data[data_key]


def _encrypt_document_secrets(doc: dict[str, Any]) -> None:
    """Encrypt every value under secrets.*.data in place (idempotent)."""
    secrets = doc.get("secrets")
    if not isinstance(secrets, dict):
        return
    for entry in secrets.values():
        data = entry.get("data") if isinstance(entry, dict) else None
        if isinstance(data, dict):
            for data_key, value in data.items():
                if isinstance(value, str):
                    data[data_key] = encrypt_secret(value)


def _encrypt_deploy_password(
    doc: dict[str, Any], previous_doc: dict[str, Any] | None
) -> bool:
    """Encrypt ``deploy.ssh_password`` in place, like a ``secrets:`` value.

    A value equal to SECRET_MASK keeps the previously stored (already
    encrypted) password; ``encrypt_secret`` is idempotent, so re-PUTs of an
    already fernet:-prefixed value are safe. Returns True when the doc changed.
    """
    deploy = doc.get("deploy")
    if not isinstance(deploy, dict):
        return False
    pwd = deploy.get("ssh_password")
    if not isinstance(pwd, str) or not pwd:
        return False
    if pwd == SECRET_MASK:
        previous = (previous_doc or {}).get("deploy") or {}
        prev_pwd = previous.get("ssh_password")
        if prev_pwd:
            deploy["ssh_password"] = prev_pwd
            return True
        return False
    deploy["ssh_password"] = encrypt_secret(pwd)
    return True


def _drop_ignored_maas(doc: dict[str, Any]) -> bool:
    """Drop a leftover ``maas:`` block. Returns True when the doc changed.

    Older documents may still contain that key. It is not used. Dropping it
    on write keeps the credential out of the next stored version.
    """
    if "maas" not in doc:
        return False
    doc.pop("maas", None)
    return True


def _resolve_server_password_sentinels(
    doc: dict[str, Any], previous_doc: dict[str, Any] | None
) -> None:
    """Replace SECRET_MASK ssh_password values with previously stored values."""
    servers = doc.get("servers")
    if not isinstance(servers, dict):
        return
    previous_servers = (previous_doc or {}).get("servers") or {}
    for hostname, entry in servers.items():
        if not isinstance(entry, dict):
            continue
        pwd = entry.get("ssh_password")
        if pwd == SECRET_MASK:
            previous_entry = previous_servers.get(hostname) or {}
            prev_pwd = previous_entry.get("ssh_password")
            if prev_pwd:
                entry["ssh_password"] = prev_pwd


def _encrypt_server_passwords(doc: dict[str, Any]) -> bool:
    """Encrypt every server's ssh_password in place. Returns True when changed."""
    servers = doc.get("servers")
    if not isinstance(servers, dict):
        return False
    changed = False
    for entry in servers.values():
        if not isinstance(entry, dict):
            continue
        pwd = entry.get("ssh_password")
        if isinstance(pwd, str) and pwd and pwd != SECRET_MASK:
            entry["ssh_password"] = encrypt_secret(pwd)
            changed = True
    return changed


def mask_document(doc: dict[str, Any]) -> dict[str, Any]:
    """Copy of ``doc`` with secrets, server ssh_passwords, and
    deploy.ssh_password masked. A leftover ``maas:`` block is omitted.

    Returns ``doc`` itself when there is nothing to mask.
    """
    secrets = doc.get("secrets")
    drop_maas = "maas" in doc
    deploy = doc.get("deploy")
    mask_deploy = isinstance(deploy, dict) and bool(deploy.get("ssh_password"))
    servers = doc.get("servers")
    mask_servers = isinstance(servers, dict) and any(
        isinstance(e, dict) and e.get("ssh_password") for e in servers.values()
    )
    if (
        not isinstance(secrets, dict)
        and not drop_maas
        and not mask_servers
        and not mask_deploy
    ):
        return doc
    masked = dict(doc)
    masked.pop("maas", None)
    if mask_deploy:
        masked["deploy"] = {**deploy, "ssh_password": SECRET_MASK}
    if isinstance(secrets, dict):
        masked["secrets"] = {
            name: (
                {**entry, "data": dict.fromkeys(entry["data"], SECRET_MASK)}
                if isinstance(entry, dict) and isinstance(entry.get("data"), dict)
                else entry
            )
            for name, entry in secrets.items()
        }
    if mask_servers:
        new_servers = {}
        for hostname, entry in servers.items():
            if isinstance(entry, dict) and entry.get("ssh_password"):
                new_servers[hostname] = {**entry, "ssh_password": SECRET_MASK}
            else:
                new_servers[hostname] = entry
        masked["servers"] = new_servers
    return masked


def mask_yaml_text(yaml_text: str) -> str:
    """Stored document text with secrets masked for API responses."""
    doc, _warnings = parse_document(yaml_text)
    masked = mask_document(doc)
    if masked is doc:
        return yaml_text
    return _dump(masked)


def mask_kubesecrets(text: str) -> str:
    """Rendered kubesecrets.yaml with every data value masked (API previews)."""
    manifests = _secret_manifests(text)
    for doc in manifests:
        data = doc.get("data")
        if isinstance(data, dict):
            doc["data"] = dict.fromkeys(data, SECRET_MASK)
    return yaml.safe_dump_all(manifests, sort_keys=False, explicit_start=True)


# ---------------------------------------------------------------------------
# Versioned document CRUD
# ---------------------------------------------------------------------------


def get_current(
    db: Session, env: Environment
) -> tuple[dict[str, Any], EnvConfigVersion] | None:
    """Return ``(doc, row)`` for the latest config version, or None."""
    row = db.scalar(
        select(EnvConfigVersion)
        .where(EnvConfigVersion.environment_id == env.id)
        .order_by(EnvConfigVersion.version.desc())
        .limit(1)
    )
    if row is None:
        return None
    doc, _warnings = parse_document(row.yaml_text)
    return doc, row


def put_version(
    db: Session,
    env: Environment,
    yaml_text: str,
    actor: str | None,
    *,
    expected_version: int | None = None,
) -> tuple[EnvConfigVersion, list[str]]:
    """Validate and store a new config version. Does not commit.

    Secrets under ``secrets:`` are encrypted at rest before storing — the
    stored yaml_text never holds plaintext. Values equal to SECRET_MASK keep
    the previously stored (encrypted) value for that key. Server-level
    ``ssh_password`` values and ``deploy.ssh_password`` are also encrypted
    at rest. A leftover ``maas:`` block is dropped and not stored.
    """
    doc, warnings = parse_document(yaml_text)
    if expected_version is not None:
        if expected_version < 0:
            raise ConfigValidationError("expected_version must be non-negative")
        latest = (
            db.scalar(
                select(func.max(EnvConfigVersion.version)).where(
                    EnvConfigVersion.environment_id == env.id
                )
            )
            or 0
        )
        if latest != expected_version:
            raise ConfigConflictError("Configuration changed; reload before saving")
    current: tuple[dict[str, Any], EnvConfigVersion] | None = None
    if (
        isinstance(doc.get("secrets"), dict)
        or isinstance(doc.get("servers"), dict)
        or isinstance(doc.get("deploy"), dict)
    ):
        current = get_current(db, env)
    changed = False
    if isinstance(doc.get("secrets"), dict):
        _resolve_secret_sentinels(doc, current[0] if current else None)
        _encrypt_document_secrets(doc)
        changed = True
    if _drop_ignored_maas(doc):
        changed = True
    if _encrypt_deploy_password(doc, current[0] if current else None):
        changed = True
    if _resolve_server_password_sentinels(doc, current[0] if current else None):
        pass  # sentinels resolved in-place, no re-dump needed for sentinel-only changes
    if _encrypt_server_passwords(doc):
        changed = True
    if changed:
        yaml_text = _dump(doc)
    next_version = (
        expected_version + 1
        if expected_version is not None
        else (
            db.scalar(
                select(func.max(EnvConfigVersion.version)).where(
                    EnvConfigVersion.environment_id == env.id
                )
            )
            or 0
        )
        + 1
    )
    row = EnvConfigVersion(
        environment_id=env.id,
        version=next_version,
        yaml_text=yaml_text,
        created_by=actor,
    )
    db.add(row)
    # UNIQUE(environment_id, version) is the CAS arbiter on SQLite/Postgres.
    # A concurrent writer of expected_version+1 wins or loses this exact slot;
    # never recompute a new version after the precondition was checked.
    db.flush()
    return row, warnings


def history(db: Session, env: Environment) -> list[EnvConfigVersion]:
    """All config versions for the environment, newest first."""
    return list(
        db.scalars(
            select(EnvConfigVersion)
            .where(EnvConfigVersion.environment_id == env.id)
            .order_by(EnvConfigVersion.version.desc())
        ).all()
    )


def get_version(db: Session, env: Environment, version: int) -> EnvConfigVersion | None:
    return db.scalar(
        select(EnvConfigVersion).where(
            EnvConfigVersion.environment_id == env.id,
            EnvConfigVersion.version == version,
        )
    )


def assign_server(
    db: Session,
    env: Environment,
    actor: str | None,
    *,
    system_id: str | None = None,
    hostname: str | None = None,
    roles: list[str] | None = None,
    ip: str | None = None,
    source: str = "static",
) -> tuple[EnvConfigVersion, list[str]]:
    """Upsert a server assignment into a NEW config version.

    Entries are keyed by hostname (falling back to the system_id). The
    bare-metal path passes ``source="baremetal"``. Does not commit.
    """
    if source not in VALID_SERVER_SOURCES:
        raise ConfigValidationError(
            f"assign_server: source must be one of {', '.join(sorted(VALID_SERVER_SOURCES))}"
        )
    current = get_current(db, env)
    doc: dict[str, Any] = dict(current[0]) if current else {}
    servers = dict(doc.get("servers") or {})
    key = hostname or system_id
    if not key:
        raise ConfigValidationError("assign_server requires a hostname or system_id")
    entry: dict[str, Any] = dict(servers.get(key) or {})
    entry["system_id"] = system_id
    entry["source"] = source
    entry["roles"] = list(roles or [])
    if ip:
        entry["ip"] = ip
    entry.setdefault("ssh_user", None)
    servers[key] = entry
    doc["servers"] = servers
    return put_version(db, env, _dump(doc), actor)


def upsert_static_server(
    db: Session,
    env: Environment,
    actor: str | None,
    *,
    hostname: str,
    ip: str | None = None,
    ssh_user: str | None = None,
    ssh_auth_method: str | None = None,
    ssh_password: str | None = None,
    roles: list[str] | None = None,
    source: str | None = None,
    service_name: str | None = None,
    public_ip: str | None = None,
    private_ip: str | None = None,
    private_mac: str | None = None,
    vrack_vni: str | None = None,
    public_mac: str | None = None,
    nics: list | None = None,
) -> tuple[EnvConfigVersion, list[str]]:
    """Upsert a server by address into a NEW config version. Does not commit.

    ``source`` (default "static") must be one of VALID_SERVER_SOURCES;
    ``service_name`` is the provider-internal name (OVH) when present.
    ``ssh_password`` is encrypted at rest by :func:`put_version`.
    """
    # Validate hostname format
    if not _HOSTNAME_KEY_RE.match(hostname):
        raise ConfigValidationError(
            f"Invalid hostname '{hostname}' for static server. "
            f"Hostname must match {_HOSTNAME_KEY_RE.pattern} (alphanumeric, dots, hyphens, underscores; start with a letter or digit)."
        )

    current = get_current(db, env)
    doc: dict[str, Any] = dict(current[0]) if current else {}
    servers = dict(doc.get("servers") or {})
    entry: dict[str, Any] = dict(servers.get(hostname) or {})
    # Preserve OVH (or other) identity on role/ip edits when the caller omits
    # source/service_name — otherwise a Save on the Inventory table silently
    # rewrites source: ovh back to static and BYOI can no longer see the host.
    if source is None:
        source = str(entry.get("source") or "").strip() or "static"
    if source not in VALID_SERVER_SOURCES:
        raise ConfigValidationError(
            f"upsert_static_server: source must be one of {', '.join(sorted(VALID_SERVER_SOURCES))}"
        )
    if service_name is None and "service_name" in entry:
        service_name = entry.get("service_name")
    if public_ip is None and "public_ip" in entry:
        public_ip = entry.get("public_ip")
    if private_ip is None and "private_ip" in entry:
        private_ip = entry.get("private_ip")
    if private_mac is None and "private_mac" in entry:
        private_mac = entry.get("private_mac")
    if vrack_vni is None and "vrack_vni" in entry:
        vrack_vni = entry.get("vrack_vni")
    if public_mac is None and "public_mac" in entry:
        public_mac = entry.get("public_mac")
    if nics is None and "nics" in entry:
        nics = entry.get("nics")
    entry["system_id"] = None
    entry["source"] = source
    entry["service_name"] = service_name
    entry["roles"] = list(roles or [])
    if ip:
        entry["ip"] = ip
    if public_ip:
        entry["public_ip"] = public_ip
    if private_ip:
        entry["private_ip"] = private_ip
        # Cluster fabric is the private NIC when we know it.
        if not ip:
            entry["ip"] = private_ip
    if private_mac:
        entry["private_mac"] = private_mac
    if vrack_vni:
        entry["vrack_vni"] = vrack_vni
    if public_mac:
        entry["public_mac"] = public_mac
    if nics:
        entry["nics"] = nics
    entry["ssh_user"] = ssh_user
    if ssh_auth_method:
        entry["ssh_auth_method"] = ssh_auth_method
    if ssh_password:
        entry["ssh_password"] = ssh_password
    servers[hostname] = entry
    doc["servers"] = servers
    return put_version(db, env, _dump(doc), actor)


def adopt_ovh_identity(
    db: Session,
    env: Environment,
    actor: str | None,
    *,
    tags: dict[str, str],
    inventory: list[dict[str, Any]] | None = None,
) -> tuple[EnvConfigVersion | None, list[dict[str, str]]]:
    """Persist ``source: ovh`` + ``service_name`` + public/private IPs.

    ``tags`` maps hostname → OVH service name. ``inventory`` is the live OVH
    list (with ``public_ip`` / ``private_ip``) used to fill the dual-NIC
    fields. No-op (None, []) when every listed host is already tagged and
    addressed correctly. Does not commit.
    """
    if not tags:
        return None, []
    current = get_current(db, env)
    if current is None:
        return None, []
    by_id: dict[str, dict[str, Any]] = {}
    for srv in inventory or []:
        sid = str(srv.get("server_id") or "").strip()
        if sid:
            by_id[sid] = srv
    doc: dict[str, Any] = dict(current[0])
    servers = dict(doc.get("servers") or {})
    adopted: list[dict[str, str]] = []
    changed = False
    for hostname, service_name in tags.items():
        entry = servers.get(hostname)
        if not isinstance(entry, dict):
            continue
        sn = str(service_name or "").strip()
        if not sn:
            continue
        srv = by_id.get(sn) or {}
        public_ip = srv.get("public_ip")
        private_ip = srv.get("private_ip")
        private_mac = srv.get("private_mac")
        vrack_vni = srv.get("vrack_vni")
        cluster_ip = srv.get("ip") or private_ip or public_ip or entry.get("ip")
        already = (
            str(entry.get("source") or "") == "ovh"
            and str(entry.get("service_name") or "") == sn
            and str(entry.get("public_ip") or "") == str(public_ip or "")
            and str(entry.get("private_ip") or "") == str(private_ip or "")
            and str(entry.get("ip") or "") == str(cluster_ip or "")
            and str(entry.get("private_mac") or "") == str(private_mac or "")
            and str(entry.get("vrack_vni") or "") == str(vrack_vni or "")
        )
        if already:
            continue
        updated = dict(entry)
        updated["source"] = "ovh"
        updated["service_name"] = sn
        if cluster_ip:
            updated["ip"] = cluster_ip
        if public_ip:
            updated["public_ip"] = public_ip
        if private_ip:
            updated["private_ip"] = private_ip
        if private_mac:
            updated["private_mac"] = private_mac
        if vrack_vni:
            updated["vrack_vni"] = vrack_vni
        servers[hostname] = updated
        adopted.append({"hostname": hostname, "service_name": sn})
        changed = True
    if not changed:
        return None, []
    doc["servers"] = servers
    row, _warnings = put_version(db, env, _dump(doc), actor)
    return row, adopted


def remove_server(
    db: Session,
    env: Environment,
    actor: str | None,
    *,
    hostname: str,
) -> tuple[EnvConfigVersion, list[str]] | None:
    """Remove a server entry (NEW config version); None when absent. Does not commit."""
    current = get_current(db, env)
    if current is None:
        return None
    doc: dict[str, Any] = dict(current[0])
    servers = dict(doc.get("servers") or {})
    if hostname not in servers:
        return None
    del servers[hostname]
    if servers:
        doc["servers"] = servers
    else:
        doc.pop("servers", None)
    return put_version(db, env, _dump(doc), actor)


def clear_server_roles(
    db: Session,
    env: Environment,
    actor: str | None,
    *,
    hostnames: list[str],
) -> tuple[EnvConfigVersion | None, list[str]]:
    """Drop the saved cluster plan on existing hosts.

    This is how a host stops being a Talos plan. The provider, the adopt
    mark, addresses, and next boot stay as they are. A host with no roles
    does not write a new config version. Does not commit.
    """
    wanted: list[str] = []
    seen: set[str] = set()
    for raw in hostnames or []:
        name = str(raw or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        wanted.append(name)
    if not wanted:
        raise ConfigValidationError("hostnames is required")
    current = get_current(db, env)
    if current is None:
        raise ConfigValidationError("No saved inventory for this environment")
    doc: dict[str, Any] = dict(current[0])
    servers = dict(doc.get("servers") or {})
    present = [name for name in wanted if isinstance(servers.get(name), dict)]
    missing = [name for name in wanted if name not in present]
    if not present:
        raise ConfigValidationError(
            "None of those hostnames are in this environment's inventory"
        )
    warnings = (
        [f"left alone, not in inventory: {', '.join(missing)}"] if missing else []
    )
    changed = False
    for name in present:
        entry = dict(servers[name])
        roles = list(entry.get("roles") or [])
        if roles:
            entry["roles"] = []
            changed = True
        servers[name] = entry
    if not changed:
        return current[1], warnings
    doc["servers"] = servers
    row, parse_warnings = put_version(db, env, _dump(doc), actor)
    return row, warnings + parse_warnings


def set_provider(
    db: Session,
    env: Environment,
    actor: str | None,
    *,
    provider: str,
    talos: dict | None = None,
    deploy: dict | None = None,
) -> tuple[EnvConfigVersion, list[str]]:
    """Set the deployment provider (+ talos/deploy sections) as a NEW config version.

    ``provider`` must be "kubespray" or "talos". A ``talos`` mapping merges its
    non-None keys onto the existing ``talos:`` section (unknown keys are kept
    and warned about by :func:`put_version`'s parse, like a document PUT);
    an existing kubespray doc without a ``talos:`` section gets one created.
    A ``deploy`` mapping merges its non-None keys onto ``deploy:`` the same
    way; unknown deploy keys warn (the parse does not check that section).
    Does not commit.
    """
    if provider not in VALID_PROVIDERS:
        raise ConfigValidationError(
            f"set_provider: provider must be one of {', '.join(sorted(VALID_PROVIDERS))}"
        )
    if talos is not None and not isinstance(talos, dict):
        raise ConfigValidationError("set_provider: talos must be a mapping")
    if deploy is not None and not isinstance(deploy, dict):
        raise ConfigValidationError("set_provider: deploy must be a mapping")
    current = get_current(db, env)
    doc: dict[str, Any] = dict(current[0]) if current else {}
    doc["provider"] = provider
    if talos is not None:
        merged = dict(doc.get("talos") or {})
        merged.update({k: v for k, v in talos.items() if v is not None})
        doc["talos"] = merged
    if provider == "talos":
        from app.services.talos import DEFAULT_TALOS_IMAGE_URL

        merged = dict(doc.get("talos") or {})
        if not str(merged.get("image_url") or "").strip():
            merged["image_url"] = DEFAULT_TALOS_IMAGE_URL
        doc["talos"] = merged
    if deploy is not None:
        merged = dict(doc.get("deploy") or {})
        merged.update({k: v for k, v in deploy.items() if v is not None})
        doc["deploy"] = merged
    row, warnings = put_version(db, env, _dump(doc), actor)
    warnings.extend(
        f"deploy: unknown key '{key}' (ignored)"
        for key in doc.get("deploy") or {}
        if key not in KNOWN_DEPLOY_KEYS
    )
    return row, warnings


def get_provider(db: Session, env: Environment) -> dict[str, Any]:
    """Provider view of the current doc (defaults when no version exists yet).

    Returns ``{"provider", "talos", "deploy"}`` — never creates a version.
    ``deploy.ssh_password`` is masked (SECRET_MASK) like every other API view;
    re-PUTting the masked value keeps the stored password.
    """
    current = get_current(db, env)
    doc = current[0] if current else {}
    deploy = mask_document({"deploy": doc.get("deploy") or {}}).get("deploy") or {}
    ovh_account_id = getattr(env, "ovh_account_id", None) or None
    ovh = doc.get("ovh") if isinstance(doc.get("ovh"), dict) else {}
    from app.services.talos import DEFAULT_TALOS_IMAGE_URL

    talos = dict(doc.get("talos") or {}) if isinstance(doc.get("talos"), dict) else {}
    return {
        "provider": doc.get("provider") or "talos",
        "talos": talos,
        "deploy": deploy,
        "ovh": ovh,
        # Bound OVH account makes this an OVH environment regardless of
        # kubespray vs talos. The UI uses this to show BYOI and to tag hosts.
        "infra": "ovh" if ovh_account_id else None,
        "ovh_account_id": ovh_account_id,
        "default_image_url": DEFAULT_TALOS_IMAGE_URL,
    }


def set_ovh_fabric(
    db: Session,
    env: Environment,
    actor: str | None,
    *,
    vrack: str | None = None,
    vlan_id: int | None = None,
    private_cidr: str | None = None,
) -> tuple[Any, list[str]]:
    """Merge ovh.vrack / vlan_id / private_cidr into a new config version."""
    current = get_current(db, env)
    doc: dict[str, Any] = dict(current[0]) if current else {}
    section = dict(doc.get("ovh") or {})
    if vrack is not None:
        section["vrack"] = str(vrack).strip() or None
    if vlan_id is not None:
        section["vlan_id"] = int(vlan_id)
    if private_cidr is not None:
        section["private_cidr"] = str(private_cidr).strip() or None
    doc["ovh"] = {k: v for k, v in section.items() if v is not None}
    return put_version(db, env, _dump(doc), actor)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _service_file(prefix: str, key: Any, filename: str) -> str:
    """Build a rendered path for a doc key used as a path segment."""
    name = str(key)
    if not _SERVICE_KEY_RE.match(name):
        raise ConfigValidationError(
            f"unsafe name '{name}' — keys used as path segments must match {_SERVICE_KEY_RE.pattern}"
        )
    return f"{prefix}/{name}/{filename}"


def render_to_files(
    doc: dict[str, Any],
    env: Environment,
    settings: (
        Settings | None
    ) = None,  # noqa: ARG001 — signature reserved for future sections
    include_secrets: bool = True,
) -> dict[str, str]:
    """Map a config document onto config-dir-relative file paths.

    Only sections present in the doc produce files; absent sections never
    touch existing files on the deploy host. This is the doc-only render the
    preview endpoint shows; push_rendered additionally merges
    ``kubesecrets.yaml`` and ``helm-chart-versions.yaml`` with the
    destination's existing files before writing (see push_rendered).
    """
    files: dict[str, str] = {}

    provider = doc.get("provider")
    if provider is not None:
        text = provider if isinstance(provider, str) else _dump(provider).strip()
        files["provider"] = f"{text}\n"

    servers = doc.get("servers")
    if servers:
        inventory = build_inventory_from_environment(
            env, servers=servers, include_deployer=False
        )
        files["inventory/inventory.yaml"] = _dump(inventory)

    components = doc.get("components")
    if components is not None:
        files["openstack-components.yaml"] = _dump({"components": components})

    chart_versions = doc.get("chart_versions")
    if chart_versions is not None:
        files["helm-chart-versions.yaml"] = _dump({"charts": chart_versions})

    helm_overrides = doc.get("helm_overrides")
    if isinstance(helm_overrides, dict):
        for service, body in helm_overrides.items():
            if service == "global":
                files[f"helm-configs/global_overrides/{RENDERED_FILENAME}"] = _dump(
                    body
                )
            else:
                files[_service_file("helm-configs", service, RENDERED_FILENAME)] = (
                    _dump(body)
                )

    if str(doc.get("provider") or "").lower() == "talos":
        kube_ovn_path = _service_file("helm-configs", "kube-ovn", RENDERED_FILENAME)
        existing: dict[str, Any] = {}
        if kube_ovn_path in files:
            loaded = yaml.safe_load(files[kube_ovn_path]) or {}
            if isinstance(loaded, dict):
                existing = loaded
        merged = dict(TALOS_KUBE_OVN_HELM)
        merged.update(existing)
        for nested in ("cni_conf", "ipv4"):
            if isinstance(existing.get(nested), dict):
                base = dict(TALOS_KUBE_OVN_HELM.get(nested) or {})
                base.update(existing[nested])
                merged[nested] = base
        files[kube_ovn_path] = _dump(merged)

    kustomize_patches = doc.get("kustomize_patches")
    if isinstance(kustomize_patches, dict):
        for service, body in kustomize_patches.items():
            path = _service_file("kustomize", service, "overlay/patches.yaml")
            if isinstance(body, list):
                files[path] = yaml.safe_dump_all(body, sort_keys=False)
            else:
                files[path] = _dump(body)
            files[_service_file("kustomize", service, "overlay/kustomization.yaml")] = (
                _OVERLAY_KUSTOMIZATION
            )

    # storage.cinder_* vars merge into the cinder_storage_nodes group vars,
    # mirroring the example inventory's cinder_storage_nodes group vars;
    # explicit group_vars entries win on a key conflict.
    storage = doc.get("storage")
    cinder_vars: dict[str, Any] = {}
    if isinstance(storage, dict):
        cinder_vars = {
            str(k): v for k, v in storage.items() if str(k).startswith("cinder_")
        }
    if cinder_vars and "cinder_storage_nodes" not in (doc.get("group_vars") or {}):
        files[
            f"inventory/group_vars/cinder_storage_nodes/{GROUP_VARS_RENDERED_FILENAME}"
        ] = _dump(cinder_vars)

    # storage.ceph renders the rook kustomize overlay (kustomize/rook-ceph/
    # overlay/kustomization.yaml), referencing the upstream rook trees the same
    # way bootstrap.sh symlinks base-kustomize/<svc>/base into the config dir.
    # ceph.enabled: false (or no ceph section) renders nothing.
    ceph = storage.get("ceph") if isinstance(storage, dict) else None
    if isinstance(ceph, dict) and ceph.get("enabled", True):
        external_pvc = bool(ceph.get("external_pvc"))
        files[f"kustomize/{_ROOK_KUSTOMIZE_SERVICE}/overlay/kustomization.yaml"] = (
            _rook_overlay_kustomization(external_pvc)
        )

    group_vars = doc.get("group_vars")
    if isinstance(group_vars, dict):
        for group, variables in group_vars.items():
            name = str(group)
            if not _GROUP_NAME_RE.match(name):
                raise ConfigValidationError(
                    f"group_vars: invalid group name '{name}' "
                    f"(must match {_GROUP_NAME_RE.pattern})"
                )
            if not isinstance(variables, dict):
                raise ConfigValidationError(
                    f"group_vars.{name} must be a mapping of var -> value"
                )
            if name == "cinder_storage_nodes" and cinder_vars:
                variables = {**cinder_vars, **variables}
            files[f"inventory/group_vars/{name}/{GROUP_VARS_RENDERED_FILENAME}"] = (
                _dump(variables)
            )

    if include_secrets:
        secrets = doc.get("secrets")
        if isinstance(secrets, dict) and secrets:
            manifests = []
            for name, entry in secrets.items():
                namespace = entry.get("namespace") or DEFAULT_SECRET_NAMESPACE
                data = entry.get("data") or {}
                manifests.append(
                    {
                        "apiVersion": "v1",
                        "kind": "Secret",
                        "metadata": {"name": str(name), "namespace": namespace},
                        "type": "Opaque",
                        "data": {
                            str(k): base64.b64encode(
                                decrypt_secret(str(v)).encode()
                            ).decode()
                            for k, v in data.items()
                        },
                    }
                )
            # explicit_start matches create-secrets.sh: every doc led by "---"
            files[KUBESECRETS_FILENAME] = yaml.safe_dump_all(
                manifests, sort_keys=False, explicit_start=True
            )

        if env.ssh_private_key_encrypted:
            from app.services.ssh_keys import get_decrypted_private_key

            private_key = get_decrypted_private_key(env)
            if private_key:
                files[".ssh/id_ed25519"] = private_key + "\n"
                files[".ssh/id_ed25519.pub"] = env.ssh_public_key + "\n"

    return files


def doc_env(doc: dict[str, Any]) -> dict[str, str]:
    """Environment variables derived from the config document's ``network:`` section.

    These are the vars genestack's bin/setup-infrastructure.sh consumes when
    pipeline/install commands run (job_runner merges this into the
    subprocess/remote env):

        gateway_domain       -> GATEWAY_DOMAIN       (envoy gateway domain)
        acme_email           -> ACME_EMAIL           (setup-envoy-gateway.sh -e)
        hyperconverged       -> HYPERCONVERGED       (bool -> "true"/"false")
        container_interface  -> CONTAINER_INTERFACE  (kube-ovn IFACE;
                             Talos: VLAN iface that holds the private IP,
                             e.g. enp3934127.100, not the untagged parent)
        compute_interface    -> COMPUTE_INTERFACE    (must not be the public
                             / default-route NIC; not an OVN br-ex fallback)
        ovn: {<key>: <val>}  -> OVN_<KEY>            (snake key uppercased)

    The ovn sub-mapping covers the OVN_* family setup-infrastructure.sh reads
    (OVN_EXTERNAL_INTERFACE, OVN_VLANS, OVN_EXTERNAL_VLAN_{INTERFACE,PARENT,
    ID,MTU}); list values are comma-joined (e.g. OVN_VLANS specs).
    OVN_EXTERNAL_INTERFACE is the br-ex physical port and must not be the
    public/default-route NIC. Leave it unset rather than copying
    COMPUTE_INTERFACE — setup-infrastructure.sh will skip the
    ovn.openstack.org/ports annotation (kubectl annotate ... ports-) so
    br-ex is not plugged into the management NIC.
    Absent keys are simply not exported.
    """
    env: dict[str, str] = {}
    network = doc.get("network")
    if not isinstance(network, dict):
        return env
    for key, var in (
        ("gateway_domain", "GATEWAY_DOMAIN"),
        ("acme_email", "ACME_EMAIL"),
        ("container_interface", "CONTAINER_INTERFACE"),
        ("compute_interface", "COMPUTE_INTERFACE"),
    ):
        if network.get(key):
            env[var] = str(network[key])
    hyperconverged = network.get("hyperconverged")
    if hyperconverged is not None:
        env["HYPERCONVERGED"] = (
            ("true" if hyperconverged else "false")
            if isinstance(hyperconverged, bool)
            else str(hyperconverged)
        )
    ovn = network.get("ovn")
    if isinstance(ovn, dict):
        for key, value in ovn.items():
            if value is None:
                continue
            # Keys become OVN_* env vars exported verbatim on the remote
            # deploy host — reject anything that is not a plain identifier
            # so a crafted key cannot inject into the remote shell string.
            if not _ENV_VAR_NAME_RE.fullmatch(str(key)):
                continue
            if isinstance(value, list):
                value = ",".join(str(item) for item in value)
            env[f"OVN_{str(key).upper()}"] = str(value)
    return env


# ---------------------------------------------------------------------------
# Push (write rendered files to the env's config dir, locally or over ssh)
# ---------------------------------------------------------------------------


def _push_file_local(
    target: Path, backup: Path, data: bytes, log: LogFn | None
) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and not backup.exists():
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(target, backup)
        _log(log, f"[backup] {target} -> {backup}")
    target.write_bytes(data)
    os.chmod(target, 0o644)
    _log(log, f"[write] {target} ({len(data)} bytes)")


def _push_file_ssh(
    ssh_target: str, target: Path, backup: Path, data: bytes, log: LogFn | None
) -> None:
    """Write one file on the deploy host: backup-if-exists, then base64 write.

    Runs as one remote bash invocation through the ssh executor:

        mkdir -p <parent> && \
        if [ -f <target> ]; then mkdir -p <backup-parent> && cp -n <target> <backup>; fi && \
        echo <base64> | base64 -d > <target> && chmod 0644 <target>
    """
    b64 = base64.b64encode(data).decode("ascii")
    t = shlex.quote(str(target))
    b = shlex.quote(str(backup))
    script = (
        f"mkdir -p {shlex.quote(str(target.parent))} && "
        f"if [ -f {t} ]; then mkdir -p {shlex.quote(str(backup.parent))} && cp -n {t} {b}; fi && "
        f"echo {shlex.quote(b64)} | base64 -d > {t} && chmod 0644 {t}"
    )
    result = bridge.run_command(
        ["bash", "-c", script],
        dry_run=False,
        ssh_target=ssh_target,
        log=log,
    )
    if result.get("returncode") not in (0, None):
        raise RuntimeError(
            f"failed to write {target} on {ssh_target}: {result.get('message', 'unknown error')}"
        )


def _push_file_agent(
    env_id: str, target: Path, backup: Path, data: bytes, log: LogFn | None
) -> None:
    """Write one file on the agent host as a file_write relay row.

    Ships ``{path, b64, mode, backup_dir}`` through the DB-backed agent relay
    (app/services/agent_relay.agent_exec); the agent backs up a pre-existing
    target into ``backup_dir`` (the parent of the local/ssh backup path, so
    the backup lands at the same <config_dir>/.console-backup/<ts>/<relpath>
    location) before writing and chmod 0644.
    """
    from app.services import agent_relay

    payload = {
        "path": str(target),
        "b64": base64.b64encode(data).decode("ascii"),
        "mode": "0644",
        "backup_dir": str(backup.parent),
    }
    reply = agent_relay.agent_exec(env_id, "file_write", payload, log_cb=None)
    rc = reply.get("rc")
    if rc != 0:
        raise RuntimeError(
            f"failed to write {target} via agent: {reply.get('error') or f'rc={rc}'}"
        )
    _log(log, f"[write] {target} ({len(data)} bytes) via agent")


def _redacting_log(log: LogFn | None, *needles: str) -> LogFn | None:
    """Log wrapper that blanks out ``needles`` (e.g. base64 secret payloads)."""
    if log is None:
        return None

    def _redacted(msg: str) -> None:
        for needle in needles:
            if needle:
                msg = msg.replace(needle, "<redacted>")
        log(msg)

    return _redacted


def _secret_manifests(text: str) -> list[dict[str, Any]]:
    """v1/Secret manifests (with a metadata.name) from a multi-doc YAML text."""
    return [
        doc
        for doc in yaml.safe_load_all(text)
        if isinstance(doc, dict)
        and doc.get("kind") == "Secret"
        and isinstance(doc.get("metadata"), dict)
        and doc["metadata"].get("name")
    ]


def merge_kubesecrets(existing_text: str, rendered_text: str) -> str:
    """Merge two multi-doc Secret files by metadata.name.

    Existing (e.g. create-secrets.sh-generated) entries are preserved in
    place; console-rendered entries win on a name conflict and new ones are
    appended. Merging instead of overwriting is what keeps push from
    triggering the mass secret rotation create-secrets.sh refuses to cause.
    """
    merged: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for text in (existing_text, rendered_text):
        for doc in _secret_manifests(text):
            name = str(doc["metadata"]["name"])
            if name not in merged:
                order.append(name)
            merged[name] = doc
    return yaml.safe_dump_all(
        [merged[name] for name in order], sort_keys=False, explicit_start=True
    )


def _read_destination_file(
    target: Path,
    ctx: EnvContext,
    log: LogFn | None,
    agent_env_id: str | None = None,
) -> str | None:
    """Existing file content on the destination, or None if absent.

    Over ssh the file is read with a remote ``cat``; via the agent channel it
    is read with a run_command ``cat`` row through the DB relay. log=None for
    the remote reads because the content may hold plaintext secrets — never
    echo it into the job log.
    """
    if agent_env_id:
        from app.services import agent_relay

        reply = agent_relay.agent_exec(
            agent_env_id,
            "run_command",
            {"cmd": ["cat", str(target)], "cwd": None, "env": {}, "timeout": 60},
            timeout=60,
            log_cb=None,
        )
        if reply.get("rc") != 0:
            _log(log, f"[merge] {target}: no existing {target.name} on agent host")
            return None
        _log(log, f"[merge] read existing {target} via agent")
        return reply.get("stdout") or ""
    if ctx.ssh_target:
        result = bridge.run_command(
            ["cat", str(target)],
            dry_run=False,
            ssh_target=ctx.ssh_target,
            log=None,
        )
        if result.get("returncode") != 0:
            _log(log, f"[merge] {target}: no existing {target.name} on remote")
            return None
        _log(log, f"[merge] read existing {target} via ssh")
        return result.get("stdout") or ""
    if not target.exists():
        return None
    return target.read_text(encoding="utf-8")


def merge_chart_versions(existing_text: str, rendered_text: str) -> str:
    """Merge two helm-chart-versions.yaml files per chart name.

    Shape: a top-level ``charts:`` mapping of chart name -> version, same as
    the repo's helm-chart-versions.yaml bootstrap copies into the config dir.
    Existing entries are preserved; console-rendered entries win per key — a
    doc pinning a few charts must not clobber bootstrap's full set (the next
    pipeline stage extracts versions from this file).
    """

    def _charts(text: str) -> dict[str, Any]:
        data = yaml.safe_load(text)
        if isinstance(data, dict) and isinstance(data.get("charts"), dict):
            return data["charts"]
        return {}

    merged = {**_charts(existing_text), **_charts(rendered_text)}
    return _dump({"charts": merged})


def prune_kubesecrets(merged_text: str, drop_names: set[str]) -> tuple[str, list[str]]:
    """Drop the console-rendered Secret entries the document stopped declaring.

    ``merged_text`` is the file after it has been merged with the destination
    (so externally generated entries are present). ``drop_names`` are the
    Secret names to remove (the previous document's pins this one dropped).
    Generated entries the console never owned are not in ``drop_names`` and
    therefore survive. Returns the pruned text and the removed names.
    """
    drop = {str(name) for name in drop_names}
    manifests = _secret_manifests(merged_text)
    removed = sorted(
        {
            str(m["metadata"]["name"])
            for m in manifests
            if str(m["metadata"]["name"]) in drop
        }
    )
    if not removed:
        return merged_text, []
    kept = [m for m in manifests if str(m["metadata"]["name"]) not in drop]
    return yaml.safe_dump_all(kept, sort_keys=False, explicit_start=True), removed


def prune_chart_entries(
    merged_charts: dict[str, Any], drop_names: set[str]
) -> tuple[dict[str, Any], list[str]]:
    """Drop chart versions the document stopped pinning.

    ``merged_charts`` is the merged mapping (bootstrap's full set + doc pins);
    ``drop_names`` are the chart names to remove (the previous document's pins
    the new document no longer sets). Everything else — including bootstrap's
    charts the doc never owned — survives. Returns the pruned mapping and the
    removed names.
    """
    drop = {str(name) for name in drop_names}
    removed = sorted(drop & set(merged_charts))
    if not removed:
        return dict(merged_charts), []
    return {k: v for k, v in merged_charts.items() if k not in drop}, removed


def _read_manifest(
    target: Path,
    ctx: EnvContext,
    log: LogFn | None,
    agent_env_id: str | None = None,
) -> dict[str, Any]:
    """Previous push's manifest (``{pushed_files, pinned_charts}``) or ``{}``."""
    text = _read_destination_file(target, ctx, log, agent_env_id)
    if text is None:
        return {}
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        _log(log, f"[prune] {target.name}: unreadable manifest — skipping pruning")
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def _tracked_manifest_files(files: dict[str, str]) -> set[str]:
    """Rendered files the manifest tracks as doc-owned.

    The env's SSH key pair (``.ssh/``) is excluded: it belongs to the
    Environment row, not the config document, so a config push that stops
    rendering it must not prune the keys off the deploy host.
    """
    return {rel for rel in files if not rel.startswith(".ssh/")}


def _manifest_prune_targets(
    prev_manifest: dict[str, Any],
    files: dict[str, str],
    config_dir: Path,
    log: LogFn | None,
) -> list[str]:
    """Previous-manifest files this render no longer produces.

    Returns config-dir-relative paths to delete (backed up first by the
    caller). The manifest itself, the backup tree, and the two merge files
    (kubesecrets.yaml / helm-chart-versions.yaml — pruned in place because
    they also hold externally generated content) are never file-deleted.
    Entries that cannot be resolved to a path inside ``config_dir`` are
    skipped with a warning rather than touched.
    """
    tracked = _tracked_manifest_files(files)
    targets: list[str] = []
    for p in [str(x) for x in prev_manifest.get("pushed_files") or []]:
        rel = p.lstrip("/")
        if rel in tracked or rel.startswith(".ssh/"):
            continue
        if rel == MANIFEST_FILENAME or rel.startswith(".console-backup/"):
            continue
        if rel in (KUBESECRETS_FILENAME, CHART_VERSIONS_FILENAME):
            continue
        path = (config_dir / rel).resolve()
        try:
            path.relative_to(config_dir.resolve())
        except ValueError:
            _log(log, f"[prune] skipping manifest entry outside config dir: {p}")
            continue
        targets.append(rel)
    return sorted(targets)


def _delete_file_local(
    config_dir: Path, relpath: str, backup: Path, log: LogFn | None
) -> None:
    """Backup-then-delete one local file, rmdir-ing emptied parents (to config_dir)."""
    target = config_dir / relpath
    if target.exists():
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(target, backup)
        _log(log, f"[backup] {target} -> {backup}")
    target.unlink(missing_ok=True)
    _log(log, f"[prune] removed {target}")
    # Remove the emptied parent dirs up to (but not including) the config dir.
    cur = target.parent.resolve()
    cfg = config_dir.resolve()
    while cur != cfg and cur != cur.parent:
        try:
            if cur.is_dir() and not any(cur.iterdir()):
                cur.rmdir()
                cur = cur.parent
            else:
                break
        except OSError:
            break


def _delete_file_remote(
    config_dir: Path,
    relpath: str,
    backup_rel: str,
    ctx: EnvContext,
    log: LogFn | None,
    agent_env_id: str | None,
) -> None:
    """Backup-then-delete one file on the deploy host (ssh or agent channel).

    One bash invocation: back the file up (if present) into the same
    ``.console-backup/<ts>`` tree used by writes, unlink it, and remove the
    emptied parent dirs up to the config dir (never the config dir itself,
    never anything outside it).
    """
    cfg = shlex.quote(str(config_dir))
    t = shlex.quote(str(config_dir / relpath))
    b = shlex.quote(str(config_dir / backup_rel))
    script = (
        f"mkdir -p {shlex.quote(str(config_dir / backup_rel.parent))} && "
        f"if [ -f {t} ]; then cp -n {t} {b}; fi && "
        f"rm -f {t} && "
        f"cur=$(dirname {t}); "
        f'while [ "$cur" != {cfg} ] && [ -n "$cur" ] && [ -d "$cur" ]; do '
        f'  if rmdir "$cur" 2>/dev/null; then cur=$(dirname "$cur"); else break; fi; '
        f"done"
    )
    if agent_env_id:
        from app.services import agent_relay

        reply = agent_relay.agent_exec(
            agent_env_id,
            "run_command",
            {"cmd": ["bash", "-c", script]},
            timeout=60,
            log_cb=None,
        )
        rc = reply.get("rc")
        if rc not in (0, None):
            _log(
                log,
                f"[prune] failed to remove {relpath} via agent: {reply.get('error') or f'rc={rc}'}",
            )
            return
    else:
        result = bridge.run_command(
            ["bash", "-c", script],
            dry_run=False,
            ssh_target=ctx.ssh_target,
            log=log,
        )
        if result.get("returncode") not in (0, None):
            _log(
                log,
                f"[prune] failed to remove {relpath} via ssh: {result.get('message', 'unknown error')}",
            )
            return
    _log(log, f"[prune] removed {relpath}")


def ensure_kustomize_layout(
    config_dir: str | Path,
    genestack_root: str | Path,
    log: Callable[[str], None] | None = None,
) -> list[str]:
    """Ensure kustomize/<svc>/overlay directories exist for all base-kustomize services.

    bootstrap.sh creates kustomize/<svc>/overlay for every base-kustomize/<svc>/base
    service, but newly split plays may have no overlay. This reconciles missing ones
    by creating a stub kustomization.yaml (resources: [../base]).

    Returns list of service names for which overlay directories were created.
    """
    config_dir = Path(config_dir)
    genestack_root = Path(genestack_root)

    created = []
    base_kustomize = genestack_root / "base-kustomize"

    if not base_kustomize.exists():
        if log:
            log(
                f"[ensure_kustomize_layout] base-kustomize directory not found at {base_kustomize}"
            )
        return created

    # Find all base-kustomize/<svc>/base directories
    for svc_dir in sorted(base_kustomize.iterdir()):
        if not svc_dir.is_dir():
            continue
        base_dir = svc_dir / "base"
        if not base_dir.exists():
            continue

        service = svc_dir.name
        overlay_dir = config_dir / "kustomize" / service / "overlay"
        kustomization_file = overlay_dir / "kustomization.yaml"

        if not kustomization_file.exists():
            overlay_dir.mkdir(parents=True, exist_ok=True)
            kustomization_file.write_text(_OVERLAY_KUSTOMIZATION)
            created.append(service)
            if log:
                log(
                    f"[ensure_kustomize_layout] created {service}/overlay/kustomization.yaml"
                )

    return created


def sync_chart_versions_from_repo(ctx, log, dry_run=False):
    """Sync helm chart versions from genestack repo to config dir.

    Called when skip_push=true to ensure chart versions are up to date even
    without a full config push. In practice this is a no-op when there's no
    config doc, since there are no chart versions to sync.
    """
    if dry_run:
        log("[sync_chart_versions] dry-run: would sync chart versions from repo")
        return

    # This is a compatibility stub - the full implementation would copy
    # helm-chart-versions.yaml from the genestack repo to the config dir
    log("[sync_chart_versions] no-op when skip_push without config doc")


def _remember_pushed_secret(
    relpath: str,
    target: Path,
    config_dir: Path,
    agent_env_id: str | None,
    ctx: EnvContext,
    backup: Path | None = None,
) -> None:
    """Record a secret file this push wrote, and the backup copy of that file.

    The backup is the previous contents under ``.console-backup``. It holds
    the same secret material, so the lease removes it with the live file.
    Other backups, such as chart versions, are not recorded. Do not delete
    anything here.
    """
    if relpath != KUBESECRETS_FILENAME and not relpath.startswith(".ssh/"):
        return
    secret_lease.remember(
        target,
        agent_env_id=agent_env_id,
        ssh_target=ctx.ssh_target,
        config_dir=config_dir,
    )
    if backup is not None:
        secret_lease.remember(
            backup,
            agent_env_id=agent_env_id,
            ssh_target=ctx.ssh_target,
            config_dir=config_dir,
        )


def push_rendered(
    files: dict[str, str],
    ctx: EnvContext,
    log: LogFn | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Write rendered files, then drop secret files when this push is not a job.

    See :func:`_push_rendered_now`. Inside a job the job lease keeps
    ``kubesecrets.yaml`` and the ``.ssh`` files until the job finishes.
    """
    if ctx.config_dir is None:
        raise ConfigValidationError(
            "Environment has no genestack_config_dir — nowhere to push rendered files"
        )
    close = secret_lease.open_push_scope(ctx.config_dir, dry_run=dry_run, log_fn=log)
    try:
        return _push_rendered_now(files, ctx, log, dry_run)
    finally:
        close()


def _push_rendered_now(
    files: dict[str, str],
    ctx: EnvContext,
    log: LogFn | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Write rendered files into the env's config dir.

    Backs up pre-existing files to ``<config_dir>/.console-backup/<utc-ts>/``
    before overwriting (never overwriting a previous backup within the same
    batch). The executor is chosen by preference — connected agent (one
    ``file_write`` relay row per file), then ssh (one base64 write command
    per file), then local (direct write, chmod 0644). dry_run logs the file
    list + byte counts and writes nothing.

    ``kubesecrets.yaml`` is merged with the destination's existing file by
    Secret name (rendered entries win conflicts, all other entries survive)
    instead of overwritten — genestack's no-mass-rotation rule.

    ``helm-chart-versions.yaml`` is likewise merged per chart name (rendered
    entries win per key, all other charts survive) so a doc pinning a few
    charts does not clobber the full file bootstrap copied. When the
    destination file is absent the doc-only (partial) render is written with
    a warning — bootstrap should run first for the full chart set.

    Pruning: every push writes ``.genestack-manifest.yaml`` next to the
    rendered files recording exactly what it wrote (``pushed_files`` plus the
    document's ``pinned_secrets``/``pinned_charts``). The next push deletes
    files the previous manifest listed that this render no longer produces —
    backed up into the same ``.console-backup/<ts>`` tree — and drops Secret
    / chart entries the document stopped declaring. The two merge files
    (kubesecrets.yaml, helm-chart-versions.yaml) are never file-deleted
    by the prune pass:
    they hold externally generated content too, so they are pruned in place
    (only entries the previous document itself wrote are removed). The
    manifest is also excluded from deletion, and dry-run pushes write
    nothing (including no manifest and no deletions).

    ``kubesecrets.yaml`` and ``.ssh`` files this push writes stay for the
    rest of the job so install scripts can read them. The job removes those
    paths when it finishes. A push that is not inside a job removes them
    before this function returns. A dry run writes nothing and deletes nothing.
    """
    if ctx.config_dir is None:
        raise ConfigValidationError(
            "Environment has no genestack_config_dir — nowhere to push rendered files"
        )
    config_dir = ctx.config_dir
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_root = config_dir / ".console-backup" / timestamp

    # Executor preference: agent (connected) -> ssh deploy host -> local.
    agent_env_id = pick_executor(ctx.environment, ctx).agent_env_id

    # Ensure .ssh directory exists with mode 0700 before pushing key files.
    # Skipped in dry-run: a rehearsal must perform no writes/side effects.
    ssh_files = {k for k in files if k.startswith(".ssh/")}
    if ssh_files and not dry_run:
        ssh_dir = config_dir / ".ssh"
        if agent_env_id:
            from app.services import agent_relay

            reply = agent_relay.agent_exec(
                agent_env_id,
                "run_command",
                {"cmd": ["mkdir", "-p", "-m", "0700", str(ssh_dir)]},
                log_cb=log,
            )
            if reply.get("rc") not in (0, None):
                _log(
                    log,
                    f"[warn] could not create {ssh_dir} via agent: {reply.get('error')}",
                )
        elif ctx.ssh_target:
            result = bridge.run_command(
                ["mkdir", "-p", "-m", "0700", str(ssh_dir)],
                dry_run=False,
                ssh_target=ctx.ssh_target,
                log=log,
            )
            if result.get("returncode") not in (0, None):
                _log(
                    log,
                    f"[warn] could not create {ssh_dir} via ssh: {result.get('message')}",
                )
        else:
            ssh_dir.mkdir(parents=True, exist_ok=True)
            os.chmod(ssh_dir, 0o700)

    written: list[str] = []
    total_bytes = 0
    pruned_files: list[str] = []
    pruned_secrets: list[str] = []
    pruned_charts: list[str] = []

    # Prune planning (real pushes only): read the previous push's manifest to
    # find files this render no longer produces and entries the document
    # stopped declaring; prepare the manifest this push will write. Dry-run
    # must not read or write anything on the destination, so it plans nothing.
    prev_manifest: dict[str, Any] = {}
    prunable: list[str] = []
    new_manifest: str | None = None
    doc_secret_names: set[str] = set()
    doc_chart_names: set[str] = set()
    if not dry_run:
        rendered_secrets = files.get(KUBESECRETS_FILENAME)
        if isinstance(rendered_secrets, str):
            doc_secret_names = {
                str(doc["metadata"]["name"])
                for doc in _secret_manifests(rendered_secrets)
            }
        rendered_charts = files.get(CHART_VERSIONS_FILENAME)
        if isinstance(rendered_charts, str):
            charts_data = yaml.safe_load(rendered_charts)
            if isinstance(charts_data, dict) and isinstance(
                charts_data.get("charts"), dict
            ):
                doc_chart_names = {str(k) for k in charts_data["charts"]}
        prev_manifest = _read_manifest(
            config_dir / MANIFEST_FILENAME, ctx, log, agent_env_id
        )
        prunable = _manifest_prune_targets(prev_manifest, files, config_dir, log)
        new_manifest = _dump(
            {
                "pushed_files": sorted(_tracked_manifest_files(files)),
                "pinned_secrets": sorted(doc_secret_names),
                "pinned_charts": sorted(doc_chart_names),
            }
        )

    for relpath in sorted(files):
        data = files[relpath].encode("utf-8")
        total_bytes += len(data)
        target = config_dir / relpath
        if dry_run:
            _log(log, f"[dry-run] would write {target} ({len(data)} bytes)")
            written.append(relpath)
            continue
        if relpath == KUBESECRETS_FILENAME:
            # Merge with any existing file (create-secrets.sh refuses to
            # regenerate it to avoid mass rotation — push must not either),
            # then prune the Secret entries the previous document pinned that
            # this one dropped (generated entries the console never owned
            # survive).
            existing = _read_destination_file(target, ctx, log, agent_env_id)
            text = data.decode("utf-8")
            if existing is not None:
                text = merge_kubesecrets(existing, text)
            drop_secrets = {
                str(s) for s in prev_manifest.get("pinned_secrets") or []
            } - doc_secret_names
            if drop_secrets:
                text, pruned_secrets = prune_kubesecrets(text, drop_secrets)
            data = text.encode("utf-8")
        elif relpath == CHART_VERSIONS_FILENAME:
            # Merge with any existing file: bootstrap copies the repo's full
            # chart set and later stages extract versions from it, so the
            # doc's pins overlay onto it instead of replacing it.
            existing = _read_destination_file(target, ctx, log, agent_env_id)
            if existing is not None:
                data = merge_chart_versions(existing, data.decode("utf-8")).encode(
                    "utf-8"
                )
                drop_charts = {
                    str(c) for c in prev_manifest.get("pinned_charts") or []
                } - doc_chart_names
                if drop_charts:
                    charts_map = (yaml.safe_load(data) or {}).get("charts") or {}
                    charts_map, pruned_charts = prune_chart_entries(
                        charts_map, drop_charts
                    )
                    data = _dump({"charts": charts_map}).encode("utf-8")
            else:
                chart_count = len((yaml.safe_load(data) or {}).get("charts") or {})
                _log(
                    log,
                    f"[warn] {CHART_VERSIONS_FILENAME} not present on target; writing partial "
                    f"file with {chart_count} chart(s) — bootstrap first for the full set",
                )
        if agent_env_id:
            # Agent channel: one file_write row per file (merges above already
            # happened hub-side). The b64 payload lives only in the relay row
            # and the wire frame — never in the job log.
            _push_file_agent(agent_env_id, target, backup_root / relpath, data, log)
            _remember_pushed_secret(
                relpath,
                target,
                config_dir,
                agent_env_id,
                ctx,
                backup_root / relpath,
            )
            if relpath in ssh_files:
                from app.services import agent_relay

                agent_relay.agent_exec(
                    agent_env_id,
                    "run_command",
                    {"cmd": ["chmod", "0600", str(target)]},
                    log_cb=log,
                )
        elif ctx.ssh_target:
            secret_payload = relpath == KUBESECRETS_FILENAME or relpath.startswith(
                ".ssh/"
            )
            write_log = (
                _redacting_log(log, base64.b64encode(data).decode("ascii"))
                if secret_payload
                else log
            )
            _push_file_ssh(
                ctx.ssh_target, target, backup_root / relpath, data, write_log
            )
            _remember_pushed_secret(
                relpath,
                target,
                config_dir,
                agent_env_id,
                ctx,
                backup_root / relpath,
            )
            if relpath in ssh_files:
                bridge.run_command(
                    ["chmod", "0600", str(target)],
                    dry_run=False,
                    ssh_target=ctx.ssh_target,
                    log=log,
                )
        else:
            _push_file_local(target, backup_root / relpath, data, log)
            _remember_pushed_secret(
                relpath,
                target,
                config_dir,
                agent_env_id,
                ctx,
                backup_root / relpath,
            )
            if relpath in ssh_files:
                os.chmod(target, 0o600)
        written.append(relpath)

    # Deletion pass: remove files the previous push produced but this render
    # no longer does (backed up into the same .console-backup/<ts> tree).
    if not dry_run:
        for relpath in prunable:
            if agent_env_id:
                _delete_file_remote(
                    config_dir,
                    relpath,
                    f"{backup_root.name}/{relpath}",
                    ctx,
                    log,
                    agent_env_id,
                )
            elif ctx.ssh_target:
                _delete_file_remote(
                    config_dir, relpath, f"{backup_root.name}/{relpath}", ctx, log, None
                )
            else:
                _delete_file_local(config_dir, relpath, backup_root / relpath, log)
            pruned_files.append(relpath)

        # Record what this push wrote so the next push knows what to prune.
        if new_manifest is not None:
            manifest_target = config_dir / MANIFEST_FILENAME
            if agent_env_id:
                _push_file_agent(
                    agent_env_id,
                    manifest_target,
                    backup_root / MANIFEST_FILENAME,
                    new_manifest.encode("utf-8"),
                    log,
                )
            elif ctx.ssh_target:
                _push_file_ssh(
                    ctx.ssh_target,
                    manifest_target,
                    backup_root / MANIFEST_FILENAME,
                    new_manifest.encode("utf-8"),
                    log,
                )
            else:
                _push_file_local(
                    manifest_target,
                    backup_root / MANIFEST_FILENAME,
                    new_manifest.encode("utf-8"),
                    log,
                )

    return {
        "ok": True,
        "dry_run": dry_run,
        "files": written,
        "count": len(written),
        "bytes": total_bytes,
        "deleted": pruned_files,
        "pruned_secrets": pruned_secrets,
        "pruned_charts": pruned_charts,
        "backup_dir": None if dry_run else str(backup_root),
        "message": (
            f"[dry-run] {len(written)} file(s), {total_bytes} bytes — nothing written"
            if dry_run
            else f"{len(written)} file(s) pushed ({total_bytes} bytes)"
        ),
    }
