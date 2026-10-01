"""Deploy-host terminal: an interactive ssh shell pty-bridged over WebSocket.

Operators open a tab in the portal and get a shell on the environment's deploy
host without leaving the app. The server spawns a pty running
``ssh -o BatchMode=yes -o ConnectTimeout=10 <user>@<host>`` and bridges frames:

  client → server  {"type": "input", "data": "..."}        → pty stdin
                   {"type": "resize", "cols": n, "rows": n} → TIOCSWINSZ
  server → client  {"type": "output", "data": "..."}        ← pty stdout
                   {"type": "exit", "code": n|null}         pty process ended

Guardrails: admin role minimum, via a single-use ``?ticket=`` (minted at
``POST /api/v1/auth/ticket``) or the standard headers — browsers cannot set
headers on WebSocket, and raw ``?token=`` credentials are no longer accepted
because query strings land in access logs; one session per
(user, environment) — a second connect replaces the first; a 15
minute idle timeout; the pty is killed when the socket closes; and there is
NO free-form command and NO local-shell fallback — the v1 target is
always the env's deploy host (``deployer_ssh_host`` +
``deployer_ssh_user``, default root); missing deploy host closes with
CLOSE_NO_DEPLOYER. Opens and
closes are written to the audit log. ``settings.terminal_command_override``
replaces the ssh argv wholesale (tests point it at ``/bin/cat`` so CI needs
no real host).
"""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import fcntl
import logging
import os
import pty
import shlex
import signal
import struct
import subprocess
import termios
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect
from sqlalchemy.orm import Session

from app.auth import ROLE_RANK
from app.config import get_settings
from app.db import SessionLocal
from app.deps import check_tenant_access, principal_from_token
from app.models import Environment
from app.schemas import Principal
from app.services import tickets
from app.services.job_runner import JobRunner

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["terminal"])

# WS close codes (4000-4999 are application-defined).
CLOSE_AUTH_FAILED = 4401
CLOSE_FORBIDDEN = 4403
CLOSE_NOT_FOUND = 4404
CLOSE_NO_DEPLOYER = 4400
CLOSE_REPLACED = 4000
CLOSE_IDLE = 4001
CLOSE_SPAWN_FAILED = 1011

IDLE_TIMEOUT_SECONDS = 15 * 60
_IDLE_CHECK_INTERVAL = 15.0
_READ_CHUNK = 65536
_DEFAULT_COLS = 80
_DEFAULT_ROWS = 24

# One live session per (username, environment_id); a new connect replaces.
_sessions: dict[tuple[str, str], "TerminalSession"] = {}


def _reset_sessions() -> None:
    """Tests: drop registry state between cases (live sessions are closed)."""
    for session in list(_sessions.values()):
        session.replaced = True
    _sessions.clear()


def _resolve_ws_principal(ticket: str | None, headers: Any, db: Session) -> Principal | None:
    """Authenticate a browser WebSocket.

    Browsers cannot set headers on the WebSocket handshake, so the UI passes a
    single-use ``?ticket=`` (from ``POST /api/v1/auth/ticket``) instead of a
    raw credential — query strings land in access logs, so ``?token=`` is no
    longer accepted here. Header credentials (X-API-Key / Authorization:
    Bearer) still work for non-browser clients, resolved via
    ``principal_from_token``.
    """
    if ticket and ticket.strip():
        return tickets.consume_ticket(ticket)
    raw: str | None = None
    api_key = headers.get("x-api-key")
    if api_key:
        raw = api_key.strip()
    else:
        authz = headers.get("authorization", "")
        if authz.lower().startswith("bearer "):
            raw = authz[7:].strip() or None
    if raw is None:
        if get_settings().dev_auto_login:
            # DEV ONLY — mirrors app.auth.resolve_principal.
            return Principal(
                username="dev-auto-login",
                role="admin",
                auth_method="dev_auto_login",
                platform_admin=True,
            )
        return None
    try:
        return principal_from_token(raw, db)
    except HTTPException:
        return None


def _write_audit(actor: str, action: str, env_id: str, details: dict[str, Any]) -> None:
    """Audit entry on its own session — the WS outlives any request session."""
    db = SessionLocal()
    try:
        JobRunner(db).write_audit(
            actor=actor,
            action=action,
            resource_type="environment",
            resource_id=env_id,
            environment_id=env_id,
            details=details,
            success=True,
        )
        db.commit()
    except Exception:  # noqa: BLE001 — audit must never break the session
        log.warning("terminal audit write failed (%s)", action, exc_info=True)
    finally:
        db.close()


