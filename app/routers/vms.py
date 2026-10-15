"""Per-environment VM (Nova server) read endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from app.config import Settings, get_settings
from app.deps import get_env_scoped
from app.models import Environment
from app.services import openstack_ops
from app.services.demo import canned_vms, is_demo_env
from app.services.read_cache import cached_read

router = APIRouter(prefix="/api/v1", tags=["vms"])


@router.get("/environments/{environment_id}/vms")
def list_environment_vms(
    refresh: bool = Query(default=False),
    env: Environment = Depends(get_env_scoped("viewer")),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """List Nova servers (VMs) running in the environment's cloud.

    Prefers OpenStack REST through the kube-apiserver service proxy.
    Falls back to the genestack openstack-admin-client pod (kubectl exec)
    when the env has no kubeconfig or the API proxy is unreachable.
    On failure the payload reports source="unavailable" with a short error
    instead of raising, so the UI can always render this endpoint.
    """
    if is_demo_env(env):
        return {"environment_id": env.id, **canned_vms()}
    env_id = str(getattr(env, "id", "") or "").strip()
    result = cached_read(
        f"vms.servers:{env_id}" if env_id else "",
        lambda: openstack_ops.list_servers(env, settings),
        refresh=refresh,
    )
    return {"environment_id": env.id, **result}
