"""Deploy-host reach: WireGuard server, Tailscale, Cloudflare Tunnel."""

from __future__ import annotations

import base64
import uuid

import pytest
from sqlalchemy import delete

from app.config import load_settings
from app.db import SessionLocal
from app.models import ReachHub, ReachLink
from app.services.crypto import decrypt_secret
from app.services.reach import (
    allocate_peer_address,
    generate_keypair,
    get_or_seed_hub,
    hub_address,
    parse_network,
    public_from_private,
)


@pytest.fixture(autouse=True)
def _no_real_tunnel_tools(monkeypatch):
    """Tests must not call wg, tailscale, or cloudflared on this machine."""

    def _missing(_name: str):
        return None

    def _refuse_run(argv):
        raise AssertionError(f"run_command was called: {argv!r}")

    def _refuse_spawn(argv, env=None):
        raise AssertionError("spawn_process was called")

    monkeypatch.setattr("app.services.reach.tool_path", _missing)
    monkeypatch.setattr("app.services.reach.run_command", _refuse_run)
    monkeypatch.setattr("app.services.reach.spawn_process", _refuse_spawn)
    monkeypatch.setattr("app.services.reach.stop_process", lambda pid: None)
    yield
    db = SessionLocal()
    try:
        db.execute(delete(ReachLink))
        db.execute(delete(ReachHub))
        db.commit()
    finally:
        db.close()


def _env(client, headers) -> str:
    resp = client.post(
        "/api/v1/environments",
        headers=headers,
        json={"name": f"reach-{uuid.uuid4().hex[:8]}"},
    )
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["id"]


