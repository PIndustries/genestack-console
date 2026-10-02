"""API key authentication and role authorization."""

from __future__ import annotations

import hashlib
import uuid

import pytest


def test_no_key_returns_401(client):
    """Protected endpoints reject missing API key."""
    for path in (
        "/api/v1/operations",
        "/api/v1/environments",
        "/api/v1/jobs",
        "/api/v1/auth/whoami",
    ):
        resp = client.get(path)
        assert (
            resp.status_code == 401
        ), f"{path} expected 401 without key, got {resp.status_code}"


def test_whoami_returns_role_per_key(
    client, admin_headers, operator_headers, viewer_headers
):
    """GET /api/v1/auth/whoami echoes key name and role for each key."""
    for headers, role in (
        (admin_headers, "admin"),
        (operator_headers, "operator"),
        (viewer_headers, "viewer"),
    ):
        resp = client.get("/api/v1/auth/whoami", headers=headers)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body.get("role") == role
        assert body.get("key_name")


def test_viewer_can_list_operations(client, viewer_headers):
    """Viewer role may list the operation catalog."""
    resp = client.get("/api/v1/operations", headers=viewer_headers)
    assert resp.status_code == 200
    data = resp.json()
    ops = (
        data
        if isinstance(data, list)
        else data.get("operations") or data.get("items") or []
    )
    assert isinstance(ops, list)
    assert len(ops) > 0


def test_viewer_cannot_create_job_requiring_operator(
    client, viewer_headers, admin_headers
):
    """Viewer is forbidden from submitting operator-level jobs (403)."""
    env_name = f"auth-test-env-{uuid.uuid4().hex[:8]}"
    env_resp = client.post(
        "/api/v1/environments",
        headers=admin_headers,
        json={"name": env_name, "description": "auth test env"},
    )
    assert env_resp.status_code in (200, 201), env_resp.text
    env_id = env_resp.json()["id"]

    # host.preflight requires operator; create-job endpoint also requires operator
    resp = client.post(
        f"/api/v1/environments/{env_id}/jobs",
        headers=viewer_headers,
        json={"operation": "host.preflight", "params": {}, "run_sync": True},
    )
    assert resp.status_code == 403, resp.text

    # Global create path also forbidden for viewers
    resp2 = client.post(
        "/api/v1/jobs",
        headers=viewer_headers,
        params={"environment_id": env_id},
        json={"operation": "host.preflight", "params": {}},
    )
    assert resp2.status_code == 403, resp2.text


def test_admin_can_create_environment(client, admin_headers):
    """Admin may create an environment."""
    name = f"admin-env-{uuid.uuid4().hex[:8]}"
    resp = client.post(
        "/api/v1/environments",
        headers=admin_headers,
        json={"name": name, "description": "created by auth test"},
    )
    assert resp.status_code in (200, 201), resp.text
    body = resp.json()
    assert body.get("name") == name
    assert body.get("id")


# ---------------------------------------------------------------------------
# Session auth (login/logout) and legacy key behavior
# ---------------------------------------------------------------------------


