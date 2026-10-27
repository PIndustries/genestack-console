"""Factory disk URLs must produce one source-matched, complete PXE payload."""

import hashlib
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.services import factory_pxe_assets as factory
from app.services import pxe
from app.services.bootselect import render_profile_ipxe
from app.services.talos import DEFAULT_TALOS_IMAGE_URL
from tests.test_pxe import PXE_CFG

KERNEL = b"MZ" + b"fixture-kernel" * 50
INITRD = b"\xfd7zXZ\x00fixture-initramfs"


def downloader(calls, fail_second=False):
    def download(url, log, *, dest_dir):
        calls.append(url)
        if fail_second and url.endswith(".xz"):
            raise RuntimeError("simulated network loss")
        root = Path(dest_dir)
        root.mkdir(parents=True, exist_ok=True)
        content = INITRD if url.endswith(".xz") else KERNEL
        path = root / url.rsplit("/", 1)[-1]
        path.write_bytes(content)
        return path, hashlib.sha256(content).hexdigest()

    return download


def test_default_factory_disk_downloads_matching_boot_files_and_profile(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(pxe, "download_factory_image", downloader(calls))
    result = pxe.fetch_talos_assets(DEFAULT_TALOS_IMAGE_URL, tmp_path)
    expected = factory.model(DEFAULT_TALOS_IMAGE_URL)
    assert calls == [expected["base"] + "/kernel-amd64", expected["base"] + "/initramfs-amd64.xz"]
    assert Path(result["kernel"]).read_bytes() == KERNEL
    assert Path(result["initrd"]).read_bytes() == INITRD
    assert result["assets_relative_path"] == expected["path"]
    script = render_profile_ipxe(
        "talos", "http://10.0.2.15:8088", None, talos_assets_path=result["assets_relative_path"]
    )
    assert "http://10.0.2.15:8088/" + expected["path"] + "/vmlinuz" in script
    assert "http://10.0.2.15:8088/" + expected["path"] + "/initramfs.xz" in script
    assert not pxe.fetch_talos_assets(DEFAULT_TALOS_IMAGE_URL, tmp_path)["changed"]
    assert len(calls) == 2


def test_version_or_schematic_change_never_reuses_another_payload(tmp_path):
    calls = []
    first = factory.model(DEFAULT_TALOS_IMAGE_URL)
    second = factory.model(
        re.sub(r"/v[0-9]+\.[0-9]+\.[0-9]+/", "/v99.0.1/", DEFAULT_TALOS_IMAGE_URL)
    )
    assert first and second and first["path"] != second["path"]
    one = factory.fetch(first, tmp_path, downloader(calls), None)
    two = factory.fetch(second, tmp_path, downloader(calls), None)
    assert one["kernel"] != two["kernel"] and len(calls) == 4
    assert Path(one["kernel"]).read_bytes() == KERNEL


def test_failed_pair_keeps_previous_ready_payload_and_never_publishes_partial(tmp_path):
    calls = []
    first = factory.model(DEFAULT_TALOS_IMAGE_URL)
    second = factory.model(
        re.sub(r"/v[0-9]+\.[0-9]+\.[0-9]+/", "/v99.0.1/", DEFAULT_TALOS_IMAGE_URL)
    )
    ready = factory.fetch(first, tmp_path, downloader(calls), None)
    with pytest.raises(RuntimeError, match="network loss"):
        factory.fetch(second, tmp_path, downloader(calls, fail_second=True), None)
    assert Path(ready["kernel"]).read_bytes() == KERNEL
    assert Path(ready["initrd"]).read_bytes() == INITRD
    assert not (tmp_path / second["path"]).exists()
    assert not list((tmp_path / "assets/talos").glob(".talos-stage-*"))


def test_corrupt_cache_is_rebuilt_as_a_pair(tmp_path):
    calls = []
    expected = factory.model(DEFAULT_TALOS_IMAGE_URL)
    result = factory.fetch(expected, tmp_path, downloader(calls), None)
    Path(result["initrd"]).write_bytes(b"broken")
    assert factory.fetch(expected, tmp_path, downloader(calls), None)["changed"]
    assert len(calls) == 4
    assert Path(result["initrd"]).read_bytes() == INITRD


def test_non_kernel_http_payload_is_never_published(tmp_path):
    def bad(url, log, *, dest_dir):
        Path(dest_dir).mkdir(parents=True, exist_ok=True)
        path = Path(dest_dir) / "html"
        path.write_bytes(b"<html>upstream error</html>")
        return path, "not-used"

    expected = factory.model(DEFAULT_TALOS_IMAGE_URL)
    with pytest.raises(ValueError, match="expected Linux boot asset"):
        factory.fetch(expected, tmp_path, bad, None)
    assert not (tmp_path / expected["path"]).exists()


@pytest.mark.parametrize(
    "url",
    [
        "http://factory.talos.dev/image/" + "a" * 64 + "/v1.14.2/metal-amd64.qcow2",
        DEFAULT_TALOS_IMAGE_URL.replace("factory.talos.dev", "factory.talos.dev.example"),
        DEFAULT_TALOS_IMAGE_URL + "?token=not-forwarded",
        DEFAULT_TALOS_IMAGE_URL.replace("https://", "https://user:password@"),
        "https://custom.example/metal-amd64.qcow2",
    ],
)
def test_conversion_does_not_rewrite_unrecognized_or_credentialed_urls(url):
    assert factory.model(url) is None


def test_prepared_profile_uses_the_published_source_specific_pair(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(pxe, "download_factory_image", downloader(calls))
    result = pxe.ensure_assets_and_config(
        SimpleNamespace(name="factory-native"),
        {"pxe": dict(PXE_CFG), "talos": {"image_url": DEFAULT_TALOS_IMAGE_URL}},
        Settings(data_dir=tmp_path),
        profiles=[{"pxe_mac": "52:54:00:27:10:87", "next_boot": "talos"}],
        apply_runtime=False,
    )
    assert result["ok"], result
    expected = factory.model(DEFAULT_TALOS_IMAGE_URL)
    assert Path(result["assets_dir"]) == tmp_path / "pxe" / expected["path"]
    profile = (tmp_path / "pxe/mac/52-54-00-27-10-87.ipxe").read_text()
    assert expected["path"] + "/vmlinuz" in profile
    assert expected["path"] + "/initramfs.xz" in profile
    assert (Path(result["assets_dir"]) / "vmlinuz").read_bytes() == KERNEL


def test_historical_xz_asset_name_can_contain_the_real_factory_zstd_format(tmp_path):
    def download(url, log, *, dest_dir):
        Path(dest_dir).mkdir(parents=True, exist_ok=True)
        data = b"\x28\xb5\x2f\xfd" + b"fixture-zstd" if url.endswith(".xz") else KERNEL
        path = Path(dest_dir) / url.rsplit("/", 1)[-1]
        path.write_bytes(data)
        return path, hashlib.sha256(data).hexdigest()

    result = factory.fetch(factory.model(DEFAULT_TALOS_IMAGE_URL), tmp_path, download, None)
    assert Path(result["initrd"]).read_bytes().startswith(b"\x28\xb5\x2f\xfd")
