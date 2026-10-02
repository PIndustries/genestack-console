"""OAuth 2 token, authorization code, refresh, revoke, and discovery."""

from __future__ import annotations

import base64
import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

from app.db import SessionLocal
from app.models import SessionToken


def _create_user(client, admin_headers, username, password):
    resp = client.post(
        "/api/v1/users",
        headers=admin_headers,
        json={"username": username, "password": password, "platform_admin": False},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _password_grant(client, username, password):
    resp = client.post(
        "/api/v1/oauth/token",
        data={
            "grant_type": "password",
            "username": username,
            "password": password,
            "client_id": "genestack-console",
            "scope": "console",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["token_type"] == "Bearer"
    assert body["access_token"]
    assert body["refresh_token"]
    assert body["expires_in"] > 0
    assert body["scope"] == "console"
    assert resp.headers["cache-control"] == "no-store"
    return body


def _pkce():
    verifier = secrets.token_urlsafe(32)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


def _whoami(client, token):
    return client.get(
        "/api/v1/auth/whoami",
        headers={"Authorization": f"Bearer {token}"},
    )


def test_password_grant_and_refresh_keep_two_logins(client, admin_headers):
    username = f"oauth-{uuid.uuid4().hex[:8]}"
    _create_user(client, admin_headers, username, "pw-oauth")
    first = _password_grant(client, username, "pw-oauth")
    second = _password_grant(client, username, "pw-oauth")
    assert first["access_token"] != second["access_token"]
    assert first["refresh_token"] != second["refresh_token"]
    assert _whoami(client, first["access_token"]).status_code == 200
    assert _whoami(client, second["access_token"]).status_code == 200

    refreshed = client.post(
        "/api/v1/oauth/token",
        data={"grant_type": "refresh_token", "refresh_token": first["refresh_token"]},
    )
    assert refreshed.status_code == 200, refreshed.text
    renewed = refreshed.json()
    assert renewed["access_token"] != first["access_token"]
    assert renewed["refresh_token"] != first["refresh_token"]
    assert _whoami(client, first["access_token"]).status_code == 401
    assert _whoami(client, renewed["access_token"]).status_code == 200
    assert _whoami(client, second["access_token"]).status_code == 200

    replay = client.post(
        "/api/v1/oauth/token",
        data={"grant_type": "refresh_token", "refresh_token": first["refresh_token"]},
    )
    assert replay.status_code == 400
    assert replay.json()["error"] == "invalid_grant"
    assert _whoami(client, renewed["access_token"]).status_code == 200
    assert _whoami(client, second["access_token"]).status_code == 200


def test_password_grant_accepts_json(client, admin_headers):
    username = f"oauth-json-{uuid.uuid4().hex[:8]}"
    _create_user(client, admin_headers, username, "pw-json")
    resp = client.post(
        "/api/v1/oauth/token",
        json={"grant_type": "password", "username": username, "password": "pw-json"},
    )
    assert resp.status_code == 200, resp.text
    assert _whoami(client, resp.json()["access_token"]).status_code == 200


def test_password_grant_rejects_a_client_secret(client, admin_headers):
    username = f"oauth-secret-{uuid.uuid4().hex[:8]}"
    _create_user(client, admin_headers, username, "pw-secret")
    issued = _password_grant(client, username, "pw-secret")
    resp = client.post(
        "/api/v1/oauth/token",
        data={
            "grant_type": "password",
            "username": username,
            "password": "pw-secret",
            "client_secret": "nope",
        },
    )
    assert resp.status_code == 401
    assert resp.json()["error"] == "invalid_client"
    assert _whoami(client, issued["access_token"]).status_code == 200


def test_refresh_works_after_the_access_token_expires(client, admin_headers):
    username = f"oauth-exp-{uuid.uuid4().hex[:8]}"
    _create_user(client, admin_headers, username, "pw-exp")
    issued = _password_grant(client, username, "pw-exp")
    with SessionLocal() as db:
        row = db.get(SessionToken, issued["access_token"])
        row.expires_at = datetime.now(timezone.utc) - timedelta(minutes=5)
        db.commit()
    assert _whoami(client, issued["access_token"]).status_code == 401
    resp = client.post(
        "/api/v1/oauth/token",
        data={"grant_type": "refresh_token", "refresh_token": issued["refresh_token"]},
    )
    assert resp.status_code == 200, resp.text
    assert _whoami(client, resp.json()["access_token"]).status_code == 200


def test_unsupported_grant_and_unknown_scope(client):
    resp = client.post("/api/v1/oauth/token", data={"grant_type": "client_credentials"})
    assert resp.status_code == 400
    assert resp.json()["error"] == "unsupported_grant_type"
    resp = client.post(
        "/api/v1/oauth/token",
        data={"grant_type": "password", "username": "a", "password": "b", "scope": "admin"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_scope"


def test_authorization_code_pkce_then_reuse_fails(client, admin_headers):
    username = f"oauth-code-{uuid.uuid4().hex[:8]}"
    _create_user(client, admin_headers, username, "pw-code")
    other = _password_grant(client, username, "pw-code")
    verifier, challenge = _pkce()
    redirect_uri = "http://127.0.0.1:53682/callback"
    started = client.post(
        "/api/v1/oauth/authorize",
        data={
            "response_type": "code",
            "client_id": "genestack-console",
            "redirect_uri": redirect_uri,
            "state": "xyz",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "scope": "console",
            "username": username,
            "password": "pw-code",
        },
        follow_redirects=False,
    )
    assert started.status_code == 302, started.text
    location = started.headers["location"]
    parsed = urlsplit(location)
    query = parse_qs(parsed.query)
    assert query["state"] == ["xyz"]
    code = query["code"][0]

    exchanged = client.post(
        "/api/v1/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": "genestack-console",
            "code_verifier": verifier,
        },
    )
    assert exchanged.status_code == 200, exchanged.text
    access = exchanged.json()["access_token"]
    assert _whoami(client, access).status_code == 200
    assert _whoami(client, other["access_token"]).status_code == 200

    again = client.post(
        "/api/v1/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": "genestack-console",
            "code_verifier": verifier,
        },
    )
    assert again.status_code == 400
    assert again.json()["error"] == "invalid_grant"
    assert _whoami(client, access).status_code == 200


def test_wrong_pkce_verifier_consumes_the_code(client, admin_headers):
    username = f"oauth-pkce-{uuid.uuid4().hex[:8]}"
    _create_user(client, admin_headers, username, "pw-pkce")
    verifier, challenge = _pkce()
    redirect_uri = "http://127.0.0.1:53682/callback"
    started = client.post(
        "/api/v1/oauth/authorize",
        data={
            "response_type": "code",
            "client_id": "genestack-console",
            "redirect_uri": redirect_uri,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "username": username,
            "password": "pw-pkce",
        },
        follow_redirects=False,
    )
    code = parse_qs(urlsplit(started.headers["location"]).query)["code"][0]
    bad = client.post(
        "/api/v1/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": "genestack-console",
            "code_verifier": "a" * 50,
        },
    )
    assert bad.status_code == 400
    retry = client.post(
        "/api/v1/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": "genestack-console",
            "code_verifier": verifier,
        },
    )
    assert retry.status_code == 400


def test_authorize_refuses_an_open_redirect(client):
    _verifier, challenge = _pkce()
    resp = client.get(
        "/api/v1/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": "genestack-console",
            "redirect_uri": "https://evil.example/steal",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        },
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_request"
    assert "location" not in {key.lower() for key in resp.headers}


def test_implicit_grant_is_refused(client):
    _verifier, challenge = _pkce()
    resp = client.get(
        "/api/v1/oauth/authorize",
        params={
            "response_type": "token",
            "client_id": "genestack-console",
            "redirect_uri": "http://127.0.0.1:53682/callback",
            "state": "keep",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302
    query = parse_qs(urlsplit(resp.headers["location"]).query)
    assert query["error"] == ["unsupported_response_type"]
    assert query["state"] == ["keep"]
    assert "code" not in query


def test_same_host_ui_redirect_is_allowed(client, admin_headers):
    username = f"oauth-ui-{uuid.uuid4().hex[:8]}"
    _create_user(client, admin_headers, username, "pw-ui")
    verifier, challenge = _pkce()
    started = client.post(
        "/api/v1/oauth/authorize",
        data={
            "response_type": "code",
            "client_id": "genestack-console",
            "redirect_uri": "http://testserver/ui",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "username": username,
            "password": "pw-ui",
        },
        follow_redirects=False,
    )
    assert started.status_code == 302
    location = urlsplit(started.headers["location"])
    assert location.netloc == "testserver"
    assert location.path == "/ui"
    code = parse_qs(location.query)["code"][0]
    exchanged = client.post(
        "/api/v1/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "http://testserver/ui",
            "client_id": "genestack-console",
            "code_verifier": verifier,
        },
    )
    assert exchanged.status_code == 200, exchanged.text


def test_revoke_drops_one_login(client, admin_headers):
    username = f"oauth-revoke-{uuid.uuid4().hex[:8]}"
    _create_user(client, admin_headers, username, "pw-revoke")
    first = _password_grant(client, username, "pw-revoke")
    second = _password_grant(client, username, "pw-revoke")
    gone = client.post(
        "/api/v1/oauth/revoke",
        data={"token": first["refresh_token"], "token_type_hint": "refresh_token"},
    )
    assert gone.status_code == 200
    assert _whoami(client, first["access_token"]).status_code == 401
    assert _whoami(client, second["access_token"]).status_code == 200
    unknown = client.post("/api/v1/oauth/revoke", data={"token": "not-a-token"})
    assert unknown.status_code == 200
    assert _whoami(client, second["access_token"]).status_code == 200


def test_introspect_hides_another_users_token(client, admin_headers):
    left_name = f"oauth-left-{uuid.uuid4().hex[:8]}"
    right_name = f"oauth-right-{uuid.uuid4().hex[:8]}"
    _create_user(client, admin_headers, left_name, "pw-left")
    _create_user(client, admin_headers, right_name, "pw-right")
    left = _password_grant(client, left_name, "pw-left")
    right = _password_grant(client, right_name, "pw-right")

    own = client.post(
        "/api/v1/oauth/introspect",
        headers={"Authorization": f"Bearer {left['access_token']}"},
        data={"token": left["access_token"]},
    )
    assert own.status_code == 200, own.text
    assert own.json()["active"] is True
    assert own.json()["username"] == left_name

    other = client.post(
        "/api/v1/oauth/introspect",
        headers={"Authorization": f"Bearer {left['access_token']}"},
        data={"token": right["access_token"]},
    )
    assert other.status_code == 200
    assert other.json() == {"active": False}

    admin = client.post(
        "/api/v1/oauth/introspect",
        headers=admin_headers,
        data={"token": right["refresh_token"], "token_type_hint": "refresh_token"},
    )
    assert admin.status_code == 200, admin.text
    assert admin.json()["active"] is True
    assert admin.json()["username"] == right_name


def test_discovery_document(client):
    resp = client.get("/.well-known/oauth-authorization-server")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["grant_types_supported"] == [
        "authorization_code",
        "password",
        "refresh_token",
    ]
    assert body["code_challenge_methods_supported"] == ["S256"]
    assert body["token_endpoint"].endswith("/api/v1/oauth/token")
    assert body["authorization_endpoint"].endswith("/api/v1/oauth/authorize")
    assert body["revocation_endpoint"].endswith("/api/v1/oauth/revoke")
    assert "implicit" not in body["grant_types_supported"]
