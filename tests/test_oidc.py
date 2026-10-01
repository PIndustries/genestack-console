"""Optional OIDC/SSO login flow (config-driven; local login stays as fallback).

Network touchpoints (discovery metadata, code exchange / id_token verification
via authlib) are monkeypatched — no real provider is involved.
"""

from __future__ import annotations

import uuid
from urllib.parse import parse_qs, urlparse

import pytest

from app.config import get_settings
from app.services import oidc as oidc_mod

_ISSUER = "https://idp.example.com"
_META = {
    "authorization_endpoint": f"{_ISSUER}/authorize",
    "token_endpoint": f"{_ISSUER}/token",
    "jwks_uri": f"{_ISSUER}/jwks.json",
}


def _enable_oidc(monkeypatch, **overrides):
    """Flip the shared settings singleton into an OIDC-enabled configuration."""
    settings = get_settings()
    values = {
        "oidc_enabled": True,
        "oidc_issuer_url": _ISSUER,
        "oidc_client_id": "console-client",
        "oidc_client_secret": "client-secret",
        "oidc_redirect_url": "https://console.example.com/api/v1/auth/oidc/callback",
        "oidc_default_tenant": "",
        "oidc_default_role": "viewer",
        "oidc_label": "SSO",
    }
    values.update(overrides)
    for key, value in values.items():
        monkeypatch.setattr(settings, key, value, raising=False)
    return settings


def _mock_metadata(monkeypatch):
    monkeypatch.setattr(oidc_mod, "fetch_metadata", lambda settings: dict(_META))


def _mock_exchange(monkeypatch, claims):
    monkeypatch.setattr(oidc_mod, "exchange_code", lambda settings, code: dict(claims))


def _start_flow(client):
    """Hit /oidc/login and return (state, nonce) from the redirect."""
    resp = client.get("/api/v1/auth/oidc/login", follow_redirects=False)
    assert resp.status_code == 302, resp.text
    params = parse_qs(urlparse(resp.headers["location"]).query)
    return params["state"][0], params["nonce"][0]


def _finish_flow(client, email):
    """Run login + callback; return (callback response, session token)."""
    state, nonce = _start_flow(client)
    claims = {"iss": _ISSUER, "aud": "console-client", "sub": "sub-1", "nonce": nonce}
    if email:
        claims["email"] = email
    return state, nonce, claims


# ---------------------------------------------------------------------------
# /api/v1/auth/methods
# ---------------------------------------------------------------------------


def test_methods_oidc_disabled_by_default(client):
    resp = client.get("/api/v1/auth/methods")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["local"] is True
    assert body["oidc"] is False
    assert body["oidc_label"] == "SSO"


def test_methods_oidc_enabled_but_no_issuer_stays_off(client, monkeypatch):
    """enabled=true with an empty issuer URL means the feature stays off."""
    _enable_oidc(monkeypatch, oidc_issuer_url="")
    body = client.get("/api/v1/auth/methods").json()
    assert body["oidc"] is False


def test_methods_reflects_enabled_config(client, monkeypatch):
    _enable_oidc(monkeypatch, oidc_label="Corp SSO")
    body = client.get("/api/v1/auth/methods").json()
    assert body == {"local": True, "oidc": True, "oidc_label": "Corp SSO"}


# ---------------------------------------------------------------------------
# /api/v1/auth/oidc/login
# ---------------------------------------------------------------------------


def test_oidc_login_404_when_disabled(client):
    resp = client.get("/api/v1/auth/oidc/login", follow_redirects=False)
    assert resp.status_code == 404


