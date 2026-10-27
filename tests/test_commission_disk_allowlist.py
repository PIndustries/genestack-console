"""Commissioning must never turn an omitted disk plan into wipe-all."""
from types import SimpleNamespace
from unittest.mock import Mock

from app.services import baremetal, bootselect

A = "naa.60000000000000000000000000000001"
B = "naa.60000000000000000000000000000002"
MAC = "aa:bb:cc:00:00:01"


def test_missing_plan_stops_before_pxe_preparation_or_node_mutation(monkeypatch):
    node = SimpleNamespace(id="node", name="host", pxe_mac=MAC, next_boot="disk", commission_token=None)
    prepare = Mock(return_value={"ok": False, "error": "unsafe preparation reached"})
    monkeypatch.setattr(baremetal, "_prepare_pxe", prepare)
    result = baremetal.begin_commission(Mock(), SimpleNamespace(), node, log=lambda _: None)
    assert result["ok"] is False
    assert "allowlist" in result["error"]
    prepare.assert_not_called()
    assert node.next_boot == "disk"
    assert node.commission_token is None


def test_legacy_commission_profile_without_disk_plan_boots_local_disk():
    script = bootselect.render_commission_ipxe("http://192.0.2.1:8088", "fixture-token")
    assert "gsc_wipe=1" not in script
    assert "# profile: disk" in script


def test_scoped_report_accepts_untouched_excluded_disk():
    report = {"wipe": True, "nics": [{"mac": MAC}], "disks": [
        {"name": "sda", "wwid": A, "wiped": True},
        {"name": "sdb", "wwid": B, "wiped": False},
    ]}
    assert bootselect.validate_scoped_report(report, MAC, [A]) == (True, "")
    report["disks"][1]["wiped"] = True
    ok, error = bootselect.validate_scoped_report(report, MAC, [A])
    assert not ok
    assert "outside" in error


def test_partial_and_duplicate_target_reports_do_not_advance_to_talos():
    report = {"wipe": True, "nics": [{"mac": MAC}], "disks": [{"name": "sda", "wwid": A, "wiped": True}]}
    assert not bootselect.validate_scoped_report(report, MAC, [A, B])[0]
    report["disks"].append({"name": "sdb", "wwid": A, "wiped": True})
    assert not bootselect.validate_scoped_report(report, MAC, [A])[0]
    assert not bootselect.validate_scoped_report(report, MAC, [])[0]


