"""What: Register Bare-Metal Node. Register a bare-metal node (BMC address + Redfish
credentials). The console installs Talos from the network.
Where: app/modules/baremetal/node_register.py. BaremetalModule lists this file.
Why: One file so this step does not grow the job runner.
"""

from __future__ import annotations

from app.modules.params import p as _p

HANDLERS = ("baremetal_node_register",)

OPERATION = {
    "id": "baremetal.node.register",
    "name": "Register Bare-Metal Node",
    "description": (
        "Register a bare-metal node (BMC address and Redfish credentials). "
        "The console installs Talos from the network. The BMC password is "
        "fernet-encrypted at rest. When pxe_mac is omitted the node's "
        "ethernet MACs are probed over Redfish to autofill it (the probe "
        "is skipped on dry-run; a probe failure registers without a MAC)."
    ),
    "required_role": "operator",
    "backend": "baremetal",
    "params": [
        _p("name", True, "Node name (hostname-safe; becomes the inventory key)"),
        _p("bmc_host", True, "BMC address (host/IP or https:// URL)"),
        _p("bmc_username", True, "BMC (Redfish) username"),
        _p("bmc_password", True, "BMC (Redfish) password — encrypted at rest"),
        _p("pxe_mac", False, "PXE boot MAC (default: probed via Redfish)"),
    ],
    "secret_params": ("bmc_password",),
    "handler": "baremetal_node_register",
    "mutating": True,
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
            "error": "baremetal.node.register requires an environment",
            "returncode": 2,
        }
    from app.services import baremetal as baremetal_service

    name = str(params.get("name") or "").strip()
    result = baremetal_service.register_node(
        self.db,
        env,
        name=name,
        bmc_host=str(params.get("bmc_host") or ""),
        bmc_username=str(params.get("bmc_username") or ""),
        bmc_password=str(params.get("bmc_password") or ""),
        pxe_mac=str(params.get("pxe_mac") or "").strip() or None,
        dry_run=dry,
        log=log,
        settings=self.settings,
    )
    self.write_audit(
        actor=job.created_by or "system",
        action="env.baremetal.register",
        resource_type="environment",
        resource_id=env.id,
        environment_id=env.id,
        details={
            "name": name,
            "bmc_host": str(params.get("bmc_host") or "").strip(),
            "dry_run": dry,
        },
        success=bool(result.get("ok")),
    )
    return result
