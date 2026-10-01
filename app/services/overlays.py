"""Read/write environment overlay YAML (helm-configs, kustomize, gateway).

Helm files follow yaml-editor/ye: three layers overlay each other —

    chart values.yaml  →  base-helm-configs  →  helm-configs (local /etc)

GET returns the **merged** document (what helm actually applies). PUT takes
that same merged document, diffs it against chart+base, and writes **only
the delta** to the local helm-configs file — same as ``ye <service>``.
"""

from __future__ import annotations

import copy
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from app.config import Settings, get_settings
from app.models import Environment
from app.services.envcontext import build_context

log = logging.getLogger(__name__)

ALLOWED_ROOTS = ("helm-configs", "kustomize", "gateway-api", "manifests")
ALLOWED_SUFFIXES = {".yaml", ".yml"}
MAX_BYTES = 4_194_304
PATH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,240}$")
_GIT_ERR_MAX = 400
_GIT_TIMEOUT = 30
# service → (helm repo name, repo URL). Chart name is the service name unless
# listed in OCI_CHARTS. Sourced from bin/install-*.sh.
HELM_CHARTS: dict[str, tuple[str, str]] = {
    "barbican": (
        "openstack-helm",
        "https://tarballs.opendev.org/openstack/openstack-helm",
    ),
    "barbican-exporter": (
        "genestack-barbician-exporter-helm-chart",
        "https://rackerlabs.github.io/genestack-barbician-exporter-helm-chart",
    ),
    "blazar": (
        "openstack-helm",
        "https://tarballs.opendev.org/openstack/openstack-helm",
    ),
    "ceilometer": (
        "openstack-helm",
        "https://tarballs.opendev.org/openstack/openstack-helm",
    ),
    "cert-manager": ("charts", "oci://quay.io/jetstack"),
    "cinder": (
        "openstack-helm",
        "https://tarballs.opendev.org/openstack/openstack-helm",
    ),
    "cloudkitty": (
        "openstack-helm",
        "https://tarballs.opendev.org/openstack/openstack-helm",
    ),
    "designate": (
        "openstack-helm",
        "https://tarballs.opendev.org/openstack/openstack-helm",
    ),
    "envoyproxy-gateway": ("gateway-helm", "oci://docker.io/envoyproxy"),
    "fluent-bit": ("fluent", "https://fluent.github.io/helm-charts"),
    "freezer": (
        "openstack-helm",
        "https://tarballs.opendev.org/openstack/openstack-helm",
    ),
    "glance": (
        "openstack-helm",
        "https://tarballs.opendev.org/openstack/openstack-helm",
    ),
    "gnocchi": (
        "openstack-helm",
        "https://tarballs.opendev.org/openstack/openstack-helm",
    ),
    "grafana": ("grafana", "https://grafana.github.io/helm-charts"),
    "heat": ("openstack-helm", "https://tarballs.opendev.org/openstack/openstack-helm"),
    "horizon": (
        "openstack-helm",
        "https://tarballs.opendev.org/openstack/openstack-helm",
    ),
    "ironic": (
        "openstack-helm",
        "https://tarballs.opendev.org/openstack/openstack-helm",
    ),
    "keystone": (
        "openstack-helm",
        "https://tarballs.opendev.org/openstack/openstack-helm",
    ),
    "kube-ovn": ("kubeovn", "https://kubeovn.github.io/kube-ovn"),
    "kube-prometheus-stack": (
        "prometheus-community",
        "https://prometheus-community.github.io/helm-charts",
    ),
    "libvirt": (
        "openstack-helm",
        "https://tarballs.opendev.org/openstack/openstack-helm",
    ),
    "loki": ("grafana", "https://grafana.github.io/helm-charts"),
    "longhorn": ("longhorn", "https://charts.longhorn.io"),
    "magnum": (
        "openstack-helm",
        "https://tarballs.opendev.org/openstack/openstack-helm",
    ),
    "manila": (
        "openstack-helm",
        "https://tarballs.opendev.org/openstack/openstack-helm",
    ),
    "mariadb-operator": (
        "mariadb-operator",
        "https://helm.mariadb.com/mariadb-operator",
    ),
    "masakari": (
        "openstack-helm",
        "https://tarballs.opendev.org/openstack/openstack-helm",
    ),
    "memcached": (
        "openstack-helm",
        "https://tarballs.opendev.org/openstack/openstack-helm",
    ),
    "metallb": ("metallb", "https://metallb.github.io/metallb"),
    "neutron": (
        "openstack-helm",
        "https://tarballs.opendev.org/openstack/openstack-helm",
    ),
    "nova": ("openstack-helm", "https://tarballs.opendev.org/openstack/openstack-helm"),
    "octavia": (
        "openstack-helm",
        "https://tarballs.opendev.org/openstack/openstack-helm",
    ),
    "opentelemetry-kube-stack": (
        "open-telemetry",
        "https://open-telemetry.github.io/opentelemetry-helm-charts",
    ),
    "placement": (
        "openstack-helm",
        "https://tarballs.opendev.org/openstack/openstack-helm",
    ),
    "postgres-operator": (
        "postgres-operator-charts",
        "https://opensource.zalando.com/postgres-operator/charts/postgres-operator",
    ),
    "prometheus-pushgateway": (
        "prometheus-community",
        "https://prometheus-community.github.io/helm-charts",
    ),
    "redis-operator": ("ot-helm", "https://ot-container-kit.github.io/helm-charts"),
    "redis-replication": ("ot-helm", "https://ot-container-kit.github.io/helm-charts"),
    "redis-sentinel": ("ot-helm", "https://ot-container-kit.github.io/helm-charts"),
    "skyline": (
        "openstack-helm",
        "https://tarballs.opendev.org/openstack/openstack-helm",
    ),
    "tempest": (
        "openstack-helm",
        "https://tarballs.opendev.org/openstack/openstack-helm",
    ),
    "tempo": ("grafana", "https://grafana.github.io/helm-charts"),
    "trove": (
        "openstack-helm",
        "https://tarballs.opendev.org/openstack/openstack-helm",
    ),
    "zaqar": (
        "openstack-helm",
        "https://tarballs.opendev.org/openstack/openstack-helm",
    ),
}
OCI_CHARTS = {
    "cert-manager": "oci://quay.io/jetstack/cert-manager",
    "envoyproxy-gateway": "oci://docker.io/envoyproxy/gateway-helm",
}
HELM_TIMEOUT = 25
_HELM_CACHE: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
_HELM_CACHE_TTL = 600.0
_helm_lock = threading.Lock()
_added_repos: set[str] = set()


