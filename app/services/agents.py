"""Agent channel — token helpers and the in-memory registry of live agents.

Protocol (locked, see docs/plan): an agent inside an environment dials OUT to
``WS /api/v1/agents/connect?token=<raw>``. The hub verifies ``sha256(token)``
against the stored :class:`~app.models.AgentCredential`, sends a
``challenge{nonce}``, and the agent proves possession with
``proof{hmac: HMAC-SHA256(token, nonce)}``. The raw token is only ever seen by
the hub transiently in the connect query string — only its hash is stored.

Live connection state never touches the DB: this module's ``AgentRegistry``
singleton maps agent_id → websocket, metadata, and pending command queues,
plus env → the set of connected agent_ids for routing. An environment may
have several agents connected (HA); commands are routed round-robin across
them and a dispatch failure fails over once to another connected agent.
``last_seen`` / ``hostname`` / ``version`` are mirrored onto the credential
row as frames arrive. An agent with no frame for ``OFFLINE_AFTER_SECONDS``
reports as offline.

The job runner is synchronous, so the registry offers ``run_command_sync``:
the coroutine is submitted to the event loop that owns the agent's websocket
(``asyncio.run_coroutine_threadsafe``) and the caller blocks on the
concurrent future. Async callers (same loop) can use ``run_command``
directly. ``run_scan_sync`` is the same wrapper for ``scan_bmc`` frames
(BMC subnet sweeps, ``baremetal.bmc_scan`` op).

Agents also report discovery sightings as ``event{kind, payload}`` frames
(``pxe_request`` / ``bmc_found``); ``handle_agent_event`` validates and
upserts them into the env's discovery inbox via a short-lived session.

This module also orchestrates the ``agent.install`` op (``install_agent``):
push-install the agent onto a host over ssh with a freshly issued credential
(``create_credential`` — the same per-name replace-on-create path the token
endpoint uses), curl-pipe first with an ssh-stdin fallback.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac as hmac_mod
import ipaddress
import logging
import secrets
import shlex
import threading
import time
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Collection
from urllib.parse import urlparse
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models import AgentCredential, DiscoveredBmc, DiscoveredNode, Environment

if TYPE_CHECKING:
    from app.config import Settings

log = logging.getLogger(__name__)

LogFn = Callable[[str], None]

TOKEN_PREFIX = "gsca_"

# Hub marks an agent offline after this much frame silence (heartbeats are 15s).
OFFLINE_AFTER_SECONDS = 45.0

# Max wait for the agent's proof frame after the challenge is sent.
HANDSHAKE_TIMEOUT_SECONDS = 10.0

# Grace added on top of the command timeout when the sync side blocks on the
# future, so the in-loop timeout fires first and produces a clean error.
SYNC_TIMEOUT_GRACE_SECONDS = 30.0

# Raw tokens are masked with this in job logs — logs persist, tokens must not.
TOKEN_MASK = "gsca_***"

# Packaged curl-pipe installer (same file GET /agent serves), streamed over
# ssh stdin when the target cannot pull it from the hub itself.
_INSTALL_SCRIPT = Path(__file__).resolve().parents[2] / "agent" / "install.sh"

# agent.command v1 allowlist: param value -> argv executed by the agent.
AGENT_COMMAND_ALLOWLIST: dict[str, list[str]] = {
    "uptime": ["uptime"],
    "hostname": ["hostname"],
    "ip addr": ["ip", "addr"],
    "talosctl version": ["talosctl", "version"],
    "kubectl get nodes": ["kubectl", "get", "nodes"],
    "ls /etc/genestack": ["ls", "/etc/genestack"],
}


def generate_token() -> str:
    """New raw enrollment token; shown once, only sha256(token) is stored."""
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def proof_for(token: str, nonce: str) -> str:
    """Expected handshake proof: HMAC-SHA256(raw token, nonce), hex digest."""
    return hmac_mod.new(
        token.encode("utf-8"), nonce.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def verify_proof(token: str, nonce: str, proof: str) -> bool:
    return hmac_mod.compare_digest(proof_for(token, nonce), proof)


class AgentUnavailableError(RuntimeError):
    """No agent connected (or it dropped) for the requested environment."""


class AgentCommandTimeout(AgentUnavailableError):
    """The agent did not return a result frame within the command timeout."""


@dataclass
class AgentRecord:
    """Live state for one connected agent (in-memory only)."""

    agent_id: str
    env_id: str
    ws: Any  # starlette WebSocket
    loop: asyncio.AbstractEventLoop
    connected_at: float = field(default_factory=time.monotonic)
    last_frame_at: float = field(default_factory=time.monotonic)
    hostname: str | None = None
    version: str | None = None
    caps: list[str] = field(default_factory=list)
    # cmd_id -> asyncio.Queue receiving ("log", line) / ("result", dict) /
    # ("error", message) items from the websocket receive loop.
    pending: dict[str, asyncio.Queue] = field(default_factory=dict)

    def online(self, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        return (now - self.last_frame_at) <= OFFLINE_AFTER_SECONDS


class AgentRegistry:
    """Process-local registry of connected agents and command routing."""

    def __init__(self) -> None:
        self._records: dict[str, AgentRecord] = {}
        # env_id -> agent_ids in connection order (the routing pool)
        self._by_env: dict[str, list[str]] = {}
        # env_id -> round-robin cursor into the env's agent list
        self._rr_cursor: dict[str, int] = {}
        self._lock = threading.Lock()

    def register(self, record: AgentRecord) -> None:
        with self._lock:
            replaced = self._records.get(record.agent_id)
            self._records[record.agent_id] = record
            ids = self._by_env.setdefault(record.env_id, [])
            if record.agent_id not in ids:
                ids.append(record.agent_id)
        if replaced is not None and replaced is not record:
            # The same credential reconnected: the stale connection's pending
            # commands fail; the new connection takes over. Other agents of
            # the env are unaffected.
            self._fail_pending(replaced, "superseded by a new agent connection")

    def unregister(self, record: AgentRecord) -> None:
        with self._lock:
            if self._records.get(record.agent_id) is not record:
                # A newer connection for the same credential superseded us.
                return
            del self._records[record.agent_id]
            ids = self._by_env.get(record.env_id)
            if ids is not None:
                if record.agent_id in ids:
                    ids.remove(record.agent_id)
                if not ids:
                    del self._by_env[record.env_id]
                    self._rr_cursor.pop(record.env_id, None)
        self._fail_pending(record, "agent disconnected")

    @staticmethod
    def _fail_pending(record: AgentRecord, message: str) -> None:
        for q in record.pending.values():
            try:
                q.put_nowait(("error", message))
            except Exception:  # noqa: BLE001 — queue/loop may already be gone
                pass
        record.pending.clear()

    def get(self, agent_id: str) -> AgentRecord | None:
        with self._lock:
            return self._records.get(agent_id)

    def records_for_env(self, env_id: str) -> list[AgentRecord]:
        """All registered agents for the env, in connection order."""
        with self._lock:
            return [
                self._records[agent_id]
                for agent_id in self._by_env.get(env_id, [])
                if agent_id in self._records
            ]

    def record_for_env(
        self, env_id: str, *, exclude: Collection[str] = frozenset()
    ) -> AgentRecord | None:
        """Pick a connected agent for the env (deterministic round-robin).

        Agents are kept in connection order and a per-env cursor advances
        past each pick, so consecutive calls rotate across the env's agents.
        Agents past the offline threshold and ids in ``exclude`` are
        skipped; returns None when no usable agent is connected.
        """
        now = time.monotonic()
        with self._lock:
            ids = [i for i in self._by_env.get(env_id, []) if i not in exclude]
            count = len(ids)
            if count == 0:
                return None
            start = self._rr_cursor.get(env_id, 0) % count
            for offset in range(count):
                idx = (start + offset) % count
                record = self._records.get(ids[idx])
                if record is not None and record.online(now):
                    self._rr_cursor[env_id] = (idx + 1) % count
                    return record
            return None

    def reset(self) -> None:
        """Drop all records (test isolation)."""
        with self._lock:
            records = list(self._records.values())
            self._records.clear()
            self._by_env.clear()
            self._rr_cursor.clear()
        for record in records:
            self._fail_pending(record, "registry reset")

    def route_frame(self, record: AgentRecord, frame: dict[str, Any]) -> None:
        """Deliver a log/result frame to its pending command queue."""
        cmd_id = str(frame.get("id") or "")
        q = record.pending.get(cmd_id)
        if q is None:
            log.warning(
                "agent %s sent %s for unknown command id %r",
                record.agent_id,
                frame.get("type"),
                cmd_id,
            )
            return
        if frame.get("type") == "log":
            q.put_nowait(("log", str(frame.get("line") or "")))
        elif frame.get("type") == "result":
            q.put_nowait(
                (
                    "result",
                    {
                        "id": cmd_id,
                        "rc": frame.get("rc"),
                        "stdout": frame.get("stdout"),
                        "stderr": frame.get("stderr"),
                        # file_write failure detail / scan_bmc result payload
                        "error": frame.get("error"),
                        "found": frame.get("found"),
                    },
                )
            )

    async def _transact(
        self,
        record: AgentRecord,
        frame: dict[str, Any],
        *,
        timeout: float,
        log_cb=None,
    ) -> dict[str, Any]:
        """Send a command/scan frame and stream log/result frames back.

        Must run on the event loop that owns the agent's websocket. Returns
        the result payload ``{id, rc, stdout, stderr, found}``; raises
        :class:`AgentUnavailableError` when the agent is gone and
        :class:`AgentCommandTimeout` when no result arrives in time.
        """
        cmd_id = str(frame["id"])
        q: asyncio.Queue = asyncio.Queue()
        record.pending[cmd_id] = q
        try:
            await record.ws.send_json(frame)
            deadline = record.loop.time() + timeout
            while True:
                remaining = deadline - record.loop.time()
                if remaining <= 0:
                    raise AgentCommandTimeout(
                        f"agent command timed out after {timeout}s"
                    )
                try:
                    kind, payload = await asyncio.wait_for(q.get(), timeout=remaining)
                except TimeoutError as exc:
                    raise AgentCommandTimeout(
                        f"agent command timed out after {timeout}s"
                    ) from exc
                if kind == "log":
                    if log_cb is not None:
                        log_cb(payload)
                elif kind == "result":
                    return payload
                elif kind == "error":
                    raise AgentUnavailableError(payload)
        finally:
            record.pending.pop(cmd_id, None)

    async def transact(
        self,
        record: AgentRecord,
        frame: dict[str, Any],
        *,
        timeout: float,
        log_cb=None,
    ) -> dict[str, Any]:
        """Public wrapper for :meth:`_transact` with a caller-built frame.

        Used by the agent command relay (app/services/agent_relay), which
        builds command/file_write frames whose id is the agent_commands row
        id rather than a fresh uuid.
        """
        return await self._transact(record, frame, timeout=timeout, log_cb=log_cb)

    async def run_command(
        self,
        agent_id: str,
        argv: list[str],
        *,
        timeout: float,
        log_cb=None,
    ) -> dict[str, Any]:
        """Send a command frame for ``argv`` to the agent (see _transact)."""
        record = self.get(agent_id)
        if record is None:
            raise AgentUnavailableError("no agent connected for this env")
        frame = {
            "type": "command",
            "id": uuid4().hex,
            "cmd": list(argv),
            "timeout": timeout,
        }
        return await self._transact(record, frame, timeout=timeout, log_cb=log_cb)

    async def run_scan(
        self,
        agent_id: str,
        subnet: str,
        *,
        timeout: float,
        log_cb=None,
    ) -> dict[str, Any]:
        """Send a scan_bmc command frame for ``subnet`` to the agent.

        Wire shape (locked with the agent): a command frame carrying a
        ``scan_bmc`` payload — ``{"type": "command", "id": ..., "scan_bmc":
        {"subnet": ...}, "timeout": ...}``. The agent sweeps the subnet for
        Redfish BMC endpoints (reporting each as a bmc_found event) and its
        result payload carries ``found`` (count) alongside the usual rc.
        """
        record = self.get(agent_id)
        if record is None:
            raise AgentUnavailableError("no agent connected for this env")
        frame = {
            "type": "command",
            "id": uuid4().hex,
            "scan_bmc": {"subnet": subnet},
            "timeout": timeout,
        }
        return await self._transact(record, frame, timeout=timeout, log_cb=log_cb)

    @staticmethod
    def _await_result(record: AgentRecord, coro: Any, timeout: float) -> dict[str, Any]:
        """Submit ``coro`` to the record's loop and block on the result."""
        try:
            future = asyncio.run_coroutine_threadsafe(coro, record.loop)
        except RuntimeError:
            # The agent's loop went away between pick and submit.
            coro.close()
            raise
        try:
            return future.result(timeout=timeout + SYNC_TIMEOUT_GRACE_SECONDS)
        except FutureTimeoutError as exc:
            raise AgentCommandTimeout(
                f"agent command timed out after {timeout}s"
            ) from exc

    def _run_sync_with_failover(
        self,
        env_id: str,
        build: Callable[[AgentRecord], Any],
        *,
        timeout: float,
    ) -> dict[str, Any]:
        """Run ``build(record)`` on a picked agent's loop; fail over once.

        ``build`` maps a record to the coroutine to run on that record's
        loop (run_command / run_scan). A dispatch failure — the agent
        dropped mid-command, the send failed, the loop is gone — retries
        the command exactly once on another connected agent for the env.
        Timeouts (agent alive but silent) are NOT retried: the command may
        already have run on the first agent.
        """
        record = self.record_for_env(env_id)
        if record is None:
            raise AgentUnavailableError("no agent connected for this env")
        try:
            return self._await_result(record, build(record), timeout)
        except AgentCommandTimeout:
            raise
        except Exception:  # noqa: BLE001 — any dispatch failure triggers failover
            other = self.record_for_env(env_id, exclude={record.agent_id})
            if other is None:
                raise
            log.warning(
                "env %s: dispatch to agent %s failed; failing over to agent %s",
                env_id,
                record.agent_id,
                other.agent_id,
            )
            return self._await_result(other, build(other), timeout)

    def run_command_sync(
        self,
        env_id: str,
        argv: list[str],
        *,
        timeout: float,
        log_cb=None,
    ) -> dict[str, Any]:
        """Sync wrapper for the job runner: route a command to the env's agent.

        Picks a connected agent (round-robin) and blocks the calling
        (worker) thread while the coroutine runs on the loop that owns the
        agent's websocket; on dispatch failure the command fails over once
        to another connected agent (see :meth:`_run_sync_with_failover`).
        """
        return self._run_sync_with_failover(
            env_id,
            lambda record: self.run_command(
                record.agent_id, argv, timeout=timeout, log_cb=log_cb
            ),
            timeout=timeout,
        )

    def run_scan_sync(
        self,
        env_id: str,
        subnet: str,
        *,
        timeout: float,
        log_cb=None,
    ) -> dict[str, Any]:
        """Sync wrapper for the job runner: route a BMC subnet sweep to the
        env's agent. Same blocking/failover semantics as
        :meth:`run_command_sync`.
        """
        return self._run_sync_with_failover(
            env_id,
            lambda record: self.run_scan(
                record.agent_id, subnet, timeout=timeout, log_cb=log_cb
            ),
            timeout=timeout,
        )


