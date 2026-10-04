"""What: Prepare Ubuntu autoinstall user-data for one hostname.
Where: app/modules/hosts/ubuntu.py. HostsModule lists this file.
Why: One file so this step does not grow the job runner.
"""

from __future__ import annotations

import re
import socket
import time
from pathlib import Path

from app.modules.params import p as _p

HANDLERS = ("hosts_ubuntu_prepare", "hosts_ubuntu_bringup")

OPERATION = {
    "id": "hosts.ubuntu.prepare",
    "name": "Prepare Ubuntu Autoinstall",
    "description": (
        "Write cloud-init autoinstall user-data and meta-data for one "
        "hostname under the PXE directory this console already serves. "
        "The seed creates an identity user and installs the SSH public "
        "key you pass. It does not install OpenStack or Kubernetes, and "
        "it does not open SSH from this job."
    ),
    "required_role": "admin",
    "backend": "internal",
    "params": [
        _p("hostname", True, "Hostname for the Ubuntu identity (DNS label)"),
        _p("ssh_key", True, "SSH public key installed for the identity user"),
    ],
    "handler": "hosts_ubuntu_prepare",
    "mutating": True,
    "timeout_seconds": 120,
}

BRINGUP = {
    "id": "hosts.ubuntu.bringup",
    "name": "Bring up Ubuntu",
    "description": (
        "Make the named hosts Ubuntu and wait until they answer. A host that "
        "already answers on SSH stays on disk and leaves the Talos plan. A "
        "host that does not answer reboots into the Ubuntu installer. A host "
        "still speaking Talos is left alone unless it is named in "
        "leave_hostnames. This does not install OpenStack or Kubernetes."
    ),
    "required_role": "operator",
    "backend": "internal",
    "params": [
        _p("hostnames", False, "Hosts to bring up. A list or comma-separated names."),
        _p(
            "leave_hostnames",
            False,
            "Talos members that may be reinstalled and removed from the cluster.",
        ),
    ],
    "handler": "hosts_ubuntu_bringup",
    "mutating": True,
    "timeout_seconds": 3600,
}

OPERATIONS = (OPERATION, BRINGUP)

_HOST_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


def safe_hostname(name: str) -> str:
    text = str(name or "").strip().lower()
    if not _HOST_RE.fullmatch(text):
        raise ValueError("hostname must be a single DNS label")
    return text


def clean_ssh_key(value: str) -> str:
    text = str(value or "").strip()
    if not text or any(ch in text for ch in "\n\r"):
        raise ValueError("ssh_key must be one public key line")
    if len(text) > 4096:
        raise ValueError("ssh_key is too long")
    return text


def _yaml_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_user_data(hostname: str, ssh_key: str) -> str:
    """Cloud-init autoinstall. Identity user, SSH key, no OpenStack or Kubernetes."""
    key = _yaml_quote(ssh_key)
    return (
        "#cloud-config\n"
        "# Ubuntu only. This autoinstall does not install OpenStack or Kubernetes.\n"
        "autoinstall:\n"
        "  version: 1\n"
        "  identity:\n"
        f"    hostname: {hostname}\n"
        "    username: ubuntu\n"
        '    password: "!"\n'
        "  ssh:\n"
        "    install-server: true\n"
        "    allow-pw: false\n"
        "    authorized-keys:\n"
        f"      - {key}\n"
        "  storage:\n"
        "    layout:\n"
        "      name: direct\n"
    )


def render_meta_data(hostname: str) -> str:
    return f"instance-id: {hostname}\nlocal-hostname: {hostname}\n"


def pxe_data_dir(runner) -> Path:
    """PXE tree root's parent: ``<data_dir>``, not a second TFTP root."""
    settings = getattr(runner, "settings", None) if runner is not None else None
    if settings is None:
        from app.config import get_settings

        settings = get_settings()
    return Path(settings.data_dir)


