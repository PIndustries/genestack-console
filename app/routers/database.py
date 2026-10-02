"""Copy this console's database between SQLite and Postgres.

Platform admin only. The running engine is not replaced. Restart after.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.config import get_settings
from app.deps import require_admin
from app.schemas import Principal
from app.services import db_move

router = APIRouter(prefix="/api/v1", tags=["database"])
log = logging.getLogger(__name__)


class DatabaseStatus(BaseModel):
    kind: str
    url: str


class DatabaseMoveIn(BaseModel):
    target_url: str = Field(min_length=1)


class DatabaseMoveOut(BaseModel):
    restart_required: bool
    source: str
    target: str
    tables: int
    rows: int


def _platform(principal: Principal) -> None:
    if not principal.platform_admin:
        raise HTTPException(status_code=403, detail="Requires platform admin")


@router.get("/database", response_model=DatabaseStatus)
def database_status(
    principal: Principal = Depends(require_admin),
) -> DatabaseStatus:
    _platform(principal)
    url = get_settings().database_url
    try:
        kind = db_move.database_kind(url)
        masked = db_move.mask_database_url(url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return DatabaseStatus(kind=kind, url=masked)


@router.post("/database/move", response_model=DatabaseMoveOut)
def database_move(
    body: DatabaseMoveIn,
    principal: Principal = Depends(require_admin),
) -> DatabaseMoveOut:
    _platform(principal)
    settings = get_settings()
    try:
        result = db_move.move_database(
            settings.database_url,
            body.target_url,
            settings.config_path,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception:
        log.warning("database move failed")
        raise HTTPException(status_code=400, detail="database move failed") from None
    return DatabaseMoveOut(**result)
