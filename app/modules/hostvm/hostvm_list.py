"""What: List Host VMs (QEMU). Discover and list the QEMU virtual machines running on the
console host (genestack lab/dev nodes) with live pid/cpu/rss stats merged onto the
HostVM registry.
Where: app/modules/hostvm/hostvm_list.py. HostVmModule lists this file.
Why: One file so this step does not grow the job runner.
"""

from __future__ import annotations

from app.services import hypervisor as hypervisor_service

HANDLERS = ("hostvm_list",)

OPERATION = {
    "id": "hostvm.list",
    "name": "List Host VMs (QEMU)",
    "description": (
        "Discover and list the QEMU virtual machines running on the "
        "console host (genestack lab/dev nodes) with live pid/cpu/rss "
        "stats merged onto the HostVM registry."
    ),
    "required_role": "viewer",
    "backend": "internal",
    "params": [],
    "handler": "hostvm_list",
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
    vms = hypervisor_service.vm_status(self.db, self.settings)
    log(f"[hostvm] listed {len(vms)} host VMs")
    return {
        "ok": True,
        "vms": vms,
        "count": len(vms),
        "message": f"{len(vms)} host VMs",
    }
