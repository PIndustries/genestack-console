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


def parse_calver(raw: str) -> tuple[int, int, int, int, str]:
    text = str(raw or "").strip()
    main, _, suf = text.partition("-")
    parts: list[int] = []
    for bit in main.split("."):
        try:
            parts.append(int(bit))
        except ValueError:
            parts.append(0)
    while len(parts) < 3:
        parts.append(0)
    return (parts[0], parts[1], parts[2], 0 if suf else 1, suf)


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
        tmp.chmod(tmp.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        os.replace(tmp, dest)
    except Exception as exc:  # noqa: BLE001
        tmp.unlink(missing_ok=True)
        return {**info, "ok": False, "applied": False, "message": str(exc)[:400]}
    restarted = False
    systemctl = shutil.which("systemctl")
    if systemctl:
        proc = subprocess.run(  # noqa: S603
            [
                systemctl,
                "restart",
                "genestack-console.service",
                "genestack-console-worker.service",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        restarted = proc.returncode == 0
    return {
        **info,
        "ok": True,
        "applied": True,
        "restarted": restarted,
        "path": str(dest),
        "message": "binary replaced" + (" and services restarted" if restarted else ""),
    }
