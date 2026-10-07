"""bootc status, upgrade, and rollback for an appliance host."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from typing import Any

_ACTIONS = frozenset({"upgrade", "rollback"})
_VERSION = re.compile(r"^\d{4}\.\d{2}\.\d{2}(?:\.\d+)?$")


def bootc_path() -> str | None:
    """Return the bootc binary, or None when this host is not an appliance."""
    for candidate in ("/usr/bin/bootc",):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    found = shutil.which("bootc")
    return found or None


def installed() -> bool:
    return bootc_path() is not None


def version_from_image(name: str) -> str:
    """Pull a CalVer tag off an image reference. Other tags stay blank."""
    text = str(name or "")
    if "@" in text:
        text = text.split("@", 1)[0]
    if ":" not in text:
        return ""
    tag = text.rsplit(":", 1)[-1]
    return tag if _VERSION.fullmatch(tag) else ""


def deployment(entry: Any, slot: str) -> dict[str, Any] | None:
    """One booted, staged, or rollback row from `bootc status --json`."""
    if not isinstance(entry, dict):
        return None
    image = entry.get("image") if isinstance(entry.get("image"), dict) else {}
    ref = image.get("image") if isinstance(image.get("image"), dict) else {}
    name = str(ref.get("image") or "")
    version = str(image.get("version") or "") or version_from_image(name)
    if not name and not version:
        return None
    digest = str(image.get("imageDigest") or "") or None
    return {
        "slot": slot,
        "version": version or None,
        "image": name or None,
        "digest": digest,
    }


def parse_status(data: Any) -> dict[str, Any]:
    """Turn a BootcHost document into the fields the settings page reads."""
    status = data.get("status") if isinstance(data, dict) else None
    if not isinstance(status, dict):
        status = {}
    return {
        "bootc": True,
        "ok": True,
        "booted": deployment(status.get("booted"), "booted"),
        "rollback": deployment(status.get("rollback"), "rollback"),
        "staged": deployment(status.get("staged"), "staged"),
        "rollback_queued": bool(status.get("rollbackQueued")),
        "read_only": bool(status.get("readOnly")),
        "message": "",
    }


def absent() -> dict[str, Any]:
    return {
        "bootc": False,
        "ok": True,
        "booted": None,
        "rollback": None,
        "staged": None,
        "rollback_queued": False,
        "read_only": False,
        "message": "",
    }


def _maybe_sudo(cmd: list[str]) -> list[str] | None:
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        sudo = shutil.which("sudo")
        if not sudo:
            return None
        return [sudo, "-n", *cmd]
    return list(cmd)


def _run(cmd: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def host_view(run: Any = None) -> dict[str, Any]:
    """Read the booted image and the previous one. Missing bootc is not an error."""
    binary = bootc_path()
    if not binary:
        return absent()
    runner = run or _run
    try:
        proc = runner([binary, "status", "--json"], 30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        view = absent()
        view.update(bootc=True, ok=False, message=str(exc)[:200])
        return view
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().replace("\n", " ")
        view = absent()
        view.update(bootc=True, ok=False, message=detail[:400] or "bootc status failed")
        return view
    try:
        data = json.loads(proc.stdout or "")
    except json.JSONDecodeError:
        view = absent()
        view.update(bootc=True, ok=False, message="bootc status was not JSON")
        return view
    return parse_status(data)


def apply_action(action: str, run: Any = None) -> dict[str, Any]:
    """Queue `bootc upgrade --apply` or `bootc rollback --apply`.

    `--apply` reboots into that image. The HTTP client may see the socket drop.
    """
    if action not in _ACTIONS:
        return {"ok": False, "applied": False, "action": action, "message": "unknown action"}
    binary = bootc_path()
    if not binary:
        return {
            "ok": False,
            "applied": False,
            "action": action,
            "message": "bootc is not installed",
        }
    cmd = _maybe_sudo([binary, action, "--apply"])
    if cmd is None:
        return {"ok": False, "applied": False, "action": action, "message": "bootc needs root"}
    runner = run or _run
    try:
        proc = runner(cmd, 900)
    except subprocess.TimeoutExpired:
        return {"ok": False, "applied": False, "action": action, "message": "bootc did not finish"}
    except OSError as exc:
        return {"ok": False, "applied": False, "action": action, "message": str(exc)[:200]}
    detail = (proc.stderr or proc.stdout or "").strip().replace("\n", " ")
    if proc.returncode != 0:
        return {
            "ok": False,
            "applied": False,
            "action": action,
            "message": detail[:400] or "bootc failed",
        }
    return {
        "ok": True,
        "applied": True,
        "action": action,
        "message": detail[:400] or "rebooting",
    }
