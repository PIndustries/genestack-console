"""One-time WebSocket tickets.

Browser-facing realtime endpoints (``/stream`` SSE, ``/terminal`` WS) cannot
set auth headers, so credentials used to travel as ``?token=`` query params —
and query strings land in uvicorn access logs and proxy logs. Instead the UI
exchanges its header credential for a short-lived, single-use ticket via
``POST /api/v1/auth/ticket`` and connects with ``?ticket=``.

Tickets are stored in memory (sha256 hash → principal + expiry), which is
fine for the single-process console. Caveat: with multiple uvicorn workers a
ticket is only valid on the worker that issued it — switch to a shared store
(DB table or Redis) if the console ever runs multi-worker.
"""

from __future__ import annotations

import hashlib
import secrets
import threading
import time

from app.schemas import Principal

TICKET_TTL_SECONDS = 60

_lock = threading.Lock()
# sha256(ticket) -> (principal, expires_at epoch seconds)
_store: dict[str, tuple[Principal, float]] = {}


def create_ticket(principal: Principal) -> tuple[str, int]:
    """Mint a single-use ticket for ``principal``; returns (ticket, ttl)."""
    ticket = f"gst_{secrets.token_urlsafe(24)}"
    digest = hashlib.sha256(ticket.encode("utf-8")).hexdigest()
    with _lock:
        _purge_expired()
        _store[digest] = (principal, time.time() + TICKET_TTL_SECONDS)
    return ticket, TICKET_TTL_SECONDS


def consume_ticket(ticket: str) -> Principal | None:
    """Validate and consume a ticket. Single-use: a second consume fails."""
    ticket = ticket.strip()
    if not ticket:
        return None
    digest = hashlib.sha256(ticket.encode("utf-8")).hexdigest()
    with _lock:
        entry = _store.pop(digest, None)
    if entry is None:
        return None
    principal, expires_at = entry
    if time.time() >= expires_at:
        return None
    return principal


def _purge_expired() -> None:
    """Drop expired entries so the store can't grow unboundedly."""
    now = time.time()
    for digest in [d for d, (_, exp) in _store.items() if now >= exp]:
        _store.pop(digest, None)


def _reset() -> None:
    """Tests: drop all outstanding tickets."""
    with _lock:
        _store.clear()
