"""Login/logout endpoints and auth introspection (whoami).

Local username/password accounts and static API keys are always available as a
fallback; OIDC/SSO login (``/oidc/login`` + ``/oidc/callback``) is optional and
only active when the ``oidc:`` config section enables it with an issuer URL.
"""

from __future__ import annotations

import time
from collections import deque

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
    Response,
    Security,
    status,
)
from fastapi.responses import RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import hosted_cookie_token
from app.config import get_settings
from app.deps import get_db, require_viewer
from app.models import Membership, Tenant, User
from app.schemas import (
    LoginRequest,
    LoginResponse,
    LoginUser,
    Principal,
    TenantMembershipRead,
    TicketResponse,
)
from app.services import accounts, oidc, tickets

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

_bearer = HTTPBearer(auto_error=False)


class _LoginThrottle:
    """In-memory sliding-window limiter for failed logins, per (ip, username).

    Bounds online password guessing. Memory-only by design: the API runs a
    single uvicorn process (a restart clears it, and a horizontally scaled
    deployment would need a shared store — out of scope here).
    """

    def __init__(self, max_failures: int = 5, window_seconds: float = 60.0) -> None:
        self.max_failures = max_failures
        self.window_seconds = window_seconds
        self._failures: dict[str, deque[float]] = {}

    def check(self, key: str) -> None:
        now = time.monotonic()
        failures = self._failures.setdefault(key, deque())
        while failures and now - failures[0] > self.window_seconds:
            failures.popleft()
        if len(failures) >= self.max_failures:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many failed login attempts — try again in a minute",
            )

    def record_failure(self, key: str) -> None:
        self._failures.setdefault(key, deque()).append(time.monotonic())

    def record_success(self, key: str) -> None:
        self._failures.pop(key, None)

    def clear(self) -> None:
        self._failures.clear()


_login_throttle = _LoginThrottle()


def _client_ip(request: Request) -> str:
    """Best-effort client IP for the login throttle key.

    ``X-Forwarded-For`` is honored only when the direct peer is loopback —
    i.e. we are sitting behind a reverse proxy on this host. From any other
    peer the header is attacker-controlled, and trusting it would let a
    brute-forcer bypass the per-IP throttle by rotating the XFF value.
    """
    client = request.client
    peer = client.host if client else "unknown"
    if peer in ("127.0.0.1", "::1"):
        forwarded = request.headers.get("x-forwarded-for", "")
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    return peer


def _tenants_for(db: Session, user_id: str) -> list[TenantMembershipRead]:
    """Tenants a user belongs to, with the membership role in each."""
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


@router.post("/login", response_model=LoginResponse)
def login(
    body: LoginRequest, request: Request, db: Session = Depends(get_db)
) -> LoginResponse:
    """Exchange username/password for a session token."""
    throttle_key = f"{_client_ip(request)}:{body.username.lower()}"
    _login_throttle.check(throttle_key)
    user = db.scalar(select(User).where(User.username == body.username))
    if (
        user is None
        or not user.active
        or not accounts.verify_password(body.password, user.password_hash)
    ):
        _login_throttle.record_failure(throttle_key)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password",
        )
    _login_throttle.record_success(throttle_key)
    session = accounts.create_session(db, user)
    # Capture before commit: attribute refresh after commit returns naive datetimes on SQLite
    token, expires_at = session.token, session.expires_at
    db.commit()
    return LoginResponse(
        token=token,
        expires_at=expires_at,
        user=LoginUser(
            username=user.username,
            platform_admin=user.platform_admin,
            tenants=_tenants_for(db, user.id),
        ),
    )


