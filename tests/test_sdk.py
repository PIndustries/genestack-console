"""SDK client and in-process spans. No live network."""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import httpx
import pytest

_SDK_ROOT = Path(__file__).resolve().parents[1] / "sdk"
if str(_SDK_ROOT) not in sys.path:
    sys.path.insert(0, str(_SDK_ROOT))

from genestack_sdk import Client  # noqa: E402
from genestack_sdk.tracing import recent, trace  # noqa: E402


def test_login_refresh_stores_tokens_and_does_not_log(caplog):
    seen: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.content)
        text = request.content.decode()
        if "grant_type=password" in text:
            return httpx.Response(
                200,
                json={
                    "access_token": "atk-first",
                    "refresh_token": "rtk-first",
                    "token_type": "Bearer",
                },
            )
        if "grant_type=refresh_token" in text:
            return httpx.Response(
                200,
                json={
                    "access_token": "atk-second",
                    "refresh_token": "rtk-second",
                    "token_type": "Bearer",
                },
            )
        return httpx.Response(404, json={"error": "not-found"})

    transport = httpx.MockTransport(handler)
    with Client("http://console.example", transport=transport) as client:
        with caplog.at_level(logging.DEBUG):
            client.login("ada", "pw-secret")
            assert client.access_token == "atk-first"
            assert client.refresh_token == "rtk-first"
            client.refresh()
            assert client.access_token == "atk-second"
            assert client.refresh_token == "rtk-second"
        assert "atk-first" not in repr(client)
        assert "rtk-second" not in repr(client)

    assert len(seen) == 2
    login_body = seen[0].decode()
    refresh_body = seen[1].decode()
    assert "grant_type=password" in login_body
    assert "client_id=genestack-console" in login_body
    assert "scope=console" in login_body
    assert "username=ada" in login_body
    assert "client_secret" not in login_body
    assert "grant_type=refresh_token" in refresh_body
    assert "refresh_token=rtk-first" in refresh_body
    assert "client_secret" not in refresh_body
    blob = caplog.text
    for secret in ("atk-first", "atk-second", "rtk-first", "rtk-second", "pw-secret"):
        assert secret not in blob


def test_login_posts_token_path():
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(
            200,
            json={"access_token": "atk-path", "refresh_token": "rtk-path"},
        )

    transport = httpx.MockTransport(handler)
    with Client("http://console.example", transport=transport) as client:
        client.login("ada", "pw")
    assert paths == ["/api/v1/oauth/token"]


def test_get_sends_api_key_and_prefixes_api_v1():
    seen: list[tuple[str, str | None, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(
            (
                request.url.path,
                request.headers.get("x-api-key"),
                request.headers.get("authorization"),
            )
        )
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    with Client(
        "http://console.example",
        api_key="dev-admin-key",
        transport=transport,
    ) as client:
        response = client.request("GET", "traces")
        absolute = client.request("GET", "/health")
    assert response.status_code == 200
    assert absolute.status_code == 200
    assert seen[0] == ("/api/v1/traces", "dev-admin-key", None)
    assert seen[1] == ("/health", "dev-admin-key", None)


def test_tracing_records_local_span():
    with trace("sdk-local"):
        pass
    spans = [span for span in recent(200) if span["name"] == "sdk-local"]
    assert spans
    assert spans[-1]["status"] == "ok"
    assert spans[-1]["duration_ms"] >= 0
    assert spans[-1]["started_at"].endswith("+00:00")
    assert "error" not in spans[-1]


def test_tracing_posts_span_without_tokens():
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content.decode()))
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    with Client(
        "http://console.example",
        api_key="dev-admin-key",
        access_token="atk-post",
        transport=transport,
    ) as client:
        with trace("sdk-post", client=client):
            pass
        with pytest.raises(RuntimeError):
            with trace("sdk-post-err", client=client):
                raise RuntimeError("access_token=atk-post secret")
    local = [span for span in recent(200) if span["name"] == "sdk-post-err"][-1]
    assert local["status"] == "error"
    assert local["error"] == "RuntimeError"
    assert "atk-post" not in json.dumps(local)
    assert "secret" not in json.dumps(local)
    assert len(bodies) == 2
    for body in bodies:
        assert set(body) == {"name", "duration_ms", "status"}
        dumped = json.dumps(body)
        assert "atk-post" not in dumped
        assert "dev-admin-key" not in dumped
    assert bodies[1]["status"] == "error"


def test_tracing_ignores_transport_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    transport = httpx.MockTransport(handler)
    with Client("http://console.example", transport=transport) as client:
        with trace("sdk-down", client=client):
            pass
    spans = [span for span in recent(200) if span["name"] == "sdk-down"]
    assert spans[-1]["status"] == "ok"
