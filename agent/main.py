#!/usr/bin/env python3
"""Genestack Console agent — standalone hub-and-spoke WebSocket client.

The agent runs inside an environment, dials OUT to the console hub over a
persistent WebSocket, proves its enrollment token via an HMAC challenge,
heartbeats, and executes hub-issued commands, streaming output back as log
frames. Fully standalone: the only dependency is ``websockets``.

Configuration (environment variables):
    GSC_HUB_URL      ws:// or wss://host[:port] of the console hub (required)
    GSC_AGENT_TOKEN  one-time enrollment token from the console (required)
    GSC_AGENT_NAME   display name (default: hostname)
    GSC_PXE_LEASES   path to a dnsmasq leases file to watch (optional); new or
                     changed leases are reported as ``pxe_request`` events
    GSC_ALLOWED_ROOT root(s) the agent may write files under for ``file_write``
                     frames (default: /etc/genestack; colon-separated for several)
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import dataclasses
import hashlib
import hmac
import ipaddress
import json
import os
import random
import shutil
import signal
import socket
import ssl
import sys
import time
from urllib.parse import urlencode

import websockets

VERSION = "0.1.0"
CAPS = ["command", "scan_bmc", "file_write"]

HEARTBEAT_INTERVAL = 15.0
DEFAULT_CMD_TIMEOUT = 300.0
BACKOFF_BASE = 1.0
BACKOFF_CAP = 60.0
BACKOFF_JITTER = 0.25

# BMC sweep tuning: probe https://<ip>/redfish/v1/ across a subnet.
SCAN_MAX_PREFIXLEN = 24  # reject sweeps larger than a /24
SCAN_CONCURRENCY = 64
SCAN_TIMEOUT = 3.0
SCAN_PROGRESS_EVERY = 50
# Poll interval for the dnsmasq leases watcher.
PXE_POLL_INTERVAL = 5.0

CONNECT_PATH = "/api/v1/agents/connect"
RC_TIMEOUT = 124
RC_SPAWN_FAILED = 127
# Cap on captured stdout/stderr echoed back in the result frame.
RESULT_CAPTURE_LIMIT = 1_000_000
# Default root file_write frames may target; overridable via GSC_ALLOWED_ROOT.
DEFAULT_ALLOWED_ROOT = "/etc/genestack"
DEFAULT_FILE_MODE = 0o644


class ConfigError(Exception):
    """Raised when agent configuration is missing or invalid."""


class ProtocolError(Exception):
    """Raised when the hub sends an unexpected frame."""


@dataclasses.dataclass
class AgentConfig:
    hub_url: str
    token: str
    name: str
    heartbeat_interval: float = HEARTBEAT_INTERVAL
    default_timeout: float = DEFAULT_CMD_TIMEOUT
    pxe_leases: str | None = None
    allowed_roots: tuple[str, ...] = (DEFAULT_ALLOWED_ROOT,)


def parse_allowed_roots(raw: str | None) -> tuple[str, ...]:
    """Parse GSC_ALLOWED_ROOT: colon-separated absolute roots, resolved."""
    if not raw:
        return (DEFAULT_ALLOWED_ROOT,)
    roots = tuple(
        os.path.realpath(part.strip()) for part in raw.split(":") if part.strip()
    )
    return roots or (DEFAULT_ALLOWED_ROOT,)


def load_config(env: dict | None = None) -> AgentConfig:
    """Build config from env vars; raises ConfigError with a clear message.

    Supports ``GSC_AGENT_TOKEN_FILE`` as an alternative to ``GSC_AGENT_TOKEN``.
    When the file path is set, the token is read from disk (enables shared
    volume provisioning from the console container).
    """
    env = os.environ if env is None else env
    hub_url = (env.get("GSC_HUB_URL") or "").strip().rstrip("/")
    token = (env.get("GSC_AGENT_TOKEN") or "").strip()
    token_file = (env.get("GSC_AGENT_TOKEN_FILE") or "").strip()
    if token_file and not token:
        # The console writes the local-agent token file during its own
        # startup (lifespan seed), which can land after this container's
        # first read — retry briefly instead of crash-looping on boot.
        deadline = time.monotonic() + 60
        while True:
            try:
                with open(token_file, encoding="utf-8") as fh:
                    token = fh.read().strip()
                break
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise ConfigError(
                        f"GSC_AGENT_TOKEN_FILE={token_file!r} not readable after 60s: {exc}"
                    )
                time.sleep(2)
    name = (env.get("GSC_AGENT_NAME") or "").strip() or socket.gethostname()
    pxe_leases = (env.get("GSC_PXE_LEASES") or "").strip() or None
    allowed_roots = parse_allowed_roots(env.get("GSC_ALLOWED_ROOT"))
    if not hub_url:
        raise ConfigError("GSC_HUB_URL is required (ws:// or wss://host[:port])")
    if not hub_url.startswith(("ws://", "wss://")):
        raise ConfigError(
            f"GSC_HUB_URL must start with ws:// or wss:// (got {hub_url!r})"
        )
    if not token:
        raise ConfigError(
            "GSC_AGENT_TOKEN is required (create an agent token in the console "
            "or set GSC_AGENT_TOKEN_FILE to a token provisioning path)"
        )
    return AgentConfig(
        hub_url=hub_url,
        token=token,
        name=name,
        pxe_leases=pxe_leases,
        allowed_roots=allowed_roots,
    )


def compute_proof(token: str, nonce: str) -> str:
    """Handshake proof: HMAC-SHA256(token, nonce) as hex."""
    return hmac.new(token.encode(), nonce.encode(), hashlib.sha256).hexdigest()


def backoff_delay(
    attempt: int,
    base: float = BACKOFF_BASE,
    cap: float = BACKOFF_CAP,
    jitter: float = BACKOFF_JITTER,
) -> float:
    """Reconnect delay: base * 2**attempt capped at cap, plus +/- jitter."""
    delay = min(cap, base * (2 ** max(0, attempt)))
    if jitter:
        delay *= 1.0 + random.uniform(-jitter, jitter)
    return delay


def connect_url(config: AgentConfig) -> str:
    return f"{config.hub_url}{CONNECT_PATH}?{urlencode({'token': config.token})}"


def log(msg: str) -> None:
    print(f"[gsc-agent] {msg}", file=sys.stderr, flush=True)


def _redfish_ssl_context() -> ssl.SSLContext:
    """BMCs ship self-signed certs; probing accepts anything."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def classify_bmc(body: bytes) -> dict:
    """Classify a Redfish ServiceRoot response: vendor + model/title when known."""
    text = body.decode(errors="replace")
    data = None
    with contextlib.suppress(ValueError):
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            data = parsed
    data = data or {}
    if "iLO" in text:
        vendor = "HPE"
    elif "iDRAC" in text:
        vendor = "Dell"
    else:
        product = data.get("Product") or ""
        oem = data.get("Oem") if isinstance(data.get("Oem"), dict) else {}
        vendor = product or next(iter(oem), "") or "redfish"
    return {
        "vendor": str(vendor),
        "model": str(data.get("Model") or data.get("Product") or ""),
        "title": str(data.get("Name") or data.get("Title") or ""),
    }


