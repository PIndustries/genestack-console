"""What: Environment Agent Status. Show the enrollment and live connection status of the
environment's console agent (connected, last_seen, hostname, version).
Where: app/modules/agents/status.py. AgentModule lists this file.
Why: One file so this step does not grow the job runner.
"""

from __future__ import annotations

HANDLERS = ("agent_status",)

OPERATION = {
    "id": "agent.status",
    "name": "Environment Agent Status",
    "description": (
        "Show the enrollment and live connection status of the "
        "environment's console agent (connected, last_seen, hostname, "
        "version). Read-only."
    ),
    "required_role": "viewer",
    "backend": "agent",
    "params": [],
    "handler": "agent_status",
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
            "error": "agent.status requires an environment",
            "returncode": 2,
        }
    from app.services import agents as agents_service

    payload = agents_service.status_for_env(self.db, env.id)
    log(
        f"[agent] status enrolled={payload['enrolled']} "
        f"connected={payload['connected']} hostname={payload['hostname']}"
    )
    return {
        "ok": True,
        "dry_run": dry,
        **payload,
        "message": (
            "agent connected" if payload["connected"] else "agent not connected"
        ),
    }
