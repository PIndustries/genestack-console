"""Transport routing tests: every command executor goes through the selected
agent / ssh / local path (A1 relay handlers + A2 bridge chokepoints).

Focus is on threading the picked executor (``agent_env_id``) into the bridge
and, for the A1 handlers, routing through the DB-backed agent relay. Bridge
and relay calls are captured with monkeypatches so the tests are fast and do
not need a live agent websocket (the end-to-end WS path is covered by
tests/test_agent_relay.py, tests/test_agents.py and tests/test_discovery.py).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.db import SessionLocal
from app.models import Environment
from app.services import (
    agent_relay,
    collector,
    genestack_bridge as bridge,
    host_prepare,
    openstack_ops,
    reconcile,
)
from app.services.catalog import get_operation
from app.services.envcontext import build_context
from app.services.executors import pick_executor
from app.services.job_runner import JobRunner
from tests.test_agent_relay import _db_env, _grant_agent


def _new_env(**fields) -> Environment:
    env = _db_env(**fields)
    _grant_agent(env.id, last_seen=datetime.now(timezone.utc))
    return env


def _no_dispatch_counter() -> tuple[dict[str, int], Any]:
    """A fake agent_exec that records whether it was called at all."""
    calls = {"n": 0}

    def fake(*_a, **_k):
        calls["n"] += 1
        return {}

    return calls, fake


# ---------------------------------------------------------------------------
# pick_executor value used for threading
# ---------------------------------------------------------------------------


def test_pick_executor_agent_env_id_equals_env_id():
    env = _new_env(deployer_ssh_host="deployer.example.com")
    choice = pick_executor(env, build_context(env))
    assert choice.kind == "agent"
    assert choice.agent_env_id == env.id


# ---------------------------------------------------------------------------
# bridge.run_command: the A1/A2 agent chokepoint
# ---------------------------------------------------------------------------


def test_run_command_agent_routes_through_relay(monkeypatch):
    calls: dict[str, Any] = {}

    def fake_agent_exec(env_id, kind, payload, *, timeout, log_cb=None, **kw):
        calls["env_id"] = env_id
        calls["kind"] = kind
        calls["payload"] = payload
        calls["timeout"] = timeout
        return {"rc": 0, "stdout": "ok-out", "stderr": ""}

    monkeypatch.setattr(agent_relay, "agent_exec", fake_agent_exec)
    result = bridge.run_command(
        ["bash", "scripts/backup-mariadb.sh"],
        cwd=Path("/opt/genestack"),
        dry_run=False,
        ssh_target=None,
        remote_env={"GENESTACK_CONFIG": "/etc/genestack"},
        agent_env_id="env-agent-1",
        timeout=300,
    )
    assert result["via"] == "agent"
    assert result["returncode"] == 0
    assert result["stdout"] == "ok-out"
    assert calls["env_id"] == "env-agent-1"
    assert calls["kind"] == "run_command"
    assert calls["payload"]["cmd"] == ["bash", "scripts/backup-mariadb.sh"]
    assert calls["payload"]["cwd"] == "/opt/genestack"
    assert calls["payload"]["env"] == {"GENESTACK_CONFIG": "/etc/genestack"}
    assert calls["payload"]["timeout"] == 300


def test_run_command_agent_maps_error_to_rc(monkeypatch):
    monkeypatch.setattr(
        agent_relay,
        "agent_exec",
        lambda *a, **k: {"error": "no agent connected for this env"},
    )
    result = bridge.run_command(["uptime"], dry_run=False, agent_env_id="env-agent-1")
    assert result["via"] == "agent"
    assert result["returncode"] == 2
    assert result["stderr"] == "no agent connected for this env"


def test_run_command_agent_dry_run_does_not_dispatch(monkeypatch):
    called = {"n": 0}

    def fake_agent_exec(*a, **k):
        called["n"] += 1
        return {"rc": 0}

    monkeypatch.setattr(agent_relay, "agent_exec", fake_agent_exec)
    result = bridge.run_command(
        ["uptime"], dry_run=True, ssh_target=None, agent_env_id="env-agent-1"
    )
    assert result["dry_run"] is True
    assert "via" not in result
    assert called["n"] == 0


# ---------------------------------------------------------------------------
# A2: bridge chokepoints thread agent_env_id from the picked executor
# ---------------------------------------------------------------------------


def _capture_run_command(monkeypatch, holder: dict) -> None:
    def fake(cmd, **kwargs):
        holder["cmd"] = cmd
        holder["kwargs"] = kwargs
        return {"returncode": 0, "stdout": "", "stderr": ""}

    monkeypatch.setattr(bridge, "run_command", fake)


def test_collector_read_host_artifact_threads_agent(monkeypatch):
    env = _new_env(
        deployer_ssh_host="deployer.example.com", genestack_config_dir="/etc/genestack"
    )
    ctx = build_context(env)
    holder: dict[str, Any] = {}
    _capture_run_command(monkeypatch, holder)

    content, error = collector._read_host_artifact(ctx, Path("/etc/genestack/provider"))
    assert error is None
    assert content == ""
    assert holder["kwargs"]["agent_env_id"] == env.id
    assert holder["kwargs"]["ssh_target"] == ctx.ssh_target
    assert holder["kwargs"]["dry_run"] is False
    assert holder["kwargs"]["timeout"] == 30
    ctx.cleanup()


def test_reconcile_fetch_releases_threads_agent(monkeypatch):
    env = _new_env(deployer_ssh_host="deployer.example.com")
    ctx = build_context(env)
    holder: dict[str, Any] = {}

    def fake(cmd, **kwargs):
        holder["kwargs"] = kwargs
        return {"returncode": 0, "stdout": "[]"}

    monkeypatch.setattr(bridge, "run_command", fake)
    releases, error = reconcile._fetch_releases(env, None, ctx=ctx)
    assert error is None
    assert releases == []
    assert holder["kwargs"]["agent_env_id"] == env.id
    assert holder["kwargs"]["ssh_target"] == ctx.ssh_target
    assert holder["kwargs"]["dry_run"] is False
    ctx.cleanup()


def test_openstack_run_threads_agent(monkeypatch):
    env = _new_env(deployer_ssh_host="deployer.example.com")
    ctx = build_context(env)
    holder: dict[str, Any] = {}

    def fake(cmd, **kwargs):
        holder["kwargs"] = kwargs
        return {"returncode": 0, "stdout": "[]"}

    monkeypatch.setattr(bridge, "run_command", fake)
    result = openstack_ops.list_servers(env, None)
    assert result["source"] == "live"
    assert holder["kwargs"]["agent_env_id"] == env.id
    assert holder["kwargs"]["ssh_target"] == ctx.ssh_target
    ctx.cleanup()


def test_host_prepare_threads_agent(monkeypatch):
    env = _new_env(deployer_ssh_host="deployer.example.com")
    ctx = build_context(env)
    holder: dict[str, Any] = {}
    _capture_run_command(monkeypatch, holder)

    host_prepare.run_host_prepare(
        env,
        ctx,
        log=lambda _m: None,
        params={},
        dry_run=True,
        timeout=600,
        extra_env={},
        ssh_target=ctx.ssh_target,
        remote_env=ctx.remote_env(),
        agent_env_id=pick_executor(env, ctx).agent_env_id,
    )
    assert holder["kwargs"]["agent_env_id"] == env.id
    assert holder["kwargs"]["ssh_target"] == ctx.ssh_target
    ctx.cleanup()


# ---------------------------------------------------------------------------
# A2: job handlers thread the picked executor into bridge calls
# ---------------------------------------------------------------------------


def _dispatch_handler(
    op_id: str, env: Environment, params: dict, monkeypatch, holder: dict
):
    db = SessionLocal()
    try:
        runner = JobRunner(db)
        job = runner.create_job(
            operation=op_id,
            params=params,
            environment_id=env.id,
            created_by="transport-test",
        )
        db.commit()
        ctx = build_context(env, runner.settings)
        result = runner._dispatch(
            get_operation(op_id),
            job,
            env,
            log=lambda _m: None,
            ctx=ctx,
            params=params,
            deadline=None,
            check_cancel=lambda: None,
        )
        ctx.cleanup()
        return result
    finally:
        db.close()


def test_handler_backup_mariadb_threads_agent(monkeypatch):
    env = _new_env(deployer_ssh_host="deployer.example.com")
    holder: dict[str, Any] = {}

    def fake(cmd, **kwargs):
        holder["kwargs"] = kwargs
        return {"returncode": 0, "stdout": "", "stderr": "", "dry_run": True}

    monkeypatch.setattr(bridge, "run_command", fake)
    result = _dispatch_handler("genestack.backup_mariadb", env, {}, monkeypatch, holder)
    assert result["ok"] is True
    assert holder["kwargs"]["agent_env_id"] == env.id
    assert holder["kwargs"]["ssh_target"] == "deployer.example.com"


def test_handler_service_enable_threads_agent(monkeypatch):
    env = _new_env(deployer_ssh_host="deployer.example.com")
    holder: dict[str, Any] = {}

    def fake_enable_service(service, gs_root, **kwargs):
        holder["kwargs"] = kwargs
        return {"ok": True, "returncode": 0, "service": service, "script": "x"}

    monkeypatch.setattr(bridge, "enable_service", fake_enable_service)
    params = {"service": "keystone"}
    result = _dispatch_handler(
        "genestack.service.enable", env, params, monkeypatch, holder
    )
    assert result["ok"] is True
    assert holder["kwargs"]["agent_env_id"] == env.id


def test_handler_service_enable_no_agent_uses_ssh(monkeypatch):
    env = _db_env(deployer_ssh_host="deployer.example.com")
    holder: dict[str, Any] = {}

    def fake_enable_service(service, gs_root, **kwargs):
        holder["kwargs"] = kwargs
        return {"ok": True, "returncode": 0, "service": service, "script": "x"}

    monkeypatch.setattr(bridge, "enable_service", fake_enable_service)
    params = {"service": "keystone"}
    result = _dispatch_handler(
        "genestack.service.enable", env, params, monkeypatch, holder
    )
    assert result["ok"] is True
    assert holder["kwargs"]["agent_env_id"] is None
    assert holder["kwargs"]["ssh_target"] == "deployer.example.com"


# ---------------------------------------------------------------------------
# A1: baremetal.bmc_scan and agent.command route through the DB relay
# ---------------------------------------------------------------------------


def _fake_agent_exec(holder: dict) -> Any:
    def fake(env_id, kind, payload, *, timeout, log_cb=None, **kw):
        holder["env_id"] = env_id
        holder["kind"] = kind
        holder["payload"] = payload
        holder["timeout"] = timeout
        return holder.pop("reply", {"rc": 0, "found": 1})

    return fake


def test_bmc_scan_no_agent_precheck_fails(monkeypatch):
    env = _db_env(dry_run=False)
    calls, fake = _no_dispatch_counter()
    monkeypatch.setattr(agent_relay, "agent_exec", fake)
    result = _dispatch_handler(
        "baremetal.bmc_scan", env, {"subnet": "10.4.0.0/24"}, monkeypatch, calls
    )
    assert result["ok"] is False
    assert result["error"] == "no agent connected for this env"
    assert result["returncode"] == 2
    assert calls["n"] == 0  # precheck short-circuits before any relay row


def test_bmc_scan_invalid_cidr_fails(monkeypatch):
    env = _new_env()
    holder: dict[str, Any] = {}
    monkeypatch.setattr(agent_relay, "agent_exec", _fake_agent_exec(holder))
    result = _dispatch_handler(
        "baremetal.bmc_scan", env, {"subnet": "bogus"}, monkeypatch, holder
    )
    assert result["ok"] is False
    assert "invalid CIDR" in result["error"]
    assert holder.get("kind") is None  # no dispatch on validation failure


def test_bmc_scan_routes_scan_bmc_via_relay(monkeypatch):
    env = _new_env(dry_run=False)
    holder: dict[str, Any] = {"reply": {"rc": 0, "found": 3}}
    monkeypatch.setattr(agent_relay, "agent_exec", _fake_agent_exec(holder))
    result = _dispatch_handler(
        "baremetal.bmc_scan", env, {"subnet": "10.5.0.0/24"}, monkeypatch, holder
    )
    assert result["ok"] is True
    assert result["found"] == 3
    assert holder["env_id"] == env.id
    assert holder["kind"] == "scan_bmc"
    assert holder["payload"]["subnet"] == "10.5.0.0/24"


def test_bmc_scan_dry_run_does_not_dispatch(monkeypatch):
    env = _db_env(deployer_ssh_host="h")
    calls, fake = _no_dispatch_counter()
    monkeypatch.setattr(agent_relay, "agent_exec", fake)
    result = _dispatch_handler(
        "baremetal.bmc_scan", env, {"subnet": "10.5.0.0/24"}, monkeypatch, calls
    )
    assert result["ok"] is True
    assert result.get("dry_run") is True
    assert calls["n"] == 0


def test_agent_command_routes_run_command_via_relay(monkeypatch):
    env = _new_env(dry_run=False)
    holder: dict[str, Any] = {
        "reply": {"rc": 0, "stdout": " 12:00 up 1 day", "stderr": ""}
    }
    monkeypatch.setattr(agent_relay, "agent_exec", _fake_agent_exec(holder))
    result = _dispatch_handler(
        "agent.command", env, {"command": "uptime"}, monkeypatch, holder
    )
    assert result["ok"] is True
    assert result["returncode"] == 0
    assert holder["env_id"] == env.id
    assert holder["kind"] == "run_command"
    assert holder["payload"]["cmd"] == ["uptime"]


def test_agent_command_disallowed_rejected(monkeypatch):
    env = _new_env(dry_run=False)
    holder: dict[str, Any] = {}
    monkeypatch.setattr(agent_relay, "agent_exec", _fake_agent_exec(holder))
    result = _dispatch_handler(
        "agent.command", env, {"command": "rm -rf /"}, monkeypatch, holder
    )
    assert result["ok"] is False
    assert "not allowlisted" in result["error"]
    assert holder.get("kind") is None


def test_agent_command_no_agent_precheck_fails(monkeypatch):
    env = _db_env(dry_run=False)
    calls, fake = _no_dispatch_counter()
    monkeypatch.setattr(agent_relay, "agent_exec", fake)
    result = _dispatch_handler(
        "agent.command", env, {"command": "uptime"}, monkeypatch, calls
    )
    assert result["ok"] is False
    assert result["error"] == "no agent connected for this env"
    assert calls["n"] == 0