class OverlayError(ValueError):
    """Invalid overlay path or content."""


class OverlayNotFound(FileNotFoundError):
    """Requested overlay file does not exist."""


def merge_dicts(base: Any, overrides: Any) -> Any:
    """Deep-merge ``overrides`` onto ``base`` (overrides win). Same as yaml-editor/ye."""
    if not isinstance(base, dict):
        base = {}
    if not isinstance(overrides, dict):
        return (
            copy.deepcopy(overrides) if overrides is not None else copy.deepcopy(base)
        )
    result = copy.deepcopy(base)
    for key, override_value in overrides.items():
        if (
            key in result
            and isinstance(result[key], dict)
            and isinstance(override_value, dict)
        ):
            result[key] = merge_dicts(result[key], override_value)
        else:
            result[key] = copy.deepcopy(override_value)
    return result


def compute_patch(base: Any, edited: Any) -> dict[str, Any]:
    """Keys in ``edited`` that differ from ``base``. Same as yaml-editor/ye.

    Keys removed from ``edited`` that still exist in ``base`` are *not*
    recorded (helm has no generic 'unset'). Lists are replaced wholesale.
    """
    if not isinstance(edited, dict):
        return {}
    if not isinstance(base, dict):
        return copy.deepcopy(edited)
    patch: dict[str, Any] = {}
    for key, edited_value in edited.items():
        if key not in base:
            patch[key] = copy.deepcopy(edited_value)
            continue
        base_value = base[key]
        if isinstance(base_value, dict) and isinstance(edited_value, dict):
            sub = compute_patch(base_value, edited_value)
            if sub:
                patch[key] = sub
        elif edited_value != base_value:
            patch[key] = copy.deepcopy(edited_value)
    return patch


