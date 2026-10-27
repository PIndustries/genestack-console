"""The real installer uses a sibling PXE directory shared with its sidecar."""
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.services import pxe
from app.services.pxe_paths import provisioning_root
from app.services.request_safety import under_root
from tests.test_pxe import PXE_CFG, _fake_download, _tar_asset


def test_installer_sibling_layout_can_prepare_actual_inspection_files(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    payload = tmp_path / "pxe"
    payload.mkdir()
    (data / "pxe").symlink_to("../pxe")
    monkeypatch.setattr(pxe, "download_factory_image", _fake_download(_tar_asset()))

    def commission(root, log):
        (root / "assets").mkdir(exist_ok=True)
        return {"ok": True, "changed": False}

    monkeypatch.setattr(pxe, "fetch_commission_assets", commission)
    result = pxe.ensure_assets_and_config(
        SimpleNamespace(name="fresh-installer"),
        {"pxe": dict(PXE_CFG), "talos": {"image_url": "https://factory.example/talos.tar"}},
        Settings(data_dir=data),
        profiles=[{"pxe_mac": "52:54:00:27:10:87", "next_boot": "inspect", "token": "a" * 32}],
        apply_runtime=False,
    )
    assert result["ok"], result
    assert (payload / "dnsmasq.conf").is_file()
    assert "profile: inspect" in (payload / "mac/52-54-00-27-10-87.ipxe").read_text()
    assert (payload / "assets/commission.apkovl.tar.gz").is_file()
    assert (data / "pxe").is_symlink(), "Preserve the installer's shared layout"
    assert provisioning_root(data) == payload
    assert under_root(provisioning_root(data), "agent-fixture") == payload / "agent-fixture"


def test_standard_directory_remains_supported(tmp_path):
    assert provisioning_root(tmp_path) == tmp_path / "pxe"


@pytest.mark.parametrize("target", ["/etc", "../outside", "../../pxe"])
def test_arbitrary_sibling_or_absolute_links_remain_rejected(tmp_path, target):
    data = tmp_path / "data"
    data.mkdir()
    (data / "pxe").symlink_to(target)
    with pytest.raises(ValueError, match="invalid path"):
        provisioning_root(data)


def test_installer_target_cannot_itself_escape_through_another_link(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "pxe").symlink_to("../pxe")
    (tmp_path / "pxe").symlink_to("/etc")
    with pytest.raises(ValueError, match="invalid path"):
        provisioning_root(data)


def test_agent_link_cannot_escape_valid_installer_payload(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    payload = tmp_path / "pxe"
    payload.mkdir()
    (data / "pxe").symlink_to("../pxe")
    (payload / "agent-fixture").symlink_to("/etc")
    with pytest.raises(ValueError, match="invalid path"):
        under_root(provisioning_root(data), "agent-fixture")
