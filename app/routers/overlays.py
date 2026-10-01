"""Expert-tab overlay YAML: view and save helm/kustomize/gateway files."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.deps import get_db, get_env_scoped, require_operator
from app.models import Environment
from app.schemas import Principal
from app.services import overlays
from app.services.job_runner import JobRunner

router = APIRouter(prefix="/api/v1", tags=["overlays"])


class OverlayPut(BaseModel):
    path: str = Field(min_length=3, max_length=240)
    content: str = Field(max_length=overlays.MAX_BYTES)


def _http(exc: Exception) -> HTTPException:
    if isinstance(exc, overlays.OverlayNotFound):
        return HTTPException(status_code=404, detail="overlay file not found")
    if isinstance(exc, overlays.OverlayError):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=500, detail="overlay failed")


@router.get("/environments/{environment_id}/overlays")
def get_overlay(
    path: str = Query(..., min_length=3, max_length=240),
    env: Environment = Depends(get_env_scoped("operator")),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    try:
        return overlays.read_overlay(env, path, settings)
    except (overlays.OverlayError, overlays.OverlayNotFound) as exc:
        raise _http(exc) from exc


@router.put("/environments/{environment_id}/overlays")
def put_overlay(
    body: OverlayPut,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_operator),
    env: Environment = Depends(get_env_scoped("operator")),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    try:
        result = overlays.write_overlay(env, body.path, body.content, settings)
    except (overlays.OverlayError, overlays.OverlayNotFound) as exc:
        raise _http(exc) from exc
    git = result.get("git")
    if not isinstance(git, dict):
        # Git dump is best-effort; never 500 the local helm-configs write.
        result["git"] = {
            "attempted": False,
            "committed": False,
            "pushed": False,
            "sha": None,
            "path": None,
            "error": None,
        }
    JobRunner(db).write_audit(
        actor=principal.username,
        action="env.overlay.put",
        resource_type="environment",
        resource_id=env.id,
        environment_id=env.id,
        details={"path": result.get("path"), "bytes": result.get("bytes")},
    )
    db.commit()
    result["saved_by"] = principal.username
    return result
