"""Host-level QEMU VM endpoints (hypervisor tier).

Host VMs are platform infrastructure (the QEMU processes on the console
host running genestack lab/dev/AIO nodes) — not tenant-scoped, so these
routes require platform roles only. Reads are viewer+; mutating actions go
through the job queue (hostvm.start|stop|restart catalog ops, operator+).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.deps import get_db, require_viewer
from app.schemas import Principal
from app.services import hypervisor

router = APIRouter(prefix="/api/v1", tags=["hostvms"])


@router.get("/hostvms")
def list_host_vms(
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    _: Principal = Depends(require_viewer),
) -> dict[str, Any]:
    """List host QEMU VMs with live state (runs discovery inline — cheap)."""
    return {"vms": hypervisor.vm_status(db, settings)}


@router.get("/hostvms/{vm_id}/serial")
def get_host_vm_serial(
    vm_id: str,
    lines: int = Query(default=200, ge=1, le=1000),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    _: Principal = Depends(require_viewer),
) -> dict[str, Any]:
    """Tail a VM's serial console log (path guarded to the VM workdir)."""
    try:
        return hypervisor.tail_serial(db, settings, vm_id, lines=lines)
    except hypervisor.VMNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except hypervisor.SerialPathError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
