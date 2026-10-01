"""Agent client tests: handshake proof, command round-trip, backoff, config errors.

Uses a fake in-process hub via ``websockets.serve`` for the round-trip tests.
Sync test functions drive asyncio with ``asyncio.run`` so no pytest-asyncio
mode configuration is required.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import json
import os
from urllib.parse import parse_qs, urlparse

import pytest
import websockets

import agent.main as agent_main
from agent.main import (
    AgentConfig,
    AgentSession,
    ConfigError,
    ProtocolError,
    amain,
    backoff_delay,
    compute_proof,
    connect_url,
    load_config,
    parse_allowed_roots,
    parse_leases,
    watch_pxe_leases,
    write_file_from_frame,
)

TOKEN = "gsca_test-token-0123456789abcdef"
NONCE = "0123456789abcdef0123456789abcdef"


def test_proof_is_hmac_sha256_hex():
    expected = hmac.new(TOKEN.encode(), NONCE.encode(), hashlib.sha256).hexdigest()
    assert compute_proof(TOKEN, NONCE) == expected
    assert len(compute_proof(TOKEN, NONCE)) == 64
    assert compute_proof(TOKEN, NONCE) != compute_proof("wrong", NONCE)


def test_backoff_sequence_doubles_and_caps():
    delays = [backoff_delay(i, base=1.0, cap=60.0, jitter=0.0) for i in range(8)]
    assert delays == [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0]


def test_backoff_jitter_stays_within_band():
    for attempt in range(6):
        base = min(60.0, 1.0 * 2**attempt)
        delay = backoff_delay(attempt, base=1.0, cap=60.0, jitter=0.25)
        assert base * 0.75 <= delay <= base * 1.25


def test_missing_token_is_clear_error():
    with pytest.raises(ConfigError, match="GSC_AGENT_TOKEN"):
        load_config({"GSC_HUB_URL": "ws://hub:8080"})


def test_missing_hub_url_is_clear_error():
    with pytest.raises(ConfigError, match="GSC_HUB_URL"):
        load_config({"GSC_AGENT_TOKEN": TOKEN})


def test_bad_hub_scheme_is_clear_error():
    with pytest.raises(ConfigError, match="ws://"):
        load_config({"GSC_HUB_URL": "http://hub:8080", "GSC_AGENT_TOKEN": TOKEN})


def test_load_config_defaults_name_to_hostname():
    cfg = load_config({"GSC_HUB_URL": "wss://hub/", "GSC_AGENT_TOKEN": TOKEN})
    assert cfg.hub_url == "wss://hub"  # trailing slash stripped
    assert cfg.name  # hostname fallback, non-empty


def test_amain_missing_token_exits_2(monkeypatch):
    monkeypatch.delenv("GSC_AGENT_TOKEN", raising=False)
    monkeypatch.setenv("GSC_HUB_URL", "ws://hub:8080")
    assert asyncio.run(amain()) == 2


def test_connect_url_carries_token_query():
    cfg = AgentConfig(hub_url="ws://hub:8080", token=TOKEN, name="x")
    parsed = urlparse(connect_url(cfg))
    assert parsed.path == "/api/v1/agents/connect"
    assert parse_qs(parsed.query)["token"] == [TOKEN]


class FakeHub:
    """In-process fake hub: verifies the handshake, issues one command."""

    def __init__(
        self,
        cmd_frame: dict,
        expect_proof: bool = True,
        agent_config: dict | None = None,
    ):
        self.cmd_frame = cmd_frame
        self.expect_proof = expect_proof
        self.agent_config = agent_config or {}
        self.hello: dict | None = None
        self.logs: list[str] = []
        self.events: list[dict] = []
        self.result: dict | None = None
        self.done = asyncio.Event()
        self.port: int | None = None

    async def __aenter__(self) -> FakeHub:
        self._server = await websockets.serve(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc) -> None:
        self._server.close()
        await self._server.wait_closed()

    async def _handle(self, ws) -> None:
        try:
            query = parse_qs(urlparse(ws.request.path).query)
            assert query.get("token") == [TOKEN], "token missing from connect query"
            await ws.send(json.dumps({"type": "challenge", "nonce": NONCE}))
            proof = json.loads(await ws.recv())
            assert proof["type"] == "proof"
            if self.expect_proof:
                assert proof["hmac"] == compute_proof(TOKEN, NONCE)
                await ws.send(json.dumps({"type": "welcome", "agent_id": "agent-test"}))
            else:
                await ws.send(json.dumps({"type": "error", "message": "bad proof"}))
                return
            self.hello = json.loads(await ws.recv())
            await ws.send(json.dumps(self.cmd_frame))
            while True:
                frame = json.loads(await ws.recv())
                if frame["type"] == "log":
                    self.logs.append(frame["line"])
                elif frame["type"] == "event":
                    self.events.append(frame)
                elif frame["type"] == "result":
                    self.result = frame
                    break
            await ws.send(json.dumps({"type": "bye", "reason": "test done"}))
        finally:
            self.done.set()

    async def run_agent(self) -> None:
        cfg = AgentConfig(
            hub_url=f"ws://127.0.0.1:{self.port}",
            token=TOKEN,
            name="test-agent",
            **self.agent_config,
        )
        stop = asyncio.Event()
        async with websockets.connect(connect_url(cfg)) as ws:
            with contextlib.suppress(ProtocolError):
                # a hub rejection ends the session (run_forever would back off + retry)
                await AgentSession(cfg, ws).run(stop)


def _round_trip(
    cmd_frame: dict, expect_proof: bool = True, agent_config: dict | None = None
) -> FakeHub:
    async def go() -> FakeHub:
        async with FakeHub(cmd_frame, expect_proof, agent_config) as hub:
            await asyncio.wait_for(
                asyncio.gather(hub.run_agent(), hub.done.wait()), timeout=15
            )
            return hub

    return asyncio.run(go())


def test_command_round_trip_echo():
    hub = _round_trip(
        {
            "type": "command",
            "id": "cmd-1",
            "cmd": ["sh", "-c", "echo hello-out; echo hello-err >&2"],
        }
    )
    assert hub.hello is not None
    assert hub.hello["type"] == "hello"
    assert hub.hello["agent_id"] == "agent-test"
    assert hub.hello["hostname"] == "test-agent"
    assert hub.hello["caps"] == ["command", "scan_bmc", "file_write"]
    assert hub.hello["version"]
    assert hub.result is not None
    assert hub.result["id"] == "cmd-1"
    assert hub.result["rc"] == 0
    assert "hello-out" in hub.logs
    assert "hello-err" in hub.logs
    assert "hello-out" in hub.result["stdout"]
    assert "hello-err" in hub.result["stderr"]


def test_command_nonzero_rc_and_env():
    hub = _round_trip(
        {
            "type": "command",
            "id": "cmd-2",
            "cmd": ["sh", "-c", 'echo "$GSC_TEST_VAR"; exit 3'],
            "env": {"GSC_TEST_VAR": "from-hub"},
        }
    )
    assert hub.result["rc"] == 3
    assert "from-hub" in hub.logs


def test_command_timeout_kills_and_reports():
    hub = _round_trip(
        {"type": "command", "id": "cmd-3", "cmd": ["sleep", "30"], "timeout": 0.2}
    )
    assert hub.result["rc"] == 124
    assert any("timed out" in line for line in hub.logs)


def test_bad_proof_rejected_by_hub():
    hub = _round_trip(
        {"type": "command", "id": "cmd-4", "cmd": ["true"]}, expect_proof=False
    )
    assert hub.hello is None  # session ended at the error frame
    assert hub.result is None


def test_handshake_wrong_first_frame_raises():
    async def go() -> None:
        async def hub(ws) -> None:
            await ws.send(json.dumps({"type": "welcome", "agent_id": "x"}))

        server = await websockets.serve(hub, "127.0.0.1", 0)
        try:
            port = server.sockets[0].getsockname()[1]
            cfg = AgentConfig(hub_url=f"ws://127.0.0.1:{port}", token=TOKEN, name="t")
            async with websockets.connect(connect_url(cfg)) as ws:
                with pytest.raises(ProtocolError, match="challenge"):
                    await AgentSession(cfg, ws).handshake()
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(go())


def _fake_prober(live: dict[str, dict]):
    """Monkeypatchable stand-in for probe_redfish: live hosts answer, rest don't."""

    async def fake_probe(ip: str, timeout: float = 0.1) -> dict | None:
        return live.get(ip)

    return fake_probe


