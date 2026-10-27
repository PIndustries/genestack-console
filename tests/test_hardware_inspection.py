"""Read-only RAM reports cannot become permission to erase or install."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.services import baremetal, bootselect, hardware_inspection
from tests.test_commission_disk_allowlist import MAC, A, _stored_node


def report():
    return {"wipe": False, "serial": "GS-INSPECT-20261027", "token": "not-stored",
            "nics": [{"mac": MAC, "link_up": True}],
            "disks": [{"name": "sda", "wwid": A, "size_sectors": 131072,
                       "wiped": False, "remote": False, "readonly": False}]}


def node():
    return SimpleNamespace(hardware_report={"serial": "GS-INSPECT-20261027", "nics": [{"mac": MAC}]},
                           pxe_mac=MAC, id="node", next_boot="disk", boot_stage="new")


@pytest.mark.parametrize("change", [
    {"wipe": True}, {"serial": "another-machine"}, {"nics": []}, {"disks": []},
])
def test_inspection_rejects_write_claims_and_wrong_hardware(change):
    body = report(); body.update(change)
    assert hardware_inspection.validate_report(body, node())[0] is False


@pytest.mark.parametrize("field,value", [("wiped", True), ("wiped", None), ("size_sectors", True),
                                          ("size_sectors", -1), ("remote", None), ("readonly", "false")])
def test_report_requires_complete_measured_disk_facts(field, value):
    body = report(); body["disks"][0][field] = value
    assert hardware_inspection.validate_report(body, node())[0] is False


def test_duplicate_disk_id_is_not_accepted_as_two_local_disks():
    body = report(); body["disks"].append({**deepcopy(body["disks"][0]), "name": "sdb"})
    assert hardware_inspection.validate_report(body, node())[0] is False


def test_preparation_dry_run_does_not_change_boot_or_prepare_pxe(monkeypatch):
    target = node(); prepare = Mock(side_effect=AssertionError("dry run must not prepare PXE"))
    monkeypatch.setattr(baremetal, "_prepare_pxe", prepare)
    result = hardware_inspection.begin(Mock(), SimpleNamespace(), target, dry_run=True, log=lambda _: None, boot_now=True)
    assert result["ok"] and result["dry_run"]
    assert target.next_boot == "disk"
    prepare.assert_not_called()


def test_unknown_serial_cannot_bind_operating_system_inventory():
    target = node(); target.hardware_report["serial"] = "unknown"
    assert not hardware_inspection.validate_report(report(), target)[0]


def test_prepare_failure_revokes_the_boot_token(monkeypatch):
    target = node()
    monkeypatch.setattr(baremetal, "_prepare_pxe", lambda *a: {"ok": False, "error": "fixture unavailable"})
    boot = Mock(side_effect=AssertionError("failed preparation must not reboot"))
    monkeypatch.setattr(baremetal, "pxe_boot", boot)
    result = hardware_inspection.begin(Mock(), SimpleNamespace(), target, dry_run=False, log=lambda _: None, boot_now=True)
    assert result["ok"] is False and target.next_boot == "disk" and target.commission_token is None
    boot.assert_not_called()


def test_actual_database_report_never_marks_machine_wiped_or_serves_talos(tmp_path):
    import uuid

    from app.db import SessionLocal
    from app.models import BaremetalNode

    token = uuid.uuid4().hex; identity = _stored_node(token)
    with SessionLocal() as db:
        target = db.get(BaremetalNode, identity)
        target.next_boot = "inspect"; target.boot_stage = "inspecting"
        target.hardware_report = node().hardware_report
        db.commit()
    directory = tmp_path / "mac"; directory.mkdir()
    filename = MAC.replace(":", "-") + ".ipxe"
    script = bootselect.render_profile_ipxe("inspect", "http://192.0.2.1", token, node_id=identity)
    (directory / filename).write_text(script)
    assert "gsc_wipe=0" in script and "gsc_wipe=1" not in script and "gsc_allow_wwids=" not in script
    assert baremetal.guarded_commission_profile(str(tmp_path), "/mac/" + filename) == script
    assert baremetal.ingest_commission(token, report())["ok"]
    with SessionLocal() as db:
        target = db.get(BaremetalNode, identity)
        assert target.boot_stage == "inspected" and target.next_boot == "disk"
        assert target.wiped_at is None and target.talos_served_at is None
        assert target.commission_token is None
        assert target.os_inventory_report["disks"][0]["wwid"] == A
        assert "token" not in target.os_inventory_report
    assert "# profile: disk" in baremetal.guarded_commission_profile(str(tmp_path), "/mac/" + filename)
    assert not baremetal.ingest_commission(token, report())["ok"]


def test_inspection_profile_with_wipe_override_is_never_served(tmp_path):
    import uuid

    token = uuid.uuid4().hex; identity = _stored_node(token)
    directory = tmp_path / "mac"; directory.mkdir()
    filename = MAC.replace(":", "-") + ".ipxe"
    script = bootselect.render_profile_ipxe("inspect", "http://192.0.2.1", token, node_id=identity)
    (directory / filename).write_text(script.replace("gsc_wipe=0", "gsc_wipe=0 gsc_wipe=1"))
    assert "# profile: disk" in baremetal.guarded_commission_profile(str(tmp_path), "/mac/" + filename)


def test_ram_identity_reconciliation_uses_local_os_wwids_and_carrier():
    from datetime import UTC, datetime

    from app.services.capacity import propose
    from tests.test_capacity_plan import machine

    target = machine(); target["mac"] = MAC
    target["hardware"]["serial"] = report()["serial"]
    target["hardware"]["nics"] = [{"mac": MAC, "link_status": None}]
    target["hardware"]["logical_volumes"] = [{"unique_identifier": A.removeprefix("naa."),
        "capacity_bytes": 128 * 1024**3, "health": "OK", "state": "Enabled"}]
    body = report(); body["disks"][0]["size_sectors"] = 128 * 1024**3 // 512
    body.update(source="ram-inspection", observed_at=datetime.now(UTC).isoformat(), hardware_modified=False)
    target["os_inventory"] = body
    plan = propose([target])
    assert plan["blockers"] == []
    assert plan["allocations"][0]["proposed_boot_disk_identifier"] == A
    assert plan["allocations"][0]["os_inventory_verified"] is True
    target["os_inventory"]["disks"][0]["remote"] = True
    assert propose([target])["blockers"]


@pytest.mark.parametrize("change", [
    {"source": "guessed"}, {"observed_at": "2020-01-01T00:00:00+00:00"},
    {"hardware_modified": True}, {"serial": "another-machine"},
])
def test_stale_or_mismatched_inventory_does_not_clear_identity_gates(change):
    from datetime import UTC, datetime

    target = {"hardware": node().hardware_report, "mac": MAC, "os_inventory": report()}
    target["os_inventory"].update(source="ram-inspection", observed_at=datetime.now(UTC).isoformat(), hardware_modified=False)
    target["os_inventory"].update(change)
    assert hardware_inspection.evidence(target, now=datetime.now(UTC)) is None


def test_malformed_nics_cannot_crash_capacity_reconciliation():
    body = report(); body["nics"].append("not-an-interface")
    assert not hardware_inspection.validate_report(body, node())[0]


def test_inspection_job_prepares_only_until_reboot_is_explicit(client, admin_headers, monkeypatch):
    from app.db import SessionLocal
    from app.models import BaremetalNode
    from tests.test_baremetal import _create_env, _make_node, _submit

    env = _create_env(client, admin_headers, dry_run=False)
    identity, _ = _make_node(env["id"], pxe_mac=MAC, hardware_report=node().hardware_report)
    monkeypatch.setattr(baremetal, "_prepare_pxe", lambda *a: {"ok": True})
    power = Mock(side_effect=AssertionError("preparing inspection must not reboot"))
    monkeypatch.setattr(baremetal, "pxe_boot", power)
    job = _submit(client, admin_headers, env["id"], "baremetal.node.inspect", {"node_id": identity})
    assert job["status"] == "success", job.get("error")
    power.assert_not_called()
    with SessionLocal() as db:
        target = db.get(BaremetalNode, identity)
        assert target.next_boot == "inspect" and target.wiped_at is None
        assert target.commission_wipe_wwids is None


def test_inspection_job_cannot_prepare_a_node_in_another_cloud(client, admin_headers, monkeypatch):
    from tests.test_baremetal import _create_env, _make_node, _submit

    env = _create_env(client, admin_headers, dry_run=False)
    other = _create_env(client, admin_headers, dry_run=False)
    identity, _ = _make_node(other["id"], pxe_mac=MAC, hardware_report=node().hardware_report)
    prepare = Mock(side_effect=AssertionError("cross-cloud node must not be prepared"))
    monkeypatch.setattr(baremetal, "_prepare_pxe", prepare)
    job = _submit(client, admin_headers, env["id"], "baremetal.node.inspect", {"node_id": identity, "boot_now": True})
    assert job["status"] == "failed"
    prepare.assert_not_called()
