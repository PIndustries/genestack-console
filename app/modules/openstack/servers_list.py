"""What: List OpenStack Servers (VMs). List Nova servers (VMs) running in the environment's
cloud via OpenStack REST (native API), with kubectl exec CLI fallback.
Where: app/modules/openstack/servers_list.py. OpenStackModule lists this file.
Why: One file so this step does not grow the job runner.
"""

from __future__ import annotations

from app.services import openstack_ops

HANDLERS = ("openstack_servers_list",)

OPERATION = {
    "id": "openstack.servers.list",
    "name": "List OpenStack Servers (VMs)",
    "description": (
        "List Nova servers (VMs) running in the environment's cloud via "
        "OpenStack REST (native API), with kubectl exec CLI fallback."
    ),
    "required_role": "viewer",
    "backend": "genestack",
    "params": [],
    "handler": "openstack_servers_list",
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
            "error": "openstack.servers.list requires an environment",
            "returncode": 2,
        }
    result = openstack_ops.list_servers(env, self.settings, log=log)
    vms = result.get("vms") or []
    ok = result.get("source") in ("live", "openstack-api")
    log(
        f"[openstack] servers source={result.get('source')} "
        f"count={len(vms)} error={result.get('error')}"
    )
    return {
        "ok": ok,
        "source": result.get("source"),
        "vms": vms,
        "count": len(vms),
        "error": result.get("error"),
        "returncode": 0 if ok else 1,
        "message": (
            f"{len(vms)} servers"
            if ok
            else f"openstack unavailable: {result.get('error')}"
        ),
    }
