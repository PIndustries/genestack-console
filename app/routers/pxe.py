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
from app.services.request_safety import client_detail, log_field

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
        logger.error("pxe status error: %s", log_field(exc))
        return {
            "enabled": False,
            "flat_config": None,
            "agent_configs": [],
            "error": client_detail(exc),
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
            logger.info("[pxe-prep] %s", log_field(msg))

        return pxe_service.ensure_assets_and_config(env, doc, settings, _log)
    except Exception as exc:
        logger.error("pxe prep error: %s", log_field(exc))
        return {"ok": False, "error": client_detail(exc)}


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
            logger.info("[pxe-render] %s", log_field(msg))

        return pxe_service.ensure_assets_and_config(env, doc, settings, _log)
    except Exception as exc:
        logger.error("pxe render error: %s", log_field(exc))
        return {"ok": False, "error": client_detail(exc)}


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
    from sqlalchemy import select

    from app.models import AgentCredential

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


@router.post("/pxe/runtime/reload")
def reload_existing_runtime(
    db: Session = Depends(get_db),
    env: Environment = Depends(get_env_scoped("operator")),
) -> dict[str, Any]:
    """Apply only this API process's runtime; never rewrite pending boot files.

    Workers use this when the API already owns the provisioning sockets. This
    direct apply deliberately cannot delegate, so a foreign port owner fails
    once rather than recursively calling back into the API.
    """
    from app.services.baremetal import boot_profiles
    from app.services.pxe_owner import ready
    from app.services.pxe_paths import provisioning_root
    from app.services.pxe_runtime import apply_pxe_cfg

    settings = get_settings()
    current = envconfig_service.get_current(db, env)
    pxe_cfg = current[0].get("pxe") if current else None
    if not isinstance(pxe_cfg, dict) or not pxe_cfg:
        raise HTTPException(status_code=409, detail="Save the provisioning network first")
    runtime = apply_pxe_cfg(pxe_cfg, boot_profiles(db, env), provisioning_root(settings.data_dir))
    return {"environment_id": env.id, "ok": ready(runtime), "runtime": runtime}


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
        logger.error("pxe restart error: %s", log_field(exc))
        return {"ok": False, "error": client_detail(exc)}
