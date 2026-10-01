"""Per-MAC boot choice, commission report gate, and fresh vs old maintenance."""

from __future__ import annotations

import io
import json
import tarfile
import uuid
from datetime import datetime, timedelta, timezone

from app.services.bootselect import (
    build_apkovl,
    classify_boot,
    commission_summary,
    metal_ready,
    served_after_wipe,
    render_chain_ipxe,
    render_commission_ipxe,
    render_disk_ipxe,
    render_profile_ipxe,
    render_talos_ipxe,
    validate_report,
)


def _report(**overrides):
    body = {
        "wipe": True,
        "serial": "SN1",
        "vendor": "HPE",
        "product": "DL380",
        "token": "secret-token",
        "nics": [{"name": "eth0", "mac": "aa:bb:cc:dd:ee:10"}],
        "disks": [{"name": "sda", "size_sectors": 100, "wiped": True}],
    }
    body.update(overrides)
    return body


def test_chain_exits_for_an_unknown_mac_and_disk_does_not_wipe():
    chain = render_chain_ipxe("http://10.10.0.1:8080")
    assert "# profile: chain" in chain
    assert "chain http://10.10.0.1:8080/mac/${net0/mac:hexhyp}.ipxe || exit" in chain
    disk = render_disk_ipxe()
    assert "# profile: disk" in disk
    assert disk.rstrip().endswith("exit")
    assert "dd " not in disk
    assert "talos.platform" not in disk


def test_talos_script_has_no_machine_config_and_commission_carries_the_token():
    talos = render_talos_ipxe("http://10.10.0.1:8080")
    assert "talos.platform=metal" in talos
    assert "talos.config=" not in talos
    assert "gsc_wipe" not in talos
    commission = render_commission_ipxe("http://10.10.0.1:8080", "abc")
    assert "gsc_wipe=1" in commission
    assert "gsc_token=abc" in commission
    assert "gsc_report=http://10.10.0.1:8080/commission/abc" in commission
    assert render_profile_ipxe("nope", "http://10.10.0.1:8080", "abc") == render_disk_ipxe()


def test_apkovl_is_a_ram_disk_script():
    raw = build_apkovl()
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
        script = archive.extractfile("etc/local.d/commission.start").read().decode()
        link = archive.getmember("etc/runlevels/default/local")
    assert "dd if=/dev/zero" in script
    assert "poweroff -f" in script
    assert "gsc_wipe" in script
    assert link.issym()
    assert link.linkname == "/etc/init.d/local"


def test_validate_report_requires_a_wipe_and_the_pxe_mac():
    assert validate_report(_report(), "aa:bb:cc:dd:ee:10") == (True, "")
    ok, err = validate_report(_report(wipe=False), "aa:bb:cc:dd:ee:10")
    assert not ok and "wipe" in err
    ok, err = validate_report(_report(disks=[]), "aa:bb:cc:dd:ee:10")
    assert not ok and "no fixed disks" in err
    ok, err = validate_report(
        _report(disks=[{"name": "sda", "wiped": False}]), "aa:bb:cc:dd:ee:10"
    )
    assert not ok and "not wiped" in err
    ok, err = validate_report(_report(), "aa:bb:cc:dd:ee:99")
    assert not ok and "PXE MAC" in err
    assert validate_report("nope", None)[0] is False


def test_classify_boot_keeps_the_old_three_way_result():
    assert classify_boot(False, None, wiped_at=None, talos_served_at=None, require_fresh=False) == "down"
    assert classify_boot(True, True, wiped_at=None, talos_served_at=None, require_fresh=False) == "old-os"
    assert classify_boot(True, False, wiped_at=None, talos_served_at=None, require_fresh=False) == "maintenance"
    assert (
        classify_boot(
            True,
            None,
            wiped_at=None,
            talos_served_at=None,
            require_fresh=False,
            was_ready=True,
            saw_down=False,
        )
        == "old-os"
    )


