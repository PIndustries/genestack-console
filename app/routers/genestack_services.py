"""Genestack service registry, cluster probes, pipeline, and components writes."""

from __future__ import annotations

from typing import Any

import yaml
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth import Role
from app.config import get_settings
from app.deps import check_tenant_access, get_db, require_operator, require_viewer
from app.models import Environment
from app.schemas import Principal
from app.services import cluster as cluster_probe
from app.services import envconfig as envconfig_service
from app.services import genestack_bridge as bridge
from app.services.envcontext import build_context
from app.services.job_runner import JobRunner
from app.services.service_registry import build_service_registry, get_pipeline

router = APIRouter(prefix="/api/v1/genestack", tags=["genestack"])


def _resolve_env(
    db: Session,
    environment_id: str | None,
    principal: Principal,
    minimum: Role = "viewer",
) -> Environment | None:
    if not environment_id:
        return None
    env = db.get(Environment, environment_id)
    if not env:
        raise HTTPException(status_code=404, detail="Environment not found")
    check_tenant_access(db, principal, env.tenant_id, minimum)
    return env


class ComponentsUpdate(BaseModel):
    components: dict[str, bool]


@router.get("/services")
def list_services(
    environment_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_viewer),
) -> dict[str, Any]:
    env = _resolve_env(db, environment_id, principal)
    root = bridge.resolve_genestack_root(get_settings(), env)
    return build_service_registry(root)


@router.get("/services/status")
def list_services_status(
    environment_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_viewer),
) -> dict[str, Any]:
    env = _resolve_env(db, environment_id, principal)
    ctx = build_context(env, get_settings())
    try:
        return cluster_probe.services_status(ctx.kubeconfig)
    finally:
        ctx.cleanup()


@router.get("/cluster/status")
def get_cluster_status(
    environment_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_viewer),
) -> dict[str, Any]:
    env = _resolve_env(db, environment_id, principal)
    ctx = build_context(env, get_settings())
    try:
        return cluster_probe.cluster_status(ctx.kubeconfig)
    finally:
        ctx.cleanup()


@router.get("/pipeline")
def get_provisioning_pipeline(
    _: Principal = Depends(require_viewer),
) -> dict[str, Any]:
    return get_pipeline()


@router.put("/components")
def update_components(
    body: ComponentsUpdate,
    environment_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_operator),
) -> dict[str, Any]:
    """Merge component booleans into the env's config DOCUMENT (new version).

    The config doc is the source of truth: this never touches the on-host
    openstack-components.yaml — rendering/pushing is an explicit operator
    action (config push). The change is captured as a new config version so
    the version history doubles as the audit trail.
    """
    if not environment_id:
        raise HTTPException(
            status_code=400,
            detail="environment_id is required — components live in the env's config document",
        )
    env = _resolve_env(db, environment_id, principal, "operator")

    current = envconfig_service.get_current(db, env)
    doc: dict[str, Any] = dict(current[0]) if current else {}
    components = doc.get("components")
    if not isinstance(components, dict):
        components = {}
    else:
        components = dict(components)

    # Valid keys: components already in the doc, plus the genestack service
    # registry (bin/install-*.sh) for the env's resolved genestack root.
    root = bridge.resolve_genestack_root(get_settings(), env)
    known = set(components) | {
        s["name"] for s in build_service_registry(root)["services"]
    }
    unknown = sorted(k for k in body.components if k not in known)
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Unknown components: {', '.join(unknown)}. "
                f"Valid components: {', '.join(sorted(known))}"
            ),
        )

    updated: dict[str, dict[str, Any]] = {}
    for key, value in body.components.items():
        old = components.get(key)
        components[key] = bool(value)
        updated[key] = {"old": old, "new": bool(value)}
    doc["components"] = components

    try:
        row, _warnings = envconfig_service.put_version(
            db, env, yaml.safe_dump(doc, sort_keys=False), principal.username
        )
    except envconfig_service.ConfigValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    runner = JobRunner(db)
    runner.write_audit(
        actor=principal.username,
        action="genestack.components.update",
        resource_type="environment",
        resource_id=env.id,
        environment_id=env.id,
        details={"updated": updated, "version": row.version},
        success=True,
    )
    db.commit()

    return {
        "updated": updated,
        "components": components,
        "version": row.version,
        "note": f"saved to config v{row.version} — push to apply",
    }
