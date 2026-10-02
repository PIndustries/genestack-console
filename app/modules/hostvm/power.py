"""What: Shared steps: Start Host VM (hostvm.start); Stop Host VM (hostvm.stop); Restart
Host VM (hostvm.restart).
Where: app/modules/hostvm/power.py. HostVmModule lists this file.
Why: These handlers share one body, so they stay in one file instead of growing the job
runner.
"""

from __future__ import annotations

from app.models import HostVM
from app.modules.params import p as _p
from app.services import hypervisor as hypervisor_service

HANDLERS = (
    "hostvm_start",
    "hostvm_stop",
    "hostvm_restart",
)

OPERATIONS = (
    {
        "id": "hostvm.start",
        "name": "Start Host VM",
        "description": (
            "Start a stopped host QEMU VM by replaying the argv captured at "
            "discovery (cwd=VM workdir, detached). Workdir must be under a "
            "configured hypervisor root."
        ),
        "required_role": "operator",
        "backend": "internal",
        "params": [
            _p("vm_id", True, "HostVM row UUID"),
        ],
        "handler": "hostvm_start",
        "mutating": True,
        "timeout_seconds": 120,
    },
    {
        "id": "hostvm.stop",
        "name": "Stop Host VM",
        "description": (
            "Stop a running host QEMU VM: SIGTERM the live pid, escalating "
            "to SIGKILL after 10s."
        ),
        "required_role": "operator",
        "backend": "internal",
        "params": [
            _p("vm_id", True, "HostVM row UUID"),
        ],
        "handler": "hostvm_stop",
        "mutating": True,
        "timeout_seconds": 120,
    },
    {
        "id": "hostvm.restart",
        "name": "Restart Host VM",
        "description": "Restart a host QEMU VM (stop, then start from the stored cmdline).",
        "required_role": "operator",
        "backend": "internal",
        "params": [
            _p("vm_id", True, "HostVM row UUID"),
        ],
        "handler": "hostvm_restart",
        "mutating": True,
        "timeout_seconds": 120,
    },
)


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
    # Host VMs are host-level infra — never environment-scoped.
    vm_id = str(params.get("vm_id", "")).strip()
    vm = self.db.get(HostVM, vm_id) if vm_id else None
    if vm is None:
        msg = f"{op.id}: unknown host VM id {vm_id!r}"
        log(f"[denied] {msg}")
        return {"ok": False, "error": msg, "returncode": 2}
    action = handler.removeprefix("hostvm_")
    result = hypervisor_service.vm_action(
        self.db, self.settings, vm, action, dry_run=dry
    )
    log(f"[hostvm] {action} {vm.name} ({vm.id}): {result.get('message')}")
    result.setdefault("vm_id", vm.id)
    result.setdefault("vm_name", vm.name)
    return result
