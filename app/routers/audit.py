"""Audit log endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.deps import check_tenant_access, get_db, require_viewer
from app.models import AuditLog, Environment, Membership
from app.schemas import AuditLogRead, Principal

router = APIRouter(prefix="/api/v1/audit", tags=["audit"])


@router.get("", response_model=list[AuditLogRead])
def list_audit_logs(
    environment_id: str | None = Query(default=None),
    action: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_viewer),
) -> list[AuditLog]:
    """List audit entries, scoped to the caller's tenants.

    Platform admins see everything. Session users only see entries for
    environments in tenants they belong to (same scoping as ``list_jobs``);
    an explicit ``environment_id`` filter 404s on a missing env and 403s when
    the caller is not a member of that env's tenant.
    """
    stmt = select(AuditLog).order_by(AuditLog.timestamp.desc()).limit(limit)
    if environment_id:
        env = db.get(Environment, environment_id)
        if not env:
            raise HTTPException(status_code=404, detail="Environment not found")
        check_tenant_access(db, principal, env.tenant_id, "viewer")
        stmt = stmt.where(AuditLog.environment_id == environment_id)
    elif not principal.platform_admin:
        # Session users only see entries for environments in their tenants
        member_envs = select(Environment.id).where(
            Environment.tenant_id.in_(
                select(Membership.tenant_id).where(
                    Membership.user_id == principal.user_id
                )
            )
        )
        stmt = stmt.where(AuditLog.environment_id.in_(member_envs))
    if action:
        stmt = stmt.where(AuditLog.action == action)
    return list(db.scalars(stmt).all())
