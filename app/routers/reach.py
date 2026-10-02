"""WireGuard, Tailscale, and Cloudflare Tunnel on this deploy host.

Hub routes are platform admin. An environment link is an operator of that
environment. Secrets are write-only. A WireGuard client config is returned
once, on the create response.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.deps import get_current_user, get_db, get_env_scoped, require_admin
from app.models import Environment
from app.schemas import Principal
from app.services import reach
from app.services.job_runner import JobRunner

router = APIRouter(prefix="/api/v1", tags=["reach"])


class HubUpdate(BaseModel):
    enabled: Optional[bool] = None
    endpoint: Optional[str] = None
    network: Optional[str] = None
    listen_port: Optional[int] = None
    interface: Optional[str] = None
    hostname: Optional[str] = None
    secret: Optional[str] = None


class HubOut(BaseModel):
    kind: str
    enabled: bool
    status: str
    detail: Optional[str] = None
    address: Optional[str] = None
    public_key: Optional[str] = None
    listen_port: Optional[int] = None
    endpoint: Optional[str] = None
    network: Optional[str] = None
    interface: Optional[str] = None
    hostname: Optional[str] = None
    secret_configured: bool
    config_path: Optional[str] = None
    pid: Optional[int] = None
    updated_at: datetime


class LinkIn(BaseModel):
    name: str = Field(default="default", max_length=128)
    address: Optional[str] = None
    local_port: Optional[int] = None
    use_for_ssh: bool = False


class LinkOut(BaseModel):
    id: str
    environment_id: str
    kind: str
    name: str
    address: Optional[str] = None
    public_key: Optional[str] = None
    local_port: Optional[int] = None
    use_for_ssh: bool
    status: str
    detail: Optional[str] = None
    pid: Optional[int] = None
    created_at: datetime
    client_config: Optional[str] = None


def _platform(principal: Principal) -> None:
    if not principal.platform_admin:
        raise HTTPException(status_code=403, detail="Requires platform admin")


def _kind(kind: str) -> str:
    if kind not in reach.KINDS:
        raise HTTPException(
            status_code=404,
            detail="Reach kind must be wireguard, tailscale, or cloudflare",
        )
    return kind


def _run(db: Session, fn):
    try:
        return fn()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except LookupError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _audit(
    db: Session,
    principal: Principal,
    action: str,
    *,
    environment_id: str | None = None,
    **details: object,
) -> None:
    JobRunner(db).write_audit(
        actor=principal.username,
        action=action,
        resource_type="reach",
        environment_id=environment_id,
        details={key: value for key, value in details.items() if value is not None},
        success=True,
    )


@router.get("/reach", response_model=list[HubOut])
def list_hubs(
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_admin),
) -> list[dict]:
    _platform(principal)
    rows = []
    for kind in reach.KINDS:
        try:
            rows.append(reach.hub_view(reach.get_or_seed_hub(db, kind)))
        except ValueError as exc:
            db.rollback()
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.commit()
    return rows


@router.put("/reach/{kind}", response_model=HubOut)
def put_hub(
    kind: str,
    body: HubUpdate,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_admin),
) -> dict:
    _platform(principal)
    kind = _kind(kind)
    fields = body.model_dump(exclude_unset=True)
    row = _run(db, lambda: reach.update_hub(db, kind, fields))
    _audit(
        db,
        principal,
        "reach.hub.update",
        kind=kind,
        enabled=row.enabled,
        secret_set="secret" in fields and bool(fields.get("secret")),
    )
    db.commit()
    db.refresh(row)
    return reach.hub_view(row)


@router.post("/reach/{kind}/apply", response_model=HubOut)
def apply_hub(
    kind: str,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_admin),
) -> dict:
    _platform(principal)
    kind = _kind(kind)
    row = _run(db, lambda: reach.apply_hub(db, kind))
    _audit(db, principal, "reach.hub.apply", kind=kind, status=row.status)
    db.commit()
    db.refresh(row)
    return reach.hub_view(row)


@router.post("/reach/{kind}/stop", response_model=HubOut)
def stop_hub(
    kind: str,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_admin),
) -> dict:
    _platform(principal)
    kind = _kind(kind)
    row = _run(db, lambda: reach.stop_hub(db, kind))
    _audit(db, principal, "reach.hub.stop", kind=kind, status=row.status)
    db.commit()
    db.refresh(row)
    return reach.hub_view(row)


@router.get("/environments/{environment_id}/reach", response_model=list[LinkOut])
def list_env_links(
    db: Session = Depends(get_db),
    env: Environment = Depends(get_env_scoped("operator")),
) -> list[dict]:
    return [reach.link_view(row) for row in reach.list_links(db, env.id)]


@router.post(
    "/environments/{environment_id}/reach/{kind}",
    response_model=LinkOut,
    status_code=201,
)
def create_env_link(
    kind: str,
    body: LinkIn,
    db: Session = Depends(get_db),
    env: Environment = Depends(get_env_scoped("operator")),
    principal: Principal = Depends(get_current_user),
) -> dict:
    kind = _kind(kind)

    def _do():
        return reach.upsert_link(
            db,
            env,
            kind,
            name=body.name,
            address=body.address,
            local_port=body.local_port,
            use_for_ssh=body.use_for_ssh,
        )

    link, client_config = _run(db, _do)
    _audit(
        db,
        principal,
        "reach.link.create",
        environment_id=env.id,
        kind=kind,
        name=link.name,
        address=link.address,
    )
    db.commit()
    db.refresh(link)
    view = reach.link_view(link)
    if client_config:
        view["client_config"] = client_config
    return view


@router.delete("/environments/{environment_id}/reach/{kind}/{name}")
def delete_env_link(
    kind: str,
    name: str,
    db: Session = Depends(get_db),
    env: Environment = Depends(get_env_scoped("operator")),
    principal: Principal = Depends(get_current_user),
) -> dict:
    kind = _kind(kind)
    _run(db, lambda: reach.delete_link(db, env.id, kind, name))
    _audit(
        db,
        principal,
        "reach.link.delete",
        environment_id=env.id,
        kind=kind,
        name=name,
    )
    db.commit()
    return {"ok": True}


@router.post(
    "/environments/{environment_id}/reach/cloudflare/{name}/forward",
    response_model=LinkOut,
)
def forward_env_link(
    name: str,
    db: Session = Depends(get_db),
    env: Environment = Depends(get_env_scoped("operator")),
    principal: Principal = Depends(get_current_user),
) -> dict:
    link = _run(db, lambda: reach.forward_link(db, env.id, name))
    _audit(
        db,
        principal,
        "reach.link.forward",
        environment_id=env.id,
        kind="cloudflare",
        name=link.name,
        status=link.status,
    )
    db.commit()
    db.refresh(link)
    return reach.link_view(link)