def _create_user(client, admin_headers, username, password, platform_admin=False):
    resp = client.post(
        "/api/v1/users",
        headers=admin_headers,
        json={
            "username": username,
            "password": password,
            "platform_admin": platform_admin,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _login(client, username, password):
    resp = client.post(
        "/api/v1/auth/login", json={"username": username, "password": password}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_legacy_keys_are_platform_admin(client, admin_headers, viewer_headers):
    """Static API keys still authenticate and carry platform_admin=True."""
    for headers in (admin_headers, viewer_headers):
        resp = client.get("/api/v1/auth/whoami", headers=headers)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["platform_admin"] is True
        assert body["auth_method"] == "api_key"
        assert body["user_id"] is None


def test_api_key_actor_label_is_non_reversible(client, admin_headers):
    """The audit label for a static key is role + sha256 fragment, never key material."""
    resp = client.get("/api/v1/auth/whoami", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    name = resp.json()["key_name"]
    expected = hashlib.sha256(b"dev-admin-key").hexdigest()[:6]
    assert name == f"admin:{expected}"
    assert "dev-admin-key" not in name
    assert "dev-admi" not in name  # the old label leaked the first 8 key chars


def test_login_token_works_on_whoami(client, admin_headers):
    """Login issues a session token that authenticates as the user."""
    username = f"login-{uuid.uuid4().hex[:8]}"
    user = _create_user(client, admin_headers, username, "pw-login")

    body = _login(client, username, "pw-login")
    assert body["token"]
    assert body["expires_at"]
    assert body["user"]["username"] == username
    assert body["user"]["platform_admin"] is False
    assert body["user"]["tenants"] == []

    resp = client.get(
        "/api/v1/auth/whoami",
        headers={"Authorization": f"Bearer {body['token']}"},
    )
    assert resp.status_code == 200, resp.text
    who = resp.json()
    assert who["key_name"] == username
    assert who["auth_method"] == "session"
    assert who["user_id"] == user["id"]
    assert who["platform_admin"] is False


def test_login_wrong_password_returns_401(client, admin_headers):
    username = f"login-bad-{uuid.uuid4().hex[:8]}"
    _create_user(client, admin_headers, username, "pw-right")
    resp = client.post(
        "/api/v1/auth/login", json={"username": username, "password": "pw-wrong"}
    )
    assert resp.status_code == 401
    resp = client.post(
        "/api/v1/auth/login", json={"username": f"missing-{username}", "password": "pw"}
    )
    assert resp.status_code == 401


def test_login_throttled_after_repeated_failures(client, admin_headers, monkeypatch):
    """Five failed logins per (ip, username) in 60s -> 429, no password check."""
    from app.routers import auth as auth_router

    monkeypatch.setattr(
        auth_router, "_login_throttle", auth_router._LoginThrottle(max_failures=3)
    )
    username = f"login-throttle-{uuid.uuid4().hex[:8]}"
    _create_user(client, admin_headers, username, "pw-right")

    for _ in range(3):
        resp = client.post(
            "/api/v1/auth/login", json={"username": username, "password": "nope"}
        )
        assert resp.status_code == 401

    # Locked out: even the correct password is rejected with 429.
    resp = client.post(
        "/api/v1/auth/login", json={"username": username, "password": "pw-right"}
    )
    assert resp.status_code == 429, resp.text
    assert "Too many" in resp.json()["detail"]

    # A different username from the same IP is not throttled.
    other = f"login-throttle2-{uuid.uuid4().hex[:8]}"
    _create_user(client, admin_headers, other, "pw-other")
    resp = client.post(
        "/api/v1/auth/login", json={"username": other, "password": "pw-other"}
    )
    assert resp.status_code == 200, resp.text


def _spoof_remote_peer(client_host: str = "10.1.2.3"):
    """A request whose direct peer is a remote host (XFF is untrusted)."""
    from types import SimpleNamespace

    class _FakeRequest:
        def __init__(self):
            self.client = SimpleNamespace(host=client_host)
            self.headers = {"x-forwarded-for": "1.1.1.1"}

    return _FakeRequest


def test_client_ip_remote_peer_ignores_xff():
    """C8: non-loopback peer -> identity is the peer, XFF is ignored."""
    from app.routers import auth as auth_router

    req = _spoof_remote_peer("10.1.2.3")()
    assert auth_router._client_ip(req) == "10.1.2.3"


def test_client_ip_loopback_peer_honors_xff():
    """C8: loopback peer (behind a local reverse proxy) -> honor XFF."""
    from app.routers import auth as auth_router

    for loopback in ("127.0.0.1", "::1"):
        req = _spoof_remote_peer(loopback)()
        assert auth_router._client_ip(req) == "1.1.1.1"


def test_login_throttle_remote_peer_cannot_rotate_xff(
    client, admin_headers, monkeypatch
):
    """C8: repeated failures from one remote peer are throttled even if the
    attacker rotates the X-Forwarded-For header on every attempt."""
    from app.routers import auth as auth_router

    monkeypatch.setattr(
        auth_router,
        "_login_throttle",
        auth_router._LoginThrottle(max_failures=3),
    )
    username = f"xff-throttle-{uuid.uuid4().hex[:8]}"
    _create_user(client, admin_headers, username, "pw-right")

    # Rotate XFF each attempt; direct peer stays a fixed remote host.
    for i in range(3):
        req = _spoof_remote_peer("10.1.2.3")()
        req.headers = {"x-forwarded-for": f"9.9.9.{i}"}
        body = auth_router.LoginRequest(username=username, password="nope")
        # Drive the handler directly with our fake request + real db session.
        from app.db import SessionLocal

        session = SessionLocal()
        try:
            with pytest.raises(Exception) as exc_info:
                auth_router.login(body, req, db=session)
        finally:
            session.close()
        assert exc_info.value.status_code == 401

    # Locked out: correct password still 429 because identity = peer, not XFF.
    req = _spoof_remote_peer("10.1.2.3")()
    req.headers = {"x-forwarded-for": "9.9.9.99"}
    body = auth_router.LoginRequest(username=username, password="pw-right")
    from app.db import SessionLocal

    session = SessionLocal()
    try:
        with pytest.raises(Exception) as exc_info:
            auth_router.login(body, req, db=session)
    finally:
        session.close()
    assert exc_info.value.status_code == 429, "XFF rotation must not reset the throttle"
    username = f"login-out-{uuid.uuid4().hex[:8]}"
    _create_user(client, admin_headers, username, "pw-logout")
    token = _login(client, username, "pw-logout")["token"]
    headers = {"Authorization": f"Bearer {token}"}

    assert client.get("/api/v1/auth/whoami", headers=headers).status_code == 200
    resp = client.post("/api/v1/auth/logout", headers=headers)
    assert resp.status_code == 200, resp.text
    assert client.get("/api/v1/auth/whoami", headers=headers).status_code == 401


def test_dev_auto_login_disabled_by_default(client):
    """No credentials and no dev_auto_login → 401."""
    resp = client.get("/api/v1/auth/whoami")
    assert resp.status_code == 401


def test_dev_auto_login_grants_platform_admin(client, monkeypatch):
    """With the toggle on, credential-less requests get a platform-admin principal."""
    from app import auth as auth_mod
    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "dev_auto_login", True, raising=False)
    monkeypatch.setattr(auth_mod, "get_settings", lambda: settings)

    resp = client.get("/api/v1/auth/whoami")
    assert resp.status_code == 200
    body = resp.json()
    assert body["auth_method"] == "dev_auto_login"
    assert body["platform_admin"] is True
    assert body["role"] == "admin"
