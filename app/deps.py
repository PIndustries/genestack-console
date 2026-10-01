"""Shared FastAPI dependencies."""

from __future__ import annotations

from collections.abc import Generator

from fastapi import Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.auth import ROLE_RANK, Role, key_label, require_role, resolve_principal
from app.config import get_settings
from app.db import get_db as _get_db
from app.models import Environment, User
from app.schemas import Principal

# Re-export common deps
get_db = _get_db
get_current_user = resolve_principal
require_viewer = require_role("viewer")
require_operator = require_role("operator")
require_admin = require_role("admin")


def get_db_session() -> Generator[Session, None, None]:
    """Alias for get_db for clarity in non-FastAPI call sites."""
    yield from _get_db()


def check_tenant_access(
    db: Session,
    principal: Principal,
    tenant_id: str | None,
    minimum: Role,
) -> None:
    """403 unless the principal may act at >= minimum role in the tenant.

    Platform admins bypass tenancy (break-glass). A session user's effective
    role is their membership role in *this* tenant, not their global role.
    Environments without a tenant are platform-admin only.
    """
    if principal.platform_admin:
        return
    if tenant_id is None or principal.user_id is None:
        raise HTTPException(status_code=403, detail="Not a member of this tenant")
    from app.services import accounts

    user = db.get(User, principal.user_id)
    member_role = accounts.role_in_tenant(db, user, tenant_id) if user else None
    if member_role is None or ROLE_RANK[member_role.value] < ROLE_RANK[minimum]:
        raise HTTPException(
            status_code=403,
            detail=f"Requires tenant role '{minimum}' or higher",
        )


def principal_from_token(token: str, db: Session) -> Principal:
    """Resolve a Principal from a raw credential token.

    Used by transports that cannot use the standard security dependencies
    (SSE/WS header fallback in ``routers/stream.py`` and
    ``routers/terminal.py``). Browser query-string auth goes through
    single-use tickets instead (``app.services.tickets``). Accepts exactly the
    same credentials as header auth in ``app.auth.resolve_principal``: static
    API keys from config (platform admin) or user session tokens.
    """
    token = token.strip()
    role = get_settings().parsed_api_keys().get(token)
    if role is not None:
        return Principal(
            username=key_label(token, role),
            role=role,
            auth_method="api_key",
            platform_admin=True,
        )

    from app.services import accounts

    user = accounts.resolve_session(db, token)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key or session token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return Principal(
        username=user.username,
        role=accounts.highest_role(db, user).value,  # type: ignore[arg-type]
        auth_method="session",
        user_id=user.id,
        platform_admin=user.platform_admin,
    )


def get_env_scoped(minimum: Role = "viewer"):
    """Dependency factory: load an Environment and enforce tenant role.

    404 if the environment does not exist; 403 unless the principal is a
    platform admin or holds a membership in the environment's tenant at
    >= ``minimum`` role. Static-key principals keep their configured role.
    """

    def _dep(
        environment_id: str,
        db: Session = Depends(get_db),
        principal: Principal = Depends(resolve_principal),
    ) -> Environment:
        env = db.get(Environment, environment_id)
        if not env:
            raise HTTPException(status_code=404, detail="Environment not found")
        if ROLE_RANK[principal.role] < ROLE_RANK[minimum]:
            raise HTTPException(
                status_code=403,
                detail=f"Role '{principal.role}' insufficient; requires '{minimum}' or higher",
            )
        check_tenant_access(db, principal, env.tenant_id, minimum)
        return env

    return _dep


__all__ = [
    "get_db",
    "get_db_session",
    "get_current_user",
    "get_env_scoped",
    "check_tenant_access",
    "principal_from_token",
    "require_viewer",
    "require_operator",
    "require_admin",
    "Principal",
]