def _dump(data: Any) -> str:
    if data is None or data == {}:
        return "{}\n"
    return yaml.safe_dump(data, default_flow_style=False, sort_keys=False)


_YAML_MAX_ANCHORS = 16
_YAML_MAX_ALIASES = 32
_YAML_MAX_DEPTH = 64
_YAML_MAX_NODES = 8_000


def _yaml_graph_ok(data: Any) -> bool:
    """Reject cyclic aliases and exponential alias expansion."""
    visits = 0

    def walk(node: Any, stack: set[int]) -> bool:
        nonlocal visits
        visits += 1
        if visits > _YAML_MAX_NODES:
            return False
        if isinstance(node, dict):
            ident = id(node)
            if ident in stack:
                return False
            stack.add(ident)
            try:
                for key, value in node.items():
                    if not walk(key, stack) or not walk(value, stack):
                        return False
            finally:
                stack.discard(ident)
        elif isinstance(node, list):
            ident = id(node)
            if ident in stack:
                return False
            stack.add(ident)
            try:
                for item in node:
                    if not walk(item, stack):
                        return False
            finally:
                stack.discard(ident)
        return True

    return walk(data, set())


def _load(text: str | None) -> Any:
    if not text or not str(text).strip():
        return {}
    raw = str(text)
    if raw.count("&") > _YAML_MAX_ANCHORS or raw.count("*") > _YAML_MAX_ALIASES:
        raise OverlayError("YAML has too many anchors")
    try:
        data = yaml.safe_load(raw)
    except RecursionError as exc:
        raise OverlayError("YAML too large or recursive") from exc
    except yaml.YAMLError as exc:
        raise OverlayError(f"invalid YAML: {exc}") from exc
    if not _yaml_graph_ok(data):
        raise OverlayError("YAML too large or recursive")
    return data if data is not None else {}


def resolve_overlay_path(config_dir: Path, rel: str) -> Path:
    """Return an absolute path inside ``config_dir`` or raise OverlayError."""
    text = str(rel or "").strip().lstrip("/")
    if not text or not PATH_RE.match(text):
        raise OverlayError("invalid overlay path")
    if ".." in text.split("/"):
        raise OverlayError("invalid overlay path")
    root = text.split("/", 1)[0]
    if root not in ALLOWED_ROOTS:
        raise OverlayError(f"path must start with {', '.join(ALLOWED_ROOTS)}")
    suffix = Path(text).suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise OverlayError("only .yaml / .yml overlay files can be edited")
    base = config_dir.resolve()
    target = (base / text).resolve()
    try:
        target.relative_to(base)
    except ValueError as exc:
        raise OverlayError("invalid overlay path") from exc
    return target


def _is_helm_service(rel: str) -> bool:
    parts = rel.strip().lstrip("/").split("/")
    return (
        len(parts) >= 3
        and parts[0] == "helm-configs"
        and parts[1] != "global_overrides"
    )


def _service_name(rel: str) -> str:
    return rel.strip().lstrip("/").split("/")[1]


def _stat(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"size": 0, "mtime": None}
    st = path.stat()
    mtime = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat()
    return {"size": st.st_size, "mtime": mtime}


def _read_text(path: Path) -> str:
    if not path.is_file():
        return ""
    if path.stat().st_size > MAX_BYTES:
        raise OverlayError(f"file larger than {MAX_BYTES} bytes")
    return path.read_text(encoding="utf-8")


def _inside_dir(root: Path, path: Path) -> Path | None:
    try:
        resolved = path.resolve()
        resolved.relative_to(root)
    except (OSError, ValueError):
        return None
    return resolved


