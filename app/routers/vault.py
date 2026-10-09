"""Names stored in the console vault.

Values stay encrypted. ``environment_id`` limits the list to that environment.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.auth import Role
from app.deps import check_tenant_access, get_db, require_admin, require_viewer
from app.models import AuditLog, Environment, Tenant
from app.schemas import Principal
from app.services.vaultstore import VaultError, list_names, save_note

router = APIRouter(prefix="/api/v1/vault", tags=["vault"])


class NoteBody(BaseModel):
    tenant_id: str = Field(min_length=1, max_length=36)
    environment_id: str | None = Field(default=None, max_length=36)
    name: str = Field(min_length=1, max_length=200)
    value: str = Field(min_length=1, max_length=100_000)


def _tenant(db: Session, principal: Principal, tenant_id: str, minimum: Role) -> Tenant:
    check_tenant_access(db, principal, tenant_id, minimum)
    row = db.get(Tenant, tenant_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Tenant not found")
    return row


def _audit(
    db: Session,
    principal: Principal,
    action: str,
    tenant_id: str,
    *,
    success: bool,
    details: dict[str, object] | None = None,
) -> None:
    db.add(
        AuditLog(
            actor=principal.username,
            action=action,
            resource_type="vault",
            resource_id=tenant_id,
            details=details or {},
            success=success,
        )
    )
    db.commit()


@router.get("/items")
def get_items(
    tenant_id: str,
    environment_id: str | None = None,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_viewer),
) -> dict:
    """Password names only. The values stay in the console.

    ``environment_id`` limits the list to that environment. Tenant-scoped
    names stay on the unfiltered list.
    """
    tenant = _tenant(db, principal, tenant_id, "viewer")
    rows = list_names(db, tenant.id)
    if environment_id:
        env = db.get(Environment, environment_id)
        if env is None or env.tenant_id != tenant.id:
            raise HTTPException(status_code=404, detail="Environment not found")
        rows = [row for row in rows if row.get("environment_id") == env.id]
    return {"ok": True, "count": len(rows), "items": rows}


@router.put("/items")
def put_item(
    body: NoteBody,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_admin),
) -> dict:
    tenant = _tenant(db, principal, body.tenant_id, "admin")
    environment_id = (body.environment_id or "").strip() or None
    try:
        row = save_note(db, tenant.id, environment_id, body.name, body.value)
    except VaultError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _audit(
        db,
        principal,
        "vault.item",
        tenant.id,
        success=True,
        details={"name": row.name, "environment_id": row.environment_id},
    )
    return {"ok": True, "name": row.name, "kind": row.kind}
