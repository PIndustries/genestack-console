"""What: Warm cluster image cache. Start the Console pull-through registries and pull every
image the live cluster already runs through them.
Where: app/modules/genestack/registry_mirror.py. GenestackModule lists this file.
Why: One file so this step does not grow the job runner.
"""

from __future__ import annotations

from app.modules.params import p as _p

HANDLERS = ("registry_mirror",)

OPERATION = {
    "id": "registry.mirror",
    "name": "Warm cluster image cache",
    "description": (
        "Start the Console pull-through registries and pull every image "
        "the live cluster already runs through them. Next greenfield "
        "boots from this cache instead of the internet. Does not skip "
        "observability or testing."
    ),
    "required_role": "admin",
    "backend": "internal",
    "params": [
        _p("dry_run", False, "List images without fetching", "boolean"),
        _p(
            "start_only",
            False,
            "Start the pull-through caches and do not pull images",
            "boolean",
        ),
    ],
    "handler": "registry_mirror",
    "mutating": True,
    "timeout_seconds": 21600,
}


def run(
    self,
    handler,
    op,
    job,
    env,
    log,
    ctx,
    params,
    deadline,
    check_cancel,
    dry,
    timeout,
    gs_root,
    ans_root,
    extra_env,
    ssh_target,
    remote_env,
    executor,
    agent_env_id,
):
    from app.services import image_registry as image_registry_service

    if env is None:
        return {
            "ok": False,
            "error": "registry.mirror requires an environment",
            "returncode": 2,
        }
    effective_dry = dry or bool(params.get("dry_run"))
    start_only = bool(params.get("start_only"))
    if start_only:
        log("[registry] starting Console image caches")
    else:
        log("[registry] warming Console image cache from live cluster")
    result = image_registry_service.mirror_cluster(
        env,
        ctx.kubeconfig if ctx is not None else None,
        log,
        dry_run=effective_dry,
        settings=self.settings,
        check_cancel=check_cancel,
        start_only=start_only,
    )
    self.write_audit(
        actor=job.created_by or "system",
        action="env.registry.mirror",
        resource_type="environment",
        resource_id=env.id,
        environment_id=env.id,
        details={
            "cached": result.get("cached"),
            "failed": result.get("failed"),
            "total": result.get("total"),
            "dry_run": effective_dry,
        },
        success=bool(result.get("ok")),
    )
    return result
