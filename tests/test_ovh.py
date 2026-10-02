"""OVH dedicated-server import tests.

Service-level tests verify the v1 request-signing algorithm, endpoint
normalisation, the account consumer-key flow, defensive server normalisation
and the role-assignment heuristic. Endpoint tests run against the FastAPI
app with the OVH HTTP layer replaced by an httpx mock transport.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Any
from urllib.parse import unquote

import httpx
import pytest
import yaml

from app.config import Settings
from app.services.ovh import (
    CONSUMER_KEY_RULES,
    DEFAULT_BYOI_OS,
    DEFAULT_EFI_BOOTLOADER,
    DEFAULT_ENDPOINT,
    OvhClient,
    OvhError,
    assign_roles,
    byoi_customizations,
    classify_install_status,
    classify_ip,
    env_is_ovh,
    infer_ovh_image_type,
    inventory_indexes,
    normalize_server,
    pick_private_nic,
    planned_ovh_tags,
    resolve_ovh_service_name,
    server_may_inherit_ovh,
    split_server_ips,
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def test_settings_ovh_defaults_off():
    settings = Settings()
    assert settings.ovh_endpoint == ""
    assert settings.ovh_app_key == ""
    assert settings.ovh_app_secret == ""


def test_load_settings_parses_ovh_section(tmp_path):
    from app.config import load_settings

    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        yaml.safe_dump(
            {
                "ovh": {
                    "endpoint": "https://api.us.ovhcloud.com/1.0",
                    "app_key": "AK",
                    "app_secret": "AS",
                }
            }
        )
    )
    settings = load_settings(cfg)
    assert settings.ovh_endpoint == "https://api.us.ovhcloud.com/1.0"
    assert settings.ovh_app_key == "AK"
    assert settings.ovh_app_secret == "AS"

    # Absent section -> all empty (OVH disabled)
    cfg.write_text(yaml.safe_dump({"maas": {"url": "", "api_key": ""}}))
    settings = load_settings(cfg)
    assert settings.ovh_endpoint == ""
    assert settings.ovh_app_key == ""
    assert not hasattr(settings, "maas_url")


# ---------------------------------------------------------------------------
# Client: normalisation, signing, configuration states
# ---------------------------------------------------------------------------


def test_endpoint_normalisation():
    assert OvhClient(endpoint="eu.api.ovh.com").endpoint == DEFAULT_ENDPOINT
    assert OvhClient(endpoint="https://api.us.ovhcloud.com/1.0/").endpoint == (
        "https://api.us.ovhcloud.com/1.0"
    )
    assert OvhClient(endpoint="https://ca.api.ovh.com/").endpoint == (
        "https://ca.api.ovh.com/1.0"
    )
    assert OvhClient(endpoint="").endpoint == ""


def test_configuration_states():
    unconfigured = OvhClient(endpoint="", app_key="", app_secret="", consumer_key="CK")
    assert unconfigured.configured is False
    assert unconfigured.authenticated is False
    with pytest.raises(OvhError) as exc:
        unconfigured._require_configured()
    assert exc.value.status_code == 503

    app_only = OvhClient(
        endpoint=DEFAULT_ENDPOINT, app_key="AK", app_secret="AS", consumer_key=""
    )
    assert app_only.configured is True
    assert app_only.authenticated is False
    with pytest.raises(OvhError) as exc:
        app_only._require_authenticated()
    assert exc.value.status_code == 503


def test_signature_matches_ovh_v1_algorithm():
    """Signature is SHA-1 over '+'-joined secret+consumer+METHOD+URL+body+time."""
    client = OvhClient(
        endpoint=DEFAULT_ENDPOINT, app_key="AK", app_secret="S3CRET", consumer_key="CK"
    )
    url = client._full_url("/dedicated/server", None)
    body = json.dumps({"accessRules": CONSUMER_KEY_RULES}, separators=(",", ":"))
    ts = "1700000000"

    expected = (
        "$1$"
        + hashlib.sha1(
            "+".join(["S3CRET", "CK", "POST", url, body, ts]).encode("utf-8")
        ).hexdigest()
    )
    assert client._sign("POST", url, body, ts) == expected

    # GET with empty body signs with an empty body segment
    get_url = client._full_url("/me", None)
    expected_get = (
        "$1$"
        + hashlib.sha1(
            "+".join(["S3CRET", "CK", "GET", get_url, "", ts]).encode("utf-8")
        ).hexdigest()
    )
    assert client._sign("GET", get_url, "", ts) == expected_get


def test_full_url_sorts_query_params_deterministically():
    client = OvhClient(endpoint=DEFAULT_ENDPOINT, app_key="AK", app_secret="AS")
    url = client._full_url("/dedicated/server", {"b": "2", "a": "1"})
    assert url == DEFAULT_ENDPOINT + "/dedicated/server?a=1&b=2"


def test_mocked_http_transport_end_to_end():
    """Full signed flow over an injected httpx MockTransport."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/1.0/auth/time":
            return httpx.Response(200, json=int(time.time()) + 5)
        if request.url.path == "/1.0/auth/credential" and request.method == "POST":
            # Unsigned credential request: only the application header is sent.
            assert request.headers["X-Ovh-Application"] == "AK"
            assert "X-Ovh-Signature" not in request.headers
            return httpx.Response(
                200,
                json={
                    "state": "pendingValidation",
                    "consumerKey": "NEWCK",
                    "validationUrl": "https://eu.api.ovh.com/auth/?credentialToken=abc",
                },
            )
        if request.url.path == "/1.0/auth/credential/NEWCK":
            # Signed: verify the signature recomputes against the request.
            body = ""
            sig = request.headers["X-Ovh-Signature"]
            ts = request.headers["X-Ovh-Timestamp"]
            expected = (
                "$1$"
                + hashlib.sha1(
                    "+".join(
                        ["S3CRET", "NEWCK", "GET", str(request.url), body, ts]
                    ).encode("utf-8")
                ).hexdigest()
            )
            assert sig == expected
            return httpx.Response(200, json={"validationStatus": "pendingValidation"})
        if request.url.path == "/1.0/dedicated/server" and request.method == "GET":
            return httpx.Response(200, json=["dedicace-1", "dedicace-2"])
        if request.url.path.startswith("/1.0/dedicated/server/"):
            sid = request.url.path.rsplit("/", 1)[-1]
            return httpx.Response(
                200,
                json={
                    "serverId": sid,
                    "displayName": f"NS-{sid}",
                    "cores": 8,
                    "memory": 65536,
                    "status": "active",
                    "datacenter": "GRA11",
                    "ip": [{"ip": "198.51.100.10"}],
                    "disk": [{"size": 1024}],
                },
            )
        return httpx.Response(404, json={"message": f"no route {request.url.path}"})

    client = OvhClient(endpoint=DEFAULT_ENDPOINT, app_key="AK", app_secret="S3CRET")
    client._client = httpx.Client(
        transport=httpx.MockTransport(handler), timeout=client.timeout
    )
    client.consumer_key = "NEWCK"

    assert client._sync_time_delta() == 5

    req = client.request_consumer_key()
    assert req["consumerKey"] == "NEWCK"
    assert req["validationUrl"].startswith("https://eu.api.ovh.com/auth/")

    state = client.credential_state("NEWCK")
    assert state["validationStatus"] == "pendingValidation"

    servers = client.list_dedicated_servers()
    assert len(servers) == 2
    assert servers[0]["hostname"] == "NS-dedicace-1"
    assert servers[0]["ram_gb"] == 64
    assert servers[0]["ip"] == "198.51.100.10"


def test_list_servers_merges_hardware_specs():
    # US endpoints omit cores/memory/disk from the base object; the
    # per-server /specifications/hardware call supplies them and must merge.
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/1.0/auth/time":
            return httpx.Response(200, json=str(int(time.time()) + 5))
        if path == "/1.0/dedicated/server":
            return httpx.Response(200, json=["ns-us-1"])
        if path == "/1.0/dedicated/server/ns-us-1/specifications/hardware":
            return httpx.Response(
                200,
                json={
                    "memorySize": {"value": 131072, "unit": "MB"},
                    "coresPerProcessor": 16,
                    "numberOfProcessors": 1,
                    "diskGroups": [
                        {
                            "diskSize": {"unit": "GB", "value": 8000},
                            "numberOfDisks": 2,
                            "diskType": "SATA",
                        }
                    ],
                },
            )
        if path == "/1.0/dedicated/server/ns-us-1":
            return httpx.Response(
                200,
                json={
                    "serverId": 31345,
                    "name": "ns-us-1",
                    "ip": "203.0.113.20",
                    "commercialRange": "KS-6 | AMD Epyc 7351P",
                    "datacenter": "vin1",
                },
            )
        return httpx.Response(404, json={"message": f"no route {path}"})

    client = OvhClient(
        endpoint=DEFAULT_ENDPOINT, app_key="AK", app_secret="S3CRET", consumer_key="CK"
    )
    client._client = httpx.Client(
        transport=httpx.MockTransport(handler), timeout=client.timeout
    )
    servers = client.list_dedicated_servers()
    assert len(servers) == 1
    s = servers[0]
    assert s["server_id"] == "ns-us-1"
    assert s["cores"] == 16
    assert s["ram_gb"] == 128
    assert s["disk_gb"] == 16000
    assert s["model"] == "KS-6"
    assert s["cpu"] == "AMD Epyc 7351P"


def test_list_servers_specs_404_degrades():
    # Specs endpoint unavailable (older EU objects inline cores/memory/disk
    # anyway) must not drop the server from the list.
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/1.0/auth/time":
            return httpx.Response(200, json=str(int(time.time()) + 5))
        if path == "/1.0/dedicated/server":
            return httpx.Response(200, json=["ns-eu-1"])
        if path == "/1.0/dedicated/server/ns-eu-1/specifications/hardware":
            return httpx.Response(404, json={"message": "not found"})
        if path == "/1.0/dedicated/server/ns-eu-1":
            return httpx.Response(
                200,
                json={
                    "serverId": "ns-eu-1",
                    "displayName": "NS-EU-1",
                    "cores": 48,
                    "memory": 196608,
                    "disk": [{"size": 4096}],
                },
            )
        return httpx.Response(404, json={"message": f"no route {path}"})

    client = OvhClient(
        endpoint=DEFAULT_ENDPOINT, app_key="AK", app_secret="S3CRET", consumer_key="CK"
    )
    client._client = httpx.Client(
        transport=httpx.MockTransport(handler), timeout=client.timeout
    )
    servers = client.list_dedicated_servers()
    assert [s["hostname"] for s in servers] == ["NS-EU-1"]
    assert servers[0]["cores"] == 48
    assert servers[0]["ram_gb"] == 192
    assert servers[0]["disk_gb"] == 4096


def test_error_mapping():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={
                "errorCode": "NOT_GRANTED_CALL",
                "message": "Not granted call: you have no permission to do this.",
            },
        )

    client = OvhClient(
        endpoint=DEFAULT_ENDPOINT, app_key="AK", app_secret="S3CRET", consumer_key="CK"
    )
    client._client = httpx.Client(
        transport=httpx.MockTransport(handler), timeout=client.timeout
    )
    with pytest.raises(OvhError) as exc:
        client.list_dedicated_servers()
    assert exc.value.status_code == 403
    assert "NOT_GRANTED_CALL" in str(exc.value)


# ---------------------------------------------------------------------------
# Normalisation (defensive)
# ---------------------------------------------------------------------------


def test_normalize_server_full():
    raw: dict[str, Any] = {
        "serverId": "dedicace-9",
        "displayName": "NS-9",
        "cores": 48,
        "memory": 196608,  # MB
        "cpu": "AMD EPYC 7402P 24-Core Processor",
        "status": "active",
        "datacenter": "GRA11",
        "dedicatedServerName": "Dedicated Server Advance",
        "ip": [{"ip": "198.51.100.7"}],
        "disk": [{"size": 1024, "type": "ssd"}, {"size": 4096, "type": "hdd"}],
        "os": "linux",
    }
    n = normalize_server(raw)
    assert n["server_id"] == "dedicace-9"
    assert n["hostname"] == "NS-9"
    assert n["ip"] == "198.51.100.7"
    assert n["cores"] == 48
    assert n["ram_gb"] == 192
    assert n["disk_gb"] == 4096
    assert n["model"] == "Dedicated Server Advance"
    assert n["datacenter"] == "GRA11"
    assert n["status"] == "active"


def test_normalize_server_degenerate():
    n = normalize_server({})
    assert n["server_id"] == "unknown"
    assert n["hostname"] == "unknown"
    assert n["ip"] is None
    assert n["ram_gb"] is None
    assert n["disk_gb"] is None


def test_normalize_server_gb_memory_not_divided():
    # A value already in GB (small) must not be divided again.
    n = normalize_server({"serverId": "s1", "memory": 128})
    assert n["ram_gb"] == 128