# Module-level singleton: one registry per process. Job handlers run in the
# worker daemon, which has no live websockets — they route agent ops through
# the DB-backed relay (app/services/agent_relay.agent_exec) and check
# liveness with agent_available(); the sync wrappers here are used by the
# API process (terminal routes) and by tests.
registry = AgentRegistry()


def credential_for_env(db: Session, env_id: str) -> AgentCredential | None:
    """Latest enrollment credential for the environment, if any."""
    stmt = (
        select(AgentCredential)
        .where(AgentCredential.environment_id == env_id)
        .order_by(AgentCredential.created_at.desc())
    )
    return db.scalars(stmt).first()


def credentials_for_env(db: Session, env_id: str) -> list[AgentCredential]:
    """All enrollment credentials for the environment, oldest first."""
    stmt = (
        select(AgentCredential)
        .where(AgentCredential.environment_id == env_id)
        .order_by(AgentCredential.created_at.asc())
    )
    return list(db.scalars(stmt).all())


def credential_by_token(db: Session, token: str) -> AgentCredential | None:
    stmt = select(AgentCredential).where(
        AgentCredential.token_hash == hash_token(token)
    )
    return db.scalar(stmt)


def create_credential(
    db: Session, env_id: str, name: str = "default"
) -> tuple[AgentCredential, str]:
    """Fresh enrollment credential ``name`` for the env; returns ``(row, raw token)``.

    Credentials are keyed by (env, name): creating with an existing name
    replaces (revokes) that credential only — other named credentials for
    the env are untouched, so an env can enroll several agents (HA). Only
    sha256(token) is stored; the raw token exists only in the returned
    value. The caller commits (and writes the audit entry).
    """
    stmt = select(AgentCredential).where(
        AgentCredential.environment_id == env_id,
        AgentCredential.name == name,
    )
    for old in db.scalars(stmt).all():
        db.delete(old)
    db.flush()  # flush the delete before the insert (env, name) is unique
    token = generate_token()
    cred = AgentCredential(
        environment_id=env_id,
        name=name,
        token_hash=hash_token(token),
    )
    db.add(cred)
    db.flush()
    from app.services.reach import attach_credential_peer

    client_config = attach_credential_peer(db, cred)
    if client_config:
        # Unmapped. The token route reads it before commit. It is not a column.
        cred.wg_client_config = client_config  # type: ignore[attr-defined]
    return cred, token


