"""Inbound GitHub webhooks for Apps (HMAC only — no API key)."""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models import App
from app.services import apps as apps_svc
from app.services.crypto import decrypt_secret
from app.services.job_runner import ConflictError, execute_operation

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["app-hooks"])


def _db() -> Session:
    return SessionLocal()


@router.post("/hooks/apps/{webhook_id}")
async def github_app_webhook(
    webhook_id: str,
    request: Request,
    x_hub_signature_256: str | None = Header(default=None),
    x_github_event: str | None = Header(default=None),
) -> dict[str, Any]:
    if not apps_svc.WEBHOOK_ID_RE.match(webhook_id or ""):
        raise HTTPException(status_code=404, detail="unknown webhook")
    body = await request.body()
    db = _db()
    try:
        row = db.scalar(select(App).where(App.webhook_id == webhook_id))
        if row is None:
            raise HTTPException(status_code=404, detail="unknown webhook")
        secret = decrypt_secret(row.webhook_secret_encrypted) or ""
        if not apps_svc.verify_github_signature(secret, body, x_hub_signature_256):
            raise HTTPException(status_code=401, detail="invalid signature")
        event = (x_github_event or "").strip().lower()
        if event == "ping":
            return {"ok": True, "event": "ping"}
        if event and event != "push":
            return {"ok": True, "ignored": event}
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="invalid json") from None
        ref = str(payload.get("ref") or "")
        expected = f"refs/heads/{row.branch}"
        if ref and ref != expected:
            return {"ok": True, "ignored": "other-branch", "ref": ref}
        try:
            job = execute_operation(
                db,
                operation="app.deploy",
                params={"app_id": row.id, "force": False},
                environment_id=row.environment_id,
                created_by="github-webhook",
            )
        except ConflictError as exc:
            return {
                "ok": False,
                "queued": False,
                "error": "deploy already running",
                "conflicting_job_id": exc.job_id,
            }
        row.last_job_id = job.id
        row.last_status = job.status.value
        db.commit()
        return {"ok": True, "queued": True, "job_id": job.id}
    finally:
        db.close()