def _base_override_path(genestack_root: Path, rel: str) -> Path | None:
    if not rel.startswith("helm-configs/"):
        return None
    rest = rel[len("helm-configs/") :]
    try:
        base_root = (genestack_root / "base-helm-configs").resolve()
    except OSError:
        return None
    if not base_root.is_dir():
        return None
    service = rest.split("/", 1)[0]
    if not _safe_service(service):
        return None

    def _file(path: Path) -> Path | None:
        found = _inside_dir(base_root, path)
        if found is not None and found.is_file():
            return found
        return None

    hit = _file(base_root / rest)
    if hit is not None:
        return hit
    hit = _file(base_root / service / f"{service}-helm-overrides.yaml")
    if hit is not None:
        return hit
    service_dir = _inside_dir(base_root, base_root / service)
    if service_dir is None or not service_dir.is_dir():
        return None
    yamls = sorted(
        p
        for p in service_dir.glob("*.yaml")
        if p.is_file() and not p.name.endswith(".example") and _inside_dir(base_root, p)
    )
    if len(yamls) == 1:
        return yamls[0]
    return None


def _chart_versions(config_dir: Path, genestack_root: Path) -> dict[str, str]:
    for path in (
        config_dir / "helm-chart-versions.yaml",
        genestack_root / "helm-chart-versions.yaml",
    ):
        if not path.is_file():
            continue
        try:
            data = _load(path.read_text(encoding="utf-8")) or {}
        except (OSError, OverlayError, yaml.YAMLError):
            continue
        charts = data.get("charts") if isinstance(data, dict) else None
        if isinstance(charts, dict):
            out: dict[str, str] = {}
            for k, v in charts.items():
                if v is None:
                    continue
                safe = _safe_chart_version(str(v))
                if safe:
                    out[str(k)] = safe
            return out
    return {}


def _helm_run(argv: list[str], timeout: int = HELM_TIMEOUT) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, "", str(exc)
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def ensure_helm_repo(helm: str, name: str, url: str) -> bool:
    """Idempotent ``helm repo add``. OCI URLs need no add."""
    if not name or not url or url.startswith("oci://"):
        return True
    with _helm_lock:
        if name in _added_repos:
            return True
    code, _out, err = _helm_run(
        [helm, "repo", "add", name, url, "--force-update"],
        timeout=40,
    )
    if code == 0 or "already exists" in err.lower():
        with _helm_lock:
            _added_repos.add(name)
        return True
    log.info("helm repo add %s: %s", name, (err or "").strip().splitlines()[-1:] or err)
    return False


def load_helm_chart_values(
    service: str, version: str | None
) -> tuple[dict[str, Any], str | None]:
    """``helm show values`` for the mapped chart. Empty if helm/repos unavailable."""
    service = _safe_service(service) or ""
    version = _safe_chart_version(version)
    if not service or not version:
        return {}, None
    key = (service, str(version))
    now = time.time()
    with _helm_lock:
        hit = _HELM_CACHE.get(key)
        if hit and now - hit[0] < _HELM_CACHE_TTL:
            return copy.deepcopy(hit[1]), f"cache:{service}:{version}"
    helm = shutil.which("helm")
    if not helm:
        return {}, None
    refs: list[str] = []
    oci = OCI_CHARTS.get(service)
    if oci:
        refs.append(oci)
    mapped = HELM_CHARTS.get(service)
    if mapped:
        repo, url = mapped
        if url.startswith("oci://"):
            refs.append(f"{url.rstrip('/')}/{service}")
        elif ensure_helm_repo(helm, repo, url):
            refs.append(f"{repo}/{service}")
    elif ensure_helm_repo(
        helm, "openstack-helm", "https://tarballs.opendev.org/openstack/openstack-helm"
    ):
        refs.append(f"openstack-helm/{service}")
    last_err = None
    seen: set[str] = set()
    for ref in refs:
        if ref in seen:
            continue
        seen.add(ref)
        argv = [helm, "show", "values", ref]
        argv.extend(["--version", version])
        code, out, err = _helm_run(argv)
        if code != 0:
            last_err = (err or out).strip().splitlines()
            last_err = last_err[-1] if last_err else f"exit {code}"
            continue
        try:
            data = _load(out) or {}
        except (OverlayError, yaml.YAMLError):
            continue
        if not isinstance(data, dict):
            data = {}
        with _helm_lock:
            _HELM_CACHE[key] = (now, data)
        return copy.deepcopy(data), f"{ref}:{version}"
    if last_err:
        log.info("helm show values %s: %s", service, last_err)
    return {}, None


