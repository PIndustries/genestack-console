"""Server-sent events stream over the in-process event bus."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator, Callable

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import ROLE_RANK
from app.config import get_settings
from app.db import SessionLocal
from app.deps import check_tenant_access, get_db, principal_from_token
from app.models import Environment, Membership
from app.schemas import Principal
from app.services import events, tickets

router = APIRouter(prefix="/api/v1", tags=["stream"])

# Topics any authenticated viewer+ may subscribe to; env:{id} is gated per env.
# "environments" is create/update/delete of an environment row. The sidebar,
# the environment selectors, and the fleet board all read that one topic.
_KNOWN_TOPICS = {"fleet", "environments", "alerts", "jobs", "metrics"}
# Shared (non env-scoped) topics whose payloads carry an environment_id and
# must be filtered per tenant at send time.
_SHARED_TOPICS = {"fleet", "environments", "alerts", "jobs", "metrics"}
_HEARTBEAT_SECONDS = 15
# How often the visible-environment set is reloaded mid-stream so membership
# changes eventually apply without reconnecting.
_VISIBILITY_REFRESH_SECONDS = 60


def _stream_principal(
    request: Request,
    db: Session = Depends(get_db),
    ticket: str | None = Query(default=None),
) -> Principal:
    """Authenticate an SSE client.

    EventSource cannot set headers, so the browser first exchanges its
    credential for a single-use ticket (``POST /api/v1/auth/ticket``) and
    passes ``?ticket=`` — a raw ``?token=`` is no longer accepted because
    query strings land in access logs. Header credentials (X-API-Key /
    Authorization: Bearer) still work for non-browser clients.
    """
    if ticket:
        principal = tickets.consume_ticket(ticket)
        if principal is not None:
            return principal
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid, expired, or already-used ticket",
            headers={"WWW-Authenticate": "Bearer"},
        )
    raw: str | None = None
    api_key = request.headers.get("X-API-Key")
    if api_key:
        raw = api_key
    else:
        authz = request.headers.get("Authorization", "")
        if authz.lower().startswith("bearer "):
            raw = authz[7:].strip() or None
    if raw is None:
        if get_settings().dev_auto_login:
            # DEV ONLY — mirrors app.auth.resolve_principal.
            return Principal(
                username="dev-auto-login",
                role="admin",
                auth_method="dev_auto_login",
                platform_admin=True,
            )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=(
                "Missing credentials. Provide X-API-Key, Authorization: Bearer <key>, "
                "or a single-use ?ticket= from POST /api/v1/auth/ticket."
            ),
            headers={"WWW-Authenticate": "Bearer"},
        )
    return principal_from_token(raw, db)


def _authorize_topics(db: Session, principal: Principal, topics: list[str]) -> None:
    """400 on unknown topics; 403 on env:{id} topics outside the principal's scope."""
    if ROLE_RANK[principal.role] < ROLE_RANK["viewer"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Role '{principal.role}' insufficient; requires 'viewer' or higher",
        )
    for topic in topics:
        if topic in _KNOWN_TOPICS:
            continue
        if topic.startswith("env:") and topic[4:]:
            env = db.get(Environment, topic[4:])
            if env is None:
                raise HTTPException(status_code=400, detail=f"Unknown topic: {topic}")
            check_tenant_access(db, principal, env.tenant_id, "viewer")
            continue
        raise HTTPException(status_code=400, detail=f"Unknown topic: {topic}")


def _visible_env_ids(db: Session, principal: Principal) -> set[str] | None:
    """Environment ids the principal may see; None means unrestricted.

    Platform admins (incl. static API keys) see everything. Session users see
    environments in tenants they hold any membership in — the same scoping
    /fleet/live uses. Tenant-less environments are platform-admin only.
    """
    if principal.platform_admin:
        return None
    member_tenants = select(Membership.tenant_id).where(
        Membership.user_id == principal.user_id
    )
    return set(
        db.scalars(
            select(Environment.id).where(Environment.tenant_id.in_(member_tenants))
        ).all()
    )


def _visible_tenant_ids(db: Session, principal: Principal) -> set[str] | None:
    """Tenant ids the principal belongs to; None means unrestricted."""
    if principal.platform_admin:
        return None
    return set(
        db.scalars(
            select(Membership.tenant_id).where(Membership.user_id == principal.user_id)
        ).all()
    )


def _payload_visible(
    topic: str,
    payload: dict,
    visible: set[str] | None,
    visible_tenants: set[str] | None = None,
) -> bool:
    """Per-tenant send-time filter for shared topics.

    Payloads without an environment_id pass through; env:{id} topics are
    re-filtered after membership refresh, including revoked memberships.

    Environment create and delete are matched on tenant_id when that set is
    known. The environment-id snapshot lags a new row and drops a deleted
    one, so an id check would swallow the lifecycle event.
    """
    if visible is None:
        return True
    if topic.startswith("env:"):
        return topic[4:] in visible
    if topic == "environments":
        if not isinstance(payload, dict):
            return False
        if visible_tenants is not None:
            tenant_id = payload.get("tenant_id")
            return bool(tenant_id) and tenant_id in visible_tenants
        env_id = payload.get("environment_id")
        return env_id is None or env_id in visible
    if topic not in _SHARED_TOPICS:
        return True
    env_id = payload.get("environment_id") if isinstance(payload, dict) else None
    return env_id is None or env_id in visible