def test_oidc_login_redirects_with_state_and_nonce(client, monkeypatch):
    _enable_oidc(monkeypatch)
    _mock_metadata(monkeypatch)
    resp = client.get("/api/v1/auth/oidc/login", follow_redirects=False)
    assert resp.status_code == 302, resp.text
    url = urlparse(resp.headers["location"])
    assert f"{url.scheme}://{url.netloc}{url.path}" == _META["authorization_endpoint"]
    params = parse_qs(url.query)
    assert params["response_type"] == ["code"]
    assert params["client_id"] == ["console-client"]
    assert params["redirect_uri"] == [
        "https://console.example.com/api/v1/auth/oidc/callback"
    ]
    assert "openid" in params["scope"][0]
    state, nonce = params["state"][0], params["nonce"][0]
    assert state and nonce
    # state is stored server-side and yields exactly this nonce once
    assert oidc_mod.pop_state(state) == nonce


def test_oidc_login_502_when_metadata_unavailable(client, monkeypatch):
    _enable_oidc(monkeypatch)

    def _boom(settings):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(oidc_mod, "fetch_metadata", _boom)
    resp = client.get("/api/v1/auth/oidc/login", follow_redirects=False)
    assert resp.status_code == 502


# ---------------------------------------------------------------------------
# /api/v1/auth/oidc/callback
# ---------------------------------------------------------------------------


def test_callback_404_when_disabled(client):
    resp = client.get(
        "/api/v1/auth/oidc/callback",
        params={"code": "x", "state": "y"},
        follow_redirects=False,
    )
    assert resp.status_code == 404


def test_callback_rejects_missing_params(client, monkeypatch):
    _enable_oidc(monkeypatch)
    resp = client.get("/api/v1/auth/oidc/callback", follow_redirects=False)
    assert resp.status_code == 400


def test_callback_rejects_unknown_state(client, monkeypatch):
    _enable_oidc(monkeypatch)
    resp = client.get(
        "/api/v1/auth/oidc/callback",
        params={"code": "abc", "state": "never-issued"},
        follow_redirects=False,
    )
    assert resp.status_code == 400


def test_callback_rejects_nonce_mismatch(client, monkeypatch):
    _enable_oidc(monkeypatch)
    _mock_metadata(monkeypatch)
    _mock_exchange(
        monkeypatch, {"iss": _ISSUER, "aud": "console-client", "nonce": "wrong"}
    )
    state, _nonce = _start_flow(client)
    resp = client.get(
        "/api/v1/auth/oidc/callback",
        params={"code": "abc", "state": state},
        follow_redirects=False,
    )
    assert resp.status_code == 400
    assert "nonce" in resp.json()["detail"].lower()


def test_callback_state_is_single_use(client, monkeypatch):
    _enable_oidc(monkeypatch)
    _mock_metadata(monkeypatch)
    email = f"sso-replay-{uuid.uuid4().hex[:8]}@example.com"
    state, nonce, claims = _finish_flow(client, email)
    _mock_exchange(monkeypatch, claims)
    params = {"code": "abc", "state": state}
    first = client.get(
        "/api/v1/auth/oidc/callback", params=params, follow_redirects=False
    )
    assert first.status_code == 302
    replay = client.get(
        "/api/v1/auth/oidc/callback", params=params, follow_redirects=False
    )
    assert replay.status_code == 400


def test_callback_provisions_user_without_memberships(client, monkeypatch):
    """No default tenant configured → new user sees nothing until an admin adds them."""
    _enable_oidc(monkeypatch)
    _mock_metadata(monkeypatch)
    email = f"sso-new-{uuid.uuid4().hex[:8]}@example.com"
    _state, _nonce, claims = _finish_flow(client, email)
    _mock_exchange(monkeypatch, claims)

    resp = client.get(
        "/api/v1/auth/oidc/callback",
        params={"code": "abc", "state": _state},
        follow_redirects=False,
    )
    assert resp.status_code == 302, resp.text
    location = resp.headers["location"]
    assert location.startswith("/ui#token=")
    token = location.split("#token=", 1)[1]
    assert token

    who = client.get(
        "/api/v1/auth/whoami", headers={"Authorization": f"Bearer {token}"}
    ).json()
    assert who["key_name"] == email
    assert who["auth_method"] == "session"
    assert who["platform_admin"] is False
    assert who["tenants"] == []


