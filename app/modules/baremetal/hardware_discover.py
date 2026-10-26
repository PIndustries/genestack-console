"""Non-destructive hardware discovery as a normal, auditable Console job."""

from __future__ import annotations

from sqlalchemy import select

from app.models import BaremetalNode
from app.services.crypto import decrypt_secret
from app.services.job_runner import clamp_deadline_timeout
from app.services.redfish import RedfishError, hardware_inventory

HANDLERS = ("baremetal_hardware_discover",)
OPERATION = {
    "id": "baremetal.hardware.discover",
    "name": "Discover hardware capacity",
    "description": "Read registered machines' CPU, memory, physical disks and NICs. No power, boot or disk changes.",
    "required_role": "operator",
    "backend": "baremetal",
    "params": [],
    "handler": "baremetal_hardware_discover",
    # Stores inventory evidence; performs no hardware mutation.
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
            "error": "Select an environment before hardware discovery",
            "returncode": 2,
        }
    nodes = self.db.scalars(
        select(BaremetalNode)
        .where(BaremetalNode.environment_id == env.id)
        .order_by(BaremetalNode.name)
    ).all()
    if not nodes:
        return {"ok": False, "error": "Register the environment's BMCs first", "returncode": 2}
    failed = []

    def check():
        check_cancel()
        clamp_deadline_timeout(10, deadline)

    for node in nodes:
        check()
        if dry:
            log(f"[dry-run] would read hardware for {node.name}")
            continue
        try:
            report = hardware_inventory(
                node.bmc_host, node.bmc_username,
                decrypt_secret(node.bmc_password, self.settings), check=check
            )
            node.hardware_report = report
            self.db.add(node)
            self.db.commit()
            log(
                f"[hardware] {node.name}: threads={report['cpu_threads']} memory_gib={report['memory_gib']} drives={len(report['drives'])}"
            )
        except RedfishError:
            # Remove obsolete evidence rather than presenting last run as new discovery.
            node.hardware_report = None
            self.db.add(node)
            self.db.commit()
            failed.append(node.name)
            log(f"[hardware] {node.name}: discovery unavailable")
    self.write_audit(
        actor=job.created_by or "system",
        action="env.hardware.discover",
        resource_type="environment",
        resource_id=env.id,
        environment_id=env.id,
        details={"machine_count": len(nodes), "failed_count": len(failed), "dry_run": dry},
        success=not failed,
    )
    return {
        "ok": not failed,
        "returncode": int(bool(failed)),
        "dry_run": dry,
        "machine_count": len(nodes),
        "failed": failed,
        "hardware_modified": False,
    }