def advertise_bases(advertise_url: str) -> tuple[str, str]:
    """``(http_base, ws_base)`` for a configured hub advertise URL.

    ``http://192.0.2.1:8080`` -> ``("http://192.0.2.1:8080",
    "ws://192.0.2.1:8080")``; https maps to wss. A scheme-less value is
    treated as http.
    """
    url = advertise_url.strip().rstrip("/")
    if "://" not in url:
        url = f"http://{url}"
    parsed = urlparse(url)
    scheme = parsed.scheme or "http"
    ws_scheme = "wss" if scheme == "https" else "ws"
    base = f"{parsed.netloc}{parsed.path}".rstrip("/")
    return f"{scheme}://{base}", f"{ws_scheme}://{base}"


def status_for_env(db: Session, env_id: str) -> dict[str, Any]:
    """Combined enrollment (DB) + live connection (registry) status payload.

    ``agents`` lists every credential of the env with its live connection
    state (``connected_count`` how many are connected right now). The flat
    top-level fields are kept for backward compatibility with the UI: they
    describe the first connected agent, falling back to the latest
    credential when no agent is connected.
    """
    creds = credentials_for_env(db, env_id)
    live = {r.agent_id: r for r in registry.records_for_env(env_id)}
    entries: list[dict[str, Any]] = []
    for cred in creds:
        record = live.pop(cred.id, None)
        entries.append(
            {
                "agent_id": cred.id,
                "name": cred.name,
                "hostname": (record.hostname if record else None) or cred.hostname,
                "version": (record.version if record else None) or cred.version,
                "connected": record is not None and record.online(),
                "last_seen": cred.last_seen,
                "pxe_config": cred.pxe_config,
            }
        )
    for agent_id, record in live.items():
        # Connected with a since-rotated credential: no row to describe it.
        entries.append(
            {
                "agent_id": agent_id,
                "name": None,
                "hostname": record.hostname,
                "version": record.version,
                "connected": record.online(),
                "last_seen": None,
            }
        )
    connected = [e for e in entries if e["connected"]]
    cred = creds[-1] if creds else None  # latest credential (compat fields)
    primary = connected[0] if connected else None
    return {
        "environment_id": env_id,
        "enrolled": cred is not None,
        "connected": bool(connected),
        "agent_id": primary["agent_id"] if primary else (cred.id if cred else None),
        "credential_name": (
            primary["name"] if primary else (cred.name if cred else None)
        ),
        "hostname": (
            primary["hostname"] if primary else (cred.hostname if cred else None)
        ),
        "version": primary["version"] if primary else (cred.version if cred else None),
        "last_seen": (
            primary["last_seen"] if primary else (cred.last_seen if cred else None)
        ),
        "credential_created_at": cred.created_at if cred else None,
        "agents": entries,
        "connected_count": len(connected),
    }