def test_callback_provisions_user_with_default_tenant_and_role(client, monkeypatch):
    _enable_oidc(
        monkeypatch, oidc_default_tenant="sso-tenant", oidc_default_role="operator"
    )
    _mock_metadata(monkeypatch)
    email = f"sso-tenant-{uuid.uuid4().hex[:8]}@example.com"
    state, _nonce, claims = _finish_flow(client, email)
    _mock_exchange(monkeypatch, claims)

    resp = client.get(
        "/api/v1/auth/oidc/callback",
        params={"code": "abc", "state": state},
        follow_redirects=False,
    )
    assert resp.status_code == 302, resp.text
    token = resp.headers["location"].split("#token=", 1)[1]
    who = client.get(
        "/api/v1/auth/whoami", headers={"Authorization": f"Bearer {token}"}
    ).json()
    tenants = {t["name"]: t["role"] for t in who["tenants"]}
    assert tenants == {"sso-tenant": "operator"}


def test_callback_reuses_existing_local_account(client, admin_headers, monkeypatch):
    _enable_oidc(monkeypatch)
    _mock_metadata(monkeypatch)
    email = f"sso-existing-{uuid.uuid4().hex[:8]}@example.com"
    created = client.post(
        "/api/v1/users",
        headers=admin_headers,
        json={"username": email, "password": "pw-local"},
    )
    assert created.status_code == 201, created.text
    user_id = created.json()["id"]

    state, _nonce, claims = _finish_flow(client, email)
    _mock_exchange(monkeypatch, claims)
    resp = client.get(
        "/api/v1/auth/oidc/callback",
        params={"code": "abc", "state": state},
        follow_redirects=False,
    )
    assert resp.status_code == 302, resp.text
    token = resp.headers["location"].split("#token=", 1)[1]
    who = client.get(
        "/api/v1/auth/whoami", headers={"Authorization": f"Bearer {token}"}
    ).json()
    assert who["user_id"] == user_id


def test_callback_fails_closed_when_exchange_fails(client, monkeypatch):
    _enable_oidc(monkeypatch)
    _mock_metadata(monkeypatch)

    def _boom(settings, code):
        raise RuntimeError("token endpoint exploded")

    monkeypatch.setattr(oidc_mod, "exchange_code", _boom)
    state, _nonce = _start_flow(client)
    resp = client.get(
        "/api/v1/auth/oidc/callback",
        params={"code": "abc", "state": state},
        follow_redirects=False,
    )
    assert resp.status_code == 502


# ---------------------------------------------------------------------------
# Local login remains the fallback alongside OIDC
# ---------------------------------------------------------------------------


def test_local_login_still_works_with_oidc_enabled(client, admin_headers, monkeypatch):
    _enable_oidc(monkeypatch)
    username = f"local-{uuid.uuid4().hex[:8]}"
    resp = client.post(
        "/api/v1/users",
        headers=admin_headers,
        json={"username": username, "password": "pw-local"},
    )
    assert resp.status_code == 201, resp.text
    login = client.post(
        "/api/v1/auth/login", json={"username": username, "password": "pw-local"}
    )
    assert login.status_code == 200, login.text
    token = login.json()["token"]
    who = client.get(
        "/api/v1/auth/whoami", headers={"Authorization": f"Bearer {token}"}
    )
    assert who.status_code == 200
    assert who.json()["key_name"] == username


def test_oidc_provisioned_user_cannot_use_local_password_login(client, monkeypatch):
    """Auto-provisioned users have no password hash → local login always fails."""
    _enable_oidc(monkeypatch)
    _mock_metadata(monkeypatch)
    email = f"sso-nopw-{uuid.uuid4().hex[:8]}@example.com"
    state, _nonce, claims = _finish_flow(client, email)
    _mock_exchange(monkeypatch, claims)
    resp = client.get(
        "/api/v1/auth/oidc/callback",
        params={"code": "abc", "state": state},
        follow_redirects=False,
    )
    assert resp.status_code == 302, resp.text
    login = client.post(
        "/api/v1/auth/login", json={"username": email, "password": "anything"}
    )
    assert login.status_code == 401


