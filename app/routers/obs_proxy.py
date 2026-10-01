"""Grafana / Prometheus / Alertmanager / Tempo session + HTTP proxy."""

from __future__ import annotations

import logging

import httpx
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.deps import get_db, get_env_scoped, resolve_principal
from app.models import Environment
from app.schemas import Principal
from app.services import obs_proxy as ob
from app.services.envcontext import build_context
from app.services.horizon_proxy import rewrite_location

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["observe-dashboards"])
_PROXY_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")


def _operator_env(
    env: Environment = Depends(get_env_scoped("operator")),
) -> Environment:
    return env


def _session_or_404(session_id: str, env_id: str) -> ob.ObsSession:
    entry = ob.get_session(session_id, env_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="dashboard session not found")
    return entry


@router.post("/environments/{environment_id}/cloud/{kind}/session")
def create_obs_session(
    kind: str,
    env: Environment = Depends(_operator_env),
    principal: Principal = Depends(resolve_principal),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Mint a short-lived monitoring dashboard session. Operator+."""
    if kind not in ob.KINDS:
        raise HTTPException(status_code=404, detail=f"unknown dashboard '{kind}'")
    ctx = build_context(env, settings)
    try:
        kube = ctx.kubeconfig
        if not kube:
            return {
                "ok": False,
                "session_id": None,
                "embed_url": None,
                "error": "environment has no kubeconfig",
            }
        sid, _entry = ob.open_session(
            kube, env_id=env.id, actor=principal.username, kind=kind
        )
    except ob.ObsProxyError as exc:
        log.warning("%s session failed env=%s err=%s", kind, env.id, exc)
        return {"ok": False, "session_id": None, "embed_url": None, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        log.warning("%s session failed env=%s", kind, env.id, exc_info=True)
        return {
            "ok": False,
            "session_id": None,
            "embed_url": None,
            "error": f"failed to reach {kind} ({type(exc).__name__})",
        }
    finally:
        ctx.cleanup()
    home = str(ob.KINDS[kind].get("home") or "/")
    embed = ob.proxy_prefix(env.id, kind, sid) + home
    log.info("%s session created env=%s session=%s", kind, env.id, sid)
    return {"ok": True, "session_id": sid, "embed_url": embed, "error": None, "kind": kind}


def _env_or_404(db: Session, environment_id: str) -> Environment:
    env = db.get(Environment, environment_id)
    if env is None:
        raise HTTPException(status_code=404, detail="Environment not found")
    return env


async def _proxy(
    request: Request,
    environment_id: str,
    kind: str,
    session_id: str,
    path: str,
    env: Environment,
    settings: Settings,
) -> Response:
    if kind not in ob.KINDS:
        raise HTTPException(status_code=404, detail="unknown dashboard")
    session = _session_or_404(session_id, environment_id)
    if session.kind != kind:
        raise HTTPException(status_code=404, detail="dashboard session not found")
    if not ob.is_safe_path(path):
        raise HTTPException(status_code=400, detail="invalid path")
    prefix = ob.proxy_prefix(environment_id, kind, session_id)
    rel = path if path.startswith("/") else f"/{path}"
    if request.url.query:
        rel = rel + "?" + request.url.query
    url = ob.upstream_url(session, rel)
    headers = {k: v for k, v in request.headers.items() if k.lower() not in _merged_skip()}
    cookie = ob.cookie_header(session)
    if cookie:
        headers["cookie"] = cookie
    body = await request.body()
    try:
        async with httpx.AsyncClient(timeout=ob.HTTP_TIMEOUT, follow_redirects=False) as client:
            upstream = await client.request(request.method, url, headers=headers, content=body)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"{kind} proxy failed") from exc
    media = upstream.headers.get("content-type") or "application/octet-stream"
    payload = ob.rewrite_body(kind, media, upstream.content, prefix)
    response = Response(content=payload, status_code=upstream.status_code, media_type=media)
    for key, val in upstream.headers.items():
        low = key.lower()
        if low in ob._STRIP_RESP or low in ob._HOP:
            continue
        if low == "location":
            response.headers["location"] = rewrite_location(
                val, prefix, public_hosts=ob.public_hosts_for(kind)
            )
            continue
        if low == "set-cookie":
            continue
        response.headers[key] = val
    getter = getattr(upstream.headers, "get_list", None)
    raw_cookies = list(getter("set-cookie")) if callable(getter) else []
    if not raw_cookies:
        one = upstream.headers.get("set-cookie")
        if one:
            raw_cookies = [one]
    secure = request.url.scheme == "https"
    for raw in raw_cookies:
        rewritten = rewrite_set_cookie(raw, prefix, secure=secure)
        if rewritten:
            response.headers.append("set-cookie", rewritten)
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self' 'unsafe-inline' 'unsafe-eval' data: blob:; "
        "frame-ancestors 'self'"
    )
    return response


def _merged_skip() -> set[str]:
    skip = set(ob._HOP) | set(ob._STRIP_REQ)
    skip.update({"host", "content-length"})
    return skip


@router.api_route(
    "/environments/{environment_id}/cloud/{kind}/{session_id}/",
    methods=_PROXY_METHODS,
)
@router.api_route(
    "/environments/{environment_id}/cloud/{kind}/{session_id}",
    methods=_PROXY_METHODS,
)
async def obs_root(
    request: Request,
    environment_id: str,
    kind: str,
    session_id: str,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> Response:
    env = _env_or_404(db, environment_id)
    return await _proxy(request, environment_id, kind, session_id, "/", env, settings)


@router.api_route(
    "/environments/{environment_id}/cloud/{kind}/{session_id}/{path:path}",
    methods=_PROXY_METHODS,
)
async def obs_path(
    request: Request,
    environment_id: str,
    kind: str,
    session_id: str,
    path: str,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> Response:
    env = _env_or_404(db, environment_id)
    return await _proxy(request, environment_id, kind, session_id, path, env, settings)
