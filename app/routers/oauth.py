"""OAuth 2.0 endpoints: authorize, token, revoke, introspect, and metadata.

The token endpoint accepts a form body, which is what the specification
requires, and the same fields as JSON. A client secret is refused: this
server has public clients only.
"""

from __future__ import annotations

import html
from urllib.parse import urlencode, urlsplit, urlunsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.deps import get_db, require_viewer
from app.models import User
from app.schemas import Principal
from app.services import accounts, oauth2
from app.services.oauth2 import IssuedToken

router = APIRouter(prefix="/api/v1/oauth", tags=["oauth"])
metadata_router = APIRouter(tags=["oauth"])
_bearer = HTTPBearer(auto_error=False)


def _error(status_code: int, error: str, description: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": error, "error_description": description},
        headers=oauth2.no_store_headers(),
    )


def _tokens(issued: IssuedToken) -> JSONResponse:
    return JSONResponse(
        content=oauth2.token_payload(issued),
        headers=oauth2.no_store_headers(),
    )


async def _params(request: Request) -> dict[str, str]:
    ctype = request.headers.get("content-type", "")
    if "application/json" in ctype:
        try:
            data = await request.json()
        except Exception:
            return {}
        if not isinstance(data, dict):
            return {}
        return {str(key): "" if value is None else str(value) for key, value in data.items()}
    form = await request.form()
    return {str(key): str(value) for key, value in form.items()}


def _client_id_and_secret(params: dict[str, str], request: Request) -> tuple[str, str]:
    """Pull client_id. A non-empty secret means this client is not public."""
    client_id = (params.get("client_id") or "").strip()
    secret = params.get("client_secret") or ""
    header = request.headers.get("authorization", "")
    if header.lower().startswith("basic "):
        import base64

        try:
            decoded = base64.b64decode(header.split(" ", 1)[1].strip()).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return client_id, "invalid"
        basic_id, _, basic_secret = decoded.partition(":")
        client_id = client_id or basic_id
        secret = secret or basic_secret
    return client_id, secret


def _request_host(request: Request) -> str:
    return request.headers.get("host") or request.url.netloc


def _with_query(uri: str, query: dict[str, str]) -> str:
    parts = urlsplit(uri)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def _authorize_error(redirect_uri: str, error: str, description: str, state: str) -> RedirectResponse:
    query = {"error": error, "error_description": description}
    if state:
        query["state"] = state
    return RedirectResponse(_with_query(redirect_uri, query), status_code=302)


def _check_password(db: Session, username: str, password: str) -> User | None:
    user = db.scalar(select(User).where(User.username == username))
    if (
        user is None
        or not user.active
        or not accounts.verify_password(password, user.password_hash)
    ):
        return None
    return user


def _login_form(params: dict[str, str], message: str) -> HTMLResponse:
    hidden = ""
    for name in (
        "client_id",
        "redirect_uri",
        "state",
        "scope",
        "code_challenge",
        "code_challenge_method",
        "response_type",
    ):
        value = html.escape(params.get(name) or "", quote=True)
        hidden += f'<input type="hidden" name="{name}" value="{value}">'
    error = f'<p class="err">{html.escape(message)}</p>' if message else ""
    body = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sign in — Genestack Console</title>
<link rel="stylesheet" href="/static/css/theme.css">
<style>
  body {{ margin: 0; min-height: 100vh; display: grid; place-items: center;
         background: var(--bg); color: var(--text); font: 16px/1.4 system-ui, sans-serif; }}
  form {{ width: min(420px, calc(100% - 32px)); background: var(--panel);
         border: 1px solid var(--border); padding: 24px; }}
  h1 {{ font-size: 1.25rem; margin: 0 0 8px; }}
  label {{ display: block; margin-top: 12px; color: var(--muted); }}
  input[type=text], input[type=password] {{ width: 100%; box-sizing: border-box;
         margin-top: 4px; padding: 8px; background: var(--inset); color: var(--text);
         border: 1px solid var(--border); }}
  button {{ margin-top: 16px; background: var(--accent); color: var(--mark-fg);
           border: 0; padding: 10px 16px; font-weight: 600; }}
  .err {{ color: var(--bad-text); }}
