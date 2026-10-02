"""In-process spans for this console and for modules people add.

A span is one named piece of work: name, duration_ms, status (ok or error),
and started_at. The buffer holds 500 spans. Reading returns at most 200,
newest last. The buffer is this process. A restart clears it. There is no
database table.
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
    """Append one span and return a copy. ``error`` is capped at 200 characters."""
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
    """Newest last. ``limit`` defaults to 50 and is capped at 200."""
    count = _clip(limit)
    with _lock:
        chosen = list(_spans)[-count:] if count else []
        return [dict(span) for span in chosen]


@contextmanager
def trace(name: str) -> Iterator[None]:
    """Record ``name``. On exception, store the exception class, not the message."""
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
        record(
            name,
            (time.perf_counter() - t0) * 1000.0,
            status,
            started_at=started_at,
            error=error,
        )
