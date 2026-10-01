"""Fleet status board endpoint (read-only)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.config import get_settings
from app.deps import get_db, require_viewer
from app.schemas import Principal
from app.services.fleet import build_fleet

router = APIRouter(prefix="/api/v1", tags=["fleet"])


@router.get("/fleet")
def get_fleet(
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_viewer),
) -> Any:
    """Fleet board: the caller's tenants plus every visible environment's step states."""
    return build_fleet(db, principal, get_settings())
