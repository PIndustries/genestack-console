"""What: OVH vRack Attach. Attach this environment's OVH dedicated servers to a vRack so
the private NICs share one L2 fabric.
Where: app/modules/ovh/vrack_attach.py. OvhModule lists this file.
Why: One file so this step does not grow the job runner.
"""

from __future__ import annotations

from app.modules.params import p as _p

HANDLERS = ("ovh_vrack_attach",)

OPERATION = {
    "id": "ovh.vrack.attach",
    "name": "OVH vRack Attach",
    "description": (
        "Attach this environment's OVH dedicated servers to a vRack so "
        "the private NICs share one L2 fabric. Rise boxes use the VNI "
        "(dedicatedServerInterface); older SKUs attach the whole server. "
        "Does not set the 802.1q tag — that is applied at Talos provision "
        "from ovh.vlan_id. Requires a consumer key with POST /vrack/*."
    ),
    "required_role": "admin",
    "backend": "internal",
    "params": [
        _p(
            "vrack", False, "vRack service name (default: ovh.vrack on the env doc)"
        ),
        _p(
            "server_hostnames",
            False,
            "Only attach these inventory hostnames (default: all OVH-owned servers)",
            "array",
        ),
        _p(
            "dry_run",
            False,
            "Force a dry-run rehearsal regardless of env setting",
            "boolean",
        ),
    ],
    "handler": "ovh_vrack_attach",
    "mutating": True,
    "timeout_seconds": 1800,
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
            "error": "ovh.vrack.attach requires an environment",
            "returncode": 2,
        }
    from app.services import ovh_fabric as fabric

    vrack = str(params.get("vrack") or "").strip() or None
    hostnames = params.get("server_hostnames")
    hostname_filter = None
    if isinstance(hostnames, list) and hostnames:
        hostname_filter = frozenset(str(h) for h in hostnames if str(h).strip())
    effective_dry = dry or bool(params.get("dry_run"))
    log(
        f"[vrack] attach environment '{env.name}' vrack={vrack or '(from doc)'} "
        f"dry_run={effective_dry}"
    )
    result = fabric.attach_env_to_vrack(
        self.db,
        env,
        vrack=vrack,
        dry_run=effective_dry,
        log=log,
        hostname_filter=hostname_filter,
    )
    self.write_audit(
        actor=job.created_by or "system",
        action="env.ovh.vrack_attach",
        resource_type="environment",
        resource_id=env.id,
        environment_id=env.id,
        details={
            "vrack": result.get("vrack") or vrack,
            "count": result.get("count"),
            "dry_run": effective_dry,
        },
        success=bool(result.get("ok")),
    )
    return result
