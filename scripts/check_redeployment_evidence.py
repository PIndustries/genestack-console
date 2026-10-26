#!/usr/bin/env python3
"""Check an app-driven rebuild's evidence. Never provision or modify a host.

The UI driver and independent probes supply this receipt. A passing backend
job, a dry run, or a simulated payload alone cannot satisfy the contract.
This checker validates completeness/consistency; it does not authenticate the
receipt or replace the actual UI tests and live probes that generate it.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

SETUP_ACTIONS = {
    "create_environment", "link_deployer", "enter_network", "register_hardware",
    "discover_capacity", "review_automatic_plan", "start_deployment",
    "follow_progress", "inspect_cluster", "create_vm_network", "create_lamp_vm",
    "verify_http", "inspect_failure", "resume", "cleanup_test_resources",
}
REQUIRED_STAGES = {
    "commission", "talos", "hosts", "infrastructure", "operators", "cni",
    "core", "compute-network", "platform-extras", "observability", "testing",
}


def check(receipt: dict) -> list[str]:
    """Return every missing acceptance item; an empty receipt always fails."""
    errors: list[str] = []
    targets = receipt.get("targets") or []
    names = [node.get("name") for node in targets]
    if not names or any(not name for name in names) or len(set(names)) != len(names):
        errors.append("Exact unique target machines are required")
    macs = [node.get("mac") for node in targets]
    if any(not mac for mac in macs) or len(set(macs)) != len(macs):
        errors.append("Exact unique target PXE MACs are required")
    if not receipt.get("off_cluster_restore_verified"):
        errors.append("Off-cluster backup restore has not been verified")
    if receipt.get("unresolved_preflight_blockers"):
        errors.append("Preflight blockers remain")

    runs = receipt.get("app_runs") or []
    # Each authoring platform must exercise the journey. Watch and TV have a
    # separate monitoring/notification contract, rather than invented parity.
    for platform in ("macos", "ios", "ipados"):
        run = next((run for run in runs if run.get("platform") == platform), {})
        missing = SETUP_ACTIONS - set(run.get("actions") or [])
        if run.get("driver") != "native_ui" or not run.get("artifact") or missing:
            errors.append(f"{platform}: native app journey incomplete")
        if run.get("manual_cli_steps"):
            errors.append(f"{platform}: setup required manual command-line steps")

    job = receipt.get("job") or {}
    if not job.get("id") or job.get("status") != "success" or job.get("dry_run") is not False:
        errors.append("Successful real deployment job is required")
    completed = {s.get("id") for s in job.get("stages", []) if s.get("status") == "success"}
    if REQUIRED_STAGES - completed:
        errors.append("Deployment stages lack successful evidence")
    old_uid, new_uid = receipt.get("old_cluster_uid"), receipt.get("new_cluster_uid")
    if not old_uid or not new_uid or old_uid == new_uid:
        errors.append("Fresh cluster identity has not been proven")

    observed = receipt.get("nodes") or []
    if len(observed) != len(targets) or {n.get("name") for n in observed} != set(names):
        errors.append("Observed nodes do not match the exact rebuild target set")
    for target in targets:
        node = next((n for n in observed if n.get("name") == target.get("name")), {})
        if node.get("mac") != target.get("mac"):
            errors.append(f"{target.get('name')}: hardware identity differs")
        wipe, served = node.get("wiped_at"), node.get("talos_served_at")
        if not isinstance(wipe, (int, float)) or not isinstance(served, (int, float)) or served <= wipe:
            errors.append(f"{target.get('name')}: fresh installation has not been proven")
        if node.get("ready") is not True:
            errors.append(f"{target.get('name')}: rebuilt node Ready state is unverified")
        if not node.get("capacity_provenance") or not node.get("allocation"):
            errors.append(f"{target.get('name')}: discovered capacity/allocation missing")

    plan = receipt.get("automatic_plan") or {}
    if plan.get("manual_roles_required") is not False or plan.get("all_eligible_capacity_accounted") is not True:
        errors.append("Automatic hardware allocation has not been verified")
    if plan.get("system_disks_reserved") is not True or plan.get("system_headroom_reserved") is not True:
        errors.append("System disk and resource reservations have not been verified")
    cloud = receipt.get("openstack") or {}
    for field in ("identity", "image", "placement", "compute", "network", "block_storage"):
        if cloud.get(field) != "passed":
            errors.append(f"OpenStack {field} verification missing")
    workload = receipt.get("workload") or {}
    for field in ("vm_active", "dhcp", "dns", "egress", "floating_ip", "http_200",
                  "php", "database_roundtrip", "volume_survives_reboot", "cleanup"):
        if workload.get(field) is not True:
            errors.append(f"LAMP/network verification missing: {field}")
    for platform in ("watchos", "tvos"):
        run = next((run for run in runs if run.get("platform") == platform), {})
        if run.get("driver") != "native_ui" or not run.get("live_progress") or not run.get("artifact"):
            errors.append(f"{platform}: live app progress unverified")
    watch = receipt.get("watch_notification") or {}
    if watch.get("transport") != "apns" or watch.get("display_confirmed") is not True:
        errors.append("Real Watch notification display unverified")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("receipt", type=Path)
    args = parser.parse_args()
    errors = check(json.loads(args.receipt.read_text()))
    print(json.dumps({"accepted": not errors, "missing": errors}, indent=2))
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
