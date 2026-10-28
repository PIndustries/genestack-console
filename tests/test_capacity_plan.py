"""Capacity, identity and staleness regressions for automatic role proposals."""

import copy
from datetime import UTC, datetime, timedelta

import pytest

from app.services.capacity import propose


def machine(name="not-a-role", identity="a", memory=128, threads=32):
    return {
        "id": identity,
        "name": name,
        "ip": "192.0.2." + str(10 + ord(identity[0])),
        "mac": "aa:bb:cc:00:00:" + format(ord(identity[0]), "02x"),
        "hardware": {
            "source": "redfish",
            "observed_at": datetime.now(UTC).isoformat(),
            "cpu_threads": threads,
            "memory_gib": memory,
            "nics": [{"mac": "aa:bb:cc:00:00:01", "link_status": "LinkUp"}],
            "drives": [
                {
                    "serial": "boot",
                    "capacity_bytes": 128 * 1024**3,
                    "health": "OK",
                    "state": "Enabled",
                },
                {
                    "serial": "data",
                    "capacity_bytes": 1024 * 1024**3,
                    "health": "OK",
                    "state": "Enabled",
                },
            ],
        },
    }


def test_same_hardware_same_roles_despite_names():
    a = propose([machine("controller")])
    b = propose([machine("storage")])
    assert a["allocations"][0]["proposed_roles"] == b["allocations"][0]["proposed_roles"]
    assert a["blockers"] == []
    assert a["allocations"][0]["proposed_boot_disk_serial"] == "boot"
    assert a["allocations"][0]["candidate_data_disks"][0]["serial"] == "data"
    assert a["candidate_vm_memory_gib"] == 120
    assert a["candidate_vm_threads"] == 30


def test_all_eligible_machines_contribute_and_three_control_planes():
    nodes = [machine(identity=str(i), memory=64 + i * 16) for i in range(5)]
    plan = propose(nodes)
    assert len(plan["allocations"]) == 5
    assert plan["control_plane_count"] == 3
    controls = {n["id"] for n in plan["allocations"] if "control" in n["proposed_roles"]}
    assert controls == {"2", "3", "4"}
    assert all(
        "compute" in n["proposed_roles"] and "storage" in n["proposed_roles"]
        for n in plan["allocations"]
    )


def test_two_hosts_do_not_default_to_two_member_quorum():
    assert propose([machine(identity="a"), machine(identity="b")])["control_plane_count"] == 1


@pytest.mark.parametrize(
    "change",
    [
        lambda n: n["hardware"].update(cpu_threads=None),
        lambda n: n["hardware"].update(memory_gib=float("nan")),
        lambda n: n["hardware"].update(memory_gib=True),
        lambda n: n["hardware"].update(
            observed_at=(datetime.now(UTC) - timedelta(days=2)).isoformat()
        ),
        lambda n: n["hardware"].update(
            observed_at=datetime.now(UTC).replace(tzinfo=None).isoformat()
        ),
        lambda n: n["hardware"].update(source="guessed"),
        lambda n: n["hardware"].update(nics=[]),
        lambda n: n["hardware"].update(drives=[]),
        lambda n: n.update(ip=None),
    ],
)
def test_unknown_or_unusable_hardware_blocks_proposal(change):
    node = machine()
    change(node)
    assert propose([node])["blockers"]


def test_plan_does_not_change_input_or_consume_boot_disk():
    node = machine()
    before = copy.deepcopy(node)
    plan = propose([node])
    assert node == before
    assert plan["hardware_modified"] is False
    assert plan["configuration_changed"] is False
    assert all(d["serial"] != "boot" for d in plan["allocations"][0]["candidate_data_disks"])


def test_empty_environment_api_flow_discovers_capacity(client, admin_headers, monkeypatch):
    from app.modules.baremetal import hardware_discover
    from tests.test_baremetal import _create_env

    def _submit(client, headers, env_id, operation, params=None):
        response = client.post(
            "/api/v1/jobs",
            headers=headers,
            json={
                "environment_id": env_id,
                "operation": operation,
                "params": params or {},
                "run_sync": True,
            },
        )
        assert response.status_code == 201, response.text
        return response.json()

    env = _create_env(client, admin_headers, dry_run=False)
    register = _submit(
        client,
        admin_headers,
        env["id"],
        "baremetal.node.register",
        {
            "name": "arbitrary-machine",
            "bmc_host": "bmc.example.test",
            "bmc_username": "admin",
            "bmc_password": "test-only",
            "pxe_mac": "aa:bb:cc:00:00:01",
            "expected_ip": "192.0.2.10",
        },
    )
    assert register["status"] == "success", register.get("error")
    monkeypatch.setattr(
        hardware_discover, "hardware_inventory", lambda *args, **kwargs: machine()["hardware"]
    )
    job = _submit(client, admin_headers, env["id"], "baremetal.hardware.discover")
    assert job["status"] == "success", job.get("error")
    result = client.get(
        f"/api/v1/environments/{env['id']}/baremetal/capacity-plan", headers=admin_headers
    )
    assert result.status_code == 200
    assert result.json()["blockers"] == []
    assert result.json()["allocations"][0]["name"] == "arbitrary-machine"
    current = client.get(f"/api/v1/environments/{env['id']}/config", headers=admin_headers).json()
    assert current["version"] is None


