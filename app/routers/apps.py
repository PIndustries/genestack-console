"""Git-backed Apps CRUD and deploy trigger."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.deps import get_db, get_env_scoped, require_operator
from app.models import App, Environment
from app.schemas import AppCreate, AppDeployBody, AppUpdate, Principal
from app.services import apps as apps_svc
from app.services.job_runner import ConflictError, JobRunner, execute_operation

router = APIRouter(prefix="/api/v1", tags=["apps"])


def _base_url(request: Request) -> str:
    advertised = (get_settings().hub_advertise_url or "").strip().rstrip("/")
    if advertised:
        return advertised
    return str(request.base_url).rstrip("/")


def _http_app_error(exc: Exception) -> HTTPException:
    if isinstance(exc, apps_svc.AppError):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=500, detail="app failed")


@router.get("/environments/{environment_id}/apps")
def list_apps(
    request: Request,
    env: Environment = Depends(get_env_scoped("viewer")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    rows = list(
        db.scalars(
            select(App)
            .where(App.environment_id == env.id)
            .order_by(App.created_at.desc())
        )
    )
    base = _base_url(request)
    return {
        "environment_id": env.id,
        "apps": [apps_svc.to_read(row, base) for row in rows],
    }


@router.post("/environments/{environment_id}/apps")
def create_app(
    body: AppCreate,
    request: Request,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_operator),
    env: Environment = Depends(get_env_scoped("operator")),
) -> dict[str, Any]:
    try:
        name = apps_svc.validate_name(body.name)
        repo_url = apps_svc.validate_repo_url(body.repo_url)
        branch = apps_svc.validate_branch(body.branch)
        root_path = apps_svc.validate_root_path(body.root_path)
        target, build = apps_svc.validate_target_build(body.target, body.build)
        namespace = (
            apps_svc.validate_namespace(body.namespace, name)
            if target == "kubernetes"
            else None
        )
    except apps_svc.AppError as exc:
        raise _http_app_error(exc) from exc
    existing = db.scalar(
        select(App).where(App.environment_id == env.id, App.name == name)
    )
    if existing is not None:
        raise HTTPException(status_code=409, detail=f"app {name!r} already exists")
    webhook_id, webhook_secret = apps_svc.mint_webhook()
    row = App(
        environment_id=env.id,
        name=name,
        repo_url=repo_url,
        branch=branch,
        root_path=root_path,
        target=target,
        build=build,
        namespace=namespace,
        stack_name=(body.stack_name or name) if target == "openstack" else None,
        webhook_id=webhook_id,
        webhook_secret_encrypted=apps_svc.encrypt_webhook_secret(webhook_secret),
        deploy_token_encrypted=apps_svc.encrypt_token(body.deploy_token),
        poll_seconds=body.poll_seconds,
        created_by=principal.username,
    )
    db.add(row)
    JobRunner(db).write_audit(
        actor=principal.username,
        action="env.app.create",
        resource_type="app",
        resource_id=row.id,
        environment_id=env.id,
        details={"name": name, "repo_url": repo_url, "target": target},
    )
    db.commit()
    db.refresh(row)
    base = _base_url(request)
    return {
        "app": apps_svc.to_read(row, base),
        "webhook_url": apps_svc.webhook_url(base, row.webhook_id),
        "webhook_secret": webhook_secret,
    }


@router.get("/environments/{environment_id}/apps/{app_id}")
def get_app(
    app_id: str,
    request: Request,
    env: Environment = Depends(get_env_scoped("viewer")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    row = db.get(App, app_id)
    if row is None or row.environment_id != env.id:
        raise HTTPException(status_code=404, detail="app not found")
    return apps_svc.to_read(row, _base_url(request))


@router.patch("/environments/{environment_id}/apps/{app_id}")
def patch_app(
    app_id: str,
    body: AppUpdate,
    request: Request,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_operator),
    env: Environment = Depends(get_env_scoped("operator")),
) -> dict[str, Any]:
    row = db.get(App, app_id)
    if row is None or row.environment_id != env.id:
        raise HTTPException(status_code=404, detail="app not found")
    data = body.model_dump(exclude_unset=True)
    try:
        if "repo_url" in data and data["repo_url"] is not None:
            row.repo_url = apps_svc.validate_repo_url(data["repo_url"])
        if "branch" in data and data["branch"] is not None:
            row.branch = apps_svc.validate_branch(data["branch"])
        if "root_path" in data:
            row.root_path = apps_svc.validate_root_path(data["root_path"])
        if "build" in data and data["build"] is not None:
            _, build = apps_svc.validate_target_build(row.target, data["build"])
            row.build = build
        if "namespace" in data and data["namespace"] is not None:
            row.namespace = apps_svc.validate_namespace(data["namespace"], row.name)
        if "stack_name" in data:
            row.stack_name = data["stack_name"]
        if "poll_seconds" in data and data["poll_seconds"] is not None:
            row.poll_seconds = int(data["poll_seconds"])
        if "deploy_token" in data:
            token = data["deploy_token"]
            if token:
                row.deploy_token_encrypted = apps_svc.encrypt_token(token)
    except apps_svc.AppError as exc:
        raise _http_app_error(exc) from exc
    JobRunner(db).write_audit(
        actor=principal.username,
        action="env.app.patch",
        resource_type="app",
        resource_id=row.id,
        environment_id=env.id,
        details={"name": row.name},
    )
    db.commit()
    db.refresh(row)
    return apps_svc.to_read(row, _base_url(request))


@router.delete("/environments/{environment_id}/apps/{app_id}")
def delete_app(
    app_id: str,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_operator),
    env: Environment = Depends(get_env_scoped("operator")),
) -> dict[str, Any]:
    row = db.get(App, app_id)
    if row is None or row.environment_id != env.id:
        raise HTTPException(status_code=404, detail="app not found")
    name = row.name
    db.delete(row)
    JobRunner(db).write_audit(
        actor=principal.username,
        action="env.app.delete",
        resource_type="app",
        resource_id=app_id,
        environment_id=env.id,
        details={"name": name},
    )
    db.commit()
    return {"ok": True, "id": app_id}


@router.post("/environments/{environment_id}/apps/{app_id}/deploy")
def deploy_app(
    app_id: str,
    body: AppDeployBody | None = None,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_operator),
    env: Environment = Depends(get_env_scoped("operator")),
) -> dict[str, Any]:
    row = db.get(App, app_id)
    if row is None or row.environment_id != env.id:
        raise HTTPException(status_code=404, detail="app not found")
    force = bool(body.force) if body else False
    try:
        job = execute_operation(
            db,
            operation="app.deploy",
            params={"app_id": row.id, "force": force},
            environment_id=env.id,
            created_by=principal.username,
        )
    except ConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail={"message": str(exc), "conflicting_job_id": exc.job_id},
        ) from exc
    row.last_job_id = job.id
    row.last_status = job.status.value
    db.commit()
    return {"ok": True, "job_id": job.id, "app_id": row.id, "status": job.status.value}
