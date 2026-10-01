"""Unified Talos + Kubernetes + OpenStack fabric."""

from __future__ import annotations

import re
from typing import Any, Literal

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.deps import get_db, get_env_scoped, require_operator
from app.models import Environment
from app.schemas import Principal
from app.services import platform
from app.services.demo import (
    DEMO_JOB_MESSAGE,
    canned_platform_overview,
    canned_talos_read,
    is_demo_env,
)
from app.services.job_runner import ConflictError, execute_operation

router = APIRouter(prefix="/api/v1", tags=["platform"])

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,80}$")


class UpgradeBody(BaseModel):
    image: str | None = None
    dry_run: bool | None = None
    run_sync: bool = False


class UpgradeManyBody(BaseModel):
    """Bulk Talos upgrade.

    ``rolling`` (default): one machine at a time.
    ``sequential`` is one-at-a-time without evacuate. ``parallel`` kicks
    every node at once (lab only). Enqueued as ``platform.talos.upgrade_many``.
    """

    image: str | None = None
    mode: Literal["parallel", "sequential", "rolling"] = "rolling"
    names: list[str] | None = None
    dry_run: bool | None = None
    run_sync: bool = False


class ResetBody(BaseModel):
    """Flags mapped onto ``talosctl reset --wait=false``.

    - ``graceful`` (default true): ``--graceful`` / ``--graceful=false``.
      Attempt an etcd leave before reset.
    - ``reboot`` (default false): ``--reboot``. Reboot after reset instead of halt.
    - ``wipe`` (default true): ``--wipe-mode=system-disk`` when true,
      ``--wipe-mode=none`` when false.
    """

    graceful: bool = True
    reboot: bool = False
    wipe: bool = True
    dry_run: bool | None = None
    run_sync: bool = False


class ApplyConfigBody(BaseModel):
    yaml: str = Field(min_length=1, max_length=platform.APPLY_YAML_MAX)
    mode: Literal["auto", "staged", "no-reboot", "reboot"] = "auto"
    dry_run: bool | None = None
    run_sync: bool = False


class MutateBody(BaseModel):
    """Optional flags for reboot/shutdown convenience routes."""

    dry_run: bool | None = None
    run_sync: bool = False


def _conflict_http(exc: ConflictError) -> HTTPException:
    detail: dict[str, Any] = {"message": str(exc)}
    if exc.job_id:
        detail["conflicting_job_id"] = exc.job_id
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)


def _enqueue(
    *,
    db: Session,
    env: Environment,
    principal: Principal,
    operation: str,
    params: dict[str, Any],
    run_sync: bool = False,
) -> dict[str, Any]:
    if is_demo_env(env):
        raise HTTPException(status_code=400, detail=DEMO_JOB_MESSAGE)
    try:
        job = execute_operation(
            db,
            operation=operation,
            params=params,
            environment_id=env.id,
            created_by=principal.username,
            run_sync=run_sync,
        )
    except ConflictError as exc:
        raise _conflict_http(exc) from exc
    db.refresh(job)
    message = f"{operation} queued"
    if run_sync:
        message = job.error or (f"{operation} {job.status.value}")
    return {
        "ok": job.status.value != "failed",
        "job_id": job.id,
        "status": job.status.value,
        "operation": job.operation,
        "dry_run": job.dry_run,
        "message": message,
        "error": job.error,
    }


def _check_node(name: str) -> str:
    if not _NAME_RE.match(name):
        raise HTTPException(status_code=400, detail="invalid node name")
    return name


def _check_service(service_id: str) -> str:
    raw = str(service_id or "").strip()
    if not platform.valid_service_id(raw):
        raise HTTPException(status_code=400, detail="invalid service")
    return raw