def test_hosted_mode_portal_issuer(client, admin_headers, monkeypatch):
    """Hosted mode: portal issuer (app.genestack.dev) provisions users with gsc_tenant claims."""
    # Create the tenant that the portal will reference in claims
    tenant_resp = client.post(
        "/api/v1/tenants",
        headers=admin_headers,
        json={"name": "tenant-abc", "description": "Portal-provisioned tenant"},
    )
    assert tenant_resp.status_code == 201, tenant_resp.text

    _enable_oidc(
        monkeypatch,
        oidc_issuer_url="https://app.genestack.dev",
        oidc_client_id="genestack-console",
        oidc_default_tenant="",  # no fallback; claims required
    )
    _mock_metadata(monkeypatch)
    email = f"hosted-{uuid.uuid4().hex[:8]}@example.com"
    state, nonce, claims = _finish_flow(client, email)
    claims["iss"] = "https://app.genestack.dev"
    claims["aud"] = "genestack-console"
    claims["gsc_tenant_id"] = tenant_resp.json()["id"]
    claims["gsc_tenant_role"] = "operator"
    _mock_exchange(monkeypatch, claims)

    resp = client.get(
        "/api/v1/auth/oidc/callback",
        params={"code": "abc", "state": state},
        follow_redirects=False,
    )
    assert resp.status_code == 302, resp.text
    token = resp.headers["location"].split("#token=", 1)[1]
    who = client.get(
        "/api/v1/auth/whoami", headers={"Authorization": f"Bearer {token}"}
    ).json()
    assert who["key_name"] == email
    tenants = {t["name"]: t["role"] for t in who["tenants"]}
    assert "tenant-abc" in tenants
    assert tenants["tenant-abc"] == "operator"


def test_hosted_mode_portal_callback_redirect_enables_native_cookies(
    client, admin_headers, monkeypatch
):
    """Hosted mode: portal callback redirect_uri enables gsc_console (native=1)."""
    from app.services import oidc as oidc_mod

    _enable_oidc(
        monkeypatch,
        oidc_issuer_url="https://app.genestack.dev",
        oidc_client_id="genestack-console",
        oidc_redirect_url="https://app.genestack.dev/api/v1/auth/oidc/callback",
    )
    assert oidc_mod.native_cookie_origin(get_settings()) == "https://app.genestack.dev"
    _mock_metadata(monkeypatch)
    resp = client.get(
        "/api/v1/auth/oidc/login", params={"native": 1}, follow_redirects=False
    )
    assert resp.status_code == 302
    url = urlparse(resp.headers["location"])
    params = parse_qs(url.query)
    assert params["redirect_uri"] == [
        "https://app.genestack.dev/api/v1/auth/oidc/callback"
    ]
    assert params["client_id"] == ["genestack-console"]


def test_self_hosted_password_login_always_available(client, admin_headers):
    """Self-hosted mode: password login always works, even without OIDC configured."""
    username = f"self-hosted-{uuid.uuid4().hex[:8]}"
    resp = client.post(
        "/api/v1/users",
        headers=admin_headers,
        json={"username": username, "password": "local-pw"},
    )
    assert resp.status_code == 201, resp.text
    login = client.post(
        "/api/v1/auth/login", json={"username": username, "password": "local-pw"}
    )
    assert login.status_code == 200, login.text
    token = login.json()["token"]
    who = client.get(
        "/api/v1/auth/whoami", headers={"Authorization": f"Bearer {token}"}
    )
    assert who.status_code == 200
    assert who.json()["key_name"] == username


@pytest.fixture(autouse=True)
def _clean_oidc_state():
    yield
    oidc_mod._states.clear()


