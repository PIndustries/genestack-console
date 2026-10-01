"""In-portal iLO 4 HTML5 remote console: session mint, asset proxy, KVM WebSocket.

GET/WS under ``/baremetal/console/{session_id}/`` are unauthenticated besides
the unguessable session id (iframe <script> cannot send X-API-Key).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import ssl
from typing import Any

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.auth import resolve_principal
from app.config import Settings, get_settings
from app.db import get_db
from app.deps import get_env_scoped
from app.models import Environment
from app.schemas import Principal
from app.services import baremetal as baremetal_service
from app.services import ilo_console as ilo_svc

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["ilo-console"])

_operator_env = get_env_scoped("operator")

CLOSE_AUTH_FAILED = 4401
CLOSE_UPSTREAM = 1011

_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "SAMEORIGIN",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' 'unsafe-eval' blob:; "
        "worker-src 'self' blob: 'unsafe-inline' 'unsafe-eval'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob:; "
        "font-src 'self' data:; "
        "connect-src 'self' ws: wss:; "
        "frame-ancestors 'self'"
    ),
}


def _html_error() -> Response:
    return Response(
        content=ilo_svc.UNAVAILABLE_HTML,
        media_type="text/html",
        headers=_HEADERS,
    )


def _session_or_404(session_id: str, environment_id: str) -> ilo_svc.IloSession:
    session = ilo_svc.get_session(session_id, environment_id)
    if session is None:
        raise HTTPException(status_code=404, detail="console session not found")
    return session


@router.post("/environments/{environment_id}/baremetal/nodes/{node_id}/console/session")
def create_ilo_console_session(
    node_id: str,
    db: Session = Depends(get_db),
    env: Environment = Depends(_operator_env),
    principal: Principal = Depends(resolve_principal),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Mint a short-lived iLO HTML5 console session; operator+. Never 500."""
    node_id = (node_id or "").strip()
    if not ilo_svc.NODE_ID_RE.match(node_id):
        raise HTTPException(status_code=400, detail="invalid node_id")
    node = baremetal_service.get_node(db, env, node_id)
    if node is None:
        return {
            "ok": False,
            "node_id": node_id,
            "session_id": None,
            "embed_url": None,
            "error": "unknown node",
        }
    try:
        session_key = ilo_svc.login_node(node, settings)
        session_id = ilo_svc.create_session(
            env_id=env.id,
            node_id=node.id,
            node_name=node.name,
            bmc_host=node.bmc_host,
            session_key=session_key,
            username=principal.username,
        )
    except ilo_svc.IloConsoleError as exc:
        log.warning(
            "ilo console login failed env=%s node=%s: %s", env.id, node.name, exc
        )
        return {
            "ok": False,
            "node_id": node_id,
            "session_id": None,
            "embed_url": None,
            "error": str(exc),
        }
    except Exception:  # noqa: BLE001
        log.warning("ilo console session failed env=%s node=%s", env.id, node.name)
        return {
            "ok": False,
            "node_id": node_id,
            "session_id": None,
            "embed_url": None,
            "error": "failed to open iLO console session",
        }
    prefix = ilo_svc.session_prefix(env.id, session_id)
    embed_url = f"{prefix}/irc.html?token={session_key}"
    log.info(
        "ilo console session created env=%s node=%s session=%s user=%s",
        env.id,
        node.name,
        session_id,
        principal.username,
    )
    return {
        "ok": True,
        "node_id": node_id,
        "node_name": node.name,
        "session_id": session_id,
        "embed_url": embed_url,
        "error": None,
    }


