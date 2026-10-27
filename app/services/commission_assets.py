"""A verified, self-contained Alpine RAM boot and local package repository."""

from __future__ import annotations

import fcntl
import hashlib
import json
import shutil
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

import pycdlib

from app.services.bootselect import (
    ALPINE_ISO_SHA256,
    ALPINE_ISO_URL,
    ALPINE_VERSION,
    COMMISSION_FILES,
)
from app.services.job_runner import download_factory_image
from app.services.request_safety import under_root

MAX_BYTES = 1024**3
MAX_FILES = 4096
MANIFEST = ".manifest.json"


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def cached(dest: Path) -> bool:
    try:
        raw = (dest / MANIFEST).read_bytes()
        if len(raw) > 1024**2:
            return False
        document = json.loads(raw)
        entries = document["files"]
        if not isinstance(entries, dict):
            return False
        if document["iso_sha256"] != ALPINE_ISO_SHA256 or not 4 <= len(entries) <= MAX_FILES:
            return False
        if not set(COMMISSION_FILES).issubset(entries):
            return False
        for name, info in entries.items():
            path = under_root(dest, name)
            if path.stat().st_size != info["bytes"] or digest(path) != info["sha256"]:
                return False
        return True
    except (OSError, ValueError, KeyError, TypeError):
        return False


def extract(archive: Path, destination: Path) -> dict[str, Any]:
    """Extract only boot payloads and the ISO's signed offline APK repository."""
    iso = pycdlib.PyCdlib()
    iso.open(str(archive))
    entries: dict[str, Any] = {}
    total = 0
    try:
        path_key = (
            "rr_path" if iso.has_rock_ridge() else "joliet_path" if iso.has_joliet() else None
        )
        if path_key is None:
            raise ValueError("Alpine ISO has no long filenames for its package repository")
        for directory, _directories, names in iso.walk(**{path_key: "/"}):
            for name in names:
                full = (directory.rstrip("/") + "/" + name).lstrip("/")
                if full in ["boot/" + n for n in COMMISSION_FILES]:
                    relative = name
                elif full.startswith("apks/x86_64/"):
                    relative = full
                else:
                    continue
                parts = PurePosixPath(relative)
                if parts.is_absolute() or ".." in parts.parts or "\\" in relative:
                    raise ValueError("Invalid Alpine asset path")
                output = under_root(destination, relative)
                output.parent.mkdir(parents=True, exist_ok=True)
                with output.open("wb") as stream:
                    iso.get_file_from_iso_fp(stream, **{path_key: "/" + full})
                total += output.stat().st_size
                if total > MAX_BYTES or len(entries) >= MAX_FILES:
                    raise ValueError("Alpine boot payload exceeds extraction limit")
                entries[relative] = {"bytes": output.stat().st_size, "sha256": digest(output)}
    finally:
        iso.close()
    if not set(COMMISSION_FILES).issubset(entries):
        raise ValueError("Alpine ISO lacks required boot files")
    if not any(n in entries for n in ["apks/x86_64/APKINDEX.tar.gz", "apks/x86_64/Packages.adb"]):
        raise ValueError("Alpine ISO lacks its signed package index")
    if not any(n.startswith("apks/x86_64/alpine-base-") and n.endswith(".apk") for n in entries):
        raise ValueError("Alpine ISO lacks base packages")
    return {"version": ALPINE_VERSION, "iso_sha256": ALPINE_ISO_SHA256, "files": entries}


def fetch(root: Path, log=None) -> dict[str, Any]:
    parent = root / "assets" / "commission"
    parent.mkdir(parents=True, exist_ok=True)
    destination = parent / ALPINE_VERSION
    with (parent / ".prepare.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if cached(destination):
            return {"ok": True, "changed": False, "version": ALPINE_VERSION}
        archive = parent / ("alpine-standard-" + ALPINE_VERSION + "-x86_64.iso")
        stage = Path(tempfile.mkdtemp(prefix=".prepare-", dir=parent))
        previous = stage.with_name(stage.name + "-previous")
        published = False
        try:
            if not archive.is_file() or digest(archive) != ALPINE_ISO_SHA256:
                download_factory_image(
                    ALPINE_ISO_URL, log, parent, filename=archive.name, max_bytes=MAX_BYTES
                )
            if digest(archive) != ALPINE_ISO_SHA256:
                raise ValueError("Alpine ISO SHA256 does not match the pinned release")
            manifest = extract(archive, stage)
            (stage / MANIFEST).write_text(json.dumps(manifest))
            if destination.exists():
                destination.rename(previous)
            try:
                stage.rename(destination)
                published = True
            except OSError:
                if previous.exists():
                    previous.rename(destination)
                raise
            if log:
                log("[pxe] verified RAM boot and local package repository " + ALPINE_VERSION)
            archive.unlink(missing_ok=True)
            return {"ok": True, "changed": True, "version": ALPINE_VERSION}
        except Exception as exc:  # noqa: BLE001 -- preparation disarms boot on failure
            return {
                "ok": False,
                "changed": False,
                "error": "RAM boot assets are not ready: " + str(exc),
            }
        finally:
            shutil.rmtree(stage, ignore_errors=True)
            if published:
                shutil.rmtree(previous, ignore_errors=True)