def _enable_wg(client, headers, **extra) -> dict:
    body = {"enabled": True, "endpoint": "203.0.113.10:51820"}
    body.update(extra)
    resp = client.put("/api/v1/reach/wireguard", headers=headers, json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _private_key(config: str) -> str:
    for line in config.splitlines():
        if line.startswith("PrivateKey = "):
            return line.split("=", 1)[1].strip()
    raise AssertionError("client config has no private key")


def test_keypair_and_address_allocation():
    private, public = generate_keypair()
    assert public_from_private(private) == public
    assert len(base64.b64decode(private)) == 32
    assert len(base64.b64decode(public)) == 32
    assert hub_address("10.67.67.0/24") == "10.67.67.1/24"
    assert allocate_peer_address("10.67.67.0/24", set()) == "10.67.67.2/32"
    assert allocate_peer_address("10.67.67.0/24", {"10.67.67.2/32"}) == "10.67.67.3/32"
    # A freed .2 is reused while .3 is still taken.
    assert allocate_peer_address("10.67.67.0/24", {"10.67.67.3"}) == "10.67.67.2/32"
    with pytest.raises(ValueError):
        parse_network("not-a-network")
    with pytest.raises(ValueError):
        parse_network("10.67.67.0/32")


def test_config_without_reach_sections_still_loads(tmp_path):
    cfg = tmp_path / "console.yaml"
    cfg.write_text(
        f"dry_run: true\ndata_dir: {tmp_path / 'data'}\n",
        encoding="utf-8",
    )
    settings = load_settings(cfg)
    assert settings.wg_enabled is False
    assert settings.wg_interface == "wg-gsc"
    assert settings.wg_network == "10.67.67.0/24"
    assert settings.ts_enabled is False
    assert settings.ts_hostname == "genestack-console"
    assert settings.ts_auth_key == ""
    assert settings.cf_enabled is False
    assert settings.cf_tunnel_token == ""


def test_placeholder_bootstrap_secret_is_not_stored(tmp_path):
    cfg = tmp_path / "console.yaml"
    cfg.write_text(
        "\n".join(
            [
                f"data_dir: {tmp_path / 'data'}",
                "tailscale:",
                "  enabled: true",
                "  auth_key: REPLACE_ME",
                "cloudflare:",
                "  enabled: true",
                "  tunnel_token: change-me",
            ]
        ),
        encoding="utf-8",
    )
    settings = load_settings(cfg)
    db = SessionLocal()
    try:
        tailscale = get_or_seed_hub(db, "tailscale", settings)
        cloudflare = get_or_seed_hub(db, "cloudflare", settings)
        assert tailscale.secret_encrypted is None
        assert cloudflare.secret_encrypted is None
    finally:
        db.rollback()
        db.close()


def test_get_does_not_echo_saved_secrets(client, admin_headers):
    auth_key = "ts-auth-key-example"
    token = "cf-tunnel-token-example"
    saved_ts = client.put(
        "/api/v1/reach/tailscale",
        headers=admin_headers,
        json={"enabled": True, "hostname": "genestack-console", "secret": auth_key},
    )
    assert saved_ts.status_code == 200, saved_ts.text
    assert saved_ts.json()["secret_configured"] is True
    assert auth_key not in saved_ts.text

    saved_cf = client.put(
        "/api/v1/reach/cloudflare",
        headers=admin_headers,
        json={"enabled": True, "secret": token},
    )
    assert saved_cf.status_code == 200, saved_cf.text
    assert token not in saved_cf.text

    listed = client.get("/api/v1/reach", headers=admin_headers)
    assert listed.status_code == 200, listed.text
    assert auth_key not in listed.text
    assert token not in listed.text

    db = SessionLocal()
    try:
        ts_row = db.get(ReachHub, "tailscale")
        cf_row = db.get(ReachHub, "cloudflare")
        assert decrypt_secret(ts_row.secret_encrypted) == auth_key
        assert decrypt_secret(cf_row.secret_encrypted) == token
        assert ts_row.secret_encrypted.startswith("fernet:")
    finally:
        db.close()


def test_viewer_cannot_read_hub_and_operator_cannot_save(
    client, admin_headers, operator_headers, viewer_headers
):
    assert client.get("/api/v1/reach", headers=viewer_headers).status_code == 403
    denied = client.put(
        "/api/v1/reach/tailscale",
        headers=operator_headers,
        json={"enabled": True, "secret": "should-not-save"},
    )
    assert denied.status_code == 403, denied.text
    listed = client.get("/api/v1/reach", headers=admin_headers)
    assert "should-not-save" not in listed.text


def test_tenant_admin_cannot_save_tunnel_token(client, admin_headers):
    tenant = client.post(
        "/api/v1/tenants",
        headers=admin_headers,
        json={"name": f"reach-t-{uuid.uuid4().hex[:8]}"},
    ).json()
    username = f"reach-admin-{uuid.uuid4().hex[:8]}"
    created = client.post(
        "/api/v1/users",
        headers=admin_headers,
        json={
            "username": username,
            "password": "reach-test-password",
            "memberships": [{"tenant_id": tenant["id"], "role": "admin"}],
        },
    )
    assert created.status_code == 201, created.text
    login = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": "reach-test-password"},
    )
    assert login.status_code == 200, login.text
    headers = {"Authorization": f"Bearer {login.json()['token']}"}
    denied = client.put(
        "/api/v1/reach/cloudflare",
        headers=headers,
        json={"enabled": True, "secret": "tenant-admin-token"},
    )
    assert denied.status_code == 403, denied.text


def test_bad_network_and_interface_are_rejected(client, admin_headers):
    enabled = _enable_wg(client, admin_headers)
    assert enabled["address"] == "10.67.67.1/24"
    assert enabled["public_key"]
    bad_net = client.put(
        "/api/v1/reach/wireguard",
        headers=admin_headers,
        json={"network": "999.1.1.1/99"},
    )
    assert bad_net.status_code == 400, bad_net.text
    bad_iface = client.put(
        "/api/v1/reach/wireguard",
        headers=admin_headers,
        json={"interface": "../wg"},
    )
    assert bad_iface.status_code == 400, bad_iface.text
    current = client.get("/api/v1/reach", headers=admin_headers).json()
    wg = next(row for row in current if row["kind"] == "wireguard")
    assert wg["network"] == "10.67.67.0/24"
    assert wg["interface"] == "wg-gsc"


