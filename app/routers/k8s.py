"""Kubernetes day-2 manage APIs (native kube-apiserver REST)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.auth import ROLE_RANK
from app.config import Settings, get_settings
from app.db import SessionLocal
from app.deps import check_tenant_access, get_db, get_env_scoped, require_operator
from app.models import Environment
from app.routers import terminal as term
from app.schemas import Principal
from app.services import k8s_ops
from app.services.demo import (
    DEMO_JOB_MESSAGE,
    canned_gateways,
    canned_httproutes,
    canned_ingresses,
    canned_k8s_nodes,
    canned_metallb_pools,
    canned_services,
    canned_workloads,
    is_demo_env,
)
from app.services.envcontext import build_context
from app.services.job_runner import ConflictError, execute_operation

router = APIRouter(prefix="/api/v1", tags=["k8s"])


class ScaleBody(BaseModel):
    replicas: int = Field(ge=0, le=100)


class DrainBody(BaseModel):
    ignore_daemonsets: bool = True
    delete_emptydir: bool = False
    grace_period: int = Field(default=30, ge=0, le=3600)
    timeout: int = Field(default=90, ge=1, le=600)
    dry_run: bool | None = None
    run_sync: bool = False


class NamespaceBody(BaseModel):
    name: str = Field(min_length=1, max_length=63)
    labels: dict[str, str] | None = None


class TaintBody(BaseModel):
    key: str = Field(min_length=1, max_length=253)
    value: str = ""
    effect: str = Field(min_length=1, max_length=32)


class UntaintBody(BaseModel):
    key: str = Field(min_length=1, max_length=253)
    effect: str = Field(min_length=1, max_length=32)


class LabelBody(BaseModel):
    key: str = Field(min_length=1, max_length=253)
    value: str = Field(default="", max_length=63)


class ApplyBody(BaseModel):
    yaml: str = Field(min_length=1, max_length=1_048_576)
    dry_run: bool | None = None
    run_sync: bool = False


def _conflict_http(exc: ConflictError) -> HTTPException:
    detail: dict[str, Any] = {"message": str(exc)}
    if exc.job_id:
        detail["conflicting_job_id"] = exc.job_id
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)


def _enqueue_k8s(
    *,
    db: Session,
    env: Environment,
    principal: Principal,
    operation: str,
    params: dict[str, Any],
    run_sync: bool = False,
) -> dict[str, Any]:
    if is_demo_env(env):
        raise HTTPException(status_code=400, detail=DEMO_JOB_MESSAGE)
    try:
        job = execute_operation(
            db,
            operation=operation,
            params=params,
            environment_id=env.id,
            created_by=principal.username,
            run_sync=run_sync,
        )
    except ConflictError as exc:
        raise _conflict_http(exc) from exc
    db.refresh(job)
    return {
        "ok": job.status.value != "failed",
        "job_id": job.id,
        "status": job.status.value,
        "operation": job.operation,
        "dry_run": job.dry_run,
        "message": (
            job.error
            if job.status.value == "failed"
            else f"{operation} {'completed' if run_sync else 'queued'}"
        ),
        "error": job.error,
    }


class HelmRollbackBody(BaseModel):
    revision: int | None = Field(default=None, ge=1)


class HelmUpgradeBody(BaseModel):
    chart: str | None = None
    values: dict[str, Any] | None = None


def _check_namespace(namespace: str, *, allow_empty: bool = False) -> str:
    text = str(namespace or "").strip()
    err = k8s_ops.validate_namespace(text, allow_empty=allow_empty)
    if err:
        raise HTTPException(status_code=400, detail=err)
    return text


def _check_name(name: str, *, field: str = "name") -> str:
    text = str(name or "").strip()
    err = k8s_ops.validate_name(text, field=field)
    if err:
        raise HTTPException(status_code=400, detail=err)
    return text


def _check_kind(kind: str, allowed: frozenset[str]) -> str:
    text = str(kind or "").strip().lower()
    if text not in allowed:
        raise HTTPException(
            status_code=400,
            detail=f"kind must be one of {', '.join(sorted(allowed))}",
        )
    return text


@router.get("/environments/{environment_id}/k8s/workloads")
def get_workloads(
    namespace: str = Query(default=""),
    pods_only: bool = Query(default=False),
    env: Environment = Depends(get_env_scoped("viewer")),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    ns = _check_namespace(namespace, allow_empty=True)
    if is_demo_env(env):
        return canned_workloads(namespace=ns, pods_only=pods_only)
    return k8s_ops.list_workloads(env, settings, namespace=ns, pods_only=pods_only)


@router.post(
    "/environments/{environment_id}/k8s/workloads/{kind}/{namespace}/{name}/scale"
)
def scale_workload(
    kind: str,
    namespace: str,
    name: str,
    body: ScaleBody,
    env: Environment = Depends(get_env_scoped("operator")),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    kind = _check_kind(kind, k8s_ops.SCALE_KINDS)
    namespace = _check_namespace(namespace)
    name = _check_name(name)
    return k8s_ops.scale_workload(
        env,
        settings,
        kind=kind,
        namespace=namespace,
        name=name,
        replicas=body.replicas,
    )


@router.post(
    "/environments/{environment_id}/k8s/workloads/{kind}/{namespace}/{name}/restart"
)
def restart_workload(
    kind: str,
    namespace: str,
    name: str,
    env: Environment = Depends(get_env_scoped("operator")),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    kind = _check_kind(kind, k8s_ops.RESTART_KINDS)
    namespace = _check_namespace(namespace)
    name = _check_name(name)
    return k8s_ops.restart_workload(
        env, settings, kind=kind, namespace=namespace, name=name
    )


@router.post("/environments/{environment_id}/k8s/gc-stale-pods")
def post_gc_stale_pods(
    env: Environment = Depends(get_env_scoped("operator")),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    return k8s_ops.gc_stale_pods(env, settings)


@router.post("/environments/{environment_id}/k8s/ensure-longhorn-labels")
def post_ensure_longhorn_labels(
    env: Environment = Depends(get_env_scoped("operator")),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    return k8s_ops.ensure_longhorn_node_labels(env, settings)


@router.delete("/environments/{environment_id}/k8s/pods/{namespace}/{name}")
def delete_pod(
    namespace: str,
    name: str,
    env: Environment = Depends(get_env_scoped("operator")),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    namespace = _check_namespace(namespace)
    name = _check_name(name)
    return k8s_ops.delete_pod(env, settings, namespace=namespace, name=name)


@router.post("/environments/{environment_id}/k8s/nodes/{name}/cordon")
def cordon_node(
    name: str,
    env: Environment = Depends(get_env_scoped("operator")),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    name = _check_name(name)
    return k8s_ops.set_node_schedulable(env, settings, name=name, unschedulable=True)


@router.post("/environments/{environment_id}/k8s/nodes/{name}/uncordon")
def uncordon_node(
    name: str,
    env: Environment = Depends(get_env_scoped("operator")),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    name = _check_name(name)
    return k8s_ops.set_node_schedulable(env, settings, name=name, unschedulable=False)


@router.post("/environments/{environment_id}/k8s/nodes/{name}/drain")
def drain_node(
    name: str,
    body: DrainBody | None = None,
    env: Environment = Depends(get_env_scoped("operator")),
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_operator),
) -> dict[str, Any]:
    """Enqueue ``k8s.node.drain`` (audited job + per-env mutex)."""
    name = _check_name(name)
    opts = body or DrainBody()
    params: dict[str, Any] = {
        "name": name,
        "ignore_daemonsets": opts.ignore_daemonsets,
        "delete_emptydir": opts.delete_emptydir,
        "grace_period": opts.grace_period,
        "timeout": opts.timeout,
    }
    if opts.dry_run is not None:
        params["dry_run"] = opts.dry_run
    return _enqueue_k8s(
        db=db,
        env=env,
        principal=principal,
        operation="k8s.node.drain",
        params=params,
        run_sync=opts.run_sync,
    )


@router.get("/environments/{environment_id}/k8s/describe")
def describe_object(
    kind: str = Query(...),
    name: str = Query(...),
    namespace: str = Query(default=""),
    env: Environment = Depends(get_env_scoped("viewer")),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    kind = _check_kind(kind, k8s_ops.DESCRIBE_KINDS)
    name = _check_name(name)
    allow_empty = kind in k8s_ops.CLUSTER_DESCRIBE_KINDS
    ns = _check_namespace(namespace, allow_empty=allow_empty)
    return k8s_ops.describe(env, settings, kind=kind, namespace=ns, name=name)


@router.get("/environments/{environment_id}/k8s/events")
def get_events(
    namespace: str = Query(default=""),
    env: Environment = Depends(get_env_scoped("viewer")),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    ns = _check_namespace(namespace, allow_empty=True)
    return k8s_ops.list_events(env, settings, namespace=ns)


@router.get("/environments/{environment_id}/k8s/namespaces")
def get_namespaces(
    env: Environment = Depends(get_env_scoped("viewer")),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    return k8s_ops.list_namespaces(env, settings)


@router.post("/environments/{environment_id}/k8s/namespaces")
def post_namespace(
    body: NamespaceBody,
    env: Environment = Depends(get_env_scoped("operator")),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    name = _check_namespace(body.name)
    return k8s_ops.create_namespace(env, settings, name=name, labels=body.labels)


@router.delete("/environments/{environment_id}/k8s/namespaces/{namespace}")
def remove_namespace(
    namespace: str,
    env: Environment = Depends(get_env_scoped("operator")),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    namespace = _check_namespace(namespace)
    return k8s_ops.delete_namespace(env, settings, name=namespace)


@router.get("/environments/{environment_id}/k8s/services")
def get_services(
    namespace: str = Query(default=""),
    env: Environment = Depends(get_env_scoped("viewer")),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    ns = _check_namespace(namespace, allow_empty=True)
    if is_demo_env(env):
        return canned_services(namespace=ns)
    return k8s_ops.list_services(env, settings, namespace=ns)


@router.delete("/environments/{environment_id}/k8s/services/{namespace}/{name}")
def remove_service(
    namespace: str,
    name: str,
    env: Environment = Depends(get_env_scoped("operator")),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    namespace = _check_namespace(namespace)
    name = _check_name(name)
    return k8s_ops.delete_service(env, settings, namespace=namespace, name=name)


@router.get("/environments/{environment_id}/k8s/ingresses")
def get_ingresses(
    namespace: str = Query(default=""),
    env: Environment = Depends(get_env_scoped("viewer")),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    ns = _check_namespace(namespace, allow_empty=True)
    if is_demo_env(env):
        return canned_ingresses(namespace=ns)
    return k8s_ops.list_ingresses(env, settings, namespace=ns)


@router.get("/environments/{environment_id}/k8s/gateways")
def get_gateways(
    namespace: str = Query(default=""),
    env: Environment = Depends(get_env_scoped("viewer")),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    ns = _check_namespace(namespace, allow_empty=True)
    if is_demo_env(env):
        return canned_gateways(namespace=ns)
    return k8s_ops.list_gateways(env, settings, namespace=ns)


@router.get("/environments/{environment_id}/k8s/httproutes")
def get_httproutes(
    namespace: str = Query(default=""),
    env: Environment = Depends(get_env_scoped("viewer")),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    ns = _check_namespace(namespace, allow_empty=True)
    if is_demo_env(env):
        return canned_httproutes(namespace=ns)
    return k8s_ops.list_httproutes(env, settings, namespace=ns)


@router.get("/environments/{environment_id}/k8s/metallb/pools")
def get_metallb_pools(
    env: Environment = Depends(get_env_scoped("viewer")),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    if is_demo_env(env):
        return canned_metallb_pools()
    return k8s_ops.list_metallb_pools(env, settings)


@router.get("/environments/{environment_id}/k8s/persistentvolumeclaims")
def get_pvcs(
    namespace: str = Query(default=""),
    env: Environment = Depends(get_env_scoped("viewer")),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    ns = _check_namespace(namespace, allow_empty=True)
    return k8s_ops.list_storage(env, settings, namespace=ns)


@router.get("/environments/{environment_id}/k8s/persistentvolumes")
def get_pvs(
    env: Environment = Depends(get_env_scoped("viewer")),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    return k8s_ops.list_pvs(env, settings)


@router.get("/environments/{environment_id}/k8s/storageclasses")
def get_storageclasses(
    env: Environment = Depends(get_env_scoped("viewer")),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    return k8s_ops.list_storageclasses(env, settings)


@router.delete(
    "/environments/{environment_id}/k8s/persistentvolumeclaims/{namespace}/{name}"
)
def remove_pvc(
    namespace: str,
    name: str,
    env: Environment = Depends(get_env_scoped("operator")),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    namespace = _check_namespace(namespace)
    name = _check_name(name)
    return k8s_ops.delete_pvc(env, settings, namespace=namespace, name=name)


@router.get("/environments/{environment_id}/k8s/configmaps")
def get_configmaps(
    namespace: str = Query(default=""),
    env: Environment = Depends(get_env_scoped("viewer")),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    ns = _check_namespace(namespace, allow_empty=True)
    return k8s_ops.list_configmaps(env, settings, namespace=ns)


@router.get("/environments/{environment_id}/k8s/secrets")
def get_secrets(
    namespace: str = Query(default=""),
    env: Environment = Depends(get_env_scoped("viewer")),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    ns = _check_namespace(namespace, allow_empty=True)
    return k8s_ops.list_secrets(env, settings, namespace=ns)


@router.get("/environments/{environment_id}/k8s/jobs")
def get_jobs(
    namespace: str = Query(default=""),
    env: Environment = Depends(get_env_scoped("viewer")),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    ns = _check_namespace(namespace, allow_empty=True)
    if is_demo_env(env):
        return {"ok": True, "namespace": ns, "jobs": [], "error": None, "demo": True}
    return k8s_ops.list_jobs(env, settings, namespace=ns)


@router.get("/environments/{environment_id}/k8s/helm")
def get_helm(
    env: Environment = Depends(get_env_scoped("viewer")),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    return k8s_ops.list_helm(env, settings)


@router.get("/environments/{environment_id}/k8s/helm/{namespace}/{name}")
def get_helm_status(
    namespace: str,
    name: str,
    env: Environment = Depends(get_env_scoped("viewer")),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    namespace = _check_namespace(namespace)
    name = _check_name(name)
    return k8s_ops.helm_status(env, settings, namespace=namespace, name=name)


@router.get("/environments/{environment_id}/k8s/helm/{namespace}/{name}/history")
def get_helm_history(
    namespace: str,
    name: str,
    env: Environment = Depends(get_env_scoped("viewer")),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    namespace = _check_namespace(namespace)
    name = _check_name(name)
    return k8s_ops.helm_history(env, settings, namespace=namespace, name=name)


@router.post("/environments/{environment_id}/k8s/helm/{namespace}/{name}/rollback")
def post_helm_rollback(
    namespace: str,
    name: str,
    body: HelmRollbackBody | None = None,
    env: Environment = Depends(get_env_scoped("operator")),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    namespace = _check_namespace(namespace)
    name = _check_name(name)
    opts = body or HelmRollbackBody()
    return k8s_ops.helm_rollback(
        env, settings, namespace=namespace, name=name, revision=opts.revision
    )


@router.post("/environments/{environment_id}/k8s/helm/{namespace}/{name}/upgrade")
def post_helm_upgrade(
    namespace: str,
    name: str,
    body: HelmUpgradeBody | None = None,
    env: Environment = Depends(get_env_scoped("operator")),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    namespace = _check_namespace(namespace)
    name = _check_name(name)
    opts = body or HelmUpgradeBody()
    return k8s_ops.helm_upgrade(
        env,
        settings,
        namespace=namespace,
        name=name,
        chart=opts.chart,
        values=opts.values,
    )


@router.get("/environments/{environment_id}/k8s/nodes")
def get_nodes(
    env: Environment = Depends(get_env_scoped("viewer")),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    if is_demo_env(env):
        return canned_k8s_nodes()
    return k8s_ops.list_nodes(env, settings)


@router.post("/environments/{environment_id}/k8s/nodes/{name}/taint")
def post_node_taint(
    name: str,
    body: TaintBody,
    env: Environment = Depends(get_env_scoped("operator")),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    name = _check_name(name)
    return k8s_ops.add_taint(
        env, settings, name=name, key=body.key, value=body.value, effect=body.effect
    )


@router.delete("/environments/{environment_id}/k8s/nodes/{name}/taint")
def delete_node_taint(
    name: str,
    body: UntaintBody,
    env: Environment = Depends(get_env_scoped("operator")),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    name = _check_name(name)
    return k8s_ops.remove_taint(
        env, settings, name=name, key=body.key, effect=body.effect
    )


@router.post("/environments/{environment_id}/k8s/nodes/{name}/label")
def post_node_label(
    name: str,
    body: LabelBody,
    env: Environment = Depends(get_env_scoped("operator")),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    name = _check_name(name)
    return k8s_ops.set_label(env, settings, name=name, key=body.key, value=body.value)


@router.post("/environments/{environment_id}/k8s/apply")
def post_apply(
    body: ApplyBody,
    env: Environment = Depends(get_env_scoped("operator")),
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_operator),
) -> dict[str, Any]:
    """Enqueue ``k8s.apply`` (audited job + per-env mutex)."""
    params: dict[str, Any] = {"yaml": body.yaml}
    if body.dry_run is not None:
        params["dry_run"] = body.dry_run
    return _enqueue_k8s(
        db=db,
        env=env,
        principal=principal,
        operation="k8s.apply",
        params=params,
        run_sync=body.run_sync,
    )


@router.delete("/environments/{environment_id}/k8s/workloads/{kind}/{namespace}/{name}")
def remove_workload(
    kind: str,
    namespace: str,
    name: str,
    env: Environment = Depends(get_env_scoped("operator")),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    kind = _check_kind(kind, k8s_ops.DELETE_WORKLOAD_KINDS)
    namespace = _check_namespace(namespace)
    name = _check_name(name)
    return k8s_ops.delete_workload(
        env, settings, kind=kind, namespace=namespace, name=name
    )


_exec_sessions: dict[tuple[str, str], term.TerminalSession] = {}


@router.websocket("/environments/{environment_id}/k8s/pods/{namespace}/{name}/exec")
async def pod_exec_ws(
    ws: WebSocket,
    environment_id: str,
    namespace: str,
    name: str,
    ticket: str | None = Query(default=None),
    container: str | None = Query(default=None),
) -> None:
    """Interactive ``kubectl exec -it`` into a pod. Operator+. Ticket auth like /terminal."""
    await ws.accept()
    ns_err = k8s_ops.validate_namespace(namespace)
    name_err = k8s_ops.validate_name(name)
    container_text = str(container or "").strip()
    container_err = (
        k8s_ops.validate_name(container_text, field="container")
        if container_text
        else None
    )
    if ns_err or name_err or container_err:
        await term._reject(
            ws, term.CLOSE_NOT_FOUND, ns_err or name_err or container_err or "invalid"
        )
        return

    db = SessionLocal()
    try:
        principal = term._resolve_ws_principal(ticket, ws.headers, db)
        if principal is None:
            await term._reject(
                ws, term.CLOSE_AUTH_FAILED, "missing or invalid credentials"
            )
            return
        if ROLE_RANK[principal.role] < ROLE_RANK["operator"]:
            await term._reject(
                ws, term.CLOSE_FORBIDDEN, f"role '{principal.role}' insufficient"
            )
            return
        env = db.get(Environment, environment_id) if environment_id else None
        if env is None:
            await term._reject(ws, term.CLOSE_NOT_FOUND, "environment not found")
            return
        try:
            check_tenant_access(db, principal, env.tenant_id, "operator")
        except HTTPException:
            await term._reject(
                ws, term.CLOSE_FORBIDDEN, "no operator access to this environment"
            )
            return
        env_id = env.id
        settings = get_settings()
        ctx = build_context(env, settings)
    finally:
        db.close()

    kube = ctx.kubeconfig
    if not kube:
        ctx.cleanup()
        await term._reject(ws, term.CLOSE_NO_DEPLOYER, "no kubeconfig")
        return

    target = f"{namespace}/{name}" + (f":{container_text}" if container_text else "")
    key = (principal.username, env_id)
    old = _exec_sessions.pop(key, None)
    if old is not None:
        old.replaced = True
        await old.finish(
            reason="replaced by a new exec", code=term.CLOSE_REPLACED, close_ws=True
        )

    session = term.TerminalSession(ws, principal.username, env_id, target)
    try:
        argv = k8s_ops.exec_argv(
            kube, namespace=namespace, name=name, container=container_text or None
        )
        session.spawn(argv, extra_env={"KUBECONFIG": kube})
    except (OSError, FileNotFoundError) as exc:
        ctx.cleanup()
        await term._reject(
            ws, term.CLOSE_SPAWN_FAILED, f"failed to spawn kubectl exec: {exc}"
        )
        return
    _exec_sessions[key] = session
    term._write_audit(
        principal.username,
        "env.k8s.exec.open",
        env_id,
        {"target": target, "auth_method": principal.auth_method},
    )
    try:
        await session.run()
    finally:
        if _exec_sessions.get(key) is session:
            _exec_sessions.pop(key, None)
        ctx.cleanup()
