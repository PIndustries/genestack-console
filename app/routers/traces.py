"""Spans of this process and of modules people add.

The buffer is in memory. A restart clears it. Admin only. No tokens.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.deps import require_admin
from app.schemas import Principal
from app.services import traces

router = APIRouter(prefix="/api/v1", tags=["traces"])


class SpanIn(BaseModel):
    name: str
    duration_ms: float
    status: str


@router.get("/traces")
def list_traces(
    limit: int = Query(default=50, ge=0),
    principal: Principal = Depends(require_admin),
) -> dict:
    del principal
    return {"spans": traces.recent(limit)}


@router.post("/traces")
def post_trace(
    body: SpanIn,
    principal: Principal = Depends(require_admin),
) -> dict:
    del principal
    if len(body.name) > 128 or not body.name.strip():
        raise HTTPException(status_code=400, detail="name must be 1 to 128 characters")
    if body.status not in {"ok", "error"}:
        raise HTTPException(status_code=400, detail="status must be ok or error")
    return traces.record(
        name=body.name,
        duration_ms=float(body.duration_ms),
        status=body.status,
    )
