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


def test_management_only_node_learns_boot_nic_and_lease_from_real_http_exchange(tmp_path, monkeypatch):
    import json
    import urllib.request
    import uuid

    from app.db import SessionLocal
    from app.models import BaremetalNode, Environment
    from app.services.pxe_runtime import PxeHttpServer

    second = "aa:bb:cc:00:00:02"
    unknown = "aa:bb:cc:00:00:ff"
    token = uuid.uuid4().hex; identity = _stored_node(token)
    with SessionLocal() as db:
        target = db.get(BaremetalNode, identity)
        target.next_boot = "inspect"; target.boot_stage = "inspecting"; target.pxe_mac = None
        target.expected_ip = None
        target.hardware_report = {"serial": report()["serial"], "nics": [{"mac": MAC}, {"mac": second}]}
        db.commit()
        profiles = baremetal.boot_profiles(db, db.get(Environment, target.environment_id))
        assert {p["pxe_mac"] for p in profiles} == {MAC, second}
        assert all(p["expected_ip"] is None for p in profiles)
    directory = tmp_path / "mac"; directory.mkdir()
    script = bootselect.render_profile_ipxe("inspect", "http://192.0.2.1", token, node_id=identity)
    for mac in [MAC, second, unknown]:
        (directory / (mac.replace(":", "-") + ".ipxe")).write_text(script)
    monkeypatch.setattr(baremetal, "_lease_ip", lambda mac: "192.0.2.77" if mac == second else "")
    server = PxeHttpServer("127.0.0.1", 0, tmp_path); server.start()
    try:
        base = "http://127.0.0.1:" + str(server._httpd.server_address[1])
        def get(mac):
            with urllib.request.urlopen(base + "/mac/" + mac.replace(":", "-") + ".ipxe", timeout=5) as response:
                return response.read().decode()
        assert "# profile: disk" in get(unknown)
        with SessionLocal() as db:
            assert db.get(BaremetalNode, identity).pxe_mac is None
        assert "gsc_wipe=0" in get(second)
        # Once the real boot NIC is observed, another alias cannot replace it.
        assert "# profile: disk" in get(MAC)
        body = report(); body["nics"] = [{"mac": second, "link_up": True}]
        req = urllib.request.Request(base + "/commission/" + token, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as response:
            assert json.load(response)["ok"] is True
        with SessionLocal() as db:
            target = db.get(BaremetalNode, identity)
            assert target.pxe_mac == second and target.expected_ip == "192.0.2.77"
            assert target.os_inventory_report["boot_mac"] == second
            assert target.wiped_at is None and target.next_boot == "disk"
    finally:
        server.stop()


def test_inspection_preparation_does_not_require_guessing_first_bmc_nic(monkeypatch):
    target = node(); target.pxe_mac = None
    monkeypatch.setattr(baremetal, "_prepare_pxe", lambda *a: {"ok": True})
    power = Mock(side_effect=AssertionError("preparation must not reboot"))
    monkeypatch.setattr(baremetal, "pxe_boot", power)
    result = hardware_inspection.begin(Mock(), SimpleNamespace(), target, dry_run=False, log=lambda _: None)
    assert result["ok"] and target.pxe_mac is None
    power.assert_not_called()


def test_registration_can_keep_a_single_unambiguous_interface(client, admin_headers, monkeypatch):
    from app.services import redfish
    from tests.test_baremetal import _create_env, _node_row, _register_params, _submit

    monkeypatch.setattr(redfish, "system_macs", lambda *a: [MAC, MAC])
    env = _create_env(client, admin_headers, dry_run=False)
    job = _submit(client, admin_headers, env["id"], "baremetal.node.register", _register_params("single-interface"))
    assert job["status"] == "success"
    assert _node_row(env["id"], "single-interface")["pxe_mac"] == MAC


def test_real_completed_inspection_is_explicitly_distinct_from_rehearsal(monkeypatch):
    from types import SimpleNamespace
    from app.models import BaremetalNode
    from app.modules.baremetal import node_inspect

    target = SimpleNamespace(id="fixture", environment_id="fixture-env", boot_stage="inspected", commission_token=None, os_inventory_report={"source": "ram-inspection"})
    class Database:
        def get(self, model, identity):
            assert model is BaremetalNode and identity == "fixture"
            return target
        def commit(self):
            pass
        def refresh(self, value):
            assert value is target
    runner = SimpleNamespace(db=Database(), settings=None, write_audit=lambda **kw: None)
    monkeypatch.setattr(node_inspect.hardware_inspection, "begin", lambda *a, **k: {"ok": True})
    result = node_inspect.run(runner, None, None, SimpleNamespace(created_by="operator"),
        SimpleNamespace(id="fixture-env"), lambda line: None, None,
        {"node_id": "fixture", "boot_now": True}, None, lambda: None,
        False, 900, None, None, None, None, None, None, None)
    assert result["ok"] is True
    assert result["dry_run"] is False, "Native callers must be able to prove this was a real inspection"
    assert result["disk_writes"] is False


def test_failed_inspection_revokes_only_its_own_pending_token():
    from types import SimpleNamespace

    from app.services.hardware_inspection import disarm

    class DB:
        writes = 0
        def refresh(self, node): pass
        def add(self, node): self.writes += 1
        def commit(self): pass
    db = DB()
    node = SimpleNamespace(commission_token='test-token', next_boot='inspect', boot_stage='inspecting')
    assert not disarm(db, node, 'older-token')
    assert db.writes == 0 and node.commission_token == 'test-token'
    assert disarm(db, node, 'test-token')
    assert node.commission_token is None and node.next_boot == 'disk' and node.boot_stage == 'failed'
    node.commission_token = None
    node.boot_stage = 'inspected'
    assert not disarm(db, node, 'test-token')
    assert node.boot_stage == 'inspected'


def test_cancelled_inspection_disarms_report_token_and_allows_retry(monkeypatch):
    from types import SimpleNamespace

    from app.modules.baremetal import node_inspect
    from app.services.job_runner import JobCancelledError

    node = SimpleNamespace(id="owned", environment_id="fixture", commission_token="owned-test-token",
                           next_boot="inspect", boot_stage="inspecting", os_inventory_report=None)
    class DB:
        def get(self, model, identity): return node
        def refresh(self, value): pass
        def add(self, value): pass
        def commit(self): pass
    runner = SimpleNamespace(db=DB(), settings=None)
    monkeypatch.setattr(node_inspect.hardware_inspection, "begin", lambda *a, **k: {"ok": True})
    def cancel(): raise JobCancelledError("fixture cancelled")
    with pytest.raises(JobCancelledError):
        node_inspect.run(runner, None, None, SimpleNamespace(created_by="fixture"),
            SimpleNamespace(id="fixture"), lambda _: None, None,
            {"node_id": "owned", "boot_now": True}, None, cancel,
            False, 900, None, None, None, None, None, None, None)
    assert node.next_boot == "disk" and node.boot_stage == "failed" and node.commission_token is None
