"""Read-only environment descriptor endpoint (Phase 3)."""

from __future__ import annotations

from typing import Any

import yaml
from fastapi import APIRouter, Depends, Query
from fastapi.responses import PlainTextResponse

from app.config import get_settings
from app.deps import get_env_scoped
from app.models import Environment
from app.services.descriptor import build_descriptor

router = APIRouter(prefix="/api/v1/environments", tags=["environments"])


@router.get("/{environment_id}/descriptor")
def get_environment_descriptor(
    format: str | None = Query(default=None),
    env: Environment = Depends(get_env_scoped("viewer")),
) -> Any:
    """Full descriptor of one environment; ``?format=yaml`` for YAML output."""
    descriptor = build_descriptor(env, get_settings())
    if format == "yaml":
        return PlainTextResponse(yaml.safe_dump(descriptor, sort_keys=False))
    return descriptor