def test_scan_bmc_reports_live_hosts(monkeypatch):
    live = {
        "10.0.0.2": {
            "ip": "10.0.0.2",
            "vendor": "HPE",
            "model": "iLO 5",
            "title": "iLO",
        },
        "10.0.0.4": {
            "ip": "10.0.0.4",
            "vendor": "Dell",
            "model": "iDRAC9",
            "title": "",
        },
    }
    monkeypatch.setattr(agent_main, "probe_redfish", _fake_prober(live))
    hub = _round_trip(
        {"type": "command", "id": "scan-1", "scan_bmc": {"subnet": "10.0.0.0/29"}}
    )
    assert hub.result is not None
    assert hub.result["id"] == "scan-1"
    assert hub.result["rc"] == 0
    assert hub.result["found"] == 2
    found = [e for e in hub.events if e["kind"] == "bmc_found"]
    assert {e["payload"]["ip"] for e in found} == {"10.0.0.2", "10.0.0.4"}
    by_ip = {e["payload"]["ip"]: e["payload"] for e in found}
    assert by_ip["10.0.0.2"]["vendor"] == "HPE"
    assert by_ip["10.0.0.4"]["model"] == "iDRAC9"
    assert any("found 2 BMC(s)" in line for line in hub.logs)


def test_scan_bmc_rejects_oversized_subnet(monkeypatch):
    calls: list[str] = []

    async def spy_probe(ip: str, timeout: float = 0.1) -> None:
        calls.append(ip)
        return None

    monkeypatch.setattr(agent_main, "probe_redfish", spy_probe)
    hub = _round_trip(
        {"type": "command", "id": "scan-2", "scan_bmc": {"subnet": "10.0.0.0/16"}}
    )
    assert hub.result["rc"] == 2
    assert hub.result["found"] == 0
    assert hub.events == []
    assert calls == []  # sweep never probed a host
    assert any("too large" in line for line in hub.logs)


