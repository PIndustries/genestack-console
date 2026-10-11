"""Redfish HTTP contract: one generic path set, no vendor manager URLs.

``_request`` is patched. These tests do not open sockets or contact a BMC.
Vendor-specific manager paths are not implemented.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.services import baremetal, redfish
from app.services.redfish import RedfishError

INSERT_MEDIA_PATH = (
    "/redfish/v1/Managers/1/VirtualMedia/CD/Actions/VirtualMedia.InsertMedia"
)


def _capture_request():
    calls: list[dict] = []

    def fake_request(method, host, username, password, path, json=None):
        calls.append(
            {
                "method": method,
                "host": host,
                "username": username,
                "password": password,
                "path": path,
                "json": json,
            }
        )
        return {}

    return calls, fake_request


def test_power_posts_reset():
    calls, fake_request = _capture_request()
    with patch("app.services.redfish._request", side_effect=fake_request):
        sent = redfish.power("bmc.example", "root", "calvin", "on")
    assert sent == "On"
    assert calls == [
        {
            "method": "POST",
            "host": "bmc.example",
            "username": "root",
            "password": "calvin",
            "path": redfish.RESET_PATH,
            "json": {"ResetType": "On"},
        }
    ]
    assert calls[0]["path"].endswith("/Actions/ComputerSystem.Reset")


def test_pxe_once_patches_boot():
    calls, fake_request = _capture_request()
    with patch("app.services.redfish._request", side_effect=fake_request):
        redfish.set_pxe_boot("bmc.example", "root", "calvin")
    assert len(calls) == 1
    call = calls[0]
    assert call["method"] == "PATCH"
    assert call["path"] == redfish.SYSTEM_PATH
    assert call["json"]["Boot"]["BootSourceOverrideTarget"] == "Pxe"
    assert call["json"]["Boot"]["BootSourceOverrideEnabled"] == "Once"


def test_virtual_cd_posts_insert():
    calls, fake_request = _capture_request()
    image = "http://10.0.0.1/metal-amd64.iso"
    with patch("app.services.redfish._request", side_effect=fake_request):
        path = redfish.insert_virtual_media("bmc.example", "root", "calvin", image)
        with pytest.raises(RedfishError, match="empty"):
            redfish.insert_virtual_media("bmc.example", "root", "calvin", "  ")
    assert path == INSERT_MEDIA_PATH
    assert calls == [
        {
            "method": "POST",
            "host": "bmc.example",
            "username": "root",
            "password": "calvin",
            "path": INSERT_MEDIA_PATH,
            "json": {"Image": image, "Inserted": True},
        }
    ]


def test_ilo_and_idrac_use_the_same_recorded_paths():
    """Both vendors get the same generic paths. Hardware is not contacted."""
    calls, fake_request = _capture_request()
    image = "http://10.0.0.1/metal-amd64.iso"
    with patch("app.services.redfish._request", side_effect=fake_request):
        for host in ("ilo.example", "idrac.example"):
            redfish.power(host, "u", "p", "on")
            redfish.set_pxe_boot(host, "u", "p")
            redfish.insert_virtual_media(host, "u", "p", image)
    by_host: dict[str, list[str]] = {}
    bodies: dict[str, list] = {}
    for call in calls:
        by_host.setdefault(call["host"], []).append(call["path"])
        bodies.setdefault(call["host"], []).append(call["json"])
    assert set(by_host) == {"ilo.example", "idrac.example"}
    assert by_host["ilo.example"] == by_host["idrac.example"]
    assert by_host["ilo.example"] == [
        redfish.RESET_PATH,
        redfish.SYSTEM_PATH,
        INSERT_MEDIA_PATH,
    ]
    assert bodies["ilo.example"] == bodies["idrac.example"]
    for path in by_host["ilo.example"]:
        lowered = path.lower()
        assert "ilo" not in lowered
        assert "idrac" not in lowered


def test_iso_boot_uses_given_url():
    image = "https://images.example/metal-amd64.iso"
    calls, fake_request = _capture_request()
    node = SimpleNamespace(
        name="ctrl1",
        bmc_host="ilo.example",
        bmc_username="admin",
        bmc_password_encrypted="secret",
    )
    env = SimpleNamespace(id="env-1")
    db = SimpleNamespace()
    logs: list[str] = []

    with (
        patch("app.services.redfish._request", side_effect=fake_request),
        patch.object(baremetal, "_cold_cycle", lambda *a, **k: None),
    ):
        result = baremetal.iso_boot(
            db,
            env,
            node,
            dry_run=False,
            log=logs.append,
            image_url=f"  {image}  ",
        )
    assert result["ok"] is True
    assert result["image_url"] == image
    inserts = [c for c in calls if c["path"] == INSERT_MEDIA_PATH]
    assert inserts == [
        {
            "method": "POST",
            "host": "ilo.example",
            "username": "admin",
            "password": "secret",
            "path": INSERT_MEDIA_PATH,
            "json": {"Image": image, "Inserted": True},
        }
    ]

    calls.clear()
    with (
        patch("app.services.redfish._request", side_effect=fake_request),
        patch("app.services.redfish.insert_virtual_media") as insert,
    ):
        dry = baremetal.iso_boot(
            db,
            env,
            node,
            dry_run=True,
            log=logs.append,
            image_url=image,
        )
    insert.assert_not_called()
    assert calls == []
    assert dry == {"ok": True, "dry_run": True, "image_url": image}
    assert any(image in line for line in logs)
