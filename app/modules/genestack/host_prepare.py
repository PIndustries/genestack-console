"""What: Prepare Deploy Host. Take a fresh deploy host to 'ready for push + deploy':
preflight tool checks, clone the genestack repo (fetch-only when already present unless
update=true), then run bootstrap.sh to build the /etc/genestack skeleton.
Where: app/modules/genestack/host_prepare.py. GenestackModule lists this file.
Why: One file so this step does not grow the job runner.
"""

from __future__ import annotations

from app.modules.params import p as _p

HANDLERS = ("genestack_host_prepare",)

OPERATION = {
    "id": "genestack.host_prepare",
    "name": "Prepare Deploy Host",
    "description": (
        "Take a fresh deploy host to 'ready for push + deploy': preflight "
        "tool checks, clone the genestack repo (fetch-only when already "
        "present unless update=true), then run bootstrap.sh to build the "
        "/etc/genestack skeleton. Mirrors docs/genestack-getting-started.md."
    ),
    "required_role": "admin",
    "backend": "genestack",
    "params": [
        _p(
            "repo_url",
            False,
            "Git repo URL (default env metadata repo_url, else rackerlabs/genestack)",
        ),
        _p(
            "repo_ref",
            False,
            "Branch/tag checked out on a fresh clone, or when update=true (default main)",
        ),
        _p(
            "genestack_path",
            False,
            "Remote clone path on the deploy host (default /opt/genestack)",
        ),
        _p(
            "config_dir",
            False,
            "Config dir (default env genestack_config_dir, else /etc/genestack)",
        ),
        _p(
            "update",
            False,
            "If true, reset an existing checkout to repo_url@repo_ref (fetch, checkout, hard reset)",
            "boolean",
        ),
    ],
    "handler": "genestack_host_prepare",
    "mutating": True,
    "timeout_seconds": 3600,
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
            "error": "genestack.host_prepare requires an environment",
            "returncode": 2,
        }
    from app.services import host_prepare as host_prepare_service

    result = host_prepare_service.run_host_prepare(
        env,
        ctx,
        log,
        params=params,
        dry_run=dry,
        timeout=timeout,
        extra_env=extra_env,
        ssh_target=ssh_target,
        remote_env=remote_env,
        agent_env_id=agent_env_id,
    )
    self.write_audit(
        actor=job.created_by or "system",
        action="env.host_prepare",
        resource_type="environment",
        resource_id=env.id,
        environment_id=env.id,
        details={
            "repo_url": result.get("repo_url"),
            "repo_ref": result.get("repo_ref"),
            "genestack_path": result.get("genestack_path"),
            "config_dir": result.get("config_dir"),
            "steps_completed": len(result.get("steps") or []),
            "dry_run": dry,
        },
        success=bool(result.get("ok")),
    )
    return result