@router.get("/environments/{environment_id}/platform")
def get_environment_platform(
    env: Environment = Depends(get_env_scoped("viewer")),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    if is_demo_env(env):
        return {"environment_id": env.id, **canned_platform_overview(env)}
    result = platform.platform_overview(env, settings, db)
    return {"environment_id": env.id, **result}


@router.get("/environments/{environment_id}/platform/nodes/{name}/dmesg")
def get_node_dmesg(
    name: str,
    env: Environment = Depends(get_env_scoped("viewer")),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    _check_node(name)
    if is_demo_env(env):
        return canned_talos_read(name)
    return platform.talos_dmesg(env, name, settings, db)


@router.get("/environments/{environment_id}/platform/nodes/{name}/services")
def get_node_services(
    name: str,
    env: Environment = Depends(get_env_scoped("viewer")),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    _check_node(name)
    if is_demo_env(env):
        return canned_talos_read(name)
    return platform.talos_services(env, name, settings, db)


@router.get("/environments/{environment_id}/platform/nodes/{name}/health")
def get_node_health(
    name: str,
    env: Environment = Depends(get_env_scoped("viewer")),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    _check_node(name)
    if is_demo_env(env):
        return canned_talos_read(name)
    return platform.talos_health(env, name, settings, db)


@router.get("/environments/{environment_id}/platform/nodes/{name}/etcd")
def get_node_etcd(
    name: str,
    env: Environment = Depends(get_env_scoped("viewer")),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    _check_node(name)
    if is_demo_env(env):
        return canned_talos_read(name)
    return platform.talos_etcd(env, name, settings, db)


@router.get("/environments/{environment_id}/platform/nodes/{name}/machineconfig")
def get_node_machineconfig(
    name: str,
    env: Environment = Depends(get_env_scoped("viewer")),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    _check_node(name)
    if is_demo_env(env):
        return canned_talos_read(name)
    return platform.talos_machineconfig(env, name, settings, db)


@router.get("/environments/{environment_id}/platform/nodes/{name}/disks")
def get_node_disks(
    name: str,
    env: Environment = Depends(get_env_scoped("viewer")),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    _check_node(name)
    if is_demo_env(env):
        return canned_talos_read(name)
    return platform.talos_disks(env, name, settings, db)


@router.get("/environments/{environment_id}/platform/nodes/{name}/logs")
def get_node_logs(
    name: str,
    service: str = Query(
        default="kubelet",
        description="Talos service id (kubelet, containerd, machined, …)",
    ),
    env: Environment = Depends(get_env_scoped("viewer")),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    _check_node(name)
    svc = str(service or "").strip() or "kubelet"
    if not platform.valid_service_id(svc):
        raise HTTPException(status_code=400, detail="invalid service")
    if is_demo_env(env):
        return canned_talos_read(name)
    return platform.talos_logs(env, name, settings, db, service=svc)


@router.get("/environments/{environment_id}/platform/nodes/{name}/resources")
def get_node_resources(
    name: str,
    env: Environment = Depends(get_env_scoped("viewer")),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    _check_node(name)
    if is_demo_env(env):
        return canned_talos_read(name)
    return platform.talos_resources(env, name, settings, db)


@router.get("/environments/{environment_id}/platform/nodes/{name}/events")
def get_node_events(
    name: str,
    since: str | None = Query(
        default=None,
        description="Look-back window for talosctl events --since (e.g. 1h, 30m).",
    ),
    env: Environment = Depends(get_env_scoped("viewer")),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    _check_node(name)
    since_raw = str(since).strip() if since is not None else ""
    if since_raw and not platform.valid_events_since(since_raw):
        raise HTTPException(status_code=400, detail="invalid since")
    if is_demo_env(env):
        return canned_talos_read(name)
    return platform.talos_events(env, name, settings, db, since=since_raw or None)


@router.get("/environments/{environment_id}/platform/nodes/{name}/containers")
def get_node_containers(
    name: str,
    env: Environment = Depends(get_env_scoped("viewer")),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    _check_node(name)
    if is_demo_env(env):
        return canned_talos_read(name)
    return platform.talos_containers(env, name, settings, db)


@router.post("/environments/{environment_id}/platform/nodes/{name}/reboot")
def reboot_node(
    name: str,
    body: MutateBody | None = Body(default=None),
    env: Environment = Depends(get_env_scoped("operator")),
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_operator),
) -> dict[str, Any]:
    """Enqueue ``platform.talos.reboot`` (audited job + per-env mutex)."""
    _check_node(name)
    payload = body or MutateBody()
    params: dict[str, Any] = {"name": name}
    if payload.dry_run is not None:
        params["dry_run"] = payload.dry_run
    return _enqueue(
        db=db,
        env=env,
        principal=principal,
        operation="platform.talos.reboot",
        params=params,
        run_sync=payload.run_sync,
    )


@router.post("/environments/{environment_id}/platform/upgrade")
def upgrade_nodes(
    body: UpgradeManyBody | None = Body(default=None),
    env: Environment = Depends(get_env_scoped("operator")),
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_operator),
) -> dict[str, Any]:
    """Enqueue ``platform.talos.upgrade_many`` (audited job + per-env mutex)."""
    payload = body or UpgradeManyBody()
    image = str(payload.image).strip() if payload.image is not None else None
    if image == "":
        image = None
    if image is not None and not platform.valid_install_image(image):
        raise HTTPException(status_code=400, detail="invalid image")
    mode = str(payload.mode).strip().lower() if payload.mode else "rolling"
    if mode not in {"parallel", "sequential", "rolling"}:
        raise HTTPException(status_code=400, detail="invalid mode")
    params: dict[str, Any] = {"mode": mode}
    if image is not None:
        params["image"] = image
    if payload.names:
        params["names"] = list(payload.names)
    if payload.dry_run is not None:
        params["dry_run"] = payload.dry_run
    return _enqueue(
        db=db,
        env=env,
        principal=principal,
        operation="platform.talos.upgrade_many",
        params=params,
        run_sync=payload.run_sync,
    )


@router.post("/environments/{environment_id}/platform/nodes/{name}/upgrade")
def upgrade_node(
    name: str,
    body: UpgradeBody | None = Body(default=None),
    env: Environment = Depends(get_env_scoped("operator")),
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_operator),
) -> dict[str, Any]:
    """Enqueue ``platform.talos.upgrade`` (audited job + per-env mutex)."""
    _check_node(name)
    payload = body or UpgradeBody()
    image = str(payload.image).strip() if payload.image is not None else None
    if image == "":
        image = None
    if image is not None and not platform.valid_install_image(image):
        raise HTTPException(status_code=400, detail="invalid image")
    params: dict[str, Any] = {"name": name}
    if image is not None:
        params["image"] = image
    if payload.dry_run is not None:
        params["dry_run"] = payload.dry_run
    return _enqueue(
        db=db,
        env=env,
        principal=principal,
        operation="platform.talos.upgrade",
        params=params,
        run_sync=payload.run_sync,
    )


@router.post("/environments/{environment_id}/platform/nodes/{name}/shutdown")
def shutdown_node(
    name: str,
    body: MutateBody | None = Body(default=None),
    env: Environment = Depends(get_env_scoped("operator")),
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_operator),
) -> dict[str, Any]:
    """Enqueue ``platform.talos.shutdown`` (audited job + per-env mutex)."""
    _check_node(name)
    payload = body or MutateBody()
    params: dict[str, Any] = {"name": name}
    if payload.dry_run is not None:
        params["dry_run"] = payload.dry_run
    return _enqueue(
        db=db,
        env=env,
        principal=principal,
        operation="platform.talos.shutdown",
        params=params,
        run_sync=payload.run_sync,
    )


@router.post(
    "/environments/{environment_id}/platform/nodes/{name}/reset",
    summary="Reset a Talos node",
    description=(
        "Enqueues ``platform.talos.reset`` (audited job + per-env mutex). "
        "Maps onto ``talosctl reset --wait=false``.\n\n"
        "- **graceful** (default true): leave etcd if possible.\n"
        "- **reboot** (default false): reboot after reset instead of halt.\n"
        "- **wipe** (default true): wipe system disk."
    ),
)
def reset_node(
    name: str,
    body: ResetBody | None = Body(default=None),
    env: Environment = Depends(get_env_scoped("operator")),
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_operator),
) -> dict[str, Any]:
    _check_node(name)
    payload = body or ResetBody()
    params: dict[str, Any] = {
        "name": name,
        "graceful": payload.graceful,
        "reboot": payload.reboot,
        "wipe": payload.wipe,
    }
    if payload.dry_run is not None:
        params["dry_run"] = payload.dry_run
    return _enqueue(
        db=db,
        env=env,
        principal=principal,
        operation="platform.talos.reset",
        params=params,
        run_sync=payload.run_sync,
    )


@router.post(
    "/environments/{environment_id}/platform/nodes/{name}/apply-config",
    summary="Apply a machineconfig",
    description=(
        "Enqueues ``platform.talos.apply_config`` (audited job + per-env mutex). "
        "Runs ``talosctl apply-config --file … --mode <mode>``. "
        "``mode`` is one of auto, staged, no-reboot, reboot (default auto)."
    ),
)
def apply_node_config(
    name: str,
    body: ApplyConfigBody,
    env: Environment = Depends(get_env_scoped("operator")),
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_operator),
) -> dict[str, Any]:
    _check_node(name)
    if body.mode not in platform.APPLY_MODES:
        raise HTTPException(status_code=400, detail="invalid mode")
    params: dict[str, Any] = {
        "name": name,
        "yaml": body.yaml,
        "mode": body.mode,
    }
    if body.dry_run is not None:
        params["dry_run"] = body.dry_run
    return _enqueue(
        db=db,
        env=env,
        principal=principal,
        operation="platform.talos.apply_config",
        params=params,
        run_sync=body.run_sync,
    )


@router.post(
    "/environments/{environment_id}/platform/nodes/{name}/service/{service_id}/{action}"
)
def node_service_action(
    name: str,
    service_id: str,
    action: str,
    env: Environment = Depends(get_env_scoped("operator")),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    _check_node(name)
    svc = _check_service(service_id)
    act = str(action or "").strip().lower()
    if act not in platform.SERVICE_ACTIONS:
        raise HTTPException(status_code=400, detail="invalid action")
    return platform.talos_service_action(env, name, svc, act, settings, db)