def test_peer_config_is_returned_once_and_addresses_are_reused(client, admin_headers):
    _enable_wg(client, admin_headers)
    env_a = _env(client, admin_headers)
    env_b = _env(client, admin_headers)
    first = client.post(
        f"/api/v1/environments/{env_a}/reach/wireguard",
        headers=admin_headers,
        json={"name": "default"},
    )
    assert first.status_code == 201, first.text
    body = first.json()
    assert body["address"] == "10.67.67.2/32"
    config = body["client_config"]
    assert "PersistentKeepalive = 25" in config
    assert "AllowedIPs = 10.67.67.0/24" in config
    assert "Endpoint = 203.0.113.10:51820" in config
    private = _private_key(config)

    second = client.post(
        f"/api/v1/environments/{env_b}/reach/wireguard",
        headers=admin_headers,
        json={"name": "default"},
    )
    assert second.status_code == 201, second.text
    assert second.json()["address"] == "10.67.67.3/32"

    listed = client.get(f"/api/v1/environments/{env_a}/reach", headers=admin_headers)
    assert listed.status_code == 200, listed.text
    assert private not in listed.text
    assert "PrivateKey" not in listed.text
    assert listed.json()[0]["address"] == "10.67.67.2/32"

    removed = client.delete(
        f"/api/v1/environments/{env_a}/reach/wireguard/default",
        headers=admin_headers,
    )
    assert removed.status_code == 200, removed.text
    other = client.get(f"/api/v1/environments/{env_b}/reach", headers=admin_headers)
    assert other.json()[0]["address"] == "10.67.67.3/32"

    env_c = _env(client, admin_headers)
    reused = client.post(
        f"/api/v1/environments/{env_c}/reach/wireguard",
        headers=admin_headers,
        json={"name": "default"},
    )
    assert reused.status_code == 201, reused.text
    assert reused.json()["address"] == "10.67.67.2/32"
    assert _private_key(reused.json()["client_config"]) != private


def test_apply_missing_wg_writes_file(client, admin_headers):
    _enable_wg(client, admin_headers, endpoint="")
    env_id = _env(client, admin_headers)
    peer = client.post(
        f"/api/v1/environments/{env_id}/reach/wireguard",
        headers=admin_headers,
        json={"name": "default"},
    )
    assert peer.status_code == 201, peer.text
    private = _private_key(peer.json()["client_config"])

    applied = client.post("/api/v1/reach/wireguard/apply", headers=admin_headers)
    assert applied.status_code == 200, applied.text
    body = applied.json()
    assert body["status"] == "tool_missing"
    assert "peers cannot dial in until endpoint is set" in body["detail"]
    assert private not in applied.text

    from app.config import get_settings

    path = get_settings().data_dir / "wireguard" / "wg-gsc.conf"
    text = path.read_text(encoding="utf-8")
    assert "Address = 10.67.67.1/24" in text
    assert "AllowedIPs = 10.67.67.2/32" in text
    assert "PersistentKeepalive" not in text
    assert private not in text
    db = SessionLocal()
    try:
        hub_private = decrypt_secret(db.get(ReachHub, "wireguard").secret_encrypted)
    finally:
        db.close()
    assert hub_private in text
    assert hub_private not in applied.text
    assert path.stat().st_mode & 0o777 == 0o600