def test_classify_boot_fresh_requires_talos_after_the_wipe():
    wiped = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
    served = wiped + timedelta(seconds=5)
    kwargs = dict(wiped_at=wiped, talos_served_at=served, require_fresh=True)
    assert classify_boot(True, False, **kwargs) == "fresh-maintenance"
    assert classify_boot(True, True, **kwargs) == "old-os"
    assert (
        classify_boot(True, False, wiped_at=wiped, talos_served_at=None, require_fresh=True)
        == "old-maintenance"
    )
    earlier = wiped - timedelta(seconds=5)
    assert (
        classify_boot(
            True, False, wiped_at=wiped, talos_served_at=earlier, require_fresh=True
        )
        == "old-maintenance"
    )
    # SQLite may hand back a naive timestamp. It still compares as UTC.
    assert (
        classify_boot(
            True,
            False,
            wiped_at=wiped.replace(tzinfo=None),
            talos_served_at=served,
            require_fresh=True,
        )
        == "fresh-maintenance"
    )


def test_metal_ready_holds_match_the_stop_point():
    assert metal_ready("commissioned", "down", "commission") is True
    assert metal_ready("commissioning", "down", "commission") is False
    assert metal_ready("commissioned", "fresh-maintenance", "") is True
    assert metal_ready("commissioned", "old-maintenance", "") is False
    assert metal_ready("talos", "fresh-maintenance", "talos") is True
    assert metal_ready("talos", "old-maintenance", "") is False


def test_summary_hides_the_token():
    summary = commission_summary(_report())
    assert summary == {
        "wiped": True,
        "disk_count": 1,
        "serial": "SN1",
        "product": "DL380",
    }
    assert "secret-token" not in json.dumps(summary)


def test_ingest_accepts_one_wipe_and_a_replay_does_not_power_cycle(monkeypatch):
    from app.db import SessionLocal
    from app.models import BaremetalNode, Environment, Tenant
    from app.services import baremetal

    calls: list[str] = []
    monkeypatch.setattr(
        baremetal,
        "_prepare_pxe",
        lambda *args, **kwargs: calls.append("prep") or {"ok": True},
    )
    token = uuid.uuid4().hex
    db = SessionLocal()
    try:
        tenant = Tenant(name=f"boot-{uuid.uuid4().hex[:8]}")
        db.add(tenant)
        db.flush()
        env = Environment(name=f"boot-env-{uuid.uuid4().hex[:8]}", tenant_id=tenant.id)
        db.add(env)
        db.flush()
        node = BaremetalNode(
            environment_id=env.id,
            name=f"node-{uuid.uuid4().hex[:6]}",
            bmc_host="bmc.example.com",
            bmc_username="root",
            bmc_password="stored",
            pxe_mac="aa:bb:cc:dd:ee:10",
            expected_ip="10.10.0.20",
            next_boot="commission",
            boot_stage="commissioning",
            commission_token=token,
        )
        db.add(node)
        db.commit()
        node_id = node.id
    finally:
        db.close()

    report = _report(token=token)
    first = baremetal.ingest_commission(token, report)
    assert first["ok"] is True, first
    assert first.get("duplicate") is not True
    assert calls == ["prep"]
    second = baremetal.ingest_commission(token, report)
    assert second == {"ok": True, "duplicate": True, "node_id": node_id}
    assert calls == ["prep"]

    db = SessionLocal()
    try:
        node = db.get(BaremetalNode, node_id)
        assert node.next_boot == "talos"
        assert node.boot_stage == "commissioned"
        assert node.wiped_at is not None
        assert node.talos_served_at is None
        assert "token" not in (node.commission_report or {})
        payload = baremetal.node_payload(node)
        assert token not in json.dumps(payload)
        assert payload["commission"]["disk_count"] == 1
    finally:
        db.close()


def test_rejected_report_does_not_switch_the_profile(monkeypatch):
    from app.db import SessionLocal
    from app.models import BaremetalNode, Environment, Tenant
    from app.services import baremetal

    monkeypatch.setattr(
        baremetal, "_prepare_pxe", lambda *a, **k: {"ok": True}
    )
    token = uuid.uuid4().hex
    db = SessionLocal()
    try:
        tenant = Tenant(name=f"boot-{uuid.uuid4().hex[:8]}")
        db.add(tenant)
        db.flush()
        env = Environment(name=f"boot-env-{uuid.uuid4().hex[:8]}", tenant_id=tenant.id)
        db.add(env)
        db.flush()
        node = BaremetalNode(
            environment_id=env.id,
            name=f"node-{uuid.uuid4().hex[:6]}",
            bmc_host="bmc.example.com",
            bmc_username="root",
            bmc_password="stored",
            pxe_mac="aa:bb:cc:dd:ee:11",
            next_boot="commission",
            boot_stage="commissioning",
            commission_token=token,
        )
        db.add(node)
        db.commit()
        node_id = node.id
    finally:
        db.close()

    result = baremetal.ingest_commission(token, _report(token=token, wipe=False))
    assert result["ok"] is False
    db = SessionLocal()
    try:
        node = db.get(BaremetalNode, node_id)
        assert node.next_boot == "commission"
        assert node.wiped_at is None
        assert node.boot_stage == "commissioning"
    finally:
        db.close()


