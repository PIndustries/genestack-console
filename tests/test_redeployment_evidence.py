"""Acceptance must reject backend-only success and partial hardware rebuilds."""
import copy
import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "redeployment_evidence", Path(__file__).parents[1] / "scripts/check_redeployment_evidence.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


@pytest.fixture
def complete_receipt():
    """Synthetic checker fixture, never a claim that a hardware test ran."""
    return {
        "targets": [{"name": "node-a", "mac": "aa:bb:cc:00:00:01"}],
        "off_cluster_restore_verified": True,
        "app_runs": [
            {"platform": p, "driver": "native_ui", "artifact": f"fixture/{p}.xcresult",
             "actions": sorted(module.SETUP_ACTIONS), "live_progress": True}
            for p in ["macos", "ios", "ipados", "watchos", "tvos"]
        ],
        "job": {"id": "job-fixture", "status": "success", "dry_run": False,
                "stages": [{"id": s, "status": "success"} for s in module.REQUIRED_STAGES]},
        "old_cluster_uid": "old", "new_cluster_uid": "new",
        "nodes": [{"name": "node-a", "mac": "aa:bb:cc:00:00:01", "wiped_at": 100,
                   "talos_served_at": 101, "ready": True, "capacity_provenance": "fixture",
                   "allocation": {"cpu": 4, "memory_gib": 8}}],
        "automatic_plan": {"manual_roles_required": False, "all_eligible_capacity_accounted": True,
                           "system_disks_reserved": True, "system_headroom_reserved": True},
        "openstack": {k: "passed" for k in ["identity", "image", "placement", "compute", "network", "block_storage"]},
        "workload": {k: True for k in ["vm_active", "dhcp", "dns", "egress", "floating_ip", "http_200", "php",
                                       "database_roundtrip", "volume_survives_reboot", "cleanup"]},
        "watch_notification": {"transport": "apns", "display_confirmed": True},
    }


def test_empty_evidence_cannot_pass():
    assert module.check({})


def test_complete_contract_fixture(complete_receipt):
    assert module.check(complete_receipt) == []


@pytest.mark.parametrize("change", [
    lambda r: r.update(app_runs=[]),
    lambda r: r["app_runs"][0].update(driver="api_only"),
    lambda r: r["app_runs"][1].update(manual_cli_steps=["ssh configure"]),
    lambda r: r["app_runs"][2].update(actions=["create_environment"]),
    lambda r: r["job"].update(dry_run=True),
    lambda r: r["job"].update(stages=[]),
    lambda r: r.update(new_cluster_uid="old"),
    lambda r: r["nodes"][0].update(talos_served_at=99),
    lambda r: r["nodes"][0].update(ready=False),
    lambda r: r["nodes"][0].update(mac="aa:bb:cc:00:00:02"),
    lambda r: r["nodes"].append({"name": "unapproved-node"}),
    lambda r: r.update(off_cluster_restore_verified=False),
    lambda r: r["automatic_plan"].update(manual_roles_required=True),
    lambda r: r["automatic_plan"].update(system_disks_reserved=False),
    lambda r: r["workload"].update(http_200=False),
    lambda r: r["workload"].update(database_roundtrip=False),
    lambda r: r["workload"].update(cleanup=False),
    lambda r: r["watch_notification"].update(transport="simctl"),
    lambda r: r["watch_notification"].update(display_confirmed=False),
])
def test_incomplete_proof_rejected(complete_receipt, change):
    receipt = copy.deepcopy(complete_receipt)
    change(receipt)
    assert module.check(receipt)
