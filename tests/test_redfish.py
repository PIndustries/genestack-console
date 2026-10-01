"""Redfish client tests — behavior against a mocked httpx layer."""

from __future__ import annotations

import httpx
import pytest

from app.services import redfish
from app.services.redfish import RedfishError


class FakeResponse:
    def __init__(self, status_code: int = 200, payload=None, content: bytes = b"{}"):
        self.status_code = status_code
        self._payload = payload
        self.content = content

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeClient:
    """Stand-in for httpx.Client: records requests, delegates to a handler."""

    def __init__(self, handler, requests: list, **kwargs):
        self._handler = handler
        self._requests = requests
        self.kwargs = kwargs

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def request(self, method, path, json=None):
        self._requests.append({"method": method, "path": path, "json": json})
        result = self._handler(method, path, json)
        if isinstance(result, Exception):
            raise result
        return result

    def get(self, path):
        return self.request("GET", path)


def _patch_client(monkeypatch, handler):
    requests: list = []

    def factory(**kwargs):
        return FakeClient(handler, requests, **kwargs)

    monkeypatch.setattr(httpx, "Client", factory)
    return requests


# ---------------------------------------------------------------------------
# power_state
# ---------------------------------------------------------------------------


def test_power_state_returns_system_power_state(monkeypatch):
    requests = _patch_client(
        monkeypatch, lambda m, p, j: FakeResponse(payload={"PowerState": "On"})
    )
    assert redfish.power_state("bmc.example.com", "root", "calvin") == "On"
    assert requests[0]["method"] == "GET"
    assert requests[0]["path"] == "/redfish/v1/Systems/1"


def test_client_normalizes_host_and_disables_verify(monkeypatch):
    captured = {}

    def factory(**kwargs):
        captured.update(kwargs)
        return FakeClient(
            lambda m, p, j: FakeResponse(payload={"PowerState": "Off"}), [], **kwargs
        )

    monkeypatch.setattr(httpx, "Client", factory)
    assert redfish.power_state("10.1.2.3", "root", "calvin") == "Off"
    assert captured["base_url"] == "https://10.1.2.3"
    assert captured["verify"] is False
    assert captured["auth"] == ("root", "calvin")
    assert captured["timeout"] == redfish.TIMEOUT_SECONDS


def test_power_state_missing_field_raises(monkeypatch):
    _patch_client(monkeypatch, lambda m, p, j: FakeResponse(payload={"Name": "System"}))
    with pytest.raises(RedfishError, match="PowerState"):
        redfish.power_state("bmc", "u", "p")


# ---------------------------------------------------------------------------
# set_pxe_boot
# ---------------------------------------------------------------------------


def test_set_pxe_boot_patches_boot_override(monkeypatch):
    requests = _patch_client(
        monkeypatch, lambda m, p, j: FakeResponse(payload={"ok": True})
    )
    redfish.set_pxe_boot("bmc", "u", "p")
    assert requests[0]["method"] == "PATCH"
    assert requests[0]["path"] == "/redfish/v1/Systems/1"
    assert requests[0]["json"] == {
        "Boot": {"BootSourceOverrideEnabled": "Once", "BootSourceOverrideTarget": "Pxe"}
    }


# ---------------------------------------------------------------------------
# power
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("action", "reset_type"),
    [("on", "On"), ("off", "ForceOff"), ("restart", "ForceRestart"), ("ON", "On")],
)
def test_power_maps_actions_to_reset_types(monkeypatch, action, reset_type):
    requests = _patch_client(monkeypatch, lambda m, p, j: FakeResponse(content=b""))
    assert redfish.power("bmc", "u", "p", action) == reset_type
    assert requests[0]["method"] == "POST"
    assert requests[0]["path"] == "/redfish/v1/Systems/1/Actions/ComputerSystem.Reset"
    assert requests[0]["json"] == {"ResetType": reset_type}


def test_power_unknown_action_raises(monkeypatch):
    requests = _patch_client(monkeypatch, lambda m, p, j: FakeResponse())
    with pytest.raises(RedfishError, match="unknown power action"):
        redfish.power("bmc", "u", "p", "nuke")
    assert requests == [], "no HTTP call for an unknown action"


# ---------------------------------------------------------------------------
# system_macs
# ---------------------------------------------------------------------------


def test_system_macs_follows_member_links(monkeypatch):
    interfaces = {
        "/redfish/v1/Systems/1/EthernetInterfaces": FakeResponse(
            payload={
                "Members": [
                    {"@odata.id": "/redfish/v1/Systems/1/EthernetInterfaces/1"},
                    {"@odata.id": "/redfish/v1/Systems/1/EthernetInterfaces/2"},
                ]
            }
        ),
        "/redfish/v1/Systems/1/EthernetInterfaces/1": FakeResponse(
            payload={"MACAddress": "aa:bb:cc:dd:ee:01"}
        ),
        "/redfish/v1/Systems/1/EthernetInterfaces/2": FakeResponse(
            payload={"PermanentMACAddress": "aa:bb:cc:dd:ee:02"}
        ),
    }
    _patch_client(monkeypatch, lambda m, p, j: interfaces[p])
    assert redfish.system_macs("bmc", "u", "p") == [
        "aa:bb:cc:dd:ee:01",
        "aa:bb:cc:dd:ee:02",
    ]


def test_system_macs_empty_collection(monkeypatch):
    _patch_client(monkeypatch, lambda m, p, j: FakeResponse(payload={"Members": []}))
    assert redfish.system_macs("bmc", "u", "p") == []


# ---------------------------------------------------------------------------
# error mapping
# ---------------------------------------------------------------------------


def test_http_error_maps_to_redfish_error_with_status(monkeypatch):
    _patch_client(
        monkeypatch, lambda m, p, j: FakeResponse(status_code=401, content=b"denied")
    )
    with pytest.raises(RedfishError, match="HTTP 401") as excinfo:
        redfish.power_state("bmc", "u", "p")
    assert excinfo.value.status_code == 401


def test_transport_error_maps_to_redfish_error(monkeypatch):
    _patch_client(
        monkeypatch,
        lambda m, p, j: httpx.ConnectError("connection refused"),
    )
    with pytest.raises(RedfishError, match="connection refused") as excinfo:
        redfish.power_state("bmc", "u", "p")
    assert excinfo.value.status_code is None