def agent_available(
    db: Session,
    env_id: str,
    *,
    max_age_seconds: float = OFFLINE_AFTER_SECONDS,
) -> bool:
    """True when ANY of the env's agents reported a frame recently (cheap DB check).

    Unlike the registry — which is process-local to the API — this works from
    any process (e.g. the job worker daemon): the hub mirrors frame arrival
    onto the credential row's ``last_seen``, and agents heartbeat every 15s.
    With several credentials per env (HA), one live agent is enough.
    """
    now = datetime.now(timezone.utc)
    for cred in credentials_for_env(db, env_id):
        if cred.last_seen is None:
            continue
        last_seen = cred.last_seen
        if last_seen.tzinfo is None:
            # SQLite drops tzinfo; stored values are UTC.
            last_seen = last_seen.replace(tzinfo=timezone.utc)
        if (now - last_seen).total_seconds() <= max_age_seconds:
            return True
    return False


# ---------------------------------------------------------------------------
# Discovery event ingestion (event{kind, payload} frames)
# ---------------------------------------------------------------------------

EVENT_KINDS = ("pxe_request", "bmc_found")


def _event_str(payload: dict[str, Any], key: str, max_len: int = 256) -> str | None:
    """Optional string field from an event payload; None when absent/blank."""
    value = payload.get(key)
    if value is None:
        return None
    text = str(value).strip()
    return text[:max_len] or None


