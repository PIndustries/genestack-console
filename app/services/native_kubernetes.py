"""Bounded source-pushed Kubernetes watches and log follow (real kubectl pipes)."""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import json
import re
import time
import uuid
from collections.abc import AsyncIterator, Callable
from typing import Any

from app.services.envcontext import EnvContext
from app.services.logredact import redact_secret_line

MAX_OBJECT_BYTES = 1024 * 1024
MAX_LINE_BYTES = 64 * 1024
HEARTBEAT_SECONDS = 15
MAX_SESSION_SECONDS = 15 * 60
_JSON_SECRET = re.compile(
    r'(?i)("(?:password|passwd|api[_-]?key|secret|client[_-]?secret|'
    r'access[_-]?token|refresh[_-]?token|token|authorization|cookie)"\s*:\s*)'
    r'("(?:[^"\\]|\\.)*"|[^,}\]\s]+)'
)
WATCH_RESOURCES = frozenset(
    {
        "pods",
        "nodes",
        "events",
        "services",
        "deployments",
        "statefulsets",
        "daemonsets",
        "jobs",
    }
)


def watch_metadata(event: dict[str, Any], resource: str) -> dict[str, Any] | None:
    """No arbitrary Kubernetes specs, annotations, labels, event messages or secrets."""
    change = event.get("type")
    if not isinstance(change, str) or change not in {
        "ADDED",
        "MODIFIED",
        "DELETED",
        "BOOKMARK",
    }:
        if change == "ERROR":
            raise ValueError("Kubernetes watch ended")
        return None
    obj = event.get("object")
    if not isinstance(obj, dict):
        return None
    meta = obj.get("metadata") if isinstance(obj.get("metadata"), dict) else {}
    status = obj.get("status") if isinstance(obj.get("status"), dict) else {}
    state = "unknown"
    conditions = (
        status.get("conditions") if isinstance(status.get("conditions"), list) else []
    )
    if resource in {"pods", "nodes"}:
        ready = next(
            (
                c.get("status")
                for c in conditions
                if isinstance(c, dict) and c.get("type") == "Ready"
            ),
            None,
        )
        if isinstance(ready, str) and ready in {"True", "False"}:
            state = "healthy" if ready == "True" else "degraded"
    return {
        "type": "kubernetes_watch",
        "resource": resource,
        "change": change,
        "object": {
            "id": meta.get("uid") if isinstance(meta.get("uid"), str) else "",
            "name": meta.get("name") if isinstance(meta.get("name"), str) else "",
            "namespace": (
                meta.get("namespace") if isinstance(meta.get("namespace"), str) else ""
            ),
            "resource_version": (
                meta.get("resourceVersion")
                if isinstance(meta.get("resourceVersion"), str)
                else ""
            ),
            "kind": obj.get("kind") if isinstance(obj.get("kind"), str) else "",
            "status": state,
        },
    }


async def watch_objects(reader: asyncio.StreamReader) -> AsyncIterator[dict]:
    decoder = json.JSONDecoder()
    utf8 = codecs.getincrementaldecoder("utf-8")()
    buffer = ""
    while True:
        chunk = await reader.read(65536)
        if not chunk:
            buffer += utf8.decode(b"", final=True)
            if buffer.strip():
                raise ValueError("Incomplete watch object")
            return
        buffer += utf8.decode(chunk)
        if len(buffer.encode("utf-8")) > MAX_OBJECT_BYTES:
            raise ValueError("Watch object limit exceeded")
        while buffer.strip():
            buffer = buffer.lstrip()
            try:
                obj, end = decoder.raw_decode(buffer)
            except json.JSONDecodeError:
                break
            buffer = buffer[end:]
            if isinstance(obj, dict):
                yield obj


async def log_lines(reader: asyncio.StreamReader) -> AsyncIterator[str]:
    while True:
        line = await reader.readline()
        if not line:
            return
        if len(line) > MAX_LINE_BYTES:
            raise ValueError("Log line limit exceeded")
        text = line.decode("utf-8", errors="replace").rstrip("\r\n")
        text = _JSON_SECRET.sub(lambda match: match[1] + '"<redacted>"', text)
        yield redact_secret_line(text)


async def _spawn(argv: list[str], ctx: EnvContext) -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(
        *argv,
        env=ctx.subprocess_env(),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        limit=MAX_OBJECT_BYTES,
    )


async def source_stream(
    argv: list[str],
    ctx: EnvContext,
    environment_id: str,
    *,
    resource: str | None,
    namespace: str | None = None,
    pod: str | None = None,
    container: str | None = None,
    authorized: Callable[[], bool],
    release: Callable[[], None],
) -> AsyncIterator[str]:
    """Backpressure travels from HTTP → bounded StreamReader → process pipe.

    No unbounded queues, stderr echo, retries disguised as continuous watches,
    or default-cluster fallback. Each EOF/error requires a new source bootstrap.
    """
    process = None
    pending = None
    epoch = str(uuid.uuid4())
    sequence = 0
    started = checked = time.monotonic()

    def frame(topic: str, payload: dict) -> str:
        nonlocal sequence
        sequence += 1
        return (
            "data: "
            + json.dumps(
                {
                    "topic": topic,
                    "payload": payload,
                    "sequence": sequence,
                    "epoch": epoch,
                }
            )
            + "\n\n"
        )

    try:
        process = await _spawn(argv, ctx)
        assert process.stdout is not None
        source = (
            watch_objects(process.stdout) if resource else log_lines(process.stdout)
        )
        yield frame(
            "stream",
            {
                "type": "connected",
                "source": "kubernetes_watch" if resource else "pod_logs",
                "resync_required": True,
            },
        )
        while True:
            if time.monotonic() - started >= MAX_SESSION_SECONDS:
                yield frame(
                    "stream",
                    {
                        "type": "resync",
                        "reason": "session_expired",
                        "resync_required": True,
                    },
                )
                break
            if time.monotonic() - checked >= HEARTBEAT_SECONDS:
                if not await asyncio.to_thread(authorized):
                    yield frame("stream", {"type": "error", "reason": "access_revoked"})
                    break
                checked = time.monotonic()
            if pending is None:
                pending = asyncio.create_task(anext(source))
            done, _ = await asyncio.wait({pending}, timeout=HEARTBEAT_SECONDS)
            if not done:
                yield ": hb\n\n"
                continue
            try:
                item = pending.result()
            except StopAsyncIteration:
                code = await process.wait()
                yield frame(
                    "stream",
                    {
                        "type": "complete" if code == 0 else "error",
                        "reason": (
                            "source_closed" if code == 0 else "source_unavailable"
                        ),
                        "resync_required": True,
                    },
                )
                break
            finally:
                pending = None
            payload = (
                watch_metadata(item, resource)
                if resource
                else {
                    "type": "pod_log",
                    "namespace": namespace,
                    "pod": pod,
                    "container": container,
                    "line": item,
                }
            )
            if payload is not None:
                yield frame(f"env:{environment_id}", payload)
    except (OSError, ValueError, UnicodeError):
        yield frame(
            "stream",
            {"type": "error", "reason": "source_unavailable", "resync_required": True},
        )
    finally:
        if pending is not None:
            pending.cancel()
            with contextlib.suppress(asyncio.CancelledError, StopAsyncIteration):
                await pending
        if process is not None and process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=3)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                await process.wait()
        ctx.cleanup()
        release()