def test_normalize_server_us_endpoint_shape():
    # US (api.us.ovhcloud.com) objects differ: integer serverId, ip as a plain
    # string, commercialRange "SKU | CPU", and no cores/memory/disk fields.
    raw: dict[str, Any] = {
        "serverId": 31345,
        "name": "ns2030113.ip-203-0-113-20.us",
        "ip": "203.0.113.20",
        "commercialRange": "KS-6 | AMD Epyc 7351P",
        "datacenter": "vin1",
        "region": "us-east-vin",
        "state": "ok",
        "powerState": "poweron",
        "os": "ubuntu2604-server_64",
        "linkSpeed": 10000,
        "iam": {"displayName": "ns2030113.ip-203-0-113-20.us"},
    }
    n = normalize_server(raw)
    assert n["server_id"] == "ns2030113.ip-203-0-113-20.us"
    assert n["hostname"] == "ns2030113.ip-203-0-113-20.us"
    assert n["ip"] == "203.0.113.20"
    assert n["ips"] == ["203.0.113.20"]
    assert n["model"] == "KS-6"
    assert n["cpu"] == "AMD Epyc 7351P"
    assert n["cores"] is None
    assert n["ram_gb"] is None
    assert n["disk_gb"] is None
    assert n["datacenter"] == "vin1"
    assert n["region"] == "us-east-vin"
    assert n["status"] == "ok"
    assert n["link_speed_mbps"] == 10000


def test_normalize_server_us_int_id_unique():
    # Distinct US serverIds must not collapse to a single row.
    a = normalize_server({"serverId": 1, "name": "ns-a.us", "ip": "1.2.3.4"})
    b = normalize_server({"serverId": 2, "name": "ns-b.us", "ip": "1.2.3.5"})
    assert a["server_id"] == "ns-a.us" and b["server_id"] == "ns-b.us"


# ---------------------------------------------------------------------------
# Role heuristic
# ---------------------------------------------------------------------------


def _mk(server_id: str, ram: int, cores: int, disk: int) -> dict[str, Any]:
    return normalize_server(
        {
            "serverId": server_id,
            "displayName": f"NS-{server_id}",
            "memory": ram * 1024,
            "cores": cores,
            "disk": [{"size": disk}],
        }
    )


def test_assign_roles_six_servers():
    servers = [
        _mk("a", 192, 48, 4096),
        _mk("b", 96, 24, 2048),
        _mk("c", 96, 24, 2048),
        _mk("d", 32, 8, 1024),
        _mk("e", 32, 8, 1024),
        _mk("f", 16, 4, 512),
    ]
    out = {s["server_id"]: s["roles"] for s in assign_roles(servers)}
    for ctrl in ("a", "b", "c"):
        assert {"k8s_control_plane", "etcd", "control"} <= set(out[ctrl])
    assert "etcd" in out["d"] and "etcd" in out["e"]
    assert out["f"] == ["compute"]
    # biggest disk (a) also gets storage
    assert "storage" in out["a"]
    # order is biggest-ram first
    assert [s["server_id"] for s in assign_roles(servers)][0] == "a"


def test_ovh_identity_matches_static_hosts_by_ip():
    inventory = [
        {
            "server_id": "server-1.example.com",
            "hostname": "server-1.example.com",
            "ip": "203.0.113.10",
            "ips": ["203.0.113.10"],
        }
    ]
    by_ip, by_host = inventory_indexes(inventory)
    assert by_ip["203.0.113.10"] == "server-1.example.com"
    servers = {
        "server-1.example.com": {
            "source": "static",
            "ip": "203.0.113.10",
            "roles": ["compute"],
        },
        "jump": {"source": "static", "ip": "10.0.0.1", "roles": ["control"]},
        "pxe-1": {"source": "baremetal", "ip": "203.0.113.10"},
    }
    tags = planned_ovh_tags(servers, inventory)
    assert tags == {
        "server-1.example.com": "server-1.example.com",
    }
    assert server_may_inherit_ovh(servers["jump"]) is True
    assert server_may_inherit_ovh(servers["pxe-1"]) is False
    assert (
        resolve_ovh_service_name(
            "server-1.example.com",
            servers["server-1.example.com"],
            by_ip,
            by_host,
        )
        == "server-1.example.com"
    )


def test_split_server_ips_prefers_private_for_cluster():
    split = split_server_ips(["203.0.113.10", "10.10.0.11", "203.0.113.10"])
    assert split["private_ip"] == "10.10.0.11"
    assert split["public_ip"] == "203.0.113.10"
    assert split["ip"] == "10.10.0.11"
    assert classify_ip("10.10.0.11") == "private"
    assert classify_ip("203.0.113.10") == "public"
    public_only = split_server_ips(["203.0.113.10"])
    assert public_only["ip"] == "203.0.113.10"
    assert public_only["private_ip"] is None
    dual = normalize_server(
        {"serverId": "ns1", "displayName": "ns1", "ip": "203.0.113.10"},
        extra_ips=["10.10.0.11"],
    )
    assert dual["ip"] == "10.10.0.11"
    assert dual["private_ip"] == "10.10.0.11"
    assert dual["public_ip"] == "203.0.113.10"


def test_pick_private_nic_prefers_vrack_vni_then_link_type():
    nics = [
        {"mac": "aa:aa:aa:aa:aa:aa", "link_type": "public", "vni": "pub-vni"},
        {"mac": "bb:bb:bb:bb:bb:bb", "link_type": "private", "vni": "priv-vni"},
    ]
    picked = pick_private_nic(nics, {"enabledVrackVnis": ["priv-vni"]})
    assert picked["mac"] == "bb:bb:bb:bb:bb:bb"
    by_link = pick_private_nic(nics, {})
    assert by_link["mac"] == "bb:bb:bb:bb:bb:bb"
    second = pick_private_nic(
        [
            {"mac": "aa:aa:aa:aa:aa:aa", "link_type": "public"},
            {"mac": "cc:cc:cc:cc:cc:cc", "link_type": "unknown"},
        ]
    )
    assert second["mac"] == "cc:cc:cc:cc:cc:cc"


def test_normalize_server_captures_private_nic():
    raw = {
        "serverId": "ns-rise-1",
        "displayName": "ns-rise-1",
        "ip": ["203.0.113.10", "10.10.0.11"],
        "enabledVrackVnis": ["vni-private"],
    }
    n = normalize_server(
        raw,
        nics=[
            {"mac": "00:11:22:33:44:55", "link_type": "public", "vni": "vni-public"},
            {"mac": "aa:bb:cc:dd:ee:ff", "link_type": "private", "vni": "vni-private"},
        ],
    )
    assert n["private_ip"] == "10.10.0.11"
    assert n["public_ip"] == "203.0.113.10"
    assert n["private_mac"] == "aa:bb:cc:dd:ee:ff"
    assert n["vrack_vni"] == "vni-private"


def test_env_is_ovh_uses_account_binding():
    class _E:
        ovh_account_id = None

    unbound = _E()
    assert env_is_ovh(unbound) is False
    unbound.ovh_account_id = "acc-1"
    assert env_is_ovh(unbound) is True


def test_consumer_key_rules_include_reinstall_and_ipmi():
    methods_paths = {(r["method"], r["path"]) for r in CONSUMER_KEY_RULES}
    assert ("POST", "/dedicated/server/*/reinstall") in methods_paths
    assert ("POST", "/dedicated/server/*/features/ipmi*") in methods_paths
    assert ("GET", "/dedicated/server/*") in methods_paths
    assert ("GET", "/vrack") in methods_paths
    assert ("GET", "/vrack/*") in methods_paths
    assert ("POST", "/vrack/*") in methods_paths
    assert ("DELETE", "/vrack/*") in methods_paths


def test_infer_ovh_image_type_and_customizations():
    assert (
        infer_ovh_image_type("https://factory.talos.dev/x/metal-amd64.qcow2") == "qcow2"
    )
    assert (
        infer_ovh_image_type("https://factory.talos.dev/x/metal-amd64.raw.xz") == "raw"
    )
    custom = byoi_customizations(
        image_url="https://factory.talos.dev/x/metal-amd64.qcow2",
        hostname="server-1.example.com",
        ssh_key="ssh-ed25519 AAAA test",
    )
    assert custom["imageURL"].endswith(".qcow2")
    assert custom["imageType"] == "qcow2"
    assert custom["efiBootloaderPath"] == DEFAULT_EFI_BOOTLOADER
    assert custom["hostname"] == "server-1.example.com"
    assert custom["sshKey"].startswith("ssh-ed25519")


def test_classify_install_status():
    assert classify_install_status(None) == "done"
    assert classify_install_status({"status": "done"}) == "done"
    assert classify_install_status({"status": "doing"}) == "doing"
    assert classify_install_status({"status": "error"}) == "error"
    assert (
        classify_install_status({"progress": [{"status": "done"}, {"status": "doing"}]})
        == "doing"
    )
    assert (
        classify_install_status({"progress": [{"status": "done"}, {"status": "done"}]})
        == "done"
    )


def test_assign_roles_three_servers():
    servers = [_mk("a", 128, 16, 512), _mk("b", 64, 8, 512), _mk("c", 64, 8, 512)]
    out = {s["server_id"]: s["roles"] for s in assign_roles(servers)}
    for roles in out.values():
        assert {"k8s_control_plane", "etcd", "control"} <= set(roles)
    # exactly 3 etcd members (odd quorum)
    assert sum(1 for r in out.values() if "etcd" in r) == 3


def test_assign_roles_two_servers_fallback():
    servers = [_mk("a", 128, 16, 512), _mk("b", 64, 8, 512)]
    out = {s["server_id"]: s["roles"] for s in assign_roles(servers)}
    for roles in out.values():
        assert {"k8s_control_plane", "etcd", "control"} <= set(roles)


def test_assign_roles_empty():
    assert assign_roles([]) == []


def test_assign_roles_does_not_mutate_input():
    servers = [_mk("a", 128, 16, 512), _mk("b", 64, 8, 512), _mk("c", 32, 4, 512)]
    before = [dict(s) for s in servers]
    assign_roles(servers)
    for orig, now in zip(before, servers):
        assert "roles" not in now
        assert orig == now


# ---------------------------------------------------------------------------
# Seed: config.yaml ovh: block -> default account (first boot only)
# ---------------------------------------------------------------------------


def test_seed_default_ovh_account(monkeypatch, tmp_path):
    from sqlalchemy import create_engine, func, select
    from sqlalchemy.orm import sessionmaker

    from app import db as dbmod
    from app.models import Base, OvhAccount

    eng = create_engine(f"sqlite:///{tmp_path}/seed.db")
    Base.metadata.create_all(eng)
    tmp_session = sessionmaker(bind=eng)
    monkeypatch.setattr(dbmod, "SessionLocal", tmp_session)
    monkeypatch.setattr(
        "app.config.get_settings",
        lambda: Settings(
            ovh_endpoint="https://eu.api.ovh.com/1.0",
            ovh_app_key="AK",
            ovh_app_secret="AS",
        ),
    )

    dbmod._seed_default_ovh_account()
    with tmp_session() as s:
        acc = s.scalar(select(OvhAccount))
        assert acc is not None
        assert acc.name == "default"
        assert acc.app_key == "AK"
        assert acc.app_secret_encrypted.startswith("fernet:")
        assert "AS" not in acc.app_secret_encrypted

    # Idempotent: a second run never duplicates.
    dbmod._seed_default_ovh_account()
    with tmp_session() as s:
        assert s.scalar(select(func.count()).select_from(OvhAccount)) == 1

    # Without config OVH credentials the seed is a no-op.
    (
        dbmod._seed_default_ovh_account.__wrapped__
        if hasattr(dbmod._seed_default_ovh_account, "__wrapped__")
        else None
    )
    monkeypatch.setattr("app.config.get_settings", lambda: Settings())
    eng2 = create_engine(f"sqlite:///{tmp_path}/seed2.db")
    Base.metadata.create_all(eng2)
    tmp_session2 = sessionmaker(bind=eng2)
    monkeypatch.setattr(dbmod, "SessionLocal", tmp_session2)
    dbmod._seed_default_ovh_account()
    with tmp_session2() as s:
        assert s.scalar(select(func.count()).select_from(OvhAccount)) == 0


# ---------------------------------------------------------------------------
# Migration: per-env consumer keys backfilled onto the account (one-time)
# ---------------------------------------------------------------------------


