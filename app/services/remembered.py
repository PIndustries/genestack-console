"""Last good Overview and cloud answers, kept on disk.

The page returns this copy immediately and refreshes behind it. A failed
gather does not replace a good copy.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

from app.config import get_settings

_KINDS = frozenset({"overview", "cloud"})
_ENV_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_FILE_MODE = 0o640
_DIR_MODE = 0o750
_SKIP = frozenset({"remembered", "age_s", "cached"})
_WORTHLESS = frozenset({"unavailable", "missing"})


def _path(kind: str, env_id: str) -> Path | None:
    if kind not in _KINDS:
        return None
    if not isinstance(env_id, str) or _ENV_ID.fullmatch(env_id) is None:
        return None
    root = (Path(get_settings().data_dir) / "remembered" / kind).resolve()
    target = (root / f"{env_id}.json").resolve()
    try:
        target.relative_to(root)
    except ValueError:
        return None
    if target.name != f"{env_id}.json":
        return None
    return target


def worth_saving(kind: str, body: dict[str, Any]) -> bool:
    """True when this body is a real answer worth keeping."""
    if kind == "cloud":
        return bool(body.get("available"))
    if kind != "overview":
        return False
    for key, section in body.items():
        if key in _SKIP or key == "environment_id":
            continue
        if not isinstance(section, dict):
            continue
        if section.get("ok") is False and section.get("error") in _WORTHLESS:
            continue
        return True
    return False


def load(kind: str, env_id: str) -> dict[str, Any] | None:
    """Return ``{"age_s", "body"}`` or None when the file is missing or bad."""
    path = _path(kind, env_id)
    if path is None or not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    body = raw.get("body")
    saved_at = raw.get("saved_at")
    if not isinstance(body, dict) or not isinstance(saved_at, (int, float)):
        return None
    if isinstance(saved_at, bool):
        return None
    age = max(0, int(time.time() - float(saved_at)))
    return {"age_s": age, "body": body}


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, _DIR_MODE)
    except OSError:
        return


def save(kind: str, env_id: str, body: dict[str, Any]) -> bool:
    """Write one answer. A worthless body leaves the previous file in place."""
    path = _path(kind, env_id)
    if path is None or not isinstance(body, dict):
        return False
    cleaned = {key: value for key, value in body.items() if key not in _SKIP}
    if not worth_saving(kind, cleaned):
        return False
    _ensure_dir(path.parent)
    payload = json.dumps(
        {"saved_at": time.time(), "body": cleaned},
        default=str,
        separators=(",", ":"),
    ).encode("utf-8")
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _FILE_MODE)
    try:
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        os.chmod(tmp, _FILE_MODE)
    except OSError:
        pass
    os.replace(tmp, path)
    try:
        os.chmod(path, _FILE_MODE)
    except OSError:
        pass
    return True
