"""Resolve the installer-created PXE layout without permitting arbitrary symlinks."""
from pathlib import Path

from app.services.request_safety import under_root


def provisioning_root(data_dir: str | Path) -> Path:
    data = Path(data_dir).resolve()
    candidate = data / "pxe"
    if candidate.is_symlink() and data.name == "data":
        # genestack-console.sh prepare_dirs creates exactly this sibling link
        # so the optional PXE sidecar and the API share one payload directory.
        sibling = data.parent / "pxe"
        if candidate.readlink() == Path("../pxe") and sibling.resolve() == sibling:
            return sibling
    return under_root(data, "pxe")