def test_tailscale_address_sets_empty_deploy_host_only(client, admin_headers):
    env_id = _env(client, admin_headers)
    saved = client.post(
        f"/api/v1/environments/{env_id}/reach/tailscale",
        headers=admin_headers,
        json={"address": "node.tailnet.ts.net", "use_for_ssh": True},
    )
    assert saved.status_code == 201, saved.text
    assert saved.json()["client_config"] is None
    env = client.get(f"/api/v1/environments/{env_id}", headers=admin_headers)
    assert env.json()["deployer_ssh_host"] == "node.tailnet.ts.net"

    again = client.post(
        f"/api/v1/environments/{env_id}/reach/cloudflare",
        headers=admin_headers,
        json={
            "address": "env.example.com",
            "local_port": 2222,
            "use_for_ssh": True,
        },
    )
    assert again.status_code == 201, again.text
    assert "already set" in (again.json()["detail"] or "")
    env = client.get(f"/api/v1/environments/{env_id}", headers=admin_headers)
    assert env.json()["deployer_ssh_host"] == "node.tailnet.ts.net"

    bad = client.post(
        f"/api/v1/environments/{env_id}/reach/tailscale",
        headers=admin_headers,
        json={"address": "host;rm -rf"},
    )
    assert bad.status_code == 400, bad.text


def test_cloudflare_apply_missing_tool_writes_token_file(client, admin_headers):
    token = "cf-tunnel-token-example"
    client.put(
        "/api/v1/reach/cloudflare",
        headers=admin_headers,
        json={"enabled": True, "secret": token},
    )
    applied = client.post("/api/v1/reach/cloudflare/apply", headers=admin_headers)
    assert applied.status_code == 200, applied.text
    assert applied.json()["status"] == "tool_missing"
    assert token not in applied.text
    from app.config import get_settings

    path = get_settings().data_dir / "cloudflared" / "tunnel.token"
    assert path.read_text(encoding="utf-8").strip() == token
    assert path.stat().st_mode & 0o777 == 0o600


def test_cloudflare_spawn_keeps_token_out_of_argv(client, admin_headers, monkeypatch):
    token = "cf-tunnel-token-example"
    spawned: list[tuple[list[str], dict]] = []
    stopped: list[int] = []

    monkeypatch.setattr("app.services.reach.tool_path", lambda name: f"/usr/bin/{name}")

    def _spawn(argv, env=None):
        spawned.append((list(argv), dict(env or {})))
        return 4242

    monkeypatch.setattr("app.services.reach.spawn_process", _spawn)
    monkeypatch.setattr("app.services.reach.stop_process", lambda pid: stopped.append(pid))

    client.put(
        "/api/v1/reach/cloudflare",
        headers=admin_headers,
        json={"enabled": True, "secret": token},
    )
    applied = client.post("/api/v1/reach/cloudflare/apply", headers=admin_headers)
    assert applied.status_code == 200, applied.text
    assert applied.json()["status"] == "applied"
    assert applied.json()["pid"] == 4242
    assert token not in applied.text
    argv, env = spawned[-1]
    assert argv == ["cloudflared", "tunnel", "run"]
    assert token not in argv
    assert env.get("TUNNEL_TOKEN") == token

    stopped_resp = client.post("/api/v1/reach/cloudflare/stop", headers=admin_headers)
    assert stopped_resp.status_code == 200, stopped_resp.text
    assert stopped == [4242]


def test_enroll_mints_a_peer_only_when_wireguard_is_on(client, admin_headers):
    env_id = _env(client, admin_headers)
    off = client.post(
        f"/api/v1/environments/{env_id}/agent/token",
        headers=admin_headers,
        json={"name": "default"},
    )
    assert off.status_code == 201, off.text
    assert off.json().get("wireguard") in (None, {})

    _enable_wg(client, admin_headers)
    on = client.post(
        f"/api/v1/environments/{env_id}/agent/token",
        headers=admin_headers,
        json={"name": "default"},
    )
    assert on.status_code == 201, on.text
    wg = on.json()["wireguard"]
    assert wg["address"] == "10.67.67.2/32"
    private = _private_key(wg["client_config"])
    listed = client.get(f"/api/v1/environments/{env_id}/agents", headers=admin_headers)
    assert private not in listed.text
    assert "GSC_HUB_URL=" in on.json()["docker_run"]
