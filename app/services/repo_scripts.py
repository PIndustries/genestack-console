"""Repo scripts inventory — surface genestack's utility/maintenance tooling.

Scans GENESTACK_ROOT for ``scripts/*.sh`` utilities, ``maintenances/*.txt``
runbooks, and ``ops-tools/**`` helpers so they are visible from the console.
Discovery is pure read-only: missing directories yield empty lists and the
scan never raises.

``SAFE_REPO_SCRIPTS`` is the curated allowlist enforced by the
``genestack.repo_script.run`` operation — it is the documented extension
point: add a script's basename here to make it runnable from the console.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

# Curated set of GENESTACK_ROOT/scripts utilities runnable via
# genestack.repo_script.run (basenames only).
SAFE_REPO_SCRIPTS = frozenset(
    {
        "backup-mariadb.sh",
        "cleanup-envoy-httproutes.sh",
        "cleanup-openstack-completed-jobs.sh",
    }
)


def _first_comment_line(path: Path) -> str:
    """First '#' comment line of a script (shebang skipped), else ''."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#!"):
                    continue
                if line.startswith("#"):
                    return line.lstrip("#").strip()
                break
    except OSError:
        pass
    return ""


def _first_non_empty_line(path: Path) -> str:
    """First non-empty line of a text file (runbook title), else ''."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    return line
    except OSError:
        pass
    return ""


def list_repo_scripts(genestack_root: Path) -> dict[str, Any]:
    """Inventory scripts/, maintenances/, and ops-tools/ under genestack_root.

    Never raises — unreadable/missing directories produce empty lists.
    """
    root = Path(genestack_root)

    scripts: list[dict[str, Any]] = []
    scripts_dir = root / "scripts"
    if scripts_dir.is_dir():
        for path in sorted(scripts_dir.glob("*.sh")):
            if not path.is_file():
                continue
            scripts.append(
                {
                    "name": path.name,
                    "path": str(path),
                    "description": _first_comment_line(path),
                    "runnable": path.name in SAFE_REPO_SCRIPTS,
                }
            )

    maintenances: list[dict[str, Any]] = []
    maint_dir = root / "maintenances"
    if maint_dir.is_dir():
        for path in sorted(maint_dir.glob("*.txt")):
            if not path.is_file():
                continue
            maintenances.append(
                {
                    "name": path.name,
                    "path": str(path),
                    "title": _first_non_empty_line(path),
                }
            )

    ops_tools: list[dict[str, Any]] = []
    ops_dir = root / "ops-tools"
    if ops_dir.is_dir():
        for path in sorted(ops_dir.rglob("*")):
            if path.is_file():
                ops_tools.append({"name": path.name, "path": str(path)})

    return {
        "genestack_root": str(root),
        "scripts": scripts,
        "maintenances": maintenances,
        "ops_tools": ops_tools,
        "safe_scripts": sorted(SAFE_REPO_SCRIPTS),
        "counts": {
            "scripts": len(scripts),
            "maintenances": len(maintenances),
            "ops_tools": len(ops_tools),
        },
    }
