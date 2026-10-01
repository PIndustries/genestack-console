"""C2: alert webhook URLs must not reach internal/metadata addresses.

``webhook_url`` is tenant-operator input, so an attacker could otherwise point
alert delivery at the cloud metadata endpoint (169.254.169.254) or at internal
RFC1918/CGNAT hosts. The guard runs at write time (400) and defensively in
``fire_webhook`` (skip).
"""

from __future__ import annotations

import uuid

import pytest

from app.services import alerts
from app.services.alerts import assert_safe_webhook_url, fire_webhook


def _suffix() -> str:
    return uuid.uuid4().hex[:8]


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata
        "http://10.0.0.5",  # RFC1918
        "http://192.168.1.1",  # RFC1918
        "http://127.0.0.1",  # loopback
        "http://[::1]",  # loopback v6
        "file:///etc/passwd",  # non-http scheme
        "gopher://127.0.0.1/",  # non-http scheme
        "http://100.64.0.1",  # CGNAT
    ],
)
def test_assert_safe_webhook_url_rejects_internal(url):
    with pytest.raises(ValueError):
        assert_safe_webhook_url(url)


def test_assert_safe_webhook_url_accepts_public():
    assert_safe_webhook_url("https://example.com/hook")


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",
        "file:///etc/passwd",
    ],
)
def test_fire_webhook_skips_blocked_url(monkeypatch, url):
    """fire_webhook must not attempt delivery to a blocked URL (no raise)."""
    called: list[str] = []
    monkeypatch.setattr(
        alerts.urllib.request,
        "urlopen",
        lambda req, timeout=None: called.append(req.full_url) or _NoopResp(),
    )
    fire_webhook(url, {"x": 1})
    assert called == []


class _NoopResp:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return b""


def test_fire_webhook_sends_public_url(monkeypatch):
    called: list[str] = []

    def fake_urlopen(req, timeout=None):  # noqa: ARG001
        called.append(req.full_url)
        return _NoopResp()

    monkeypatch.setattr(alerts.urllib.request, "urlopen", fake_urlopen)
    fire_webhook("https://example.com/hook", {"x": 1})
    # fire_webhook posts in a daemon thread; join by polling briefly.
    import time

    deadline = time.time() + 2
    while not called and time.time() < deadline:
        time.sleep(0.01)
    assert called == ["https://example.com/hook"]


# ---------------------------------------------------------------- router 400


def test_create_rule_rejects_metadata_webhook(client, admin_headers):
    """A tenant operator cannot create a rule whose webhook hits metadata."""
    env_id = _create_env(client, admin_headers)
    resp = client.post(
        "/api/v1/alerts/rules",
        headers=admin_headers,
        json={
            "name": f"ssrf-{_suffix()}",
            "condition": "probe_failed",
            "severity": "critical",
            "environment_id": env_id,
            "webhook_url": "http://169.254.169.254/latest/meta-data/",
        },
    )
    assert resp.status_code == 400, resp.text
    assert "blocked" in resp.json()["detail"] or "scheme" in resp.json()["detail"]


def test_patch_rule_rejects_internal_webhook(client, admin_headers):
    env_id = _create_env(client, admin_headers)
    created = client.post(
        "/api/v1/alerts/rules",
        headers=admin_headers,
        json={
            "name": f"ssrf-{_suffix()}",
            "condition": "probe_failed",
            "severity": "warning",
            "environment_id": env_id,
        },
    )
    assert created.status_code == 201, created.text
    rule_id = created.json()["id"]

    resp = client.patch(
        f"/api/v1/alerts/rules/{rule_id}",
        headers=admin_headers,
        json={"webhook_url": "http://10.0.0.5"},
    )
    assert resp.status_code == 400, resp.text


def test_create_rule_accepts_public_webhook(client, admin_headers):
    env_id = _create_env(client, admin_headers)
    resp = client.post(
        "/api/v1/alerts/rules",
        headers=admin_headers,
        json={
            "name": f"ok-{_suffix()}",
            "condition": "pod_crashloop",
            "severity": "warning",
            "environment_id": env_id,
            "webhook_url": "https://example.com/hook",
        },
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["webhook_url"] == "https://example.com/hook"


def _create_env(client, admin_headers) -> str:
    resp = client.post(
        "/api/v1/environments",
        headers=admin_headers,
        json={"name": f"ssrf-env-{_suffix()}"},
    )
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["id"]
