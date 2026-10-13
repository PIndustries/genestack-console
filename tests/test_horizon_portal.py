"""Horizon opens inside the console through an unguessable session."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth import resolve_principal
from app.db import get_db
from app.models import Environment
from app.routers import horizon as horizon_router
from app.schemas import Principal
from app.services import horizon_proxy as hz


def setup_function() -> None:
    hz._reset()


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(horizon_router.router)
    return app


class _Ctx:
    kubeconfig = "/tmp/kube"

    def cleanup(self) -> None:
        return None


class _DB:
    def get(self, _model, key):
        if key == "env-1":
            return Environment(id="env-1", name="e", kubeconfig_path="/tmp/kube")
        return None


def _operator_app() -> FastAPI:
    app = _app()
    app.dependency_overrides[horizon_router._operator_env] = lambda: Environment(
        id="env-1", name="e", kubeconfig_path="/tmp/kube"
    )
    app.dependency_overrides[resolve_principal] = lambda: Principal(
        username="ops", role="operator", auth_method="test"
    )
    return app


def test_openstack_page_offers_horizon():
    text = Path("app/static/js/pages/environment_cloud.js").read_text(encoding="utf-8")
    assert 'id="os-cloud-horizon"' in text
    assert "Open Horizon" in text
    assert "/cloud/horizon/session" in text
    assert "export async function openHorizon" in text
    cluster = Path("app/static/js/pages/environment_cluster.js").read_text(encoding="utf-8")
    assert "data-open-horizon" in cluster
    detail = Path("app/static/js/pages/environment_detail.js").read_text(encoding="utf-8")
    assert "environment_cloud.js?v=ls73" in detail
    assert "environment_cluster.js?v=ls61" in detail
    assert "onHorizon: (id) => openHorizon(id)" in detail


def test_session_returns_embed_url(monkeypatch):
    def fake_open(_kube, *, env_id, actor):
        sid = hz.create_session(env_id=env_id, actor=actor)
        entry = hz.get_session(sid, env_id)
        assert entry is not None
        return sid, entry

    monkeypatch.setattr(horizon_router, "build_context", lambda *_a, **_k: _Ctx())
    monkeypatch.setattr(horizon_router.hz, "open_session", fake_open)
    client = TestClient(_operator_app())
    resp = client.post("/api/v1/environments/env-1/cloud/horizon/session")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["error"] is None
    assert body["session_id"].startswith("hz_")
    assert body["embed_url"] == (
        f"/api/v1/environments/env-1/cloud/horizon/{body['session_id']}/"
    )


def test_session_failure_is_not_a_500(monkeypatch):
    def boom(_kube, *, env_id, actor):
        raise hz.HorizonProxyError("horizon service not found")

    monkeypatch.setattr(horizon_router, "build_context", lambda *_a, **_k: _Ctx())
    monkeypatch.setattr(horizon_router.hz, "open_session", boom)
    client = TestClient(_operator_app())
    resp = client.post("/api/v1/environments/env-1/cloud/horizon/session")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert body["embed_url"] is None
    assert "not found" in body["error"]


def test_proxy_rewrites_horizon_links(monkeypatch):
    sid = hz.create_session(env_id="env-1", actor="ops")
    entry = hz.get_session(sid, "env-1")
    assert entry is not None
    entry.service = "horizon-int"

    def fake_forward(_kube, _session, **kwargs):
        assert kwargs["path"] == "/dashboard/"
        html = '<a href="/dashboard/">Home</a>'
        return 200, html.encode(), {"content-type": "text/html; charset=utf-8"}, []

    monkeypatch.setattr(horizon_router, "build_context", lambda *_a, **_k: _Ctx())
    monkeypatch.setattr(horizon_router.hz, "forward", fake_forward)
    app = _app()
    app.dependency_overrides[get_db] = lambda: _DB()
    client = TestClient(app)
    resp = client.get(f"/api/v1/environments/env-1/cloud/horizon/{sid}/dashboard/")
    assert resp.status_code == 200
    prefix = f"/api/v1/environments/env-1/cloud/horizon/{sid}"
    assert f'href="{prefix}/dashboard/"' in resp.text


def test_proxy_sends_the_csrf_cookie(monkeypatch):
    sid = hz.create_session(env_id="env-1", actor="ops")
    entry = hz.get_session(sid, "env-1")
    assert entry is not None
    entry.service = "horizon-int"
    entry.cookies = {"csrftoken": "tok123", "sessionid": "sess123"}

    def fake_forward(_kube, _session, **kwargs):
        assert kwargs["path"] == "/"
        assert kwargs["extra_headers"]["Referer"] == "http://horizon-int/"
        return 200, b"<html></html>", {"content-type": "text/html"}, []

    monkeypatch.setattr(horizon_router, "build_context", lambda *_a, **_k: _Ctx())
    monkeypatch.setattr(horizon_router.hz, "forward", fake_forward)
    app = _app()
    app.dependency_overrides[get_db] = lambda: _DB()
    client = TestClient(app)
    resp = client.get(f"/api/v1/environments/env-1/cloud/horizon/{sid}/")
    assert resp.status_code == 200
    cookies = resp.headers.get_list("set-cookie")
    csrf = next(item for item in cookies if item.startswith("csrftoken="))
    session = next(item for item in cookies if item.startswith("sessionid="))
    assert "HttpOnly" not in csrf
    assert "HttpOnly" in session
    assert f"Path=/api/v1/environments/env-1/cloud/horizon/{sid}" in csrf


def test_session_unexpected_error_is_not_a_500(monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("kube down")

    monkeypatch.setattr(horizon_router, "build_context", boom)
    client = TestClient(_operator_app())
    resp = client.post("/api/v1/environments/env-1/cloud/horizon/session")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert "RuntimeError" in body["error"]


def test_unknown_session_404():
    app = _app()
    app.dependency_overrides[get_db] = lambda: _DB()
    client = TestClient(app)
    resp = client.get("/api/v1/environments/env-1/cloud/horizon/hz_not-a-real-session/dashboard/")
    assert resp.status_code == 404
