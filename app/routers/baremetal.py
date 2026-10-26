"""Bare-metal node endpoints. The console installs Talos from the network.

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
from app.services import capacity

router = APIRouter(prefix="/api/v1/environments/{environment_id}", tags=["baremetal"])


@router.get("/baremetal")
def list_baremetal_nodes(
    db: Session = Depends(get_db),
    env: Environment = Depends(get_env_scoped("viewer")),
) -> dict[str, Any]:
    """List the environment's registered bare-metal nodes (BMC password never included)."""
    nodes = baremetal_service.list_nodes(db, env)
    return {"count": len(nodes), "nodes": nodes}


@router.get("/baremetal/capacity-plan")
def get_capacity_plan(
    db: Session = Depends(get_db),
    env: Environment = Depends(get_env_scoped("viewer")),
) -> dict[str, Any]:
    """Propose roles from fresh hardware evidence, without changing configuration."""
    return capacity.for_environment(db, env)