def _proxy_asset(
    environment_id: str,
    session_id: str,
    path: str,
    *,
    method: str = "GET",
    body: bytes | None = None,
    extra_headers: dict[str, str] | None = None,
) -> Response:
    if not ilo_svc.SESSION_ID_RE.match(session_id or ""):
        raise HTTPException(status_code=404, detail="console session not found")
    rel = (path or "irc.html").strip()
    if rel.endswith("/"):
        rel = rel.rstrip("/") or "irc.html"
    if rel == "wss/ircport":
        raise HTTPException(status_code=404, detail="not found")
    if not ilo_svc.is_safe_console_path(rel):
        raise HTTPException(status_code=400, detail="invalid path")
    session = _session_or_404(session_id, environment_id)
    prefix = ilo_svc.session_prefix(environment_id, session_id)
    html = rel.endswith(".html") or rel in ("irc.html", "")
    try:
        status, body, ctype = ilo_svc.fetch_ilo_asset(
            ilo_svc.bmc_origin(session.bmc_host),
            rel,
            session.session_key,
            method=method,
            body=body,
            extra_headers=extra_headers,
        )
    except ilo_svc.IloConsoleError:
        log.warning("ilo asset proxy failed env=%s path=%s", environment_id, rel)
        if html:
            return _html_error()
        raise HTTPException(
            status_code=502, detail="iLO console proxy could not reach BMC"
        ) from None
    if html and status != 200:
        return _html_error()
    if status == 404:
        raise HTTPException(status_code=404, detail="not found")
    if method.upper() in ("GET", "HEAD"):
        body = ilo_svc.apply_rewrites(rel, body, prefix)
    headers = dict(_HEADERS)
    return Response(content=body, media_type=ctype, status_code=status, headers=headers)


@router.get("/environments/{environment_id}/baremetal/console/{session_id}")
def ilo_asset_root(environment_id: str, session_id: str) -> Response:
    return _proxy_asset(environment_id, session_id, "irc.html")


def _forward_headers(request: Request) -> dict[str, str]:
    extra: dict[str, str] = {}
    for name in ("content-type", "x-auth-token", "x-client-type", "accept"):
        value = request.headers.get(name)
        if value:
            extra[name] = value
    return extra


@router.api_route(
    "/environments/{environment_id}/baremetal/console/{session_id}/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
)
async def ilo_asset(
    environment_id: str,
    session_id: str,
    path: str,
    request: Request,
) -> Response:
    raw = b""
    if request.method not in ("GET", "HEAD"):
        raw = await request.body()
    return _proxy_asset(
        environment_id,
        session_id,
        path,
        method=request.method,
        body=raw,
        extra_headers=_forward_headers(request),
    )


async def _bridge(browser: WebSocket, upstream: Any) -> None:
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
        asyncio.create_task(to_upstream(), name="ilo-up"),
        asyncio.create_task(to_browser(), name="ilo-down"),
    ]
    _done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    for task in pending:
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def _connect_ilo_ws(bmc_host: str, session_key: str) -> Any:
    import websockets

    origin = ilo_svc.bmc_origin(bmc_host)
    url = origin.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
    url = url.rstrip("/") + "/wss/ircport"
    ctx = ssl._create_unverified_context()
    headers = {"Cookie": f"sessionKey={session_key}"}
    kwargs: dict[str, Any] = {
        "ssl": ctx if url.startswith("wss://") else None,
        "max_size": None,
        "open_timeout": 15,
        "proxy": None,
    }
    try:
        return await websockets.connect(url, additional_headers=headers, **kwargs)
    except TypeError:
        kwargs.pop("proxy", None)
        return await websockets.connect(url, extra_headers=headers, **kwargs)


@router.websocket(
    "/environments/{environment_id}/baremetal/console/{session_id}/wss/ircport"
)
async def ilo_kvm_socket(
    websocket: WebSocket,
    environment_id: str,
    session_id: str,
) -> None:
    session = ilo_svc.get_session(session_id, environment_id)
    if session is None:
        await websocket.close(code=CLOSE_AUTH_FAILED)
        return
    await websocket.accept()
    upstream = None
    try:
        try:
            upstream = await _connect_ilo_ws(session.bmc_host, session.session_key)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "ilo kvm upstream failed env=%s session=%s err=%s",
                environment_id,
                session_id,
                type(exc).__name__,
            )
            await websocket.close(code=CLOSE_UPSTREAM)
            return
        log.info("ilo kvm open env=%s node=%s", environment_id, session.node_name)
        await _bridge(websocket, upstream)
    finally:
        if upstream is not None:
            with contextlib.suppress(Exception):
                await upstream.close()
        with contextlib.suppress(Exception):
            await websocket.close()
