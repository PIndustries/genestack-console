"""Agent command relay tests: DB-backed dispatch, agent_exec, executor pick."""

from __future__ import annotations

import asyncio
import base64
import os
import shutil
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select
from starlette.websockets import WebSocketDisconnect

from app.db import SessionLocal
from app.models import AgentCommand, Environment
from app.services import agent_relay, agents
from app.services.envcontext import build_context
from app.services.executors import pick_executor


def _suffix() -> str:
    return uuid.uuid4().hex[:8]


def _create_env(client, headers, prefix="relay-env", **fields) -> dict:
    resp = client.post(
        "/api/v1/environments",
        headers=headers,
        json={"name": f"{prefix}-{_suffix()}", **fields},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _create_token(client, headers, env_id: str, name: str = "default") -> dict:
    resp = client.post(
        f"/api/v1/environments/{env_id}/agent/token",
        headers=headers,
        json={"name": name},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _handshake(ws, token: str) -> str:
    """Complete the challenge/proof handshake as a fake agent; returns agent_id."""
    challenge = ws.receive_json()
    assert challenge["type"] == "challenge"
    ws.send_json({"type": "proof", "hmac": agents.proof_for(token, challenge["nonce"])})
    welcome = ws.receive_json()
    assert welcome["type"] == "welcome"
    return welcome["agent_id"]


def _wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def _fake_exec_agent_loop(ws, frames: list, rc: int = 0, reply: bool = True) -> None:
    """Answer command frames with a log line + result; execute file_write locally.

    Every received frame is appended to ``frames``. reply=False swallows
    command frames without answering (relay timeout tests).
    """
    import anyio

    try:
        while True:
            frame = ws.receive_json()
            frames.append(frame)
            ftype = frame.get("type")
            if ftype == "command":
                if not reply:
                    continue
                ws.send_json(
                    {"type": "log", "id": frame["id"], "line": "fake-output-line"}
                )
                ws.send_json(
                    {
                        "type": "result",
                        "id": frame["id"],
                        "rc": rc,
                        "stdout": "fake stdout",
                        "stderr": "",
                    }
                )
            elif ftype == "file_write":
                try:
                    path = Path(frame["path"])
                    data = base64.b64decode(frame["b64"])
                    path.parent.mkdir(parents=True, exist_ok=True)
                    backup_dir = frame.get("backup_dir")
                    if backup_dir and path.exists():
                        bdir = Path(backup_dir)
                        bdir.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(path, bdir / path.name)
                    path.write_bytes(data)
                    os.chmod(path, int(str(frame.get("mode") or "0644"), 8))
                    ws.send_json({"type": "result", "id": frame["id"], "rc": 0})
                except Exception as exc:  # noqa: BLE001
                    ws.send_json(
                        {
                            "type": "result",
                            "id": frame["id"],
                            "rc": 1,
                            "error": str(exc),
                        }
                    )
    except (WebSocketDisconnect, anyio.EndOfStream):
        pass


def _start_relay_pump(interval: float = 0.05) -> threading.Event:
    """Drive AgentRelay.poll_once from a daemon thread; returns the stop event."""
    stop = threading.Event()
    relay = agent_relay.AgentRelay(SessionLocal, interval=interval)

    def _pump() -> None:
        while not stop.is_set():
            asyncio.run(relay.poll_once())
            time.sleep(interval)

    threading.Thread(target=_pump, daemon=True).start()
    return stop


def _insert_row(env_id: str, kind: str, payload: dict) -> str:
    db = SessionLocal()
    try:
        row = AgentCommand(environment_id=env_id, kind=kind, payload=payload)
        db.add(row)
        db.commit()
        return row.id
    finally:
        db.close()


def _get_row(row_id: str) -> AgentCommand | None:
    db = SessionLocal()
    try:
        row = db.get(AgentCommand, row_id)
        if row is not None:
            db.expunge(row)
        return row
    finally:
        db.close()


def _wait_terminal(row_id: str, timeout: float = 10.0) -> AgentCommand:
    row = None

    def _terminal() -> bool:
        nonlocal row
        row = _get_row(row_id)
        return row is not None and row.status in agent_relay.TERMINAL_STATUSES

    assert _wait_for(
        _terminal, timeout
    ), f"row {row_id} never reached a terminal status"
    return row


# ---------------------------------------------------------------------------
# Relay dispatch
# ---------------------------------------------------------------------------


def test_relay_dispatch_run_command_happy_path(client, admin_headers):
    env = _create_env(client, admin_headers)
    data = _create_token(client, admin_headers, env["id"])
    url = f"/api/v1/agents/connect?token={data['token']}"
    stop = _start_relay_pump()
    frames: list = []
    try:
        with client.websocket_connect(url) as ws:
            _handshake(ws, data["token"])
            threading.Thread(
                target=_fake_exec_agent_loop, args=(ws, frames), daemon=True
            ).start()

            row_id = _insert_row(
                env["id"], "run_command", {"cmd": ["uptime"], "timeout": 10}
            )
            row = _wait_terminal(row_id)
    finally:
        stop.set()

    assert row.status == "done"
    assert row.result["rc"] == 0
    assert row.result["stdout"] == "fake stdout"
    assert "fake-output-line" in row.log_text
    assert row.finished_at is not None

    # The frame id is the row id; shape follows the locked command contract.
    commands = [f for f in frames if f.get("type") == "command"]
    assert len(commands) == 1
    assert commands[0]["id"] == row_id
    assert commands[0]["cmd"] == ["uptime"]
    assert commands[0]["timeout"] == 10


def test_relay_dispatch_file_write_happy_path(client, admin_headers, tmp_path):
    env = _create_env(client, admin_headers)
    data = _create_token(client, admin_headers, env["id"])
    url = f"/api/v1/agents/connect?token={data['token']}"
    target = tmp_path / "sub" / "rendered.yaml"
    payload = {
        "path": str(target),
        "b64": base64.b64encode(b"key: value\n").decode(),
        "mode": "0644",
        "backup_dir": str(tmp_path / ".console-backup" / "ts" / "sub"),
    }
    stop = _start_relay_pump()
    frames: list = []
    try:
        with client.websocket_connect(url) as ws:
            _handshake(ws, data["token"])
            threading.Thread(
                target=_fake_exec_agent_loop, args=(ws, frames), daemon=True
            ).start()

            row_id = _insert_row(env["id"], "file_write", payload)
            row = _wait_terminal(row_id)
    finally:
        stop.set()

    assert row.status == "done"
    assert row.result == {"rc": 0}
    assert target.read_text(encoding="utf-8") == "key: value\n"

    writes = [f for f in frames if f.get("type") == "file_write"]
    assert len(writes) == 1
    assert writes[0]["id"] == row_id
    assert writes[0]["path"] == str(target)
    assert writes[0]["b64"] == payload["b64"]
    assert writes[0]["mode"] == "0644"
    assert writes[0]["backup_dir"] == payload["backup_dir"]


def test_relay_no_agent_fails_row(client, admin_headers):
    env = _create_env(client, admin_headers)
    row_id = _insert_row(env["id"], "run_command", {"cmd": ["uptime"], "timeout": 10})
    stop = _start_relay_pump()
    try:
        row = _wait_terminal(row_id)
    finally:
        stop.set()
    assert row.status == "failed"
    assert row.result == {"error": "no agent connected for this env"}


def test_relay_dispatch_failover_to_second_agent(client, admin_headers):
    """First agent's socket dies mid-command; the relay retries on the second."""
    import contextlib

    env = _create_env(client, admin_headers)
    tok_a = _create_token(client, admin_headers, env["id"], name="agent-a")
    tok_b = _create_token(client, admin_headers, env["id"], name="agent-b")
    url_a = f"/api/v1/agents/connect?token={tok_a['token']}"
    url_b = f"/api/v1/agents/connect?token={tok_b['token']}"
    stop = _start_relay_pump()
    frames_b: list = []
    try:
        with client.websocket_connect(url_a) as ws_a:
            _handshake(ws_a, tok_a["token"])
            with client.websocket_connect(url_b) as ws_b:
                _handshake(ws_b, tok_b["token"])
                threading.Thread(
                    target=_fake_exec_agent_loop, args=(ws_b, frames_b), daemon=True
                ).start()
                assert _wait_for(
                    lambda: len(agents.registry.records_for_env(env["id"])) == 2
                )

                # agent-a takes the command frame, then drops the socket.
                def _dying_agent() -> None:
                    with contextlib.suppress(Exception):
                        ws_a.receive_json()
                        ws_a.close()

                threading.Thread(target=_dying_agent, daemon=True).start()

                # Round-robin picks the first-connected agent (agent-a) first.
                row_id = _insert_row(
                    env["id"], "run_command", {"cmd": ["uptime"], "timeout": 10}
                )
                row = _wait_terminal(row_id)
    finally:
        stop.set()

    assert row.status == "done"
    assert row.result["rc"] == 0
    assert row.result["stdout"] == "fake stdout"

    # The retry carried the same row id to the second agent.
    commands_b = [f for f in frames_b if f.get("type") == "command"]
    assert len(commands_b) == 1
    assert commands_b[0]["id"] == row_id


def test_relay_timeout_marks_row(client, admin_headers):
    env = _create_env(client, admin_headers)
    data = _create_token(client, admin_headers, env["id"])
    url = f"/api/v1/agents/connect?token={data['token']}"
    stop = _start_relay_pump()
    frames: list = []
    try:
        with client.websocket_connect(url) as ws:
            _handshake(ws, data["token"])
            # Agent receives frames but never replies.
            threading.Thread(
                target=_fake_exec_agent_loop,
                args=(ws, frames),
                kwargs={"reply": False},
                daemon=True,
            ).start()

            row_id = _insert_row(
                env["id"], "run_command", {"cmd": ["sleep"], "timeout": 0.3}
            )
            row = _wait_terminal(row_id)
    finally:
        stop.set()

    assert row.status == "timeout"
    assert "timed out" in row.result["error"]
    assert row.finished_at is not None


# ---------------------------------------------------------------------------
# agent_exec (sync client)
# ---------------------------------------------------------------------------


def test_agent_exec_roundtrip(client, admin_headers):
    env = _create_env(client, admin_headers)
    data = _create_token(client, admin_headers, env["id"])
    url = f"/api/v1/agents/connect?token={data['token']}"
    stop = _start_relay_pump()
    lines: list[str] = []
    frames: list = []
    try:
        with client.websocket_connect(url) as ws:
            _handshake(ws, data["token"])
            threading.Thread(
                target=_fake_exec_agent_loop, args=(ws, frames), daemon=True
            ).start()

            result = agent_relay.agent_exec(
                env["id"],
                "run_command",
                {"cmd": ["hostname"], "timeout": 10},
                timeout=10,
                log_cb=lines.append,
                poll_interval=0.1,
            )
    finally:
        stop.set()

    assert result["rc"] == 0
    assert result["stdout"] == "fake stdout"
    assert result["stderr"] == ""
    assert "fake-output-line" in lines


def test_agent_exec_no_agent_returns_error(client, admin_headers):
    env = _create_env(client, admin_headers)
    stop = _start_relay_pump()
    try:
        result = agent_relay.agent_exec(
            env["id"],
            "run_command",
            {"cmd": ["uptime"], "timeout": 5},
            timeout=5,
            poll_interval=0.1,
        )
    finally:
        stop.set()
    assert result == {"error": "no agent connected for this env"}


def test_agent_exec_rejects_unknown_kind():
    with pytest.raises(ValueError, match="unknown kind"):
        agent_relay.agent_exec("env-x", "rm_rf", {}, timeout=1)


# ---------------------------------------------------------------------------
# Executor preference (agent -> ssh -> local)
# ---------------------------------------------------------------------------


def _db_env(**fields) -> Environment:
    db = SessionLocal()
    try:
        env = Environment(name=f"env-pick-{_suffix()}", **fields)
        db.add(env)
        db.commit()
        db.refresh(env)
        db.expunge(env)
        return env
    finally:
        db.close()


def _grant_agent(env_id: str, *, last_seen: datetime) -> None:
    db = SessionLocal()
    try:
        cred, _token = agents.create_credential(db, env_id, "default")
        cred.last_seen = last_seen
        db.add(cred)
        db.commit()
    finally:
        db.close()


def test_pick_executor_prefers_agent_over_ssh():
    env = _db_env(deployer_ssh_host="deployer.example.com", deployer_ssh_user="deploy")
    _grant_agent(env.id, last_seen=datetime.now(timezone.utc))
    choice = pick_executor(env, build_context(env))
    assert choice.kind == "agent"
    assert choice.agent_env_id == env.id


def test_pick_executor_ssh_when_no_agent():
    env = _db_env(deployer_ssh_host="deployer.example.com")
    choice = pick_executor(env, build_context(env))
    assert choice.kind == "ssh"
    assert choice.ssh_target == "deployer.example.com"


def test_pick_executor_falls_back_when_agent_stale():
    env = _db_env(deployer_ssh_host="deployer.example.com")
    stale = datetime.now(timezone.utc) - timedelta(
        seconds=agents.OFFLINE_AFTER_SECONDS + 60
    )
    _grant_agent(env.id, last_seen=stale)
    choice = pick_executor(env, build_context(env))
    assert choice.kind == "ssh"


def test_pick_executor_local_without_agent_or_ssh():
    env = _db_env()
    choice = pick_executor(env, build_context(env))
    assert choice.kind == "local"
    assert choice.agent_env_id is None
    assert choice.ssh_target is None


# ---------------------------------------------------------------------------
# Pipeline run via agent (job level)
# ---------------------------------------------------------------------------


def test_pipeline_run_dispatches_via_agent(client, admin_headers, tmp_path):
    config_dir = tmp_path / "etc-genestack"
    config_dir.mkdir()
    env = _create_env(
        client, admin_headers, genestack_config_dir=str(config_dir), dry_run=False
    )
    data = _create_token(client, admin_headers, env["id"])
    url = f"/api/v1/agents/connect?token={data['token']}"
    stop = _start_relay_pump()
    frames: list = []
    try:
        with client.websocket_connect(url) as ws:
            _handshake(ws, data["token"])
            threading.Thread(
                target=_fake_exec_agent_loop, args=(ws, frames), daemon=True
            ).start()

            resp = client.post(
                f"/api/v1/environments/{env['id']}/jobs",
                headers=admin_headers,
                json={
                    "operation": "genestack.pipeline.run",
                    "params": {"stage": "core"},
                    "run_sync": True,
                },
            )
            assert resp.status_code == 201, resp.text
            job = resp.json()
            assert job["status"] == "success", job["error"]
    finally:
        stop.set()

    # Every pipeline item ran as a command frame through the agent channel.
    commands = [f for f in frames if f.get("type") == "command"]
    assert commands, "expected pipeline commands dispatched via the agent"
    assert all(f["cmd"][0] == "bash" for f in commands)

    log_text = client.get(f"/api/v1/jobs/{job['id']}", headers=admin_headers).json()[
        "log_text"
    ]
    assert "via agent" in log_text

    # And the relay rows all completed.
    db = SessionLocal()
    try:
        rows = db.scalars(
            select(AgentCommand).where(AgentCommand.environment_id == env["id"])
        ).all()
        assert rows
        assert all(row.status == "done" for row in rows)
    finally:
        db.close()