async def probe_redfish(ip: str, timeout: float = SCAN_TIMEOUT) -> dict | None:
    """GET https://<ip>/redfish/v1/ — info dict if a Redfish endpoint answers, else None."""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(ip, 443, ssl=_redfish_ssl_context()), timeout
        )
    except (OSError, asyncio.TimeoutError):
        return None
    try:
        writer.write(
            f"GET /redfish/v1/ HTTP/1.1\r\nHost: {ip}\r\nConnection: close\r\n\r\n".encode()
        )
        await writer.drain()
        status_line = await asyncio.wait_for(reader.readline(), timeout)
        parts = status_line.decode(errors="replace").split()
        if len(parts) < 2 or not parts[1].isdigit():
            return None
        status = int(parts[1])
        # Drain headers, then read the body (server closes: Connection: close).
        while True:
            header = await asyncio.wait_for(reader.readline(), timeout)
            if header in (b"\r\n", b"\n", b""):
                break
        body = await asyncio.wait_for(reader.read(64 * 1024), timeout)
    except (OSError, asyncio.TimeoutError):
        return None
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
    if status not in (200, 401, 403):
        return None
    return {"ip": ip, **classify_bmc(body)}


async def scan_bmc(subnet: str, send_log, send_event) -> dict:
    """Sweep a subnet for Redfish BMCs (max /24), streaming findings as events.

    ``send_log``/``send_event`` are async callables taking a log line / event
    payload. Returns a result dict: {rc, found} plus "error" on failure.
    """
    try:
        network = ipaddress.ip_network(subnet, strict=False)
    except ValueError as exc:
        await send_log(f"invalid subnet {subnet!r}: {exc}")
        return {"rc": 2, "found": 0, "error": str(exc)}
    if network.version != 4 or network.prefixlen < SCAN_MAX_PREFIXLEN:
        msg = f"subnet {subnet!r} too large: max /{SCAN_MAX_PREFIXLEN}"
        await send_log(msg)
        return {"rc": 2, "found": 0, "error": msg}
    hosts = [str(host) for host in network.hosts()] or [str(network.network_address)]
    await send_log(f"scanning {len(hosts)} hosts in {network} for Redfish BMCs")
    semaphore = asyncio.Semaphore(SCAN_CONCURRENCY)
    found = 0
    done = 0

    async def probe_one(ip: str) -> None:
        nonlocal found, done
        try:
            async with semaphore:
                info = await probe_redfish(ip)
        except Exception as exc:  # noqa: BLE001 — one bad probe must not kill the sweep
            info = None
            await send_log(f"probe {ip} failed: {exc}")
        done += 1
        if info:
            found += 1
            await send_event(info)
            await send_log(f"found BMC at {ip} ({info.get('vendor', 'redfish')})")
        if done % SCAN_PROGRESS_EVERY == 0 or done == len(hosts):
            await send_log(f"scanned {done}/{len(hosts)} hosts, {found} found so far")

    try:
        await asyncio.gather(*(probe_one(ip) for ip in hosts))
    except Exception as exc:  # noqa: BLE001
        await send_log(f"scan failed: {exc}")
        return {"rc": 1, "found": found, "error": str(exc)}
    await send_log(f"found {found} BMC(s) in {subnet}")
    return {"rc": 0, "found": found}


