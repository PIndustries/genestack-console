"""One read for the Overview page."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from app.deps import get_env_scoped
from app.models import Environment
from app.services.overview import overview_read

router = APIRouter(prefix="/api/v1", tags=["overview"])


@router.get("/environments/{environment_id}/overview")
def get_environment_overview(
    refresh: bool = Query(default=False),
    env: Environment = Depends(get_env_scoped("viewer")),
) -> dict[str, Any]:
    """Machines, cloud, VMs, and the Kubernetes lists Overview draws.

    Inventory and jobs are not included. A section that does not answer is
    marked unavailable and the rest of the read still returns.
    """
    return overview_read(str(env.id), refresh=refresh)
