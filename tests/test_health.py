"""Health endpoint checks."""

from __future__ import annotations

import app.main as main_mod


def test_health_returns_200(client):
    """GET /health returns 200 (unauthenticated)."""
    resp = client.get("/health")
    assert resp.status_code == 200
    # Body is optional; accept common shapes when present
    if resp.content:
        data = resp.json()
        if isinstance(data, dict):
            status = data.get("status") or data.get("health") or data.get("state")
            if status is not None:
                assert str(status).lower() in {"ok", "healthy", "up", "pass", "true"}


def test_health_live_returns_200(client):
    resp = client.get("/health/live")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "alive"
    assert "uptime_seconds" in body


def test_health_ready_returns_200(client):
    resp = client.get("/health/ready")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ready"
    assert body["database"] == "ok"


def test_health_startup_ok_after_lifespan(client):
    """Lifespan must set the module global; otherwise startupProbe stays 503 forever."""
    assert main_mod._startup_time is not None
    resp = client.get("/health/startup")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "started"
    assert body["uptime_seconds"] >= 0


def test_health_startup_503_while_unstarted(client):
    """Simulate pre-lifespan state: module global cleared → 503."""
    previous = main_mod._startup_time
    try:
        main_mod._startup_time = None
        resp = client.get("/health/startup")
        assert resp.status_code == 503
        assert "Startup in progress" in resp.json()["detail"]
    finally:
        main_mod._startup_time = previous
