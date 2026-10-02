"""What: Scan Subnet for BMCs. Route a scan_bmc command to the environment's connected
agent: it sweeps the given subnet (CIDR) for Redfish BMC endpoints and reports each find
back as a bmc_found event into the discovery inbox (GET
/api/v1/environments/{id}/discovery).
Where: app/modules/baremetal/bmc_scan.py. BaremetalModule lists this file.
Why: One file so this step does not grow the job runner.
"""

from __future__ import annotations

from app.modules.params import p as _p
from app.services import agent_relay

HANDLERS = ("baremetal_bmc_scan",)

OPERATION = {
    "id": "baremetal.bmc_scan",
    "name": "Scan Subnet for BMCs",
    "description": (
        "Route a scan_bmc command to the environment's connected agent: "
        "it sweeps the given subnet (CIDR) for Redfish BMC endpoints and "
        "reports each find back as a bmc_found event into the discovery "
        "inbox (GET /api/v1/environments/{id}/discovery). The agent's "
        "found count lands in the job result. Fails cleanly when no "
        "agent is connected."
    ),
    "required_role": "operator",
    "backend": "baremetal",
    "params": [
        _p("subnet", True, "CIDR to sweep for BMCs, e.g. 10.0.0.0/24"),
    ],
    "handler": "baremetal_bmc_scan",
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
            "error": "baremetal.bmc_scan requires an environment",
            "returncode": 2,
        }
    import ipaddress

    from app.services import agents as agents_service

    subnet = str(params.get("subnet") or "").strip()
    try:
        subnet = str(ipaddress.ip_network(subnet, strict=False))
    except ValueError:
        msg = f"baremetal.bmc_scan: invalid CIDR subnet {subnet!r}"
        log(f"[denied] {msg}")
        return {"ok": False, "error": msg, "returncode": 2}

    if dry:
        log(
            f"[dry-run] would scan {subnet} for BMCs via the env's connected agent"
        )
        return {
            "ok": True,
            "dry_run": True,
            "subnet": subnet,
            "message": f"[dry-run] would scan {subnet} for BMCs",
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
        "scan_bmc",
        {"subnet": subnet, "timeout": timeout},
        timeout=timeout,
        log_cb=lambda line: log(f"[agent] {line}"),
    )
    if result.get("error"):
        log(f"[agent] {result['error']}")
        return {"ok": False, "error": str(result["error"]), "returncode": 2}

    rc = result.get("rc")
    ok = rc in (0, None)
    found = result.get("found")
    # Audit the subnet and the found count — never credentials.
    self.write_audit(
        actor=job.created_by or "system",
        action="env.bmc_scan",
        resource_type="environment",
        resource_id=env.id,
        environment_id=env.id,
        details={"subnet": subnet, "found": found, "dry_run": dry},
        success=ok,
    )
    return {
        "ok": ok,
        "subnet": subnet,
        "found": found,
        "returncode": rc,
        "stdout": result.get("stdout"),
        "stderr": result.get("stderr"),
        "dry_run": False,
        "message": (
            f"bmc scan of {subnet} complete: {found} found"
            if ok
            else f"bmc scan of {subnet} failed (rc={rc}) — see log"
        ),
    }