def _helm_layers(
    *,
    config_dir: Path,
    genestack_root: Path,
    rel: str,
    local_text: str,
) -> dict[str, Any]:
    service = _service_name(rel)
    versions = _chart_versions(config_dir, genestack_root)
    chart_data, chart_source = load_helm_chart_values(service, versions.get(service))
    base_path = _base_override_path(genestack_root, rel)
    base_text = _read_text(base_path) if base_path else ""
    base_data = _load(base_text) if isinstance(_load(base_text), dict) else {}
    local_data = _load(local_text)
    if not isinstance(local_data, dict):
        local_data = {}
    if not isinstance(chart_data, dict):
        chart_data = {}
    if not isinstance(base_data, dict):
        base_data = {}
    floor = merge_dicts(chart_data, base_data)
    merged = merge_dicts(floor, local_data)
    rel_base = None
    if base_path is not None:
        try:
            rel_base = str(base_path.relative_to(genestack_root))
        except ValueError:
            rel_base = str(base_path)
    return {
        "mode": "ye",
        "service": service,
        "chart_source": chart_source,
        "chart_content": _dump(chart_data) if chart_data else None,
        "base_path": rel_base,
        "base_content": base_text or None,
        "local_content": local_text or "{}\n",
        "content": _dump(merged),
        "floor": floor,
        "merged": merged,
        "local": local_data,
    }


def read_overlay(
    env: Environment,
    rel: str,
    settings: Settings | None = None,
) -> dict[str, Any]:
    settings = settings or get_settings()
    rel = str(rel or "").strip().lstrip("/")
    ctx = build_context(env, settings)
    try:
        if ctx.config_dir is None:
            raise OverlayError("no genestack config dir for this environment")
        path = resolve_overlay_path(ctx.config_dir, rel)
        helm = _is_helm_service(rel)
        if not path.is_file() and not helm:
            raise OverlayNotFound(rel)
        local_text = _read_text(path) if path.is_file() else ""
        if helm:
            layers = _helm_layers(
                config_dir=ctx.config_dir,
                genestack_root=ctx.genestack_root,
                rel=rel,
                local_text=local_text,
            )
            return {
                "ok": True,
                "path": rel,
                "error": None,
                "exists": path.is_file(),
                **_stat(path),
                "mode": "ye",
                "service": layers["service"],
                "content": layers["content"],
                "local_content": layers["local_content"],
                "base_path": layers["base_path"],
                "base_content": layers["base_content"],
                "chart_source": layers["chart_source"],
                "chart_content": layers["chart_content"],
            }
        base_path = _base_override_path(ctx.genestack_root, rel)
        base_text = _read_text(base_path) if base_path else None
        rel_base = None
        if base_path is not None:
            try:
                rel_base = str(base_path.relative_to(ctx.genestack_root))
            except ValueError:
                rel_base = str(base_path)
        return {
            "ok": True,
            "path": rel,
            "mode": "raw",
            "content": local_text,
            "local_content": local_text,
            "base_path": rel_base,
            "base_content": base_text,
            "chart_source": None,
            "chart_content": None,
            "error": None,
            "exists": True,
            **_stat(path),
        }
    finally:
        ctx.cleanup()


