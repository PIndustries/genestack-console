"""Version channel: recommend and optionally apply a compiled Console update."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.config import get_settings
from app.deps import require_admin, require_viewer
from app.schemas import Principal
from app.services import bootc, updatecheck

router = APIRouter(prefix="/api/v1/update", tags=["update"])


class ApplyBody(BaseModel):
    version: str | None = None


class BootcBody(BaseModel):
    action: str


@router.get("")
def update_status(_principal: Principal = Depends(require_viewer)) -> dict:
    return updatecheck.status(get_settings())


@router.get("/feed")
def update_feed(_principal: Principal = Depends(require_viewer)) -> dict:
    """Release notes and the public pipeline. A viewer can read both."""
    return updatecheck.feed(get_settings())


@router.get("/host")
def update_host(_principal: Principal = Depends(require_viewer)) -> dict:
    """Booted and previous images when this machine is a bootc appliance."""
    return bootc.host_view()


@router.post("/apply")
def update_apply(
    body: ApplyBody | None = None,
    _principal: Principal = Depends(require_admin),
) -> dict:
    version = (body.version or "").strip() if body else ""
    if version:
        return updatecheck.apply_version(get_settings(), version)
    return updatecheck.apply_binary(get_settings())


@router.post("/bootc")
def update_bootc(
    body: BootcBody,
    _principal: Principal = Depends(require_admin),
) -> dict:
    return bootc.apply_action(body.action)
