"""What: Internal Health Check. Return console health and path configuration (no side
effects).
Where: app/modules/console/internal_health.py. ConsoleModule lists this file.
Why: One file so this step does not grow the job runner.
"""

from __future__ import annotations

HANDLERS = ("internal_health",)

OPERATION = {
    "id": "internal.health",
    "name": "Internal Health Check",
    "description": "Return console health and path configuration (no side effects).",
    "required_role": "viewer",
    "backend": "internal",
    "params": [],
    "handler": "internal_health",
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
    log("internal.health")
    return {
        "ok": True,
        "message": "healthy",
        "dry_run": dry,
        "genestack_root": str(gs_root),
        "ansible_root": str(ans_root),
    }
