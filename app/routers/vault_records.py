"""Read, replace, and delete one environment vault record.

The list route stays on the vault router and returns names only. These
routes take the name as a query parameter because names contain slashes.
The value is returned only by GET, and it is not cached or written to the
audit row.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.auth import Role
from app.deps import check_tenant_access, get_db, require_admin
from app.models import AuditLog, Environment, Tenant
from app.schemas import Principal
from app.services.vaultstore import VaultError, delete_record, read_record, write_record

router = APIRouter(prefix="/api/v1/vault", tags=["vault"])

_NO_STORE = {"Cache-Control": "no-store"}
_MISSING = {
    "That record is not in this vault.",
    "That machine is not in this environment.",
}


class RecordBody(BaseModel):
    tenant_id: str = Field(min_length=1, max_length=36)
    environment_id: str = Field(min_length=1, max_length=36)
    name: str = Field(min_length=1, max_length=200)
    value: str = Field(min_length=1, max_length=100_000)


def _tenant(db: Session, principal: Principal, tenant_id: str, minimum: Role) -> Tenant:
    check_tenant_access(db, principal, tenant_id, minimum)
    row = db.get(Tenant, tenant_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Tenant not found")
    return row


def _environment(
    db: Session, principal: Principal, tenant_id: str, environment_id: str
) -> Environment:
    tenant = _tenant(db, principal, tenant_id, "admin")
    env = db.get(Environment, environment_id)
    if env is None or env.tenant_id != tenant.id:
        raise HTTPException(status_code=404, detail="Environment not found")
    return env


def _failure(exc: VaultError) -> HTTPException:
    text = str(exc)
    if text == "Choose an environment in this tenant.":
        return HTTPException(status_code=404, detail="Environment not found")
    if text in _MISSING:
        return HTTPException(status_code=404, detail=text)
    return HTTPException(status_code=400, detail=text)


def _audit(
    db: Session,
    principal: Principal,
    action: str,
    environment_id: str,
    name: str,
    kind: str,
) -> None:
    db.add(
        AuditLog(
            actor=principal.username,
            action=action,
            resource_type="vault",
            resource_id=environment_id,
            environment_id=environment_id,
            details={"name": name, "kind": kind},
            success=True,
        )
    )
    db.commit()


def _saved(row: dict[str, str]) -> JSONResponse:
    return JSONResponse(
        {"ok": True, "name": row["name"], "kind": row["kind"]},
        headers=_NO_STORE,
    )


@router.get("/records")
def get_record(
    tenant_id: str = Query(min_length=1, max_length=36),
    environment_id: str = Query(min_length=1, max_length=36),
    name: str = Query(min_length=1, max_length=200),
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_admin),
) -> JSONResponse:
    """One record, including its value. Admin. The list route stays names-only."""
    env = _environment(db, principal, tenant_id, environment_id)
    try:
        row = read_record(db, env.tenant_id, env.id, name)
    except VaultError as exc:
        raise _failure(exc) from exc
    _audit(db, principal, "vault.record.read", env.id, row["name"], row["kind"])
    return JSONResponse(
        {
            "ok": True,
            "name": row["name"],
            "kind": row["kind"],
            "value": row["value"],
        },
        headers=_NO_STORE,
    )


@router.put("/records")
def put_record(
    body: RecordBody,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_admin),
) -> JSONResponse:
    """Replace ssh, a machine password, a client certificate, or a note."""
    env = _environment(db, principal, body.tenant_id, body.environment_id)
    try:
        row = write_record(db, env.tenant_id, env.id, body.name, body.value)
    except VaultError as exc:
        raise _failure(exc) from exc
    _audit(db, principal, "vault.record.write", env.id, row["name"], row["kind"])
    return _saved(row)


@router.delete("/records")
def remove_record(
    tenant_id: str = Query(min_length=1, max_length=36),
    environment_id: str = Query(min_length=1, max_length=36),
    name: str = Query(min_length=1, max_length=200),
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_admin),
) -> JSONResponse:
    """Delete one record. A machine, and the on-disk client certificate, stay."""
    env = _environment(db, principal, tenant_id, environment_id)
    try:
        row = delete_record(db, env.tenant_id, env.id, name)
    except VaultError as exc:
        raise _failure(exc) from exc
    _audit(db, principal, "vault.record.delete", env.id, row["name"], row["kind"])
    return _saved(row)
