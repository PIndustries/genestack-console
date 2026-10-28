"""The real blank-machine boot failed without Alpine's base packages."""

import hashlib
import io
import tarfile
from pathlib import Path

import pycdlib

from app.services import bootselect, commission_assets


def image(include_packages=True):
    iso = pycdlib.PyCdlib()
    iso.new(interchange_level=3, rock_ridge="1.09")
    for path, name in [("/BOOT", "boot"), ("/APKS", "apks"), ("/APKS/X86_64", "x86_64")]:
        iso.add_directory(iso_path=path, rr_name=name)
    content = [
        ("/BOOT/VMLINUZ.;1", "vmlinuz-lts", b"kernel"),
        ("/BOOT/INITRAMF.;1", "initramfs-lts", b"initramfs"),
        ("/BOOT/MODLOOP.;1", "modloop-lts", b"modules"),
    ]
    if include_packages:
        content += [
            ("/APKS/X86_64/INDEX.GZ;1", "APKINDEX.tar.gz", b"signed index"),
            ("/APKS/X86_64/ALPBASE.APK;1", "alpine-base-3.24.3-r0.apk", b"base package"),
        ]
    for path, name, data in content:
        iso.add_fp(io.BytesIO(data), len(data), iso_path=path, rr_name=name)
    buf = io.BytesIO()
    iso.write_fp(buf)
    iso.close()
    return buf.getvalue()


def downloader(monkeypatch, data):
    calls = []
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as archive:
        for name, value in [("vmlinuz-lts", b"kernel"), ("initramfs-lts", b"physical network drivers"), ("modloop-lts", b"modules")]:
            info = tarfile.TarInfo("boot/" + name)
            info.size = len(value)
            archive.addfile(info, io.BytesIO(value))
    netboot = buf.getvalue()
    monkeypatch.setattr(commission_assets, "ALPINE_ISO_SHA256", hashlib.sha256(data).hexdigest())
    monkeypatch.setattr(commission_assets, "ALPINE_NETBOOT_SHA256", hashlib.sha256(netboot).hexdigest())

    def download(url, log, directory, filename, max_bytes):
        calls.append(url)
        target = Path(directory) / filename
        payload = netboot if url == commission_assets.ALPINE_NETBOOT_URL else data
        target.write_bytes(payload)
        return target, hashlib.sha256(payload).hexdigest()

    monkeypatch.setattr(commission_assets, "download_factory_image", download)
    return calls


def test_verified_netboot_provides_physical_drivers_and_iso_provides_local_repository(tmp_path, monkeypatch):
    calls = downloader(monkeypatch, image())
    first = commission_assets.fetch(tmp_path)
    assert first["ok"] and first["changed"]
    destination = tmp_path / "assets/commission" / bootselect.ALPINE_VERSION
    assert (destination / "vmlinuz-lts").read_bytes() == b"kernel"
    assert (destination / "initramfs-lts").read_bytes() == b"physical network drivers"
    assert (destination / "apks/x86_64/APKINDEX.tar.gz").read_bytes() == b"signed index"
    assert (destination / "apks/x86_64/alpine-base-3.24.3-r0.apk").is_file()
    assert commission_assets.cached(destination)
    assert commission_assets.fetch(tmp_path)["changed"] is False
    assert len(calls) == 2
    # Corrupt cached data is not reused as a complete boot image.
    (destination / "vmlinuz-lts").write_bytes(b"corrupt")
    assert commission_assets.fetch(tmp_path)["changed"]
    assert len(calls) == 4
    assert (destination / "vmlinuz-lts").read_bytes() == b"kernel"


def test_digest_mismatch_never_publishes_boot_assets(tmp_path, monkeypatch):
    downloader(monkeypatch, image())
    monkeypatch.setattr(commission_assets, "ALPINE_ISO_SHA256", "0" * 64)
    result = commission_assets.fetch(tmp_path)
    assert not result["ok"]
    assert "SHA256" in result["error"]
    assert not (tmp_path / "assets/commission" / bootselect.ALPINE_VERSION).exists()


def test_netboot_digest_mismatch_preserves_previous_assets(tmp_path, monkeypatch):
    downloader(monkeypatch, image())
    destination = tmp_path / "assets/commission" / bootselect.ALPINE_VERSION
    destination.mkdir(parents=True)
    (destination / "initramfs-lts").write_bytes(b"previous")
    monkeypatch.setattr(commission_assets, "ALPINE_NETBOOT_SHA256", "0" * 64)
    result = commission_assets.fetch(tmp_path)
    assert not result["ok"] and "netboot SHA256" in result["error"]
    assert (destination / "initramfs-lts").read_bytes() == b"previous"
    assert not (destination / commission_assets.MANIFEST).exists()