def test_scan_bmc_rejects_invalid_subnet():
    hub = _round_trip(
        {"type": "command", "id": "scan-3", "scan_bmc": {"subnet": "not-a-cidr"}}
    )
    assert hub.result["rc"] == 2
    assert hub.result["found"] == 0


def test_parse_leases_dnsmasq_format():
    leases = parse_leases(
        "1000 aa:bb:cc:dd:ee:ff 10.0.0.10 node-1 01:aa:bb:cc:dd:ee:ff\n"
        "2000 11:22:33:44:55:66 10.0.0.11 * *\n"
    )
    assert leases == {
        "aa:bb:cc:dd:ee:ff": ("10.0.0.10", "node-1"),
        "11:22:33:44:55:66": ("10.0.0.11", ""),
    }


def test_pxe_watcher_reports_only_new_leases(tmp_path):
    async def go() -> list[dict]:
        leases = tmp_path / "dnsmasq.leases"
        leases.write_text("1000 aa:bb:cc:dd:ee:ff 10.0.0.10 oldhost *\n")
        events: list[dict] = []
        stop = asyncio.Event()

        async def send_event(payload: dict) -> None:
            events.append(payload)

        task = asyncio.create_task(
            watch_pxe_leases(str(leases), send_event, stop, poll_interval=0.05)
        )
        try:
            # let the watcher take its first (skip-only) read
            for _ in range(100):
                if events or task.done():
                    break
                await asyncio.sleep(0.02)
            assert events == []  # pre-existing lease not reported
            with leases.open("a") as handle:
                handle.write("2000 11:22:33:44:55:66 10.0.0.11 newhost *\n")
            for _ in range(100):
                if events:
                    break
                await asyncio.sleep(0.02)
        finally:
            stop.set()
            await task
        return events

    events = asyncio.run(go())
    assert events == [
        {"mac": "11:22:33:44:55:66", "ip": "10.0.0.11", "hostname": "newhost"}
    ]