def parse_leases(text: str) -> dict[str, tuple[str, str]]:
    """Parse dnsmasq leases: mac -> (ip, hostname); '*' hostname means unknown."""
    leases: dict[str, tuple[str, str]] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 4:
            _expiry, mac, ip, hostname = parts[:4]
            leases[mac] = (ip, "" if hostname == "*" else hostname)
    return leases


async def watch_pxe_leases(
    path: str,
    send_event,
    stop: asyncio.Event,
    poll_interval: float = PXE_POLL_INTERVAL,
    seen: dict[str, tuple[str, str]] | None = None,
) -> None:
    """Poll a dnsmasq leases file; report leases added/changed after agent start.

    ``seen`` persists lease state across reconnects so a session bounce does
    not re-report leases the hub already heard about.
    """
    seen = {} if seen is None else seen
    first_read = True
    while not stop.is_set():
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                current = parse_leases(handle.read())
        except OSError:
            current = None
        if current is not None:
            if first_read:
                seen.update(current)  # pre-existing leases: record, don't report
                first_read = False
            else:
                for mac, (ip, hostname) in current.items():
                    if seen.get(mac) != (ip, hostname):
                        await send_event({"mac": mac, "ip": ip, "hostname": hostname})
                seen.update(current)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), poll_interval)


def _match_allowed_root(resolved: str, allowed_roots: tuple[str, ...]) -> str | None:
    """Return the allowed root containing ``resolved`` (already realpath'd), else None."""
    for root in allowed_roots:
        root = os.path.realpath(root)
        prefix = root if root == os.sep else root + os.sep
        if resolved == root or resolved.startswith(prefix):
            return root
    return None


