"""Terraform bare-metal provider accounts (Rackspace, AWS, Azure, GCP)."""

from __future__ import annotations

import json
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.deps import get_db, require_admin, require_viewer
from app.models import HardwareAccount, Membership
from app.schemas import Principal
from app.services.crypto import encrypt_secret

KINDS = ("rackspace", "aws", "azure", "gcp")
KindName = Literal["rackspace", "aws", "azure", "gcp"]

router = APIRouter(prefix="/api/v1/hardware", tags=["hardware"])


class HardwareAccountCreate(BaseModel):
    kind: KindName
    name: str = Field(min_length=1, max_length=128)
    region: str = Field(default="", max_length=128)
    credentials: dict[str, str] = Field(default_factory=dict)
    tenant_id: str | None = Field(default=None, max_length=36)


class HardwareAccountPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    region: str | None = Field(default=None, max_length=128)
    credentials: dict[str, str] | None = None


def _payload(row: HardwareAccount) -> dict[str, Any]:
    return {
        "id": row.id,
        "kind": row.kind,
        "name": row.name,
        "region": row.region,
        "has_credentials": bool((row.credentials_encrypted or "").strip()),
        "tenant_id": row.tenant_id,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


@router.get("/providers")
def list_providers(principal: Principal = Depends(require_viewer)) -> dict[str, Any]:
    """What Hardware can take metal from. Same set the portal demo shows."""
    _ = principal
    return {
        "ok": True,
        "providers": [
            {
                "id": "rackspace",
                "label": "Rackspace",
                "how": "terraform",
                "native": False,
            },
            {"id": "aws", "label": "AWS", "how": "terraform", "native": False},
            {"id": "azure", "label": "Azure", "how": "terraform", "native": False},
            {"id": "gcp", "label": "GCP", "how": "terraform", "native": False},
            {"id": "ovh", "label": "OVH", "how": "api", "native": False},
            {"id": "pxe", "label": "PXE", "how": "native", "native": True},
            {"id": "ssh", "label": "SSH", "how": "native", "native": True},
            {"id": "bmc", "label": "BMC", "how": "redfish", "native": True},
        ],
    }


@router.get("/accounts")
def list_accounts(
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_viewer),
) -> dict[str, Any]:
    stmt = select(HardwareAccount).order_by(HardwareAccount.kind, HardwareAccount.name)
    if not principal.platform_admin:
        member_tenants = select(Membership.tenant_id).where(
            Membership.user_id == principal.user_id
        )
        stmt = stmt.where(HardwareAccount.tenant_id.in_(member_tenants))
    rows = db.scalars(stmt).all()
    return {"ok": True, "count": len(rows), "accounts": [_payload(r) for r in rows]}


@router.post("/accounts", status_code=201)
def create_account(
    body: HardwareAccountCreate,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    from app.deps import check_tenant_access

    name = body.name.strip()
    kind = body.kind
    tenant_id = body.tenant_id

    if not principal.platform_admin and not tenant_id:
        raise HTTPException(
            status_code=400, detail="tenant_id is required for non-platform-admin users"
        )

    if tenant_id:
        check_tenant_access(db, principal, tenant_id, "admin")

    existing = db.scalar(
        select(HardwareAccount).where(
            HardwareAccount.tenant_id == tenant_id,
            HardwareAccount.kind == kind,
            HardwareAccount.name == name,
        )
    )
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail=f"{kind} account '{name}' already exists in this tenant",
        )
    creds = {k: str(v) for k, v in (body.credentials or {}).items() if str(v).strip()}
    if not creds:
        raise HTTPException(status_code=400, detail="credentials required")
    row = HardwareAccount(
        kind=kind,
        name=name,
        region=(body.region or "").strip(),
        credentials_encrypted=encrypt_secret(json.dumps(creds)) or "",
        tenant_id=tenant_id,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _payload(row)


@router.patch("/accounts/{account_id}")
def patch_account(
    account_id: str,
    body: HardwareAccountPatch,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    from app.deps import check_tenant_access

    row = db.get(HardwareAccount, account_id)
    if row is None:
        raise HTTPException(status_code=404, detail="account not found")

    check_tenant_access(db, principal, row.tenant_id, "admin")

    if body.name is not None:
        row.name = body.name.strip()
    if body.region is not None:
        row.region = body.region.strip()
    if body.credentials is not None:
        creds = {k: str(v) for k, v in body.credentials.items() if str(v).strip()}
        if creds:
            row.credentials_encrypted = encrypt_secret(json.dumps(creds)) or ""
    db.commit()
    db.refresh(row)
    return _payload(row)


@router.delete("/accounts/{account_id}", status_code=204)
def delete_account(
    account_id: str,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_admin),
) -> None:
    from app.deps import check_tenant_access

    row = db.get(HardwareAccount, account_id)
    if row is None:
        raise HTTPException(status_code=404, detail="account not found")

    check_tenant_access(db, principal, row.tenant_id, "admin")

    db.delete(row)
    db.commit()