def write_ubuntu_seed(data_dir: Path, hostname: str, ssh_key: str) -> dict[str, Path]:
    """Write the named seed and the file the Ubuntu iPXE profile fetches."""
    root = Path(data_dir) / "pxe" / "ubuntu"
    named = root / hostname
    named.mkdir(parents=True, exist_ok=True)
    user = render_user_data(hostname, ssh_key)
    meta = render_meta_data(hostname)
    user_path = named / "user-data"
    meta_path = named / "meta-data"
    user_path.write_text(user, encoding="utf-8")
    meta_path.write_text(meta, encoding="utf-8")
    # A named boot reads /ubuntu/<hostname>/. The shared files are the fallback.
    (root / "user-data").write_text(user, encoding="utf-8")
    (root / "meta-data").write_text(meta, encoding="utf-8")
    return {
        "seed_dir": named,
        "user_data": user_path,
        "meta_data": meta_path,
        "boot_user_data": root / "user-data",
    }


def bringup_action(*, port_open: bool, talos_api: bool, leave: bool) -> str:
    """Pick the one automatic step for a host.

    ``already`` stays on disk. ``install`` reboots into Ubuntu. ``leave``
    does not touch a machine that is still Talos.
    """
    if port_open and not talos_api:
        return "already"
    if talos_api and not leave:
        return "leave"
    return "install"


