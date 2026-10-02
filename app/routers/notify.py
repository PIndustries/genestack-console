"""Notification channels stored on this console.

An admin creates and deletes them. A viewer can list the name and kind.
Secret fields are masked and are not returned after save.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.deps import get_db, require_admin, require_viewer
from app.models import AlertRule, NotifyChannel
from app.schemas import Principal
from app.services.job_runner import JobRunner
from app.services.notify import KINDS, mask_config, normalize_config, pack_config, unpack_config

router = APIRouter(prefix="/api/v1/notify", tags=["notify"])


class ChannelIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    kind: str
    config: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class ChannelPatch(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=128)
    enabled: Optional[bool] = None
    config: Optional[dict[str, Any]] = None


class ChannelOut(BaseModel):
    id: str
    name: str
    kind: str
    enabled: bool
    config: dict[str, Any]
    created_at: datetime


def _out(row: NotifyChannel) -> ChannelOut:
    return ChannelOut(
        id=row.id,
        name=row.name,
        kind=row.kind,
        enabled=row.enabled,
        config=mask_config(unpack_config(row.config_encrypted)),
        created_at=row.created_at,
    )


def _clean(kind: str, config: dict[str, Any] | None) -> dict[str, str]:
    if kind not in KINDS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"kind must be one of {', '.join(KINDS)}",
        )
    try:
        return normalize_config(kind, config)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.get("/channels", response_model=list[ChannelOut])
def list_channels(
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_viewer),
) -> list[ChannelOut]:
    del principal
    rows = db.scalars(select(NotifyChannel).order_by(NotifyChannel.created_at)).all()
    return [_out(row) for row in rows]


@router.post("/channels", response_model=ChannelOut, status_code=status.HTTP_201_CREATED)
def create_channel(
    body: ChannelIn,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_admin),
) -> ChannelOut:
    cleaned = _clean(body.kind, body.config)
    row = NotifyChannel(
        name=body.name.strip(),
        kind=body.kind,
        config_encrypted=pack_config(cleaned),
        enabled=body.enabled,
    )
    db.add(row)
    db.flush()
    JobRunner(db).write_audit(
        actor=principal.username,
        action="notify_channel.create",
        resource_type="notify_channel",
        resource_id=row.id,
        details={"name": row.name, "kind": row.kind},
        success=True,
    )
    db.commit()
    db.refresh(row)
    return _out(row)


@router.patch("/channels/{channel_id}", response_model=ChannelOut)
def update_channel(
    channel_id: str,
    body: ChannelPatch,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_admin),
) -> ChannelOut:
    row = db.get(NotifyChannel, channel_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Notification channel not found")
    data = body.model_dump(exclude_unset=True)
    if "name" in data and data["name"] is not None:
        row.name = data["name"].strip()
    if "enabled" in data and data["enabled"] is not None:
        row.enabled = data["enabled"]
    if "config" in data and data["config"] is not None:
        row.config_encrypted = pack_config(_clean(row.kind, data["config"]))
    db.add(row)
    db.flush()
    JobRunner(db).write_audit(
        actor=principal.username,
        action="notify_channel.update",
        resource_type="notify_channel",
        resource_id=row.id,
        details={"fields": list(data.keys())},
        success=True,
    )
    db.commit()
    db.refresh(row)
    return _out(row)


@router.delete("/channels/{channel_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_channel(
    channel_id: str,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_admin),
) -> None:
    row = db.get(NotifyChannel, channel_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Notification channel not found")
    db.execute(
        update(AlertRule)
        .where(AlertRule.channel_id == row.id)
        .values(channel_id=None)
    )
    db.delete(row)
    JobRunner(db).write_audit(
        actor=principal.username,
        action="notify_channel.delete",
        resource_type="notify_channel",
        resource_id=channel_id,
        details={"name": row.name, "kind": row.kind},
        success=True,
    )
    db.commit()
