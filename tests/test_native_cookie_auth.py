"""Hosted native OIDC cookies; hermetic tests never contact an identity provider."""

from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit
import uuid

import pytest

from app.config import get_settings
from app.db import SessionLocal
from app.models import User
from app.services import accounts, oidc

ORIGIN = "https://app.genestack.dev"


@pytest.fixture
def hosted(monkeypatch):
    settings = get_settings()
    for name, value in {
        "oidc_enabled": True,
        "oidc_issuer_url": ORIGIN,  # must match portal iss for gsc_console
        "oidc_redirect_url": ORIGIN + "/api/v1/auth/oidc/callback",
        "oidc_client_id": "genestack-console",
        "oidc_default_tenant": "",
        "dev_auto_login": False,
    }.items():
        monkeypatch.setattr(settings, name, value)
    return settings


@pytest.fixture
def cookie_session():
    with SessionLocal() as db:
        user = User(
            username="native-" + uuid.uuid4().hex,
            password_hash=None,
            platform_admin=True,
            active=True,
        )
        db.add(user)
        db.flush()
        session = accounts.create_session(db, user)
        token = session.token
        db.commit()
    return token


def headers(token, origin=None):
    result = {"Cookie": "gsc_console=" + token}
    if origin is not None:
        result["Origin"] = origin
    return result


def test_cookie_reads_and_ticket_require_trusted_origin(client, hosted, cookie_session):
    assert (
        client.get("/api/v1/auth/whoami", headers=headers(cookie_session)).status_code
        == 200
    )
    for origin in (
        None,
        "null",
        "https://evil.example",
        ORIGIN + "/",
        "https://app.genestack.dev.evil.example",
    ):
        response = client.post(
            "/api/v1/auth/ticket", headers=headers(cookie_session, origin)
        )
        assert response.status_code == 403
    assert (
        client.post(
            "/api/v1/auth/ticket", headers=headers(cookie_session, ORIGIN)
        ).status_code
        == 201
    )


def test_host_and_forwarded_headers_cannot_override_config(
    client, hosted, cookie_session
):
    forged = headers(cookie_session, "https://evil.example")
    forged.update(
        {
            "Host": "evil.example",
            "X-Forwarded-Host": "evil.example",
            "X-Forwarded-Proto": "https",
        }
    )
    assert client.post("/api/v1/auth/ticket", headers=forged).status_code == 403
    duplicated = list(headers(cookie_session, ORIGIN).items()) + [
        ("Origin", "https://evil.example")
    ]
    assert client.post("/api/v1/auth/ticket", headers=duplicated).status_code == 403


@pytest.mark.parametrize(
    "redirect",
    [
        "http://app.genestack.dev/api/v1/auth/oidc/callback",
        "https://other.example/api/v1/auth/oidc/callback",
        "https://app.genestack.dev:444/api/v1/auth/oidc/callback",
        "https://user@app.genestack.dev/api/v1/auth/oidc/callback",
        ORIGIN + "/wrong-path",
        ORIGIN + "/api/v1/auth/oidc/callback?untrusted=1",
    ],
)
def test_hosted_config_is_required(
    client, hosted, cookie_session, monkeypatch, redirect
):
    monkeypatch.setattr(hosted, "oidc_redirect_url", redirect)
    assert (
        client.get("/api/v1/auth/whoami", headers=headers(cookie_session)).status_code
        == 401
    )
    assert (
        client.get(
            "/api/v1/auth/oidc/login?native=1", follow_redirects=False
        ).status_code
        == 404
    )


def test_my_genestack_dev_is_a_hosted_cookie_origin(hosted, monkeypatch):
    """The current door is my.genestack.dev. app.genestack.dev stays valid on its own."""
    monkeypatch.setattr(hosted, "oidc_issuer_url", "https://my.genestack.dev")
    monkeypatch.setattr(
        hosted,
        "oidc_redirect_url",
        "https://my.genestack.dev/api/v1/auth/oidc/callback",
    )
    assert oidc.native_cookie_origin(hosted) == "https://my.genestack.dev"
    monkeypatch.setattr(
        hosted,
        "oidc_redirect_url",
        "https://app.genestack.dev/api/v1/auth/oidc/callback",
    )
    assert oidc.native_cookie_origin(hosted) is None


