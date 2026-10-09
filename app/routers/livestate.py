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
from app.services.clientconfig import grab_client_config, renew_client_config
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
    path: Path | None,
    filename: str,
    cleanup: Callable[[], None],
    source: str | None = None,
) -> FileResponse:
    if path is None or not path.is_file():
        cleanup()
        detail = f"{filename} not found"
        if source == "stored":
            detail = (
                f"The cluster did not issue a {filename}, and this console "
                "has no saved copy."
            )
        raise HTTPException(status_code=404, detail=detail)
    headers = {"Cache-Control": "no-store"}
    if source:
        headers["X-Genestack-Credential"] = source
    return FileResponse(
        path,
        filename=filename,
        media_type="application/yaml",
        headers=headers,
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


def _send_client_config(
    env: Environment, kind: str, db: Session, *, renew: bool
) -> FileResponse:
    """Send the vault copy, or replace it when ``renew`` is set.

    Download grabs the copy in this environment's vault. The first grab files
    the on-disk copy into the vault. Regenerate asks the cluster for a new
    client certificate, valid for one year, and replaces the vault copy.
    """
    ctx = build_context(env, get_settings())
    if renew:
        path, source, cleanup_issued = renew_client_config(ctx, env, db, kind)
        if path is None:
            cleanup_issued()
            ctx.cleanup()
            raise HTTPException(
                status_code=409,
                detail=(
                    "The cluster did not issue a new certificate. "
                    "The copy in the vault is unchanged."
                ),
            )
    else:
        path, source, cleanup_issued = grab_client_config(ctx, env, db, kind)

    def cleanup() -> None:
        cleanup_issued()
        ctx.cleanup()

    return _config_download(path, kind, cleanup, source)


@router.get("/environments/{environment_id}/access/kubeconfig")
def get_environment_kubeconfig(
    env: Environment = Depends(get_env_scoped("operator")),
    db: Session = Depends(get_db),
) -> FileResponse:
    """Download the kubeconfig saved in this environment's vault."""
    return _send_client_config(env, "kubeconfig", db, renew=False)


@router.post("/environments/{environment_id}/access/kubeconfig")
def renew_environment_kubeconfig(
    env: Environment = Depends(get_env_scoped("operator")),
    db: Session = Depends(get_db),
) -> FileResponse:
    """Regenerate the kubeconfig and replace the vault copy."""
    return _send_client_config(env, "kubeconfig", db, renew=True)


@router.get("/environments/{environment_id}/access/talosconfig")
def get_environment_talosconfig(
    env: Environment = Depends(get_env_scoped("operator")),
    db: Session = Depends(get_db),
) -> FileResponse:
    """Download the talosconfig saved in this environment's vault."""
    return _send_client_config(env, "talosconfig", db, renew=False)


@router.post("/environments/{environment_id}/access/talosconfig")
def renew_environment_talosconfig(
    env: Environment = Depends(get_env_scoped("operator")),
    db: Session = Depends(get_db),
) -> FileResponse:
    """Regenerate the talosconfig and replace the vault copy."""
    return _send_client_config(env, "talosconfig", db, renew=True)
