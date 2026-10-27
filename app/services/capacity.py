"""A read-only, measured capacity proposal. Does not change roles or disks."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import BaremetalNode, Environment

MAX_REPORT_AGE = timedelta(hours=24)


def propose(nodes: list[dict[str, Any]], *, now: datetime | None = None) -> dict[str, Any]:
    """Propose converged roles using capacity, never a machine's name.

    This output is deliberately separate from install configuration: BMC disk
    serials must still be matched to the OS-visible disks before provisioning.
    No raw or reserved capacity is advertised as installed VM capacity.
    """
    now = now or datetime.now(UTC)
    candidates, blockers, warnings = [], [], []
    for key, label in [("id", "Machine identity"), ("ip", "Provisioning IP"), ("mac", "PXE MAC")]:
        values = [str(n[key]).lower() for n in nodes if n.get(key)]
        if len(values) != len(set(values)):
            blockers.append(f"{label} is duplicated in the target inventory")
    if not nodes:
        blockers.append("Register the environment's machines and discover their hardware")
    for node in nodes:
        name = node["name"]
        report = node.get("hardware") or {}
        try:
            observed = datetime.fromisoformat(report["observed_at"])
            if observed.tzinfo is None or not timedelta(0) <= now - observed <= MAX_REPORT_AGE:
                raise ValueError("stale")
            cpu, ram = report["cpu_threads"], report["memory_gib"]
            if (
                report.get("source") != "redfish"
                or isinstance(cpu, bool)
                or isinstance(ram, bool)
                or not isinstance(cpu, int)
                or cpu < 1
                or not isinstance(ram, (int, float))
                or not math.isfinite(ram)
                or ram <= 0
            ):
                raise ValueError("missing capacity")
        except (KeyError, TypeError, ValueError):
            blockers.append(f"{name}: fresh measured CPU and memory are required")
            continue
        if cpu < 4 or ram < 8:
            warnings.append(f"{name}: below the initial compute minimum (4 threads, 8 GiB)")
            continue
        if not node.get("ip") or not node.get("mac"):
            blockers.append(f"{name}: provisioning IP and PXE MAC are required")
        up_nics = [
            n for n in report.get("nics", []) if n.get("link_status") == "LinkUp" and n.get("mac")
        ]
        if not up_nics:
            blockers.append(f"{name}: a verified connected network interface is required")
        logical = report.get("logical_volumes") or []
        if logical:
            # A RAID member's serial is not an installable OS disk. Only whole
            # logical volumes may contribute, and OS discovery must reconcile them.
            disk_inventory = [dict(d, serial=d.get("unique_identifier")) for d in logical]
            blockers.append(f"{name}: verify logical-volume identifiers against OS-visible disks before installation")
        else:
            disk_inventory = report.get("drives", [])
        healthy = [
            d
            for d in disk_inventory
            if d.get("health") == "OK"
            and d.get("state") == "Enabled"
            and d.get("serial")
            and isinstance(d.get("capacity_bytes"), (int, float))
            and not isinstance(d.get("capacity_bytes"), bool)
            and math.isfinite(d["capacity_bytes"])
            and d["capacity_bytes"] >= 64 * 1024**3
        ]
        healthy.sort(key=lambda d: (d["capacity_bytes"], d["serial"]))
        if not healthy:
            blockers.append(f"{name}: a healthy, uniquely identified boot disk is required")
        serials = [d["serial"] for d in healthy]
        if len(serials) != len(set(serials)):
            blockers.append(f"{name}: disk serials are ambiguous")
        candidates.append(
            {
                "id": node["id"],
                "name": name,
                "ip": node.get("ip"),
                "mac": node.get("mac"),
                "cpu_threads": cpu,
                "memory_gib": ram,
                "observed_at": report["observed_at"],
                "disks": healthy,
                "logical_storage": bool(logical),
                "connected_nics": up_nics,
            }
        )
    control_eligible = [n for n in candidates if n["memory_gib"] >= 16]
    control_eligible.sort(key=lambda n: (-n["memory_gib"], -n["cpu_threads"], n["id"]))
    control_count = 3 if len(control_eligible) >= 3 else min(1, len(control_eligible))
    controls = {n["id"] for n in control_eligible[:control_count]}
    if not controls:
        blockers.append(
            "At least one measured control-plane-capable machine is required (4 threads, 16 GiB)"
        )
    elif control_count < 3:
        warnings.append(
            "Single-control-plane proposal; high availability requires three eligible machines"
        )
    allocations = []
    for node in candidates:
        cp = node["id"] in controls
        roles = ["compute"]
        if node["connected_nics"]:
            roles.append("network")
        if cp:
            roles += ["k8s_control_plane", "etcd", "control"]
        boot = node["disks"][0] if node["disks"] else None
        data = node["disks"][1:]
        if data:
            roles.append("storage")
        reserved_cpu, reserved_ram = (2, 8) if cp else (1, 4)
        allocations.append(
            {
                "id": node["id"],
                "name": node["name"],
                "ip": node["ip"],
                "mac": node["mac"],
                "proposed_roles": roles,
                "cpu_threads": node["cpu_threads"],
                "memory_gib": node["memory_gib"],
                "system_reserved_threads": reserved_cpu,
                "system_reserved_memory_gib": reserved_ram,
                "candidate_vm_threads": node["cpu_threads"] - reserved_cpu,
                "candidate_vm_memory_gib": node["memory_gib"] - reserved_ram,
                "proposed_boot_disk_serial": boot["serial"] if boot and not node["logical_storage"] else None,
                "proposed_boot_disk_identifier": boot["serial"] if boot and node["logical_storage"] else None,
                "disk_identity_kind": "logical_volume" if node["logical_storage"] else "physical_drive",
                "candidate_data_disks": [
                    {"serial": None if node["logical_storage"] else d["serial"],
                     "unique_identifier": d["serial"] if node["logical_storage"] else None,
                     "capacity_bytes": d["capacity_bytes"]} for d in data
                ],
                "measurement_time": node["observed_at"],
            }
        )
    return {
        "allocations": allocations,
        "blockers": blockers,
        "warnings": warnings,
        "control_plane_count": control_count,
        "hardware_modified": False,
        "configuration_changed": False,
        "manual_roles_required": False,
        "candidate_vm_threads": sum(n["candidate_vm_threads"] for n in allocations),
        "candidate_vm_memory_gib": sum(n["candidate_vm_memory_gib"] for n in allocations),
        "next_steps": [
            "Verify OS-visible boot/data disk mapping and existing-data disposition",
            "Verify external network, VLAN/MTU, addressing and storage replication",
            "Review and save the final installation configuration before deployment",
        ],
    }


def for_environment(db: Session, env: Environment) -> dict[str, Any]:
    rows = db.scalars(
        select(BaremetalNode)
        .where(BaremetalNode.environment_id == env.id)
        .order_by(BaremetalNode.id)
    ).all()
    return propose(
        [
            {
                "id": n.id,
                "name": n.name,
                "ip": n.expected_ip,
                "mac": n.pxe_mac,
                "hardware": n.hardware_report,
            }
            for n in rows
        ]
    )