def _target_argv(env_host: str, env_user: str | None) -> list[str]:
    """Command the pty runs: the ssh to the deploy host (or the test override).

    There is intentionally NO local-shell fallback: an empty deploy host must
    be rejected by the websocket handler (CLOSE_NO_DEPLOYER) before spawn.
    This helper refuses to synthesize ``/bin/bash`` (or any local shell) when
    host is missing — fail closed.
    """
    override = (get_settings().terminal_command_override or "").strip()
    if override:
        return shlex.split(override)
    host = (env_host or "").strip()
    if not host:
        raise ValueError("terminal requires a deploy host; local-shell fallback removed")
    user = (env_user or "").strip() or "root"
    # Defense in depth: username must stay a single ssh destination component.
    if (
        user.startswith("-")
        or " " in user
        or "\t" in user
        or "@" in user
        or "proxycommand" in user.lower()
    ):
        raise ValueError("refusing unsafe deployer_ssh_user for terminal ssh")
    return [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        f"{user}@{host}",
    ]


def _set_winsize(fd: int, cols: int, rows: int) -> None:
    cols = max(1, min(int(cols), 1000))
    rows = max(1, min(int(rows), 1000))
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


class TerminalSession:
    """One pty ↔ WebSocket bridge.

    The pty master fd is registered with the event loop's ``add_reader``;
    readable data is queued (decode-incremental utf-8) and a forwarder task
    streams it to the socket, preserving order. Input frames write straight
    to the master fd. ``finish`` is idempotent and always kills the pty.
    """

    def __init__(self, ws: WebSocket, actor: str, env_id: str, target: str) -> None:
        self.ws = ws
        self.actor = actor
        self.env_id = env_id
        self.target = target
        self.loop = asyncio.get_running_loop()
        self.last_activity = time.monotonic()
        self.replaced = False
        self._done = False
        self._proc: subprocess.Popen[bytes] | None = None
        self._master_fd: int | None = None
        self._queue: asyncio.Queue[str | None] = asyncio.Queue()
        self._tasks: list[asyncio.Task[Any]] = []
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

    def spawn(self, argv: list[str]) -> None:
        master_fd, slave_fd = pty.openpty()
        try:
            env = dict(os.environ)
            env["TERM"] = "xterm-256color"
            self._proc = subprocess.Popen(  # noqa: S603 — argv is fixed, no shell
                argv,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                env=env,
                start_new_session=True,
                close_fds=True,
            )
        finally:
            os.close(slave_fd)
        self._master_fd = master_fd
        _set_winsize(master_fd, _DEFAULT_COLS, _DEFAULT_ROWS)
        self.loop.add_reader(master_fd, self._on_pty_readable)

    def _on_pty_readable(self) -> None:
        """Event-loop callback: pty has data (or EOF). Runs on the loop thread."""
        assert self._master_fd is not None
        try:
            chunk = os.read(self._master_fd, _READ_CHUNK)
        except OSError:
            chunk = b""
        if not chunk:
            # EOF: the child exited and its side of the pty is closed.
            self._queue.put_nowait(None)
            return
        self._queue.put_nowait(self._decoder.decode(chunk))

    def write_input(self, data: str) -> None:
        self.last_activity = time.monotonic()
        if self._master_fd is None:
            return
        try:
            os.write(self._master_fd, data.encode("utf-8", errors="replace"))
        except OSError:
            pass

    def resize(self, cols: int, rows: int) -> None:
        self.last_activity = time.monotonic()
        if self._master_fd is not None:
            with contextlib.suppress(OSError):
                _set_winsize(self._master_fd, cols, rows)

    async def run(self) -> None:
        """Forward pty output, watch for idle, and pump client frames."""
        self._tasks = [
            asyncio.create_task(self._forward_output()),
            asyncio.create_task(self._idle_watchdog()),
        ]
        try:
            while True:
                frame = await self.ws.receive_json()
                if not isinstance(frame, dict):
                    continue
                ftype = frame.get("type")
                if ftype == "input":
                    data = frame.get("data")
                    if isinstance(data, str) and data:
                        self.write_input(data)
                elif ftype == "resize":
                    try:
                        cols, rows = int(frame.get("cols")), int(frame.get("rows"))
                    except (TypeError, ValueError):
                        continue
                    self.resize(cols, rows)
                # Unknown frame types are ignored.
        except WebSocketDisconnect:
            pass
        except Exception:  # noqa: BLE001 — never let a session crash the app
            log.warning("terminal session error (env %s)", self.env_id, exc_info=True)
        finally:
            await self.finish(reason="client disconnected")

    async def _forward_output(self) -> None:
        """Queue → socket; the None sentinel means the pty hit EOF."""
        while True:
            item = await self._queue.get()
            if item is None:
                code = self._proc.poll() if self._proc is not None else None
                try:
                    await self.ws.send_json({"type": "exit", "code": code})
                except Exception:  # noqa: BLE001 — client may already be gone
                    pass
                await self.finish(reason="process exited", close_ws=True)
                return
            try:
                await self.ws.send_json({"type": "output", "data": item})
            except Exception:  # noqa: BLE001 — socket dead; run() will finish
                return

    async def _idle_watchdog(self) -> None:
        while True:
            await asyncio.sleep(_IDLE_CHECK_INTERVAL)
            if time.monotonic() - self.last_activity >= IDLE_TIMEOUT_SECONDS:
                await self.finish(reason="idle timeout", code=CLOSE_IDLE, close_ws=True)
                return

    def _reap_proc(self) -> None:
        """Worker-thread reaper: wait briefly, then force-kill the pty child."""
        assert self._proc is not None
        try:
            self._proc.wait(timeout=3)
        except Exception:  # noqa: BLE001 — still alive; force-kill the group
            with contextlib.suppress(OSError, ProcessLookupError):
                os.killpg(self._proc.pid, signal.SIGKILL)

    async def finish(
        self,
        reason: str,
        code: int = 1000,
        close_ws: bool = False,
    ) -> None:
        """Tear down exactly once: kill the pty, stop tasks, audit the close.

        Everything except the optional socket close is synchronous: when the
        handler task is being cancelled (e.g. test-client teardown), a
        suspension point here would abort cleanup mid-way, so the process
        reaping is handed to a worker thread instead of awaited.
        """
        if self._done:
            return
        self._done = True
        for task in self._tasks:
            task.cancel()
        if self._master_fd is not None:
            try:
                self.loop.remove_reader(self._master_fd)
            except Exception:  # noqa: BLE001 — loop may be closing
                pass
            try:
                os.close(self._master_fd)
            except OSError:
                pass
            self._master_fd = None
        if self._proc is not None and self._proc.poll() is None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                # The pty child is a session leader; kill its group so the
                # remote-end ssh session dies with it.
                os.killpg(self._proc.pid, signal.SIGTERM)
            self.loop.run_in_executor(None, self._reap_proc)
        if _sessions.get((self.actor, self.env_id)) is self:
            _sessions.pop((self.actor, self.env_id), None)
        _write_audit(
            self.actor,
            "env.terminal.close",
            self.env_id,
            {"target": self.target, "reason": reason},
        )
        log.info(
            "terminal closed: actor=%s env=%s target=%s reason=%s",
            self.actor,
            self.env_id,
            self.target,
            reason,
        )
        if close_ws:
            try:
                await self.ws.close(code=code)
            except Exception:  # noqa: BLE001 — already closed
                pass


