"""Tests for app/services/pxe.py — PXE/DHCP sidecar rendering + asset prep."""

from __future__ import annotations

import io
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.services import bootselect, envconfig, pxe

PXE_CFG = {
    "interface": "eth1",
    "range_start": "10.10.0.100",
    "range_end": "10.10.0.199",
    "netmask": "255.255.255.0",
    "gateway": "10.10.0.1",
    "dns": "10.10.0.1",
}

NODES = [
    {"name": "node-a", "pxe_mac": "aa:bb:cc:dd:ee:01", "expected_ip": "10.10.0.11"},
    {"name": "node-b", "pxe_mac": "aa:bb:cc:dd:ee:02", "expected_ip": "10.10.0.12"},
    # No expected_ip — no reservation, silently skipped.
    {"name": "node-c", "pxe_mac": "aa:bb:cc:dd:ee:03", "expected_ip": None},
]


def _tar_asset() -> bytes:
    """In-memory tar with a talos-style boot/ layout (kernel + initramfs)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as archive:
        for name, payload in (
            ("boot/vmlinuz", b"fake-kernel"),
            ("boot/initramfs.xz", b"fake-initramfs"),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    return buf.getvalue()


def _fake_download(content: bytes, sha256: str = "deadbeef"):
    """Stand-in for the disk-streaming download_factory_image: write the
    bytes to dest_dir and return (path, sha256) like the real one."""

    def _download(
        url: str, log, dest_dir, filename=None, max_bytes: int = 0
    ):  # noqa: ARG001
        if log:
            log(f"[talos-image] downloaded {len(content)} bytes sha256={sha256}")
        dest = Path(dest_dir)
        dest.mkdir(parents=True, exist_ok=True)
        path = dest / (filename or "factory-image")
        path.write_bytes(content)
        return path, sha256

    return _download


# ---------------------------------------------------------------------------
# render_pxe_config
# ---------------------------------------------------------------------------


def test_render_pxe_config_range_and_interface():
    conf = pxe.render_pxe_config([], PXE_CFG)
    assert "interface=eth1" in conf
    assert "bind-interfaces" in conf
    assert "dhcp-authoritative" in conf
    assert "dhcp-range=10.10.0.100,10.10.0.199,255.255.255.0,1h" in conf
    assert "dhcp-option=option:router,10.10.0.1" in conf
    assert "dhcp-option=option:dns-server,10.10.0.1" in conf


def test_render_pxe_config_chainload_lines():
    conf = pxe.render_pxe_config([], PXE_CFG)
    # iPXE clients get boot.ipxe over HTTP from the sidecar; BIOS PXE ROMs
    # get the undionly fallback first.
    assert 'dhcp-match=set:ipxe,option:user-class,"iPXE"' in conf
    assert "dhcp-boot=tag:ipxe,http://10.10.0.1:8080/boot.ipxe" in conf
    assert "dhcp-boot=undionly.kpxe" in conf


def test_render_pxe_config_next_server_and_port_override():
    cfg = {**PXE_CFG, "next_server": "10.10.0.2", "http_port": 9090}
    conf = pxe.render_pxe_config([], cfg)
    assert "dhcp-boot=tag:ipxe,http://10.10.0.2:9090/boot.ipxe" in conf


def test_render_pxe_config_reservations():
    conf = pxe.render_pxe_config(NODES, PXE_CFG)
    assert "dhcp-host=aa:bb:cc:dd:ee:01,node-a,10.10.0.11" in conf
    assert "dhcp-host=aa:bb:cc:dd:ee:02,node-b,10.10.0.12" in conf
    # node-c has no expected_ip — no reservation emitted.
    assert "aa:bb:cc:dd:ee:03" not in conf


def test_render_pxe_config_requires_core_keys():
    with pytest.raises(pxe.PxeError, match="range_start"):
        pxe.render_pxe_config([], {"interface": "eth1", "range_end": "10.10.0.199"})


# ---------------------------------------------------------------------------
# render_boot_ipxe
# ---------------------------------------------------------------------------


def test_render_boot_ipxe_chains_per_mac_and_talos_stays_separate():
    script = pxe.render_boot_ipxe("http://10.10.0.1:8080")
    assert script.startswith("#!ipxe")
    assert (
        "chain http://10.10.0.1:8080/mac/${net0/mac:hexhyp}.ipxe || exit" in script
    )
    assert "talos.platform" not in script
    talos = bootselect.render_talos_ipxe("http://10.10.0.1:8080")
    assert (
        "kernel http://10.10.0.1:8080/assets/vmlinuz "
        "talos.platform=metal ip=dhcp console=tty0 console=ttyS0,115200" in talos
    )
    assert "initrd http://10.10.0.1:8080/assets/initramfs.xz" in talos
    assert talos.rstrip().endswith("boot")
    # Maintenance-mode boot: no machine-config source on the cmdline.
    assert "talos.config=" not in talos


# ---------------------------------------------------------------------------
# fetch_talos_assets
# ---------------------------------------------------------------------------


def test_fetch_talos_assets_extracts_tar(tmp_path, monkeypatch):
    monkeypatch.setattr(pxe, "download_factory_image", _fake_download(_tar_asset()))
    logs: list[str] = []
    result = pxe.fetch_talos_assets(
        "https://factory.example/talos.tar", tmp_path, logs.append
    )
    assert result["changed"] is True
    assert result["sha256"] == "deadbeef"
    assert (tmp_path / "assets" / "vmlinuz").read_bytes() == b"fake-kernel"
    assert (tmp_path / "assets" / "initramfs.xz").read_bytes() == b"fake-initramfs"


def test_fetch_talos_assets_skips_when_present(tmp_path, monkeypatch):
    def _boom(url, log, dest_dir, filename=None, max_bytes=0):  # pragma: no cover
        raise AssertionError("download should not run when assets exist")

    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "vmlinuz").write_bytes(b"k")
    (tmp_path / "assets" / "initramfs.xz").write_bytes(b"i")
    monkeypatch.setattr(pxe, "download_factory_image", _boom)
    result = pxe.fetch_talos_assets("https://factory.example/talos.tar", tmp_path)
    assert result["changed"] is False


def test_fetch_talos_assets_requires_https(tmp_path):
    with pytest.raises(pxe.PxeError, match="https://"):
        pxe.fetch_talos_assets("http://factory.example/talos.tar", tmp_path)


def test_fetch_talos_assets_unusable_image(tmp_path, monkeypatch):
    monkeypatch.setattr(
        pxe, "download_factory_image", _fake_download(b"not-an-archive")
    )
    with pytest.raises(pxe.PxeError, match="could not extract"):
        pxe.fetch_talos_assets("https://factory.example/talos.iso", tmp_path)


def test_fetch_talos_assets_extracts_iso(tmp_path, monkeypatch):
    """Factory ISO layout (/boot/vmlinuz + /boot/initramfs.xz) via pycdlib."""
    pycdlib = pytest.importorskip("pycdlib")
    iso = pycdlib.PyCdlib()
    iso.new(interchange_level=3, joliet=True)
    iso.add_directory(iso_path="/BOOT", joliet_path="/boot")
    iso.add_file(
        io.BytesIO(b"iso-kernel"),
        iso_path="/BOOT/VMLINUZ.;1",
        joliet_path="/boot/vmlinuz",
    )
    iso.add_file(
        io.BytesIO(b"iso-initramfs"),
        iso_path="/BOOT/INITRAMFS.XZ;1",
        joliet_path="/boot/initramfs.xz",
    )
    buf = io.BytesIO()
    iso.write_fp(buf)
    iso.close()
    monkeypatch.setattr(pxe, "download_factory_image", _fake_download(buf.getvalue()))
    result = pxe.fetch_talos_assets("https://factory.example/talos.iso", tmp_path)
    assert (tmp_path / "assets" / "vmlinuz").read_bytes() == b"iso-kernel"
    assert result["changed"] is True


# ---------------------------------------------------------------------------
# ensure_assets_and_config
# ---------------------------------------------------------------------------


def _settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path)


def test_ensure_assets_and_config_happy_path(tmp_path, monkeypatch):
    monkeypatch.setattr(pxe, "download_factory_image", _fake_download(_tar_asset()))
    doc = {
        "pxe": dict(PXE_CFG),
        "talos": {"image_url": "https://factory.example/talos.tar"},
        "servers": {
            "node-a": {
                "source": "baremetal",
                "pxe_mac": "aa:bb:cc:dd:ee:01",
                "ip": "10.10.0.11",
                "roles": ["control"],
            },
            "node-b": {"source": "static", "ip": "10.0.0.5", "roles": ["compute"]},
        },
    }
    env = SimpleNamespace(name="bm-env")
    logs: list[str] = []
    result = pxe.ensure_assets_and_config(env, doc, _settings(tmp_path), logs.append)
    assert result["ok"] is True, result
    assert result["changed"] is True
    assert result["nodes"] == 1  # only source: baremetal servers are reserved

    root = tmp_path / "pxe"
    conf = (root / "dnsmasq.conf").read_text()
    assert "dhcp-range=10.10.0.100,10.10.0.199,255.255.255.0,1h" in conf
    assert "dhcp-host=aa:bb:cc:dd:ee:01,node-a,10.10.0.11" in conf
    assert (root / "boot.ipxe").read_text().startswith("#!ipxe")
    assert (root / "assets" / "vmlinuz").read_bytes() == b"fake-kernel"
    # Product no longer logs docker restart hint; runtime is managed in-process
    assert any("[pxe]" in line for line in logs)

    # Second run: everything up to date — nothing rewritten, no download.
    monkeypatch.setattr(
        pxe,
        "download_factory_image",
        lambda *a, **k: pytest.fail("unexpected re-download"),  # pragma: no cover
    )
    again = pxe.ensure_assets_and_config(env, doc, _settings(tmp_path), logs.append)
    assert again["ok"] is True
    assert again["changed"] is False


def test_ensure_assets_and_config_missing_pxe_section(tmp_path):
    env = SimpleNamespace(name="no-pxe-env")
    result = pxe.ensure_assets_and_config(env, {}, _settings(tmp_path))
    assert result["ok"] is False
    assert "no pxe: section" in result["error"]


def test_ensure_assets_and_config_download_failure_never_raises(tmp_path, monkeypatch):
    def _fail(url, log, dest_dir, filename=None, max_bytes=0):
        from app.services.job_runner import ImageDownloadError

        raise ImageDownloadError("download failed: HTTP 404")

    monkeypatch.setattr(pxe, "download_factory_image", _fail)
    doc = {
        "pxe": dict(PXE_CFG),
        "talos": {"image_url": "https://factory.example/x.tar"},
    }
    env = SimpleNamespace(name="bm-env")
    result = pxe.ensure_assets_and_config(env, doc, _settings(tmp_path))
    assert result["ok"] is False
    assert "HTTP 404" in result["error"]


def test_ensure_assets_and_config_invalid_section_never_raises(tmp_path):
    doc = {"pxe": {"interface": "eth1"}}  # missing range_start/range_end
    env = SimpleNamespace(name="bm-env")
    result = pxe.ensure_assets_and_config(env, doc, _settings(tmp_path))
    assert result["ok"] is False
    assert "range_start" in result["error"]


def test_disk_profile_writes_an_exit_script_and_does_not_download(tmp_path, monkeypatch):
    monkeypatch.setattr(
        pxe,
        "download_factory_image",
        lambda *a, **k: pytest.fail("disk profile must not download"),
    )
    env = SimpleNamespace(name="bm-env")
    result = pxe.ensure_assets_and_config(
        env,
        {"pxe": dict(PXE_CFG)},
        _settings(tmp_path),
        profiles=[
            {
                "name": "node-a",
                "pxe_mac": "aa:bb:cc:dd:ee:01",
                "expected_ip": "10.10.0.11",
                "next_boot": "disk",
                "token": "should-not-appear",
            }
        ],
        apply_runtime=False,
    )
    assert result["ok"] is True, result
    root = tmp_path / "pxe"
    boot = (root / "boot.ipxe").read_text()
    assert "chain " in boot and "|| exit" in boot
    script = (root / "mac" / "aa-bb-cc-dd-ee-01.ipxe").read_text()
    assert "# profile: disk" in script
    assert "should-not-appear" not in script
    assert not (root / "assets" / "commission").exists()


def test_commission_profile_fetches_assets_without_the_network(tmp_path, monkeypatch):
    seen: list[str] = []

    def fake(url, log, dest_dir, filename=None, max_bytes=0):
        name = filename or "asset"
        seen.append(name)
        dest = Path(dest_dir) / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"asset")
        return dest, "abc"

    monkeypatch.setattr(pxe, "download_factory_image", fake)
    env = SimpleNamespace(name="bm-env")
    result = pxe.ensure_assets_and_config(
        env,
        {"pxe": dict(PXE_CFG)},
        _settings(tmp_path),
        profiles=[
            {
                "name": "node-a",
                "pxe_mac": "AA-BB-CC-DD-EE-01",
                "expected_ip": "10.10.0.11",
                "next_boot": "commission",
                "token": "tok-1",
            }
        ],
        apply_runtime=False,
    )
    assert result["ok"] is True, result
    assert set(seen) == {"vmlinuz-lts", "initramfs-lts", "modloop-lts"}
    script = (tmp_path / "pxe" / "mac" / "aa-bb-cc-dd-ee-01.ipxe").read_text()
    assert "gsc_wipe=1" in script
    assert "gsc_token=tok-1" in script
    assert "gsc_report=http://10.10.0.1:" in script
    apkovl = tmp_path / "pxe" / "assets" / "commission.apkovl.tar.gz"
    assert apkovl.is_file()
    with tarfile.open(apkovl, "r:gz") as archive:
        script_bytes = archive.extractfile("etc/local.d/commission.start").read()
    assert b"dd if=/dev/zero" in script_bytes
    assert b"poweroff -f" in script_bytes


# ---------------------------------------------------------------------------
# envconfig pxe: section validation
# ---------------------------------------------------------------------------


def test_pxe_doc_validated_and_known():
    doc, warnings = envconfig.parse_document(
        "pxe:\n"
        "  interface: eth1\n"
        "  range_start: 10.10.0.100\n"
        "  range_end: 10.10.0.199\n"
    )
    assert doc["pxe"]["interface"] == "eth1"
    assert warnings == []


def test_pxe_doc_unknown_key_warns():
    _doc, warnings = envconfig.parse_document(
        "pxe:\n"
        "  interface: eth1\n"
        "  range_start: 10.10.0.100\n"
        "  range_end: 10.10.0.199\n"
        "  bogus: true\n"
    )
    assert any("pxe: unknown key 'bogus'" in w for w in warnings)


def test_pxe_doc_missing_required_keys_rejected():
    with pytest.raises(envconfig.ConfigValidationError, match="range_start"):
        envconfig.parse_document("pxe:\n  interface: eth1\n  range_end: 10.10.0.199\n")


def test_pxe_doc_non_mapping_rejected():
    with pytest.raises(
        envconfig.ConfigValidationError, match="'pxe' must be a mapping"
    ):
        envconfig.parse_document("pxe: eth1\n")