def upsert_discovered_node(
    db: Session,
    env_id: str,
    *,
    mac: str,
    ip: str | None = None,
    hostname: str | None = None,
) -> DiscoveredNode:
    """Upsert a PXE/DHCP sighting keyed by (env, mac); bumps ``last_seen``."""
    now = datetime.now(timezone.utc)
    node = db.scalar(
        select(DiscoveredNode).where(
            DiscoveredNode.environment_id == env_id,
            DiscoveredNode.mac == mac,
        )
    )
    if node is None:
        node = DiscoveredNode(
            environment_id=env_id,
            mac=mac,
            ip=ip,
            hostname=hostname,
            first_seen=now,
            last_seen=now,
        )
    else:
        node.last_seen = now
        if ip:
            node.ip = ip
        if hostname:
            node.hostname = hostname
    db.add(node)
    db.flush()
    return node


def upsert_discovered_bmc(
    db: Session,
    env_id: str,
    *,
    ip: str,
    vendor: str | None = None,
    model: str | None = None,
) -> DiscoveredBmc:
    """Upsert a Redfish BMC find keyed by (env, ip); bumps ``last_seen``."""
    now = datetime.now(timezone.utc)
    bmc = db.scalar(
        select(DiscoveredBmc).where(
            DiscoveredBmc.environment_id == env_id,
            DiscoveredBmc.ip == ip,
        )
    )
    if bmc is None:
        bmc = DiscoveredBmc(
            environment_id=env_id,
            ip=ip,
            vendor=vendor,
            model=model,
            first_seen=now,
            last_seen=now,
        )
    else:
        bmc.last_seen = now
        if vendor:
            bmc.vendor = vendor
        if model:
            bmc.model = model
    db.add(bmc)
    db.flush()
    return bmc


