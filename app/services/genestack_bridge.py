"""Map console operations to genestack bin scripts, ansible, and local paths.

Commands normally execute on the console host. When an environment sets a
deploy host (env.deployer_ssh_host), run_command can wrap execution as
``ssh -o BatchMode=yes -o ConnectTimeout=10 <target> '<assignments> cd <cwd> && <cmd>'``
so the same genestack scripts run on that environment's own deploy host.
Only the genestack-scoped variables (remote_env) are sent over ssh — never
the console's whole process environment.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable

import yaml

from app.config import Settings, get_settings
from app.models import Environment
from app.services.catalog import PLAYBOOK_ALLOWLIST
from app.services.service_registry import SERVICE_NAME_RE, discover_deployable_services

LogFn = Callable[[str], None]


def _log(log: LogFn | None, msg: str) -> None:
    if log:
        log(msg)


def resolve_genestack_root(
    settings: Settings | None = None,
    env: Environment | None = None,
) -> Path:
    settings = settings or get_settings()
    if env and env.genestack_path:
        return Path(env.genestack_path).expanduser().resolve()
    return Path(settings.genestack_root).expanduser().resolve()


def resolve_ansible_root(settings: Settings | None = None) -> Path:
    settings = settings or get_settings()
    return Path(settings.ansible_root).expanduser().resolve()


def resolve_components_path(
    settings: Settings | None = None,
    env: Environment | None = None,
) -> tuple[Path, str]:
    """Resolve openstack-components.yaml for an env, with its scope.

    Env config dir wins (scope "environment"); otherwise the repo-root copy
    (scope "global", legacy behavior). Reads and writes share this rule.
    """
    if env and env.genestack_config_dir:
        config_dir = Path(env.genestack_config_dir).expanduser().resolve()
        return config_dir / "openstack-components.yaml", "environment"
    return resolve_genestack_root(settings, env) / "openstack-components.yaml", "global"


def list_install_scripts(genestack_root: Path) -> list[dict[str, str]]:
    bin_dir = genestack_root / "bin"
    if not bin_dir.is_dir():
        return []
    scripts = sorted(bin_dir.glob("install-*.sh"))
    return [
        {
            "name": s.name,
            "path": str(s),
            "service": s.name.removeprefix("install-").removesuffix(".sh"),
        }
        for s in scripts
    ]


def read_components_desired(components_path: Path) -> dict[str, Any]:
    """Read an openstack-components.yaml file (resolved path)."""
    path = Path(components_path)
    if not path.is_file():
        return {
            "path": str(path),
            "exists": False,
            "components": {},
            "error": f"File not found: {path}",
        }
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    components = data.get("components") if isinstance(data, dict) else {}
    return {
        "path": str(path),
        "exists": True,
        "components": components or {},
        "raw_keys": list((data or {}).keys()) if isinstance(data, dict) else [],
    }


def smoke_check(genestack_root: Path, ansible_root: Path) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"name": name, "ok": ok, "detail": detail})

    check("genestack_root_exists", genestack_root.is_dir(), str(genestack_root))
    check("bin_dir", (genestack_root / "bin").is_dir(), str(genestack_root / "bin"))
    check(
        "openstack_components",
        (genestack_root / "openstack-components.yaml").is_file(),
        str(genestack_root / "openstack-components.yaml"),
    )
    check("ansible_root", ansible_root.is_dir(), str(ansible_root))
    scripts = list_install_scripts(genestack_root)
    check("install_scripts", len(scripts) > 0, f"{len(scripts)} scripts")
    ansible_bin = shutil.which("ansible-playbook")
    check("ansible_playbook_on_path", bool(ansible_bin), ansible_bin or "not found")

    return {
        "ok": all(c["ok"] for c in checks if c["name"] != "ansible_playbook_on_path"),
        "checks": checks,
        "script_count": len(scripts),
    }


def _ssh_wrap(
    cmd: list[str],
    ssh_target: str,
    cwd: Path | None,
    remote_env: dict[str, str] | None,
) -> tuple[list[str], str]:
    """Build the ssh argv for running ``cmd`` on a remote deploy host.

    The remote string (a single argv element) prefixes the genestack-scoped
    env assignments, then ``cd <cwd> && <cmd>``. The returned display string
    is the shell-safe form (remote string quoted) used for logs/dry-run.
    """
    parts = []
    for key, value in (remote_env or {}).items():
        # Env names are interpolated unquoted into the remote shell string;
        # refuse anything that is not a plain identifier (defense in depth —
        # doc_env already validates OVN_* keys, remote_env only sets scoped
        # keys).
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(key)):
            raise ValueError(f"invalid environment variable name: {key!r}")
        parts.append(f"{key}={shlex.quote(str(value))}")
    if cwd:
        parts.append(f"cd {shlex.quote(str(cwd))} &&")
    parts.append(shlex.join([str(c) for c in cmd]))
    remote = " ".join(parts)
    ssh_cmd = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "UserKnownHostsFile=/dev/null",
        ssh_target,
        remote,
    ]
    return ssh_cmd, shlex.join(ssh_cmd)


def run_command(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    timeout: int = 600,
    dry_run: bool = True,
    env: dict[str, str] | None = None,
    extra_env: dict[str, str] | None = None,
    ssh_target: str | None = None,
    remote_env: dict[str, str] | None = None,
    agent_env_id: str | None = None,
    input_text: str | None = None,
    log: LogFn | None = None,
) -> dict[str, Any]:
    """Run a subprocess or dry-run log it. Returns result dict.

    When ``ssh_target`` is set, execution is wrapped as an ssh call to the
    env's deploy host: the remote command carries only ``remote_env``
    assignments, and the local ``env``/``extra_env`` are not forwarded (ssh
    itself runs locally with the console's own environment).

    When ``agent_env_id`` is set (and this is not a dry run), execution goes
    through the env's connected agent via the DB-backed relay
    (app/services/agent_relay.agent_exec) — never the in-process registry —
    so it works from any process, including the worker daemon. Like ssh, the
    agent gets only ``remote_env`` assignments. Dry-run is unchanged: the
    command is logged, nothing is dispatched.

    ``input_text`` is piped to the process stdin (over ssh it reaches the
    remote command — e.g. streaming a script into ``bash -s``); it is not
    supported over the agent channel and is ignored there.
    """
    ssh_cmd: list[str] | None = None
    if ssh_target:
        ssh_cmd, display = _ssh_wrap(cmd, ssh_target, cwd, remote_env)
    else:
        display = " ".join(cmd)
    run_argv = ssh_cmd if ssh_cmd is not None else cmd
    _log(log, f"$ {display}")
    if cwd:
        _log(log, f"  cwd={cwd}")

    if dry_run:
        _log(log, "[dry-run] command not executed")
        if input_text is not None:
            _log(log, f"[dry-run] would pipe {len(input_text)} bytes to stdin")
        return {
            "dry_run": True,
            "cmd": run_argv,
            "returncode": 0,
            "stdout": "",
            "stderr": "",
            "message": f"Would run: {display}",
        }

    if agent_env_id:
        # Agent channel: route through the DB-backed relay (works from any
        # process); the agent receives cmd/cwd/timeout plus remote_env only.
        from app.services import agent_relay

        _log(log, f"[agent] executing via the connected agent for env {agent_env_id}")
        reply = agent_relay.agent_exec(
            agent_env_id,
            "run_command",
            {
                "cmd": [str(c) for c in cmd],
                "cwd": str(cwd) if cwd else None,
                "env": dict(remote_env or {}),
                "timeout": timeout,
            },
            timeout=timeout,
            log_cb=lambda line: _log(log, line),
        )
        rc = reply.get("rc")
        error = reply.get("error")
        returncode = rc if rc is not None else 2
        stdout = reply.get("stdout") or ""
        stderr = reply.get("stderr") or (str(error) if error else "")
        if error:
            _log(log, f"[agent] error: {error}")
        _log(log, f"[exit] returncode={returncode}")
        message = (
            "ok"
            if returncode == 0
            else (str(error) if error else f"failed rc={returncode}")
        )
        return {
            "dry_run": False,
            "cmd": cmd,
            "via": "agent",
            "returncode": returncode,
            "stdout": stdout,
            "stderr": stderr,
            "message": message,
        }

    if ssh_cmd is not None:
        # ssh runs locally; only ssh's own env matters — the remote side gets
        # its assignments inline from remote_env (already baked into ssh_cmd).
        run_cwd = None
        run_env = None
    else:
        run_cwd = cwd
        if extra_env:
            # extra_env overrides either the explicit env or the process env
            env = {**(env if env is not None else os.environ), **extra_env}
        run_env = env

    try:
        proc = subprocess.run(
            run_argv,
            cwd=str(run_cwd) if run_cwd else None,
            capture_output=True,
            text=True,
            input=input_text,
            timeout=timeout,
            env=run_env,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        _log(log, f"[timeout] after {timeout}s")
        return {
            "dry_run": False,
            "cmd": run_argv,
            "returncode": 124,
            "stdout": (exc.stdout or "") if isinstance(exc.stdout, str) else "",
            "stderr": (
                (exc.stderr or "")
                if isinstance(exc.stderr, str)
                else f"Timeout after {timeout}s"
            ),
            "message": f"Command timed out after {timeout}s",
        }
    except FileNotFoundError as exc:
        _log(log, f"[error] executable not found: {exc}")
        return {
            "dry_run": False,
            "cmd": run_argv,
            "returncode": 127,
            "stdout": "",
            "stderr": str(exc),
            "message": str(exc),
        }

    if proc.stdout:
        _log(log, proc.stdout.rstrip())
    if proc.stderr:
        _log(log, proc.stderr.rstrip())
    _log(log, f"[exit] returncode={proc.returncode}")

    return {
        "dry_run": False,
        "cmd": run_argv,
        "returncode": proc.returncode,
        "stdout": proc.stdout or "",
        "stderr": proc.stderr or "",
        "message": "ok" if proc.returncode == 0 else f"failed rc={proc.returncode}",
    }


def enable_service(
    service: str,
    genestack_root: Path,
    *,
    dry_run: bool = True,
    timeout: int = 600,
    extra_env: dict[str, str] | None = None,
    ssh_target: str | None = None,
    remote_env: dict[str, str] | None = None,
    agent_env_id: str | None = None,
    log: LogFn | None = None,
) -> dict[str, Any]:
    service = service.strip().lower()
    deployable = discover_deployable_services(genestack_root)
    if not SERVICE_NAME_RE.match(service) or service not in deployable:
        msg = (
            f"Service '{service}' is not a discovered deployable service "
            f"(bin/install-*.sh under {genestack_root})"
        )
        _log(log, f"[denied] {msg}")
        return {"ok": False, "error": msg, "returncode": 2}

    script = genestack_root / "bin" / f"install-{service}.sh"
    if not script.is_file():
        msg = f"Install script not found: {script}"
        _log(log, f"[missing] {msg}")
        if dry_run:
            # A rehearsal of a missing script cannot succeed — report it as a
            # failure so the caller (and the job) reflects the real state.
            _log(log, f"[dry-run] would run missing script: {script}")
            return {
                "ok": False,
                "dry_run": True,
                "script": str(script),
                "message": f"Would run (script missing): {script}",
                "returncode": 1,
            }
        return {"ok": False, "error": msg, "returncode": 1, "script": str(script)}

    result = run_command(
        ["bash", str(script)],
        cwd=genestack_root,
        timeout=timeout,
        dry_run=dry_run,
        extra_env=extra_env,
        ssh_target=ssh_target,
        remote_env=remote_env,
        agent_env_id=agent_env_id,
        log=log,
    )
    result["ok"] = result.get("returncode", 1) == 0
    result["script"] = str(script)
    result["service"] = service
    return result


def find_playbook(
    ansible_root: Path, playbook_name: str, genestack_root: Path | None = None
) -> Path | None:
    """Locate playbook under console ansible_root or genestack ansible/playbooks."""
    candidates = [
        ansible_root / playbook_name,
        ansible_root / "playbooks" / playbook_name,
    ]
    if genestack_root:
        candidates.extend(
            [
                genestack_root / "ansible" / "playbooks" / playbook_name,
                genestack_root / "ansible" / playbook_name,
            ]
        )
    for c in candidates:
        if c.is_file():
            return c
    return None


def run_playbook(
    playbook_name: str,
    *,
    ansible_root: Path,
    genestack_root: Path | None = None,
    limit: str | None = None,
    extra_vars: dict[str, Any] | None = None,
    tags: str | None = None,
    inventory_path: str | None = None,
    check: bool = False,
    dry_run: bool = True,
    timeout: int = 600,
    extra_env: dict[str, str] | None = None,
    ssh_target: str | None = None,
    remote_env: dict[str, str] | None = None,
    log: LogFn | None = None,
    allowlist: frozenset[str] | None = PLAYBOOK_ALLOWLIST,
) -> dict[str, Any]:
    # Normalize basename only
    name = Path(playbook_name).name
    if allowlist is not None and name not in allowlist:
        msg = f"Playbook '{name}' not in allowlist: {', '.join(sorted(allowlist))}"
        _log(log, f"[denied] {msg}")
        return {"ok": False, "error": msg, "returncode": 2}

    path = find_playbook(ansible_root, name, genestack_root)
    ansible_bin = shutil.which("ansible-playbook")

    if ssh_target is not None:
        # Remote execution: the playbook and ansible must exist on the deploy
        # host, not here — resolve for logging, but never gate on local paths.
        if path is None:
            path = ansible_root / name
            _log(log, f"[ssh] playbook not found locally; assuming remote path {path}")
        if ansible_bin is None:
            ansible_bin = "ansible-playbook"
    elif path is None or ansible_bin is None:
        reason_parts = []
        if path is None:
            reason_parts.append(f"playbook not found under {ansible_root}")
        if ansible_bin is None:
            reason_parts.append("ansible-playbook not on PATH")
        reason = "; ".join(reason_parts)
        if dry_run:
            _log(log, f"[dry-run fallback] {reason}")
            cmd_preview = ["ansible-playbook", name]
            if limit:
                cmd_preview.extend(["--limit", limit])
            if check:
                cmd_preview.append("--check")
            if tags:
                cmd_preview.extend(["--tags", tags])
            if extra_vars:
                cmd_preview.extend(["-e", json.dumps(extra_vars)])
            _log(log, f"$ {' '.join(cmd_preview)}")
            _log(log, f"[dry-run] {reason} — not executed")
            return {
                "ok": True,
                "dry_run": True,
                "playbook": name,
                "playbook_path": str(path) if path else None,
                "message": f"Would run playbook (unavailable: {reason})",
                "returncode": 0,
                "cmd": cmd_preview,
            }
        # Live mode: a missing playbook or binary must fail the job, never
        # report green without executing anything.
        msg = f"playbook unavailable and not a dry-run: {reason}"
        _log(log, f"[failed] {msg}")
        return {"ok": False, "error": msg, "returncode": 2}

    # Prefer running from console ansible root so roles_path/library resolve.
    # Core Genestack playbooks use GENESTACK ansible dir when that is where the
    # playbook lives.
    if path.is_relative_to(ansible_root) or str(path).startswith(str(ansible_root)):
        run_cwd = ansible_root
        playbook_arg = (
            str(path.relative_to(ansible_root))
            if path.is_relative_to(ansible_root)
            else str(path)
        )
        cfg = ansible_root / "ansible.cfg"
    elif genestack_root and str(path).startswith(str(genestack_root / "ansible")):
        run_cwd = genestack_root / "ansible" / "playbooks"
        playbook_arg = path.name
        cfg = genestack_root / "ansible" / "playbooks" / "ansible.cfg"
        if not cfg.is_file():
            cfg = ansible_root / "ansible.cfg"
            run_cwd = path.parent
            playbook_arg = str(path)
    else:
        run_cwd = path.parent
        playbook_arg = str(path)
        cfg = ansible_root / "ansible.cfg"

    cmd = [ansible_bin, playbook_arg]
    if inventory_path:
        cmd.extend(["-i", inventory_path])
    else:
        default_inv = ansible_root / "inventory" / "localhost.ini"
        if default_inv.is_file() and run_cwd == ansible_root:
            cmd.extend(["-i", str(default_inv)])
    if limit:
        cmd.extend(["--limit", limit])
    if check:
        cmd.append("--check")
    if tags:
        cmd.extend(["--tags", tags])
    if extra_vars:
        cmd.extend(["-e", json.dumps(extra_vars)])

    env_vars = None
    if ssh_target is None and (cfg.is_file() or extra_env):
        # Local execution only — when remote, run_command ignores env= and the
        # deploy host gets its assignments from remote_env instead.
        env_vars = {**os.environ, **(extra_env or {})}
        if cfg.is_file():
            env_vars["ANSIBLE_CONFIG"] = str(cfg)

    # If global dry_run, still only log; if not, execute
    result = run_command(
        cmd,
        cwd=run_cwd,
        timeout=timeout,
        dry_run=dry_run,
        env=env_vars,
        ssh_target=ssh_target,
        remote_env=remote_env,
        log=log,
    )
    result["ok"] = result.get("returncode", 1) == 0
    result["playbook"] = name
    result["playbook_path"] = str(path)
    return result
