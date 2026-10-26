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