def handle_agent_event(env_id: str, frame: dict[str, Any]) -> bool:
    """Validate and persist one ``event{kind, payload}`` frame from an agent.

    Known kinds: ``pxe_request`` {mac, ip?, hostname?} and ``bmc_found``
    {ip, vendor?, model?, title?}. Writes go through a short-lived session
    (the caller is the async WS loop); invalid payloads and unknown kinds are
    logged and ignored. Returns True when a row was upserted.
    """
    kind = str(frame.get("kind") or "").strip()
    payload = frame.get("payload")
    if kind not in EVENT_KINDS or not isinstance(payload, dict):
        log.warning("agent event ignored (env %s): bad kind/payload %r", env_id, kind)
        return False

    db = SessionLocal()
    try:
        if kind == "pxe_request":
            mac = (_event_str(payload, "mac", 32) or "").lower()
            if not mac:
                log.warning("pxe_request ignored (env %s): missing mac", env_id)
                return False
            upsert_discovered_node(
                db,
                env_id,
                mac=mac,
                ip=_event_str(payload, "ip", 64),
                hostname=_event_str(payload, "hostname"),
            )
        else:  # bmc_found — `title` is accepted but not persisted
            ip = _event_str(payload, "ip", 64) or ""
            try:
                ip = str(ipaddress.ip_address(ip))
            except ValueError:
                log.warning("bmc_found ignored (env %s): bad ip %r", env_id, ip)
                return False
            upsert_discovered_bmc(
                db,
                env_id,
                ip=ip,
                vendor=_event_str(payload, "vendor", 128),
                model=_event_str(payload, "model", 128),
            )
        db.commit()
        return True
    except Exception:  # noqa: BLE001 — a bad event must never kill the WS loop
        log.warning(
            "agent event ingest failed (env %s, kind %s)", env_id, kind, exc_info=True
        )
        db.rollback()
        return False
    finally:
        db.close()


def _run_over_ssh(
    bridge: Any,
    cmd: list[str],
    *,
    user: str,
    host: str,
    port: int,
    timeout: int,
    dry_run: bool,
    log: LogFn,
    input_text: str | None = None,
) -> dict[str, Any]:
    """bridge.run_command against an ad-hoc ssh target (not the env deploy host).

    Port 22 goes through the bridge's ssh_target wrapping; a non-default port
    builds the ssh argv directly (the wrapper has no port knob).
    """
    target = f"{user}@{host}"
    if int(port) == 22:
        return bridge.run_command(
            cmd,
            timeout=timeout,
            dry_run=dry_run,
            ssh_target=target,
            log=log,
            input_text=input_text,
        )
    argv = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-p",
        str(port),
        target,
        shlex.join([str(c) for c in cmd]),
    ]
    return bridge.run_command(
        argv,
        timeout=timeout,
        dry_run=dry_run,
        log=log,
        input_text=input_text,
    )


