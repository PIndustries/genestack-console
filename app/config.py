"""Console settings — primarily from config.yaml (not a pile of env vars).

Only optional env var:
  CONSOLE_CONFIG  path to YAML config (default: <console>/config.yaml)

Everything else lives in that file, with auto-detection for paths.
A leftover ``maas:`` block in an older file is ignored.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

from app.paths import frozen, host_prefix, package_root

log = logging.getLogger(__name__)

CONSOLE_DIR = package_root()
DEFAULT_CONFIG_PATH = (
    host_prefix() / "config.yaml" if frozen() else CONSOLE_DIR / "config.yaml"
)
Role = Literal["viewer", "operator", "admin"]

# Publicly-known development credentials. Safe on loopback-only binds;
# refuse to start when they guard a server reachable off this host.
DEFAULT_SECRET_KEY = "change-me-in-production-console-secret"
DEFAULT_DEV_API_KEYS = ("dev-admin-key", "dev-operator-key", "dev-viewer-key")
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


def assert_safe_bind(settings: "Settings") -> None:
    """Refuse to start on a non-loopback bind with default credentials.

    Loopback binds stay dev-friendly (the dev keys and default secret are
    fine when only this host can reach the console). Anything else with
    publicly-known credentials is a fail-closed startup error naming the
    fix — set real auth.api_keys and secret_key in config.yaml.
    """
    host = (settings.host or "").strip().lower().strip("[]")
    if host in LOOPBACK_HOSTS:
        return
    problems: list[str] = []
    dev_keys = sorted(set(DEFAULT_DEV_API_KEYS) & set(settings.parsed_api_keys()))
    if dev_keys:
        problems.append("default dev API key(s) active: " + ", ".join(dev_keys))
    if settings.secret_key == DEFAULT_SECRET_KEY:
        problems.append("secret_key is the built-in default")
    if settings.dev_auto_login:
        problems.append(
            "auth.dev_auto_login is enabled (unauthenticated requests become platform-admin)"
        )
    if (settings.terminal_command_override or "").strip():
        problems.append("terminal.command_override is set on a non-loopback bind")
    if problems:
        raise RuntimeError(
            f"Refusing to start: binding non-loopback host '{settings.host}' with "
            + "; ".join(problems)
            + ". These credentials are publicly known — set auth.api_keys and "
            "secret_key in config.yaml before exposing the console."
        )


def _genestack_probe_candidates() -> list[Path]:
    """Ordered probe list: dev checkout layout first, then container mount paths."""
    return [
        CONSOLE_DIR.parent,
        Path("/genestack"),
        Path("/opt/genestack"),
        Path.cwd().parent,
    ]


def _detect_genestack_root() -> Path:
    """Best-effort Genestack checkout (parent of console, /genestack, /opt/genestack, …)."""
    ordered = _genestack_probe_candidates()
    for path in ordered:
        if (path / "bin").is_dir() and (path / "openstack-components.yaml").is_file():
            return path.resolve()
    if CONSOLE_DIR.parent.is_dir():
        log.error(
            "genestack root not found — probed %s for bin/ + openstack-components.yaml; "
            "set genestack.root in config.yaml",
            ", ".join(str(p) for p in ordered),
        )
        return CONSOLE_DIR.parent.resolve()
    return Path("/opt/genestack")


def _default_ansible_root() -> Path:
    local = CONSOLE_DIR / "ansible"
    if local.is_dir():
        return local.resolve()
    genestack = _detect_genestack_root() / "ansible"
    if genestack.is_dir():
        return genestack.resolve()
    return local.resolve()


def _deep_get(data: dict[str, Any], *keys: str, default: Any = None) -> Any:
    cur: Any = data
    for key in keys:
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return cur


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Config root must be a mapping: {path}")
    return data


class Settings(BaseModel):
    """Runtime settings resolved from config.yaml + auto-detect."""

    config_path: Path = DEFAULT_CONFIG_PATH
    data_dir: Path = Field(default_factory=lambda: host_prefix() / "data")
    database_url: str = "sqlite:///./data/console.db"
    api_keys: dict[str, Role] = Field(
        default_factory=lambda: {
            "dev-admin-key": "admin",
            "dev-operator-key": "operator",
            "dev-viewer-key": "viewer",
        }
    )
    genestack_root: str = ""
    ansible_root: str = ""
    # Extra operation modules. Each entry is a directory with __init__.py
    # (a Module subclass) and one Python file per operation. Relative paths
    # in config.yaml are resolved from that file's directory.
    module_paths: list[str] = Field(default_factory=list)
    dry_run: bool = True
    # First-boot walkthrough tenant/env. Installer sets true; tests stay false.
    seed_demo: bool = False
    # OVH dedicated-server import. The app key/secret identify the console as
    # an OVH "application" (set once by the operator); the consumer key is
    # per-environment (created via the in-console flow) and stored encrypted on
    # the Environment row, not here.
    ovh_endpoint: str = ""
    ovh_app_key: str = ""
    ovh_app_secret: str = ""
    job_timeout_seconds: int = 600
    session_ttl_hours: int = 12
    # How long a refresh token can mint a new session. Longer than the
    # bearer token so a client can continue after the bearer expires.
    refresh_ttl_hours: int = 168
    collector_enabled: bool = True
    collector_interval_seconds: int = 60
    collector_probe_timeout_seconds: int = 15
    # 7 days of snapshots
    collector_retention_hours: int = 168
    # Retention sweep (worker daemon, daily-ish): bounded growth for the
    # non-snapshot tables. Days are max(1, ...) clamped; the versions keep
    # count keeps the newest N per environment.
    retention_jobs_days: int = 30
    retention_audit_days: int = 90
    retention_agent_commands_days: int = 7
    retention_alert_events_days: int = 30
    retention_env_config_versions_keep: int = 50
    metrics_enabled: bool = False
    metrics_retention_hours: int = 72
    hypervisor_enabled: bool = True
    # Directories scanned for QEMU pidfiles/VMs (genestack lab/dev node VMs).
    hypervisor_roots: list[str] = Field(default_factory=lambda: ["/var/lib/genestack/vms"])
    # Root helper (via NOPASSWD sudo) used when QEMU processes/files are not
    # owned by the console user. Only this exact path is allowed by sudoers.
    hypervisor_sudo_helper: str = "/usr/local/sbin/gsc-qemu-ctl"
    # External URL agents/targets use to reach this hub (e.g.
    # http://192.0.2.1:8080). Required for agent.install: remote hosts need
    # a hub URL THEY can reach, not one derived from the API client's headers.
    hub_advertise_url: str = ""
    stream_max_subscribers: int = 100
    stream_relay_enabled: bool = True
    stream_relay_interval_seconds: float = 3.0
    # DB-backed agent command relay (API process): polls agent_commands rows
    # and dispatches them to connected agents so the worker daemon process
    # can route commands/file-writes through agents (app/services/agent_relay.py).
    agent_relay_enabled: bool = True
    agent_relay_interval_seconds: float = 2.0
    # Which environment receives the auto "local-agent" credential seeded at
    # startup (config.yaml ``agent.default_environment_id``). Empty = use the
    # single env, or skip seeding if several exist (never guess across tenants).
    agent_default_environment_id: str = ""
    # Replaces the terminal's ssh argv wholesale (shlex-split) — test/CI hook so
    # the pty bridge can run e.g. /bin/cat instead of sshing to a real host.
    terminal_command_override: str = ""
    cors_allow_origins: list[str] = Field(default_factory=lambda: ["*"])
    # DEV ONLY: authenticate every request as platform-admin without credentials.
    # Toggle for local development; MUST stay false in production.
    dev_auto_login: bool = False
    host: str = "0.0.0.0"
    port: int = 8080
    secret_key: str = "change-me-in-production-console-secret"
    # Optional OIDC/SSO login (oidc: section). The feature is active only when
    # oidc_enabled is true AND oidc_issuer_url is non-empty; local accounts and
    # static API keys always remain available as a fallback.
    oidc_enabled: bool = False
    oidc_issuer_url: str = ""
    oidc_client_id: str = ""
    oidc_client_secret: str = ""
    oidc_redirect_url: str = ""
    oidc_default_tenant: str = ""
    oidc_default_role: Role = "viewer"
    oidc_label: str = "SSO"
    update_url: str = "https://github.com/PIndustries/genestack-console/releases/latest/download/version.json"
    update_auto: bool = False
    # Pull GitHub releases when the channel has no stack pin (open-source
    # installs cannot be pushed). Empty disables the GitHub fallback.
    update_github_repo: str = "rackerlabs/genestack"
    # After too many "not now" answers on an EOL component, queue the hop.
    # Default off — notify, don't surprise-roll a live cloud.
    update_eol_force: bool = False
    update_refusal_limit: int = 3
    # Console-managed WireGuard overlay (hub is the server; agents get a peer
    # on first adopt). Separate from any other overlay already on the hosts.
    wg_enabled: bool = False
    wg_interface: str = "wg-gsc"
    wg_address: str = ""
    wg_listen_port: int = 51820
    wg_network: str = "10.67.67.0/24"
    wg_endpoint: str = ""
    wg_private_key: str = ""

    def parsed_api_keys(self) -> dict[str, Role]:
        return dict(self.api_keys)


def _is_placeholder(value: str) -> bool:
    """True for shipped config.yaml.example placeholders (REPLACE_ME, change-me).

    The example file documents the values to replace but must never be taken
    as real credentials: treating placeholders as "unset" lets the built-in
    default warnings and the non-loopback fail-closed check (assert_safe_bind)
    do their job instead of silently encrypting secrets under a public key.
    """
    marker = value.strip().lower()
    return "replace_me" in marker or "replace-me" in marker or marker == "change-me"


def _parse_api_keys(raw: Any) -> dict[str, Role]:
    result: dict[str, Role] = {}
    if isinstance(raw, dict):
        for key, role in raw.items():
            r = str(role).strip().lower()
            k = str(key).strip()
            if r in ("viewer", "operator", "admin") and k and not _is_placeholder(k):
                result[k] = r  # type: ignore[assignment]
        return result
    if isinstance(raw, str):
        # legacy "key:role,key:role" if someone still pastes it into yaml as a string
        for part in raw.split(","):
            part = part.strip()
            if ":" not in part:
                continue
            key, role = part.rsplit(":", 1)
            r = role.strip().lower()
            if (
                r in ("viewer", "operator", "admin")
                and key.strip()
                and not _is_placeholder(key)
            ):
                result[key.strip()] = r  # type: ignore[assignment]
    return result


def _real_secret(raw_value: Any) -> str:
    """Placeholder/empty secret_key resolves to the built-in default.

    The default is what the startup warning and the non-loopback fail-closed
    check key off — a REPLACE_ME value must never be used to encrypt stored
    secrets as if it were a unique per-install key.
    """
    value = str(raw_value or "").strip()
    if not value or _is_placeholder(value):
        return DEFAULT_SECRET_KEY
    return value


def load_settings(config_path: Path | None = None) -> Settings:
    """Load settings from YAML; fill gaps with auto-detect and safe defaults."""
    import os

    path = config_path
    if path is None:
        env_path = os.environ.get("CONSOLE_CONFIG", "").strip()
        path = Path(env_path).expanduser() if env_path else DEFAULT_CONFIG_PATH
    path = (
        path.resolve()
        if path.exists() or path.is_absolute()
        else (CONSOLE_DIR / path).resolve()
    )

    raw = _load_yaml(path) if path.is_file() else {}

    data_dir_raw = raw.get("data_dir", "./data")
    data_dir = Path(str(data_dir_raw)).expanduser()
    if not data_dir.is_absolute():
        # Frozen onefile extracts to a temp dir (CONSOLE_DIR). Writable state
        # must live on the host prefix, not inside the extract.
        data_dir = (host_prefix() / data_dir).resolve()
    else:
        data_dir = data_dir.resolve()
    data_dir.mkdir(parents=True, exist_ok=True)

    db_url = raw.get("database_url")
    if not db_url:
        db_url = f"sqlite:///{data_dir / 'console.db'}"

    auth = raw.get("auth") if isinstance(raw.get("auth"), dict) else {}
    keys = _parse_api_keys(auth.get("api_keys") if auth else None)
    if not keys:
        keys = {
            "dev-admin-key": "admin",
            "dev-operator-key": "operator",
            "dev-viewer-key": "viewer",
        }

    dev_auto_login = auth.get("dev_auto_login", False)
    if isinstance(dev_auto_login, str):
        dev_auto_login = dev_auto_login.strip().lower() in {"1", "true", "yes", "on"}

    gs_section = raw.get("genestack") if isinstance(raw.get("genestack"), dict) else {}
    gs_root = gs_section.get("root") if gs_section else raw.get("genestack_root")
    if gs_root:
        genestack_root = str(Path(str(gs_root)).expanduser().resolve())
    else:
        genestack_root = str(_detect_genestack_root())

    ans_section = raw.get("ansible") if isinstance(raw.get("ansible"), dict) else {}
    ans_root = ans_section.get("root") if ans_section else raw.get("ansible_root")
    if ans_root:
        ansible_root = str(Path(str(ans_root)).expanduser().resolve())
    else:
        ansible_root = str(_default_ansible_root())

    modules_section = raw.get("modules") if isinstance(raw.get("modules"), dict) else {}
    raw_paths = modules_section.get("paths", [])
    if isinstance(raw_paths, str):
        module_path_items = [item.strip() for item in raw_paths.split(",") if item.strip()]
    elif isinstance(raw_paths, list):
        module_path_items = [str(item).strip() for item in raw_paths if str(item).strip()]
    else:
        module_path_items = []
    module_base = path.parent if path.is_file() else Path.cwd()
    module_paths: list[str] = []
    for item in module_path_items:
        module_path = Path(item).expanduser()
        if not module_path.is_absolute():
            module_path = (module_base / module_path).resolve()
        else:
            module_path = module_path.resolve()
        module_paths.append(str(module_path))

    # Older config.yaml files may still contain a maas: block. It is unused.
    raw.pop("maas", None)
    raw.pop("maas_url", None)
    raw.pop("maas_api_key", None)
    raw.pop("maas_mock", None)

    ovh = raw.get("ovh") if isinstance(raw.get("ovh"), dict) else {}
    ovh_endpoint = str(ovh.get("endpoint") or raw.get("ovh_endpoint") or "").strip()
    ovh_app_key = str(ovh.get("app_key") or raw.get("ovh_app_key") or "").strip()
    ovh_app_secret = str(
        ovh.get("app_secret") or raw.get("ovh_app_secret") or ""
    ).strip()

    server = raw.get("server") if isinstance(raw.get("server"), dict) else {}
    jobs = raw.get("jobs") if isinstance(raw.get("jobs"), dict) else {}
    collector = raw.get("collector") if isinstance(raw.get("collector"), dict) else {}
    retention = raw.get("retention") if isinstance(raw.get("retention"), dict) else {}
    metrics = raw.get("metrics") if isinstance(raw.get("metrics"), dict) else {}
    stream = raw.get("stream") if isinstance(raw.get("stream"), dict) else {}
    agent = raw.get("agent") if isinstance(raw.get("agent"), dict) else {}
    hub = raw.get("hub") if isinstance(raw.get("hub"), dict) else {}
    cors = raw.get("cors") if isinstance(raw.get("cors"), dict) else {}
    hypervisor = (
        raw.get("hypervisor") if isinstance(raw.get("hypervisor"), dict) else {}
    )
    terminal = raw.get("terminal") if isinstance(raw.get("terminal"), dict) else {}
    oidc = raw.get("oidc") if isinstance(raw.get("oidc"), dict) else {}

    dry_run = raw.get("dry_run", True)
    if isinstance(dry_run, str):
        dry_run = dry_run.strip().lower() in {"1", "true", "yes", "on"}

    seed_demo = raw.get("seed_demo", False)
    if isinstance(seed_demo, str):
        seed_demo = seed_demo.strip().lower() in {"1", "true", "yes", "on"}

    timeout = jobs.get("timeout_seconds", raw.get("job_timeout_seconds", 600))
    try:
        timeout = int(timeout)
    except (TypeError, ValueError):
        timeout = 600

    try:
        session_ttl_hours = int(auth.get("session_ttl_hours", 12))
    except (TypeError, ValueError):
        session_ttl_hours = 12
    try:
        refresh_ttl_hours = int(auth.get("refresh_ttl_hours", 168))
    except (TypeError, ValueError):
        refresh_ttl_hours = 168

    collector_enabled = collector.get("enabled", True)
    if isinstance(collector_enabled, str):
        collector_enabled = collector_enabled.strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }

    metrics_enabled = metrics.get("enabled", False)
    if isinstance(metrics_enabled, str):
        metrics_enabled = metrics_enabled.strip().lower() in {"1", "true", "yes", "on"}

    hypervisor_enabled = hypervisor.get("enabled", True)
    if isinstance(hypervisor_enabled, str):
        hypervisor_enabled = hypervisor_enabled.strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }

    oidc_enabled = oidc.get("enabled", False)
    if isinstance(oidc_enabled, str):
        oidc_enabled = oidc_enabled.strip().lower() in {"1", "true", "yes", "on"}
    oidc_default_role = str(oidc.get("default_role") or "viewer").strip().lower()
    if oidc_default_role not in ("viewer", "operator", "admin"):
        oidc_default_role = "viewer"

    hv_roots_raw = hypervisor.get("roots")
    if isinstance(hv_roots_raw, str):
        hypervisor_roots = [r.strip() for r in hv_roots_raw.split(",") if r.strip()]
    elif isinstance(hv_roots_raw, list):
        hypervisor_roots = [str(r).strip() for r in hv_roots_raw if str(r).strip()]
    else:
        hypervisor_roots = ["/var/lib/genestack/vms"]

    hypervisor_sudo_helper = str(
        hypervisor.get("sudo_helper") or "/usr/local/sbin/gsc-qemu-ctl"
    ).strip()

    cors_origins_raw = cors.get("allow_origins")
    if isinstance(cors_origins_raw, str):
        cors_allow_origins = [
            o.strip() for o in cors_origins_raw.split(",") if o.strip()
        ]
    elif isinstance(cors_origins_raw, list):
        cors_allow_origins = [
            str(o).strip() for o in cors_origins_raw if str(o).strip()
        ]
    else:
        cors_allow_origins = ["*"]
    if not cors_allow_origins:
        cors_allow_origins = ["*"]

    stream_relay_enabled = stream.get("relay_enabled", True)
    if isinstance(stream_relay_enabled, str):
        stream_relay_enabled = stream_relay_enabled.strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }

    agent_relay_enabled = agent.get("relay_enabled", True)
    if isinstance(agent_relay_enabled, str):
        agent_relay_enabled = agent_relay_enabled.strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }

    agent_default_environment_id = str(
        agent.get("default_environment_id") or ""
    ).strip()

    wg = raw.get("wireguard") if isinstance(raw.get("wireguard"), dict) else {}
    wg_enabled = wg.get("enabled", False)
    if isinstance(wg_enabled, str):
        wg_enabled = wg_enabled.strip().lower() in {"1", "true", "yes", "on"}

    upd = raw.get("update") if isinstance(raw.get("update"), dict) else {}
    update_url = (
        str(
            upd.get("url")
            or raw.get("update_url")
            or "https://github.com/PIndustries/genestack-console/releases/latest/download/version.json"
        ).strip()
        or "https://github.com/PIndustries/genestack-console/releases/latest/download/version.json"
    )
    update_auto = upd.get("auto", False)
    if isinstance(update_auto, str):
        update_auto = update_auto.strip().lower() in {"1", "true", "yes", "on"}
    update_github_repo = (
        str(upd.get("github_repo") or "rackerlabs/genestack").strip()
        or "rackerlabs/genestack"
    )
    update_eol_force = upd.get("eol_force", False)
    if isinstance(update_eol_force, str):
        update_eol_force = update_eol_force.strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }

    def _int_or(value: Any, fallback: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return fallback

    def _float_or(value: Any, fallback: float) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return fallback

    update_refusal_limit = max(1, min(_int_or(upd.get("refusal_limit"), 3), 20))

    return Settings(
        config_path=path if path.is_file() else DEFAULT_CONFIG_PATH,
        data_dir=data_dir,
        database_url=str(db_url),
        api_keys=keys,
        genestack_root=genestack_root,
        ansible_root=ansible_root,
        module_paths=module_paths,
        dry_run=bool(dry_run),
        seed_demo=bool(seed_demo),
        ovh_endpoint=ovh_endpoint,
        ovh_app_key=ovh_app_key,
        ovh_app_secret=ovh_app_secret,
        job_timeout_seconds=max(30, min(int(timeout), 86400)),
        session_ttl_hours=max(1, session_ttl_hours),
        refresh_ttl_hours=max(1, refresh_ttl_hours),
        collector_enabled=bool(collector_enabled),
        collector_interval_seconds=max(
            5, _int_or(collector.get("interval_seconds"), 60)
        ),
        collector_probe_timeout_seconds=max(
            1, _int_or(collector.get("probe_timeout_seconds"), 15)
        ),
        collector_retention_hours=max(
            1, _int_or(collector.get("retention_hours"), 168)
        ),
        retention_jobs_days=max(1, _int_or(retention.get("jobs_days"), 30)),
        retention_audit_days=max(1, _int_or(retention.get("audit_days"), 90)),
        retention_agent_commands_days=max(
            1, _int_or(retention.get("agent_commands_days"), 7)
        ),
        retention_alert_events_days=max(
            1, _int_or(retention.get("alert_events_days"), 30)
        ),
        retention_env_config_versions_keep=max(
            1, _int_or(retention.get("env_config_versions_keep"), 50)
        ),
        metrics_enabled=bool(metrics_enabled),
        metrics_retention_hours=max(1, _int_or(metrics.get("retention_hours"), 72)),
        hypervisor_enabled=bool(hypervisor_enabled),
        hypervisor_roots=[str(Path(r).expanduser()) for r in hypervisor_roots],
        hypervisor_sudo_helper=hypervisor_sudo_helper,
        stream_max_subscribers=max(1, _int_or(stream.get("max_subscribers"), 100)),
        stream_relay_enabled=bool(stream_relay_enabled),
        stream_relay_interval_seconds=max(
            0.1, _float_or(stream.get("relay_interval_seconds"), 3.0)
        ),
        agent_relay_enabled=bool(agent_relay_enabled),
        agent_relay_interval_seconds=max(
            0.1, _float_or(agent.get("relay_interval_seconds"), 2.0)
        ),
        agent_default_environment_id=agent_default_environment_id,
        terminal_command_override=str(terminal.get("command_override") or "").strip(),
        cors_allow_origins=cors_allow_origins,
        hub_advertise_url=str(hub.get("advertise_url") or "").strip(),
        dev_auto_login=bool(dev_auto_login),
        host=str(server.get("host") or raw.get("host") or "0.0.0.0"),
        port=int(server.get("port") or raw.get("port") or 8080),
        secret_key=_real_secret(raw.get("secret_key")),
        oidc_enabled=bool(oidc_enabled),
        oidc_issuer_url=str(oidc.get("issuer_url") or "").strip(),
        oidc_client_id=str(oidc.get("client_id") or "").strip(),
        oidc_client_secret=str(oidc.get("client_secret") or ""),
        oidc_redirect_url=str(oidc.get("redirect_url") or "").strip(),
        oidc_default_tenant=str(oidc.get("default_tenant") or "").strip(),
        oidc_default_role=oidc_default_role,  # type: ignore[arg-type]
        oidc_label=str(oidc.get("label") or "SSO").strip() or "SSO",
        update_url=update_url,
        update_auto=bool(update_auto),
        update_github_repo=update_github_repo,
        update_eol_force=bool(update_eol_force),
        update_refusal_limit=update_refusal_limit,
        wg_enabled=bool(wg_enabled),
        wg_interface=str(wg.get("interface") or "wg-gsc").strip() or "wg-gsc",
        wg_address=str(wg.get("address") or "").strip(),
        wg_listen_port=max(1, min(_int_or(wg.get("listen_port"), 51820), 65535)),
        wg_network=str(wg.get("network") or "10.67.67.0/24").strip() or "10.67.67.0/24",
        wg_endpoint=str(wg.get("endpoint") or "").strip(),
        wg_private_key=str(wg.get("private_key") or "").strip(),
    )


@lru_cache
def get_settings() -> Settings:
    return load_settings()


def reload_settings(config_path: Path | None = None) -> Settings:
    """Clear cache and reload (tests / config changes)."""
    get_settings.cache_clear()
    if config_path is not None:
        # Temporarily set CONSOLE_CONFIG for this load
        import os

        os.environ["CONSOLE_CONFIG"] = str(config_path)
    return get_settings()