def test_set_next_boot_talos_without_wipe_and_disk_without_power(monkeypatch):
    from app.db import SessionLocal
    from app.models import BaremetalNode, Environment, Tenant
    from app.services import baremetal

    monkeypatch.setattr(baremetal, "_prepare_pxe", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(
        baremetal,
        "pxe_boot",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("powered")),
    )
    db = SessionLocal()
    try:
        tenant = Tenant(name=f"boot-{uuid.uuid4().hex[:8]}")
        db.add(tenant)
        db.flush()
        env = Environment(name=f"boot-env-{uuid.uuid4().hex[:8]}", tenant_id=tenant.id)
        db.add(env)
        db.flush()
        node = BaremetalNode(
            environment_id=env.id,
            name=f"node-{uuid.uuid4().hex[:6]}",
            bmc_host="bmc.example.com",
            bmc_username="root",
            bmc_password="stored",
            pxe_mac="aa:bb:cc:dd:ee:12",
            next_boot="disk",
            boot_stage="new",
        )
        db.add(node)
        db.commit()
        refused = baremetal.set_next_boot(
            db,
            env,
            node,
            "talos",
            boot_now=True,
            dry_run=False,
            log=lambda *_: None,
        )
        assert refused["ok"] is False
        assert node.next_boot == "disk"
        node.wiped_at = datetime.now(timezone.utc)
        db.add(node)
        db.commit()
        chosen = baremetal.set_next_boot(
            db,
            env,
            node,
            "talos",
            boot_now=False,
            dry_run=False,
            log=lambda *_: None,
        )
        assert chosen["ok"] is True, chosen
        assert node.next_boot == "talos"
        assert node.talos_served_at is None
    finally:
        db.close()


def test_boot_fetch_of_talos_stamps_served_after_a_wipe(tmp_path, monkeypatch):
    from app.db import SessionLocal
    from app.models import BaremetalNode, Environment, Tenant
    from app.services import baremetal

    db = SessionLocal()
    try:
        tenant = Tenant(name=f"boot-{uuid.uuid4().hex[:8]}")
        db.add(tenant)
        db.flush()
        env = Environment(name=f"boot-env-{uuid.uuid4().hex[:8]}", tenant_id=tenant.id)
        db.add(env)
        db.flush()
        node = BaremetalNode(
            environment_id=env.id,
            name=f"node-{uuid.uuid4().hex[:6]}",
            bmc_host="bmc.example.com",
            bmc_username="root",
            bmc_password="stored",
            pxe_mac="aa:bb:cc:dd:ee:13",
            next_boot="talos",
            boot_stage="commissioned",
            wiped_at=datetime.now(timezone.utc),
            commission_token="not-logged",
        )
        db.add(node)
        db.commit()
        node_id = node.id
    finally:
        db.close()

    mac_dir = tmp_path / "mac"
    mac_dir.mkdir()
    (mac_dir / "aa-bb-cc-dd-ee-13.ipxe").write_text(
        render_talos_ipxe("http://10.10.0.1:8080"), encoding="utf-8"
    )
    baremetal.record_boot_fetch(str(tmp_path), "/mac/aa-bb-cc-dd-ee-13.ipxe")
    baremetal.record_boot_fetch(str(tmp_path), "/mac/ff-ff-ff-ff-ff-ff.ipxe")
    db = SessionLocal()
    try:
        node = db.get(BaremetalNode, node_id)
        assert served_after_wipe(node.talos_served_at, node.wiped_at)
        assert node.boot_stage == "talos"
        assert "not-logged" not in json.dumps(node.boot_log)
        assert node.boot_log[-1]["profile"] == "talos"
    finally:
        db.close()
