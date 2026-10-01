"""DB-backed agent command relay (API process) + sync client (any process).

Live agent connections (WebSockets) live in the API process, but the callers
that need agent execution — job handlers — run in the worker daemon process.
Like the SSE stream relay (app/services/relay.py), SQLite is the shared
medium: callers insert ``agent_commands`` rows with :func:`agent_exec`, and
the relay — started from the API lifespan like the other daemons — polls for
``pending`` rows, claims them, dispatches the matching frame to the env's
connected agent, streams agent ``log`` frames into the row's ``log_text``
(append-only), and records the result payload + terminal status.

Wire shapes (locked with the agent):

    hub -> agent  command{id, cmd[], cwd?, env?, timeout}
    hub -> agent  command{id, scan_bmc{subnet}, timeout}
    hub -> agent  file_write{id, path, b64, mode, backup_dir}
    agent -> hub  log{id, line}
    agent -> hub  result{id, rc, stdout?, stderr?, found?, error?}

Row lifecycle: pending -> dispatched -> done | failed | timeout. A row whose
env has no connected agent fails immediately with
``{"error": "no agent connected for this env"}``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models import AgentCommand
from app.services import agents as agents_service

log = logging.getLogger(__name__)

LogFn = Callable[[str], None]

COMMAND_KINDS = frozenset({"run_command", "scan_bmc", "file_write"})

# Terminal row states; pending/dispatched are in flight.
TERMINAL_STATUSES = frozenset({"done", "failed", "timeout"})

# Default result wait for file_write rows (their payload carries no timeout).
FILE_WRITE_TIMEOUT_SECONDS = 300.0

# Default result wait for run_command rows without an explicit payload timeout.
DEFAULT_COMMAND_TIMEOUT_SECONDS = 600.0

# Grace added to the caller's timeout so a slow-to-claim row still completes.
WAIT_GRACE_SECONDS = 30.0

# How often log frames buffered on the event loop are flushed onto the row.
LOG_FLUSH_INTERVAL_SECONDS = 0.5


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AgentRelay:
    """Poll ``agent_commands`` for pending rows and dispatch them to agents."""

    def __init__(
        self,
        session_factory: Callable[[], Session] = SessionLocal,
        interval: float = 2.0,
    ):
        self._session_factory = session_factory
        self._interval = interval
        # row_id -> concurrent future of the dispatch coroutine
        self._in_flight: dict[str, Any] = {}

    async def run(self) -> None:
        """Poll loop: one iteration, sleep, repeat; cancel-clean."""
        try:
            while True:
                await self.poll_once()
                await asyncio.sleep(self._interval)
        except asyncio.CancelledError:
            log.info("agent command relay stopped")
            raise

    async def poll_once(self) -> None:
        """One iteration: reap finished dispatches, claim and dispatch pending.

        A failed poll is logged and skipped — the relay keeps running.
        """
        self._reap()
        try:
            rows = await asyncio.to_thread(self._pending_rows)
        except Exception:  # noqa: BLE001
            log.exception("agent command relay poll failed")
            return
        for row_id, env_id, kind, payload in rows:
            if row_id in self._in_flight:
                continue
            try:
                self._dispatch(row_id, env_id, kind, payload)
            except Exception:  # noqa: BLE001 — one bad row must not stall the poll
                log.exception("agent command %s dispatch failed", row_id)
                await asyncio.to_thread(
                    self._finish, row_id, "failed", {"error": "relay dispatch error"}
                )

    # --------------------------------------------------------- sync DB work

    def _pending_rows(self) -> list[tuple[str, str, str, dict[str, Any]]]:
        db = self._session_factory()
        try:
            rows = db.scalars(
                select(AgentCommand)
                .where(AgentCommand.status == "pending")
                .order_by(AgentCommand.created_at.asc())
            ).all()
            return [
                (row.id, row.environment_id, row.kind, dict(row.payload or {}))
                for row in rows
            ]
        finally:
            db.close()

    def _claim(self, row_id: str) -> bool:
        """Atomically flip pending -> dispatched; False when already claimed."""
        db = self._session_factory()
        try:
            result = db.execute(
                update(AgentCommand)
                .where(AgentCommand.id == row_id)
                .where(AgentCommand.status == "pending")
                .values(status="dispatched")
            )
            db.commit()
            if result.rowcount != 1:
                db.rollback()
                return False
            return True
        finally:
            db.close()

    def _finish(self, row_id: str, status: str, result: dict[str, Any]) -> None:
        """Write the terminal status + result; never overwrites a terminal row."""
        db = self._session_factory()
        try:
            row = db.get(AgentCommand, row_id)
            if row is None or row.status in TERMINAL_STATUSES:
                return
            row.status = status
            row.result = result
            row.finished_at = _utcnow()
            db.commit()
        finally:
            db.close()

    def _append_logs_sync(self, row_id: str, lines: list[str]) -> None:
        db = self._session_factory()
        try:
            row = db.get(AgentCommand, row_id)
            if row is not None:
                row.log_text = (row.log_text or "") + "".join(
                    f"{line}\n" for line in lines
                )
                db.commit()
        finally:
            db.close()

    # ------------------------------------------------------------- dispatch

    def _reap(self) -> None:
        self._in_flight = {
            row_id: future
            for row_id, future in self._in_flight.items()
            if not future.done()
        }

    def _dispatch(
        self,
        row_id: str,
        env_id: str,
        kind: str,
        payload: dict[str, Any],
        *,
        retried: bool = False,
        exclude: frozenset[str] = frozenset(),
    ) -> None:
        record = agents_service.registry.record_for_env(env_id, exclude=exclude)
        if record is None:
            self._finish(row_id, "failed", {"error": "no agent connected for this env"})
            return
        if not retried and not self._claim(row_id):
            # Another relay pass (or a second relay) claimed it first.
            return
        frame = self._frame_for(row_id, kind, payload)
        timeout = self._timeout_for(kind, payload)
        coro = self._run_on_agent(record, row_id, frame, timeout)
        try:
            future = asyncio.run_coroutine_threadsafe(coro, record.loop)
        except RuntimeError:
            # The agent's loop went away between the registry check and the
            # submit (disconnect): fail the row cleanly.
            coro.close()
            self._finish(row_id, "failed", {"error": "no agent connected for this env"})
            return
        future.add_done_callback(
            lambda fut, rid=row_id: self._on_done(
                rid,
                fut,
                env_id,
                kind,
                payload,
                retried=retried,
                failed_agent=record.agent_id,
            )
        )
        self._in_flight[row_id] = future

    def _on_done(
        self,
        row_id: str,
        future: Any,
        env_id: str,
        kind: str,
        payload: dict[str, Any],
        *,
        retried: bool,
        failed_agent: str,
    ) -> None:
        """Translate the dispatch future into the row's terminal state.

        A dispatch failure (agent dropped mid-command, send failed) retries
        the row exactly once on another connected agent for the env before
        the row is failed; timeouts are not retried — the command may
        already have run on the first agent.
        """
        try:
            result_payload = future.result()
        except agents_service.AgentCommandTimeout as exc:
            self._finish(row_id, "timeout", {"error": str(exc)})
            return
        except Exception as exc:  # noqa: BLE001 — dispatch failure; fail over once
            if not retried:
                log.info(
                    "agent command %s: dispatch via agent %s failed (%s); "
                    "failing over to another agent",
                    row_id,
                    failed_agent,
                    exc,
                )
                self._dispatch(
                    row_id,
                    env_id,
                    kind,
                    payload,
                    retried=True,
                    exclude=frozenset({failed_agent}),
                )
                return
            log.warning("agent command %s raised", row_id, exc_info=True)
            self._finish(row_id, "failed", {"error": f"agent dispatch failed: {exc}"})
            return
        result: dict[str, Any] = {"rc": result_payload.get("rc")}
        for key in ("stdout", "stderr", "found", "error"):
            if result_payload.get(key) is not None:
                result[key] = result_payload[key]
        self._finish(row_id, "done", result)

    async def _run_on_agent(
        self,
        record: agents_service.AgentRecord,
        row_id: str,
        frame: dict[str, Any],
        timeout: float,
    ) -> dict[str, Any]:
        """Send the frame on the agent's loop and stream log frames to the row.

        Log lines are buffered and flushed at most every
        LOG_FLUSH_INTERVAL_SECONDS (plus once at the end) so a chatty command
        does not turn into a sqlite commit per line on the API loop.
        """
        buffer: list[str] = []
        last_flush = record.loop.time()

        def log_cb(line: str) -> None:
            nonlocal last_flush
            buffer.append(line)
            now = record.loop.time()
            if now - last_flush >= LOG_FLUSH_INTERVAL_SECONDS:
                last_flush = now
                lines, buffer[:] = buffer[:], []
                record.loop.create_task(self._flush_logs(row_id, lines))

        try:
            return await agents_service.registry.transact(
                record, frame, timeout=timeout, log_cb=log_cb
            )
        finally:
            if buffer:
                lines, buffer[:] = buffer[:], []
                await self._flush_logs(row_id, lines)

    async def _flush_logs(self, row_id: str, lines: list[str]) -> None:
        try:
            await asyncio.to_thread(self._append_logs_sync, row_id, lines)
        except Exception:  # noqa: BLE001 — log loss must not kill a dispatch
            log.warning("agent command %s: log flush failed", row_id, exc_info=True)

    # ------------------------------------------------------------- frames

    @staticmethod
    def _frame_for(row_id: str, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Build the hub -> agent frame; the frame id is the row id."""
        if kind == "file_write":
            return {
                "type": "file_write",
                "id": row_id,
                "path": payload.get("path"),
                "b64": payload.get("b64"),
                "mode": payload.get("mode"),
                "backup_dir": payload.get("backup_dir"),
            }
        if kind == "scan_bmc":
            # Wire shape (locked with the agent): a command frame carrying a
            # scan_bmc payload; the agent replies rc + found (+ error).
            return {
                "type": "command",
                "id": row_id,
                "scan_bmc": {"subnet": str(payload.get("subnet") or "")},
                "timeout": payload.get("timeout"),
            }
        # run_command
        frame: dict[str, Any] = {
            "type": "command",
            "id": row_id,
            "cmd": [str(c) for c in payload.get("cmd") or []],
            "timeout": payload.get("timeout"),
        }
        if payload.get("cwd"):
            frame["cwd"] = str(payload["cwd"])
        if payload.get("env"):
            frame["env"] = {str(k): str(v) for k, v in dict(payload["env"]).items()}
        return frame

    @staticmethod
    def _timeout_for(kind: str, payload: dict[str, Any]) -> float:
        if kind == "file_write":
            return FILE_WRITE_TIMEOUT_SECONDS
        try:
            return float(payload.get("timeout") or DEFAULT_COMMAND_TIMEOUT_SECONDS)
        except (TypeError, ValueError):
            return DEFAULT_COMMAND_TIMEOUT_SECONDS


