"""Fetch a matching Image Factory kernel/initramfs instead of unpacking a VM disk.

The public API documents kernel-amd64 and initramfs-amd64.xz per schematic/version:
https://github.com/siderolabs/image-factory/blob/main/docs/api.md
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from app.services.request_safety import under_root


def model(image_url: str) -> dict[str, str] | None:
    url = urlsplit(image_url)
    if url.scheme != "https" or url.netloc != "factory.talos.dev" or url.query or url.fragment:
        return None
    match = re.fullmatch(
        r"/image/([0-9a-f]{64})/(v[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?)/metal-amd64\.qcow2",
        url.path,
    )
    if not match:
        return None
    base = "https://factory.talos.dev/image/" + match[1] + "/" + match[2]
    key = hashlib.sha256((base + "/amd64").encode()).hexdigest()
    return {"base": base, "path": "assets/talos/" + key}


def _digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _cached(destination: Path, expected: dict[str, str]) -> bool:
    try:
        raw = (destination / "manifest.json").read_bytes()
        if len(raw) > 16384:
            return False
        manifest = json.loads(raw)
        if manifest["model"] != expected or set(manifest["files"]) != {"vmlinuz", "initramfs.xz"}:
            return False
        for name, info in manifest["files"].items():
            path = under_root(destination, name)
            if path.stat().st_size != info["bytes"] or _digest(path) != info["sha256"]:
                return False
        return True
    except (OSError, KeyError, TypeError, ValueError):
        return False


def fetch(
    expected: dict[str, str],
    root: Path,
    download: Callable[..., Any],
    log: Callable[[str], None] | None,
) -> dict[str, Any]:
    destination = under_root(root, expected["path"])
    destination.parent.mkdir(parents=True, exist_ok=True)
    with (destination.parent / ".prepare.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        changed = not _cached(destination, expected)
        if changed:
            stage = Path(tempfile.mkdtemp(prefix=".talos-stage-", dir=destination.parent))
            previous: Path | None = None
            try:
                entries = {}
                for filename, asset in [
                    ("vmlinuz", "kernel-amd64"),
                    ("initramfs.xz", "initramfs-amd64.xz"),
                ]:
                    source, _reported_hash = download(
                        expected["base"] + "/" + asset, log, dest_dir=stage / "downloads"
                    )
                    source = Path(source)
                    size = source.stat().st_size
                    limit = 128 * 1024**2 if filename == "vmlinuz" else 2 * 1024**3
                    if not 0 < size <= limit:
                        raise ValueError("Factory PXE asset size is invalid")
                    with source.open("rb") as stream:
                        header = stream.read(520)
                    valid = (
                        (header.startswith(b"MZ") or header[514:518] == b"HdrS")
                        if filename == "vmlinuz"
                        else header.startswith((b"\xfd7zXZ\x00", b"\x28\xb5\x2f\xfd"))
                    )
                    if not valid:
                        raise ValueError("Factory did not return the expected Linux boot asset")
                    target = stage / filename
                    shutil.move(str(source), target)
                    entries[filename] = {"bytes": size, "sha256": _digest(target)}
                (stage / "manifest.json").write_text(
                    json.dumps({"model": expected, "files": entries})
                )
                if destination.exists():
                    previous = Path(
                        tempfile.mkdtemp(prefix=".talos-previous-", dir=destination.parent)
                    )
                    previous.rmdir()
                    os.replace(destination, previous)
                try:
                    os.replace(stage, destination)
                except BaseException:
                    if previous is not None:
                        os.replace(previous, destination)
                        previous = None
                    raise
                if log:
                    log("[pxe-assets] prepared matching Image Factory kernel and initramfs")
            finally:
                shutil.rmtree(stage, ignore_errors=True)
                if previous is not None:
                    shutil.rmtree(previous, ignore_errors=True)
        return {
            "ok": True,
            "changed": changed,
            "kernel": str(destination / "vmlinuz"),
            "initrd": str(destination / "initramfs.xz"),
            "assets_relative_path": expected["path"],
        }
