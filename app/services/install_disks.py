"""Strict per-machine persistent disk selectors; this module performs no I/O."""
from __future__ import annotations

import re
from typing import Any

_WWID = re.compile(r"^(?:(?:naa|eui)\.|0x)?[0-9a-fA-F]{16,64}$")


def install_disk_selectors(talos: dict[str, Any], servers: dict[str, Any]) -> dict[str, str] | None:
    if "install_disk_selectors" not in talos:
        return None
    selectors = talos["install_disk_selectors"]
    if not isinstance(selectors, dict) or not selectors or set(selectors) != set(servers):
        raise ValueError("talos.install_disk_selectors must name exactly every configured machine")
    if talos.get("install_disk"):
        raise ValueError("talos.install_disk cannot be combined with install_disk_selectors")
    identities = set()
    for identity in selectors.values():
        if not isinstance(identity, str) or not _WWID.fullmatch(identity):
            raise ValueError("talos.install_disk_selectors requires exact WWIDs without wildcards")
        canonical = re.sub(r"^(?:naa\.|eui\.|0x)", "", identity.lower())
        if canonical in identities:
            raise ValueError("talos.install_disk_selectors contains duplicate disk identities")
        identities.add(canonical)
    return dict(selectors)