def _backup_existing(target: str, root: str, backup_dir: str) -> None:
    """Copy ``target`` to <backup_dir>/<relpath>; never clobber an earlier backup."""
    relpath = os.path.relpath(target, root)
    dest = os.path.join(backup_dir, relpath)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    candidate = dest
    counter = 1
    while os.path.exists(candidate):
        candidate = f"{dest}.{counter}"
        counter += 1
    shutil.copy2(target, candidate)


def write_file_from_frame(frame: dict, allowed_roots: tuple[str, ...]) -> dict:
    """Execute a ``file_write`` frame: decode, back up, write with mode + fsync.

    Returns result fields: ``{"rc": 0, "bytes": n}`` on success or
    ``{"rc": 1, "error": msg}`` on any validation or I/O failure.
    """
    path = frame.get("path")
    if not isinstance(path, str) or not os.path.isabs(path):
        return {"rc": 1, "error": f"path must be an absolute path string, got {path!r}"}
    resolved = os.path.realpath(path)
    root = _match_allowed_root(resolved, allowed_roots)
    if root is None:
        roots = ":".join(allowed_roots)
        return {"rc": 1, "error": f"path {path!r} outside allowed root(s) {roots!r}"}
    b64 = frame.get("b64")
    if not isinstance(b64, str):
        return {"rc": 1, "error": "missing base64 payload field 'b64'"}
    try:
        data = base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        return {"rc": 1, "error": f"invalid base64 payload: {exc}"}
    try:
        mode = int(frame.get("mode", DEFAULT_FILE_MODE)) & 0o7777
    except (TypeError, ValueError):
        return {"rc": 1, "error": f"invalid mode: {frame.get('mode')!r}"}
    backup_dir = frame.get("backup_dir")
    if backup_dir and os.path.exists(resolved):
        try:
            _backup_existing(resolved, root, str(backup_dir))
        except OSError as exc:
            return {"rc": 1, "error": f"backup of {path!r} failed: {exc}"}
    try:
        os.makedirs(os.path.dirname(resolved), exist_ok=True)
        fd = os.open(resolved, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(resolved, mode)  # os.open mode only applies on creation
    except OSError as exc:
        return {"rc": 1, "error": f"write to {path!r} failed: {exc}"}
    return {"rc": 0, "bytes": len(data)}


class AgentSession:
    """One hub connection: handshake, hello, heartbeats, command execution."""

    def __init__(
        self,
        config: AgentConfig,
        ws,
        pxe_seen: dict[str, tuple[str, str]] | None = None,
    ) -> None:
        self.config = config
        self.ws = ws
        self.agent_id: str | None = None
        self._cmd_tasks: set[asyncio.Task] = set()
        self._pxe_seen = pxe_seen if pxe_seen is not None else {}

    async def send(self, frame: dict) -> None:
        await self.ws.send(json.dumps(frame))

    async def handshake(self) -> None:
        frame = json.loads(await self.ws.recv())
        if frame.get("type") == "error":
            raise ProtocolError(
                f"hub rejected connection: {frame.get('message', frame)}"
            )
        if frame.get("type") != "challenge" or "nonce" not in frame:
            raise ProtocolError(f"expected challenge frame, got {frame!r}")
        await self.send(
            {"type": "proof", "hmac": compute_proof(self.config.token, frame["nonce"])}
        )
        frame = json.loads(await self.ws.recv())
        if frame.get("type") == "error":
            raise ProtocolError(f"hub rejected proof: {frame.get('message', frame)}")
        if frame.get("type") != "welcome":
            raise ProtocolError(f"expected welcome frame, got {frame!r}")
        self.agent_id = frame.get("agent_id")

    async def send_hello(self) -> None:
        caps = list(CAPS)
        if self.config.pxe_leases:
            caps.append("pxe_watch")
        await self.send(
            {
                "type": "hello",
                "agent_id": self.agent_id,
                "version": VERSION,
                "hostname": self.config.name,
                "caps": caps,
            }
        )

    async def heartbeat_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await self.send({"type": "heartbeat", "ts": time.time()})
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), self.config.heartbeat_interval)

    async def run_command(self, frame: dict) -> None:
        cmd_id = frame.get("id")
        cmd = [str(part) for part in (frame.get("cmd") or [])]
        timeout = float(frame.get("timeout") or self.config.default_timeout)
        cwd = frame.get("cwd") or None
        env = None
        if frame.get("env"):
            env = {**os.environ, **{str(k): str(v) for k, v in frame["env"].items()}}
        if not cmd:
            await self.send(
                {"type": "result", "id": cmd_id, "rc": 2, "stderr": "empty cmd"}
            )
            return
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=cwd,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (OSError, ValueError) as exc:
            await self.send(
                {"type": "log", "id": cmd_id, "line": f"failed to start: {exc}"}
            )
            await self.send(
                {
                    "type": "result",
                    "id": cmd_id,
                    "rc": RC_SPAWN_FAILED,
                    "stderr": str(exc),
                }
            )
            return

        out_lines: list[str] = []
        err_lines: list[str] = []

        async def pump(stream, sink: list[str]) -> None:
            while True:
                line = await stream.readline()
                if not line:
                    return
                text = line.decode(errors="replace").rstrip("\n")
                sink.append(text)
                await self.send({"type": "log", "id": cmd_id, "line": text})

        pumps = [
            asyncio.create_task(pump(proc.stdout, out_lines)),
            asyncio.create_task(pump(proc.stderr, err_lines)),
        ]
        try:
            await asyncio.wait_for(asyncio.gather(*pumps), timeout=timeout)
            rc = await proc.wait()
        except TimeoutError:
            proc.kill()
            await proc.wait()
            rc = RC_TIMEOUT
            await asyncio.gather(*pumps, return_exceptions=True)
            await self.send(
                {
                    "type": "log",
                    "id": cmd_id,
                    "line": f"command timed out after {timeout:g}s",
                }
            )
        await self.send(
            {
                "type": "result",
                "id": cmd_id,
                "rc": rc,
                "stdout": "\n".join(out_lines)[-RESULT_CAPTURE_LIMIT:],
                "stderr": "\n".join(err_lines)[-RESULT_CAPTURE_LIMIT:],
            }
        )

    async def run_scan_command(self, frame: dict) -> None:
        """Handle a scan_bmc command frame: sweep the subnet, stream findings."""
        cmd_id = frame.get("id")
        scan = frame.get("scan_bmc") or {}
        subnet = str(scan.get("subnet") or "")

        async def send_log(line: str) -> None:
            await self.send({"type": "log", "id": cmd_id, "line": line})

        async def send_event(payload: dict) -> None:
            await self.send({"type": "event", "kind": "bmc_found", "payload": payload})

        if not subnet:
            await self.send(
                {
                    "type": "result",
                    "id": cmd_id,
                    "rc": 2,
                    "found": 0,
                    "stderr": "empty subnet",
                }
            )
            return
        result = await scan_bmc(subnet, send_log, send_event)
        await self.send({"type": "result", "id": cmd_id, **result})

    async def run_file_write(self, frame: dict) -> None:
        """Handle a file_write frame: deliver a rendered config file to disk."""
        cmd_id = frame.get("id")
        try:
            result = write_file_from_frame(frame, self.config.allowed_roots)
        except Exception as exc:  # noqa: BLE001 — a bad write must not kill the session
            result = {"rc": 1, "error": f"file_write failed: {exc}"}
        written = result.pop("bytes", None)
        if result["rc"] == 0:
            await self.send(
                {
                    "type": "log",
                    "id": cmd_id,
                    "line": f"wrote {frame.get('path')} ({written} bytes)",
                }
            )
        await self.send({"type": "result", "id": cmd_id, **result})

    async def _shutdown_watcher(self, stop: asyncio.Event) -> None:
        """On SIGTERM: say bye and close, which ends the receive loop."""
        await stop.wait()
        with contextlib.suppress(Exception):
            await self.send({"type": "bye", "reason": "shutdown"})
        with contextlib.suppress(Exception):
            await self.ws.close()

    async def run(self, stop: asyncio.Event) -> None:
        await self.handshake()
        await self.send_hello()
        log(
            f"connected to {self.config.hub_url} as {self.agent_id!r} (name={self.config.name!r})"
        )
        heartbeat = asyncio.create_task(self.heartbeat_loop(stop))
        watcher = asyncio.create_task(self._shutdown_watcher(stop))
        pxe_watcher = None
        if self.config.pxe_leases:

            async def send_pxe_event(payload: dict) -> None:
                await self.send(
                    {"type": "event", "kind": "pxe_request", "payload": payload}
                )

            pxe_watcher = asyncio.create_task(
                watch_pxe_leases(
                    self.config.pxe_leases, send_pxe_event, stop, seen=self._pxe_seen
                )
            )
        try:
            async for raw in self.ws:
                try:
                    frame = json.loads(raw)
                except ValueError:
                    log(f"ignoring non-JSON frame from hub: {raw!r}")
                    continue
                if not isinstance(frame, dict):
                    log(f"ignoring non-object frame from hub: {frame!r}")
                    continue
                ftype = frame.get("type")
                if ftype == "command":
                    handler = (
                        self.run_scan_command
                        if "scan_bmc" in frame
                        else self.run_command
                    )
                    task = asyncio.create_task(handler(frame))
                    self._cmd_tasks.add(task)
                    task.add_done_callback(self._cmd_tasks.discard)
                elif ftype == "file_write":
                    task = asyncio.create_task(self.run_file_write(frame))
                    self._cmd_tasks.add(task)
                    task.add_done_callback(self._cmd_tasks.discard)
                elif ftype == "bye":
                    log(f"hub closed session: {frame.get('reason', 'no reason given')}")
                    return
                # heartbeat acks / event frames: nothing to do in phase 1
        finally:
            heartbeat.cancel()
            watcher.cancel()
            if pxe_watcher is not None:
                pxe_watcher.cancel()
            for task in self._cmd_tasks:
                task.cancel()


