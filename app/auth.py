"""Authentication: static API keys (break-glass) and user session tokens."""

from __future__ import annotations

import hashlib
import re
from typing import Literal

from fastapi import Depends, HTTPException, Request, Security, status
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.schemas import Principal
from app.services import accounts, oidc

Role = Literal["viewer", "operator", "admin"]

ROLE_RANK: dict[Role, int] = {
    "viewer": 1,
    "operator": 2,
    "admin": 3,
}

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
_bearer = HTTPBearer(auto_error=False)


def key_label(token: str, role: str) -> str:
    """Non-reversible audit label for a static API key.

    The label is ``<role>:<6 hex chars of sha256(key)>`` — stable per key for
    audit correlation, but it never exposes any substring of the key itself.
    """
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()[:6]
    return f"{role}:{digest}"


def hosted_cookie_token(request: Request, *, check_origin: bool = True) -> str | None:
    """Return a well-formed session-only cookie under the configured hosted policy."""
    settings = get_settings()
    token = request.cookies.get(oidc.NATIVE_SESSION_COOKIE)
    if token is None:
        return None
    origin = oidc.native_cookie_origin(settings)
    if origin is None:
        raise HTTPException(
            status_code=401, detail="Hosted cookie authentication is not enabled"
        )
    occurrences = sum(
        part.strip().split("=", 1)[0] == oidc.NATIVE_SESSION_COOKIE
        for header in request.headers.getlist("cookie")
        for part in header.split(";")
    )
    if occurrences != 1 or re.fullmatch(r"[A-Za-z0-9_-]{43}", token) is None:
        raise HTTPException(status_code=401, detail="Invalid session cookie")
    if check_origin and request.method not in {"GET", "HEAD", "OPTIONS"}:
        if request.headers.getlist("origin") != [origin]:
            raise HTTPException(
                status_code=403,
                detail="Trusted Origin required for cookie authentication",
            )
    return token


def resolve_principal(
    request: Request,
    api_key: str | None = Security(_api_key_header),
    bearer: HTTPAuthorizationCredentials | None = Security(_bearer),
    db: Session = Depends(get_db),
) -> Principal:
    """Authenticate via X-API-Key or Authorization: Bearer <token>.

    Static keys from config still work and are treated as platform admins
    (break-glass / bootstrap). Anything else is tried as a session token.
    """
    settings = get_settings()
    keys = settings.parsed_api_keys()

    token: str | None = None
    method = "api_key"
    if api_key:
        token = api_key.strip()
        method = "api_key"
    elif bearer and bearer.credentials:
        token = bearer.credentials.strip()
        method = "bearer"

    if not token:
        token = hosted_cookie_token(request)
        if token:
            method = "cookie"

    if not token:
        if settings.dev_auto_login:
            # DEV ONLY (auth.dev_auto_login in config.yaml): no credentials
            # presented — authenticate as platform-admin. Never enable in
            # production.
            return Principal(
                username="dev-auto-login",
                role="admin",
                auth_method="dev_auto_login",
                platform_admin=True,
            )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing credentials. Provide X-API-Key or Authorization: Bearer <key>.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    role = keys.get(token) if method != "cookie" else None
    if role is not None:
        return Principal(
            username=key_label(token, role),
            role=role,
            auth_method=method,
            platform_admin=True,
        )

    # Not a static key: try a login session token
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
        auth_method="cookie" if method == "cookie" else "session",
        user_id=user.id,
        platform_admin=user.platform_admin,
    )


def require_role(minimum: Role):
    """Dependency factory: require at least the given role."""

    def _checker(principal: Principal = Depends(resolve_principal)) -> Principal:
        if ROLE_RANK[principal.role] < ROLE_RANK[minimum]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Role '{principal.role}' insufficient; requires '{minimum}' or higher",
            )
        return principal

    return _checker


def role_allows(user_role: Role, required: Role) -> bool:
    return ROLE_RANK[user_role] >= ROLE_RANK[required]