def _name_list(value) -> list[str]:
    if value is None:
        return []
    raw = value if isinstance(value, list) else str(value).split(",")
    names: list[str] = []
    seen: set[str] = set()
    for item in raw:
        name = str(item or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        names.append(name)
    return names


def _port_open(ip: str, port: int, timeout: float = 3) -> bool:
    if not ip:
        return False
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def _wait_port(ip, port, deadline, check_cancel, log, hostname) -> bool:
    if not ip:
        return False
    limit = deadline if isinstance(deadline, (int, float)) else time.time() + 1500
    while time.time() < limit:
        if check_cancel:
            check_cancel()
        if _port_open(ip, port):
            log(f"[ubuntu] {hostname} answers on {ip}")
            return True
        log(f"[ubuntu] waiting for {hostname} to answer on {ip}")
        time.sleep(10)
    return False


def _bring_up(runner, env, log, params, dry, deadline, check_cancel, actor):
    from sqlalchemy import select

    from app.models import BaremetalNode
    from app.services import envconfig as envconfig_service
    from app.services.baremetal import set_next_boot, talos_api_ready

    ordered = _name_list(params.get("hostnames"))
    for name in _name_list(params.get("leave_hostnames")):
        if name not in ordered:
            ordered.append(name)
    leave_names = set(_name_list(params.get("leave_hostnames")))
    if not ordered:
        return {"ok": False, "rebooted": False, "error": "hostnames is required", "hosts": []}
    current = envconfig_service.get_current(runner.db, env)
    servers = (current[0].get("servers") or {}) if current else {}
    by_lower = {str(key).lower(): key for key in servers}
    results = []
    cleared: list[str] = []
    for hostname in ordered:
        key = hostname if isinstance(servers.get(hostname), dict) else by_lower.get(hostname.lower())
        entry = servers.get(key) if key and isinstance(servers.get(key), dict) else None
        if entry is None:
            results.append(
                {"hostname": hostname, "action": "missing", "rebooted": False, "ok": False}
            )
            continue
        hostname = key
        node = runner.db.scalar(
            select(BaremetalNode).where(
                BaremetalNode.environment_id == env.id,
                BaremetalNode.name == hostname,
            )
        )
        ip = ""
        if node is not None and getattr(node, "expected_ip", None):
            ip = str(node.expected_ip).strip()
        if not ip:
            ip = str(entry.get("private_ip") or entry.get("ip") or "").strip()
        answering = _port_open(ip, 22) if ip else False
        talos = bool(ip and talos_api_ready(ip, log))
        action = bringup_action(
            port_open=answering,
            talos_api=talos,
            leave=hostname in leave_names,
        )
        log(f"[ubuntu] {hostname} answer={answering} talos={talos} action={action}")
        if action == "leave":
            results.append(
                {"hostname": hostname, "action": "leave", "rebooted": False, "ok": True}
            )
            continue
        if action == "already":
            cleared.append(hostname)
            results.append(
                {
                    "hostname": hostname,
                    "action": "already",
                    "rebooted": False,
                    "ok": True,
                    "ip": ip,
                }
            )
            continue
        if dry:
            log(f"[ubuntu] dry-run would install {hostname} and wait until it answers")
            results.append(
                {
                    "hostname": hostname,
                    "action": "install",
                    "rebooted": False,
                    "ok": True,
                    "dry_run": True,
                }
            )
            continue
        if node is None:
            results.append(
                {
                    "hostname": hostname,
                    "action": "install",
                    "rebooted": False,
                    "ok": False,
                    "error": "no bare-metal record, so it cannot be installed from here",
                }
            )
            continue
        booted = set_next_boot(
            runner.db,
            env,
            node,
            "ubuntu",
            boot_now=True,
            dry_run=False,
            log=log,
            settings=getattr(runner, "settings", None),
        )
        if not booted.get("ok"):
            results.append(
                {
                    "hostname": hostname,
                    "action": "install",
                    "rebooted": False,
                    "ok": False,
                    "error": booted.get("error") or "boot failed",
                }
            )
            continue
        if not _wait_port(ip, 22, deadline, check_cancel, log, hostname):
            results.append(
                {
                    "hostname": hostname,
                    "action": "install",
                    "rebooted": True,
                    "ok": False,
                    "error": "rebooted into Ubuntu and did not answer",
                }
            )
            continue
        set_next_boot(
            runner.db,
            env,
            node,
            "disk",
            boot_now=False,
            dry_run=False,
            log=log,
            settings=getattr(runner, "settings", None),
        )
        cleared.append(hostname)
        results.append(
            {"hostname": hostname, "action": "install", "rebooted": True, "ok": True, "ip": ip}
        )
    if cleared:
        try:
            envconfig_service.clear_server_roles(
                runner.db, env, actor, hostnames=cleared
            )
        except envconfig_service.ConfigValidationError as exc:
            log(f"[ubuntu] roles stayed: {exc}")
    ok = bool(results) and all(row.get("ok") for row in results)
    rebooted = any(row.get("rebooted") for row in results)
    parts = []
    for row in results:
        if row.get("action") == "already":
            parts.append(f"{row['hostname']} already up, stayed on disk")
        elif row.get("action") == "leave":
            parts.append(f"{row['hostname']} still Talos, left alone")
        elif row.get("rebooted"):
            parts.append(f"{row['hostname']} installed and answering" if row.get("ok") else f"{row['hostname']} rebooted and did not answer")
        else:
            parts.append(f"{row['hostname']} {row.get('error') or row.get('action')}")
    return {"ok": ok, "rebooted": rebooted, "hosts": results, "message": "; ".join(parts)}


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
    if handler == "hosts_ubuntu_bringup":
        return _bring_up(
            self,
            env,
            log,
            params or {},
            dry,
            deadline,
            check_cancel,
            getattr(job, "created_by", None) or "console",
        )
    try:
        hostname = safe_hostname(str(params.get("hostname") or ""))
        ssh_key = clean_ssh_key(str(params.get("ssh_key") or ""))
    except ValueError as exc:
        log(f"[ubuntu] {exc}")
        return {"ok": False, "dry_run": bool(dry), "ssh_used": False, "error": str(exc)}
    written = write_ubuntu_seed(pxe_data_dir(self), hostname, ssh_key)
    log(
        f"[ubuntu] wrote autoinstall user-data for {hostname} "
        f"at {written['user_data']} (ssh was not used)"
    )
    return {
        "ok": True,
        "dry_run": bool(dry),
        "ssh_used": False,
        "hostname": hostname,
        "seed_dir": str(written["seed_dir"]),
        "user_data": str(written["user_data"]),
        "meta_data": str(written["meta_data"]),
        "message": (
            "Wrote Ubuntu autoinstall user-data. SSH was not used. "
            "This does not install OpenStack or Kubernetes."
        ),
    }
