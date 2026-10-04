"""Check the GitHub release for a newer compiled Console (no source clone)."""

from __future__ import annotations

import logging
import os
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import httpx

from app import __version__
from app.config import Settings
from app.paths import host_prefix

log = logging.getLogger(__name__)

DEFAULT_CHANNEL = "https://github.com/PIndustries/genestack-console/releases/latest/download/version.json"


def parse_calver(raw: str) -> tuple[int, int, int, int, int, str]:
    """Return ``(year, month, day, build, release, suffix)``.

    A three-part version has build 0, so ``2026.10.03`` is older than
    ``2026.10.04.1``. A hyphen suffix is a prerelease (release 0) and sorts
    below the same numbers. A leading ``v`` is ignored.
    """
    text = str(raw or "").strip()
    if text[:1] in ("v", "V"):
        text = text[1:]
    main, _, suf = text.partition("-")
    parts: list[int] = []
    for bit in main.split("."):
        if not bit:
            parts.append(0)
            continue
        try:
            parts.append(int(bit))
        except ValueError:
            parts.append(0)
    while len(parts) < 4:
        parts.append(0)
    release = 0 if suf else 1
    return (parts[0], parts[1], parts[2], parts[3], release, suf)


def is_newer(latest: str, current: str) -> bool:
    if not latest or not current:
        return False
    return parse_calver(latest) > parse_calver(current)


def fetch_channel(url: str, timeout: float = 5.0) -> dict[str, Any] | None:
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            res = client.get(url)
            res.raise_for_status()
            data = res.json()
    except Exception as exc:  # noqa: BLE001 — channel is optional
        log.debug("update channel unreachable: %s", exc)
        return None
    if not isinstance(data, dict):
        return None
    return data


def status(settings: Settings) -> dict[str, Any]:
    url = (settings.update_url or DEFAULT_CHANNEL).strip() or DEFAULT_CHANNEL
    channel = fetch_channel(url)
    current = __version__
    latest = str((channel or {}).get("version") or "")
    binary = str((channel or {}).get("binary") or "")
    if binary.startswith("/"):
        binary = "https://genestack.dev" + binary
    available = bool(channel) and is_newer(latest, current)
    return {
        "current": current,
        "latest": latest or None,
        "update_available": available,
        "auto": bool(settings.update_auto),
        "channel": url,
        "binary": binary or None,
        "reachable": channel is not None,
    }


def apply_binary(settings: Settings) -> dict[str, Any]:
    info = status(settings)
    if not info.get("update_available"):
        return {**info, "ok": True, "applied": False, "message": "already current"}
    url = info.get("binary")
    if not url:
        return {
            **info,
            "ok": False,
            "applied": False,
            "message": "channel has no binary URL",
        }
    prefix = host_prefix()
    dest = prefix / "bin" / "genestack-console"
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_name = tempfile.mkstemp(prefix="gsc-update-", dir=str(dest.parent))
    os.close(tmp_fd)
    tmp = Path(tmp_name)
    try:
        with httpx.Client(timeout=120.0, follow_redirects=True) as client:
            with client.stream("GET", url) as res:
                res.raise_for_status()
                with tmp.open("wb") as fh:
                    for chunk in res.iter_bytes():
                        fh.write(chunk)
        with tmp.open("rb") as fh:
            magic = fh.read(4)
        if magic != b"\x7fELF":
            tmp.unlink(missing_ok=True)
            return {
                **info,
                "ok": False,
                "applied": False,
                "message": "download is not a Linux ELF",
            }
        tmp.chmod(tmp.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        os.replace(tmp, dest)
    except Exception as exc:  # noqa: BLE001
        tmp.unlink(missing_ok=True)
        return {**info, "ok": False, "applied": False, "message": str(exc)[:400]}
    restarted, message, ok = _restart_services()
    return {
        **info,
        "ok": ok,
        "applied": True,
        "restarted": restarted,
        "path": str(dest),
        "message": message,
    }


def _restart_services() -> tuple[bool, str, bool]:
    """Restart both units. Returns ``(restarted, message, ok)``."""
    systemctl = shutil.which("systemctl")
    if not systemctl:
        return False, "binary replaced; systemctl not found", True
    unit = Path("/etc/systemd/system/genestack-console.service")
    if not unit.is_file():
        return False, "binary replaced; genestack-console.service is not installed", True
    cmd = [
        systemctl,
        "restart",
        "genestack-console.service",
        "genestack-console-worker.service",
    ]
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        sudo = shutil.which("sudo")
        if not sudo:
            return False, "binary replaced; restart needs root", False
        # A timer has no tty, so it must not sit on a password prompt.
        cmd = [sudo, *cmd] if os.isatty(0) else [sudo, "-n", *cmd]
    proc = subprocess.run(  # noqa: S603
        cmd,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if proc.returncode == 0:
        return True, "binary replaced and services restarted", True
    detail = (proc.stderr or proc.stdout or "").strip().replace("\n", " ")
    if detail:
        return False, f"binary replaced; restart failed: {detail[:200]}", False
    return False, "binary replaced; restart failed", False
