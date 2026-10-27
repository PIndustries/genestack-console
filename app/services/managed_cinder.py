"""Prepare an explicitly sized Longhorn export before Cinder's Helm install.

The export and its data are retained on retries; this module never deletes a PVC.
"""

from __future__ import annotations

import base64
import json
from importlib.resources import files
from typing import Any


def settings(doc: dict[str, Any]) -> dict[str, Any] | None:
    storage = doc.get("storage") or {}
    if not isinstance(storage, dict):
        raise ValueError("storage must be a mapping")  # noqa: TRY004 -- config validation contract
    value = storage.get("longhorn_cinder")
    if value is None:
        return None
    if not isinstance(value, dict) or not isinstance(value.get("enabled", False), bool):
        raise ValueError("storage.longhorn_cinder requires a boolean enabled field")  # noqa: TRY004 -- config validation contract
    if set(value) - {"enabled", "capacity_gib"}:
        raise ValueError("Unknown storage.longhorn_cinder setting")
    if not value.get("enabled", False):
        return None
    size = value.get("capacity_gib")
    if isinstance(size, bool) or not isinstance(size, int) or not 8 <= size <= 1048576:
        raise ValueError("Review storage.longhorn_cinder.capacity_gib (8..1048576 GiB)")
    ceph = storage.get("ceph") or {}
    if not isinstance(ceph, dict):
        raise ValueError("storage.ceph must be a mapping")  # noqa: TRY004 -- config validation contract
    if ceph.get("enabled"):
        raise ValueError("Managed Longhorn Cinder cannot replace an enabled Ceph backend")
    if (doc.get("helm_overrides") or {}).get("cinder"):
        raise ValueError("Review custom Cinder overrides before enabling managed storage")
    return {"capacity_gib": size}


def prepare_command(doc: dict[str, Any], environment_id: str, config_dir: str) -> list[str] | None:
    value = settings(doc)
    if value is None:
        return None
    payload = dict(
        value, environment_id=environment_id, config_dir=config_dir, namespace="openstack"
    )
    source = files("app").joinpath("assets/managed-cinder.py.in").read_text()
    encoded = base64.b64encode(source.encode()).decode()
    return [
        "python3",
        "-c",
        "import base64; exec(compile(base64.b64decode("
        + repr(encoded)
        + "), '<managed-cinder>', 'exec'))",
        json.dumps(payload),
    ]