def test_hosted_issuer_must_be_portal(client, hosted, cookie_session, monkeypatch):
    """Wrong iss disables gsc_console even when redirect_uri is correct."""
    monkeypatch.setattr(hosted, "oidc_issuer_url", "https://idp.example")
    assert (
        client.get("/api/v1/auth/whoami", headers=headers(cookie_session)).status_code
        == 401
    )
    assert (
        client.get(
            "/api/v1/auth/oidc/login?native=1", follow_redirects=False
        ).status_code
        == 404
    )


def test_disabled_oidc_rejects_cookies_but_keeps_headers(
    client, hosted, cookie_session, monkeypatch, admin_headers
):
    monkeypatch.setattr(hosted, "oidc_enabled", False)
    assert (
        client.get("/api/v1/auth/whoami", headers=headers(cookie_session)).status_code
        == 401
    )
    assert client.get("/api/v1/auth/whoami", headers=admin_headers).status_code == 200


@pytest.mark.parametrize(
    "token", ["", "short", "x" * 200, "bad token", "dev-admin-key"]
)
def test_malformed_cookie_never_authenticates(client, hosted, token):
    assert client.get("/api/v1/auth/whoami", headers=headers(token)).status_code == 401


def test_duplicate_cookies_rejected(client, hosted, cookie_session):
    h = {"Cookie": "gsc_console=" + cookie_session + "; gsc_console=" + cookie_session}
    assert client.get("/api/v1/auth/whoami", headers=h).status_code == 401


def test_expired_and_disabled_sessions_rejected(client, hosted, cookie_session):
    from app.models import SessionToken

    with SessionLocal() as db:
        session = db.get(SessionToken, cookie_session)
        session.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()
    assert (
        client.get("/api/v1/auth/whoami", headers=headers(cookie_session)).status_code
        == 401
    )


def test_logout_checks_origin_revokes_and_clears_cookie(client, hosted, cookie_session):
    assert (
        client.post("/api/v1/auth/logout", headers=headers(cookie_session)).status_code
        == 403
    )
    assert (
        client.get("/api/v1/auth/whoami", headers=headers(cookie_session)).status_code
        == 200
    )
    response = client.post(
        "/api/v1/auth/logout", headers=headers(cookie_session, ORIGIN)
    )
    assert response.status_code == 200
    assert "Max-Age=0" in response.headers["set-cookie"]
    assert (
        "Secure" in response.headers["set-cookie"]
        and "HttpOnly" in response.headers["set-cookie"]
    )
    assert (
        client.get("/api/v1/auth/whoami", headers=headers(cookie_session)).status_code
        == 401
    )


def flow(client, monkeypatch, native):
    monkeypatch.setattr(
        oidc,
        "fetch_metadata",
        lambda settings: {"authorization_endpoint": "https://idp.example/authorize"},
    )
    response = client.get(
        "/api/v1/auth/oidc/login",
        params={"native": int(native)},
        follow_redirects=False,
    )
    assert response.status_code == 302
    params = parse_qs(urlsplit(response.headers["location"]).query)
    monkeypatch.setattr(
        oidc,
        "exchange_code",
        lambda settings, code: {
            "nonce": params["nonce"][0],
            "email": "oidc-" + uuid.uuid4().hex + "@example.test",
        },
    )
    return params["state"][0]


def test_native_oidc_cookie_authenticates_whoami_without_api_key(
    client, hosted, monkeypatch
):
    """Mac selectDeployer path: native OIDC sets gsc_console; whoami needs no X-API-Key."""
    state = flow(client, monkeypatch, True)
    response = client.get(
        "/api/v1/auth/oidc/callback",
        params={"state": state, "code": "test-code"},
        follow_redirects=False,
    )
    assert response.status_code == 302 and response.headers["location"] == "/ui"
    # Pull the session cookie the callback set (no Authorization / X-API-Key).
    token = response.cookies.get(oidc.NATIVE_SESSION_COOKIE)
    assert token and len(token) == 43
    client.cookies.clear()
    who = client.get("/api/v1/auth/whoami", headers=headers(token))
    assert who.status_code == 200
    body = who.json()
    assert body["auth_method"] == "cookie"
    assert body["key_name"]
    # Confirm headers-only still works and cookie path does not require API keys.
    assert "X-API-Key" not in headers(token)


