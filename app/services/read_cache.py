"""Short-lived singleflight cache for live Kubernetes and OpenStack reads.

One caller runs the loader on a miss. Others wait and reuse that result so
Overview does not open a dial per request. Stored and returned values are
copies; a caller cannot mutate the cache.
"""

from __future__ import annotations

import copy
import threading
import time
from typing import Any, Callable

_lock = threading.Lock()
_cache: dict[str, tuple[float, Any]] = {}
_inflight: dict[str, threading.Event] = {}
# Distinct from a stored None. A loader is allowed to return None.
_MISSING = object()


def _copy(key: str, *, max_age: float | None) -> Any:
    with _lock:
        hit = _cache.get(key)
        if hit is None:
            return _MISSING
        stored_at, payload = hit
        if max_age is not None and time.monotonic() - stored_at > max_age:
            return _MISSING
    return copy.deepcopy(payload)


def _store(key: str, payload: Any) -> None:
    stored = copy.deepcopy(payload)
    with _lock:
        _cache[key] = (time.monotonic(), stored)


def _claim(key: str) -> tuple[bool, threading.Event]:
    with _lock:
        current = _inflight.get(key)
        if current is not None:
            return False, current
        event = threading.Event()
        _inflight[key] = event
        return True, event


def _release(key: str, event: threading.Event) -> None:
    with _lock:
        if _inflight.get(key) is event:
            _inflight.pop(key, None)
    event.set()


def cached_read(
    key: str,
    loader: Callable[[], Any],
    *,
    refresh: bool = False,
    ttl: float = 15.0,
    wait: float = 20.0,
) -> Any:
    """Return a recent loader result, or run ``loader`` once for this key.

    An empty key calls ``loader`` and is not stored. ``refresh`` skips the
    fresh hit and stores a new result; it may overlap an in-flight leader.
    A waiter that times out with nothing stored may run ``loader`` itself.
    """
    if not key:
        return loader()
    if not refresh:
        fresh = _copy(key, max_age=ttl)
        if fresh is not _MISSING:
            return fresh
        leader, event = _claim(key)
        if not leader:
            event.wait(wait)
            cached = _copy(key, max_age=None)
            if cached is not _MISSING:
                return cached
        else:
            try:
                result = loader()
                _store(key, result)
                return result
            finally:
                _release(key, event)
    result = loader()
    _store(key, result)
    return result
