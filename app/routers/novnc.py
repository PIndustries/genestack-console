"""OpenStack noVNC in the console UI: session mint, HTML/asset proxy, websockify.

GET/WS under ``/cloud/console/{session_id}/`` are unauthenticated besides the
unguessable session id (iframe and <script> tags cannot send X-API-Key).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.auth import resolve_principal
from app.config import Settings, get_settings
from app.db import SessionLocal, get_db
from app.deps import get_env_scoped
from app.models import Environment
from app.schemas import Principal
from app.services import novnc as novnc_svc
from app.services import openstack_ops
from app.services.envcontext import build_context

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["novnc"])

_console_env = get_env_scoped("operator")

CLOSE_AUTH_FAILED = 4401
CLOSE_UPSTREAM = 1011


_NOVNC_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "SAMEORIGIN",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' 'unsafe-eval'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "connect-src 'self' ws: wss:; "
        "frame-ancestors 'self'"
    ),
}


def _html_error() -> Response:
    return Response(
        content=novnc_svc.UNAVAILABLE_HTML,
        media_type="text/html",
        headers=_NOVNC_HEADERS,
    )


def _session_or_404(session_id: str, environment_id: str) -> novnc_svc.NovncSession:
    session = novnc_svc.get_session(session_id, environment_id)
    if session is None:
        raise HTTPException(status_code=404, detail="console session not found")
    return session


@router.post("/environments/{environment_id}/cloud/servers/{server_id}/console/session")
def create_console_session(
    server_id: str,
    env: Environment = Depends(_console_env),
    principal: Principal = Depends(resolve_principal),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Mint a short-lived console session; operator+. Never 500."""
    server_id = (server_id or "").strip()
    if not novnc_svc.SERVER_ID_RE.match(server_id):
        raise HTTPException(status_code=400, detail="invalid server_id")
    try:
        result = openstack_ops.server_console(env, settings, server_id)
        url = (result or {}).get("url") if isinstance(result, dict) else None
        token = novnc_svc.parse_nova_token(str(url) if url else "")
        if not token:
            error = None
            if isinstance(result, dict):
                error = result.get("error")
            return {
                "ok": False,
                "server_id": server_id,
                "session_id": None,
                "embed_url": None,
                "error": error or "Nova console URL missing or has no token",
            }
        session_id = novnc_svc.create_session(
            env_id=env.id,
            server_id=server_id,
            nova_token=token,
            username=principal.username,
        )
    except Exception:  # noqa: BLE001 — never 500; token must not leak
        log.warning("novnc session: nova console lookup failed env=%s", env.id)
        return {
            "ok": False,
            "server_id": server_id,
            "session_id": None,
            "embed_url": None,
            "error": "failed to obtain Nova console URL",
        }
    embed_url = (
        f"/api/v1/environments/{env.id}/cloud/console/{session_id}/vnc_lite.html"
    )
    log.info(
        "novnc session created env=%s server=%s session=%s user=%s",
        env.id,
        server_id,
        session_id,
        principal.username,
    )
    return {
        "ok": True,
        "server_id": server_id,
        "session_id": session_id,
        "embed_url": embed_url,
        "error": None,
    }


def _proxy_asset(
    environment_id: str,
    session_id: str,
    path: str,
    db: Session,
    settings: Settings,
) -> Response:
    if not novnc_svc.SESSION_ID_RE.match(session_id or ""):
        raise HTTPException(status_code=404, detail="console session not found")
    if not novnc_svc.is_safe_console_path(path):
        raise HTTPException(status_code=400, detail="invalid path")
    _session_or_404(session_id, environment_id)
    html = novnc_svc.is_html_path(path)
    env = db.get(Environment, environment_id)
    if env is None:
        raise HTTPException(status_code=404, detail="console session not found")
    ctx = build_context(env, settings)
    try:
        kubeconfig = ctx.kubeconfig
        if not kubeconfig:
            if html:
                return _html_error()
            raise HTTPException(
                status_code=502, detail="console proxy could not reach nova-novncproxy"
            )
        upstream_path = path or "vnc_lite.html"
        try:
            status, body, ctype = novnc_svc.fetch_novnc_asset(kubeconfig, upstream_path)
            if html and status == 404 and upstream_path == "vnc_lite.html":
                status, body, ctype = novnc_svc.fetch_novnc_asset(
                    kubeconfig, "vnc_auto.html"
                )
        except novnc_svc.NovncProxyError:
            log.warning(
                "novnc asset proxy failed env=%s path=%s", environment_id, upstream_path
            )
            if html:
                return _html_error()
            raise HTTPException(
                status_code=502, detail="console proxy could not reach nova-novncproxy"
            ) from None
        headers = dict(_NOVNC_HEADERS)
        if html:
            if status != 200:
                return _html_error()
            rewritten = novnc_svc.rewrite_novnc_html(
                body.decode("utf-8", errors="replace")
            )
            return Response(content=rewritten, media_type="text/html", headers=headers)
        if status == 404:
            raise HTTPException(status_code=404, detail="not found")
        return Response(
            content=body, media_type=ctype, status_code=status, headers=headers
        )
    finally:
        ctx.cleanup()


