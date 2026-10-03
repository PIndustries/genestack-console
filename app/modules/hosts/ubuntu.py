"""What: Prepare Ubuntu autoinstall user-data for one hostname.
Where: app/modules/hosts/ubuntu.py. HostsModule lists this file.
Why: One file so this step does not grow the job runner.
"""

from __future__ import annotations

import re
from pathlib import Path

from app.modules.params import p as _p

HANDLERS = ("hosts_ubuntu_prepare",)

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
