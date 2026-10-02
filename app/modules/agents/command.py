"""What: Run Command on Environment Agent. Run an allowlisted proof command on the
environment's connected console agent over the agent channel; output streams into the
job log and the agent's return code decides the job result.
Where: app/modules/agents/command.py. AgentModule lists this file.
Why: One file so this step does not grow the job runner.
"""

from __future__ import annotations

from app.modules.params import p as _p
from app.services import agent_relay
from app.services.agents import AGENT_COMMAND_ALLOWLIST

HANDLERS = ("agent_command",)

OPERATION = {
    "id": "agent.command",
    "name": "Run Command on Environment Agent",
    "description": (
        "Run an allowlisted proof command on the environment's connected "
        "console agent over the agent channel; output streams into the "
        "job log and the agent's return code decides the job result. "
        f"Allowlist: {', '.join(sorted(AGENT_COMMAND_ALLOWLIST))}. "
        "Fails cleanly when no agent is connected."
    ),
    "required_role": "admin",
    "backend": "agent",
    "params": [
        _p(
            "command",
            True,
            "Allowlisted command, e.g. 'uptime' or 'kubectl get nodes'",
        ),
    ],
    "handler": "agent_command",
    "mutating": True,
    "timeout_seconds": 600,
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
            "error": "agent.command requires an environment",
            "returncode": 2,
        }
    from app.services import agents as agents_service

    command = str(params.get("command") or "").strip()
    argv = agents_service.AGENT_COMMAND_ALLOWLIST.get(command)
    if argv is None:
        allowed = ", ".join(sorted(agents_service.AGENT_COMMAND_ALLOWLIST))
        msg = f"Command '{command}' is not allowlisted (allowed: {allowed})"
        log(f"[denied] {msg}")
        return {"ok": False, "error": msg, "returncode": 2}

    if dry:
        log(f"[dry-run] would run '{command}' on the env's connected agent")
        return {
            "ok": True,
            "dry_run": True,
            "command": command,
            "message": f"[dry-run] would run '{command}' on agent",
        }

    if not agents_service.agent_available(self.db, env.id):
        msg = "no agent connected for this env"
        log(f"[agent] {msg}")
        return {"ok": False, "error": msg, "returncode": 2}

    # Cross-process: the row is dispatched by the API-process relay,
    # which owns the agent's websocket (the worker's local registry
    # is always empty).
    result = agent_relay.agent_exec(
        env.id,
        "run_command",
        {"cmd": argv, "timeout": timeout},
        timeout=timeout,
        log_cb=lambda line: log(f"[agent] {line}"),
    )
    if result.get("error"):
        log(f"[agent] {result['error']}")
        return {"ok": False, "error": str(result["error"]), "returncode": 2}

    rc = result.get("rc")
    ok = rc == 0
    if result.get("stderr"):
        log(f"[agent] stderr: {result['stderr']}")
    self.write_audit(
        actor=job.created_by or "system",
        action="env.agent.command",
        resource_type="environment",
        resource_id=env.id,
        environment_id=env.id,
        details={"command": command, "returncode": rc, "dry_run": dry},
        success=ok,
    )
    return {
        "ok": ok,
        "command": command,
        "returncode": rc,
        "stdout": result.get("stdout"),
        "stderr": result.get("stderr"),
        "dry_run": False,
        "message": (
            f"agent command '{command}' completed"
            if ok
            else f"agent command '{command}' failed (rc={rc}) — see log"
        ),
    }
