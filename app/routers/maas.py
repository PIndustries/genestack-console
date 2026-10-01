"""MAAS proxy / convenience endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.config import get_settings
from app.deps import check_tenant_access, get_db, require_viewer
from app.models import Environment
from app.schemas import Principal
from app.services.crypto import decrypt_secret
from app.services.maas import MaasClient, MaasError

router = APIRouter(prefix="/api/v1/maas", tags=["maas"])


def _client_for_environment(
    db: Session,
    environment_id: str | None,
    principal: Principal,
) -> MaasClient:
    settings = get_settings()
    maas_url = settings.maas_url
    maas_key = settings.maas_api_key

    if environment_id:
        env = db.get(Environment, environment_id)
        if not env:
            raise HTTPException(status_code=404, detail="Environment not found")
        check_tenant_access(db, principal, env.tenant_id, "viewer")
        maas_url = env.maas_url or maas_url
        maas_key = decrypt_secret(env.maas_api_key_encrypted) or maas_key

    return MaasClient.from_settings(
        {
            "maas_url": maas_url or "",
            "maas_api_key": maas_key or "",
            # Env-specific URL always wins; the dev-only mock flag applies
            # only when no MAAS is configured anywhere.
            "maas_mock": bool(getattr(settings, "maas_mock", False)) and not maas_url,
        }
    )


@router.get("/machines")
def list_maas_machines(
    environment_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_viewer),
) -> dict[str, Any]:
    client = _client_for_environment(db, environment_id, principal)
    try:
        machines = client.list_machines()
        return {
            "ok": True,
            "dry_run": client.mock,
            "mock": client.mock,
            "maas_configured": client.configured,
            "machines": machines,
            "count": len(machines),
        }
    except MaasError as exc:
        raise HTTPException(
            status_code=exc.status_code or 502, detail=str(exc)
        ) from exc
    finally:
        client.close()


@router.get("/machines/{system_id}/power")
def get_maas_machine_power(
    system_id: str,
    environment_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_viewer),
) -> dict[str, Any]:
    client = _client_for_environment(db, environment_id, principal)
    try:
        machine = client.get_machine(system_id)
        return {
            "system_id": system_id,
            "power_state": machine.get("power_state", "unknown"),
        }
    except MaasError as exc:
        raise HTTPException(
            status_code=exc.status_code or 502, detail=str(exc)
        ) from exc
    finally:
        client.close()
