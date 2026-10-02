"""What: Greenfield redeploy (commission + Talos + OpenStack). DESTRUCTIVE.
Where: app/modules/genestack/greenfield.py. GenestackModule lists this file.
Why: One file so this step does not grow the job runner.
"""

from __future__ import annotations

from app.modules.params import p as _p

HANDLERS = ("genestack_greenfield",)

OPERATION = {
    "id": "genestack.greenfield",
    "name": "Greenfield redeploy (commission + Talos + OpenStack)",
    "description": (
        "DESTRUCTIVE. For each inventory server: PXE a RAM-disk commission "
        "image that wipes fixed disks and reports hardware, then one-shot "
        "PXE the Talos image. Fresh maintenance means the Talos API is up, "
        "the node is not Kubernetes Ready, and Talos was served after this "
        "wipe. Then deploy OpenStack from hosts. boot=iso is rejected "
        "because an ISO cannot run the wipe. stop_after=commission returns "
        "after the wipe report; stop_after=talos returns after fresh "
        "maintenance and does not deploy. A machine still running the old "
        "OS is not deployed onto. Requires a BMC row per server (or an "
        "OVH-bound env, which uses BYOI). Workloads are destroyed."
    ),
    "required_role": "admin",
    "backend": "genestack",
    "params": [
        _p("dry_run", False, "Force a dry-run rehearsal", "boolean"),
        _p("skip_push", False, "Do not re-push the config document", "boolean"),
        _p(
            "boot",
            False,
            "auto or pxe. iso is rejected because it cannot wipe disks",
            "string",
        ),
        _p(
            "parallelism",
            False,
            "OpenStack services parallelism after metal is up (1..16)",
            "integer",
        ),
        _p(
            "stop_after",
            False,
            "Optional hold: commission (after the wipe report, no Talos) "
            "or talos (after fresh maintenance, no OpenStack). "
            "Omit to run the full path.",
        ),
    ],
    "handler": "genestack_greenfield",
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
    if env is None:
        return {
            "ok": False,
            "error": (
                "Cannot greenfield: this job has no environment assigned. "
                "Create the job under an environment with POST /api/v1/environments/<id>/jobs."
            ),
            "returncode": 2,
        }
    from app.services import greenfield as greenfield_service

    effective_dry = dry or bool(params.get("dry_run"))
    skip_push = params.get("skip_push")
    if skip_push is None:
        skip_push = True
    boot = str(params.get("boot") or "auto").strip() or "auto"
    stop_after = str(params.get("stop_after") or "").strip().lower()
    log(
        f"[greenfield] Starting greenfield redeploy for '{env.name}' "
        f"(dry_run={effective_dry} boot={boot} "
        f"stop_after={stop_after or 'deploy'})"
    )
    result = greenfield_service.run_greenfield(
        self.db,
        env,
        ctx,
        log,
        dry_run=effective_dry,
        skip_push=bool(skip_push),
        timeout=timeout,
        extra_env=extra_env,
        ssh_target=ssh_target,
        remote_env=remote_env,
        settings=self.settings,
        deadline=deadline,
        check_cancel=check_cancel,
        parallelism=params.get("parallelism"),
        boot=boot,
        stop_after=stop_after,
    )
    self.write_audit(
        actor=job.created_by or "system",
        action="env.greenfield",
        resource_type="environment",
        resource_id=env.id,
        environment_id=env.id,
        details={
            "boot": boot,
            "stop_after": stop_after,
            "skip_push": bool(skip_push),
            "dry_run": effective_dry,
            "failed_at": result.get("failed_at"),
            "stages_completed": result.get("stages_completed"),
        },
        success=bool(result.get("ok")),
    )
    return result