def install_agent(
    db: Session,
    env: Environment,
    settings: Settings,
    *,
    host: str,
    ssh_user: str = "root",
    ssh_port: int = 22,
    name: str | None = None,
    dry_run: bool,
    timeout: int,
    log: LogFn,
) -> dict[str, Any]:
    """Push-install the agent onto ``host`` over ssh (agent.install op).

    Issues a FRESH enrollment credential for the env (per-name
    replace-on-create under the install name, same path as the token
    endpoint — other named credentials stay valid), commits it so the agent
    can connect as soon as the installer finishes, then runs the curl-pipe
    installer on the target — the remote pulls agent/install.sh from the
    hub's GET /agent.
    When that pull fails (the target cannot reach the hub over http), the
    packaged script is streamed over the ssh stdin instead. The raw token is
    masked as ``gsca_***`` in every log line — job logs persist.
    """
    from app.services import genestack_bridge as bridge

    advertise = (settings.hub_advertise_url or "").strip()
    if not advertise:
        msg = (
            "Cannot install agent: hub.advertise_url is not set in config.yaml. "
            "Set it to the address that target hosts can reach (e.g. https://console.example.com:8080)."
        )
        log(f"[denied] {msg}")
        return {"ok": False, "error": msg, "returncode": 2}

    if not host or not host.strip():
        msg = (
            "Cannot install agent: host parameter is empty. "
            "Provide a valid hostname or IP address via the 'host' parameter."
        )
        log(f"[denied] {msg}")
        return {"ok": False, "error": msg, "returncode": 2}

    http_base, ws_base = advertise_bases(advertise)
    agent_name = name or host
    ssh_port = int(ssh_port or 22)
    script_text = (
        _INSTALL_SCRIPT.read_text(encoding="utf-8")
        if _INSTALL_SCRIPT.is_file()
        else None
    )
    if not dry_run and script_text is None:
        log(
            f"[warn] Fallback install script not found at {_INSTALL_SCRIPT}; ssh stdin streaming will not be available if curl-pipe fails"
        )

    if dry_run:
        # No credential rotation, no execution: plan logged with a placeholder
        # token (already the masked form).
        token = TOKEN_MASK

        def mlog(msg: str) -> None:
            log(msg)

    else:
        _cred, token = create_credential(db, env.id, agent_name)
        db.commit()
        log(
            f"[agent] step 0: Issued fresh enrollment credential — "
            f"id={_cred.id} name={agent_name!r} ✓"
        )

        def mlog(msg: str) -> None:
            log(msg.replace(token, TOKEN_MASK))

    install_args = ["--hub", ws_base, "--token", token]
    if name:
        install_args += ["--name", name]
    args_str = " ".join(shlex.quote(a) for a in install_args)
    curl_script = (
        f"curl -fsSL {shlex.quote(http_base + '/agent')} | bash -s -- {args_str}"
    )
    stdin_argv = ["bash", "-s", "--", *install_args]

    def _run(cmd: list[str], input_text: str | None = None) -> dict[str, Any]:
        return _run_over_ssh(
            bridge,
            cmd,
            user=ssh_user,
            host=host,
            port=ssh_port,
            timeout=timeout,
            dry_run=dry_run,
            log=mlog,
            input_text=input_text,
        )

    log(f"[agent] install target {ssh_user}@{host} port={ssh_port} hub={http_base}")
    log(f"[agent] step 1: Connecting to {ssh_user}@{host} via SSH...")
    result = _run(["bash", "-c", curl_script])
    rc = result.get("returncode")
    method = "curl"

    if dry_run:
        log(
            "[agent] step 2 (fallback, planned): Would stream packaged agent/install.sh over ssh stdin"
        )
        _run(stdin_argv, input_text=script_text)
        msg = f"[dry-run] would install agent on {ssh_user}@{host} (nothing executed)"
        log(f"[agent] {msg}")
        return {
            "ok": True,
            "dry_run": True,
            "host": host,
            "agent_name": agent_name,
            "returncode": 0,
            "message": msg,
        }

    # Inspect SSH-layer errors before falling back: a connect/refused/timeout
    # error means the host is unreachable, so the stdin fallback will also fail.
    stderr = (result.get("stderr") or "").strip()
    ssh_fail = any(
        sig in stderr
        for sig in (
            "Connection refused",
            "Network is unreachable",
            "Timeout",
            "No route to host",
            "Permission denied (publickey)",
        )
    )
    if ssh_fail:
        msg = (
            f"Cannot reach {ssh_user}@{host}:{ssh_port} over SSH: "
            f"{stderr[:300]} — verify host is reachable and SSH credentials are correct."
        )
        log(f"[agent] FAILED {msg}")
        return {
            "ok": False,
            "error": msg,
            "host": host,
            "agent_name": agent_name,
            "returncode": 2,
        }

    if rc != 0:
        log(f"[agent] curl-pipe failed rc={rc} — trying fallback: ssh stdin streaming")
        if script_text is None:
            msg = (
                f"Agent install on {host} failed: curl-pipe returned rc={rc} "
                f"and the fallback install script is not available at {_INSTALL_SCRIPT}. "
                f"Ensure the target host can reach {http_base}/agent over HTTPS, "
                f"or re-build the console image to include agent/install.sh."
            )
            log(f"[agent] FAILED {msg}")
            return {
                "ok": False,
                "error": msg,
                "host": host,
                "agent_name": agent_name,
                "returncode": 2,
            }
        log("[agent] step 2 (fallback): Streaming install script via ssh stdin...")
        result = _run(stdin_argv, input_text=script_text)
        rc = result.get("returncode")
        method = "stdin"
        stderr = (result.get("stderr") or "").strip()
        stdin_ssh_fail = any(
            sig in stderr
            for sig in (
                "Connection refused",
                "Network is unreachable",
                "Timeout",
                "No route to host",
                "Permission denied (publickey)",
            )
        )
        if stdin_ssh_fail:
            msg = (
                f"Cannot reach {ssh_user}@{host}:{ssh_port} over SSH: "
                f"{stderr[:300]} — verify host is reachable and SSH credentials are correct."
            )
            log(f"[agent] FAILED {msg}")
            return {
                "ok": False,
                "error": msg,
                "host": host,
                "agent_name": agent_name,
                "returncode": 2,
            }

    ok = rc == 0
    if ok:
        container_name = name or "gsc-agent"
        # Post-install verification: check the container is actually running.
        log(
            f"[agent] step 3: Verifying agent container '{container_name}' is running..."
        )
        verify_result = _run(
            [
                "bash",
                "-c",
                f"docker inspect --format '{{{{.State.Running}}}}' {shlex.quote(container_name)} 2>/dev/null || echo 'not-found'",
            ]
        )
        verify_stdout = (verify_result.get("stdout") or "").strip().lower()
        if verify_stdout == "true":
            log(f"[agent] Agent container '{container_name}' is running ✓")
            # Check if this replaced an existing container (re-install).
            log(
                f"[agent] step 4: Verify the agent connects to {ws_base} within 30 seconds"
            )
            msg = f"Agent installed successfully on {host} via {method} — container '{container_name}' is running"
        else:
            log(
                f"[agent] WARN: container '{container_name}' not in running state (got: {verify_stdout})"
            )
            log(
                f"[agent] Agent may still be starting — check with 'docker logs -f {container_name}' on {host}"
            )
            msg = f"Agent install script succeeded on {host} via {method}, but container '{container_name}' is not yet running — verify manually"
            # Don't treat as hard failure: container may need a few seconds.
    else:
        msg = (
            f"Agent install on {host} failed via {method} (rc={rc}). "
            f"Check: 1) SSH connectivity to {ssh_user}@{host}:{ssh_port}, "
            f"2) target host can resolve and reach {http_base}, "
            f"3) docker is installed and running on target. See log for details."
        )
        log(f"[agent] {msg}")
    return {
        "ok": ok,
        "host": host,
        "agent_name": agent_name,
        "returncode": rc,
        "method": method,
        "dry_run": False,
        "message": msg,
    }


