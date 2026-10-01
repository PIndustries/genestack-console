"""Tenant, membership, and user account endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.deps import check_tenant_access, get_db, require_viewer
from app.models import Membership, Tenant, User, UserRole
from app.schemas import (
    MemberAdd,
    MemberRead,
    Principal,
    TenantCreate,
    TenantMembershipRead,
    TenantRead,
    TenantUpdate,
    UserCreate,
    UserPasswordSet,
    UserRead,
)
from app.services import accounts

router = APIRouter(prefix="/api/v1", tags=["tenants"])


def _require_platform_admin(principal: Principal) -> None:
    if not principal.platform_admin:
        raise HTTPException(status_code=403, detail="Requires platform admin")


def _get_tenant_or_404(db: Session, tenant_id: str) -> Tenant:
    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant not found")
    return tenant


def _tenants_for_user(db: Session, user_id: str) -> list[TenantMembershipRead]:
    stmt = (
        select(Tenant, Membership.role)
        .join(Membership, Membership.tenant_id == Tenant.id)
        .where(Membership.user_id == user_id)
        .order_by(Tenant.name)
    )
    return [
        TenantMembershipRead(id=tenant.id, name=tenant.name, role=role.value)
        for tenant, role in db.execute(stmt).all()
    ]


def _user_read(db: Session, user: User) -> UserRead:
    return UserRead(
        id=user.id,
        username=user.username,
        platform_admin=user.platform_admin,
        active=user.active,
        created_at=user.created_at,
        tenants=_tenants_for_user(db, user.id),
    )


# ---------------------------------------------------------------------------
# Tenants
# ---------------------------------------------------------------------------


@router.get("/tenants", response_model=list[TenantRead])
def list_tenants(
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_viewer),
) -> list[Tenant]:
    """Platform admins see all tenants; others only their own."""
    if principal.platform_admin:
        return list(db.scalars(select(Tenant).order_by(Tenant.name)).all())
    stmt = (
        select(Tenant)
        .join(Membership, Membership.tenant_id == Tenant.id)
        .where(Membership.user_id == principal.user_id)
        .order_by(Tenant.name)
    )
    return list(db.scalars(stmt).all())


@router.post("/tenants", response_model=TenantRead, status_code=status.HTTP_201_CREATED)
def create_tenant(
    body: TenantCreate,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_viewer),
) -> Tenant:
    _require_platform_admin(principal)
    if db.scalar(select(Tenant).where(Tenant.name == body.name)):
        raise HTTPException(
            status_code=409, detail=f"Tenant name already exists: {body.name}"
        )
    tenant = Tenant(name=body.name, description=body.description)
    db.add(tenant)
    db.commit()
    db.refresh(tenant)
    return tenant


@router.get("/tenants/{tenant_id}", response_model=TenantRead)
def get_tenant(
    tenant_id: str,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_viewer),
) -> Tenant:
    tenant = _get_tenant_or_404(db, tenant_id)
    check_tenant_access(db, principal, tenant.id, "viewer")
    return tenant


@router.patch("/tenants/{tenant_id}", response_model=TenantRead)
def update_tenant(
    tenant_id: str,
    body: TenantUpdate,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_viewer),
) -> Tenant:
    tenant = _get_tenant_or_404(db, tenant_id)
    check_tenant_access(db, principal, tenant.id, "admin")
    data = body.model_dump(exclude_unset=True)
    if "name" in data and data["name"] != tenant.name:
        clash = db.scalar(
            select(Tenant).where(Tenant.name == data["name"], Tenant.id != tenant.id)
        )
        if clash:
            raise HTTPException(
                status_code=409, detail=f"Tenant name already exists: {data['name']}"
            )
    for key, value in data.items():
        setattr(tenant, key, value)
    db.add(tenant)
    db.commit()
    db.refresh(tenant)
    return tenant


@router.delete("/tenants/{tenant_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_tenant(
    tenant_id: str,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_viewer),
) -> None:
    _require_platform_admin(principal)
    tenant = _get_tenant_or_404(db, tenant_id)
    db.delete(tenant)
    db.commit()


# ---------------------------------------------------------------------------
# Memberships
# ---------------------------------------------------------------------------


@router.get("/tenants/{tenant_id}/members", response_model=list[MemberRead])
def list_members(
    tenant_id: str,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_viewer),
) -> list[MemberRead]:
    tenant = _get_tenant_or_404(db, tenant_id)
    check_tenant_access(db, principal, tenant.id, "admin")
    stmt = (
        select(User, Membership.role)
        .join(Membership, Membership.user_id == User.id)
        .where(Membership.tenant_id == tenant.id)
        .order_by(User.username)
    )
    return [
        MemberRead(user_id=user.id, username=user.username, role=role.value)
        for user, role in db.execute(stmt).all()
    ]


@router.post(
    "/tenants/{tenant_id}/members",
    response_model=MemberRead,
    status_code=status.HTTP_201_CREATED,
)
def add_member(
    tenant_id: str,
    body: MemberAdd,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_viewer),
) -> MemberRead:
    tenant = _get_tenant_or_404(db, tenant_id)
    check_tenant_access(db, principal, tenant.id, "admin")
    user = db.scalar(select(User).where(User.username == body.username))
    if user is None:
        raise HTTPException(status_code=404, detail=f"User not found: {body.username}")
    existing = db.scalar(
        select(Membership).where(
            Membership.user_id == user.id, Membership.tenant_id == tenant.id
        )
    )
    if existing:
        raise HTTPException(
            status_code=409, detail=f"User '{body.username}' is already a member"
        )
    membership = Membership(
        user_id=user.id, tenant_id=tenant.id, role=UserRole(body.role)
    )
    db.add(membership)
    db.commit()
    return MemberRead(
        user_id=user.id, username=user.username, role=membership.role.value
    )


@router.delete(
    "/tenants/{tenant_id}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT
)
def remove_member(
    tenant_id: str,
    user_id: str,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_viewer),
) -> None:
    tenant = _get_tenant_or_404(db, tenant_id)
    check_tenant_access(db, principal, tenant.id, "admin")
    membership = db.scalar(
        select(Membership).where(
            Membership.user_id == user_id, Membership.tenant_id == tenant.id
        )
    )
    if membership is None:
        raise HTTPException(status_code=404, detail="Membership not found")
    db.delete(membership)
    db.commit()


# ---------------------------------------------------------------------------
# Users (platform admin only)
# ---------------------------------------------------------------------------


@router.get("/users", response_model=list[UserRead])
def list_users(
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_viewer),
) -> list[UserRead]:
    _require_platform_admin(principal)
    users = db.scalars(select(User).order_by(User.username)).all()
    return [_user_read(db, user) for user in users]


@router.post("/users", response_model=UserRead, status_code=status.HTTP_201_CREATED)
def create_user_endpoint(
    body: UserCreate,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_viewer),
) -> UserRead:
    _require_platform_admin(principal)
    if db.scalar(select(User).where(User.username == body.username)):
        raise HTTPException(
            status_code=409, detail=f"Username already exists: {body.username}"
        )
    user = accounts.create_user(
        db, body.username, body.password, platform_admin=body.platform_admin
    )
    for ref in body.memberships:
        tenant = db.get(Tenant, ref.tenant_id)
        if tenant is None:
            raise HTTPException(
                status_code=404, detail=f"Tenant not found: {ref.tenant_id}"
            )
        db.add(
            Membership(user_id=user.id, tenant_id=tenant.id, role=UserRole(ref.role))
        )
    db.commit()
    db.refresh(user)
    return _user_read(db, user)


def _get_user_or_404(db: Session, username: str) -> User:
    user = db.scalar(select(User).where(User.username == username))
    if user is None:
        raise HTTPException(status_code=404, detail=f"User not found: {username}")
    return user


@router.post("/users/{username}/password", status_code=status.HTTP_204_NO_CONTENT)
def set_user_password(
    username: str,
    body: UserPasswordSet,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_viewer),
) -> None:
    """Reset a user's password: platform admins for anyone, users for themselves."""
    user = _get_user_or_404(db, username)
    if not principal.platform_admin and principal.user_id != user.id:
        raise HTTPException(
            status_code=403, detail="Requires platform admin or the account itself"
        )
    user.password_hash = accounts.hash_password(body.password)
    db.add(user)
    db.commit()


@router.delete("/users/{username}", status_code=status.HTTP_204_NO_CONTENT)
def delete_user(
    username: str,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_viewer),
) -> None:
    """Delete a user; memberships and session tokens cascade. Platform admin only."""
    _require_platform_admin(principal)
    user = _get_user_or_404(db, username)
    if principal.user_id is not None and principal.user_id == user.id:
        raise HTTPException(status_code=409, detail="Cannot delete your own account")
    db.delete(user)
    db.commit()
