"""Native console descriptors backed by existing vendor/RFB transports.

No browser assets, BMC credential values or fabricated image codec endpoints.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket
from sqlalchemy.orm import Session

from app.auth import ROLE_RANK, resolve_principal
from app.config import get_settings
from app.db import SessionLocal
from app.deps import check_tenant_access, get_db
from app.models import Environment
from app.routers import ilo_console, novnc
from app.routers.terminal import _resolve_ws_principal
from app.schemas import Principal
from app.services import ilo_console as ilo_service

router = APIRouter(
    prefix="/api/v1/environments/{environment_id}/native/consoles", tags=["native"]
)


def operator_environment(
    environment_id: str,
    db: Annotated[Session, Depends(get_db)],
    principal: Annotated[Principal, Depends(resolve_principal)],
) -> Environment:
    env = db.get(Environment, environment_id)
    if env is None:
        raise HTTPException(404, "Environment not found")
    if ROLE_RANK[principal.role] < ROLE_RANK["operator"]:
        raise HTTPException(403, "Operator role required")
    check_tenant_access(db, principal, env.tenant_id, "operator")
    return env


@router.post("/baremetal/{node_id}")
def baremetal_descriptor(
    node_id: str,
    db: Annotated[Session, Depends(get_db)],
    env: Annotated[Environment, Depends(operator_environment)],
    principal: Annotated[Principal, Depends(resolve_principal)],
) -> dict:
    # The newer shared hub authenticates with the BMC itself. Older direct
    # passthrough requires a BMC key in client frames; refuse instead of leaking it.
    if not callable(getattr(ilo_service, "open_console_session", None)):
        raise HTTPException(409, "Native iLO transport requires the shared KVM hub")
    result = ilo_console.create_ilo_console_session(
        node_id=node_id, db=db, env=env, principal=principal, settings=get_settings()
    )
    if not result.get("ok") or not result.get("session_id"):
        raise HTTPException(502, "BMC console session unavailable")
    session_id = result["session_id"]
    return {
        "schema_version": 1,
        "kind": "ilo-dvc",
        "session_id": session_id,
        "websocket_path": f"/api/v1/environments/{env.id}/native/consoles/ilo/{session_id}",
        "channels": [1, 2],
        "frame_encoding": "hpe-ilo-dvc",
        "renderer_required": "HPE iLO DVC decoder",
        "decoded_images": False,
        "authentication": "single-use-ticket-or-header",
    }


@router.post("/cloud/{server_id}")
def cloud_descriptor(
    server_id: str,
    env: Annotated[Environment, Depends(operator_environment)],
    principal: Annotated[Principal, Depends(resolve_principal)],
) -> dict:
    result = novnc.create_console_session(
        server_id=server_id, env=env, principal=principal, settings=get_settings()
    )
    if not result.get("ok") or not result.get("session_id"):
        raise HTTPException(502, "Nova console session unavailable")
    session_id = result["session_id"]
    return {
        "schema_version": 1,
        "kind": "rfb",
        "session_id": session_id,
        "websocket_path": f"/api/v1/environments/{env.id}/native/consoles/rfb/{session_id}",
        "subprotocols": ["binary"],
        "frame_encoding": "rfb",
        "renderer_required": "RFB client",
        "decoded_images": False,
        "authentication": "single-use-ticket-or-header",
    }


def _socket_allowed(ws: WebSocket, environment_id: str, ticket: str | None) -> bool:
    with SessionLocal() as db:
        principal = _resolve_ws_principal(ticket, ws.headers, db)
        if principal is None or ROLE_RANK[principal.role] < ROLE_RANK["operator"]:
            return False
        env = db.get(Environment, environment_id)
        if env is None:
            return False
        try:
            check_tenant_access(db, principal, env.tenant_id, "operator")
            return True
        except HTTPException:
            return False


@router.websocket("/ilo/{session_id}")
async def native_ilo(
    ws: WebSocket,
    environment_id: str,
    session_id: str,
    ticket: str | None = Query(default=None),
) -> None:
    if not _socket_allowed(ws, environment_id, ticket):
        await ws.close(code=4403)
        return
    if not callable(getattr(ilo_service, "open_console_session", None)):
        await ws.close(code=4409)
        return
    await ilo_console.ilo_kvm_socket(ws, environment_id, session_id)


@router.websocket("/rfb/{session_id}")
async def native_rfb(
    ws: WebSocket,
    environment_id: str,
    session_id: str,
    ticket: str | None = Query(default=None),
) -> None:
    if not _socket_allowed(ws, environment_id, ticket):
        await ws.close(code=4403)
        return
    await novnc.novnc_websockify(ws, environment_id, session_id)
