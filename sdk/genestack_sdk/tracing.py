"""Spans recorded in the process that imports this package.

A span is one named piece of work. This buffer is the caller's process, not
the console's buffer. Pass a Client and the span is also posted to
``/api/v1/traces``. A transport error is ignored so the caller's work still
finishes. The span does not include tokens.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

_BUFFER_MAX = 500
_RECENT_MAX = 200
_NAME_MAX = 128
_ERROR_MAX = 200

_lock = threading.Lock()
_spans: deque[dict[str, Any]] = deque(maxlen=_BUFFER_MAX)


def _clip(limit: int) -> int:
    try:
        count = int(limit)
    except (TypeError, ValueError):
        return 50
    if count < 0:
        return 0
    if count > _RECENT_MAX:
        return _RECENT_MAX
    return count


def record(
    name: str,
    duration_ms: float,
    status: str = "ok",
    *,
    started_at: str | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    """Append one span. The returned dict is a copy."""
    if status not in {"ok", "error"}:
        raise ValueError("status must be ok or error")
    span: dict[str, Any] = {
        "name": str(name)[:_NAME_MAX],
        "duration_ms": float(duration_ms),
        "status": status,
        "started_at": started_at or datetime.now(timezone.utc).isoformat(),
    }
    if error:
        span["error"] = str(error)[:_ERROR_MAX]
    with _lock:
        _spans.append(span)
    return dict(span)


def recent(limit: int = 50) -> list[dict[str, Any]]:
    """Newest last. ``limit`` is capped at 200."""
    count = _clip(limit)
    with _lock:
        chosen = list(_spans)[-count:] if count else []
        return [dict(span) for span in chosen]


def _post(client: Any, span: dict[str, Any]) -> None:
    payload = {
        "name": span["name"],
        "duration_ms": span["duration_ms"],
        "status": span["status"],
    }
    try:
        client.request("POST", "/api/v1/traces", json=payload)
    except Exception:
        return


@contextmanager
def trace(name: str, client: Any = None) -> Iterator[None]:
    """Time ``name`` in this process. ``client`` also posts the span."""
    started_at = datetime.now(timezone.utc).isoformat()
    t0 = time.perf_counter()
    status = "ok"
    error: str | None = None
    try:
        yield
    except Exception as exc:
        status = "error"
        error = type(exc).__name__[:_ERROR_MAX]
        raise
    finally:
        span = record(
            name,
            (time.perf_counter() - t0) * 1000.0,
            status,
            started_at=started_at,
            error=error,
        )
        if client is not None:
            _post(client, span)
