"""In-portal noVNC session store, HTML rewrite, and kube proxy helpers."""

from __future__ import annotations

import base64
import ssl
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth import resolve_principal
from app.db import SessionLocal
from app.models import Environment
from app.routers import novnc as novnc_router
from app.schemas import Principal
from app.services import novnc as novnc_svc

FAKE_CA = base64.b64encode(b"not-a-real-ca").decode()
SERVER_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

ENCODED_NOVA_URL = (
    "https://novnc.cluster.local/vnc_lite.html?path=%2Fwebsockify%3Ftoken%3Dabc123"
)
DIRECT_NOVA_URL = "http://x/vnc_auto.html?token=abc"


@pytest.fixture(autouse=True)
def _clean_sessions():
    yield
    novnc_svc._reset()


def _kubeconfig(
    path: Path, *, token: str = "k8s-token", with_certs: bool = False
) -> Path:
    cluster: dict[str, Any] = {
        "server": "https://kube.example:6443",
        "insecure-skip-tls-verify": True,
    }
    user: dict[str, Any] = {"token": token}
    if with_certs:
        cluster["certificate-authority-data"] = FAKE_CA
        del cluster["insecure-skip-tls-verify"]
        user["client-certificate-data"] = FAKE_CA
        user["client-key-data"] = FAKE_CA
    doc = {
        "apiVersion": "v1",
        "clusters": [{"name": "fake", "cluster": cluster}],
        "users": [{"name": "fake", "user": user}],
        "contexts": [{"name": "fake", "context": {"cluster": "fake", "user": "fake"}}],
        "current-context": "fake",
    }
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return path


def _tiny_app() -> FastAPI:
    app = FastAPI()
    app.include_router(novnc_router.router)
    return app


def _insert_env(kubeconfig_path: str | None = None) -> Environment:
    db = SessionLocal()
    try:
        env = Environment(
            name=f"novnc-{uuid.uuid4().hex[:8]}",
            kubeconfig_path=kubeconfig_path,
        )
        db.add(env)
        db.commit()
        db.refresh(env)
        db.expunge(env)
        return env
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Token parse
# ---------------------------------------------------------------------------


def test_parse_token_from_encoded_path_query():
    assert novnc_svc.parse_nova_token(ENCODED_NOVA_URL) == "abc123"


def test_parse_token_from_direct_query():
    assert novnc_svc.parse_nova_token(DIRECT_NOVA_URL) == "abc"


def test_parse_token_missing_returns_none():
    assert novnc_svc.parse_nova_token("") is None
    assert (
        novnc_svc.parse_nova_token("https://novnc.cluster.local/vnc_lite.html") is None
    )


# ---------------------------------------------------------------------------
# Session store
# ---------------------------------------------------------------------------


def test_session_create_get_expire(monkeypatch):
    sid = novnc_svc.create_session(
        env_id="env-1",
        server_id=SERVER_ID,
        nova_token="abc123",
        username="viewer-1",
    )
    assert sid.startswith("nvc_")
    assert novnc_svc.SESSION_ID_RE.match(sid)
    got = novnc_svc.get_session(sid, "env-1")
    assert got is not None
    assert got.server_id == SERVER_ID
    assert got.nova_token == "abc123"
    assert got.username == "viewer-1"
    assert novnc_svc.get_session(sid, "other-env") is None
    monkeypatch.setattr(novnc_svc, "SESSION_TTL_SECONDS", -1)
    assert novnc_svc.get_session(sid, "env-1") is None


def test_session_unknown_rejected():
    assert novnc_svc.get_session("nvc_nopeeeee", "env-1") is None
    assert novnc_svc.get_session("not-a-session", "env-1") is None


# ---------------------------------------------------------------------------
# Path traversal + HTML rewrite
# ---------------------------------------------------------------------------


def test_path_traversal_rejected():
    assert not novnc_svc.is_safe_console_path("../etc/passwd")
    assert not novnc_svc.is_safe_console_path("/vnc_lite.html")
    assert not novnc_svc.is_safe_console_path("foo/../../secret")
    assert not novnc_svc.is_safe_console_path("vnc_lite.html?token=x")
    assert not novnc_svc.is_safe_console_path("http://evil")
    assert not novnc_svc.is_safe_console_path("core/rfb.js%2e%2e")
    assert novnc_svc.is_safe_console_path("vnc_lite.html")
    assert novnc_svc.is_safe_console_path("app/ui.js")
    assert novnc_svc.is_safe_console_path("")
    client = TestClient(_tiny_app())
    resp = client.get("/api/v1/environments/env-1/cloud/console/nvc_abcd1234/foo/..bar")
    assert resp.status_code == 400


def test_html_rewrite_makes_websocket_path_relative():
    html = """
    <script>
      const path = '/websockify';
      UI.getConfig('path') || '/websockify';
    </script>
    <a href="vnc_lite.html?path=/websockify?token=SECRET">open</a>
    <a href="vnc_lite.html?path=%2Fwebsockify%3Ftoken%3DSECRET">enc</a>
    """
    out = novnc_svc.rewrite_novnc_html(html)
    assert "/websockify" not in out
    assert "%2Fwebsockify" not in out
    assert "websockify" in out
    assert "SECRET" not in out
    assert "token=" not in out


