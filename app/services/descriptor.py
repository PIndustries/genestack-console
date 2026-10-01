"""Read-only environment descriptor assembly (Phase 3).

Summarizes everything the console can learn about one environment from its
genestack config dir (provider, inventory topology, components, helm
overrides, kustomize overlays, gateway yamls) plus live cluster probes.
Every section degrades gracefully: missing files/dirs produce nulls or empty
lists with an ``error`` note — this module never raises.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import yaml

from app.config import Settings, get_settings
from app.models import Environment
from app.services import cluster as cluster_probe
from app.services import genestack_bridge as bridge
from app.services.envcontext import EnvContext, build_context

DESCRIPTOR_VERSION = 1

_NO_CONFIG_DIR = "no genestack_config_dir configured"


def _read_yaml(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _provider(config_dir: Path | None) -> dict[str, Any]:
    result: dict[str, Any] = {"provider": None, "error": None}
    if config_dir is None:
        result["error"] = _NO_CONFIG_DIR
        return result
    path = config_dir / "provider"
    if not path.is_file():
        result["error"] = f"File not found: {path}"
        return result
    try:
        result["provider"] = path.read_text(encoding="utf-8").strip() or None
    except OSError as exc:
        result["error"] = str(exc)
    return result


def _collect_groups(node: dict[str, Any], groups: list[dict[str, Any]]) -> None:
    """Walk a kubespray-style inventory collecting groups with their hosts."""
    for name, value in node.items():
        if not isinstance(value, dict):
            continue
        hosts = value.get("hosts")
        if isinstance(hosts, dict):
            host_names = sorted(str(h) for h in hosts)
            groups.append(
                {"name": str(name), "hosts": host_names, "count": len(host_names)}
            )
        children = value.get("children")
        if isinstance(children, dict):
            _collect_groups(children, groups)


def _host_set(node: Any) -> set[str]:
    """All host names appearing under any ``hosts:`` mapping in an inventory."""
    found: set[str] = set()
    if isinstance(node, dict):
        hosts = node.get("hosts")
        if isinstance(hosts, dict):
            found.update(str(h) for h in hosts)
        for value in node.values():
            if isinstance(value, dict):
                found.update(_host_set(value))
    return found


def _config_drift(
    env: Environment, settings: Settings, config_dir: Path | None
) -> bool | None:
    """True when the doc-rendered inventory host set differs from on-disk.

    None when either side is missing or unreadable (no config doc, no servers
    section, no on-disk inventory). Never raises.
    """
    if config_dir is None:
        return None
    try:
        from app.db import SessionLocal
        from app.services import envconfig as envconfig_service

        db = SessionLocal()
        try:
            current = envconfig_service.get_current(db, env)
        finally:
            db.close()
        if current is None:
            return None
        doc, _row = current
        if not doc.get("servers"):
            return None
        rendered = envconfig_service.render_to_files(doc, env, settings).get(
            "inventory/inventory.yaml"
        )
        if rendered is None:
            return None
        rendered_hosts = _host_set(yaml.safe_load(rendered))
        on_disk = config_dir / "inventory" / "inventory.yaml"
        if not on_disk.is_file():
            return None
        disk_hosts = _host_set(_read_yaml(on_disk))
    except Exception:  # noqa: BLE001 — descriptor must never raise
        return None
    return rendered_hosts != disk_hosts


def _topology(config_dir: Path | None) -> dict[str, Any]:
    result: dict[str, Any] = {"groups": [], "group_vars": [], "error": None}
    if config_dir is None:
        result["error"] = _NO_CONFIG_DIR
        return result
    inventory = config_dir / "inventory" / "inventory.yaml"
    if not inventory.is_file():
        result["error"] = f"File not found: {inventory}"
    else:
        try:
            data = _read_yaml(inventory)
        except (OSError, yaml.YAMLError) as exc:
            result["error"] = str(exc)
        else:
            if isinstance(data, dict):
                _collect_groups(data, result["groups"])
            else:
                result["error"] = f"inventory is not a mapping: {inventory}"
    group_vars = config_dir / "inventory" / "group_vars"
    if group_vars.is_dir():
        result["group_vars"] = sorted(
            p.name for p in group_vars.iterdir() if p.is_dir()
        )
    return result


def _chart_versions(ctx: EnvContext) -> tuple[Any, str | None, str | None]:
    """Env config dir copy wins; fall back to the genestack root copy."""
    candidates: list[tuple[Path, str]] = []
    if ctx.config_dir is not None:
        candidates.append((ctx.config_dir / "helm-chart-versions.yaml", "environment"))
    candidates.append((ctx.genestack_root / "helm-chart-versions.yaml", "global"))
    for path, source in candidates:
        if not path.is_file():
            continue
        try:
            return _read_yaml(path), source, None
        except (OSError, yaml.YAMLError) as exc:
            return None, source, str(exc)
    return None, None, f"File not found: {candidates[-1][0]}"


def _components(
    settings: Settings, env: Environment, ctx: EnvContext
) -> dict[str, Any]:
    path, scope = bridge.resolve_components_path(settings, env)
    desired = bridge.read_components_desired(path)
    chart_versions, versions_source, versions_error = _chart_versions(ctx)
    return {
        "scope": scope,
        "path": str(path),
        "components": desired.get("components") or {},
        "chart_versions": chart_versions,
        "chart_versions_source": versions_source,
        "chart_versions_error": versions_error,
        "error": desired.get("error"),
    }


def _helm_overrides(config_dir: Path | None, genestack_root: Path) -> dict[str, Any]:
    result: dict[str, Any] = {"services": {}, "base_defaults": {}, "error": None}
    if config_dir is None:
        result["error"] = _NO_CONFIG_DIR
        return result
    helm_dir = config_dir / "helm-configs"
    base_configs = genestack_root / "base-helm-configs"
    names: set[str] = set()
    if helm_dir.is_dir():
        names.update(p.name for p in helm_dir.iterdir() if p.is_dir())
    if base_configs.is_dir():
        names.update(p.name for p in base_configs.iterdir() if p.is_dir())
    if not names:
        result["error"] = f"Directory not found: {helm_dir}"
        return result
    for name in sorted(names):
        local = helm_dir / name
        files = sorted(p.name for p in local.glob("*.yaml")) if local.is_dir() else []
        default = f"{name}-helm-overrides.yaml"
        if name != "global_overrides" and default not in files:
            files = [default, *files]
        result["services"][name] = files
        result["base_defaults"][name] = (base_configs / name).is_dir()
    return result


def _kustomize_overlays(config_dir: Path | None) -> list[str]:
    if config_dir is None:
        return []
    kustomize_dir = config_dir / "kustomize"
    if not kustomize_dir.is_dir():
        return []
    return sorted(
        service_dir.name
        for service_dir in kustomize_dir.iterdir()
        if service_dir.is_dir()
        and (service_dir / "overlay" / "kustomization.yaml").is_file()
    )


def _yaml_tree(root: Path) -> list[str]:
    if not root.is_dir():
        return []
    return sorted(
        str(p.relative_to(root))
        for p in root.rglob("*")
        if p.is_file() and p.suffix in (".yaml", ".yml")
    )


def _gateway(config_dir: Path | None) -> dict[str, Any]:
    result: dict[str, Any] = {"gateway_api": [], "metallb": [], "error": None}
    if config_dir is None:
        result["error"] = _NO_CONFIG_DIR
        return result
    result["gateway_api"] = _yaml_tree(config_dir / "gateway-api")
    result["metallb"] = _yaml_tree(config_dir / "manifests" / "metallb")
    return result


def _live_state(ctx: EnvContext) -> dict[str, Any]:
    result: dict[str, Any] = {"cluster": None, "services": None, "error": None}
    try:
        cluster = cluster_probe.cluster_status(ctx.kubeconfig)
        result["cluster"] = {
            "reachable": bool(cluster.get("reachable")),
            "nodes": len(cluster.get("nodes") or []),
            "error": cluster.get("error"),
        }
        services = cluster_probe.services_status(ctx.kubeconfig)
        result["services"] = {
            "reachable": bool(services.get("reachable")),
            "releases": len(services.get("releases") or []),
            "error": services.get("error"),
        }
    except Exception as exc:  # noqa: BLE001 — probes must never break the descriptor
        result["error"] = str(exc)
    return result


def _section(fn: Callable[[], Any]) -> Any:
    """Run one section builder; turn any unexpected failure into an error note."""
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 — descriptor must never raise
        return {"error": str(exc)}


def build_descriptor(
    env: Environment, settings: Settings | None = None
) -> dict[str, Any]:
    """Assemble the full read-only descriptor for one environment."""
    settings = settings or get_settings()
    ctx = build_context(env, settings)
    try:
        return {
            "descriptor_version": DESCRIPTOR_VERSION,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "environment": {
                "id": env.id,
                "name": env.name,
                "region": env.region,
                "tier": env.tier,
                "description": env.description,
            },
            "provider": _section(lambda: _provider(ctx.config_dir)),
            "topology": _section(
                lambda: {
                    **_topology(ctx.config_dir),
                    "drift": _config_drift(env, settings, ctx.config_dir),
                }
            ),
            "components": _section(lambda: _components(settings, env, ctx)),
            "helm_overrides": _section(
                lambda: _helm_overrides(ctx.config_dir, ctx.genestack_root)
            ),
            "kustomize_overlays": _section(lambda: _kustomize_overlays(ctx.config_dir)),
            "gateway": _section(lambda: _gateway(ctx.config_dir)),
            "live_state": _section(lambda: _live_state(ctx)),
        }
    finally:
        ctx.cleanup()
