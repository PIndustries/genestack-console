"""SSE stream tests: auth, topic validation, subscriber cap, event round trip."""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest
from fastapi.responses import StreamingResponse

from app.config import get_settings
from app.db import SessionLocal
from app.routers import stream
from app.services import events


@pytest.fixture(scope="module", autouse=True)
def _wire_router(app):
    # main.py wiring lands with integration; mount here if absent so these
    # tests run standalone.
    paths = {getattr(r, "path", None) for r in app.routes}
    if "/api/v1/stream" not in paths:
        app.include_router(stream.router)


@pytest.fixture(autouse=True)
def _fast_heartbeat(monkeypatch):
    # Keep streaming tests snappy and ensure disconnect is detected promptly.
    monkeypatch.setattr(stream, "_HEARTBEAT_SECONDS", 0.1)


def _suffix() -> str:
    return uuid.uuid4().hex[:8]


def _create_env(client, admin_headers, tenant_id=None):
    body = {"name": f"env-{_suffix()}"}
    if tenant_id:
        body["tenant_id"] = tenant_id
    resp = client.post("/api/v1/environments", headers=admin_headers, json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.fixture
def member_setup(client, admin_headers):
    """A tenant with an env and a viewer session token for that tenant."""
    suffix = _suffix()
    tenant = client.post(
        "/api/v1/tenants", headers=admin_headers, json={"name": f"t-{suffix}"}
    ).json()
    env = _create_env(client, admin_headers, tenant_id=tenant["id"])
    user = client.post(
        "/api/v1/users",
        headers=admin_headers,
        json={
            "username": f"viewer-{suffix}",
            "password": "pw",
            "memberships": [{"tenant_id": tenant["id"], "role": "viewer"}],
        },
    ).json()
    login = client.post(
        "/api/v1/auth/login", json={"username": user["username"], "password": "pw"}
    )
    assert login.status_code == 200, login.text
    return {"tenant": tenant, "env": env, "token": login.json()["token"]}


def _ticket(client, credential: str = "dev-admin-key", bearer: bool = False) -> str:
    """Mint a single-use stream/terminal ticket for the given credential."""
    headers = (
        {"Authorization": f"Bearer {credential}"}
        if bearer
        else {"X-API-Key": credential}
    )
    resp = client.post("/api/v1/auth/ticket", headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["ticket"]


# ---------------------------------------------------------------------------
# Auth
#
# A successful connect returns a never-ending StreamingResponse, and the ASGI
# test transport does not propagate a client disconnect — a full HTTP round
# trip for the 200 path hangs the test client. So success paths are exercised
# at the handler/dependency level (the generator itself is covered below),
# while all error paths (401/400/403/503) are plain JSON and tested over HTTP.
# ---------------------------------------------------------------------------


def test_stream_requires_credentials(client):
    assert client.get("/api/v1/stream").status_code == 401


def test_stream_rejects_raw_token_query_param(client):
    # Raw credentials in the URL are no longer accepted — query strings land
    # in access logs. Even a valid key must be exchanged for a ticket first.
    assert client.get("/api/v1/stream?token=dev-viewer-key").status_code == 401
    assert client.get("/api/v1/stream?token=not-a-real-token").status_code == 401


def test_stream_rejects_garbage_ticket(client):
    assert client.get("/api/v1/stream?ticket=gst_bogus").status_code == 401


def test_stream_accepts_ticket(client, member_setup):
    ticket = _ticket(client, member_setup["token"], bearer=True)
    with SessionLocal() as db:
        principal = stream._stream_principal(_FakeRequest(), db, ticket=ticket)
    assert principal.auth_method == "session"
    assert principal.role == "viewer"
    assert principal.platform_admin is False


def test_stream_ticket_is_single_use(client):
    ticket = _ticket(client)
    with SessionLocal() as db:
        first = stream._stream_principal(_FakeRequest(), db, ticket=ticket)
    assert first.role == "admin"
    # Second use of the same ticket: rejected (unit level — the 200 stream
    # path can't round-trip through the ASGI test transport, see above).
    assert client.get(f"/api/v1/stream?ticket={ticket}").status_code == 401


def test_stream_header_auth_still_works():
    with SessionLocal() as db:
        api_key = stream._stream_principal(
            _FakeRequest(headers={"X-API-Key": "dev-admin-key"}), db, ticket=None
        )
        bearer = stream._stream_principal(
            _FakeRequest(headers={"Authorization": "Bearer dev-viewer-key"}),
            db,
            ticket=None,
        )
    assert api_key.role == "admin" and api_key.platform_admin is True
    assert bearer.role == "viewer"


def test_stream_endpoint_returns_sse_response(client, member_setup):
    ticket = _ticket(client, member_setup["token"], bearer=True)

    async def run():
        with SessionLocal() as db:
            principal = stream._stream_principal(_FakeRequest(), db, ticket=ticket)
            resp = await stream.stream_events(
                _FakeRequest(false_count=0), "fleet,alerts", db, principal
            )
            # Client already gone: first poll disconnects, finally unsubscribes.
            with pytest.raises(StopAsyncIteration):
                await resp.body_iterator.__anext__()
            return resp

    resp = asyncio.run(run())
    assert isinstance(resp, StreamingResponse)
    assert resp.media_type == "text/event-stream"
    assert resp.headers["cache-control"] == "no-cache"


# ---------------------------------------------------------------------------
# Topic parsing / validation
# ---------------------------------------------------------------------------


def test_stream_unknown_topic_rejected(client, admin_headers):
    resp = client.get("/api/v1/stream?topics=bogus", headers=admin_headers)
    assert resp.status_code == 400
    assert "Unknown topic" in resp.json()["detail"]


def test_stream_empty_topics_rejected(client, admin_headers):
    resp = client.get("/api/v1/stream?topics=%20%2C%20", headers=admin_headers)
    assert resp.status_code == 400


def test_stream_env_topic_for_missing_env_rejected(client, admin_headers):
    resp = client.get("/api/v1/stream?topics=env:no-such-env", headers=admin_headers)
    assert resp.status_code == 400


def test_stream_env_topic_tenant_gating(client, admin_headers, member_setup):
    # Member may subscribe to their own tenant's env topic.
    ticket = _ticket(client, member_setup["token"], bearer=True)

    async def run():
        with SessionLocal() as db:
            principal = stream._stream_principal(_FakeRequest(), db, ticket=ticket)
            resp = await stream.stream_events(
                _FakeRequest(false_count=0),
                f"env:{member_setup['env']['id']}",
                db,
                principal,
            )
            with pytest.raises(StopAsyncIteration):
                await resp.body_iterator.__anext__()
            return resp

    resp = asyncio.run(run())
    assert isinstance(resp, StreamingResponse)

    # ... but not to an env in another tenant.
    other = _create_env(client, admin_headers)
    foreign = _ticket(client, member_setup["token"], bearer=True)
    resp = client.get(f"/api/v1/stream?topics=env:{other['id']}&ticket={foreign}")
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Subscriber cap
# ---------------------------------------------------------------------------


def test_stream_subscriber_cap_returns_503(client, admin_headers, monkeypatch):
    monkeypatch.setattr(get_settings(), "stream_max_subscribers", 0)
    resp = client.get("/api/v1/stream?topics=fleet", headers=admin_headers)
    assert resp.status_code == 503


# ---------------------------------------------------------------------------
# Event generator round trip (driven directly — the app lifespan does not
# init the event bus loop under the test client, so cross-thread publishing
# is out of scope here).
# ---------------------------------------------------------------------------


class _FakeRequest:
    """Minimal Request stand-in: headers for auth, and is_disconnected()
    that returns False `false_count` times, then True."""

    def __init__(self, headers: dict | None = None, false_count: int = 1):
        self.headers = headers or {}
        self._remaining = false_count

    async def is_disconnected(self) -> bool:
        if self._remaining > 0:
            self._remaining -= 1
            return False
        return True


def test_event_stream_data_frame_and_cleanup():
    before = events.subscriber_count()

    async def run():
        queue = events.subscribe(["fleet"])
        queue.put_nowait(("fleet", {"ok": True}))
        gen = stream._event_stream(_FakeRequest(false_count=1), queue)

        frame = await gen.__anext__()
        assert (
            frame
            == f"data: {json.dumps({'topic': 'fleet', 'payload': {'ok': True}})}\n\n"
        )
        assert json.loads(frame.removeprefix("data: ")) == {
            "topic": "fleet",
            "payload": {"ok": True},
        }

        # Client "disconnects" on the next poll: generator ends and unsubscribes.
        with pytest.raises(StopAsyncIteration):
            await gen.__anext__()

    asyncio.run(run())
    assert events.subscriber_count() == before


def test_event_stream_heartbeat_on_idle():
    async def run():
        queue = events.subscribe(["alerts"])
        gen = stream._event_stream(_FakeRequest(false_count=1), queue)
        frame = await gen.__anext__()
        assert frame == ": hb\n\n"
        with pytest.raises(StopAsyncIteration):
            await gen.__anext__()

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Per-tenant filtering of shared topics (fleet/jobs/alerts/metrics)
# ---------------------------------------------------------------------------


def test_visible_env_ids_scoping(client, admin_headers, member_setup):
    other = _create_env(client, admin_headers)
    ticket = _ticket(client, member_setup["token"], bearer=True)
    with SessionLocal() as db:
        principal = stream._stream_principal(_FakeRequest(), db, ticket=ticket)
        visible = stream._visible_env_ids(db, principal)
        admin = stream._stream_principal(
            _FakeRequest(headers={"X-API-Key": "dev-admin-key"}), db, ticket=None
        )
        # Platform admin is unrestricted (None).
        assert stream._visible_env_ids(db, admin) is None
    assert member_setup["env"]["id"] in visible
    assert other["id"] not in visible


def test_event_stream_filters_other_tenant_payloads():
    async def run():
        queue = events.subscribe(["fleet", "jobs", "alerts"])
        gen = stream._event_stream(
            _FakeRequest(false_count=5), queue, visible_env_ids={"own-env"}
        )
        # All three shared topics for another tenant's env: filtered out.
        queue.put_nowait(("fleet", {"type": "fleet", "environment_id": "other-env"}))
        queue.put_nowait(("jobs", {"type": "job", "environment_id": "other-env"}))
        queue.put_nowait(
            ("alerts", {"type": "alert_fired", "environment_id": "other-env"})
        )
        # Own env: delivered.
        queue.put_nowait(("fleet", {"type": "fleet", "environment_id": "own-env"}))
        frame = await gen.__anext__()
        data = json.loads(frame.removeprefix("data: "))
        assert data == {
            "topic": "fleet",
            "payload": {"type": "fleet", "environment_id": "own-env"},
        }
        # Payloads without an environment_id pass through.
        queue.put_nowait(("jobs", {"type": "job", "id": "j1"}))
        frame = await gen.__anext__()
        assert json.loads(frame.removeprefix("data: "))["payload"] == {
            "type": "job",
            "id": "j1",
        }
        with pytest.raises(StopAsyncIteration):
            await gen.__anext__()

    asyncio.run(run())


def test_event_stream_unrestricted_visible_passes_everything():
    async def run():
        queue = events.subscribe(["fleet"])
        # None = platform admin: no filtering.
        gen = stream._event_stream(_FakeRequest(false_count=1), queue, None)
        queue.put_nowait(("fleet", {"type": "fleet", "environment_id": "any-env"}))
        frame = await gen.__anext__()
        assert "any-env" in frame
        with pytest.raises(StopAsyncIteration):
            await gen.__anext__()

    asyncio.run(run())


def test_event_stream_refreshes_visible_envs(monkeypatch):
    monkeypatch.setattr(stream, "_VISIBILITY_REFRESH_SECONDS", 0)
    calls = []

    def refresh():
        calls.append(1)
        return {"env-b"}

    async def run():
        queue = events.subscribe(["fleet"])
        gen = stream._event_stream(
            _FakeRequest(false_count=2),
            queue,
            visible_env_ids={"env-a"},
            refresh_visible=refresh,
        )
        # After refresh only env-b is visible: env-a filtered, env-b delivered.
        queue.put_nowait(("fleet", {"type": "fleet", "environment_id": "env-a"}))
        queue.put_nowait(("fleet", {"type": "fleet", "environment_id": "env-b"}))
        frame = await gen.__anext__()
        assert "env-b" in frame
        with pytest.raises(StopAsyncIteration):
            await gen.__anext__()

    asyncio.run(run())
    assert calls


def test_stream_handler_filters_shared_topics_for_member(
    client, admin_headers, member_setup
):
    other = _create_env(client, admin_headers)
    ticket = _ticket(client, member_setup["token"], bearer=True)

    async def run():
        with SessionLocal() as db:
            principal = stream._stream_principal(_FakeRequest(), db, ticket=ticket)
            resp = await stream.stream_events(
                _FakeRequest(false_count=4), "fleet,jobs,alerts", db, principal
            )
        it = resp.body_iterator
        await events.publish(
            "fleet", {"type": "fleet", "environment_id": other["id"], "health": "down"}
        )
        await events.publish(
            "jobs", {"type": "job", "environment_id": other["id"], "status": "running"}
        )
        await events.publish(
            "alerts", {"type": "alert_fired", "environment_id": other["id"]}
        )
        await events.publish(
            "fleet",
            {
                "type": "fleet",
                "environment_id": member_setup["env"]["id"],
                "health": "healthy",
            },
        )
        frame = await it.__anext__()
        data = json.loads(frame.removeprefix("data: "))
        assert data["topic"] == "fleet"
        assert data["payload"]["environment_id"] == member_setup["env"]["id"]
        with pytest.raises(StopAsyncIteration):
            await it.__anext__()

    asyncio.run(run())


def test_stream_handler_admin_receives_all_tenants(client, admin_headers):
    other = _create_env(client, admin_headers)

    async def run():
        with SessionLocal() as db:
            principal = stream._stream_principal(
                _FakeRequest(headers={"X-API-Key": "dev-admin-key"}), db, ticket=None
            )
            resp = await stream.stream_events(
                _FakeRequest(false_count=1), "fleet", db, principal
            )
        it = resp.body_iterator
        await events.publish("fleet", {"type": "fleet", "environment_id": other["id"]})
        frame = await it.__anext__()
        data = json.loads(frame.removeprefix("data: "))
        assert data["payload"]["environment_id"] == other["id"]
        with pytest.raises(StopAsyncIteration):
            await it.__anext__()

    asyncio.run(run())
