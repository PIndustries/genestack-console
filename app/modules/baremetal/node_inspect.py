"""Explicit RAM inventory boot, separate from any destructive installation."""
from __future__ import annotations

import time

from app.models import BaremetalNode
from app.modules.params import p
from app.services import hardware_inspection

HANDLERS = ("baremetal_node_inspect",)
OPERATION = {
    "id": "baremetal.node.inspect",
    "name": "Inspect operating-system disk identities",
    "description": "Boot a registered machine into RAM to read disk WWIDs and connected NICs. No disk writes or automatic installation. boot_now reboots the machine and leaves it powered off after its report.",
    "required_role": "operator", "backend": "baremetal", "mutating": True,
    "params": [p("node_id", True, "Registered node UUID"),
               p("boot_now", False, "Explicitly reboot into RAM inventory and wait for the report", "boolean")],
    "handler": "baremetal_node_inspect", "timeout_seconds": 900,
}


def run(self, handler, op, job, env, log, ctx, params, deadline, check_cancel,
        dry, timeout, gs_root, ans_root, extra_env, ssh_target, remote_env, executor, agent_env_id):
    node = self.db.get(BaremetalNode, str(params.get("node_id") or ""))
    if env is None or node is None or node.environment_id != env.id:
        return {"ok": False, "error": "Select a registered node in this environment", "returncode": 2}
    boot_now = params.get("boot_now") is True
    result = hardware_inspection.begin(self.db, env, node, dry_run=dry, log=log,
                                       settings=self.settings, boot_now=boot_now)
    self.db.commit()
    if not result.get("ok") or dry or not boot_now:
        return result
    stop = min(time.monotonic() + min(timeout, 900), deadline or float("inf"))
    while time.monotonic() < stop:
        check_cancel()
        self.db.refresh(node)
        if node.boot_stage == "inspected" and node.os_inventory_report:
            self.write_audit(actor=job.created_by or "system", action="node.inspect", resource_type="baremetal_node",
                             resource_id=node.id, environment_id=env.id, details={"disk_writes": False, "rebooted": True}, success=True)
            return {"ok": True, "node_id": node.id, "boot_stage": "inspected", "disk_writes": False}
        if node.next_boot != "inspect" or node.boot_stage != "inspecting":
            return {"ok": False, "error": "Inspection was replaced or cancelled", "returncode": 2}
        time.sleep(2)
    return {"ok": False, "error": "RAM inventory report did not arrive; inspect the selected NIC and provisioning network", "returncode": 2}