def test_backfill_moves_env_consumer_key_to_account(client, admin_headers):
    from sqlalchemy import text

    from app import db as dbmod
    from app.db import SessionLocal
    from app.models import Environment, OvhAccount
    from app.services.crypto import decrypt_secret, encrypt_secret

    acc = _create_ovh_account(client, admin_headers, f"bf-{int(time.time() * 1000)}")
    eid = _create_env(client, admin_headers, f"ovh-env-{int(time.time() * 1000)}")

    # Simulate the legacy per-env state: key stored on the env row.
    legacy_key = "LEGACYCK"
    with SessionLocal() as db:
        env = db.get(Environment, eid)
        env.ovh_consumer_key_encrypted = encrypt_secret(legacy_key)
        env.ovh_account_id = acc["id"]
        db.commit()

    # The session-scoped init_db already marked the backfill done; clear the
    # marker so the migration runs against this legacy state.
    with SessionLocal() as db:
        db.execute(
            text("DELETE FROM _gsc_migrations WHERE name='backfill_ovh_consumer_keys'")
        )
        db.commit()

    dbmod._backfill_ovh_consumer_keys()

    with SessionLocal() as db:
        env = db.get(Environment, eid)
        row = db.get(OvhAccount, acc["id"])
        assert env.ovh_consumer_key_encrypted is None
        assert decrypt_secret(row.consumer_key_encrypted) == legacy_key


# ---------------------------------------------------------------------------
# API endpoints (mocked OVH transport + admin-managed accounts)
# ---------------------------------------------------------------------------


