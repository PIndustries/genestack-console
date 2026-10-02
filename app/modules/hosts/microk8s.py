"""What: Install MicroK8s on one Ubuntu host over SSH.
Where: app/modules/hosts/microk8s.py. HostsModule lists this file.
Why: One file so this step does not grow the job runner.
"""

from __future__ import annotations

import re
from typing import Any

from app.modules.params import p as _p

HANDLERS = ("hosts_microk8s_install",)

OPERATION = {
    "id": "hosts.microk8s.install",
    "name": "Install MicroK8s",
    "description": (
        "SSH to an Ubuntu host and run sudo snap install microk8s --classic, "
        "then sudo microk8s status --wait-ready. A dry run returns those "
        "commands and does not connect. This does not install OpenStack."
    ),
    "required_role": "admin",
    "backend": "internal",
    "params": [
        _p("host", True, "Ubuntu host (IP or name) the console can ssh to"),
        _p("ssh_user", False, "SSH user (default ubuntu)"),
    ],
    "handler": "hosts_microk8s_install",
    "mutating": True,
    "timeout_seconds": 900,
}

_REMOTE = (
    "sudo snap install microk8s --classic",
    "sudo microk8s status --wait-ready",
)
_HOST_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,252}")
_USER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9._-]{0,31}")


def _clean_host(value: str) -> str:
    text = str(value or "").strip()
    if not _HOST_RE.fullmatch(text):
        raise ValueError("host must be a hostname or IP address")
    return text


def _clean_user(value: str) -> str:
    text = str(value or "").strip() or "ubuntu"
    if not _USER_RE.fullmatch(text):
        raise ValueError("ssh_user must be a plain account name")
    return text


def command_lines(user: str, host: str) -> list[str]:
    return [f"ssh {user}@{host} {cmd}" for cmd in _REMOTE]


def _run_ssh(user: str, host: str, remote_cmd: str, timeout: int) -> dict[str, Any]:
    """One ssh command. Dry-run never calls this."""
    import subprocess

    argv = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "StrictHostKeyChecking=accept-new",
        f"{user}@{host}",
        remote_cmd,
    ]
    proc = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "argv": argv,
    }


def run(
    self,
    handler,
    op,
    job,
    env,
    log,
    ctx,
    params,
    deadline,
    check_cancel,
    dry,
    timeout,
    gs_root,
    ans_root,
    extra_env,
    ssh_target,
    remote_env,
    executor,
    agent_env_id,
):
    try:
        host = _clean_host(str(params.get("host") or ""))
        user = _clean_user(str(params.get("ssh_user") or ""))
    except ValueError as exc:
        log(f"[microk8s] {exc}")
        return {"ok": False, "dry_run": bool(dry), "ssh_used": False, "error": str(exc)}
    commands = command_lines(user, host)
    if dry:
        for line in commands:
            log(f"[dry-run] would run: {line}")
        return {
            "ok": True,
            "dry_run": True,
            "ssh_used": False,
            "host": host,
            "ssh_user": user,
            "commands": commands,
            "message": "Dry run: SSH was not used.",
        }
    limit = int(timeout or 900)
    for remote, line in zip(_REMOTE, commands, strict=True):
        if callable(check_cancel):
            check_cancel()
        log(f"[microk8s] {line}")
        result = _run_ssh(user, host, remote, limit)
        if not result.get("ok"):
            rc = result.get("returncode")
            log(f"[microk8s] rc={rc}")
            return {
                "ok": False,
                "dry_run": False,
                "ssh_used": True,
                "host": host,
                "ssh_user": user,
                "commands": commands,
                "returncode": rc,
                "error": f"command failed: {remote}",
            }
    return {
        "ok": True,
        "dry_run": False,
        "ssh_used": True,
        "host": host,
        "ssh_user": user,
        "commands": commands,
        "message": "MicroK8s install finished.",
    }