async def _event_stream(
    request: Request,
    queue: asyncio.Queue,
    visible_env_ids: set[str] | None = None,
    refresh_visible: Callable[[], set[str] | None] | None = None,
    enhanced: bool = False,
    visible_tenant_ids: set[str] | None = None,
    refresh_tenants: Callable[[], set[str] | None] | None = None,
) -> AsyncIterator[str]:
    """Yield SSE frames from the subscription queue until the client goes away.

    ``visible_env_ids`` (None = unrestricted) filters shared-topic payloads
    per tenant; ``refresh_visible`` reloads it every
    ``_VISIBILITY_REFRESH_SECONDS`` so membership changes apply mid-stream.
    """
    refreshed_at = time.monotonic()
    epoch = str(uuid.uuid4())
    sequence = 0
    dropped = getattr(queue, "dropped_events", 0)

    def frame(topic, payload):
        nonlocal sequence
        sequence += 1
        data = {"topic": topic, "payload": payload}
        if enhanced:
            data.update(sequence=sequence, epoch=epoch)
        return f"data: {json.dumps(data)}\n\n"

    try:
        if enhanced:
            yield frame("stream", {"type": "connected", "resync_required": True})
        while True:
            if await request.is_disconnected():
                break
            if (
                refresh_visible is not None
                and time.monotonic() - refreshed_at >= _VISIBILITY_REFRESH_SECONDS
            ):
                previous = visible_env_ids
                visible_env_ids = await asyncio.to_thread(refresh_visible)
                if refresh_tenants is not None:
                    visible_tenant_ids = await asyncio.to_thread(refresh_tenants)
                refreshed_at = time.monotonic()
                if enhanced and previous != visible_env_ids:
                    yield frame(
                        "stream",
                        {
                            "type": "resync",
                            "reason": "scope_changed",
                            "resync_required": True,
                        },
                    )
            lost = getattr(queue, "dropped_events", 0)
            if enhanced and lost != dropped:
                dropped = lost
                yield frame(
                    "stream",
                    {"type": "resync", "reason": "overflow", "resync_required": True},
                )
            try:
                topic, payload = await asyncio.wait_for(
                    queue.get(), timeout=_HEARTBEAT_SECONDS
                )
            except asyncio.TimeoutError:
                # Heartbeat comment so proxies don't kill idle connections.
                yield ": hb\n\n"
                continue
            if not _payload_visible(topic, payload, visible_env_ids, visible_tenant_ids):
                continue
            yield frame(topic, payload)
    finally:
        events.unsubscribe(queue)


@router.get("/stream")
async def stream_events(
    request: Request,
    topics: str = Query(default="fleet"),
    db: Session = Depends(get_db),
    principal: Principal = Depends(_stream_principal),
    protocol: int = Query(default=1, ge=1, le=2),
) -> StreamingResponse:
    """SSE stream of (topic, payload) events for the requested topics.

    env:{id} topics are gated at connect time; shared topics (fleet, jobs,
    alerts, metrics, environments) are filtered per tenant at send time.
    Non-admin principals only receive payloads whose environment_id is in a
    tenant they belong to. Environment create and delete are matched on
    tenant_id so a new or just-removed row is not dropped. The visible set
    is refreshed every ``_VISIBILITY_REFRESH_SECONDS`` so membership changes
    apply mid-stream.
    """
    requested = [t.strip() for t in topics.split(",") if t.strip()]
    if not requested:
        raise HTTPException(status_code=400, detail="No topics requested")
    _authorize_topics(db, principal, requested)

    if events.subscriber_count() >= get_settings().stream_max_subscribers:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Stream subscriber limit reached",
        )

    visible = _visible_env_ids(db, principal)
    visible_tenants = _visible_tenant_ids(db, principal)

    def _refresh() -> set[str] | None:
        # Runs off the event loop via asyncio.to_thread; use a fresh session.
        with SessionLocal() as fresh:
            return _visible_env_ids(fresh, principal)

    def _refresh_tenants() -> set[str] | None:
        with SessionLocal() as fresh:
            return _visible_tenant_ids(fresh, principal)

    queue = events.subscribe(requested)
    return StreamingResponse(
        _event_stream(
            request,
            queue,
            visible,
            _refresh,
            enhanced=protocol == 2,
            visible_tenant_ids=visible_tenants,
            refresh_tenants=_refresh_tenants,
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