def _run_ram_script(tmp_path, identities, *, allowed=None, remote=(), readonly=(), mounted=()):
    """Run the real shell logic against sparse files, never this host's devices."""
    import json
    import shlex
    import subprocess
    import sys

    root = tmp_path
    for name in ["sys/block", "sys/class/net/eth0", "proc", "dev", "tmp"]:
        (root / name).mkdir(parents=True, exist_ok=True)
    (root / "sys/class/net/eth0/address").write_text(MAC)
    (root / "sys/class/net/eth0/carrier").write_text("1")
    (root / "proc/mounts").write_text("".join(f"/dev/{n}1 /data ext4 rw 0 0\n" for n in mounted))
    (root / "proc/cmdline").write_text("gsc_wipe=1 gsc_token=fixture-token " + (
        "gsc_allow_wwids=" + ",".join(allowed) if allowed is not None else ""
    ))
    for name, identity in identities.items():
        block = root / "sys/block" / name
        (block / "device").mkdir(parents=True)
        for field, value in {"wwid": identity, "removable": "0", "size": str(64 * 1024**2 // 512), "ro": "1" if name in readonly else "0"}.items():
            (block / field).write_text(value)
        (block / "device/transport").write_text("iscsi" if name in remote else "sas")
        disk = root / "dev" / name
        with disk.open("wb") as stream:
            stream.truncate(64 * 1024**2)
            stream.write(b"KEEP-FRONT")
            stream.seek(64 * 1024**2 - 16)
            stream.write(b"KEEP-END")
    writer = root / "write-fixture-disk.py"
    writer.write_text('''import sys,pathlib,json
root=pathlib.Path(sys.argv[1]).resolve()
args=dict(x.split('=',1) for x in sys.argv[2:])
target=pathlib.Path(args['of']).resolve()
assert target.parent==root/'dev' and target.is_file()
assert args['if']=='/dev/zero'
size=int(args['bs']);offset=int(args.get('seek','0'))*size;length=int(args['count'])*size
assert offset>=0 and offset+length<=target.stat().st_size
with target.open('r+b') as stream:
 stream.seek(offset)
 for _ in range(length//512):stream.write(bytes(512))
 stream.flush()
with (root/'writes.jsonl').open('a') as log:log.write(json.dumps({'name':target.name,'offset':offset,'length':length})+'\\n')
''')
    script = bootselect.COMMISSION_SCRIPT
    script = script.replace("/sys/", str(root / "sys") + "/").replace("/proc/", str(root / "proc") + "/")
    script = script.replace("/tmp/gsc", str(root / "tmp/gsc")).replace("/dev/console", str(root / "console"))
    script = script.replace('of="/dev/$name"', 'of="' + str(root / "dev") + '/$name"')
    script = script.replace("dd if=/dev/zero", shlex.quote(sys.executable) + " " + shlex.quote(str(writer)) + " " + shlex.quote(str(root)) + " if=/dev/zero")
    script = script.replace("sleep 2", ":").replace("poweroff -f", ":")
    assert "in /sys/block/" not in script and '="/sys/block/' not in script and 'of="/dev/' not in script
    assert "poweroff" not in script and "dd if=" not in script
    result = subprocess.run(["sh", "-s"], input=script, capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == 0, result.stderr
    report = json.loads((root / "tmp/gsc-report.json").read_text())
    writes = [json.loads(line) for line in (root / "writes.jsonl").read_text().splitlines()] if (root / "writes.jsonl").exists() else []
    return report, writes


def test_actual_ram_script_clears_only_selected_local_disk_headers(tmp_path):
    report, writes = _run_ram_script(tmp_path, {"sda": A, "sdb": B}, allowed=[A], remote=["sdb"])
    assert report["wipe"] is True
    assert [row["name"] for row in writes] == ["sda", "sda"]
    assert [row["offset"] for row in writes] == [0, 48 * 1024**2]
    assert bootselect.validate_scoped_report(report, MAC, [A]) == (True, "")
    for name, expected in [("sda", bytes(10)), ("sdb", b"KEEP-FRONT")]:
        with (tmp_path / "dev" / name).open("rb") as stream:
            assert stream.read(10) == expected
    with (tmp_path / "dev/sdb").open("rb") as stream:
        stream.seek(64 * 1024**2 - 16)
        assert stream.read(8) == b"KEEP-END"


def test_actual_ram_script_missing_or_partial_plan_performs_no_writes(tmp_path):
    for index, allowed in enumerate([None, [A, "naa.60000000000000000000000000000003"], [A, A]]):
        report, writes = _run_ram_script(tmp_path / str(index), {"sda": A, "sdb": B}, allowed=allowed)
        assert report["wipe"] is False
        assert writes == []


def test_actual_ram_script_rejects_ambiguous_remote_readonly_and_mounted_targets(tmp_path):
    cases = [({"sda": A, "sdb": A}, {}), ({"sda": A}, {"remote": ["sda"]}),
             ({"sda": A}, {"readonly": ["sda"]}), ({"sda": A}, {"mounted": ["sda"]})]
    for index, (identities, options) in enumerate(cases):
        report, writes = _run_ram_script(tmp_path / str(index), identities, allowed=[A], **options)
        assert report["wipe"] is False
        assert writes == []


def _stored_node(token, *, mac=MAC):
    import uuid

    from app.db import SessionLocal
    from app.models import BaremetalNode, Environment, Tenant
    with SessionLocal() as db:
        tenant = Tenant(name="allowlist-" + uuid.uuid4().hex)
        db.add(tenant); db.flush()
        env = Environment(name="isolated-" + uuid.uuid4().hex, tenant_id=tenant.id)
        db.add(env); db.flush()
        node = BaremetalNode(environment_id=env.id, name="fixture", bmc_host="bmc.example.test",
            bmc_username="fixture", bmc_password="fixture", pxe_mac=mac, next_boot="commission",
            boot_stage="commissioning", commission_token=token, commission_wipe_wwids=[A])
        db.add(node); db.commit()
        return node.id


def test_cached_wipe_profile_requires_current_token_and_exact_node_disk_plan(tmp_path):
    import uuid

    from app.db import SessionLocal
    from app.models import BaremetalNode
    token = uuid.uuid4().hex
    identity = _stored_node(token)
    directory = tmp_path / "mac"; directory.mkdir()
    filename = MAC.replace(":", "-") + ".ipxe"
    script = bootselect.render_profile_ipxe("commission", "http://192.0.2.1", token, wipe_wwids=[A], node_id=identity)
    (directory / filename).write_text(script)
    assert baremetal.guarded_commission_profile(str(tmp_path), "/mac/" + filename) == script
    with SessionLocal() as db:
        node = db.get(BaremetalNode, identity); node.commission_wipe_wwids = [B]; db.commit()
    assert "# profile: disk" in baremetal.guarded_commission_profile(str(tmp_path), "/mac/" + filename)
    (directory / filename).write_text("#!ipxe\n# profile: commission\nkernel /old gsc_wipe=1 gsc_token=" + token)
    assert "# profile: disk" in baremetal.guarded_commission_profile(str(tmp_path), "/mac/" + filename)


def test_same_mac_in_two_environments_records_boot_only_for_bound_node(tmp_path):
    import uuid
    from datetime import UTC, datetime

    from app.db import SessionLocal
    from app.models import BaremetalNode
    old = _stored_node(uuid.uuid4().hex)
    new = _stored_node(uuid.uuid4().hex)
    with SessionLocal() as db:
        for identity in (old, new):
            node = db.get(BaremetalNode, identity)
            node.next_boot = "talos"; node.boot_stage = "commissioned"; node.wiped_at = datetime.now(UTC)
        db.commit()
    directory = tmp_path / "mac"; directory.mkdir()
    filename = MAC.replace(":", "-") + ".ipxe"
    (directory / filename).write_text(bootselect.render_profile_ipxe("talos", "http://192.0.2.1", None, node_id=new))
    baremetal.record_boot_fetch(str(tmp_path), "/mac/" + filename)
    with SessionLocal() as db:
        assert db.get(BaremetalNode, old).talos_served_at is None
        assert db.get(BaremetalNode, new).talos_served_at is not None


def test_cancelled_commission_token_cannot_advance_boot_state():
    import uuid

    from app.db import SessionLocal
    from app.models import BaremetalNode
    token = uuid.uuid4().hex
    identity = _stored_node(token)
    with SessionLocal() as db:
        node = db.get(BaremetalNode, identity); node.next_boot = "disk"; db.commit()
    result = baremetal.ingest_commission(token, {"wipe": True, "nics": [{"mac": MAC}], "disks": [{"name": "sda", "wwid": A, "wiped": True}]})
    assert result["ok"] is False
    with SessionLocal() as db:
        assert db.get(BaremetalNode, identity).next_boot == "disk"
        assert db.get(BaremetalNode, identity).wiped_at is None


def test_http_does_not_serve_cached_unscoped_wipe_profile(tmp_path):
    import urllib.request

    from app.services.pxe_runtime import PxeHttpServer
    directory = tmp_path / "mac"; directory.mkdir()
    filename = MAC.replace(":", "-") + ".ipxe"
    (directory / filename).write_text("#!ipxe\n# profile: commission\nkernel /old gsc_wipe=1\nboot\n")
    server = PxeHttpServer("127.0.0.1", 0, tmp_path)
    server.start()
    try:
        port = server._httpd.server_address[1]
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/mac/{filename}", timeout=5) as response:
            content = response.read().decode()
            assert response.headers["Cache-Control"] == "no-store"
        assert "gsc_wipe=1" not in content
        assert "# profile: disk" in content
    finally:
        server.stop()


def test_http_authorization_failure_never_falls_through_to_destructive_file(tmp_path, monkeypatch):
    import urllib.error
    import urllib.request

    import pytest

    from app.services.pxe_runtime import PxeHttpServer
    directory = tmp_path / "mac"; directory.mkdir()
    filename = MAC.replace(":", "-") + ".ipxe"
    (directory / filename).write_text("#!ipxe\n# profile: commission\nkernel /old gsc_wipe=1\nboot\n")
    def unavailable(*args): raise RuntimeError("database unavailable")
    monkeypatch.setattr(baremetal, "guarded_commission_profile", unavailable)
    server = PxeHttpServer("127.0.0.1", 0, tmp_path); server.start()
    try:
        port = server._httpd.server_address[1]
        with pytest.raises(urllib.error.HTTPError) as failure:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/mac/{filename}", timeout=5)
        assert failure.value.code == 503
        assert b"gsc_wipe=1" not in failure.value.read()
    finally:
        server.stop()


def test_whole_cluster_plan_is_complete_and_globally_unique():
    import pytest

    from app.services.greenfield import _wipe_plan

    assert _wipe_plan({"a": [A], "b": [B]}, {"a", "b"}) == {"a": [A], "b": [B]}
    for raw in [None, {"a": [A]}, {"a": [A], "b": []}, {"a": [A], "b": [A]},
                {"a": [A], "b": [B], "extra": [B]}]:
        with pytest.raises(ValueError):
            _wipe_plan(raw, {"a", "b"})


def test_greenfield_missing_second_host_plan_does_not_start_first_host(client, admin_headers, monkeypatch):
    from app.services import baremetal
    from tests.test_baremetal import _make_node
    from tests.test_greenfield import DOC, _create_env, _put_doc

    env = _create_env(client, admin_headers, dry_run=False)
    _put_doc(client, admin_headers, env["id"], DOC)
    _make_node(env["id"], name="ctrl1", expected_ip="10.200.0.51")
    _make_node(env["id"], name="compute1", expected_ip="10.200.0.55")
    start = Mock(side_effect=AssertionError("No machine may start for an incomplete fleet plan"))
    monkeypatch.setattr(baremetal, "begin_commission", start)
    result = client.post(f"/api/v1/environments/{env['id']}/jobs", headers=admin_headers,
                         json={"operation": "genestack.greenfield", "run_sync": True,
                               "params": {"stop_after": "commission", "wipe_disk_wwids": {"ctrl1": [A]}}})
    assert result.status_code == 201
    assert result.json()["status"] == "failed"
    assert "allowlist" in (result.json().get("error") or "")
    start.assert_not_called()


def test_allowlist_fits_boot_command_line():
    import pytest

    with pytest.raises(ValueError, match="command-line budget"):
        bootselect.validate_wipe_wwids(["naa." + format(i, "064x") for i in range(32)])


import pytest


@pytest.mark.parametrize("raises", [False, True])
def test_failed_preparation_revokes_commission_profile(monkeypatch, raises):
    target = SimpleNamespace(id="node", name="host", pxe_mac=MAC, next_boot="disk", commission_token=None)
    def fail(*a):
        if raises:
            raise OSError("fixture preparation failed")
        return {"ok": False, "error": "fixture preparation failed"}
    monkeypatch.setattr(baremetal, "_prepare_pxe", fail)
    result = baremetal.begin_commission(Mock(), SimpleNamespace(), target, log=lambda _: None, wipe_disk_wwids=[A])
    assert result["ok"] is False
    assert target.next_boot == "disk" and target.commission_token is None
    assert target.commission_wipe_wwids is None
