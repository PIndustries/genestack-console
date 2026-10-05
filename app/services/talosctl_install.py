"""Place the pinned talosctl on the deploy host. A missing download does not stop the console.

The installer does the same download. This path covers a console that updated
its own binary and did not re-run the shell installer.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request

from app.services.talos import TALOSCTL_VERSION

log = logging.getLogger(__name__)

_VERSION_RE = re.compile(r"^v\d+\.\d+\.\d+$")
_ELF = b"\x7fELF"
_ARCH = {"x86_64": "amd64", "aarch64": "arm64", "amd64": "amd64", "arm64": "arm64"}


def pinned_version() -> str:
    """Stable client pin. ``GSC_TALOSCTL_VERSION`` may name another stable tag."""
    override = os.environ.get("GSC_TALOSCTL_VERSION", "").strip()
    if override and _VERSION_RE.match(override):
        return override
    return TALOSCTL_VERSION


def should_ensure() -> bool:
    """Linux deploy host only. Tests and laptops do not download."""
    if os.environ.get("GSC_SKIP_TALOSCTL") == "1":
        return False
    if "pytest" in sys.modules:
        return False
    if sys.platform != "linux":
        return False
    return True


def install_prefix() -> str:
    raw = os.environ.get("GSC_PREFIX", "").strip()
    return raw or "/opt/genestack-console"


def client_tag(path: str) -> str:
    try:
        proc = subprocess.run(
            [path, "version", "--client"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    for line in (proc.stdout or "").splitlines():
        if "Tag:" in line:
            return line.split("Tag:", 1)[1].strip().split()[0]
    return ""


def file_is_elf(path: str) -> bool:
    try:
        with open(path, "rb") as handle:
            return handle.read(4) == _ELF
    except OSError:
        return False


def _link(src: str) -> None:
    dest = os.environ.get("GSC_TALOSCTL_LINK", "/usr/local/bin/talosctl").strip()
    if not dest:
        return
    try:
        if os.path.exists(dest) and not os.path.islink(dest):
            log.info("talosctl at %s left in place; jobs use %s", dest, src)
            return
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        tmp = dest + ".new"
        if os.path.lexists(tmp):
            os.unlink(tmp)
        os.symlink(src, tmp)
        os.replace(tmp, dest)
    except OSError as exc:
        log.info("talosctl link skipped: %s", exc)


def ensure() -> None:
    """Download the pinned client into ``$GSC_PREFIX/bin`` when it is missing."""
    if not should_ensure():
        return
    version = pinned_version()
    arch = _ARCH.get(os.uname().machine, "")
    if not arch:
        log.warning("talosctl: unsupported architecture %s", os.uname().machine)
        return
    dest_dir = os.path.join(install_prefix(), "bin")
    dest = os.path.join(dest_dir, "talosctl")
    if os.path.isfile(dest) and os.access(dest, os.X_OK) and client_tag(dest) == version:
        _link(dest)
        return
    url = (
        "https://github.com/siderolabs/talos/releases/download/"
        f"{version}/talosctl-linux-{arch}"
    )
    tmp = ""
    try:
        os.makedirs(dest_dir, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix="talosctl-", dir=dest_dir)
        os.close(fd)
        request = urllib.request.Request(url, headers={"User-Agent": "genestack-console"})
        with (
            urllib.request.urlopen(request, timeout=120) as response,
            open(tmp, "wb") as handle,
        ):
            shutil.copyfileobj(response, handle)
        if not file_is_elf(tmp):
            log.warning("talosctl download was not a Linux ELF — left unused")
            return
        os.chmod(tmp, 0o755)
        os.replace(tmp, dest)
        tmp = ""
        log.info("talosctl %s installed at %s", version, dest)
        _link(dest)
    except (OSError, urllib.error.URLError, TimeoutError) as exc:
        log.warning("talosctl %s was not installed: %s", version, exc)
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def start_ensure() -> None:
    """Background install. Startup does not wait on GitHub."""
    if not should_ensure():
        return
    threading.Thread(target=ensure, name="talosctl-ensure", daemon=True).start()
