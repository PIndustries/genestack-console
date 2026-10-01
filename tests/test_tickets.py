"""One-time WS/SSE tickets and access-log redaction.

Tickets (POST /api/v1/auth/ticket) replace raw ``?token=`` credentials on the
browser-facing /stream and /terminal endpoints; the RedactCredentialsFilter
keeps any remaining token=/ticket= query values out of uvicorn access logs.
"""

from __future__ import annotations

import logging

import pytest

from app.schemas import Principal
from app.services import tickets
from app.services.logredact import RedactCredentialsFilter


@pytest.fixture(autouse=True)
def _clean_tickets():
    yield
    tickets._reset()


def _viewer() -> Principal:
    return Principal(username="viewer-1", role="viewer", auth_method="session")


# ---------------------------------------------------------------------------
# Store semantics
# ---------------------------------------------------------------------------


def test_create_returns_prefixed_ticket_and_ttl():
    ticket, expires_in = tickets.create_ticket(_viewer())
    assert ticket.startswith("gst_")
    assert expires_in == tickets.TICKET_TTL_SECONDS == 60


def test_consume_returns_stored_principal():
    ticket, _ = tickets.create_ticket(_viewer())
    principal = tickets.consume_ticket(ticket)
    assert principal is not None
    assert principal.username == "viewer-1"
    assert principal.role == "viewer"


def test_ticket_is_single_use():
    ticket, _ = tickets.create_ticket(_viewer())
    assert tickets.consume_ticket(ticket) is not None
    assert tickets.consume_ticket(ticket) is None


def test_unknown_ticket_rejected():
    assert tickets.consume_ticket("gst_nope") is None
    assert tickets.consume_ticket("") is None


def test_expired_ticket_rejected(monkeypatch):
    monkeypatch.setattr(tickets, "TICKET_TTL_SECONDS", -1)
    ticket, _ = tickets.create_ticket(_viewer())
    assert tickets.consume_ticket(ticket) is None


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


def test_ticket_endpoint_requires_auth(client):
    assert client.post("/api/v1/auth/ticket").status_code == 401


def test_ticket_endpoint_any_role(
    client, viewer_headers, operator_headers, admin_headers
):
    for headers in (viewer_headers, operator_headers, admin_headers):
        resp = client.post("/api/v1/auth/ticket", headers=headers)
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["ticket"].startswith("gst_")
        assert body["expires_in"] == 60


def test_ticket_carries_caller_principal(client, viewer_headers):
    resp = client.post("/api/v1/auth/ticket", headers=viewer_headers)
    ticket = resp.json()["ticket"]
    principal = tickets.consume_ticket(ticket)
    assert principal is not None
    assert principal.role == "viewer"
    assert principal.platform_admin is True  # static dev keys are break-glass admins


# ---------------------------------------------------------------------------
# Access-log redaction filter
# ---------------------------------------------------------------------------


def _record(message: str) -> logging.LogRecord:
    return logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg=message,
        args=(),
        exc_info=None,
    )


def test_redaction_filter_masks_token_and_ticket_values():
    record = _record(
        '127.0.0.1:5000 - "GET /api/v1/stream?topics=fleet&ticket=gst_abc123secret HTTP/1.1" 200'
    )
    assert RedactCredentialsFilter().filter(record) is True
    out = record.getMessage()
    assert "gst_abc123secret" not in out
    assert "ticket=<redacted>" in out

    record = _record(
        '127.0.0.1:5000 - "GET /api/v1/agents/connect?token=supersecret&x=1 HTTP/1.1" 101'
    )
    RedactCredentialsFilter().filter(record)
    out = record.getMessage()
    assert "supersecret" not in out
    assert "token=<redacted>" in out
    assert "x=1" in out  # other params untouched


def test_redaction_filter_passes_clean_messages_through():
    record = _record('127.0.0.1:5000 - "GET /api/v1/health HTTP/1.1" 200')
    msg_before = record.msg
    assert RedactCredentialsFilter().filter(record) is True
    assert record.getMessage() == '127.0.0.1:5000 - "GET /api/v1/health HTTP/1.1" 200'
    assert record.msg is msg_before  # untouched when nothing to redact