# ---------------------------------------------------------------------------
# HTTP asset proxy via kube service proxy
# ---------------------------------------------------------------------------


class RecordingTransport(httpx.BaseTransport):
    def __init__(
        self,
        status: int = 200,
        body: bytes = b"js",
        content_type: str = "application/javascript",
    ):
        self.calls: list[tuple[str, str]] = []
        self.status = status
        self.body = body
        self.content_type = content_type

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.method, str(request.url)))
        return httpx.Response(
            self.status,
            content=self.body,
            headers={"content-type": self.content_type},
        )


def test_http_asset_proxy_uses_kube_novnc_url(monkeypatch):
    transport = RecordingTransport()

    def fake_load(_path: str):
        client = httpx.Client(transport=transport)
        return client, "https://kube.example:6443", []

    monkeypatch.setattr(novnc_svc, "load_kube_http", fake_load)
    status, body, ctype = novnc_svc.fetch_novnc_asset("/tmp/kube", "core/rfb.js")
    assert status == 200
    assert body == b"js"
    assert "javascript" in ctype
    assert transport.calls
    url = transport.calls[0][1]
    assert "nova-novncproxy" in url
    assert "6080" in url
    assert "/proxy/core/rfb.js" in url
    assert "namespaces/openstack" in url


def test_novnc_proxy_url_shape():
    url = novnc_svc.novnc_proxy_url("https://kube.example:6443", "vnc_lite.html")
    assert "nova-novncproxy" in url
    assert "6080" in url
    assert url.endswith("/proxy/vnc_lite.html")


# ---------------------------------------------------------------------------
# Router: invalid session, session POST, HTML rewrite through GET
# ---------------------------------------------------------------------------


def test_invalid_session_404():
    client = TestClient(_tiny_app())
    resp = client.get(
        "/api/v1/environments/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        "/cloud/console/nvc_deadbeef12/vnc_lite.html"
    )
    assert resp.status_code == 404