def test_pxe_watcher_reports_changed_lease(tmp_path):
    async def go() -> list[dict]:
        leases = tmp_path / "dnsmasq.leases"
        leases.write_text("1000 aa:bb:cc:dd:ee:ff 10.0.0.10 host *\n")
        events: list[dict] = []
        stop = asyncio.Event()

        async def send_event(payload: dict) -> None:
            events.append(payload)

        seen: dict = {}
        task = asyncio.create_task(
            watch_pxe_leases(
                str(leases), send_event, stop, poll_interval=0.05, seen=seen
            )
        )
        try:
            for _ in range(100):
                if seen:
                    break
                await asyncio.sleep(0.02)
            # same MAC, new IP/hostname => report again
            leases.write_text("3000 aa:bb:cc:dd:ee:ff 10.0.0.20 renamed *\n")
            for _ in range(100):
                if events:
                    break
                await asyncio.sleep(0.02)
        finally:
            stop.set()
            await task
        return events

    events = asyncio.run(go())
    assert events == [
        {"mac": "aa:bb:cc:dd:ee:ff", "ip": "10.0.0.20", "hostname": "renamed"}
    ]


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def test_parse_allowed_roots_defaults_to_etc_genestack():
    assert parse_allowed_roots(None) == ("/etc/genestack",)
    assert parse_allowed_roots("") == ("/etc/genestack",)
    assert parse_allowed_roots("::") == ("/etc/genestack",)


def test_parse_allowed_roots_colon_separated_override():
    assert parse_allowed_roots("/srv/a:/srv/b") == ("/srv/a", "/srv/b")


def test_load_config_allowed_root_override_via_env():
    env = {
        "GSC_HUB_URL": "ws://hub:8080",
        "GSC_AGENT_TOKEN": TOKEN,
        "GSC_ALLOWED_ROOT": "/srv/genestack:/opt/genestack",
    }
    cfg = load_config(env)
    # roots are stored realpath-resolved (symlinks like /opt are followed)
    assert cfg.allowed_roots == (
        os.path.realpath("/srv/genestack"),
        os.path.realpath("/opt/genestack"),
    )
    env.pop("GSC_ALLOWED_ROOT")
    assert load_config(env).allowed_roots == ("/etc/genestack",)


def test_file_write_content_mode_and_parent_dirs(tmp_path):
    target = tmp_path / "inventory" / "nested" / "inventory.yaml"
    result = write_file_from_frame(
        {
            "type": "file_write",
            "id": "fw-1",
            "path": str(target),
            "b64": _b64(b"hello: world\n"),
            "mode": 0o640,  # decimal int on the wire (e.g. 416)
        },
        (str(tmp_path),),
    )
    assert result == {"rc": 0, "bytes": len(b"hello: world\n")}
    assert target.read_bytes() == b"hello: world\n"
    assert (target.stat().st_mode & 0o777) == 0o640


def test_file_write_default_mode_is_0644(tmp_path):
    target = tmp_path / "clouds.yaml"
    result = write_file_from_frame(
        {"path": str(target), "b64": _b64(b"clouds: {}")}, (str(tmp_path),)
    )
    assert result["rc"] == 0
    assert (target.stat().st_mode & 0o777) == 0o644


def test_file_write_mode_applies_to_preexisting_file(tmp_path):
    target = tmp_path / "existing.yaml"
    target.write_text("old")
    os.chmod(target, 0o600)
    result = write_file_from_frame(
        {"path": str(target), "b64": _b64(b"new"), "mode": 0o644}, (str(tmp_path),)
    )
    assert result["rc"] == 0
    assert (target.stat().st_mode & 0o777) == 0o644


