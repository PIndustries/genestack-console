"""Live read-only cluster and OpenStack visibility endpoints."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from app.config import get_settings
from app.deps import get_db, get_env_scoped
from app.models import Environment
from app.services import livestate
from app.services.demo import (
    canned_cluster_logs,
    canned_cluster_overview,
    canned_environment_health,
    canned_openstack_overview,
    is_demo_env,
)
from app.services.envcontext import build_context

router = APIRouter(prefix="/api/v1", tags=["livestate"])


def _config_download(
    path: Path | None, filename: str, cleanup: Callable[[], None]
) -> FileResponse:
    if path is None or not path.is_file():
        cleanup()
        raise HTTPException(status_code=404, detail=f"{filename} not found")
    return FileResponse(
        path,
        filename=filename,
        media_type="application/yaml",
        background=BackgroundTask(cleanup),
    )


@router.get("/environments/{environment_id}/cluster")
def get_environment_cluster(
    env: Environment = Depends(get_env_scoped("viewer")),
) -> dict[str, Any]:
    """Live cluster view: nodes, pod health, helm releases. Never raises.

    An unreachable cluster returns HTTP 200 with ``reachable: false`` and an
    ``error`` note; nodes/pods/releases stay empty.
    """
    if is_demo_env(env):
        return canned_cluster_overview()
    ctx = build_context(env, get_settings())
    try:
        return livestate.cluster_overview(ctx)
    finally:
        ctx.cleanup()


@router.get("/environments/{environment_id}/openstack")
def get_environment_openstack(
    env: Environment = Depends(get_env_scoped("viewer")),
) -> dict[str, Any]:
    """Live OpenStack view via the in-cluster admin client pod. Never raises.

    When the admin client pod cannot be used, returns HTTP 200 with
    ``available: false`` and an ``error`` note; all sections stay empty.
    """
    if is_demo_env(env):
        return canned_openstack_overview()
    ctx = build_context(env, get_settings())
    try:
        return livestate.openstack_overview(ctx)
    finally:
        ctx.cleanup()


@router.get("/environments/{environment_id}/health")
def get_environment_health(
    env: Environment = Depends(get_env_scoped("viewer")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Live health chips and last Tempest proof. Viewer. Never 500.

    Probe failures return HTTP 200 with chips in ``unknown`` and an ``error``
    string. Live pods win over helm release status.
    """
    if is_demo_env(env):
        return canned_environment_health()
    ctx = build_context(env, get_settings())
    try:
        return livestate.environment_health(ctx, db, env.id)
    except Exception as exc:  # noqa: BLE001
        return livestate._unknown_health(str(exc))
    finally:
        ctx.cleanup()


@router.get("/environments/{environment_id}/cluster/logs")
def get_environment_cluster_logs(
    pod: str = Query(..., min_length=1, max_length=253),
    namespace: str = Query(default="default", min_length=1, max_length=63),
    container: str | None = Query(default=None, max_length=253),
    tail: int = Query(default=200, ge=1, le=2000),
    previous: bool = Query(default=False),
    env: Environment = Depends(get_env_scoped("viewer")),
) -> dict[str, Any]:
    """Last N lines of a pod. HTTP 200 with ``error`` on kubectl failure; never 500."""
    if is_demo_env(env):
        return canned_cluster_logs(
            namespace=namespace,
            pod=pod,
            container=container,
            tail=tail,
            previous=previous,
        )
    ctx = build_context(env, get_settings())
    try:
        return livestate.cluster_pod_logs(
            ctx,
            namespace=namespace,
            pod=pod,
            container=container,
            tail=tail,
            previous=previous,
        )
    finally:
        ctx.cleanup()


@router.get("/environments/{environment_id}/access/kubeconfig")
def get_environment_kubeconfig(
    env: Environment = Depends(get_env_scoped("operator")),
) -> FileResponse:
    """Download this environment's kubeconfig. 404 if the file is missing."""
    ctx = build_context(env, get_settings())
    path = Path(ctx.kubeconfig) if ctx.kubeconfig else None
    return _config_download(path, "kubeconfig", ctx.cleanup)


@router.get("/environments/{environment_id}/access/talosconfig")
def get_environment_talosconfig(
    env: Environment = Depends(get_env_scoped("operator")),
) -> FileResponse:
    """Download this environment's talosconfig. 404 if the file is missing."""
    ctx = build_context(env, get_settings())
    path = (
        (ctx.config_dir / "talos" / "talosconfig")
        if ctx.config_dir is not None
        else None
    )
    return _config_download(path, "talosconfig", ctx.cleanup)
