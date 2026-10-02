"""Agent channel endpoints: enrollment token, status, and the agent WebSocket.

The WS route is unauthenticated in the usual header sense — the per-env
credential token in the query string plus the challenge/proof handshake IS
the auth (the token is env-scoped, so tenant enforcement is inherent).
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.auth import resolve_principal
from app.config import get_settings
from app.db import SessionLocal
from app.deps import get_db, get_env_scoped, require_operator
from app.models import AgentCredential, Environment
from app.schemas import AgentStatusRead, AgentTokenCreate, AgentTokenRead, Principal
from app.services import agents as agents_service
from app.services.agents import AgentRecord
from app.services.job_runner import JobRunner

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["agents"])

# Unauthenticated, no-prefix routes (the agent install script the curl-pipe
# one-liner fetches). Included separately in app.main.
install_router = APIRouter(tags=["agents"])

# Repo checkout layout: genestack-console/agent/install.sh. In the container
# the console image COPYs agent/ next to app/ (/app/agent/install.sh).
_INSTALL_SCRIPT = Path(__file__).resolve().parents[2] / "agent" / "install.sh"
# The curl-pipe case fetches the agent source (main.py + Containerfile) from
# the console itself — the same directory install.sh lives in.
_AGENT_SRC_DIR = _INSTALL_SCRIPT.parent

# WS close code for any authentication/handshake failure (bad token, bad proof).
CLOSE_AUTH_FAILED = 4001


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _request_scheme_host(request: Request) -> tuple[str, str]:
    """(http scheme, host) the client used, honoring reverse-proxy headers."""
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = (
        request.headers.get("x-forwarded-host")
        or request.headers.get("host")
        or request.url.netloc
    )
    return proto, host


def _hub_bases(request: Request) -> tuple[str, str]:
    """(http base, ws base) an agent can reach — hub.advertise_url wins.

    The advertise URL is required for remote hosts: the request-header
    derivation only yields an address the API client can reach, which is not
    necessarily one an agent inside the environment can.
    """
    advertise = (get_settings().hub_advertise_url or "").strip()
    if advertise:
        return agents_service.advertise_bases(advertise)
    proto, host = _request_scheme_host(request)
    ws_scheme = "wss" if proto == "https" else "ws"
    return f"{proto}://{host}", f"{ws_scheme}://{host}"


def _hub_ws_url(request: Request) -> str:
    """External ws(s) URL of the connect endpoint."""
    return f"{_hub_bases(request)[1]}/api/v1/agents/connect"


def _install_one_liner(request: Request, token: str) -> str:
    """Curl-pipe install command served back with a fresh enrollment token."""
    http_base, ws_base = _hub_bases(request)
    return (
        f"curl -fsSL {http_base}/agent | " f"bash -s -- --hub {ws_base} --token {token}"
    )


@install_router.get("/agent", include_in_schema=False)
def agent_install_script() -> FileResponse:
    """Serve agent/install.sh so the console self-hosts the curl-pipe install.

    Unauthenticated and plain text by design — this is a public installer
    script, the same bytes as agent/install.sh in the repo.
    """
    if not _INSTALL_SCRIPT.is_file():
        raise HTTPException(
            status_code=404,
            detail=(
                "Agent install script not packaged at {path}. "
                "The console image must include agent/install.sh; re-build the image or check the build context."
            ).format(path=_INSTALL_SCRIPT),
        )
    return FileResponse(_INSTALL_SCRIPT, media_type="text/plain; charset=utf-8")


@install_router.get("/agent-src/{file_name}", include_in_schema=False)
def agent_src_file(file_name: str) -> FileResponse:
    """Serve the agent source files (main.py, Containerfile) the install
    script fetches in the curl-pipe case — the console self-hosts its own
    agent source, so the one-liner works against a self-hosted deployment.

    Unauthenticated by design: these are the same public repo bytes as
    agent/ in this checkout. Path traversal is impossible — only the exact
    file names below are served, never a user-supplied path component.
    """
    allowed = {
        "main.py": "text/x-python; charset=utf-8",
        "Containerfile": "text/plain; charset=utf-8",
    }
    if file_name not in allowed:
        raise HTTPException(status_code=404, detail="not found")
    path = _AGENT_SRC_DIR / file_name
    if not path.is_file():
        raise HTTPException(
            status_code=404,
            detail=(
                f"Agent source file not packaged at {path}. "
                "The console image must include agent/ (main.py + Containerfile); "
                "re-build the image or set GSC_AGENT_SRC_URL to a reachable mirror."
            ),
        )
    return FileResponse(path, media_type=allowed[file_name])


@router.post(
    "/environments/{environment_id}/agent/token",
    response_model=AgentTokenRead,
    status_code=status.HTTP_201_CREATED,
)
def create_agent_token(
    body: AgentTokenCreate,
    request: Request,
    db: Session = Depends(get_db),
    env: Environment = Depends(get_env_scoped("admin")),
    principal: Principal = Depends(resolve_principal),
) -> AgentTokenRead:
    """Enroll an agent: issue a one-time token (raw shown once, hash stored).

    Credentials are keyed by (env, name): re-creating an existing name
    replaces (revokes) that credential only; new names add credentials so
    several agents can serve one environment (HA).
    """
    try:
        cred, token = agents_service.create_credential(db, env.id, body.name)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    client_config = getattr(cred, "wg_client_config", None)
    wg_address = cred.wg_address
    wg_public = cred.wg_public_key
    JobRunner(db).write_audit(
        actor=principal.username,
        action="env.agent_token.create",
        resource_type="environment",
        resource_id=env.id,
        environment_id=env.id,
        details={"agent_id": cred.id, "name": cred.name},
        success=True,
    )
    db.commit()
    db.refresh(cred)

    hub_url = _hub_ws_url(request)
    instructions = _install_one_liner(request, token)
    docker_run = (
        "docker run -d --name gsc-agent --restart unless-stopped "
        f"-e GSC_HUB_URL={hub_url} -e GSC_AGENT_TOKEN={token} gsc-agent:local"
    )
    wireguard = None
    if client_config:
        wireguard = {
            "address": wg_address,
            "public_key": wg_public,
            "client_config": client_config,
        }
    return AgentTokenRead(
        agent_id=cred.id,
        environment_id=env.id,
        name=cred.name,
        token=token,
        hub_url=hub_url,
        instructions=instructions,
        docker_run=docker_run,
        created_at=cred.created_at,
        wireguard=wireguard,
    )


@router.get(
    "/environments/{environment_id}/agent/status",
    response_model=AgentStatusRead,
)
def get_agent_status(
    db: Session = Depends(get_db),
    env: Environment = Depends(get_env_scoped("viewer")),
) -> dict[str, Any]:
    return agents_service.status_for_env(db, env.id)


@router.get("/environments/{environment_id}/agents")
def list_agents(
    db: Session = Depends(get_db),
    env: Environment = Depends(get_env_scoped("viewer")),
) -> list[dict]:
    """List all enrolled agents for this environment with their PXE config."""
    from sqlalchemy import select

    creds = db.scalars(
        select(AgentCredential)
        .where(AgentCredential.environment_id == env.id)
        .order_by(AgentCredential.created_at)
    ).all()
    registry = agents_service.registry
    entries = []
    for cred in creds:
        record = registry.get(cred.id) if registry else None
        entries.append(
            {
                "agent_id": cred.id,
                "name": cred.name,
                "hostname": cred.hostname,
                "version": cred.version,
                "connected": record and record.online() if record else False,
                "last_seen": cred.last_seen.isoformat() if cred.last_seen else None,
                "created_at": cred.created_at.isoformat() if cred.created_at else None,
                "pxe_config": cred.pxe_config,
            }
        )
    return entries


@router.patch("/environments/{environment_id}/agent/pxe-config")
def update_agent_pxe_config(
    body: dict[str, Any],
    db: Session = Depends(get_db),
    env: Environment = Depends(get_env_scoped("operator")),
    principal: Principal = Depends(require_operator),
) -> dict:
    """Set or clear PXE config on an agent credential.
    body: {agent_id, pxe_config} — pxe_config is a dict or null to clear.
    """
    from sqlalchemy import select

    agent_id = str(body.get("agent_id") or "").strip()
    pxe_config = body.get("pxe_config")
    if not agent_id:
        raise HTTPException(
            status_code=400,
            detail="agent_id is required. Provide {agent_id: '<uuid>', pxe_config: {...}} in the request body.",
        )
    if pxe_config is not None and not isinstance(pxe_config, dict):
        raise HTTPException(
            status_code=400,
            detail="pxe_config must be a JSON object or null (to clear). Example: {agent_id: '...', pxe_config: {...}}.",
        )
    cred = db.scalar(
        select(AgentCredential).where(
            AgentCredential.environment_id == env.id, AgentCredential.id == agent_id
        )
    )
    if not cred:
        raise HTTPException(
            status_code=404,
            detail=f"Agent credential '{agent_id}' not found in environment '{env.id}'. Verify the agent_id and create a new token if needed.",
        )
    cred.pxe_config = pxe_config
    db.commit()
    JobRunner(db).write_audit(
        actor=principal.username,
        action="env.agent.pxe_config",
        resource_type="agent_credential",
        resource_id=cred.id,
        environment_id=env.id,
        details={"name": cred.name, "pxe_config": pxe_config},
        success=True,
    )
    db.commit()
    return {
        "agent_id": cred.id,
        "name": cred.name,
        "pxe_config": cred.pxe_config,
    }


def _touch_credential(agent_id: str, **fields: Any) -> None:
    """Mirror live metadata (last_seen/hostname/version) onto the DB row."""
    db = SessionLocal()
    try:
        cred = db.get(AgentCredential, agent_id)
        if cred is None:
            return
        for key, value in fields.items():
            setattr(cred, key, value)
        db.commit()
    finally:
        db.close()


async def _fail(ws: WebSocket, reason: str) -> None:
    log.warning("agent connect rejected: %s", reason)
    await ws.close(code=CLOSE_AUTH_FAILED)


@router.websocket("/agents/connect")
async def agent_connect(ws: WebSocket, token: str = Query(default="")) -> None:
    await ws.accept()

    token = token.strip()
    cred: AgentCredential | None = None
    if token:
        db = SessionLocal()
        try:
            cred = agents_service.credential_by_token(db, token)
            if cred is not None:
                # Detach what we need before the session closes.
                db.expunge(cred)
        finally:
            db.close()
    if cred is None:
        await _fail(ws, "unknown or missing token")
        return

    # Challenge/proof: the agent proves HMAC-SHA256(raw token, nonce). The raw
    # token only exists here, transiently, from the connect query string.
    nonce = secrets.token_urlsafe(24)
    await ws.send_json({"type": "challenge", "nonce": nonce})
    try:
        frame = await asyncio.wait_for(
            ws.receive_json(), timeout=agents_service.HANDSHAKE_TIMEOUT_SECONDS
        )
    except Exception:  # noqa: BLE001 — timeout, disconnect, garbage JSON
        await _fail(ws, "no proof frame received")
        return
    proof = str(frame.get("hmac") or "") if isinstance(frame, dict) else ""
    if (
        not isinstance(frame, dict)
        or frame.get("type") != "proof"
        or not agents_service.verify_proof(token, nonce, proof)
    ):
        await _fail(ws, "bad proof")
        return

    agent_id, env_id = cred.id, cred.environment_id
    await ws.send_json({"type": "welcome", "agent_id": agent_id})

    record = AgentRecord(
        agent_id=agent_id,
        env_id=env_id,
        ws=ws,
        loop=asyncio.get_running_loop(),
    )
    agents_service.registry.register(record)
    _touch_credential(agent_id, last_seen=_utcnow())
    log.info("agent %s connected for env %s", agent_id, env_id)

    try:
        while True:
            frame = await ws.receive_json()
            record.last_frame_at = time.monotonic()
            if not isinstance(frame, dict):
                continue
            ftype = frame.get("type")
            if ftype == "heartbeat":
                _touch_credential(agent_id, last_seen=_utcnow())
            elif ftype == "hello":
                record.hostname = str(frame.get("hostname") or "") or None
                record.version = str(frame.get("version") or "") or None
                caps = frame.get("caps")
                record.caps = [str(c) for c in caps] if isinstance(caps, list) else []
                _touch_credential(
                    agent_id,
                    last_seen=_utcnow(),
                    hostname=record.hostname,
                    version=record.version,
                )
            elif ftype in ("log", "result"):
                agents_service.registry.route_frame(record, frame)
            elif ftype == "event":
                # Discovery sightings (pxe_request / bmc_found) — validated and
                # upserted into the env's discovery inbox; bad frames ignored.
                agents_service.handle_agent_event(env_id, frame)
            elif ftype == "bye":
                log.info("agent %s said bye: %s", agent_id, frame.get("reason"))
                break
            # Unknown frame types are ignored.
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001 — never let a bad agent crash the app
        log.warning("agent %s connection error", agent_id, exc_info=True)
    finally:
        agents_service.registry.unregister(record)
        _touch_credential(agent_id, last_seen=_utcnow())
        log.info("agent %s disconnected", agent_id)