def _git_sync_result(
    *,
    attempted: bool = False,
    committed: bool = False,
    pushed: bool = False,
    sha: str | None = None,
    path: str | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    return {
        "attempted": attempted,
        "committed": committed,
        "pushed": pushed,
        "sha": sha,
        "path": path,
        "error": error,
    }


def _truncate_git_error(message: str, limit: int = _GIT_ERR_MAX) -> str:
    text = " ".join(str(message or "").split())
    if not text:
        return "git failed"
    text = _CRED_IN_URL_RE.sub(r"\1<redacted>@", text)
    text = _TOKEN_RE.sub("<redacted-token>", text)
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text


def _state_env_name_ok(name: str) -> bool:
    """Same path-segment rules as genestack.state.export (job_runner)."""
    return (
        bool(name)
        and "/" not in name
        and not name.startswith("-")
        and name not in (".", "..")
    )


_CHART_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,80}$")
_SERVICE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")
_GIT_REMOTE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_GIT_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,200}$")
_GIT_HOST_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
_CRED_IN_URL_RE = re.compile(r"(https?://)([^/@\s]+@)", re.IGNORECASE)
_TOKEN_RE = re.compile(
    r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9_]{8,}\b"
    r"|github_pat_[A-Za-z0-9_]{8,}"
    r"|glpat-[A-Za-z0-9_-]{8,}",
    re.IGNORECASE,
)
_GIT_SAFE_CONFIG = (
    "core.hooksPath=/dev/null",
    "protocol.file.allow=never",
    "protocol.ext.allow=never",
    "core.fsmonitor=false",
    "core.sshCommand=",
    "core.pager=cat",
)


def _safe_chart_version(version: str | None) -> str | None:
    text = str(version or "").strip()
    if not text or text.startswith("-") or not _CHART_VERSION_RE.match(text):
        return None
    return text


def _safe_service(name: str | None) -> str | None:
    text = str(name or "").strip()
    if not text or text.startswith("-") or not _SERVICE_RE.match(text):
        return None
    return text


def _git_host_ok(host: str) -> bool:
    text = str(host or "").strip().strip("[]")
    if not text or text.startswith("-"):
        return False
    return bool(_GIT_HOST_RE.match(text))


def _git_url_ok(url: str) -> bool:
    """https/http/ssh/git URL or git@host:path. No file://, ext::, or flag hosts."""
    text = str(url or "").strip()
    if not text or text.startswith("-"):
        return False
    lower = text.lower()
    if lower.startswith(("file:", "ext:", "fd:")):
        return False
    if text.startswith("git@"):
        host = text[4:].split(":", 1)[0].split("/", 1)[0]
        return _git_host_ok(host)
    if "://" not in text:
        return False
    scheme, rest = text.split("://", 1)
    if scheme.lower() not in ("https", "http", "ssh", "git"):
        return False
    authority = rest.split("/", 1)[0]
    if not authority or authority.startswith("-"):
        return False
    if "@" in authority:
        user, authority = authority.rsplit("@", 1)
        if not user or user.startswith("-"):
            return False
    host = authority.split(":")[0]
    return _git_host_ok(host)


def _git_remote_ok(remote: str) -> bool:
    """Named remote (origin) or https/ssh URL. No file://, no leading dash."""
    text = str(remote or "").strip()
    if not text or text.startswith("-") or text.startswith("file:"):
        return False
    if text.startswith(("https://", "http://", "ssh://", "git://", "git@")):
        return _git_url_ok(text)
    return bool(_GIT_REMOTE_NAME_RE.match(text))


def _git_branch_ok(branch: str) -> bool:
    text = str(branch or "").strip()
    if not text or text.startswith("-") or ".." in text.split("/"):
        return False
    return bool(_GIT_BRANCH_RE.match(text))


def git_safe_argv(args: list[str]) -> list[str]:
    extra: list[str] = []
    for item in _GIT_SAFE_CONFIG:
        extra.extend(["-c", item])
    rest = args[1:] if args and args[0] == "git" else list(args or [])
    return ["git", *extra, *rest]


def _run_git(
    args: list[str], cwd: Path, timeout: int = _GIT_TIMEOUT
) -> subprocess.CompletedProcess[str]:
    env_os = os.environ.copy()
    env_os["GIT_TERMINAL_PROMPT"] = "0"
    env_os["GIT_ASKPASS"] = "true"
    env_os["GIT_CONFIG_NOSYSTEM"] = "1"
    env_os["GIT_CONFIG_GLOBAL"] = "/dev/null"
    env_os["GIT_ALLOW_PROTOCOL"] = "https:http:ssh:git"
    argv = git_safe_argv(args)
    return subprocess.run(
        argv,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env=env_os,
    )