async def _reject(ws: WebSocket, code: int, reason: str) -> None:
    log.warning("terminal connect rejected: %s", reason)
    await ws.close(code=code, reason=reason)


@router.websocket("/terminal")
async def terminal_ws(
    ws: WebSocket,
    environment_id: str = Query(default=""),
    ticket: str | None = Query(default=None),
) -> None:
    await ws.accept()

    db = SessionLocal()
    try:
        principal = _resolve_ws_principal(ticket, ws.headers, db)
        if principal is None:
            await _reject(ws, CLOSE_AUTH_FAILED, "missing or invalid credentials")
            return
        if ROLE_RANK[principal.role] < ROLE_RANK["admin"]:
            await _reject(ws, CLOSE_FORBIDDEN, f"role '{principal.role}' insufficient")
            return
        env = db.get(Environment, environment_id) if environment_id else None
        if env is None:
            await _reject(ws, CLOSE_NOT_FOUND, "environment not found")
            return
        try:
            check_tenant_access(db, principal, env.tenant_id, "admin")
        except HTTPException:
            await _reject(ws, CLOSE_FORBIDDEN, "no admin access to this environment")
            return
        host = (env.deployer_ssh_host or "").strip()
        user = (env.deployer_ssh_user or "").strip() or "root"
        env_id = env.id
    finally:
        db.close()

    if not host:
        await _reject(ws, CLOSE_NO_DEPLOYER, "environment has no deploy host configured")
        return

    target = f"{user}@{host}"
    key = (principal.username, env_id)

    # One session per (user, env): a second connect replaces the first.
    old = _sessions.pop(key, None)
    if old is not None:
        old.replaced = True
        await old.finish(reason="replaced by a new session", code=CLOSE_REPLACED, close_ws=True)

    session = TerminalSession(ws, principal.username, env_id, target)
    try:
        argv = _target_argv(host, user)
        session.spawn(argv)
    except ValueError as exc:
        await _reject(ws, CLOSE_NO_DEPLOYER, str(exc))
        return
    except (OSError, FileNotFoundError) as exc:
        await _reject(ws, CLOSE_SPAWN_FAILED, f"failed to spawn ssh: {exc}")
        return
    _sessions[key] = session
    _write_audit(
        principal.username,
        "env.terminal.open",
        env_id,
        {"target": target, "auth_method": principal.auth_method},
    )
    log.info("terminal opened: actor=%s env=%s target=%s", principal.username, env_id, target)
    await session.run()