def test_file_write_backup_created_and_never_overwritten(tmp_path):
    target = tmp_path / "inventory" / "inventory.yaml"
    target.parent.mkdir(parents=True)
    target.write_text("version-1")
    backup_dir = tmp_path / ".console-backup" / "batch1"
    roots = (str(tmp_path),)

    # first write: backs up version-1
    r1 = write_file_from_frame(
        {
            "path": str(target),
            "b64": _b64(b"version-2"),
            "backup_dir": str(backup_dir),
        },
        roots,
    )
    assert r1["rc"] == 0
    backup1 = backup_dir / "inventory" / "inventory.yaml"
    assert backup1.read_text() == "version-1"
    assert target.read_text() == "version-2"

    # second write in the same batch: earlier backup is kept, new one suffixed
    r2 = write_file_from_frame(
        {
            "path": str(target),
            "b64": _b64(b"version-3"),
            "backup_dir": str(backup_dir),
        },
        roots,
    )
    assert r2["rc"] == 0
    assert backup1.read_text() == "version-1"  # untouched
    backup2 = backup_dir / "inventory" / "inventory.yaml.1"
    assert backup2.read_text() == "version-2"
    assert target.read_text() == "version-3"


def test_file_write_no_backup_when_target_missing(tmp_path):
    target = tmp_path / "fresh.yaml"
    backup_dir = tmp_path / "backup"
    result = write_file_from_frame(
        {"path": str(target), "b64": _b64(b"new"), "backup_dir": str(backup_dir)},
        (str(tmp_path),),
    )
    assert result["rc"] == 0
    assert not backup_dir.exists()


@pytest.mark.parametrize(
    "bad_path",
    [
        "/etc/passwd",  # outside the allowed root
        "relative/path.yaml",  # not absolute
        "sub/../../escape.yaml",  # relative traversal
    ],
)
def test_file_write_rejects_bad_paths(tmp_path, bad_path):
    result = write_file_from_frame(
        {"path": bad_path, "b64": _b64(b"x")}, (str(tmp_path),)
    )
    assert result["rc"] == 1
    assert result["error"]


def test_file_write_rejects_dotdot_traversal_escaping_root(tmp_path):
    escape = os.path.join(str(tmp_path), "..", "escape.yaml")
    result = write_file_from_frame(
        {"path": escape, "b64": _b64(b"x")}, (str(tmp_path),)
    )
    assert result["rc"] == 1
    assert "outside allowed root" in result["error"]
    assert not (tmp_path.parent / "escape.yaml").exists()


def test_file_write_rejects_garbage_b64(tmp_path):
    result = write_file_from_frame(
        {"path": str(tmp_path / "x.yaml"), "b64": "!!!not-base64!!!"},
        (str(tmp_path),),
    )
    assert result["rc"] == 1
    assert "base64" in result["error"]
    assert not (tmp_path / "x.yaml").exists()


def test_file_write_rejects_missing_b64_and_bad_mode(tmp_path):
    missing = write_file_from_frame(
        {"path": str(tmp_path / "x.yaml")}, (str(tmp_path),)
    )
    assert missing["rc"] == 1
    assert "b64" in missing["error"]
    bad_mode = write_file_from_frame(
        {"path": str(tmp_path / "x.yaml"), "b64": _b64(b"x"), "mode": "not-a-mode"},
        (str(tmp_path),),
    )
    assert bad_mode["rc"] == 1
    assert "mode" in bad_mode["error"]


def test_file_write_round_trip_via_fake_hub(tmp_path):
    target = tmp_path / "inventory" / "inventory.yaml"
    hub = _round_trip(
        {
            "type": "file_write",
            "id": "fw-rt-1",
            "path": str(target),
            "b64": _b64(b"rendered: true\n"),
            "mode": 0o640,
        },
        agent_config={"allowed_roots": (str(tmp_path),)},
    )
    assert hub.result is not None
    assert hub.result["id"] == "fw-rt-1"
    assert hub.result["rc"] == 0
    assert "error" not in hub.result
    assert target.read_bytes() == b"rendered: true\n"
    assert (target.stat().st_mode & 0o777) == 0o640
    assert any("wrote" in line and str(target) in line for line in hub.logs)


def test_file_write_round_trip_rejected_path_via_fake_hub(tmp_path):
    hub = _round_trip(
        {
            "type": "file_write",
            "id": "fw-rt-2",
            "path": "/etc/passwd",
            "b64": _b64(b"nope"),
        },
        agent_config={"allowed_roots": (str(tmp_path),)},
    )
    assert hub.result["rc"] == 1
    assert hub.result["error"]