@router.get("/environments/{environment_id}/cloud/console/{session_id}")
def novnc_asset_root(
    environment_id: str,
    session_id: str,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> Response:
    return _proxy_asset(environment_id, session_id, "", db, settings)


@router.get("/environments/{environment_id}/cloud/console/{session_id}/{path:path}")
def novnc_asset(
    environment_id: str,
    session_id: str,
    path: str,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> Response:
    return _proxy_asset(environment_id, session_id, path, db, settings)


def _pick_subprotocol(header: str | None) -> str | None:
    if not header:
        return None
    for item in header.split(","):
        name = item.strip()
        if name in ("binary", "base64"):
            return name
    return None


async def _bridge(browser: WebSocket, upstream: Any) -> None:
    """Copy binary (and text) frames browser ↔ upstream until either side closes."""

    async def to_upstream() -> None:
        try:
            while True:
                message = await browser.receive()
                if message.get("type") == "websocket.disconnect":
                    return
                payload = message.get("bytes")
                if payload is None:
                    payload = message.get("text")
                if payload is not None:
                    await upstream.send(payload)
        except WebSocketDisconnect:
            return
        except Exception:  # noqa: BLE001
            return

    async def to_browser() -> None:
        try:
            async for frame in upstream:
                if isinstance(frame, (bytes, bytearray, memoryview)):
                    await browser.send_bytes(bytes(frame))
                else:
                    await browser.send_text(str(frame))
        except Exception:  # noqa: BLE001
            return

    tasks = [
        asyncio.create_task(to_upstream(), name="novnc-up"),
        asyncio.create_task(to_browser(), name="novnc-down"),
    ]
    _done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    for task in pending:
        with contextlib.suppress(asyncio.CancelledError):
            await task


@router.websocket(
    "/environments/{environment_id}/cloud/console/{session_id}/websockify"
)
async def novnc_websockify(
    websocket: WebSocket,
    environment_id: str,
    session_id: str,
) -> None:
    session = novnc_svc.get_session(session_id, environment_id)
    if session is None:
        await websocket.close(code=CLOSE_AUTH_FAILED)
        return
    subprotocol = _pick_subprotocol(websocket.headers.get("sec-websocket-protocol"))
    await websocket.accept(subprotocol=subprotocol)
    db = SessionLocal()
    try:
        env = db.get(Environment, environment_id)
    finally:
        db.close()
    if env is None:
        await websocket.close(code=CLOSE_AUTH_FAILED)
        return
    ctx = build_context(env, get_settings())
    upstream = None
    try:
        kubeconfig = ctx.kubeconfig
        if not kubeconfig:
            await websocket.close(code=CLOSE_UPSTREAM)
            return
        try:
            port = await asyncio.to_thread(
                novnc_svc.ensure_novnc_portforward, kubeconfig
            )
            url = novnc_svc.local_websockify_url(port, session.nova_token)
        except novnc_svc.NovncProxyError:
            log.warning(
                "novnc port-forward failed env=%s session=%s",
                environment_id,
                session_id,
            )
            await websocket.close(code=CLOSE_UPSTREAM)
            return
        log.info("novnc websockify open env=%s session=%s", environment_id, session_id)
        try:
            upstream = await novnc_svc.connect_novnc_upstream(
                url, None, {}, subprotocol
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "novnc upstream connect failed env=%s session=%s err=%s",
                environment_id,
                session_id,
                type(exc).__name__,
            )
            await websocket.close(code=CLOSE_UPSTREAM)
            return
        await _bridge(websocket, upstream)
    finally:
        if upstream is not None:
            with contextlib.suppress(Exception):
                await upstream.close()
        with contextlib.suppress(Exception):
            await websocket.close()
        ctx.cleanup()
