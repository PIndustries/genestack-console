"""What: Deploy environment (push config + full pipeline). Helm restack: push config, then
run pipeline stages.
Where: app/modules/genestack/deploy.py. GenestackModule lists this file.
Why: One file so this step does not grow the job runner.
"""

from __future__ import annotations

from app.modules.params import p as _p

HANDLERS = ("genestack_deploy",)

OPERATION = {
    "id": "genestack.deploy",
    "name": "Deploy environment (push config + full pipeline)",
    "description": (
        "Helm restack: push config, then run pipeline stages. Required "
        "stages (hosts through compute-network) stop the job on failure. "
        "Optional extras/observability warn and continue. Tempest is not "
        "in this job — run genestack.tempest. from_stage / until_stage "
        "are the control points."
    ),
    "required_role": "admin",
    "backend": "genestack",
    "params": [
        _p(
            "dry_run",
            False,
            "Force a dry-run rehearsal regardless of env setting",
            "boolean",
        ),
        _p(
            "skip_push",
            False,
            "Run the pipeline without pushing config first",
            "boolean",
        ),
        _p(
            "from_stage",
            False,
            "Start the pipeline at this stage id (e.g. core) instead of hosts",
            "string",
        ),
        _p(
            "until_stage",
            False,
            "Stop after this stage id (run one control point)",
            "string",
        ),
        _p(
            "include_testing",
            False,
            "Also run Tempest as the last stage (default: skip; use genestack.tempest)",
            "boolean",
        ),
        _p(
            "parallelism",
            False,
            "Run up to N hosts in parallel (1..16, default 1 = sequential); "
            "keystone is always installed first, independently",
            "integer",
        ),
        _p(
            "replace_hosts",
            False,
            "Hostnames already confirmed for a Talos install replacement. "
            "Passed through when this deploy starts at hosts.",
        ),
    ],
    "handler": "genestack_deploy",
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
                "Cannot deploy: this job has no environment assigned. "
                "Create the job under an environment with POST /api/v1/environments/<id>/jobs."
            ),
            "returncode": 2,
        }
    from app.services import deploy as deploy_service

    # Effective dry_run: the env/global dry-run pin always wins.
    # params.dry_run may only force a rehearsal on top of it — it can
    # never force LIVE execution on a dry-run-pinned environment.
    effective_dry = dry or bool(params.get("dry_run"))
    raw_from_stage = params.get("from_stage")
    from_stage = str(raw_from_stage).strip() if raw_from_stage else None
    raw_until = params.get("until_stage")
    until_stage = str(raw_until).strip() if raw_until else None
    include_testing = bool(params.get("include_testing"))
    parallelism = params.get("parallelism")
    log(
        f"[deploy] Starting deploy for environment '{env.name}' (dry_run={effective_dry})"
    )
    if from_stage:
        log(f"[deploy] Resuming from stage '{from_stage}'")
    if until_stage:
        log(f"[deploy] until_stage '{until_stage}'")
    result = deploy_service.run_deploy(
        self.db,
        env,
        ctx,
        log,
        dry_run=effective_dry,
        skip_push=bool(params.get("skip_push")),
        timeout=timeout,
        extra_env=extra_env,
        ssh_target=ssh_target,
        remote_env=remote_env,
        settings=self.settings,
        from_stage=from_stage,
        until_stage=until_stage,
        include_testing=include_testing,
        deadline=deadline,
        check_cancel=check_cancel,
        parallelism=parallelism,
        replace_hosts=params.get("replace_hosts") if isinstance(params, dict) else None,
    )
    self.write_audit(
        actor=job.created_by or "system",
        action="env.deploy",
        resource_type="environment",
        resource_id=env.id,
        environment_id=env.id,
        details={
            "version": result.get("version"),
            "stages_completed": result.get("stages_completed"),
            "stages_total": result.get("stages_total"),
            "failed_at": result.get("failed_at"),
            "skip_push": bool(params.get("skip_push")),
            "from_stage": from_stage,
            "dry_run": effective_dry,
            "parallelism": parallelism,
        },
        success=bool(result.get("ok")),
    )
    return result
