"""Guided lifecycle workflow endpoint (read-only)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.config import get_settings
from app.deps import get_db, get_env_scoped
from app.models import Environment
from app.services.workflow import build_workflow

router = APIRouter(prefix="/api/v1/environments", tags=["environments"])


@router.get("/{environment_id}/workflow")
def get_environment_workflow(
    env: Environment = Depends(get_env_scoped("viewer")),
    db: Session = Depends(get_db),
) -> Any:
    """Lifecycle stepper view: connect -> inventory -> config -> push -> deploy -> operate."""
    return build_workflow(db, env, get_settings())