def _resolved_push_url(repo_root: Path, remote: str) -> str | None:
    """Look up the URL behind a named remote. URLs are returned as-is."""
    text = str(remote or "").strip()
    if not text:
        return None
    if text.startswith(("https://", "http://", "ssh://", "git://", "git@")):
        return text
    for extra in (["--push"], []):
        proc = _run_git(["git", "remote", "get-url", *extra, "--", text], cwd=repo_root)
        url = (proc.stdout or "").strip().splitlines()
        if proc.returncode == 0 and url:
            return url[0].strip()
    return None


def sync_overlay_to_state_repo(
    env: Environment, rel: str, content: str
) -> dict[str, Any]:
    """Copy a YE overlay into ``state/<env>/<rel>`` and commit (push if remote set).

    Git is optional: missing checkout is not an error. Exceptions never propagate.
    """
    state_repo_path = (getattr(env, "state_repo_path", None) or "").strip()
    if not state_repo_path:
        return _git_sync_result()

    repo_rel: str | None = None
    try:
        repo_root = Path(state_repo_path).expanduser()
        if not repo_root.is_dir():
            return _git_sync_result()
        if not (repo_root / ".git").exists():
            return _git_sync_result(
                attempted=True,
                error=_truncate_git_error(
                    f"state_repo_path '{state_repo_path}' is not a git checkout"
                ),
            )

        inside = _run_git(["git", "rev-parse", "--is-inside-work-tree"], cwd=repo_root)
        if inside.returncode != 0 or (inside.stdout or "").strip() != "true":
            return _git_sync_result(
                attempted=True,
                error=_truncate_git_error(
                    inside.stderr.strip()
                    or f"state_repo_path '{state_repo_path}' is not a git checkout"
                ),
            )

        env_name = (getattr(env, "name", None) or "").strip()
        if not _state_env_name_ok(env_name):
            return _git_sync_result(
                attempted=True,
                error=_truncate_git_error(
                    f"environment name '{env_name}' is not safe as a state repo path segment"
                ),
            )

        rel = str(rel or "").strip().lstrip("/")
        if not rel or ".." in rel.split("/"):
            return _git_sync_result(attempted=True, error="invalid overlay path")

        repo_rel = f"state/{env_name}/{rel}"
        target = repo_root / "state" / env_name / rel
        state_root = (repo_root / "state" / env_name).resolve()
        resolved = target.resolve()
        try:
            resolved.relative_to(state_root)
        except ValueError:
            return _git_sync_result(
                attempted=True, error="overlay path escapes state repo"
            )

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        target.chmod(0o644)

        added = _run_git(["git", "add", "--", repo_rel], cwd=repo_root)
        if added.returncode != 0:
            raise OSError(
                added.stderr.strip() or added.stdout.strip() or "git add failed"
            )

        diff = _run_git(
            ["git", "diff", "--cached", "--quiet", "--", repo_rel], cwd=repo_root
        )
        if diff.returncode == 0:
            return _git_sync_result(attempted=True, path=repo_rel)
        if diff.returncode != 1:
            raise OSError(
                diff.stderr.strip() or diff.stdout.strip() or "git diff --cached failed"
            )

        commit_msg = f"overlay({env.name}): {rel}"
        committed = _run_git(
            [
                "git",
                "-c",
                "user.name=genestack-console",
                "-c",
                "user.email=console@localhost",
                "commit",
                "-m",
                commit_msg,
                "--",
                repo_rel,
            ],
            cwd=repo_root,
        )
        if committed.returncode != 0:
            raise OSError(
                committed.stderr.strip()
                or committed.stdout.strip()
                or "git commit failed"
            )

        head = _run_git(["git", "rev-parse", "HEAD"], cwd=repo_root)
        sha = head.stdout.strip() if head.returncode == 0 else None

        pushed = False
        remote = (getattr(env, "state_repo_remote", None) or "").strip()
        if remote:
            if not _git_remote_ok(remote):
                return _git_sync_result(
                    attempted=True,
                    committed=True,
                    pushed=False,
                    sha=sha,
                    path=repo_rel,
                    error="invalid git remote",
                )
            resolved = _resolved_push_url(repo_root, remote)
            if not resolved or not _git_url_ok(resolved):
                return _git_sync_result(
                    attempted=True,
                    committed=True,
                    pushed=False,
                    sha=sha,
                    path=repo_rel,
                    error="invalid git remote",
                )
            branch_proc = _run_git(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_root
            )
            branch = (branch_proc.stdout or "").strip()
            if branch_proc.returncode != 0 or not branch:
                raise OSError(
                    branch_proc.stderr.strip() or "git rev-parse --abbrev-ref failed"
                )
            if not _git_branch_ok(branch):
                return _git_sync_result(
                    attempted=True,
                    committed=True,
                    pushed=False,
                    sha=sha,
                    path=repo_rel,
                    error="invalid git branch",
                )
            # Push to the configured remote/URL; do not add or rewrite remotes.
            push = _run_git(
                ["git", "push", "--", remote, branch], cwd=repo_root, timeout=60
            )
            if push.returncode != 0:
                return _git_sync_result(
                    attempted=True,
                    committed=True,
                    pushed=False,
                    sha=sha,
                    path=repo_rel,
                    error=_truncate_git_error(
                        push.stderr.strip() or push.stdout.strip() or "git push failed"
                    ),
                )
            pushed = True

        return _git_sync_result(
            attempted=True,
            committed=True,
            pushed=pushed,
            sha=sha,
            path=repo_rel,
        )
    except Exception as exc:
        log.info("overlay git sync failed: %s", _truncate_git_error(str(exc)))
        return _git_sync_result(
            attempted=True,
            path=repo_rel,
            error=_truncate_git_error(str(exc)),
        )