</style>
</head>
<body>
<form method="post" action="/api/v1/oauth/authorize">
  <h1>Sign in</h1>
  <p>Sign in to continue back to the program that sent you here.</p>
  {error}
  <label>Username <input type="text" name="username" autocomplete="username" required></label>
  <label>Password <input type="password" name="password" autocomplete="current-password" required></label>
  {hidden}
  <button type="submit">Sign in</button>
</form>
</body>
</html>"""
    return HTMLResponse(body, headers=oauth2.no_store_headers())


def _authorize_params_ok(params: dict[str, str], request: Request) -> JSONResponse | None:
    """Refuse a request we must not redirect. None means the redirect URI is safe."""
    redirect_uri = params.get("redirect_uri") or ""
    client_id = (params.get("client_id") or "").strip()
    if len(params.get("state") or "") > 512 or len(params.get("code_challenge") or "") > 128:
        return _error(400, "invalid_request", "state or code_challenge is too long.")
    if not oauth2.client_id_ok(client_id) or not oauth2.redirect_allowed(
        redirect_uri, _request_host(request)
    ):
        return _error(
            400,
            "invalid_request",
            "client_id and redirect_uri are required. redirect_uri is this console's /ui, or a loopback address.",
        )
    return None


def _code_request_error(
    params: dict[str, str], request: Request
) -> JSONResponse | RedirectResponse | None:
    """None when this authorization request may issue a code."""
    refused = _authorize_params_ok(params, request)
    if refused is not None:
        return refused
    redirect_uri = params["redirect_uri"]
    state = params.get("state") or ""
    if (params.get("response_type") or "") != "code":
        return _authorize_error(
            redirect_uri,
            "unsupported_response_type",
            "response_type must be code.",
            state,
        )
    if (params.get("code_challenge_method") or "") != "S256" or not (
        params.get("code_challenge") or ""
    ).strip():
        return _authorize_error(
            redirect_uri,
            "invalid_request",
            "code_challenge with code_challenge_method=S256 is required.",
            state,
        )
    if not oauth2.scope_allowed(params.get("scope") or ""):
        return _authorize_error(
            redirect_uri, "invalid_scope", "The only scope is console.", state
        )
    return None


def _issue_code_redirect(db: Session, user: User, params: dict[str, str]) -> RedirectResponse:
    code = oauth2.issue_code(
        db,
        user,
        client_id=params["client_id"].strip(),
        redirect_uri=params["redirect_uri"],
        code_challenge=(params.get("code_challenge") or "").strip(),
    )
    db.commit()
    query = {"code": code}
    state = params.get("state") or ""
    if state:
        query["state"] = state
    return RedirectResponse(_with_query(params["redirect_uri"], query), status_code=302)


@router.get("/authorize", response_model=None)
def authorize_get(
    request: Request,
    db: Session = Depends(get_db),
    bearer: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> HTMLResponse | JSONResponse | RedirectResponse:
    """Start the authorization-code grant. A signed-in bearer skips the form."""
    params = dict(request.query_params)
    refused = _code_request_error(params, request)
    if refused is not None:
        return refused
    token = bearer.credentials.strip() if bearer and bearer.credentials else ""
    if token:
        user = accounts.resolve_session(db, token)
        if user is not None:
            return _issue_code_redirect(db, user, params)
    return _login_form(params, "")


@router.post("/authorize", response_model=None)
async def authorize_post(request: Request, db: Session = Depends(get_db)):
    """Sign in on the authorization page and send the browser back with a code."""
    from app.routers import auth as auth_routes

    params = await _params(request)
    refused = _code_request_error(params, request)
    if refused is not None:
        return refused
    username = (params.get("username") or "").strip()
    password = params.get("password") or ""
    throttle_key = f"{auth_routes._client_ip(request)}:{username.lower()}"
    try:
        auth_routes._login_throttle.check(throttle_key)
    except HTTPException as exc:
        return _login_form(params, str(exc.detail))
    user = _check_password(db, username, password)
    if user is None:
        auth_routes._login_throttle.record_failure(throttle_key)
        return _login_form(params, "Invalid username or password.")
    auth_routes._login_throttle.record_success(throttle_key)
    return _issue_code_redirect(db, user, params)


@router.post("/token")
async def token(request: Request, db: Session = Depends(get_db)) -> JSONResponse:
    """Exchange a grant for an access token and a refresh token.

    grant_type is password, authorization_code, or refresh_token.
    """
    from app.routers import auth as auth_routes

    params = await _params(request)
    client_id, secret = _client_id_and_secret(params, request)
    if secret:
        return _error(401, "invalid_client", "This server has no confidential clients.")
    if not oauth2.scope_allowed(params.get("scope") or ""):
        return _error(400, "invalid_scope", "The only scope is console.")
    grant = (params.get("grant_type") or "").strip()
    if grant == "password":
        username = (params.get("username") or "").strip()
        password = params.get("password") or ""
        if not username or not password:
            return _error(400, "invalid_request", "username and password are required.")
        throttle_key = f"{auth_routes._client_ip(request)}:{username.lower()}"
        try:
            auth_routes._login_throttle.check(throttle_key)
        except HTTPException as exc:
            return _error(429, "invalid_grant", str(exc.detail))
        user = _check_password(db, username, password)
        if user is None:
            auth_routes._login_throttle.record_failure(throttle_key)
            return _error(400, "invalid_grant", "Invalid username or password.")
        auth_routes._login_throttle.record_success(throttle_key)
        return _tokens(oauth2.issue_session(db, user))
    if grant == "refresh_token":
        session = accounts.refresh_session(db, params.get("refresh_token") or "")
        if session is None:
            return _error(400, "invalid_grant", "Invalid refresh token.")
        issued = IssuedToken(
            access_token=session.token,
            expires_at=session.expires_at,
            refresh_token=session.raw_refresh_token,
            refresh_expires_at=session.refresh_expires_at,
        )
        db.commit()
        return _tokens(issued)
    if grant == "authorization_code":
        if not oauth2.client_id_ok(client_id):
            return _error(400, "invalid_request", "client_id is required.")
        issued = oauth2.exchange_code(
            db,
            raw_code=params.get("code") or "",
            client_id=client_id,
            redirect_uri=params.get("redirect_uri") or "",
            verifier=params.get("code_verifier") or "",
        )
        if issued is None:
            return _error(400, "invalid_grant", "Invalid authorization code.")
        return _tokens(issued)
    if not grant:
        return _error(400, "invalid_request", "grant_type is required.")
    return _error(400, "unsupported_grant_type", "grant_type is not supported.")


@router.post("/revoke")
async def revoke(request: Request, db: Session = Depends(get_db)) -> JSONResponse:
    """RFC 7009. Always 200. Drops the one session this token names."""
    params = await _params(request)
    _client_id, secret = _client_id_and_secret(params, request)
    if secret:
        return _error(401, "invalid_client", "This server has no confidential clients.")
    oauth2.revoke_token(
        db,
        params.get("token") or "",
        (params.get("token_type_hint") or "").strip(),
    )
    return JSONResponse(content={}, headers=oauth2.no_store_headers())


@router.post("/introspect")
async def introspect(
    request: Request,
    principal: Principal = Depends(require_viewer),
    db: Session = Depends(get_db),
) -> JSONResponse:
    """RFC 7662. The caller must already be signed in. Another person's token is inactive."""
    params = await _params(request)
    found = oauth2.introspect(
        db,
        params.get("token") or "",
        (params.get("token_type_hint") or "").strip(),
    )
    if found is None:
        return JSONResponse({"active": False}, headers=oauth2.no_store_headers())
    if not principal.platform_admin and principal.user_id != found.get("sub"):
        return JSONResponse({"active": False}, headers=oauth2.no_store_headers())
    return JSONResponse(found, headers=oauth2.no_store_headers())


@metadata_router.get("/.well-known/oauth-authorization-server")
def authorization_server_metadata(request: Request) -> JSONResponse:
    """RFC 8414. Tells a client where the OAuth 2 endpoints are."""
    issuer = str(request.base_url).rstrip("/")
    return JSONResponse(
        {
            "issuer": issuer,
            "authorization_endpoint": f"{issuer}/api/v1/oauth/authorize",
            "token_endpoint": f"{issuer}/api/v1/oauth/token",
            "revocation_endpoint": f"{issuer}/api/v1/oauth/revoke",
            "introspection_endpoint": f"{issuer}/api/v1/oauth/introspect",
            "response_types_supported": ["code"],
            "grant_types_supported": [
                "authorization_code",
                "password",
                "refresh_token",
            ],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
            "revocation_endpoint_auth_methods_supported": ["none"],
            "scopes_supported": [oauth2.SCOPE],
        }
    )
