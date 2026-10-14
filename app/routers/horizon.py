"""Horizon inside the console: session mint and same-origin HTTP proxy.

The operator browser often cannot reach horizon.<domain>. The console logs
in with the Keystone admin secret, which stays on the server, then proxies
the dashboard under an unguessable session id. Iframe requests cannot send
X-API-Key, so that id is the capability.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.auth import resolve_principal
from app.config import Settings, get_settings
from app.db import get_db
from app.deps import get_env_scoped
from app.models import Environment
from app.schemas import Principal
from app.services import horizon_proxy as hz
from app.services.envcontext import build_context
from app.services.request_safety import client_detail, log_field

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["horizon"])
_operator_env = get_env_scoped("operator")
_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")
_PASS = frozenset({"accept", "content-type", "x-csrftoken", "x-requested-with"})
_COOKIE_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_COOKIE_VALUE_RE = re.compile(r"^[A-Za-z0-9._~+/-]{0,4096}$")
_COOKIE_ATTR_RE = re.compile(
    r"^(?:; (?:Path=/[A-Za-z0-9._~/+-]{0,512}"
    r"|Secure|HttpOnly|SameSite=(?:Lax|Strict|None)"
    r"|Max-Age=[0-9]{1,10}|Expires=[A-Za-z0-9, :+-]{1,80}))*$",
    re.IGNORECASE,
)


def _session_or_404(session_id: str, environment_id: str) -> hz.HorizonSession:
    session = hz.get_session(session_id, environment_id)
    if session is None:
        raise HTTPException(status_code=404, detail="horizon session not found")
    return session


def _failed(error: str) -> dict[str, Any]:
    return {"ok": False, "session_id": None, "embed_url": None, "error": error}


def _cookie_match(name: str, value: str) -> tuple[str, str] | None:
    name_match = _COOKIE_NAME_RE.fullmatch(name.strip())
    value_match = _COOKIE_VALUE_RE.fullmatch(value)
    if name_match is None or value_match is None:
        return None
    return name_match.group(0), value_match.group(0)


def _browser_cookies(session: hz.HorizonSession, upstream: list[str]) -> list[str]:
    """Give the iframe the login cookies. Horizon JS must be able to read csrftoken."""
    seen: set[str] = set()
    for raw in upstream:
        name = raw.split("=", 1)[0].strip()
        name_match = _COOKIE_NAME_RE.fullmatch(name)
        if name_match is not None:
            seen.add(name_match.group(0).lower())
    extra: list[str] = []
    for name, value in session.cookies.items():
        pair = _cookie_match(str(name), str(value))
        if pair is None:
            continue
        key, text = pair
        if key.lower() in seen:
            continue
        flags = "; SameSite=Lax"
        if key.lower() != "csrftoken":
            flags += "; HttpOnly"
        extra.append(f"{key}={text}{flags}")
    return extra


def _accept_set_cookie(header: str) -> str | None:
    """Keep a Set-Cookie only when its name and value match the allowlist."""
    raw = (header or "").strip()
    if not raw or "=" not in raw:
        return None
    pair, sep, rest = raw.partition(";")
    name, eq, value = pair.strip().partition("=")
    if not eq:
        return None
    matched = _cookie_match(name, value.strip())
    if matched is None:
        return None
    key, text = matched
    if not sep:
        return f"{key}={text}"
    attr_match = _COOKIE_ATTR_RE.fullmatch(";" + rest)
    if attr_match is None:
        return None
    return f"{key}={text}{attr_match.group(0)}"


@router.post("/environments/{environment_id}/cloud/horizon/session")
def create_horizon_session(
    env: Environment = Depends(_operator_env),
    principal: Principal = Depends(resolve_principal),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Open Horizon as the cloud admin. Operator+. Never 500."""
    ctx = None
    sid = None
    try:
        ctx = build_context(env, settings)
        kube = ctx.kubeconfig
        if not kube:
            return _failed("environment has no kubeconfig")
        sid, _entry = hz.open_session(kube, env_id=env.id, actor=principal.username)
    except hz.HorizonProxyError as exc:
        log.warning("horizon session failed env=%s err=%s", env.id, exc)
        return _failed(str(exc))
    except Exception as exc:
        log.warning("horizon session failed env=%s", env.id, exc_info=True)
        return _failed(f"failed to reach horizon ({type(exc).__name__})")
    finally:
        if ctx is not None:
            ctx.cleanup()
    if not sid:
        return _failed("horizon session was not created")
    embed = hz.proxy_prefix(env.id, sid) + "/"
    log.info("horizon session created env=%s session=%s user=%s", env.id, sid, principal.username)
    return {"ok": True, "session_id": sid, "embed_url": embed, "error": None}


