"""Native Kubernetes streams: scoped tickets/headers, true watch/log follow."""

from __future__ import annotations

import shutil
import threading
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from app.auth import ROLE_RANK
from app.config import get_settings
from app.db import SessionLocal
from app.deps import check_tenant_access, get_db
from app.models import Environment, User
from app.routers.stream import _stream_principal
from app.schemas import Principal
from app.services import native_kubernetes as source
from app.services.envcontext import build_context
from app.services.livestate import _valid_k8s_name

router = APIRouter(
    prefix="/api/v1/environments/{environment_id}/native/kubernetes", tags=["native"]
)
_lock = threading.Lock()
_active = 0


def scoped_environment(
    environment_id: str,
    db: Annotated[Session, Depends(get_db)],
    principal: Annotated[Principal, Depends(_stream_principal)],
) -> Environment:
    env = db.get(Environment, environment_id)
    if env is None:
        raise HTTPException(404, "Environment not found")
    if ROLE_RANK[principal.role] < ROLE_RANK["viewer"]:
        raise HTTPException(403, "Viewer role required")
    check_tenant_access(db, principal, env.tenant_id, "viewer")
    return env


def _authorized(environment_id: str, principal: Principal) -> bool:
    with SessionLocal() as db:
        env = db.get(Environment, environment_id)
        if env is None:
            return False
        if principal.user_id:
            user = db.get(User, principal.user_id)
            if user is None or not user.active:
                return False
            principal = principal.model_copy(
                update={"platform_admin": user.platform_admin}
            )
        try:
            check_tenant_access(db, principal, env.tenant_id, "viewer")
            return True
        except HTTPException:
            return False


def _response(
    env: Environment, principal: Principal, arguments: list[str], **options
) -> StreamingResponse:
    global _active
    executable = shutil.which("kubectl")
    if not executable:
        raise HTTPException(503, "kubectl is unavailable")
    ctx = build_context(env, get_settings())
    if not ctx.kubeconfig:
        ctx.cleanup()
        raise HTTPException(409, "Environment has no configured kubeconfig")
    with _lock:
        if _active >= min(get_settings().stream_max_subscribers, 16):
            ctx.cleanup()
            raise HTTPException(503, "Native Kubernetes stream limit reached")
        _active += 1

    released = False

    def release():
        global _active
        nonlocal released
        with _lock:
            if released:
                return
            released = True
            _active -= 1
        ctx.cleanup()

    return StreamingResponse(
        source.source_stream(
            [executable, *arguments],
            ctx,
            env.id,
            authorized=lambda: _authorized(env.id, principal),
            release=release,
            **options,
        ),
        media_type="text/event-stream",
        background=BackgroundTask(release),
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@router.get("/watch")
def watch(
    env: Annotated[Environment, Depends(scoped_environment)],
    principal: Annotated[Principal, Depends(_stream_principal)],
    resource: str = "pods",
    namespace: str | None = None,
):
    if resource not in source.WATCH_RESOURCES:
        raise HTTPException(422, "Unsupported watch resource")
    if namespace and not _valid_k8s_name(namespace, namespace=True):
        raise HTTPException(422, "Invalid namespace")
    if namespace and resource == "nodes":
        raise HTTPException(422, "Nodes are not namespaced")
    args = [
        "get",
        resource,
        "--watch",
        "--output-watch-events",
        "--output=json",
        "--request-timeout=15m",
    ]
    if namespace:
        args.extend(["--namespace", namespace])
    elif resource != "nodes":
        args.append("--all-namespaces")
    return _response(env, principal, args, resource=resource)


@router.get("/pods/{namespace}/{pod}/logs")
def logs(
    namespace: str,
    pod: str,
    env: Annotated[Environment, Depends(scoped_environment)],
    principal: Annotated[Principal, Depends(_stream_principal)],
    container: str | None = None,
    tail: int = Query(default=200, ge=1, le=2000),
):
    if not _valid_k8s_name(namespace, namespace=True) or not _valid_k8s_name(pod):
        raise HTTPException(422, "Invalid namespace or pod")
    if container and not _valid_k8s_name(container):
        raise HTTPException(422, "Invalid container")
    args = [
        "logs",
        "--namespace",
        namespace,
        pod,
        "--follow",
        "--timestamps",
        f"--tail={tail}",
        "--request-timeout=15m",
    ]
    if container:
        args.extend(["--container", container])
    return _response(
        env,
        principal,
        args,
        resource=None,
        namespace=namespace,
        pod=pod,
        container=container,
    )
