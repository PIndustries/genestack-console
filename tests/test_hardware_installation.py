"""Installation review must use current, exact local disk identities."""

import copy
from datetime import UTC, datetime, timedelta

import pytest

from app.services.hardware_installation import preview
from tests.test_capacity_plan import machine


def fixture():
    node = machine()
    node.update(ip="10.77.27.191", mac="aa:bb:cc:00:00:01")
    node["os_inventory"] = {
        "source": "ram-inspection",
        "serial": "ACTUAL-SERIAL",
        "wipe": False,
        "hardware_modified": False,
        "observed_at": datetime.now(UTC).isoformat(),
        "nics": [{"mac": node["mac"], "link_up": True}],
        "disks": [
            {
                "name": "sda",
                "serial": "boot",
                "wwid": "naa.6000000000000001",
                "size_sectors": 128 * 1024**3 // 512,
                "remote": False,
                "readonly": False,
                "wiped": False,
            },
            {
                "name": "sdb",
                "serial": "data",
                "wwid": "naa.6000000000000002",
                "size_sectors": 1024**4 // 512,
                "remote": False,
                "readonly": False,
                "wiped": False,
            },
        ],
    }
    node["hardware"]["serial"] = "ACTUAL-SERIAL"
    doc = {
        "pxe": {"next_server": "10.77.27.1", "netmask": "255.255.255.0", "gateway": "10.77.27.1"},
        "secrets": {"never-return": {"data": {"value": "PRIVATE"}}},
    }
    return node, doc


def test_review_maps_physical_serials_to_verified_os_wwids_without_changes():
    node, doc = fixture()
    original = copy.deepcopy((node, doc))
    plan = preview([node], doc, 2)
    assert not plan["blockers"]
    assert plan["machines"][0]["disks"] == [
        {"wwid": "naa.6000000000000001", "capacity_bytes": 128 * 1024**3, "purpose": "boot"},
        {"wwid": "naa.6000000000000002", "capacity_bytes": 1024**4, "purpose": "storage"},
    ]
    assert (node, doc) == original
    assert "PRIVATE" not in str(plan)
    assert plan["hardware_modified"] is False and plan["configuration_changed"] is False


@pytest.mark.parametrize(
    "change",
    [
        lambda n, d: n.pop("os_inventory"),
        lambda n, d: n["os_inventory"].update(
            observed_at=(datetime.now(UTC) - timedelta(days=2)).isoformat()
        ),
        lambda n, d: n["os_inventory"]["disks"][0].update(remote=True),
        lambda n, d: n["os_inventory"]["disks"][0].update(readonly=True),
        lambda n, d: n["os_inventory"]["disks"][0].update(wwid="*"),
        lambda n, d: n["os_inventory"]["disks"][0].update(serial="different-disk"),
        lambda n, d: n.update(ip="10.77.27.1"),
        lambda n, d: n.update(ip="192.0.2.10"),
        lambda n, d: d.update(pxe={}),
    ],
)
def test_unverified_or_unusable_targets_block_installation(change):
    node, doc = fixture()
    change(node, doc)
    assert preview([node], doc, 2)["blockers"]


def test_review_fingerprint_changes_with_config_or_evidence():
    node, doc = fixture()
    original = preview([node], doc, 2)["fingerprint"]
    assert preview([node], doc, 3)["fingerprint"] != original
    node["os_inventory"]["disks"][1]["wwid"] = "naa.6000000000000003"
    assert preview([node], doc, 2)["fingerprint"] != original


def test_unexpected_local_disk_is_not_silently_left_out():
    node, doc = fixture()
    extra = dict(
        node["os_inventory"]["disks"][1],
        name="sdc",
        serial="unexpected",
        wwid="naa.6000000000000003",
    )
    node["os_inventory"]["disks"].append(extra)
    assert any("additional local disks" in b for b in preview([node], doc, 2)["blockers"])


def test_logical_raid_identity_is_reconciled_with_os_prefix():
    node, doc = fixture()
    node["hardware"]["logical_volumes"] = [
        {
            "unique_identifier": d["wwid"].removeprefix("naa."),
            "capacity_bytes": d["size_sectors"] * 512,
            "health": "OK",
            "state": "Enabled",
        }
        for d in node["os_inventory"]["disks"]
    ]
    plan = preview([node], doc, 2)
    assert not plan["blockers"]
    assert plan["machines"][0]["disks"][0]["wwid"] == "naa.6000000000000001"


def test_duplicate_target_disk_across_machines_blocks_plan():
    node, doc = fixture()
    other = copy.deepcopy(node)
    other.update(id="b", name="second", ip="10.77.27.192", mac="aa:bb:cc:00:00:02")
    other["hardware"].update(serial="SECOND")
    other["hardware"]["nics"][0]["mac"] = other["mac"]
    other["os_inventory"].update(serial="SECOND", nics=[{"mac": other["mac"], "link_up": True}])
    assert any("unique across" in b for b in preview([node, other], doc, 2)["blockers"])


def test_empty_cloud_review_does_not_create_config_or_jobs(client, admin_headers):
    from tests.test_baremetal import _create_env

    env = _create_env(client, admin_headers, dry_run=False)
    base = f"/api/v1/environments/{env['id']}"
    before = client.get(base + "/config", headers=admin_headers).json()
    response = client.get(base + "/baremetal/installation-plan", headers=admin_headers)
    assert response.status_code == 200
    plan = response.json()
    assert plan["config_version"] == 0 and plan["machines"] == [] and plan["blockers"]
    assert client.get(base + "/config", headers=admin_headers).json() == before
    assert (
        client.get(
            "/api/v1/jobs", params={"environment_id": env["id"]}, headers=admin_headers
        ).json()
        == []
    )
