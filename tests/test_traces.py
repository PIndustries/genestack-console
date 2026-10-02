"""In-process trace buffer and /api/v1/traces."""

from __future__ import annotations

import pytest

from app.services import traces


@pytest.fixture(scope="module", autouse=True)
def _mount_traces(client):
    """Mount the router when app/main.py has not included it yet."""
    from app.routers.traces import router

    paths = {getattr(route, "path", None) for route in client.app.routes}
    if "/api/v1/traces" not in paths:
        client.app.include_router(router)


def test_trace_records_ok_and_error():
    with traces.trace("ok-span"):
        pass
    with pytest.raises(RuntimeError, match="sekret-value"):
        with traces.trace("err-span"):
            raise RuntimeError("token=sekret-value")
    ok = [span for span in traces.recent(200) if span["name"] == "ok-span"][-1]
    err = [span for span in traces.recent(200) if span["name"] == "err-span"][-1]
    assert ok["status"] == "ok"
    assert ok["duration_ms"] >= 0
    assert isinstance(ok["duration_ms"], float)
    assert ok["started_at"].endswith("+00:00")
    assert "error" not in ok
    assert err["status"] == "error"
    assert err["error"] == "RuntimeError"
    assert "sekret-value" not in str(err)
    assert "token" not in str(err)


def test_get_returns_span(client, admin_headers):
    with traces.trace("from-get"):
        pass
    response = client.get("/api/v1/traces?limit=50", headers=admin_headers)
    assert response.status_code == 200
    body = response.json()
    assert any(
        span["name"] == "from-get" and span["status"] == "ok" for span in body["spans"]
    )
    assert "dev-admin-key" not in response.text
    assert body["spans"][-1]["name"] == traces.recent(50)[-1]["name"]


def test_viewer_get_is_403(client, viewer_headers):
    response = client.get("/api/v1/traces", headers=viewer_headers)
    assert response.status_code == 403


def test_post_bad_status_is_400(client, admin_headers):
    before = len(traces._spans)
    response = client.post(
        "/api/v1/traces",
        headers=admin_headers,
        json={"name": "nope-span", "duration_ms": 1.5, "status": "nope"},
    )
    assert response.status_code == 400
    assert len(traces._spans) == before
    ok = client.post(
        "/api/v1/traces",
        headers=admin_headers,
        json={"name": "posted-span", "duration_ms": 1.5, "status": "ok"},
    )
    assert ok.status_code == 200
    span = ok.json()
    assert span["name"] == "posted-span"
    assert span["status"] == "ok"
    assert span["duration_ms"] == 1.5
    assert "access_token" not in span
    assert "refresh_token" not in span


def test_buffer_does_not_grow_past_500():
    for index in range(520):
        traces.record(name=f"cap-{index}", duration_ms=0.1, status="ok")
    assert len(traces._spans) == 500
    assert traces._spans[0]["name"] == "cap-20"
    assert traces._spans[-1]["name"] == "cap-519"
    window = traces.recent(limit=50)
    assert len(window) == 50
    assert window[0]["name"] == "cap-470"
    assert window[-1]["name"] == "cap-519"
    assert "cap-0" not in {span["name"] for span in traces._spans}