def test_mirrored_physical_members_are_not_counted_as_separate_os_disks():
    node = machine()
    node["hardware"]["logical_volumes"] = [{
        "unique_identifier": "60000000000000000000000000000001",
        "capacity_bytes": 128 * 1024**3, "health": "OK", "state": "Enabled",
        "physical_drive_serials": ["boot", "data"], "os_mapping_verified": False,
    }]
    plan = propose([node])
    allocation = plan["allocations"][0]
    assert allocation["proposed_boot_disk_serial"] is None
    assert allocation["proposed_boot_disk_identifier"] == "60000000000000000000000000000001"
    assert allocation["disk_identity_kind"] == "logical_volume"
    assert allocation["candidate_data_disks"] == []
    assert "storage" not in allocation["proposed_roles"]
    assert any("OS-visible" in blocker for blocker in plan["blockers"])


def test_two_logical_disks_use_persistent_identifiers_not_member_serials():
    node = machine()
    node["hardware"]["logical_volumes"] = [
        {"unique_identifier": "volume-a", "capacity_bytes": 128 * 1024**3, "health": "OK", "state": "Enabled"},
        {"unique_identifier": "volume-b", "capacity_bytes": 1024 * 1024**3, "health": "OK", "state": "Enabled"},
    ]
    allocation = propose([node])["allocations"][0]
    assert allocation["proposed_boot_disk_identifier"] == "volume-a"
    assert allocation["candidate_data_disks"] == [{"serial": None, "unique_identifier": "volume-b", "capacity_bytes": 1024 * 1024**3}]


def test_unknown_logical_disk_identity_does_not_fall_back_to_raid_members():
    node = machine()
    node["hardware"]["logical_volumes"] = [{"capacity_bytes": 1024**4, "health": "OK", "state": "Enabled"}]
    plan = propose([node])
    assert plan["blockers"]
    assert plan["allocations"][0]["proposed_boot_disk_serial"] is None
    assert plan["allocations"][0]["proposed_boot_disk_identifier"] is None
    assert plan["allocations"][0]["candidate_data_disks"] == []


@pytest.mark.parametrize("raid_type", ["1", "RAID1", "1+0", "RAID10"])
@pytest.mark.parametrize("os_verified", [False, True])
def test_dfw_mirrored_system_volume_wins_equal_capacity_raid0_tiebreak(raid_type, os_verified):
    # The real DFW storage machines have a firmware boot entry for the RAID 1
    # logical volume and four equally sized RAID 0 data volumes. Sorting only
    # capacity/WWID incorrectly selected the third logical drive as the OS disk.
    node = machine(name="arbitrary-name")
    mirror = "600508b1001cef0ab851d126b6b3b611"
    identities = [mirror, "600508b1001cf4dccb56aab04725af5c",
                  "600508b1001c2b3a24c9aadba135def4", "600508b1001cc606b6a5da4e861dcd57",
                  "600508b1001ce07721ab8f4c7e0288cb"]
    node["hardware"].update(serial="physical-machine", nics=[{"mac": node["mac"], "link_status": "LinkUp"}],
        logical_volumes=[{"unique_identifier": identity, "raid_type": raid_type if identity == mirror else "0",
                          "capacity_bytes": 400054812672, "health": "OK", "state": "Enabled"}
                         for identity in identities])
    if os_verified:
        node["os_inventory"] = {"source": "ram-inspection", "observed_at": datetime.now(UTC).isoformat(),
            "serial": "physical-machine", "hardware_modified": False, "wipe": False,
            "nics": [{"mac": node["mac"], "link_up": True}],
            "disks": [{"name": f"sd{index}", "wwid": "naa."+identity, "size_sectors": 781357232,
                       "remote": False, "readonly": False, "wiped": False}
                      for index, identity in enumerate(identities)]}
    before = copy.deepcopy(node)
    allocation = propose([node])["allocations"][0]
    prefix = "naa." if os_verified else ""
    assert allocation["proposed_boot_disk_identifier"] == prefix+mirror
    assert {d["unique_identifier"] for d in allocation["candidate_data_disks"]} == {
        prefix+identity for identity in identities if identity != mirror}
    assert len(allocation["candidate_data_disks"]) == 4
    assert allocation["os_inventory_verified"] is os_verified
    assert node == before, "Proposing the existing mirror must not reconfigure RAID"


def test_dfw_controller3_keeps_the_larger_firmware_boot_mirror_for_system():
    # This real machine's firmware boot order places its 3 TB RAID 1 LUN
    # before the 800 GB RAID 0 LUN. Capacity alone is not the boot preference.
    node = machine(name="no-role-inference")
    mirror = "600508b1001c72fb48dc1742e16aa183"
    striped = "600508b1001cc255e7d2a290e265dc1b"
    node["hardware"]["logical_volumes"] = [
        {"unique_identifier": striped, "raid_type": "0", "capacity_bytes": 800109625344,
         "health": "OK", "state": "Enabled"},
        {"unique_identifier": mirror, "raid_type": "1", "capacity_bytes": 3000558944256,
         "health": "OK", "state": "Enabled"},
    ]
    allocation = propose([node])["allocations"][0]
    assert allocation["proposed_boot_disk_identifier"] == mirror
    assert allocation["candidate_data_disks"] == [{"serial": None, "unique_identifier": striped,
                                                  "capacity_bytes": 800109625344}]
