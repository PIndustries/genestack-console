"""A native network save must preserve the cloud and be safe to retry offline."""

import json

import pytest

from app.services import provisioning_network as network
from tests.test_envconfig import _create_env

CONFIG = {
    "interface": "eth1",
    "range_start": "10.20.0.192",
    "range_end": "10.20.0.254",
    "next_server": "10.20.0.50",
    "netmask": "255.255.255.0",
    "gateway": "10.20.0.1",
    "dns": "1.1.1.1",
    "http_port": 8088,
}
CHOICE = {
    "id": "eth1:10.20.0.50/24",
    "name": "eth1",
    "address": "10.20.0.50/24",
    "mac": "52:54:00:00:00:01",
    "suggested_config": CONFIG,
}


@pytest.fixture
def prepared(client, admin_headers, monkeypatch):
    monkeypatch.setattr(network, "interfaces", lambda: [CHOICE])
    env = _create_env(client, admin_headers)
    eid = env["id"]
    document = """provider: kubespray
servers:
  existing:
    ip: 192.0.2.10
    roles: [compute]
secrets:
  existing:
    data:
      password: fixture-keep-this-value
"""
    response = client.put(
        f"/api/v1/environments/{eid}/config", headers=admin_headers, json={"yaml_text": document}
    )
    assert response.status_code == 201, response.text
    return eid, response.json()["version"]


def test_network_settings_are_discoverable_without_secret_roundtrip(
    client, admin_headers, prepared
):
    eid, version = prepared
    value = client.get(f"/api/v1/environments/{eid}/config/provider", headers=admin_headers).json()
    assert value["supports_provisioning_config"] is True
    assert value["config_version"] == version
    assert value["pxe"] == {}
    assert value["provisioning_interfaces"] == [CHOICE]
    assert "fixture-keep-this-value" not in json.dumps(value)


def test_save_preserves_config_and_retry_does_not_create_another_version(
    client, admin_headers, prepared, monkeypatch
):
    import yaml

    from app.services import pxe

    monkeypatch.setattr(
        pxe,
        "ensure_assets_and_config",
        lambda *a, **k: pytest.fail("Save must not start DHCP or boot a machine"),
    )
    eid, version = prepared
    path = f"/api/v1/environments/{eid}/config/provider"
    body = {"provider": "kubespray", "pxe": CONFIG, "expected_version": version}
    first = client.put(path, headers=admin_headers, json=body)
    assert first.status_code == 200, first.text
    assert first.json()["provisioning_config_saved"] is True
    retry = client.put(path, headers=admin_headers, json=body)
    assert retry.status_code == 200 and retry.json()["version"] == first.json()["version"]
    saved = client.get(f"/api/v1/environments/{eid}/config", headers=admin_headers).json()
    document = yaml.safe_load(saved["yaml"])
    assert document["servers"]["existing"]["ip"] == "192.0.2.10"
    assert document["secrets"]["existing"]["data"]["password"] == "***"
    assert document["pxe"] == CONFIG
    assert document["provider"] == "kubespray"


def test_stale_edit_conflicts_without_overwriting_newer_settings(client, admin_headers, prepared):
    eid, version = prepared
    path = f"/api/v1/environments/{eid}/config/provider"
    body = {"provider": "kubespray", "pxe": CONFIG, "expected_version": version}
    assert client.put(path, headers=admin_headers, json=body).status_code == 200
    changed = dict(body, pxe=dict(CONFIG, gateway="10.20.0.2"))
    assert client.put(path, headers=admin_headers, json=changed).status_code == 409
    assert client.get(path, headers=admin_headers).json()["pxe"]["gateway"] == "10.20.0.1"


@pytest.mark.parametrize(
    "change",
    [
        {"range_start": "10.20.0.1"},
        {"range_end": "10.21.0.254"},
        {"next_server": "10.20.0.51"},
        {"gateway": "10.21.0.1"},
        {"dns": "127.0.0.53"},
        {"http_port": 8080},
        {"http_port": True},
        {"netmask": "255.0.0.0"},
        {"interface": "eth9"},
        {"range_start": "10.20.0.255"},
    ],
)
def test_invalid_network_never_creates_a_config_version(client, admin_headers, prepared, change):
    eid, version = prepared
    path = f"/api/v1/environments/{eid}/config/provider"
    response = client.put(
        path,
        headers=admin_headers,
        json={"provider": "kubespray", "pxe": dict(CONFIG, **change), "expected_version": version},
    )
    assert response.status_code == 422, response.text
    assert client.get(path, headers=admin_headers).json()["config_version"] == version


def test_network_save_requires_version_and_operator(
    client, admin_headers, viewer_headers, prepared
):
    eid, version = prepared
    path = f"/api/v1/environments/{eid}/config/provider"
    assert (
        client.put(
            path, headers=admin_headers, json={"provider": "kubespray", "pxe": CONFIG}
        ).status_code
        == 422
    )
    assert (
        client.put(
            path,
            headers=viewer_headers,
            json={"provider": "kubespray", "pxe": CONFIG, "expected_version": version},
        ).status_code
        == 403
    )


def test_suggestions_skip_vpn_and_container_links_and_exclude_server_gateway(monkeypatch):
    records = [
        {
            "ifname": "eth1",
            "link_type": "ether",
            "flags": ["UP"],
            "address": CHOICE["mac"],
            "addr_info": [{"family": "inet", "local": "10.20.0.250", "prefixlen": 24}],
        },
        {
            "ifname": "wg0",
            "link_type": "none",
            "flags": ["UP"],
            "addr_info": [{"family": "inet", "local": "10.30.0.1", "prefixlen": 24}],
        },
        {
            "ifname": "docker0",
            "link_type": "ether",
            "flags": ["UP"],
            "addr_info": [{"family": "inet", "local": "172.17.0.1", "prefixlen": 16}],
        },
    ]
    monkeypatch.setattr(
        network,
        "_json_command",
        lambda argv: records if "address" in argv else [{"dev": "eth1", "gateway": "10.20.0.249"}],
    )
    monkeypatch.setattr(network, "dns_servers", lambda: ["1.1.1.1"])
    values = network.interfaces()
    assert len(values) == 1 and values[0]["name"] == "eth1"
    suggestion = values[0]["suggested_config"]
    assert suggestion["range_end"] == "10.20.0.248"
    assert suggestion["range_start"] <= suggestion["range_end"]


def test_first_save_uses_empty_configuration_version(client, admin_headers, monkeypatch):
    monkeypatch.setattr(network, "interfaces", lambda: [CHOICE])
    eid = _create_env(client, admin_headers)["id"]
    path = f"/api/v1/environments/{eid}/config/provider"
    snapshot = client.get(path, headers=admin_headers).json()
    assert snapshot["config_version"] == 0
    response = client.put(
        path,
        headers=admin_headers,
        json={
            "provider": snapshot["provider"],
            "expected_version": 0,
            "pxe": CONFIG,
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["version"] == 1
    assert client.get(path, headers=admin_headers).json()["pxe"] == CONFIG