# ---------------------------------------------------------------------------
# OIDC gsc_tenant_id and gsc_tenant_role claims
# ---------------------------------------------------------------------------


def test_provision_user_honors_gsc_tenant_id_claim(client, monkeypatch):
    """provision_user honors gsc_tenant_id and gsc_tenant_role OIDC claims."""
    from app.db import SessionLocal
    from app.models import Tenant

    _enable_oidc(monkeypatch)
    db = SessionLocal()
    try:
        tenant = Tenant(name="oidc-claim-tenant", description="OIDC Claim Test")
        db.add(tenant)
        db.commit()
        db.refresh(tenant)
        tenant_id = tenant.id
    finally:
        db.close()

    _mock_metadata(monkeypatch)
    email = f"claim-user-{uuid.uuid4().hex[:8]}@example.com"
    state, _nonce, claims = _finish_flow(client, email)
    claims["gsc_tenant_id"] = tenant_id
    claims["gsc_tenant_role"] = "operator"
    _mock_exchange(monkeypatch, claims)

    resp = client.get(
        "/api/v1/auth/oidc/callback",
        params={"code": "abc", "state": state},
        follow_redirects=False,
    )
    assert resp.status_code == 302, resp.text
    token = resp.headers["location"].split("#token=", 1)[1]
    who = client.get(
        "/api/v1/auth/whoami", headers={"Authorization": f"Bearer {token}"}
    ).json()
    tenants = {t["name"]: t["role"] for t in who["tenants"]}
    assert tenants == {"oidc-claim-tenant": "operator"}


def test_provision_user_gsc_tenant_id_fallback_to_default(client, monkeypatch):
    """If gsc_tenant_id is missing, provision_user falls back to oidc_default_tenant."""
    _enable_oidc(
        monkeypatch, oidc_default_tenant="oidc-fallback", oidc_default_role="admin"
    )
    _mock_metadata(monkeypatch)
    email = f"fallback-user-{uuid.uuid4().hex[:8]}@example.com"
    state, _nonce, claims = _finish_flow(client, email)
    _mock_exchange(monkeypatch, claims)

    resp = client.get(
        "/api/v1/auth/oidc/callback",
        params={"code": "abc", "state": state},
        follow_redirects=False,
    )
    assert resp.status_code == 302, resp.text
    token = resp.headers["location"].split("#token=", 1)[1]
    who = client.get(
        "/api/v1/auth/whoami", headers={"Authorization": f"Bearer {token}"}
    ).json()
    tenants = {t["name"]: t["role"] for t in who["tenants"]}
    assert tenants == {"oidc-fallback": "admin"}


def test_provision_user_gsc_role_defaults_to_viewer(client, monkeypatch):
    """If gsc_tenant_role is invalid or missing, defaults to viewer."""
    from app.db import SessionLocal
    from app.models import Tenant

    _enable_oidc(monkeypatch)
    db = SessionLocal()
    try:
        tenant = Tenant(name="oidc-role-default", description="Role Default Test")
        db.add(tenant)
        db.commit()
        db.refresh(tenant)
        tenant_id = tenant.id
    finally:
        db.close()

    _mock_metadata(monkeypatch)
    email = f"role-default-{uuid.uuid4().hex[:8]}@example.com"
    state, _nonce, claims = _finish_flow(client, email)
    claims["gsc_tenant_id"] = tenant_id
    claims["gsc_tenant_role"] = "invalid_role"
    _mock_exchange(monkeypatch, claims)

    resp = client.get(
        "/api/v1/auth/oidc/callback",
        params={"code": "abc", "state": state},
        follow_redirects=False,
    )
    assert resp.status_code == 302, resp.text
    token = resp.headers["location"].split("#token=", 1)[1]
    who = client.get(
        "/api/v1/auth/whoami", headers={"Authorization": f"Bearer {token}"}
    ).json()
    tenants = {t["name"]: t["role"] for t in who["tenants"]}
    assert tenants == {"oidc-role-default": "viewer"}