def _rewrite_payload(payload: bytes, media: str, prefix: str) -> bytes:
    """Rewrite Horizon HTML, JavaScript, and CSS onto the session prefix."""
    low = (media or "").lower()
    if "html" in low:
        rewrite = hz.rewrite_html
    elif "javascript" in low or "ecmascript" in low:
        rewrite = hz.rewrite_js
    elif "css" in low:
        rewrite = hz.rewrite_css
    else:
        return payload
    return rewrite(payload.decode("utf-8", "replace"), prefix).encode("utf-8")


def _env_or_404(db: Session, environment_id: str) -> Environment:
    env = db.get(Environment, environment_id)
    if env is None:
        raise HTTPException(status_code=404, detail="horizon session not found")
    return env


async def _proxy(
    request: Request,
    environment_id: str,
    session_id: str,
    path: str,
    settings: Settings,
    db: Session,
) -> Response:
    if not hz.SESSION_ID_RE.match(session_id or ""):
        raise HTTPException(status_code=404, detail="horizon session not found")
    if not hz.is_safe_path(path):
        raise HTTPException(status_code=400, detail="invalid path")
    session = _session_or_404(session_id, environment_id)
    env = _env_or_404(db, environment_id)
    try:
        ctx = build_context(env, settings)
    except Exception as exc:
        log.warning("horizon proxy context failed env=%s", environment_id, exc_info=True)
        raise HTTPException(
            status_code=502, detail="horizon proxy could not open the cluster"
        ) from exc
    rel = path if path.startswith("/") else f"/{path or ''}"
    if rel == "/":
        rel = "/"
    prefix = hz.proxy_prefix(environment_id, session_id)
    extra: dict[str, str] = {}
    for key, val in request.headers.items():
        if key.lower() in _PASS and val:
            extra[key] = val
    extra["Referer"] = f"http://{session.service}{rel}"
    try:
        kube = ctx.kubeconfig
        if not kube:
            raise HTTPException(status_code=502, detail="horizon proxy has no kubeconfig")
        body = await request.body()
        ctype = request.headers.get("content-type")
        status, payload, headers, set_cookies = hz.forward(
            kube,
            session,
            method=request.method,
            path=rel,
            query=request.url.query,
            body=body,
            content_type=ctype,
            extra_headers=extra,
        )
    except hz.HorizonProxyError as exc:
        log.warning("%s", log_field(exc))
        raise HTTPException(status_code=502, detail=client_detail(exc)) from exc
    finally:
        ctx.cleanup()
    media = headers.get("content-type") or headers.get("Content-Type") or "application/octet-stream"
    payload = _rewrite_payload(payload, media, prefix)
    response = Response(content=payload, status_code=status, media_type=media.split(";")[0])
    for key, val in headers.items():
        low = key.lower()
        if low in hz._STRIP_RESP or low in hz._HOP or low == "location":
            continue
        response.headers[key] = val
    location = headers.get("location") or headers.get("Location")
    if location:
        response.headers["location"] = hz.rewrite_location(location, prefix)
    secure = request.url.scheme == "https"
    for raw in list(set_cookies) + _browser_cookies(session, set_cookies):
        rewritten = hz.rewrite_set_cookie(raw, prefix, secure=secure)
        accepted = _accept_set_cookie(rewritten or "")
        if accepted:
            response.headers.append("set-cookie", accepted)
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Content-Security-Policy"] = "frame-ancestors 'self'"
    return response


@router.api_route(
    "/environments/{environment_id}/cloud/horizon/{session_id}",
    methods=_METHODS,
)
@router.api_route(
    "/environments/{environment_id}/cloud/horizon/{session_id}/",
    methods=_METHODS,
)
async def horizon_root(
    request: Request,
    environment_id: str,
    session_id: str,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> Response:
    return await _proxy(request, environment_id, session_id, "/", settings, db)


@router.api_route(
    "/environments/{environment_id}/cloud/horizon/{session_id}/{path:path}",
    methods=_METHODS,
)
async def horizon_path(
    request: Request,
    environment_id: str,
    session_id: str,
    path: str,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> Response:
    return await _proxy(request, environment_id, session_id, path, settings, db)