def _mock_transport(
    servers: dict[str, dict[str, Any]] | None = None,
    ids: list[str] | None = None,
    credential_state: dict[str, str] | None = None,
    os_availability: dict[str, list[str]] | None = None,
    compatible: dict[str, dict[str, Any]] | None = None,
    install_status: dict[str, dict[str, Any]] | None = None,
    reinstall_tasks: dict[str, Any] | None = None,
    reinstall_fail: dict[str, str] | None = None,
    reinstall_requests: list[dict[str, Any]] | None = None,
    nics: dict[str, list[dict[str, Any]]] | None = None,
    vracks: list[str] | None = None,
    vrack_info: dict[str, dict[str, Any]] | None = None,
    vrack_servers: dict[str, list[str]] | None = None,
    vrack_interfaces: dict[str, list[str]] | None = None,
    vrack_interface_details: dict[str, list[dict[str, Any]]] | None = None,
    vrack_eligible: dict[str, dict[str, Any]] | None = None,
    vrack_ips: dict[str, list[dict[str, Any]]] | None = None,
    attach_requests: list[dict[str, Any]] | None = None,
) -> httpx.MockTransport:
    """Build a MockTransport emulating the OVH endpoints used by the client.

    ``servers`` maps server id -> raw OVH object; ``ids`` is the id list
    returned by GET /dedicated/server (defaults to the keys of ``servers``).
    ``credential_state`` maps consumer key -> validationStatus.
    BYOI knobs: ``os_availability`` maps hardware ref -> template name list,
    ``compatible`` / ``install_status`` map server id -> raw mapping,
    ``reinstall_tasks`` maps server id -> task id to return (default
    ``"task-<id>"``), ``reinstall_fail`` maps server id -> error message
    (answered as a 403 OVH fault).
    vRack knobs: ``nics`` maps server id -> NIC objects (mac/linkType/vni);
    ``vracks`` is GET /vrack; membership lists are mutated by POST attach.
    """
    servers = servers or {}
    ids = ids if ids is not None else list(servers.keys())
    credential_state = credential_state or {}
    os_availability = os_availability or {}
    compatible = compatible or {}
    install_status = install_status or {}
    reinstall_tasks = reinstall_tasks or {}
    reinstall_fail = reinstall_fail or {}
    nics = nics or {}
    vrack_names = list(vracks or [])
    vrack_info = vrack_info or {}
    attached_servers = {k: list(v) for k, v in (vrack_servers or {}).items()}
    attached_ifaces = {k: list(v) for k, v in (vrack_interfaces or {}).items()}
    vrack_interface_details = {
        k: [dict(x) for x in v] for k, v in (vrack_interface_details or {}).items()
    }
    vrack_eligible = vrack_eligible or {}
    vrack_ips = vrack_ips or {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/1.0/auth/time":
            return httpx.Response(200, json=int(time.time()))
        if request.url.path == "/1.0/auth/credential" and request.method == "POST":
            assert "X-Ovh-Application" in request.headers
            assert "X-Ovh-Signature" not in request.headers
            return httpx.Response(
                200,
                json={
                    "state": "pendingValidation",
                    "consumerKey": "NEWCK",
                    "validationUrl": "https://eu.api.ovh.com/auth/?credentialToken=abc",
                },
            )
        if request.url.path.startswith("/1.0/auth/credential/"):
            key = request.url.path.rsplit("/", 1)[-1]
            return httpx.Response(
                200, json={"validationStatus": credential_state.get(key, "unknown")}
            )
        if request.url.path == "/1.0/dedicated/server" and request.method == "GET":
            return httpx.Response(200, json=ids)
        if request.url.path == "/1.0/dedicated/server/osAvailabilities":
            hardware = request.url.params.get("hardware", "")
            return httpx.Response(200, json=os_availability.get(hardware, []))
        if request.url.path.startswith("/1.0/dedicated/server/"):
            rest = request.url.path.split("/1.0/dedicated/server/", 1)[1]
            parts = rest.split("/")
            sid = parts[0]
            if rest.endswith("/reinstall") and request.method == "POST":
                sid = rest.rsplit("/", 1)[0]
                if reinstall_requests is not None:
                    try:
                        reinstall_requests.append(
                            {
                                "server": sid,
                                "body": json.loads(request.content.decode() or "{}"),
                            }
                        )
                    except ValueError:
                        reinstall_requests.append({"server": sid, "body": {}})
                if sid in reinstall_fail:
                    return httpx.Response(
                        403,
                        json={
                            "message": reinstall_fail[sid],
                            "errorCode": "NOT_GRANTED_CALL",
                        },
                    )
                return httpx.Response(200, json=reinstall_tasks.get(sid, f"task-{sid}"))
            if rest.endswith("/install/compatibleTemplates"):
                sid = rest.split("/install/")[0]
                return httpx.Response(200, json=compatible.get(sid, {}))
            if rest.endswith("/install/status"):
                sid = rest.split("/install/")[0]
                return httpx.Response(
                    200, json=install_status.get(sid, {"status": "done"})
                )
            if len(parts) >= 2 and parts[1] == "networkInterfaceController":
                nic_list = nics.get(sid) or []
                if len(parts) == 2:
                    return httpx.Response(
                        200,
                        json=[
                            n.get("mac") if isinstance(n, dict) else n for n in nic_list
                        ],
                    )
                mac = unquote(parts[2])
                for nic in nic_list:
                    if not isinstance(nic, dict):
                        continue
                    if str(nic.get("mac") or "").lower() == mac.lower():
                        return httpx.Response(
                            200,
                            json={
                                "mac": nic.get("mac"),
                                "linkType": nic.get("linkType") or nic.get("link_type"),
                                "virtualNetworkInterface": nic.get("vni")
                                or nic.get("virtualNetworkInterface"),
                            },
                        )
                return httpx.Response(404, json={"message": f"nic {mac} not found"})
            if len(parts) == 2 and parts[1] == "ips":
                extra = servers.get(sid, {}).get("_ips")
                if isinstance(extra, list):
                    return httpx.Response(200, json=extra)
                return httpx.Response(200, json=[])
            if "/" not in rest and sid not in servers:
                # Faithful to OVH: an unknown service name is a 404.
                return httpx.Response(
                    404,
                    json={
                        "message": f"server {sid} not found",
                        "errorCode": "NOT_FOUND",
                    },
                )
            if "/" not in rest:
                return httpx.Response(200, json=servers.get(sid, {}))
            return httpx.Response(200, json=servers.get(sid, {}))
        if request.url.path == "/1.0/vrack" and request.method == "GET":
            return httpx.Response(200, json=vrack_names)
        if request.url.path.startswith("/1.0/vrack/"):
            rest = request.url.path.split("/1.0/vrack/", 1)[1]
            parts = rest.split("/")
            vrack = unquote(parts[0])
            sub = "/".join(parts[1:])
            if not sub:
                info = vrack_info.get(vrack) or {"name": vrack, "description": ""}
                if not info and vrack not in vrack_names:
                    return httpx.Response(
                        404, json={"message": f"vrack {vrack} not found"}
                    )
                return httpx.Response(200, json=info)
            if sub == "eligibleServices":
                return httpx.Response(200, json=vrack_eligible.get(vrack, {}))
            if sub == "dedicatedServer" and request.method == "GET":
                return httpx.Response(200, json=attached_servers.get(vrack, []))
            if sub == "dedicatedServer" and request.method == "POST":
                body = json.loads(request.content.decode() or "{}")
                name = str(body.get("dedicatedServer") or "")
                attached_servers.setdefault(vrack, []).append(name)
                if attach_requests is not None:
                    attach_requests.append(
                        {"vrack": vrack, "kind": "server", "id": name}
                    )
                return httpx.Response(200, json=f"task-srv-{name}")
            if sub == "dedicatedServerInterface" and request.method == "GET":
                return httpx.Response(200, json=attached_ifaces.get(vrack, []))
            if sub == "dedicatedServerInterface" and request.method == "POST":
                body = json.loads(request.content.decode() or "{}")
                iface = str(body.get("dedicatedServerInterface") or "")
                attached_ifaces.setdefault(vrack, []).append(iface)
                if attach_requests is not None:
                    attach_requests.append(
                        {"vrack": vrack, "kind": "interface", "id": iface}
                    )
                return httpx.Response(200, json=f"task-iface-{iface}")
            if sub == "dedicatedServerInterfaceDetails":
                return httpx.Response(200, json=vrack_interface_details.get(vrack, []))
            if sub == "ip":
                blocks = vrack_ips.get(vrack) or []
                return httpx.Response(
                    200,
                    json=[b.get("ip") if isinstance(b, dict) else b for b in blocks],
                )
            if sub.startswith("ip/"):
                want = unquote(sub.split("/", 1)[1])
                for block in vrack_ips.get(vrack) or []:
                    ip = block.get("ip") if isinstance(block, dict) else block
                    if str(ip) == want:
                        return httpx.Response(
                            200,
                            json=block if isinstance(block, dict) else {"ip": block},
                        )
                return httpx.Response(404, json={"message": f"ip {want} not found"})
            return httpx.Response(404, json={"message": f"no vrack route {rest}"})
        return httpx.Response(404, json={"message": f"no route {request.url.path}"})

    return httpx.MockTransport(handler)


def _patch_client_factory(monkeypatch, transport: httpx.MockTransport) -> None:
    """Attach the mock transport to every OvhClient (replaces the network one)."""
    original_post_init = OvhClient.__post_init__

    def patched_post_init(self) -> None:
        original_post_init(self)
        if self._client is not None:
            self._client = httpx.Client(transport=transport, timeout=self.timeout)

    monkeypatch.setattr(OvhClient, "__post_init__", patched_post_init)


def _create_env(client, admin_headers, name: str) -> str:
    resp = client.post(
        "/api/v1/environments",
        headers=admin_headers,
        json={"name": name, "tier": "dev"},
    )
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["id"]


def _create_ovh_account(client, admin_headers, name: str, app_key: str = "AK"):
    resp = client.post(
        "/api/v1/ovh/accounts",
        headers=admin_headers,
        json={
            "name": name,
            "endpoint": DEFAULT_ENDPOINT,
            "app_key": app_key,
            "app_secret": "S3CRET",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _store_account_key(
    client, admin_headers, account_id: str, consumer_key: str = "CKSTORED"
) -> None:
    """Store an encrypted consumer key directly on the account (test helper)."""
    from app.db import SessionLocal
    from app.models import OvhAccount
    from app.services.crypto import encrypt_secret

    with SessionLocal() as db:
        acc = db.get(OvhAccount, account_id)
        acc.consumer_key_encrypted = encrypt_secret(consumer_key)
        db.commit()


def _bind_env_to_account(client, admin_headers, env_id: str, account_id: str) -> None:
    """Bind an environment to an OVH account via the operator endpoint."""
    resp = client.post(
        f"/api/v1/environments/{env_id}/ovh/bind",
        headers=admin_headers,
        json={"account_id": account_id},
    )
    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# Account CRUD (platform admin)
# ---------------------------------------------------------------------------


def test_ovh_account_crud(client, admin_headers):
    acc = _create_ovh_account(client, admin_headers, f"acc-{int(time.time() * 1000)}")
    assert acc["endpoint"] == DEFAULT_ENDPOINT
    assert "app_secret" not in acc

    # List: app_key visible (for editing), app_secret never present.
    resp = client.get("/api/v1/ovh/accounts", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    listed = [a for a in resp.json()["accounts"] if a["id"] == acc["id"]]
    assert len(listed) == 1
    assert listed[0]["app_key"] == "AK"
    assert "app_secret" not in listed[0]

    # Update: rename + replace secret.
    new_name = f"acc-upd-{int(time.time() * 1000)}"
    resp = client.put(
        f"/api/v1/ovh/accounts/{acc['id']}",
        headers=admin_headers,
        json={"name": new_name, "app_secret": "NEWSECRET"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == new_name

    # The new secret is what signs now (decrypt path works): test ping.
    resp = client.post(
        f"/api/v1/ovh/accounts/{acc['id']}/test", headers=admin_headers, json={}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True

    # Delete.
    resp = client.delete(f"/api/v1/ovh/accounts/{acc['id']}", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    resp = client.get("/api/v1/ovh/accounts", headers=admin_headers)
    assert all(a["id"] != acc["id"] for a in resp.json()["accounts"])


def test_ovh_account_test_ping_uses_mock(client, admin_headers, monkeypatch):
    _patch_client_factory(monkeypatch, _mock_transport())
    acc = _create_ovh_account(client, admin_headers, f"ping-{int(time.time() * 1000)}")
    resp = client.post(
        f"/api/v1/ovh/accounts/{acc['id']}/test", headers=admin_headers, json={}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["latency_ms"] >= 0


def test_ovh_account_duplicate_name(client, admin_headers):
    name = f"dup-{int(time.time() * 1000)}"
    _create_ovh_account(client, admin_headers, name)
    resp = client.post(
        "/api/v1/ovh/accounts",
        headers=admin_headers,
        json={
            "name": name,
            "endpoint": DEFAULT_ENDPOINT,
            "app_key": "AK2",
            "app_secret": "S2",
        },
    )
    assert resp.status_code == 409, resp.text


def test_ovh_account_requires_admin(client, operator_headers):
    resp = client.get("/api/v1/ovh/accounts", headers=operator_headers)
    assert resp.status_code == 403, resp.text
    resp = client.post(
        "/api/v1/ovh/accounts",
        headers=operator_headers,
        json={
            "name": "nope",
            "endpoint": DEFAULT_ENDPOINT,
            "app_key": "AK",
            "app_secret": "S",
        },
    )
    assert resp.status_code == 403, resp.text


# ---------------------------------------------------------------------------
# Endpoint validation (OVH-documented regions + canonicalisation)
# ---------------------------------------------------------------------------


def _endpoint_of(client, admin_headers, **fields):
    """Create an account with custom endpoint fields; returns the response."""
    payload = {
        "name": f"ep-{uuid.uuid4().hex[:8]}",
        "endpoint": DEFAULT_ENDPOINT,
        "app_key": "AK",
        "app_secret": "AS",
    }
    payload.update(fields)
    return client.post("/api/v1/ovh/accounts", headers=admin_headers, json=payload)


def test_ovh_endpoint_region_codes_and_urls(client, admin_headers):
    from app.routers.ovh import _validate_endpoint

    # Region codes (the UI dropdown values, both cases) -> canonical URLs.
    assert _validate_endpoint("eu") == "https://eu.api.ovh.com/1.0"
    assert _validate_endpoint("US") == "https://api.us.ovhcloud.com/1.0"
    assert _validate_endpoint("ca") == "https://ca.api.ovh.com/1.0"
    # URLs -> canonical form (trailing slash, missing /1.0, http scheme).
    assert (
        _validate_endpoint("https://eu.api.ovh.com/1.0/")
        == "https://eu.api.ovh.com/1.0"
    )
    assert _validate_endpoint("eu.api.ovh.com") == "https://eu.api.ovh.com/1.0"
    assert (
        _validate_endpoint("http://api.us.ovhcloud.com/1.0")
        == "https://api.us.ovhcloud.com/1.0"
    )
    # Custom endpoints are allowed (canonicalised) for API users.
    assert (
        _validate_endpoint("https://ovh.example.com") == "https://ovh.example.com/1.0"
    )

    resp = _endpoint_of(client, admin_headers, endpoint="us")
    assert resp.status_code == 201, resp.text
    assert resp.json()["endpoint"] == "https://api.us.ovhcloud.com/1.0"

    resp = _endpoint_of(client, admin_headers, endpoint="https://ca.api.ovh.com/")
    assert resp.status_code == 201, resp.text
    assert resp.json()["endpoint"] == "https://ca.api.ovh.com/1.0"


def test_ovh_endpoint_unknown_region_rejected(client, admin_headers):
    resp = _endpoint_of(client, admin_headers, endpoint="au")
    assert resp.status_code == 422, resp.text
    assert "unknown OVH region" in resp.text


def test_ovh_endpoint_update_validates_too(client, admin_headers):
    acc = _create_ovh_account(client, admin_headers, f"epup-{int(time.time() * 1000)}")
    resp = client.put(
        f"/api/v1/ovh/accounts/{acc['id']}",
        headers=admin_headers,
        json={"endpoint": "CA"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["endpoint"] == "https://ca.api.ovh.com/1.0"

    resp = client.put(
        f"/api/v1/ovh/accounts/{acc['id']}",
        headers=admin_headers,
        json={"endpoint": "mars"},
    )
    assert resp.status_code == 422, resp.text


def test_ovh_account_delete_in_use(client, admin_headers):
    acc = _create_ovh_account(client, admin_headers, f"inuse-{int(time.time() * 1000)}")
    eid = _create_env(client, admin_headers, f"ovh-env-{int(time.time() * 1000)}")
    _bind_env_to_account(client, admin_headers, eid, acc["id"])
    resp = client.delete(f"/api/v1/ovh/accounts/{acc['id']}", headers=admin_headers)
    assert resp.status_code == 409, resp.text


def test_ovh_env_scoped_accounts_list(client, admin_headers, operator_headers):
    acc = _create_ovh_account(client, admin_headers, f"drop-{int(time.time() * 1000)}")
    eid = _create_env(client, admin_headers, f"ovh-env-{int(time.time() * 1000)}")
    # Env-scoped list is what the wizard dropdown loads: viewer-level, and
    # never exposes app_key/app_secret.
    resp = client.get(
        f"/api/v1/environments/{eid}/ovh/accounts", headers=operator_headers
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["count"] >= 1
    for a in body["accounts"]:
        assert "app_key" not in a
        assert "app_secret" not in a
    assert any(a["id"] == acc["id"] for a in body["accounts"])
    assert body["bound_account_id"] is None


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


def test_ovh_status_reports_accounts(client, admin_headers):
    acc = _create_ovh_account(client, admin_headers, f"stat-{int(time.time() * 1000)}")
    eid = _create_env(client, admin_headers, f"ovh-env-{int(time.time() * 1000)}")
    resp = client.get(f"/api/v1/ovh?environment_id={eid}", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ovh_configured"] is True
    assert body["account_count"] >= 1
    assert body["has_consumer_key"] is False
    assert body["account_id"] is None

    # Binding alone does NOT grant a key — the account must have one.
    _bind_env_to_account(client, admin_headers, eid, acc["id"])
    resp = client.get(f"/api/v1/ovh?environment_id={eid}", headers=admin_headers)
    body = resp.json()
    assert body["has_consumer_key"] is False
    assert body["account_id"] == acc["id"]

    _store_account_key(client, admin_headers, acc["id"])
    resp = client.get(f"/api/v1/ovh?environment_id={eid}", headers=admin_headers)
    body = resp.json()
    assert body["has_consumer_key"] is True
    assert body["account_id"] == acc["id"]
    assert body["account_with_key_count"] >= 1


def test_ovh_status_requires_admin_for_platform(client, operator_headers):
    resp = client.get("/api/v1/ovh", headers=operator_headers)
    assert resp.status_code == 403, resp.text


# ---------------------------------------------------------------------------
# Bind + servers (env-scoped) and consumer-key lifecycle (account-level)
# ---------------------------------------------------------------------------


def test_ovh_servers_unbound_env_404(client, admin_headers):
    eid = _create_env(client, admin_headers, f"ovh-env-{int(time.time() * 1000)}")
    resp = client.get(f"/api/v1/environments/{eid}/ovh/servers", headers=admin_headers)
    assert resp.status_code == 404, resp.text
    assert "not bound" in resp.json()["detail"].lower()


def test_ovh_servers_bound_without_key_503(client, admin_headers):
    acc = _create_ovh_account(client, admin_headers, f"nokey-{int(time.time() * 1000)}")
    eid = _create_env(client, admin_headers, f"ovh-env-{int(time.time() * 1000)}")
    _bind_env_to_account(client, admin_headers, eid, acc["id"])
    resp = client.get(f"/api/v1/environments/{eid}/ovh/servers", headers=admin_headers)
    assert resp.status_code == 503, resp.text
    assert "consumer key" in resp.json()["detail"].lower()


def test_ovh_bind_unbind_flow(client, admin_headers, operator_headers):
    acc = _create_ovh_account(client, admin_headers, f"bind-{int(time.time() * 1000)}")
    eid = _create_env(client, admin_headers, f"ovh-env-{int(time.time() * 1000)}")

    resp = client.post(
        f"/api/v1/environments/{eid}/ovh/bind",
        headers=operator_headers,
        json={"account_id": acc["id"]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["account_id"] == acc["id"]
    assert resp.json()["has_consumer_key"] is False

    resp = client.get(
        f"/api/v1/environments/{eid}/ovh/accounts", headers=operator_headers
    )
    assert resp.json()["bound_account_id"] == acc["id"]

    # Unknown account -> 404.
    resp = client.post(
        f"/api/v1/environments/{eid}/ovh/bind",
        headers=operator_headers,
        json={"account_id": "does-not-exist"},
    )
    assert resp.status_code == 404, resp.text

    resp = client.delete(
        f"/api/v1/environments/{eid}/ovh/bind", headers=operator_headers
    )
    assert resp.status_code == 200, resp.text
    resp = client.get(
        f"/api/v1/environments/{eid}/ovh/accounts", headers=operator_headers
    )
    assert resp.json()["bound_account_id"] is None


def test_ovh_servers_lists_and_suggests_roles(client, admin_headers, monkeypatch):
    servers = {
        "s1": {
            "serverId": "s1",
            "displayName": "NS-big",
            "cores": 48,
            "memory": 196608,
            "disk": [{"size": 4096}],
            "ip": [{"ip": "198.51.100.1"}],
        },
        "s2": {
            "serverId": "s2",
            "displayName": "NS-mid",
            "cores": 24,
            "memory": 98304,
            "disk": [{"size": 2048}],
            "ip": [{"ip": "198.51.100.2"}],
        },
        "s3": {
            "serverId": "s3",
            "displayName": "NS-small",
            "cores": 8,
            "memory": 32768,
            "disk": [{"size": 1024}],
            "ip": [{"ip": "198.51.100.3"}],
        },
    }
    _patch_client_factory(
        monkeypatch, _mock_transport(servers=servers, ids=["s1", "s2", "s3"])
    )
    acc = _create_ovh_account(client, admin_headers, f"srv-{int(time.time() * 1000)}")
    eid = _create_env(client, admin_headers, f"ovh-env-{int(time.time() * 1000)}")
    _bind_env_to_account(client, admin_headers, eid, acc["id"])
    _store_account_key(client, admin_headers, acc["id"])

    resp = client.get(f"/api/v1/environments/{eid}/ovh/servers", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["count"] == 3
    by_host = {s["hostname"]: s for s in body["servers"]}
    # Exactly 3 servers: every node carries the full required set.
    for host in ("NS-big", "NS-mid", "NS-small"):
        assert {"k8s_control_plane", "etcd", "control"} <= set(by_host[host]["roles"])
    assert "storage" in by_host["NS-big"]["roles"]
    assert by_host["NS-small"]["roles"] == [
        "k8s_control_plane",
        "etcd",
        "control",
        "compute",
    ]
    # raw payload never leaves the API
    assert all("raw" not in s for s in body["servers"])


def test_ovh_cross_tenant_access_control(client, admin_headers, monkeypatch):
    """Session users from another tenant get 403; own-tenant operator gets 200."""
    from tests.test_tenants import _create_tenant, _create_user, _login_headers

    servers = {
        "s1": {"serverId": "s1", "displayName": "NS-only", "memory": 32768, "cores": 8}
    }
    _patch_client_factory(monkeypatch, _mock_transport(servers=servers, ids=["s1"]))
    acc = _create_ovh_account(client, admin_headers, f"ct-{int(time.time() * 1000)}")

    tenant_a = _create_tenant(client, admin_headers)
    tenant_b = _create_tenant(client, admin_headers)

    # Env in tenant B bound to an account that has an approved key.
    resp = client.post(
        "/api/v1/environments",
        headers=admin_headers,
        json={
            "name": f"ovh-env-{int(time.time() * 1000)}",
            "tenant_id": tenant_b["id"],
        },
    )
    eid = resp.json()["id"]
    _bind_env_to_account(client, admin_headers, eid, acc["id"])
    _store_account_key(client, admin_headers, acc["id"])

    outsider = _create_user(
        client,
        admin_headers,
        memberships=[{"tenant_id": tenant_a["id"], "role": "viewer"}],
    )
    outsider_headers = _login_headers(client, outsider["username"])

    resp = client.get(
        f"/api/v1/environments/{eid}/ovh/servers", headers=outsider_headers
    )
    assert resp.status_code == 403, resp.text
    # The dropdown list is tenant-gated too.
    resp = client.get(
        f"/api/v1/environments/{eid}/ovh/accounts", headers=outsider_headers
    )
    assert resp.status_code == 403, resp.text

    own_operator = _create_user(
        client,
        admin_headers,
        memberships=[{"tenant_id": tenant_b["id"], "role": "operator"}],
    )
    op_headers = _login_headers(client, own_operator["username"])
    resp = client.get(f"/api/v1/environments/{eid}/ovh/servers", headers=op_headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["count"] == 1
    resp = client.get(f"/api/v1/environments/{eid}/ovh/accounts", headers=op_headers)
    assert resp.status_code == 200, resp.text


def test_ovh_account_consumer_key_flow(client, admin_headers, monkeypatch):
    """Account-level: request -> validate (pending->ok) -> store -> status.

    The key lives on the account, so every environment bound to it can list
    servers once it is stored.
    """
    acc = _create_ovh_account(client, admin_headers, f"flow-{int(time.time() * 1000)}")
    eid = _create_env(client, admin_headers, f"ovh-env-{int(time.time() * 1000)}")

    states: dict[str, str] = {"NEWCK": "pendingValidation"}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/1.0/auth/time":
            return httpx.Response(200, json=int(time.time()))
        if request.url.path == "/1.0/auth/credential" and request.method == "POST":
            body = json.loads(request.content)
            assert body["accessRules"] == CONSUMER_KEY_RULES
            return httpx.Response(
                200,
                json={
                    "state": "pendingValidation",
                    "consumerKey": "NEWCK",
                    "validationUrl": "https://eu.api.ovh.com/auth/?credentialToken=tok",
                },
            )
        if request.url.path.startswith("/1.0/auth/credential/"):
            key = request.url.path.rsplit("/", 1)[-1]
            return httpx.Response(
                200, json={"validationStatus": states.get(key, "unknown")}
            )
        return httpx.Response(404, json={"message": "no route"})

    _patch_client_factory(monkeypatch, httpx.MockTransport(handler))

    # request
    resp = client.post(
        f"/api/v1/ovh/accounts/{acc['id']}/consumer-key/request",
        headers=admin_headers,
        json={},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["consumer_key"] == "NEWCK"
    assert body["validation_url"].startswith("https://eu.api.ovh.com/auth/")
    assert body["account_id"] == acc["id"]

    # validate while pending
    resp = client.get(
        f"/api/v1/ovh/accounts/{acc['id']}/consumer-key/validate"
        f"?consumer_key=NEWCK",
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["valid"] is False

    # store while pending -> 409
    resp = client.post(
        f"/api/v1/ovh/accounts/{acc['id']}/consumer-key/store",
        headers=admin_headers,
        json={"consumer_key": "NEWCK"},
    )
    assert resp.status_code == 409, resp.text

    # admin approves in OVH
    states["NEWCK"] = "ok"
    resp = client.post(
        f"/api/v1/ovh/accounts/{acc['id']}/consumer-key/store",
        headers=admin_headers,
        json={"consumer_key": "NEWCK"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["has_consumer_key"] is True
    assert resp.json()["account_id"] == acc["id"]

    # the account list now reports the key
    resp = client.get("/api/v1/ovh/accounts", headers=admin_headers)
    listed = [a for a in resp.json()["accounts"] if a["id"] == acc["id"]]
    assert listed[0]["has_consumer_key"] is True

    # key is stored encrypted on the account row, never returned in plain
    from app.db import SessionLocal
    from app.models import OvhAccount

    with SessionLocal() as db:
        row = db.get(OvhAccount, acc["id"])
        stored = row.consumer_key_encrypted or ""
        assert stored.startswith("fernet:")
        assert "NEWCK" not in stored

    # a bound env can list servers; status reflects the account key
    _bind_env_to_account(client, admin_headers, eid, acc["id"])
    _patch_client_factory(
        monkeypatch,
        _mock_transport(
            servers={
                "s1": {
                    "serverId": "s1",
                    "displayName": "NS-1",
                    "memory": 32768,
                    "cores": 8,
                }
            },
            ids=["s1"],
        ),
    )
    resp = client.get(f"/api/v1/environments/{eid}/ovh/servers", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["count"] == 1
    assert resp.json()["account_id"] == acc["id"]

    resp = client.get(f"/api/v1/ovh?environment_id={eid}", headers=admin_headers)
    body = resp.json()
    assert body["has_consumer_key"] is True
    assert body["account_id"] == acc["id"]

    # delete
    resp = client.delete(
        f"/api/v1/ovh/accounts/{acc['id']}/consumer-key", headers=admin_headers
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["has_consumer_key"] is False
    resp = client.get(f"/api/v1/ovh?environment_id={eid}", headers=admin_headers)
    body = resp.json()
    assert body["has_consumer_key"] is False
    assert body["account_id"] == acc["id"]


def test_ovh_consumer_key_endpoints_require_admin(client, operator_headers):
    resp = client.post(
        "/api/v1/ovh/accounts/whatever/consumer-key/request",
        headers=operator_headers,
        json={},
    )
    assert resp.status_code == 403, resp.text
    resp = client.delete(
        "/api/v1/ovh/accounts/whatever/consumer-key", headers=operator_headers
    )
    assert resp.status_code == 403, resp.text


def test_ovh_credential_change_invalidates_consumer_key(
    client, admin_headers, monkeypatch
):
    """Changing endpoint/app_key/app_secret drops the stored consumer key."""
    _patch_client_factory(monkeypatch, _mock_transport())
    acc = _create_ovh_account(client, admin_headers, f"inv-{int(time.time() * 1000)}")
    _store_account_key(client, admin_headers, acc["id"])

    # Name-only update keeps the key.
    resp = client.put(
        f"/api/v1/ovh/accounts/{acc['id']}",
        headers=admin_headers,
        json={"name": f"inv2-{int(time.time() * 1000)}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["has_consumer_key"] is True

    # Endpoint change resets it.
    resp = client.put(
        f"/api/v1/ovh/accounts/{acc['id']}",
        headers=admin_headers,
        json={"endpoint": "ca"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["endpoint"] == "https://ca.api.ovh.com/1.0"
    assert resp.json()["has_consumer_key"] is False

    # App key change resets it too.
    _store_account_key(client, admin_headers, acc["id"])
    resp = client.put(
        f"/api/v1/ovh/accounts/{acc['id']}",
        headers=admin_headers,
        json={"app_key": "OTHERKEY"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["has_consumer_key"] is False

    # An explicit consumer_key in the update wins (no reset).
    resp = client.put(
        f"/api/v1/ovh/accounts/{acc['id']}",
        headers=admin_headers,
        json={"app_key": "OTHERKEY", "consumer_key": "FRESH"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["has_consumer_key"] is True


# ---------------------------------------------------------------------------
# Account preview (admin) + overview (viewer)
# ---------------------------------------------------------------------------


def test_ovh_preview_requires_consumer_key_when_unbound(client, admin_headers):
    acc = _create_ovh_account(client, admin_headers, f"pv0-{int(time.time() * 1000)}")
    resp = client.get(
        f"/api/v1/ovh/accounts/{acc['id']}/servers", headers=admin_headers
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["consumer_key_required"] is True
    assert body["servers"] == []
    assert body["bound_environment_ids"] == []


def test_ovh_preview_uses_account_consumer_key(client, admin_headers, monkeypatch):
    servers = {
        "s1": {
            "serverId": "s1",
            "displayName": "NS-prev",
            "cores": 16,
            "memory": 65536,
            "disk": [{"size": 2048}],
            "ip": [{"ip": "198.51.100.9"}],
        },
    }
    _patch_client_factory(monkeypatch, _mock_transport(servers=servers, ids=["s1"]))
    acc = _create_ovh_account(client, admin_headers, f"pv1-{int(time.time() * 1000)}")
    eid = _create_env(client, admin_headers, f"ovh-env-{int(time.time() * 1000)}")
    _bind_env_to_account(client, admin_headers, eid, acc["id"])
    _store_account_key(client, admin_headers, acc["id"])
    resp = client.get(
        f"/api/v1/ovh/accounts/{acc['id']}/servers", headers=admin_headers
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["consumer_key_required"] is False
    assert len(body["servers"]) == 1
    assert body["servers"][0]["hostname"] == "NS-prev"
    assert "raw" not in body["servers"][0]
    # bound envs are reported
    assert eid in body["bound_environment_ids"]


def test_ovh_preview_requires_admin(client, operator_headers):
    resp = client.get("/api/v1/ovh/accounts/nope/servers", headers=operator_headers)
    assert resp.status_code == 403, resp.text


def test_ovh_preview_unknown_account(client, admin_headers):
    resp = client.get(
        "/api/v1/ovh/accounts/does-not-exist/servers", headers=admin_headers
    )
    assert resp.status_code == 404, resp.text


def test_ovh_accounts_overview_lists_accounts_and_bound_envs(client, admin_headers):
    from tests.test_tenants import _create_tenant, _create_user, _login_headers

    ts = int(time.time() * 1000)
    acc = _create_ovh_account(client, admin_headers, f"ov-{ts}", app_key="OVKEY")

    # Platform-admin view: platform-level (tenantless) bound envs visible.
    eid = _create_env(client, admin_headers, f"ovh-env-{ts}")
    _bind_env_to_account(client, admin_headers, eid, acc["id"])
    resp = client.get("/api/v1/ovh/accounts/overview", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    mine = [a for a in resp.json()["accounts"] if a["id"] == acc["id"]]
    assert len(mine) == 1
    # app key visible (not secret)
    assert mine[0]["app_key"] == "OVKEY"
    assert "app_secret" not in mine[0]
    assert eid in mine[0]["bound_environment_ids"]
    assert mine[0]["endpoint"] == DEFAULT_ENDPOINT

    # Member view: only the member's own tenant envs appear as bound.
    tenant = _create_tenant(client, admin_headers)
    resp = client.post(
        "/api/v1/environments",
        headers=admin_headers,
        json={"name": f"ovh-env-{ts}-t", "tenant_id": tenant["id"]},
    )
    etid = resp.json()["id"]
    _bind_env_to_account(client, admin_headers, etid, acc["id"])

    member = _create_user(
        client,
        admin_headers,
        memberships=[{"tenant_id": tenant["id"], "role": "viewer"}],
    )
    member_headers = _login_headers(client, member["username"])
    resp = client.get("/api/v1/ovh/accounts/overview", headers=member_headers)
    assert resp.status_code == 200, resp.text
    mine = [a for a in resp.json()["accounts"] if a["id"] == acc["id"]]
    assert mine[0]["bound_environment_ids"] == [etid]


# ---------------------------------------------------------------------------
# BYOI (bring-your-own-image): reinstall + templates
# ---------------------------------------------------------------------------


def _ovh_byoi_servers() -> dict[str, dict[str, Any]]:
    """Two EU-shaped dedicated servers, one with a BYOI ref, one with a SKU."""
    return {
        "ns-byoi-1": {
            "serverId": "ns-byoi-1",
            "displayName": "ns-byoi-1",
            "hardware": "byoi:debian-12",
            "cores": 16,
            "memory": 65536,
            "ip": [{"ip": "198.51.100.21"}],
        },
        "ns-sku-2": {
            "serverId": "ns-sku-2",
            "displayName": "ns-sku-2",
            "cores": 8,
            "memory": 32768,
            "commercialRange": "KS-6 | AMD Epyc 7351P",
            "ip": [{"ip": "198.51.100.22"}],
        },
    }


def test_byoi_client_reinstall_posts_signed_body():
    """reinstall_server sends the operatingSystem body and returns the task id."""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/1.0/auth/time":
            return httpx.Response(200, json=int(time.time()))
        if request.url.path == "/1.0/dedicated/server/ns-byoi-1/reinstall":
            assert request.method == "POST"
            assert "X-Ovh-Signature" in request.headers
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json="1234567890")
        return httpx.Response(404, json={"message": f"no route {request.url.path}"})

    client = OvhClient(
        endpoint=DEFAULT_ENDPOINT, app_key="AK", app_secret="S3CRET", consumer_key="CK"
    )
    client._client = httpx.Client(
        transport=httpx.MockTransport(handler), timeout=client.timeout
    )
    task_id = client.reinstall_server("ns-byoi-1", "byoi:debian-12")
    assert task_id == "1234567890"
    assert seen["body"] == {"operatingSystem": "byoi:debian-12"}


def test_byoi_client_reinstall_with_customizations_and_storage():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/1.0/auth/time":
            return httpx.Response(200, json=int(time.time()))
        if request.url.path == "/1.0/dedicated/server/s1/reinstall":
            assert json.loads(request.content) == {
                "operatingSystem": "centos_7_x64",
                "customizations": {"sshKey": "ssh-rsa AAAA"},
                "storage": [{"type": "destroy"}],
            }
            return httpx.Response(200, json="t2")
        return httpx.Response(404, json={"message": "no"})

    client = OvhClient(
        endpoint=DEFAULT_ENDPOINT, app_key="AK", app_secret="S3CRET", consumer_key="CK"
    )
    client._client = httpx.Client(
        transport=httpx.MockTransport(handler), timeout=client.timeout
    )
    assert (
        client.reinstall_server(
            "s1",
            "centos_7_x64",
            customizations={"sshKey": "ssh-rsa AAAA"},
            storage=[{"type": "destroy"}],
        )
        == "t2"
    )


def test_byoi_client_templates_and_status_defensive():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/1.0/auth/time":
            return httpx.Response(200, json=int(time.time()))
        if request.url.path == "/1.0/dedicated/server/osAvailabilities":
            hardware = request.url.params["hardware"]
            return httpx.Response(
                200,
                json={"byoi:debian-12": ["debian-12", "ubuntu-2204"], "KS-6": []}.get(
                    hardware, []
                ),
            )
        if request.url.path == "/1.0/dedicated/server/s1/install/compatibleTemplates":
            return httpx.Response(200, json={"ovh": {"debian-12": True}, "byoi": ["x"]})
        if request.url.path == "/1.0/dedicated/server/s1/install/status":
            return httpx.Response(200, json={"status": "running", "progress": 12})
        if (
            request.url.path
            == "/1.0/dedicated/server/weird/install/compatibleTemplates"
        ):
            return httpx.Response(200, json=["not", "a", "dict"])
        if request.url.path == "/1.0/dedicated/server/gone":
            return httpx.Response(
                404, json={"message": "not found", "errorCode": "NOT_FOUND"}
            )
        return httpx.Response(404, json={"message": f"no route {request.url.path}"})

    client = OvhClient(
        endpoint=DEFAULT_ENDPOINT, app_key="AK", app_secret="S3CRET", consumer_key="CK"
    )
    client._client = httpx.Client(
        transport=httpx.MockTransport(handler), timeout=client.timeout
    )

    assert client.list_os_templates("byoi:debian-12") == ["debian-12", "ubuntu-2204"]
    # Unknown hardware: OVH legitimately answers [] (and the transport default does too)
    assert client.list_os_templates("KS-6") == []
    assert client.list_compatible_templates("s1") == {
        "ovh": {"debian-12": True},
        "byoi": ["x"],
    }
    assert client.list_compatible_templates("weird") == {}
    assert client.install_status("s1") == {"status": "running", "progress": 12}
    # get_server degrades 404 to {}
    assert client.get_server("gone") == {}
    # reinstall error surfaces as OvhError (403 code mapping)
    with pytest.raises(OvhError):
        client.reinstall_server("denied", "debian-12")


def test_byoi_templates_endpoint(client, admin_headers, monkeypatch):
    servers = _ovh_byoi_servers()
    _patch_client_factory(
        monkeypatch,
        _mock_transport(
            servers=servers,
            ids=["ns-byoi-1", "ns-sku-2"],
            os_availability={
                "byoi:debian-12": ["debian-12", "ubuntu-2204"],
                "KS-6": [],
            },
            compatible={"ns-byoi-1": {"ovh": {"debian-12": True}}},
            install_status={"ns-byoi-1": {"status": "running"}},
        ),
    )
    ts = int(time.time() * 1000)
    acc = _create_ovh_account(client, admin_headers, f"tpl-{ts}")
    eid = _create_env(client, admin_headers, f"ovh-env-{ts}")
    _bind_env_to_account(client, admin_headers, eid, acc["id"])
    _store_account_key(client, admin_headers, acc["id"])

    resp = client.get(
        f"/api/v1/environments/{eid}/ovh/templates",
        headers=admin_headers,
        params={"server": "ns-byoi-1"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["hardware"] == "byoi:debian-12"
    assert body["hardware_templates"] == ["debian-12", "ubuntu-2204"]
    assert body["compatible"] == {"ovh": {"debian-12": True}}
    assert body["status"] == {"status": "running"}

    # SKU server: hardware from commercialRange; OVH answers [] for KS-6
    resp = client.get(
        f"/api/v1/environments/{eid}/ovh/templates",
        headers=admin_headers,
        params={"server": "ns-sku-2"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["hardware"] == "KS-6 | AMD Epyc 7351P"
    assert resp.json()["hardware_templates"] == []

    # Unknown server: 404 from OVH degrades to an error message, still 200.
    resp = client.get(
        f"/api/v1/environments/{eid}/ovh/templates",
        headers=admin_headers,
        params={"server": "nope"},
    )
    assert resp.status_code == 200, resp.text
    assert "not found" in resp.json()["error"]

    # No server param: only the configured flag.
    resp = client.get(
        f"/api/v1/environments/{eid}/ovh/templates", headers=admin_headers
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True, "configured": True, "account_id": acc["id"]}


def test_byoi_templates_endpoint_guards(
    client, admin_headers, operator_headers, monkeypatch
):
    _patch_client_factory(monkeypatch, _mock_transport())
    ts = int(time.time() * 1000)
    eid = _create_env(client, admin_headers, f"ovh-env-{ts}")
    # Unbound env -> 404.
    resp = client.get(
        f"/api/v1/environments/{eid}/ovh/templates", headers=admin_headers
    )
    assert resp.status_code == 404, resp.text
    # Bound but no consumer key -> 503.
    acc = _create_ovh_account(client, admin_headers, f"tpl-{ts}")
    _bind_env_to_account(client, admin_headers, eid, acc["id"])
    resp = client.get(
        f"/api/v1/environments/{eid}/ovh/templates", headers=admin_headers
    )
    assert resp.status_code == 503, resp.text
    # Viewer may read templates (env-scoped viewer guard).
    _store_account_key(client, admin_headers, acc["id"])
    resp = client.get(
        f"/api/v1/environments/{eid}/ovh/templates",
        headers=operator_headers,
        params={"server": "s1"},
    )
    assert resp.status_code == 200, resp.text


def _seed_ovh_doc(client, admin_headers, env_id: str, monkeypatch) -> None:
    """Store a config doc with source:ovh servers.

    Seeds the version row directly (bypassing the PUT /config endpoint,
    which validates against VALID_SERVER_SOURCES — only maas/static/
    baremetal until the envconfig source change lands). The monkeypatch
    keeps get_current's re-parse valid for the life of the test.
    """
    from app.db import SessionLocal
    from app.models import Environment, EnvConfigVersion

    doc_yaml = (
        "provider: kubespray\n"
        "servers:\n"
        "  ns-byoi-1:\n"
        "    source: ovh\n"
        "    ip: 198.51.100.21\n"
        "    service_name: ns-byoi-1\n"
        "    roles: [compute]\n"
        "  ns-sku-2:\n"
        "    source: ovh\n"
        "    ip: 198.51.100.22\n"
        "    roles: [compute]\n"
        "  other-node:\n"
        "    source: static\n"
        "    ip: 198.51.100.99\n"
    )
    monkeypatch.setattr(
        "app.services.envconfig.VALID_SERVER_SOURCES",
        frozenset({"maas", "static", "baremetal", "ovh"}),
    )
    with SessionLocal() as db:
        env = db.get(Environment, env_id)
        db.add(
            EnvConfigVersion(
                environment_id=env.id,
                version=1,
                yaml_text=doc_yaml,
                created_by="test",
            )
        )
        db.commit()


def test_byoi_reinstall_op_in_catalog():
    from app.services.catalog import get_operation

    op = get_operation("ovh.byoi.reinstall")
    assert op is not None
    assert op.required_role == "admin"
    assert op.mutating is True
    names = {p.name: p for p in op.params}
    assert names["operating_system"].required is False
    assert names["server_hostnames"].required is False
    assert names["server_hostnames"].type == "array"
    assert "image_url" in names
    assert "wait" in names
    assert op.timeout_seconds >= 7200


def _byoi_env_setup(client, admin_headers, monkeypatch, **transport):
    ts = int(time.time() * 1000)
    monkeypatch.setattr(
        "app.services.baremetal.talos_api_ready", lambda ip, log=None: True
    )
    _patch_client_factory(
        monkeypatch,
        _mock_transport(
            servers=_ovh_byoi_servers(), ids=["ns-byoi-1", "ns-sku-2"], **transport
        ),
    )
    acc = _create_ovh_account(client, admin_headers, f"byoi-{ts}")
    eid = _create_env(client, admin_headers, f"ovh-env-{ts}")
    _bind_env_to_account(client, admin_headers, eid, acc["id"])
    _store_account_key(client, admin_headers, acc["id"])
    _seed_ovh_doc(client, admin_headers, eid, monkeypatch)
    return eid


def _job(client, admin_headers, job_id):
    resp = client.get(f"/api/v1/jobs/{job_id}", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_byoi_reinstall_dry_run_job(client, admin_headers, monkeypatch):
    """Global dry_run=True: run_sync job rehearses, no reinstall POST is sent."""
    eid = _byoi_env_setup(client, admin_headers, monkeypatch)
    resp = client.post(
        f"/api/v1/environments/{eid}/ovh/byoi",
        headers=admin_headers,
        json={"operating_system": "byoi:debian-12", "run_sync": True},
    )
    assert resp.status_code == 200, resp.text
    job = _job(client, admin_headers, resp.json()["job_id"])
    assert job["status"] == "success", job
    assert job["dry_run"] is True
    log_text = job["log_text"]
    assert "[dry-run] would POST /dedicated/server/ns-byoi-1/reinstall" in log_text
    assert "[dry-run] would POST /dedicated/server/ns-sku-2/reinstall" in log_text
    assert "resolved ns-sku-2 -> ns-sku-2" in log_text
    assert "would reinstall 2 server(s)" in log_text
    # the static server is never selected
    assert "other-node" not in log_text


def test_byoi_reinstall_wet_env_runs_and_reports_failure(
    client, admin_headers, monkeypatch
):
    """Wet env (dry_run=False): real reinstalls; one failing server does not
    abort the others and the job reports the per-server outcome."""
    eid = _byoi_env_setup(
        client,
        admin_headers,
        monkeypatch,
        reinstall_fail={"ns-sku-2": "server is busy"},
    )
    env = client.patch(
        f"/api/v1/environments/{eid}",
        headers=admin_headers,
        json={"dry_run": False},
    )
    assert env.status_code == 200, env.text

    resp = client.post(
        f"/api/v1/environments/{eid}/ovh/byoi",
        headers=admin_headers,
        json={"operating_system": "byoi:debian-12", "run_sync": True},
    )
    assert resp.status_code == 200, resp.text
    job = _job(client, admin_headers, resp.json()["job_id"])
    assert job["status"] == "failed", job
    log_text = job["log_text"]
    assert "reinstall ns-byoi-1 accepted" in log_text
    assert "reinstall ns-sku-2 failed: OVH denied the call" in log_text
    assert "reinstalled 1/2 server(s)" in log_text
    assert "task task-ns-byoi-1" in log_text


def test_byoi_reinstall_hostnames_filter(client, admin_headers, monkeypatch):
    eid = _byoi_env_setup(client, admin_headers, monkeypatch)
    resp = client.post(
        f"/api/v1/environments/{eid}/ovh/byoi",
        headers=admin_headers,
        json={
            "operating_system": "byoi:debian-12",
            "server_hostnames": ["ns-byoi-1"],
            "run_sync": True,
        },
    )
    assert resp.status_code == 200, resp.text
    job = _job(client, admin_headers, resp.json()["job_id"])
    assert job["status"] == "success", job
    log_text = job["log_text"]
    assert "would POST /dedicated/server/ns-byoi-1/reinstall" in log_text
    assert "ns-sku-2" not in log_text
    assert "would reinstall 1 server(s)" in log_text


def test_byoi_reinstall_requires_admin(
    client, admin_headers, operator_headers, monkeypatch
):
    eid = _byoi_env_setup(client, admin_headers, monkeypatch)
    resp = client.post(
        f"/api/v1/environments/{eid}/ovh/byoi",
        headers=operator_headers,
        json={"operating_system": "byoi:debian-12"},
    )
    assert resp.status_code == 403, resp.text


def test_byoi_reinstall_guards(client, admin_headers, monkeypatch):
    ts = int(time.time() * 1000)
    _patch_client_factory(monkeypatch, _mock_transport())
    eid = _create_env(client, admin_headers, f"ovh-env-{ts}")
    # Unbound env: enqueues, the job fails at run time with a clear error.
    # operating_system is optional (Talos reads talos.image_url); bind is not.
    resp = client.post(
        f"/api/v1/environments/{eid}/ovh/byoi",
        headers=admin_headers,
        json={"operating_system": "byoi:debian-12", "run_sync": True},
    )
    assert resp.status_code == 200, resp.text
    job = _job(client, admin_headers, resp.json()["job_id"])
    assert job["status"] == "failed", job
    assert "not bound to an OVH account" in job["log_text"]


def test_byoi_reinstall_missing_doc_fails_job(client, admin_headers, monkeypatch):
    ts = int(time.time() * 1000)
    _patch_client_factory(monkeypatch, _mock_transport(servers=_ovh_byoi_servers()))
    acc = _create_ovh_account(client, admin_headers, f"byoi-{ts}")
    eid = _create_env(client, admin_headers, f"ovh-env-{ts}")
    _bind_env_to_account(client, admin_headers, eid, acc["id"])
    _store_account_key(client, admin_headers, acc["id"])
    # No config document stored at all.
    resp = client.post(
        f"/api/v1/environments/{eid}/ovh/byoi",
        headers=admin_headers,
        json={"operating_system": "byoi:debian-12", "run_sync": True},
    )
    assert resp.status_code == 200, resp.text
    job = _job(client, admin_headers, resp.json()["job_id"])
    assert job["status"] == "failed", job
    assert "no config document" in job["log_text"]


def test_byoi_reinstall_unresolved_server_reported(client, admin_headers, monkeypatch):
    """An ovh server with no service_name and no matching inventory IP is a
    per-server failure, not a silent skip."""
    from app.db import SessionLocal
    from app.models import Environment, EnvConfigVersion

    eid = _byoi_env_setup(client, admin_headers, monkeypatch)
    # Replace the doc with an unresolvable ovh server (keep monkeypatch active
    # so the new version parses too).
    monkeypatch.setattr(
        "app.services.envconfig.VALID_SERVER_SOURCES",
        frozenset({"maas", "static", "baremetal", "ovh"}),
    )
    doc_yaml = (
        "provider: kubespray\n"
        "servers:\n"
        "  ghost-node:\n"
        "    source: ovh\n"
        "    ip: 203.0.113.99\n"
        "    roles: [compute]\n"
    )
    with SessionLocal() as db:
        env = db.get(Environment, eid)
        db.add(
            EnvConfigVersion(
                environment_id=env.id, version=2, yaml_text=doc_yaml, created_by="test"
            )
        )
        db.commit()

    resp = client.post(
        f"/api/v1/environments/{eid}/ovh/byoi",
        headers=admin_headers,
        json={"operating_system": "byoi:debian-12", "run_sync": True},
    )
    assert resp.status_code == 200, resp.text
    job = _job(client, admin_headers, resp.json()["job_id"])
    assert job["status"] == "failed", job
    log_text = job["log_text"]
    assert "skipping 'ghost-node'" in log_text
    assert "no OVH server matches" in log_text


def test_adopt_tags_static_servers_matching_ovh_inventory(
    client, admin_headers, monkeypatch
):
    """OVH-bound env with source:static hosts: POST /ovh/adopt writes source:ovh."""
    from app.db import SessionLocal
    from app.models import Environment, EnvConfigVersion
    from app.services import envconfig as envconfig_service

    ts = int(time.time() * 1000)
    _patch_client_factory(
        monkeypatch,
        _mock_transport(servers=_ovh_byoi_servers(), ids=["ns-byoi-1", "ns-sku-2"]),
    )
    acc = _create_ovh_account(client, admin_headers, f"adopt-{ts}")
    eid = _create_env(client, admin_headers, f"ovh-adopt-{ts}")
    _bind_env_to_account(client, admin_headers, eid, acc["id"])
    _store_account_key(client, admin_headers, acc["id"])
    doc_yaml = (
        "provider: talos\n"
        "servers:\n"
        "  ns-byoi-1:\n"
        "    source: static\n"
        "    ip: 198.51.100.21\n"
        "    roles: [k8s_control_plane]\n"
        "  jump:\n"
        "    source: static\n"
        "    ip: 10.0.0.9\n"
        "    roles: [control]\n"
    )
    with SessionLocal() as db:
        env = db.get(Environment, eid)
        db.add(
            EnvConfigVersion(
                environment_id=env.id, version=1, yaml_text=doc_yaml, created_by="test"
            )
        )
        db.commit()

    resp = client.post(f"/api/v1/environments/{eid}/ovh/adopt", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["ovh_environment"] is True
    adopted_hosts = {a["hostname"]: a["service_name"] for a in body["adopted"]}
    assert adopted_hosts == {"ns-byoi-1": "ns-byoi-1"}

    with SessionLocal() as db:
        env = db.get(Environment, eid)
        doc, _row = envconfig_service.get_current(db, env)
    assert doc["servers"]["ns-byoi-1"]["source"] == "ovh"
    assert doc["servers"]["ns-byoi-1"]["service_name"] == "ns-byoi-1"
    assert doc["servers"]["jump"]["source"] == "static"

    servers = client.get(
        f"/api/v1/environments/{eid}/servers", headers=admin_headers
    ).json()
    assert servers["ovh_bound"] is True
    by_host = {s["hostname"]: s for s in servers["servers"]}
    assert by_host["ns-byoi-1"]["source"] == "ovh"
    assert by_host["jump"]["source"] == "static"

    provider = client.get(
        f"/api/v1/environments/{eid}/config/provider", headers=admin_headers
    ).json()
    assert provider["infra"] == "ovh"
    assert provider["ovh_account_id"] == acc["id"]

    env_body = client.get(f"/api/v1/environments/{eid}", headers=admin_headers).json()
    assert env_body["ovh_account_id"] == acc["id"]


def test_byoi_inherits_static_servers_in_ovh_environment(
    client, admin_headers, monkeypatch
):
    """Bound OVH env: static hosts that match inventory are BYOI targets.

    The unmatched static jump host is skipped (not failed).
    """
    from app.db import SessionLocal
    from app.models import Environment, EnvConfigVersion

    ts = int(time.time() * 1000)
    _patch_client_factory(
        monkeypatch,
        _mock_transport(servers=_ovh_byoi_servers(), ids=["ns-byoi-1", "ns-sku-2"]),
    )
    acc = _create_ovh_account(client, admin_headers, f"inherit-{ts}")
    eid = _create_env(client, admin_headers, f"ovh-inherit-{ts}")
    _bind_env_to_account(client, admin_headers, eid, acc["id"])
    _store_account_key(client, admin_headers, acc["id"])
    doc_yaml = (
        "provider: talos\n"
        "talos:\n"
        "  cluster_name: example\n"
        "  image_url: https://factory.talos.dev/image/abc/v1.9.0/metal-amd64.qcow2\n"
        "servers:\n"
        "  ns-byoi-1:\n"
        "    source: static\n"
        "    ip: 198.51.100.21\n"
        "    roles: [k8s_control_plane]\n"
        "  ns-sku-2:\n"
        "    source: static\n"
        "    ip: 198.51.100.22\n"
        "    roles: [compute]\n"
        "  jump:\n"
        "    source: static\n"
        "    ip: 10.0.0.9\n"
        "    roles: [control]\n"
    )
    with SessionLocal() as db:
        env = db.get(Environment, eid)
        db.add(
            EnvConfigVersion(
                environment_id=env.id, version=1, yaml_text=doc_yaml, created_by="test"
            )
        )
        db.commit()

    resp = client.post(
        f"/api/v1/environments/{eid}/ovh/byoi",
        headers=admin_headers,
        json={"operating_system": "byoi:debian-12", "run_sync": True},
    )
    assert resp.status_code == 200, resp.text
    job = _job(client, admin_headers, resp.json()["job_id"])
    assert job["status"] == "success", job
    log_text = job["log_text"]
    assert "resolved ns-byoi-1 -> ns-byoi-1" in log_text
    assert "resolved ns-sku-2 -> ns-sku-2" in log_text
    assert "jump" not in log_text or "would POST /dedicated/server/jump" not in log_text


def test_ovh_status_marks_bound_environment(client, admin_headers):
    acc = _create_ovh_account(client, admin_headers, f"idenv-{int(time.time() * 1000)}")
    eid = _create_env(client, admin_headers, f"ovh-idenv-{int(time.time() * 1000)}")
    resp = client.get(f"/api/v1/ovh?environment_id={eid}", headers=admin_headers)
    assert resp.json()["ovh_environment"] is False
    _bind_env_to_account(client, admin_headers, eid, acc["id"])
    resp = client.get(f"/api/v1/ovh?environment_id={eid}", headers=admin_headers)
    assert resp.json()["ovh_environment"] is True
    assert resp.json()["account_id"] == acc["id"]


def test_byoi_talos_sends_image_url_and_marks_talos_ready(
    client, admin_headers, monkeypatch
):
    """Talos + image_url: POST body carries BYOI customizations; wait hits :50000."""
    from app.db import SessionLocal
    from app.models import Environment, EnvConfigVersion

    captured: list[dict[str, Any]] = []
    ts = int(time.time() * 1000)
    monkeypatch.setattr(
        "app.services.baremetal.talos_api_ready", lambda ip, log=None: True
    )
    _patch_client_factory(
        monkeypatch,
        _mock_transport(
            servers=_ovh_byoi_servers(),
            ids=["ns-byoi-1", "ns-sku-2"],
            reinstall_requests=captured,
        ),
    )
    acc = _create_ovh_account(client, admin_headers, f"img-{ts}")
    eid = _create_env(client, admin_headers, f"ovh-img-{ts}")
    _bind_env_to_account(client, admin_headers, eid, acc["id"])
    _store_account_key(client, admin_headers, acc["id"])
    image = "https://factory.talos.dev/image/abc/v1.9.0/metal-amd64.qcow2"
    doc_yaml = (
        "provider: talos\n"
        "talos:\n"
        f"  image_url: {image}\n"
        "servers:\n"
        "  ns-byoi-1:\n"
        "    source: ovh\n"
        "    service_name: ns-byoi-1\n"
        "    ip: 198.51.100.21\n"
        "    roles: [k8s_control_plane]\n"
    )
    with SessionLocal() as db:
        env = db.get(Environment, eid)
        db.add(
            EnvConfigVersion(
                environment_id=env.id, version=1, yaml_text=doc_yaml, created_by="test"
            )
        )
        db.commit()
    client.patch(
        f"/api/v1/environments/{eid}",
        headers=admin_headers,
        json={"dry_run": False},
    )
    resp = client.post(
        f"/api/v1/environments/{eid}/ovh/byoi",
        headers=admin_headers,
        json={"run_sync": True},
    )
    assert resp.status_code == 200, resp.text
    job = _job(client, admin_headers, resp.json()["job_id"])
    assert job["status"] == "success", job
    assert captured, "reinstall POST was not issued"
    body = captured[0]["body"]
    assert body["operatingSystem"] == DEFAULT_BYOI_OS
    assert body["customizations"]["imageURL"] == image
    assert body["customizations"]["imageType"] == "qcow2"
    assert "efiBootloaderPath" in body["customizations"]
    log_text = job["log_text"]
    assert "talos API ready" in log_text
    assert "talos-ready" in log_text


def test_byoi_talos_uses_factory_default_image_url(client, admin_headers, monkeypatch):
    from app.services.talos import DEFAULT_TALOS_IMAGE_URL

    eid = _byoi_env_setup(client, admin_headers, monkeypatch)
    from app.db import SessionLocal
    from app.models import Environment, EnvConfigVersion

    with SessionLocal() as db:
        env = db.get(Environment, eid)
        db.add(
            EnvConfigVersion(
                environment_id=env.id,
                version=2,
                yaml_text="provider: talos\nservers:\n  ns-byoi-1:\n    source: ovh\n    ip: 198.51.100.21\n",
                created_by="test",
            )
        )
        db.commit()
    resp = client.post(
        f"/api/v1/environments/{eid}/ovh/byoi",
        headers=admin_headers,
        json={"run_sync": True},
    )
    assert resp.status_code == 200, resp.text
    job = _job(client, admin_headers, resp.json()["job_id"])
    assert job["status"] == "success", job
    assert DEFAULT_TALOS_IMAGE_URL in (job["log_text"] or "")


# ---------------------------------------------------------------------------
# vRack fabric (list / attach / interconnect / VLAN lookup)
# ---------------------------------------------------------------------------


def test_vrack_attach_op_in_catalog():
    from app.services.catalog import get_operation

    op = get_operation("ovh.vrack.attach")
    assert op is not None
    assert op.required_role == "admin"
    assert op.mutating is True
    assert op.handler == "ovh_vrack_attach"
    names = {p.name: p for p in op.params}
    assert "vrack" in names
    assert names["vrack"].required is False


def test_list_vracks_and_nics_client():
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/1.0/auth/time":
            return httpx.Response(200, json=int(time.time()))
        if path == "/1.0/vrack":
            return httpx.Response(200, json=["pn-111", "pn-222"])
        if path == "/1.0/vrack/pn-111":
            return httpx.Response(200, json={"name": "cluster", "description": "lab"})
        if path == "/1.0/vrack/pn-111/ip":
            return httpx.Response(200, json=["10.10.0.0/24"])
        if path.endswith("/ip/10.10.0.0%2F24") or path.endswith("/ip/10.10.0.0/24"):
            return httpx.Response(200, json={"ip": "10.10.0.0/24", "vlan": 10})
        if path == "/1.0/dedicated/server/ns1/networkInterfaceController":
            return httpx.Response(200, json=["00:11:22:33:44:55", "aa:bb:cc:dd:ee:ff"])
        if "networkInterfaceController/" in path:
            mac = unquote(path.rsplit("/", 1)[-1])
            if mac == "aa:bb:cc:dd:ee:ff":
                return httpx.Response(
                    200,
                    json={
                        "mac": mac,
                        "linkType": "private",
                        "virtualNetworkInterface": "vni-priv",
                    },
                )
            return httpx.Response(
                200,
                json={
                    "mac": mac,
                    "linkType": "public",
                    "virtualNetworkInterface": "vni-pub",
                },
            )
        return httpx.Response(404, json={"message": path})

    client = OvhClient(
        endpoint=DEFAULT_ENDPOINT, app_key="AK", app_secret="S3CRET", consumer_key="CK"
    )
    client._client = httpx.Client(
        transport=httpx.MockTransport(handler), timeout=client.timeout
    )
    assert client.list_vracks() == ["pn-111", "pn-222"]
    info = client.get_vrack("pn-111")
    assert info["name"] == "cluster"
    blocks = client.list_vrack_ips("pn-111")
    assert blocks[0]["ip"] == "10.10.0.0/24"
    assert blocks[0]["vlan"] == 10
    nics = client.list_nics("ns1")
    assert [n["mac"] for n in nics] == ["00:11:22:33:44:55", "aa:bb:cc:dd:ee:ff"]
    assert nics[1]["link_type"] == "private"
    assert nics[1]["vni"] == "vni-priv"


_VRACK_NICS = {
    "ns-byoi-1": [
        {"mac": "00:11:22:33:44:01", "link_type": "public", "vni": "vni-pub-1"},
        {"mac": "aa:bb:cc:dd:ee:01", "link_type": "private", "vni": "vni-priv-1"},
    ],
    "ns-sku-2": [
        {"mac": "00:11:22:33:44:02", "link_type": "public", "vni": "vni-pub-2"},
        {"mac": "aa:bb:cc:dd:ee:02", "link_type": "private", "vni": "vni-priv-2"},
    ],
}


def _vrack_env_setup(client, admin_headers, monkeypatch, **transport):
    ts = int(time.time() * 1000)
    captured = transport.pop("attach_requests", None)
    if captured is None:
        captured = []
        transport["attach_requests"] = captured
    _patch_client_factory(
        monkeypatch,
        _mock_transport(
            servers=_ovh_byoi_servers(),
            ids=["ns-byoi-1", "ns-sku-2"],
            nics=_VRACK_NICS,
            vracks=["pn-lab"],
            vrack_info={"pn-lab": {"name": "lab fabric", "description": "example"}},
            vrack_eligible={
                "pn-lab": {
                    "dedicatedServerInterface": ["vni-priv-1", "vni-priv-2"],
                    "dedicatedServer": ["ns-byoi-1", "ns-sku-2"],
                }
            },
            vrack_ips={
                "pn-lab": [{"ip": "10.10.0.0/24", "vlan": 10, "gateway": "10.10.0.1"}]
            },
            **transport,
        ),
    )
    acc = _create_ovh_account(client, admin_headers, f"vrack-{ts}")
    eid = _create_env(client, admin_headers, f"ovh-vrack-{ts}")
    _bind_env_to_account(client, admin_headers, eid, acc["id"])
    _store_account_key(client, admin_headers, acc["id"])
    from app.db import SessionLocal
    from app.models import Environment, EnvConfigVersion

    doc_yaml = (
        "provider: talos\n"
        "talos:\n"
        "  cluster_name: example\n"
        "ovh:\n"
        "  vrack: pn-lab\n"
        "  vlan_id: 10\n"
        "  private_cidr: 10.10.0.0/24\n"
        "servers:\n"
        "  ns-byoi-1:\n"
        "    source: ovh\n"
        "    service_name: ns-byoi-1\n"
        "    ip: 10.10.0.11\n"
        "    private_ip: 10.10.0.11\n"
        "    roles: [k8s_control_plane]\n"
        "  ns-sku-2:\n"
        "    source: ovh\n"
        "    service_name: ns-sku-2\n"
        "    ip: 10.10.0.12\n"
        "    private_ip: 10.10.0.12\n"
        "    roles: [compute]\n"
    )
    with SessionLocal() as db:
        env = db.get(Environment, eid)
        db.add(
            EnvConfigVersion(
                environment_id=env.id, version=1, yaml_text=doc_yaml, created_by="test"
            )
        )
        db.commit()
    return eid, captured


def test_vrack_status_reports_unattached_and_looks_up_vlan(
    client, admin_headers, monkeypatch
):
    eid, _captured = _vrack_env_setup(client, admin_headers, monkeypatch)
    cached = client.get(f"/api/v1/environments/{eid}/ovh/vrack", headers=admin_headers)
    assert cached.status_code == 200, cached.text
    assert cached.json()["source"] in ("local", "cache")
    assert cached.json()["stale"] is True
    resp = client.get(
        f"/api/v1/environments/{eid}/ovh/vrack",
        headers=admin_headers,
        params={"refresh": "true"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["source"] == "live"
    assert body["stale"] is False
    assert body["vrack"] == "pn-lab"
    assert body["vlan_id"] == 10
    assert body["private_cidr"] == "10.10.0.0/24"
    assert body["interconnect"] == "unattached"
    assert body["discovered_vlans"] == [10]
    assert body["ip_blocks"][0]["ip"] == "10.10.0.0/24"
    assert {s["hostname"] for s in body["servers"]} == {"ns-byoi-1", "ns-sku-2"}
    by_host = {s["hostname"]: s for s in body["servers"]}
    assert by_host["ns-byoi-1"]["vrack_vni"] == "vni-priv-1"
    assert by_host["ns-byoi-1"]["private_mac"] == "aa:bb:cc:dd:ee:01"
    assert by_host["ns-byoi-1"]["attached_to"] is None
    assert body["missing"] == ["ns-byoi-1", "ns-sku-2"]
    again = client.get(
        f"/api/v1/environments/{eid}/ovh/vrack", headers=admin_headers
    ).json()
    assert again["source"] == "cache"
    assert again["interconnect"] == "unattached"
    assert again["discovered_vlans"] == [10]


def test_vrack_save_and_attach_dry_run(client, admin_headers, monkeypatch):
    eid, captured = _vrack_env_setup(client, admin_headers, monkeypatch)
    resp = client.put(
        f"/api/v1/environments/{eid}/ovh/vrack",
        headers=admin_headers,
        json={"vrack": "pn-lab", "vlan_id": 20, "private_cidr": "10.20.0.0/24"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["ovh"]["vlan_id"] == 20
    assert resp.json()["ovh"]["private_cidr"] == "10.20.0.0/24"

    resp = client.post(
        f"/api/v1/environments/{eid}/ovh/vrack/attach",
        headers=admin_headers,
        json={"run_sync": True},
    )
    assert resp.status_code == 200, resp.text
    job = _job(client, admin_headers, resp.json()["job_id"])
    assert job["status"] == "success", job
    assert job["dry_run"] is True
    log_text = job["log_text"]
    assert "would attach ns-byoi-1" in log_text
    assert "would attach ns-sku-2" in log_text
    assert captured == []


def test_vrack_attach_wet_interconnects_via_vni(client, admin_headers, monkeypatch):
    eid, captured = _vrack_env_setup(client, admin_headers, monkeypatch)
    env = client.patch(
        f"/api/v1/environments/{eid}",
        headers=admin_headers,
        json={"dry_run": False},
    )
    assert env.status_code == 200, env.text
    resp = client.post(
        f"/api/v1/environments/{eid}/ovh/vrack/attach",
        headers=admin_headers,
        json={"run_sync": True},
    )
    assert resp.status_code == 200, resp.text
    job = _job(client, admin_headers, resp.json()["job_id"])
    assert job["status"] == "success", job
    assert {row["kind"] for row in captured} == {"interface"}
    assert {row["id"] for row in captured} == {"vni-priv-1", "vni-priv-2"}
    status = client.get(
        f"/api/v1/environments/{eid}/ovh/vrack",
        headers=admin_headers,
        params={"refresh": "true"},
    ).json()
    assert status["interconnect"] == "ok"
    assert status["missing"] == []
    attached = {s["hostname"]: s["attached_to"] for s in status["servers"]}
    assert attached == {"ns-byoi-1": "pn-lab", "ns-sku-2": "pn-lab"}


def test_vrack_attach_requires_admin(
    client, admin_headers, operator_headers, monkeypatch
):
    eid, _captured = _vrack_env_setup(client, admin_headers, monkeypatch)
    resp = client.post(
        f"/api/v1/environments/{eid}/ovh/vrack/attach",
        headers=operator_headers,
        json={"run_sync": True},
    )
    assert resp.status_code == 403


def test_vrack_status_partial_when_one_server_already_on_fabric(
    client, admin_headers, monkeypatch
):
    eid, _captured = _vrack_env_setup(
        client,
        admin_headers,
        monkeypatch,
        vrack_interfaces={"pn-lab": ["vni-priv-1"]},
        vrack_interface_details={
            "pn-lab": [
                {
                    "dedicatedServer": "ns-byoi-1",
                    "dedicatedServerInterface": "vni-priv-1",
                }
            ]
        },
    )
    body = client.get(
        f"/api/v1/environments/{eid}/ovh/vrack",
        headers=admin_headers,
        params={"refresh": "true"},
    ).json()
    assert body["interconnect"] == "partial"
    assert body["attached"] == ["ns-byoi-1"]
    assert body["missing"] == ["ns-sku-2"]


def test_private_ips_from_cidr_skips_gateway():
    from app.services.ovh_fabric import private_ips_from_cidr

    ips = private_ips_from_cidr("10.10.0.0/24", ["b", "a", "c"])
    assert ips == {"a": "10.10.0.11", "b": "10.10.0.12", "c": "10.10.0.13"}


def test_suggest_vrack_matches_cluster_name():
    from app.services.ovh_fabric import suggest_vrack

    picked = suggest_vrack(
        [
            {"id": "pn-1", "name": "other", "description": ""},
            {"id": "pn-2", "name": "Example Fabric", "description": "prod fabric"},
        ],
        ["OVH", "example", "Example Fabric Prod"],
    )
    assert picked is not None
    assert picked["id"] == "pn-2"


def test_vrack_refresh_includes_display_name(client, admin_headers, monkeypatch):
    eid, _captured = _vrack_env_setup(client, admin_headers, monkeypatch)
    body = client.get(
        f"/api/v1/environments/{eid}/ovh/vrack",
        headers=admin_headers,
        params={"refresh": "true"},
    ).json()
    by_id = {v["id"]: v for v in body["vracks"]}
    assert by_id["pn-lab"]["name"] == "lab fabric"
    assert body["suggested_vrack"]["id"] == "pn-lab"
    assert body["defaults"]["private_cidr"] == "10.10.0.0/24"
    assert body["proposed_ips"]["ns-byoi-1"] == "10.10.0.11"


def test_vrack_provision_assigns_private_ips(client, admin_headers, monkeypatch):
    eid, _captured = _vrack_env_setup(client, admin_headers, monkeypatch)
    client.get(
        f"/api/v1/environments/{eid}/ovh/vrack",
        headers=admin_headers,
        params={"refresh": "true"},
    )
    resp = client.post(
        f"/api/v1/environments/{eid}/ovh/vrack/provision",
        headers=admin_headers,
        json={"assign_ips": True, "attach": False},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["vrack"] == "pn-lab"
    assert body["private_cidr"] == "10.10.0.0/24"
    assert body["vlan_id"] == 10
    assert body["ips"]["ns-byoi-1"] == "10.10.0.11"
    servers = client.get(
        f"/api/v1/environments/{eid}/servers", headers=admin_headers
    ).json()["servers"]
    by_host = {s["hostname"]: s for s in servers}
    assert by_host["ns-byoi-1"]["private_ip"] == "10.10.0.11"
    assert by_host["ns-sku-2"]["private_ip"] == "10.10.0.12"


def test_vrack_nic_put_updates_inventory(client, admin_headers, monkeypatch):
    eid, _captured = _vrack_env_setup(client, admin_headers, monkeypatch)
    resp = client.put(
        f"/api/v1/environments/{eid}/ovh/vrack/nics",
        headers=admin_headers,
        json={
            "hostname": "ns-byoi-1",
            "private_ip": "10.10.0.11",
            "private_mac": "aa:bb:cc:dd:ee:01",
            "vrack_vni": "vni-priv-1",
            "public_ip": "198.51.100.21",
            "public_mac": "00:11:22:33:44:01",
        },
    )
    assert resp.status_code == 200, resp.text
    servers = client.get(
        f"/api/v1/environments/{eid}/servers", headers=admin_headers
    ).json()["servers"]
    by_host = {s["hostname"]: s for s in servers}
    assert by_host["ns-byoi-1"]["private_mac"] == "aa:bb:cc:dd:ee:01"
    assert by_host["ns-byoi-1"]["vrack_vni"] == "vni-priv-1"
    assert by_host["ns-byoi-1"]["public_mac"] == "00:11:22:33:44:01"
