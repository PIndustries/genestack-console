"""Bare-metal node endpoints (console-managed provisioning, no MAAS).

The UI's bare-metal card reads the node list here; all mutations go through
the job queue (baremetal.node.* catalog ops).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.deps import get_db, get_env_scoped
from app.models import Environment
from app.services import baremetal as baremetal_service

router = APIRouter(prefix="/api/v1/environments/{environment_id}", tags=["baremetal"])


@router.get("/baremetal")
def list_baremetal_nodes(
    db: Session = Depends(get_db),
    env: Environment = Depends(get_env_scoped("viewer")),
) -> dict[str, Any]:
    """List the environment's registered bare-metal nodes (BMC password never included)."""
    nodes = baremetal_service.list_nodes(db, env)
    return {"count": len(nodes), "nodes": nodes}