async def run_forever(config: AgentConfig, stop: asyncio.Event) -> None:
    """Connect, serve, and reconnect with exponential backoff + jitter."""
    attempt = 0
    pxe_seen: dict[str, tuple[str, str]] = (
        {}
    )  # survives reconnects: no duplicate reports
    while not stop.is_set():
        try:
            async with websockets.connect(connect_url(config)) as ws:
                await AgentSession(config, ws, pxe_seen=pxe_seen).run(stop)
                attempt = 0  # clean session: reset backoff
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — reconnect on any failure
            log(f"connection error: {exc}")
        if stop.is_set():
            break
        delay = backoff_delay(attempt)
        attempt += 1
        log(f"reconnecting in {delay:.1f}s")
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), delay)


async def _load_config_with_retry(env: dict | None = None) -> AgentConfig:
    """Load config, retrying up to 5 min when token file is missing.

    The local-agent container starts before the console writes its token
    provisioning file.  Poll the file until it appears (5 s between attempts).
    """
    max_attempts = 72
    for i in range(max_attempts):
        try:
            return load_config(env)
        except ConfigError as exc:
            msg = str(exc)
            if ("TOKEN" in msg or "not readable" in msg) and i < max_attempts - 1:
                log(f"token not yet available, retrying in 5s ({i+1}/{max_attempts})")
                await asyncio.sleep(5)
            else:
                raise


async def amain() -> int:
    try:
        config = await _load_config_with_retry()
    except ConfigError as exc:
        log(f"configuration error: {exc}")
        return 2
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError, ValueError):
            loop.add_signal_handler(sig, stop.set)
    await run_forever(config, stop)
    return 0


def main() -> None:
    try:
        rc = asyncio.run(amain())
    except KeyboardInterrupt:
        rc = 0
    sys.exit(rc)


if __name__ == "__main__":
    main()