def write_overlay(
    env: Environment,
    rel: str,
    content: str,
    settings: Settings | None = None,
) -> dict[str, Any]:
    settings = settings or get_settings()
    rel = str(rel or "").strip().lstrip("/")
    if not isinstance(content, str):
        raise OverlayError("content must be text")
    raw = content.encode("utf-8")
    if len(raw) > MAX_BYTES:
        raise OverlayError(f"content larger than {MAX_BYTES} bytes")
    try:
        parsed = _load(content)
    except OverlayError:
        raise
    except yaml.YAMLError as exc:
        raise OverlayError(f"invalid YAML: {exc}") from exc
    ctx = build_context(env, settings)
    try:
        if ctx.config_dir is None:
            raise OverlayError("no genestack config dir for this environment")
        path = resolve_overlay_path(ctx.config_dir, rel)
        helm = _is_helm_service(rel)
        if helm:
            local_text = _read_text(path) if path.is_file() else ""
            layers = _helm_layers(
                config_dir=ctx.config_dir,
                genestack_root=ctx.genestack_root,
                rel=rel,
                local_text=local_text,
            )
            edited = parsed if isinstance(parsed, dict) else {}
            patch = compute_patch(layers["floor"], edited)
            dumped = _dump(patch)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(dumped, encoding="utf-8")
            result: dict[str, Any] = {
                "ok": True,
                "path": rel,
                "mode": "ye",
                "bytes": len(dumped.encode("utf-8")),
                "delta_keys": sorted(patch.keys()),
                "delta_content": dumped,
                "content": _dump(merge_dicts(layers["floor"], patch)),
                "error": None,
                **_stat(path),
            }
            file_content = dumped
        else:
            if not path.is_file():
                raise OverlayNotFound(rel)
            path.write_text(content, encoding="utf-8")
            result = {
                "ok": True,
                "path": rel,
                "mode": "raw",
                "bytes": len(raw),
                "error": None,
                **_stat(path),
            }
            file_content = content
        try:
            result["git"] = sync_overlay_to_state_repo(env, rel, file_content)
        except Exception as exc:
            log.info("overlay git sync failed: %s", _truncate_git_error(str(exc)))
            result["git"] = _git_sync_result(
                attempted=True,
                error=_truncate_git_error(str(exc)),
            )
        return result
    finally:
        ctx.cleanup()
