"""PXE status and controls — in-process DHCP + boot HTTP owned by the Console."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.config import get_settings
from app.deps import get_db, get_env_scoped
from app.models import Environment
from app.services import envconfig as envconfig_service
from app.services import pxe as pxe_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/environments/{environment_id}", tags=["pxe"])


@router.get("/pxe")
def get_pxe_status(
    db: Session = Depends(get_db),
    env: Environment = Depends(get_env_scoped("viewer")),
) -> dict[str, Any]:
    """Full PXE status snapshot: env config, assets, in-process runtime, leases."""
    try:
        settings = get_settings()
        current = envconfig_service.get_current(db, env)
        doc = current[0] if current else {}
        return pxe_service.render_env_pxe_status(env, db, doc, settings)
    except Exception as exc:
        logger.error("pxe status error: %s", exc)
        return {
            "enabled": False,
            "flat_config": None,
            "agent_configs": [],
            "error": str(exc),
        }


@router.post("/pxe/prep")
def pxe_prep(
    db: Session = Depends(get_db),
    env: Environment = Depends(get_env_scoped("operator")),
) -> dict[str, Any]:
    """Download Talos assets, render boot files, and reload in-process PXE."""
    try:
        settings = get_settings()
        current = envconfig_service.get_current(db, env)
        doc = current[0] if current else {}

        def _log(msg: str) -> None:
            logger.info("[pxe-prep] %s", msg)

        return pxe_service.ensure_assets_and_config(env, doc, settings, _log)
    except Exception as exc:
        logger.error("pxe prep error: %s", exc)
        return {"ok": False, "error": str(exc)}


@router.post("/pxe/render")
def pxe_render(
    db: Session = Depends(get_db),
    env: Environment = Depends(get_env_scoped("operator")),
) -> dict[str, Any]:
    """Re-render dnsmasq.conf and boot.ipxe from the current env config document."""
    try:
        settings = get_settings()
        current = envconfig_service.get_current(db, env)
        doc = current[0] if current else {}

        def _log(msg: str) -> None:
            logger.info("[pxe-render] %s", msg)

        return pxe_service.ensure_assets_and_config(env, doc, settings, _log)
    except Exception as exc:
        logger.error("pxe render error: %s", exc)
        return {"ok": False, "error": str(exc)}


@router.post("/pxe/agent")
def agent_pxe_prep(
    body: dict[str, Any],
    db: Session = Depends(get_db),
    env: Environment = Depends(get_env_scoped("operator")),
) -> dict:
    """Prep PXE for a specific agent: fetch assets, render configs under pxe/{agent_id}/.
    body: {agent_id}
    """
    agent_id = str(body.get("agent_id") or "").strip()
    if not agent_id:
        raise HTTPException(status_code=400, detail="agent_id is required")
    from app.models import AgentCredential
    from sqlalchemy import select

    settings = get_settings()
    cred = db.scalar(
        select(AgentCredential).where(
            AgentCredential.environment_id == env.id, AgentCredential.id == agent_id
        )
    )
    if not cred:
        raise HTTPException(status_code=404, detail=f"agent {agent_id} not found")
    current = envconfig_service.get_current(db, env)
    doc = current[0] if current else {}
    log_fn = lambda msg: None  # noqa: E731
    result = pxe_service.ensure_assets_and_config(
        env,
        doc,
        settings,
        log_fn,
        agent_id=agent_id,
        agent_pxe_config=cred.pxe_config,
    )
    return result


@router.post("/pxe/restart")
def pxe_restart(
    db: Session = Depends(get_db),
    env: Environment = Depends(get_env_scoped("operator")),
) -> dict[str, Any]:
    """Reload the in-process PXE runtime from the current env config."""
    try:
        settings = get_settings()
        current = envconfig_service.get_current(db, env)
        doc = current[0] if current else {}
        result = pxe_service.ensure_assets_and_config(env, doc, settings)
        return {
            "ok": bool(result.get("ok")),
            "runtime": result.get("runtime"),
            "error": result.get("error"),
        }
    except Exception as exc:
        logger.error("pxe restart error: %s", exc)
        return {"ok": False, "error": str(exc)}
