"""What: Push-Install Agent on Host. Install the console agent on a host inside the
environment over ssh: issues a fresh enrollment credential (replacing any existing one),
then runs the curl-pipe installer on the target (curl <advertise>/agent | bash -s --
--hub ...
Where: app/modules/agents/install.py. AgentModule lists this file.
Why: One file so this step does not grow the job runner.
"""

from __future__ import annotations

from app.modules.params import p as _p

HANDLERS = ("agent_install",)

OPERATION = {
    "id": "agent.install",
    "name": "Push-Install Agent on Host",
    "description": (
        "Install the console agent on a host inside the environment over "
        "ssh: issues a fresh enrollment credential (replacing any "
        "existing one), then runs the curl-pipe installer on the target "
        "(curl <advertise>/agent | bash -s -- --hub ... --token ...). "
        "When the target cannot pull the script from the hub, the "
        "packaged agent/install.sh is streamed over the ssh stdin "
        "instead. Requires hub.advertise_url in config.yaml — the "
        "address agents can reach. The agent dials OUT to the hub, so "
        "this is how agents get onto networks the console cannot reach "
        "inbound. The raw token is masked (gsca_***) in the job log."
    ),
    "required_role": "admin",
    "backend": "agent",
    "params": [
        _p("host", True, "Target host (IP or name) the console can ssh to"),
        _p("ssh_user", False, "SSH user (default root)"),
        _p("ssh_port", False, "SSH port (default 22)", "integer"),
        _p("name", False, "Agent/container name (default: the host)"),
    ],
    "handler": "agent_install",
    "mutating": True,
    "timeout_seconds": 900,
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
                "Cannot install agent: this job has no environment assigned. "
                "Create the job scoped to an environment with POST /api/v1/environments/<id>/jobs."
            ),
            "returncode": 2,
        }
    from app.services import agents as agents_service

    host = str(params.get("host") or "").strip()
    if not host:
        msg = (
            "Cannot install agent: 'host' parameter is missing or empty. "
            "Specify the target host as a hostname or IP address. "
            'Example: {"operation": "agent.install", "params": {"host": "node01.example.com"}}'
        )
        log(f"[agent] {msg}")
        return {"ok": False, "error": msg, "returncode": 2}

    ssh_user = str(params.get("ssh_user") or "root").strip() or "root"
    try:
        ssh_port = int(params.get("ssh_port") or 22)
    except (TypeError, ValueError):
        ssh_port = 22
    name = str(params.get("name") or "").strip() or None
    log(
        f"[agent] Starting agent install on {ssh_user}@{host}:{ssh_port} for environment '{env.name}'"
    )
    result = agents_service.install_agent(
        self.db,
        env,
        self.settings,
        host=host,
        ssh_user=ssh_user,
        ssh_port=ssh_port,
        name=name,
        dry_run=dry,
        timeout=timeout,
        log=log,
    )
    # Audit carries the host, never the token.
    self.write_audit(
        actor=job.created_by or "system",
        action="env.agent.install",
        resource_type="environment",
        resource_id=env.id,
        environment_id=env.id,
        details={
            "host": host,
            "ssh_user": ssh_user,
            "agent_name": result.get("agent_name"),
            "dry_run": dry,
        },
        success=bool(result.get("ok")),
    )
    return result