def test_create_session_endpoint_ok(monkeypatch):
    env = Environment(id="env-1", name="e")
    app = _tiny_app()
    app.dependency_overrides[novnc_router._console_env] = lambda: env
    app.dependency_overrides[resolve_principal] = lambda: Principal(
        username="viewer-1", role="viewer", auth_method="test"
    )
    monkeypatch.setattr(
        novnc_router.openstack_ops,
        "server_console",
        lambda *_a, **_k: {"url": DIRECT_NOVA_URL, "error": None},
    )
    client = TestClient(app)
    resp = client.post(
        f"/api/v1/environments/env-1/cloud/servers/{SERVER_ID}/console/session"
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["error"] is None
    assert body["server_id"] == SERVER_ID
    assert body["session_id"].startswith("nvc_")
    assert body["embed_url"] == (
        f"/api/v1/environments/env-1/cloud/console/{body['session_id']}/vnc_lite.html"
    )
    stored = novnc_svc.get_session(body["session_id"], "env-1")
    assert stored is not None
    assert stored.nova_token == "abc"


def test_create_session_missing_nova_url(monkeypatch):
    env = Environment(id="env-1", name="e")
    app = _tiny_app()
    app.dependency_overrides[novnc_router._console_env] = lambda: env
    app.dependency_overrides[resolve_principal] = lambda: Principal(
        username="viewer-1", role="viewer", auth_method="test"
    )
    monkeypatch.setattr(
        novnc_router.openstack_ops,
        "server_console",
        lambda *_a, **_k: {"url": None, "error": "nova unreachable"},
    )
    client = TestClient(app)
    resp = client.post(
        f"/api/v1/environments/env-1/cloud/servers/{SERVER_ID}/console/session"
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert body["embed_url"] is None
    assert body["error"]


def test_create_session_invalid_server_id():
    env = Environment(id="env-1", name="e")
    app = _tiny_app()
    app.dependency_overrides[novnc_router._console_env] = lambda: env
    app.dependency_overrides[resolve_principal] = lambda: Principal(
        username="viewer-1", role="viewer", auth_method="test"
    )
    client = TestClient(app)
    resp = client.post("/api/v1/environments/env-1/cloud/servers/bad/console/session")
    assert resp.status_code == 400


def test_html_proxy_rewrites_and_strips_token(tmp_path, monkeypatch):
    kc = _kubeconfig(tmp_path / "kubeconfig")
    env = _insert_env(str(kc))
    sid = novnc_svc.create_session(
        env_id=env.id,
        server_id=SERVER_ID,
        nova_token="SECRETTOKEN",
        username="viewer-1",
    )
    html = (
        b"<html><script>const p='/websockify';</script>"
        b"<a href='?path=/websockify?token=SECRETTOKEN'></a></html>"
    )
    transport = RecordingTransport(body=html, content_type="text/html")

    def fake_load(_path: str):
        return httpx.Client(transport=transport), "https://kube.example:6443", []

    monkeypatch.setattr(novnc_svc, "load_kube_http", fake_load)
    client = TestClient(_tiny_app())
    resp = client.get(
        f"/api/v1/environments/{env.id}/cloud/console/{sid}/vnc_lite.html"
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert "frame-ancestors 'self'" in (
        resp.headers.get("content-security-policy") or ""
    )
    assert resp.headers.get("x-content-type-options") == "nosniff"
    assert "/websockify" not in resp.text
    assert "websockify" in resp.text
    assert "SECRETTOKEN" not in resp.text
    assert "token=" not in resp.text
    assert transport.calls
    assert "nova-novncproxy" in transport.calls[0][1]
    assert "6080" in transport.calls[0][1]


def test_html_proxy_error_page_when_upstream_fails(tmp_path, monkeypatch):
    kc = _kubeconfig(tmp_path / "kubeconfig")
    env = _insert_env(str(kc))
    sid = novnc_svc.create_session(
        env_id=env.id, server_id=SERVER_ID, nova_token="t", username="v"
    )

    def boom(_path: str):
        raise novnc_svc.NovncProxyError("nope")

    monkeypatch.setattr(novnc_svc, "load_kube_http", boom)
    client = TestClient(_tiny_app())
    resp = client.get(
        f"/api/v1/environments/{env.id}/cloud/console/{sid}/vnc_lite.html"
    )
    assert resp.status_code == 200
    assert "nova-novncproxy" in resp.text


def test_kube_ws_target_url_and_bearer(tmp_path):
    path = _kubeconfig(tmp_path / "kubeconfig")
    url, ctx, headers, cleanup = novnc_svc._kube_ws_target(str(path))
    try:
        assert url.startswith("wss://kube.example:6443/")
        assert "nova-novncproxy" in url
        assert "6080" in url
        assert url.endswith("/proxy/websockify")
        assert "token=" not in url
        assert headers.get("Authorization") == "Bearer k8s-token"
        assert isinstance(ctx, ssl.SSLContext)
    finally:
        novnc_svc._cleanup_files(cleanup)


def test_kube_ws_target_uses_load_cert_chain(tmp_path, monkeypatch):
    loaded: dict[str, str] = {}

    class FakeCtx:
        def load_cert_chain(self, certfile, keyfile):
            loaded["cert"] = certfile
            loaded["key"] = keyfile

    monkeypatch.setattr(
        "app.services.novnc.ssl.create_default_context", lambda **_kw: FakeCtx()
    )
    path = _kubeconfig(tmp_path / "admin.conf", with_certs=True)
    url, ctx, _headers, cleanup = novnc_svc._kube_ws_target(str(path))
    try:
        assert isinstance(ctx, FakeCtx)
        assert loaded.get("cert") and loaded.get("key")
        assert "nova-novncproxy" in url
    finally:
        novnc_svc._cleanup_files(cleanup)


def test_local_websockify_url_keeps_token_off_logs():
    url = novnc_svc.local_websockify_url(16080, "abc-token")
    assert url == "ws://127.0.0.1:16080/websockify?token=abc-token"
    assert novnc_svc.redact_token(url, "abc-token") == (
        "ws://127.0.0.1:16080/websockify?token=<redacted>"
    )


def test_ensure_portforward_reuses_matching_forward(monkeypatch, tmp_path):
    kc = _kubeconfig(tmp_path / "kubeconfig")

    class LiveProc:
        def poll(self):
            return None

        def terminate(self):
            return None

    novnc_svc._pf_proc = LiveProc()
    novnc_svc._pf_digest = novnc_svc._kubeconfig_digest(str(kc))
    novnc_svc._pf_port = novnc_svc.NOVNC_LOCAL_PORT
    monkeypatch.setattr(novnc_svc, "_port_open", lambda _port: True)
    called = {"popen": 0}

    def boom(*_a, **_k):
        called["popen"] += 1
        raise AssertionError("should not spawn kubectl")

    monkeypatch.setattr(novnc_svc.subprocess, "Popen", boom)
    monkeypatch.setattr(novnc_svc.shutil, "which", lambda _n: "/usr/bin/kubectl")
    assert novnc_svc.ensure_novnc_portforward(str(kc)) == novnc_svc.NOVNC_LOCAL_PORT
    assert called["popen"] == 0


def test_ensure_portforward_does_not_trust_stray_port(monkeypatch, tmp_path):
    kc = _kubeconfig(tmp_path / "kubeconfig")
    monkeypatch.setattr(novnc_svc, "_port_open", lambda _port: True)
    monkeypatch.setattr(novnc_svc.shutil, "which", lambda _n: "/usr/bin/kubectl")
    called = {"popen": 0}

    class FakeProc:
        def poll(self):
            return 0

    def fake_popen(*_a, **_k):
        called["popen"] += 1
        return FakeProc()

    monkeypatch.setattr(novnc_svc.subprocess, "Popen", fake_popen)
    try:
        novnc_svc.ensure_novnc_portforward(str(kc))
    except novnc_svc.NovncProxyError:
        pass
    assert called["popen"] == 1