async def start_agent_relay(
    app: Any, session_factory: Callable[[], Session], interval: float
) -> asyncio.Task:
    """Create the relay task and register it on app.state for shutdown.

    The lifespan shutdown path cancels ``app.state.agent_relay_task``.
    """
    relay = AgentRelay(session_factory, interval)
    task = asyncio.create_task(relay.run(), name="agent-command-relay")
    app.state.agent_relay_task = task
    return task


def agent_exec(
    env_id: str,
    kind: str,
    payload: dict[str, Any],
    *,
    timeout: float = DEFAULT_COMMAND_TIMEOUT_SECONDS,
    log_cb: LogFn | None = None,
    session_factory: Callable[[], Session] = SessionLocal,
    poll_interval: float = 1.0,
) -> dict[str, Any]:
    """Enqueue an agent command row and block until it reaches a terminal status.

    Works from ANY process — the dispatch happens in the API process where
    the agent's websocket lives. Polls the row every ``poll_interval`` seconds
    (forwarding newly appended log_text lines to ``log_cb``) and returns the
    stored result payload: ``{rc, stdout, stderr}`` for run_command,
    ``{rc, found, error?}`` for scan_bmc, ``{rc, error?}`` for file_write,
    or ``{error}`` on dispatch failure/timeout.
    """
    if kind not in COMMAND_KINDS:
        raise ValueError(
            f"agent_exec: unknown kind {kind!r} (valid: {sorted(COMMAND_KINDS)})"
        )

    db = session_factory()
    try:
        row = AgentCommand(environment_id=env_id, kind=kind, payload=dict(payload))
        db.add(row)
        db.commit()
        row_id = row.id
    finally:
        db.close()

    deadline = time.monotonic() + float(timeout) + WAIT_GRACE_SECONDS
    seen = 0
    while True:
        db = session_factory()
        try:
            current = db.get(AgentCommand, row_id)
            status = current.status if current is not None else "failed"
            log_text = (current.log_text or "") if current is not None else ""
            result = (
                dict(current.result or {})
                if current is not None
                else {"error": "agent command row vanished"}
            )
        finally:
            db.close()

        if log_cb is not None and len(log_text) > seen:
            chunk = log_text[seen:]
            seen = len(log_text)
            for line in chunk.splitlines():
                log_cb(line)

        if status in TERMINAL_STATUSES:
            return result

        if time.monotonic() >= deadline:
            # Best-effort: mark the still-open row so it does not linger.
            db = session_factory()
            try:
                db.execute(
                    update(AgentCommand)
                    .where(AgentCommand.id == row_id)
                    .where(AgentCommand.status.notin_(TERMINAL_STATUSES))
                    .values(status="timeout", finished_at=_utcnow())
                )
                db.commit()
            finally:
                db.close()
            return {"error": f"agent command timed out after {timeout}s"}

        time.sleep(poll_interval)