def test_native_oidc_cookie_attributes_and_replay(client, hosted, monkeypatch):
    state = flow(client, monkeypatch, True)
    response = client.get(
        "/api/v1/auth/oidc/callback",
        params={"state": state, "code": "test-code", "native": "0"},
        follow_redirects=False,
    )
    assert response.status_code == 302 and response.headers["location"] == "/ui"
    cookie = response.headers["set-cookie"]
    assert (
        "Secure" in cookie
        and "HttpOnly" in cookie
        and "SameSite=lax" in cookie
        and "Path=/" in cookie
    )
    assert "Domain=" not in cookie
    assert response.headers["cache-control"] == "no-store"
    assert (
        client.get(
            "/api/v1/auth/oidc/callback",
            params={"state": state, "code": "test-code"},
            follow_redirects=False,
        ).status_code
        == 400
    )
    client.cookies.clear()


def test_callback_query_cannot_upgrade_normal_web_mode(client, hosted, monkeypatch):
    state = flow(client, monkeypatch, False)
    response = client.get(
        "/api/v1/auth/oidc/callback",
        params={"state": state, "code": "test-code", "native": "1"},
        follow_redirects=False,
    )
    assert response.status_code == 302 and response.headers["location"].startswith(
        "/ui#token="
    )
    assert "set-cookie" not in response.headers


def test_native_state_expiry_and_nonce_failure(client, hosted, monkeypatch):
    state = flow(client, monkeypatch, True)
    with oidc._state_lock:
        oidc._states[state]["created"] -= oidc.STATE_TTL_SECONDS + 1
    assert (
        client.get(
            "/api/v1/auth/oidc/callback", params={"state": state, "code": "test-code"}
        ).status_code
        == 400
    )
    state = flow(client, monkeypatch, True)
    monkeypatch.setattr(
        oidc, "exchange_code", lambda settings, code: {"nonce": "wrong"}
    )
    response = client.get(
        "/api/v1/auth/oidc/callback", params={"state": state, "code": "test-code"}
    )
    assert response.status_code == 400 and "set-cookie" not in response.headers


def test_disabled_user_cookie_is_rejected(client, hosted, cookie_session):
    from app.models import SessionToken

    with SessionLocal() as db:
        session = db.get(SessionToken, cookie_session)
        user = db.get(User, session.user_id)
        user.active = False
        db.commit()
    assert (
        client.get("/api/v1/auth/whoami", headers=headers(cookie_session)).status_code
        == 401
    )


def test_native_callback_rechecks_trusted_configuration(client, hosted, monkeypatch):
    state = flow(client, monkeypatch, True)
    monkeypatch.setattr(
        hosted, "oidc_redirect_url", "https://other.example/api/v1/auth/oidc/callback"
    )
    response = client.get(
        "/api/v1/auth/oidc/callback",
        params={"state": state, "code": "test-code"},
        follow_redirects=False,
    )
    assert response.status_code == 400 and "set-cookie" not in response.headers


def test_cookie_cannot_become_static_admin_key(client, hosted, monkeypatch):
    import app.auth as auth

    token = "x" * 43

    class SettingsProxy:
        def __getattr__(self, key):
            return getattr(hosted, key)

        def parsed_api_keys(self):
            return {token: "admin"}

    monkeypatch.setattr(auth, "get_settings", lambda: SettingsProxy())
    assert client.get("/api/v1/auth/whoami", headers=headers(token)).status_code == 401
    assert (
        client.get("/api/v1/auth/whoami", headers={"X-API-Key": token}).status_code
        == 200
    )