def test_netboot_rejects_links_duplicates_and_missing_payloads(tmp_path):
    import pytest

    for failure in ["link", "duplicate", "missing"]:
        archive_path = tmp_path / (failure + ".tar.gz")
        with tarfile.open(archive_path, "w:gz") as archive:
            for name in bootselect.COMMISSION_FILES:
                if failure == "missing" and name == "initramfs-lts":
                    continue
                info = tarfile.TarInfo("boot/" + name)
                if failure == "link" and name == "initramfs-lts":
                    info.type = tarfile.SYMTYPE
                    info.linkname = "/etc/passwd"
                else:
                    info.size = 4
                archive.addfile(info, io.BytesIO(b"test") if info.isfile() else None)
                if failure == "duplicate" and name == "initramfs-lts":
                    archive.addfile(info, io.BytesIO(b"test"))
        with pytest.raises(ValueError):
            commission_assets.extract_netboot(archive_path, tmp_path, {"files": {}})


def test_missing_base_packages_preserves_existing_payload(tmp_path, monkeypatch):
    downloader(monkeypatch, image(include_packages=False))
    destination = tmp_path / "assets/commission" / bootselect.ALPINE_VERSION
    destination.mkdir(parents=True)
    (destination / "vmlinuz-lts").write_bytes(b"previous")
    result = commission_assets.fetch(tmp_path)
    assert not result["ok"]
    assert (destination / "vmlinuz-lts").read_bytes() == b"previous"
    assert not (destination / commission_assets.MANIFEST).exists()


def test_inspection_and_wipe_boot_with_deployer_local_packages():
    base = "http://192.0.2.10:8090"
    token = "test_fixture_nonce_12345678"
    for script in [
        bootselect.render_inspection_ipxe(base, token),
        bootselect.render_commission_ipxe(base, token, ["naa.6000000000000001"]),
    ]:
        assert "ip=dhcp" in script
        assert "BOOTIF=01-${net0/mac:hexhyp}" in script
        assert (
            "alpine_repo=" + base + "/assets/commission/" + bootselect.ALPINE_VERSION + "/apks"
            in script
        )
        assert "https://dl-cdn" not in script
        assert "/assets/commission/" + bootselect.ALPINE_VERSION + "/initramfs-lts" in script


def test_overlay_enables_full_hardware_drivers_before_inventory():
    import tarfile

    with tarfile.open(fileobj=io.BytesIO(bootselect.build_apkovl()), mode="r:gz") as archive:
        for name in ["devfs", "dmesg", "mdev", "hwdrivers", "modloop"]:
            link = archive.getmember("etc/runlevels/sysinit/" + name)
            assert link.issym() and link.linkname == "/etc/init.d/" + name
        assert archive.getmember("etc/runlevels/default/local").issym()


def test_overlay_is_identical_across_retries_at_different_times(monkeypatch):
    import time

    monkeypatch.setattr(time, "time", lambda: 1000)
    first = bootselect.build_apkovl()
    monkeypatch.setattr(time, "time", lambda: 2000)
    assert bootselect.build_apkovl() == first


def test_malformed_cache_manifest_is_repaired(tmp_path, monkeypatch):
    import json

    calls = downloader(monkeypatch, image())
    destination = tmp_path / "assets/commission" / bootselect.ALPINE_VERSION
    destination.mkdir(parents=True)
    (destination / commission_assets.MANIFEST).write_text(
        json.dumps(
            {
                "iso_sha256": commission_assets.ALPINE_ISO_SHA256,
                "files": list(bootselect.COMMISSION_FILES) + ["index", "package"],
            }
        )
    )
    assert not commission_assets.cached(destination)
    assert commission_assets.fetch(tmp_path)["ok"]
    assert len(calls) == 2


def test_failed_publish_restores_previous_payload_and_retry_recovers(tmp_path, monkeypatch):
    downloader(monkeypatch, image())
    assert commission_assets.fetch(tmp_path)["ok"]
    destination = tmp_path / "assets/commission" / bootselect.ALPINE_VERSION
    (destination / "vmlinuz-lts").write_bytes(b"previous")
    rename = Path.rename

    def fail_stage_publish(path, target):
        if path.name.startswith(".prepare-") and not path.name.endswith("-previous"):
            raise OSError("injected publication failure")
        return rename(path, target)

    monkeypatch.setattr(Path, "rename", fail_stage_publish)
    assert not commission_assets.fetch(tmp_path)["ok"]
    assert (destination / "vmlinuz-lts").read_bytes() == b"previous"
    monkeypatch.setattr(Path, "rename", rename)
    assert commission_assets.fetch(tmp_path)["ok"]
    assert (destination / "vmlinuz-lts").read_bytes() == b"kernel"
