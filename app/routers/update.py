"""Version channel: recommend and optionally apply a compiled Console update."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.config import get_settings
from app.deps import require_admin, require_viewer
from app.schemas import Principal
from app.services import updatecheck

router = APIRouter(prefix="/api/v1/update", tags=["update"])


@router.get("")
def update_status(_principal: Principal = Depends(require_viewer)) -> dict:
    return updatecheck.status(get_settings())


@router.get("/feed")
def update_feed(_principal: Principal = Depends(require_viewer)) -> dict:
    """Release notes and the public pipeline. A viewer can read both."""
    return updatecheck.feed(get_settings())


@router.post("/apply")
def update_apply(_principal: Principal = Depends(require_admin)) -> dict:
    return updatecheck.apply_binary(get_settings())