def _resolve_seed_environment(
    db: Session,
    settings: "Settings",
) -> tuple[Environment | None, str]:
    """Pick the environment the 'local-agent' seed targets, deterministically.

    Returns ``(env, reason, env_count)`` where ``reason`` is one of:
    ``"override"``, ``"single"``, ``"multi-env"``, ``"no-envs"``,
    ``"override-not-found"``. Callers act on the reason; this helper never
    writes anything.
    """
    envs = list(db.scalars(select(Environment)).all())
    if not envs:
        return None, "no-envs", 0

    override = settings.agent_default_environment_id
    if override:
        match = next((e for e in envs if e.id == override), None)
        if match is None:
            return None, "override-not-found", len(envs)
        return match, "override", len(envs)

    if len(envs) == 1:
        return envs[0], "single", 1

    return None, "multi-env", len(envs)


def seed_default_local_agent(
    db: Session,
    token_dir: Path,
    settings: "Settings | None" = None,
) -> None:
    """Create the 'local-agent' credential and write its token to disk for the
    docker-compose local-agent container.

    Environment selection is tenancy-aware and deterministic:
      1. ``agent.default_environment_id`` (config override), if it exists.
      2. The single environment, when there is exactly one.
      3. Otherwise (multi-env, no override) seeding is SKIPPED with a warning
         — never guess, never rotate.

    The credential is only created when none exists for the target env. If a
    credential already exists but the token file is missing, the existing
    credential is left untouched and an error is logged: only
    ``sha256(token)`` is stored, so the raw token cannot be re-derived —
    regenerating it would rotate the credential and kick any connected agent
    off the hub.
    """
    if settings is None:
        from app.config import get_settings

        settings = get_settings()

    env, reason, env_count = _resolve_seed_environment(db, settings)
    if env is None:
        if reason == "no-envs":
            return
        if reason == "multi-env":
            log.warning(
                "local-agent seed skipped: %d environments exist and "
                "agent.default_environment_id is not set. Set it in config.yaml "
                "to the environment that should own the auto 'local-agent' "
                "credential (no tenant should be guessed).",
                env_count,
            )
            return
        if reason == "override-not-found":
            log.warning(
                "local-agent seed skipped: agent.default_environment_id=%s "
                "does not match any environment.",
                settings.agent_default_environment_id,
            )
            return

    name = "local-agent"
    existing = db.scalar(
        select(AgentCredential).where(
            AgentCredential.environment_id == env.id,
            AgentCredential.name == name,
        )
    )

    # If the credential and token file already exist, nothing to do.
    token_file = token_dir / "local-agent-token"
    if existing and token_file.is_file():
        return

    if existing is not None:
        # The raw token is not recoverable from the stored sha256 hash, so we
        # cannot regenerate the file without rotating the credential. Rotating
        # here would silently kick a possibly-connected agent off the hub, so
        # leave it alone and surface the problem to the operator instead.
        log.error(
            "local-agent credential exists for environment %s (%s) but its "
            "token file %s is missing. The raw token is not recoverable "
            "(only sha256 is stored); refusing to rotate it because that "
            "would disconnect any agent currently using it. Re-create the "
            "token explicitly via the API (POST /environments/{id}/agent/token) "
            "if a rotation is intended.",
            env.id,
            env.name,
            token_file,
        )
        return

    # First-time seed for a tenancy-determined environment: create credential.
    _cred, token = create_credential(db, env.id, name=name)

    # Write token to shared volume (owner-only — raw bearer for the local agent).
    token_dir.mkdir(parents=True, exist_ok=True)
    token_file.write_text(token, encoding="utf-8")
    try:
        token_file.chmod(0o600)
    except OSError:
        log.warning("could not chmod 0600 %s", token_file)
    log.info(
        "seeded local-agent credential for environment %s (%s); token written to %s",
        env.id,
        env.name,
        token_file,
    )
