"""Job listing and creation endpoints."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import role_allows
from app.deps import (
    check_tenant_access,
    get_db,
    get_env_scoped,
    require_operator,
    require_viewer,
)
from app.models import Environment, Job, JobStatus, Membership
from app.schemas import JobCreate, JobRead, JobRetry, Principal
from app.services.catalog import get_operation
from app.services.demo import DEMO_JOB_MESSAGE, is_demo_env
from app.services.job_runner import (
    CANCELLED_ERROR,
    ConflictError,
    JobRunner,
    execute_operation,
    merge_confirmed_replace,
    release_env_mutex,
)

router = APIRouter(prefix="/api/v1", tags=["jobs"])


def _conflict_http(exc: ConflictError) -> HTTPException:
    detail = {"message": str(exc)}
    if exc.job_id:
        detail["conflicting_job_id"] = exc.job_id
    return HTTPException(status_code=409, detail=detail)


@router.get("/jobs", response_model=list[JobRead])
def list_jobs(
    environment_id: str | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=500),
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_viewer),
) -> list[Job]:
    stmt = select(Job).order_by(Job.created_at.desc()).limit(limit)
    if environment_id:
        env = db.get(Environment, environment_id)
        if not env:
            raise HTTPException(
                status_code=404,
                detail=f"Environment '{environment_id}' not found. Check the URL or create a new environment.",
            )
        check_tenant_access(db, principal, env.tenant_id, "viewer")
        stmt = stmt.where(Job.environment_id == environment_id)
    elif not principal.platform_admin:
        # Session users only see jobs for environments in their tenants
        member_envs = select(Environment.id).where(
            Environment.tenant_id.in_(
                select(Membership.tenant_id).where(
                    Membership.user_id == principal.user_id
                )
            )
        )
        stmt = stmt.where(Job.environment_id.in_(member_envs))
    if status_filter:
        stmt = stmt.where(Job.status == status_filter)
    return list(db.scalars(stmt).all())


@router.get("/jobs/{job_id}", response_model=JobRead)
def get_job(
    job_id: str,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_viewer),
) -> Job:
    job = db.get(Job, job_id)
    if not job:
        raise HTTPException(
            status_code=404,
            detail=f"Job '{job_id}' not found. Check the job ID or list jobs with GET /api/v1/jobs.",
        )
    if job.environment_id:
        env = db.get(Environment, job.environment_id)
        if env is not None:
            check_tenant_access(db, principal, env.tenant_id, "viewer")
    elif not principal.platform_admin:
        raise HTTPException(
            status_code=403,
            detail=(
                "This job is not scoped to a tenant, and your role does not grant access. "
                "You need platform-admin privileges or the job must belong to a tenant you're a member of."
            ),
        )
    return job


@router.post(
    "/environments/{environment_id}/jobs",
    response_model=JobRead,
    status_code=status.HTTP_201_CREATED,
)
def create_environment_job(
    environment_id: str,
    body: JobCreate,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_operator),
    env: Environment = Depends(get_env_scoped("operator")),
) -> Job:
    op = get_operation(body.operation)
    if op is None:
        raise HTTPException(
            status_code=400, detail=f"Unknown operation: {body.operation}"
        )

    if not role_allows(principal.role, op.required_role):
        raise HTTPException(
            status_code=403,
            detail=f"Operation '{body.operation}' requires role '{op.required_role}'",
        )

    if is_demo_env(env):
        raise HTTPException(status_code=400, detail=DEMO_JOB_MESSAGE)

    # Operators cannot run admin-only; already covered by role_allows.
    # Viewer cannot create jobs (require_operator).

    try:
        job = execute_operation(
            db,
            operation=body.operation,
            params=body.params,
            environment_id=environment_id,
            created_by=principal.username,
            run_sync=body.run_sync,
        )
    except ConflictError as exc:
        raise _conflict_http(exc) from exc
    return job


@router.post("/jobs", response_model=JobRead, status_code=status.HTTP_201_CREATED)
def create_job_global(
    body: JobCreate,
    environment_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_operator),
) -> Job:
    """Create a job not necessarily bound to an environment (e.g. internal.health)."""
    op = get_operation(body.operation)
    if op is None:
        raise HTTPException(
            status_code=400, detail=f"Unknown operation: {body.operation}"
        )
    if not role_allows(principal.role, op.required_role):
        raise HTTPException(
            status_code=403,
            detail=f"Operation '{body.operation}' requires role '{op.required_role}'",
        )
    # Prefer body.environment_id; query param is a fallback
    env_id = body.environment_id or environment_id
    if env_id:
        env = db.get(Environment, env_id)
        if not env:
            raise HTTPException(
                status_code=404,
                detail=f"Environment '{env_id}' not found. Check the URL or create a new environment.",
            )
        check_tenant_access(db, principal, env.tenant_id, "operator")
        if is_demo_env(env):
            raise HTTPException(status_code=400, detail=DEMO_JOB_MESSAGE)

    try:
        return execute_operation(
            db,
            operation=body.operation,
            params=body.params,
            environment_id=env_id,
            created_by=principal.username,
            run_sync=body.run_sync,
        )
    except ConflictError as exc:
        raise _conflict_http(exc) from exc


@router.post(
    "/jobs/{job_id}/retry", response_model=JobRead, status_code=status.HTTP_201_CREATED
)
def retry_job(
    job_id: str,
    body: JobRetry | None = None,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_operator),
) -> Job:
    """Create a NEW job with the same operation/params as an existing job.

    The source job is left untouched.
    """
    job = db.get(Job, job_id)
    if not job:
        raise HTTPException(
            status_code=404,
            detail=f"Job '{job_id}' not found. Cannot retry a job that doesn't exist.",
        )

    if job.environment_id:
        env = db.get(Environment, job.environment_id)
        if env is not None:
            check_tenant_access(db, principal, env.tenant_id, "operator")
            if is_demo_env(env):
                raise HTTPException(status_code=400, detail=DEMO_JOB_MESSAGE)
    elif not principal.platform_admin:
        raise HTTPException(status_code=403, detail="Not a member of this tenant")

    op = get_operation(job.operation)
    if op is None:
        raise HTTPException(
            status_code=400, detail=f"Unknown operation: {job.operation}"
        )

    if not role_allows(principal.role, op.required_role):
        raise HTTPException(
            status_code=403,
            detail=f"Operation '{job.operation}' requires role '{op.required_role}'",
        )

    try:
        return execute_operation(
            db,
            operation=job.operation,
            params=merge_confirmed_replace(job.params, job.user_step),
            environment_id=job.environment_id,
            created_by=principal.username,
            run_sync=body.run_sync if body else True,
            # The source row's params are scrubbed ("***"); carry the
            # encrypted secret values over so the retried job can execute.
            secret_params=job.secret_params or None,
        )
    except ConflictError as exc:
        raise _conflict_http(exc) from exc


@router.post("/jobs/{job_id}/cancel", response_model=JobRead)
def cancel_job(
    job_id: str,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_operator),
) -> Job:
    """Cancel a queued or running job (operator+, tenant-scoped to the job's env).

    Queued jobs are failed immediately ("cancelled") so the worker never
    claims them. Running jobs get ``cancel_requested`` set — best-effort:
    the worker polls the flag between commands (pipeline/deploy item loops)
    and marks the job failed at the next boundary.
    """
    job = db.get(Job, job_id)
    if not job:
        raise HTTPException(
            status_code=404,
            detail=f"Job '{job_id}' not found. Check the job ID or list jobs with GET /api/v1/jobs.",
        )

    if job.environment_id:
        env = db.get(Environment, job.environment_id)
        if env is not None:
            check_tenant_access(db, principal, env.tenant_id, "operator")
    elif not principal.platform_admin:
        raise HTTPException(
            status_code=403,
            detail=(
                "This job is not scoped to a tenant, and your role does not grant access. "
                "You need platform-admin privileges or the job must belong to a tenant you're a member of."
            ),
        )

    if job.status in (JobStatus.success, JobStatus.failed):
        raise HTTPException(
            status_code=409,
            detail=f"Job already finished ({job.status.value}) — cannot cancel",
        )

    runner = JobRunner(db)
    if job.status == JobStatus.queued:
        job.status = JobStatus.failed
        job.error = CANCELLED_ERROR
        job.finished_at = datetime.now(timezone.utc)
        runner.append_log(job, CANCELLED_ERROR)
        runner.write_audit(
            actor=principal.username,
            action="job.cancelled",
            resource_type="job",
            resource_id=job.id,
            environment_id=job.environment_id,
            details={"operation": job.operation, "previous_status": "queued"},
            success=True,
        )
    else:  # running
        if job.cancel_requested:
            # Second cancel: worker never hit a command boundary (hung helm, dead process).
            job.status = JobStatus.failed
            job.error = CANCELLED_ERROR
            job.finished_at = datetime.now(timezone.utc)
            runner.append_log(
                job, "force-cancelled — worker did not stop at the command boundary"
            )
            runner.write_audit(
                actor=principal.username,
                action="job.cancelled",
                resource_type="job",
                resource_id=job.id,
                environment_id=job.environment_id,
                details={
                    "operation": job.operation,
                    "previous_status": "running",
                    "force": True,
                },
                success=True,
            )
            if job.environment_id:
                release_env_mutex(db, job.environment_id, job.id)
        else:
            job.cancel_requested = True
            runner.append_log(
                job, "cancellation requested — stops at the next command boundary"
            )
            runner.write_audit(
                actor=principal.username,
                action="job.cancel_requested",
                resource_type="job",
                resource_id=job.id,
                environment_id=job.environment_id,
                details={"operation": job.operation, "previous_status": "running"},
                success=True,
            )
    db.commit()
    db.refresh(job)
    return job
