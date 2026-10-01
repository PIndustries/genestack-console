"""In-process async event bus.

Topics: 'fleet', 'env:{id}', 'jobs', 'alerts', 'metrics'.

Subscribers get (topic, payload) tuples on an asyncio.Queue. Publishing is
non-blocking: a subscriber whose queue is full simply misses the event rather
than slowing the publisher down.
"""

from __future__ import annotations

import asyncio
import logging
import threading

log = logging.getLogger(__name__)

# Per-subscriber backlog before events get dropped for that subscriber.
_QUEUE_MAXSIZE = 1000

_lock = threading.Lock()
_subscriptions: dict[asyncio.Queue, frozenset[str]] = {}
_loop: asyncio.AbstractEventLoop | None = None


def init(loop: asyncio.AbstractEventLoop) -> None:
    """Record the app's running loop so sync worker threads can publish."""
    global _loop
    _loop = loop


def subscribe(topics: list[str]) -> asyncio.Queue:
    """Subscribe to the given topics; returns a queue of (topic, payload)."""
    queue: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
    queue.dropped_events = 0
    with _lock:
        _subscriptions[queue] = frozenset(topics)
    return queue


def unsubscribe(queue: asyncio.Queue) -> None:
    """Stop delivering events to a queue returned by subscribe()."""
    with _lock:
        _subscriptions.pop(queue, None)


def subscriber_count() -> int:
    """Number of active subscriber queues (tests/diagnostics)."""
    with _lock:
        return len(_subscriptions)


def _fanout(topic: str, payload: dict) -> None:
    with _lock:
        targets = [q for q, topics in _subscriptions.items() if topic in topics]
    for queue in targets:
        try:
            queue.put_nowait((topic, payload))
        except asyncio.QueueFull:
            queue.dropped_events += 1
            log.warning("event bus: dropping '%s' event for slow subscriber", topic)


async def publish(topic: str, payload: dict) -> None:
    """Publish an event to subscribers of `topic` (never blocks)."""
    _fanout(topic, payload)


def publish_sync(topic: str, payload: dict) -> None:
    """Publish from sync code / worker threads; no-op until init() is called."""
    loop = _loop
    if loop is None:
        return
    loop.call_soon_threadsafe(_fanout, topic, payload)