@router.post("/logout")
def logout(
    request: Request,
    response: Response,
    bearer: HTTPAuthorizationCredentials | None = Security(_bearer),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    """Invalidate the caller's session token (no-op for static API keys)."""
    if bearer and bearer.credentials:
        accounts.delete_session(db, bearer.credentials.strip())
        db.commit()
    else:
        token = hosted_cookie_token(request)
        if token:
            accounts.delete_session(db, token)
            db.commit()
    response.delete_cookie(
        oidc.NATIVE_SESSION_COOKIE, path="/", secure=True, httponly=True, samesite="lax"
    )
    return {"message": "logged out"}


@router.post(
    "/ticket", response_model=TicketResponse, status_code=status.HTTP_201_CREATED
)
def create_ticket(principal: Principal = Depends(require_viewer)) -> TicketResponse:
    """Exchange the caller's credential for a single-use WS/SSE ticket.

    Browser realtime endpoints (``/stream``, ``/terminal``) cannot set auth
    headers; instead of putting a session token or API key in the URL query
    string (which lands in access logs), the UI fetches a short-lived ticket
    here and connects with ``?ticket=``. Any authenticated role may mint one;
    the ticket carries exactly the caller's principal.
    """
    ticket, expires_in = tickets.create_ticket(principal)
    return TicketResponse(ticket=ticket, expires_in=expires_in)


@router.get("/whoami")
def whoami(
    principal: Principal = Depends(require_viewer),
    db: Session = Depends(get_db),
) -> dict:
    """Return the identity, role, and tenants for the current credential."""
    tenants = _tenants_for(db, principal.user_id) if principal.user_id else []
    return {
        "key_name": principal.username,
        "role": principal.role,
        "auth_method": principal.auth_method,
        "user_id": principal.user_id,
        "platform_admin": principal.platform_admin,
        "tenants": tenants,
    }


# ---------------------------------------------------------------------------
# Optional OIDC/SSO login (config-driven; local accounts stay the fallback)
# ---------------------------------------------------------------------------


@router.get("/methods")
def auth_methods() -> dict:
    """Advertise available login methods to the UI (unauthenticated)."""
    settings = get_settings()
    enabled = oidc.oidc_enabled(settings)
    return {
        "local": True,
        "oidc": enabled,
        "oidc_label": settings.oidc_label if enabled else "SSO",
    }


@router.get("/oidc/login")
def oidc_login(native: bool = False) -> RedirectResponse:
    """Redirect the browser to the OIDC provider's authorize URL.

    A single-use state + nonce pair is stored server-side (short TTL) and
    validated in the callback.
    """
    settings = get_settings()
    if not oidc.oidc_enabled(settings):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="OIDC login is not enabled"
        )
    if native and oidc.native_cookie_origin(settings) is None:
        raise HTTPException(
            status_code=404, detail="Hosted native sign-in is not configured"
        )
    try:
        url = oidc.build_authorize_url(settings, native=native)
    except Exception:
        # Do not leak provider/error internals (may contain client config).
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="OIDC provider metadata unavailable",
        ) from None
    return RedirectResponse(url=url, status_code=status.HTTP_302_FOUND)


@router.get("/oidc/callback")
def oidc_callback(
    code: str = "",
    state: str = "",
    error: str = "",
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """Complete the OIDC flow and hand the session token to the UI.

    Validates state and nonce, exchanges the code (id_token signature/iss/aud
    verified by app.services.oidc), looks up or auto-provisions the local
    user, and issues a normal console session. The token travels in the URL
    fragment (``/ui#token=…``): fragments are never sent to servers, so the
    token cannot appear in access logs or proxy logs. The UI reads it once and
    strips it from history.
    """
    settings = get_settings()
    if not oidc.oidc_enabled(settings):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="OIDC login is not enabled"
        )
    if error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="OIDC provider returned an error",
        )
    if not code or not state:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Missing code or state"
        )
    flow = oidc.pop_state_details(state)
    if flow is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or expired state"
        )
    if flow["native"] and oidc.native_cookie_origin(settings) is None:
        raise HTTPException(
            status_code=400, detail="Hosted native sign-in configuration changed"
        )
    try:
        claims = oidc.exchange_code(settings, code)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="OIDC token exchange or validation failed",
        ) from None
    if str(claims.get("nonce") or "") != flow["nonce"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="OIDC nonce mismatch"
        )
    user = oidc.provision_user(db, settings, claims)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="OIDC identity has no usable email/username",
        )
    if not user.active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Account is disabled"
        )
    session = accounts.create_session(db, user)
    token = session.token  # capture before commit (never logged)
    db.commit()
    if flow["native"]:
        response = RedirectResponse(url="/ui", status_code=status.HTTP_302_FOUND)
        response.set_cookie(
            oidc.NATIVE_SESSION_COOKIE,
            token,
            path="/",
            secure=True,
            httponly=True,
            samesite="lax",
            max_age=max(1, int(settings.session_ttl_hours * 3600)),
        )
        response.headers["Cache-Control"] = "no-store"
        return response
    return RedirectResponse(url=f"/ui#token={token}", status_code=status.HTTP_302_FOUND)
