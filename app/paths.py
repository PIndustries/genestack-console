"""Install vs compiled-binary locations.

Nuitka onefile extracts to a temp dir. __file__ still works there for bundled
static/templates/ansible. Writable state (DB, PXE) must live on the host, not
inside the extract.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def frozen() -> bool:
    """True for a published binary.

    PyInstaller sets ``sys.frozen``. Nuitka onefile does not. It sets
    ``__compiled__`` on this module instead. A source checkout has neither,
    so the update watcher must not download the Linux binary there.
    """
    if getattr(sys, "frozen", False):
        return True
    return "__compiled__" in globals()


def package_root() -> Path:
    """Directory that contains the ``app`` package (checkout or extract)."""
    return Path(__file__).resolve().parent.parent


def host_prefix() -> Path:
    raw = (os.environ.get("GSC_PREFIX") or "").strip()
    if raw:
        return Path(raw)
    if frozen():
        return Path("/opt/genestack-console")
    return package_root()
