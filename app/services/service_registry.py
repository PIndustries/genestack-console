"""Genestack service registry — discovery from bin/install-*.sh headers.

Joins four sources under the genestack root (all optional, never raises):
  - bin/install-<service>.sh bash headers (namespace, helm repo)
  - helm-chart-versions.yaml (charts: service -> version)
  - openstack-components.yaml (components: service -> desired bool)
  - base-helm-configs/<svc>/ and base-kustomize/<svc>/ directories
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable

import yaml

LogFn = Callable[[str], None]


def _log(log: LogFn | None, msg: str) -> None:
    if log:
        log(msg)


_HEADER_LINE = re.compile(r'^\s*([A-Za-z_][A-Za-z0-9_]*)="([^"]*)"\s*(?:#.*)?$')
SERVICE_NAME_RE = re.compile(r"^[a-z0-9-]+$")

# Services that never count as deployable (scaffolding, not real installs).
NON_DEPLOYABLE = frozenset({"service-template"})

_CATEGORY_MAP: dict[str, str] = {}


def _fill(category: str, names: str) -> None:
    for name in names.split():
        _CATEGORY_MAP[name] = category


_fill(
    "core",
    "barbican blazar blazar-reservation-splitter ceilometer cinder cloudkitty "
    "designate freezer glance gnocchi heat horizon ironic keystone magnum manila "
    "masakari neutron nova octavia placement skyline trove zaqar",
)
_fill(
    "infrastructure",
    "mariadb-operator postgres-operator redis-operator redis-replication "
    "redis-sentinel memcached cert-manager sealed-secrets kube-ovn metallb "
    "longhorn topolvm libvirt envoy-gateway",
)
_fill(
    "monitoring",
    "grafana loki tempo kube-prometheus-stack prometheus-pushgateway fluentbit "
    "openstack-exporter opentelemetry-kube-stack barbican-exporter",
)
_fill("testing", "tempest")


def category_for(service: str) -> str:
    return _CATEGORY_MAP.get(service, "other")


def _parse_script_header(path: Path) -> dict[str, str]:
    """Parse simple KEY="value" lines from an install script header.

    Inline comments after the closing quote are stripped. Missing/unreadable
    files yield an empty dict.
    """
    header: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return header
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = _HEADER_LINE.match(line)
        if m:
            header[m.group(1)] = m.group(2)
    return header


def _load_yaml_map(path: Path, top_key: str) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except (OSError, yaml.YAMLError):
        return {}
    if not isinstance(data, dict):
        return {}
    section = data.get(top_key)
    return dict(section) if isinstance(section, dict) else {}


def discover_deployable_services(genestack_root: Path) -> frozenset[str]:
    """Service names from bin/install-*.sh, excluding scaffolding templates."""
    bin_dir = Path(genestack_root) / "bin"
    if not bin_dir.is_dir():
        return frozenset()
    names = {
        s.name.removeprefix("install-").removesuffix(".sh")
        for s in bin_dir.glob("install-*.sh")
    }
    return frozenset(n for n in names if n not in NON_DEPLOYABLE)


def build_service_registry(genestack_root: Path) -> dict[str, Any]:
    """Build the service registry dict (frontend contract — see module docstring)."""
    root = Path(genestack_root)
    chart_versions = _load_yaml_map(root / "helm-chart-versions.yaml", "charts")
    desired_map = _load_yaml_map(root / "openstack-components.yaml", "components")

    services: list[dict[str, Any]] = []
    bin_dir = root / "bin"
    scripts = sorted(bin_dir.glob("install-*.sh")) if bin_dir.is_dir() else []
    for script in scripts:
        name = script.name.removeprefix("install-").removesuffix(".sh")
        header = _parse_script_header(script)
        desired = desired_map.get(name)
        services.append(
            {
                "name": name,
                "script": f"bin/{script.name}",
                "namespace": header.get("SERVICE_NAMESPACE"),
                "helm_repo": header.get("HELM_REPO_NAME_DEFAULT"),
                "helm_repo_url": header.get("HELM_REPO_URL_DEFAULT"),
                "chart_version": chart_versions.get(name),
                "desired": bool(desired) if desired is not None else None,
                "has_helm_configs": (root / "base-helm-configs" / name).is_dir(),
                "has_kustomize": (root / "base-kustomize" / name).is_dir(),
                "category": category_for(name),
            }
        )

    return {
        "genestack_root": str(root.resolve()) if root.is_absolute() else str(root),
        "services": services,
    }


def _svc(name: str) -> dict[str, str]:
    return {"type": "service", "name": name, "script": f"bin/install-{name}.sh"}


def _script(name: str) -> dict[str, str]:
    return {"type": "script", "name": name, "script": f"bin/{name}"}


PIPELINE_STAGES: list[dict[str, Any]] = [
    {
        "id": "hosts",
        "name": "Host Setup",
        "description": "Prepare bare-metal/VM hosts (OS tuning, deps, kernels).",
        "required": True,
        "control": "metal",
        "items": [_script("setup-hosts.sh")],
    },
    {
        "id": "infrastructure",
        "name": "Infrastructure",
        "description": "Base infrastructure (storage, networking prerequisites).",
        "required": True,
        "control": "helm",
        "items": [_script("setup-infrastructure.sh")],
    },
    {
        "id": "operators",
        "name": "Operators & Platform Base",
        "description": "Kubernetes operators and platform primitives OpenStack builds on.",
        "required": True,
        "control": "helm",
        "items": [
            _svc(n)
            for n in (
                "cert-manager sealed-secrets mariadb-operator postgres-operator "
                "redis-operator redis-replication redis-sentinel memcached "
                "metallb longhorn topolvm envoy-gateway"
            ).split()
        ],
    },
    {
        "id": "cni",
        "name": "Cluster CNI",
        "description": "kube-ovn. Own control point — not part of a daily OpenStack restack.",
        "required": True,
        "control": "helm",
        "items": [_svc("kube-ovn")],
    },
    {
        "id": "core",
        "name": "OpenStack Core",
        "description": "Identity and image services required by everything else.",
        "required": True,
        "control": "helm",
        "items": [_svc("keystone"), _svc("placement"), _svc("glance")],
    },
    {
        "id": "compute-network",
        "name": "Compute & Network",
        "description": "Compute and networking services.",
        "required": True,
        "control": "helm",
        "items": [_svc("nova"), _svc("neutron"), _svc("libvirt")],
    },
    {
        "id": "platform-extras",
        "name": "Additional OpenStack Services",
        "description": "Optional OpenStack services (block storage, orchestration, etc.).",
        "required": False,
        "control": "helm",
        "items": [
            _svc(n)
            for n in (
                "barbican cinder heat horizon octavia manila magnum ironic designate "
                "trove zaqar blazar blazar-reservation-splitter masakari freezer "
                "ceilometer gnocchi cloudkitty skyline"
            ).split()
        ],
    },
    {
        "id": "observability",
        "name": "Monitoring & Logging",
        "description": "Metrics, logs, and tracing stack.",
        "required": False,
        "control": "helm",
        "items": [
            _svc(n)
            for n in (
                "grafana loki tempo kube-prometheus-stack prometheus-pushgateway "
                "fluentbit openstack-exporter opentelemetry-kube-stack barbican-exporter"
            ).split()
        ],
    },
    {
        "id": "testing",
        "name": "Testing",
        "description": "Tempest is its own control point — not part of deploy/greenfield.",
        "required": False,
        "control": "test",
        "items": [_svc("tempest")],
    },
]


def get_pipeline() -> dict[str, Any]:
    return {"stages": PIPELINE_STAGES}


def filter_stage_items(
    stage: dict[str, Any],
    components: dict[str, Any] | None,
    log: LogFn | None = None,
) -> list[dict[str, str]]:
    """Stage items minus services explicitly disabled in the config doc.

    Service items (bin/install-<svc>.sh) whose component key is set to
    ``false`` in the doc's ``components:`` section are dropped; non-install
    scripts (setup-hosts.sh, setup-infrastructure.sh) always run. A doc with
    no components section (``components`` None) disables no filtering — every
    item runs, as before.
    """
    items = list(stage["items"])
    if not components:
        return items
    kept: list[dict[str, str]] = []
    for item in items:
        if item.get("type") == "service" and components.get(item.get("name")) is False:
            _log(log, f"[pipeline] skipping {item['name']} (disabled in config doc)")
            continue
        kept.append(item)
    return kept


def get_pipeline_stage(stage_id: str) -> dict[str, Any] | None:
    for stage in PIPELINE_STAGES:
        if stage["id"] == stage_id:
            return stage
    return None


def stage_required(stage: dict[str, Any] | None) -> bool:
    if not stage:
        return True
    return bool(stage.get("required", True))


def stage_control(stage: dict[str, Any] | None) -> str:
    if not stage:
        return "helm"
    return str(stage.get("control") or "helm")
