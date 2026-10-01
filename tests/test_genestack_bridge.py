"""Unit tests for genestack service enable allowlist."""

from __future__ import annotations

import importlib


def test_allowlist_rejects_rm_and_unknown(genestack_root):
    """Enable-service validation rejects 'rm', unknown, and malformed names."""
    catalog = importlib.import_module("app.services.catalog")
    allow = getattr(catalog, "SERVICE_ENABLE_ALLOWLIST", None)
    assert allow is not None, "SERVICE_ENABLE_ALLOWLIST missing from catalog"

    assert "rm" not in allow
    assert "unknown-service-xyz" not in allow

    bridge = importlib.import_module("app.services.genestack_bridge")
    enable = getattr(bridge, "enable_service", None)
    assert callable(enable), "enable_service not found on genestack_bridge"

    for bad in ("rm", "unknown-service-xyz", "../etc/passwd", "keystone; reboot"):
        result = enable(bad, genestack_root, dry_run=True)
        assert result.get("ok") is False, f"expected reject for {bad!r}: {result}"
        assert result.get("returncode", 1) != 0 or "error" in result


def test_allowlist_accepts_placement(genestack_root):
    """Discovered install scripts (e.g. placement) are accepted."""
    catalog = importlib.import_module("app.services.catalog")
    allow = getattr(catalog, "SERVICE_ENABLE_ALLOWLIST", None)
    assert allow is not None
    assert "placement" in allow

    bridge = importlib.import_module("app.services.genestack_bridge")
    enable = bridge.enable_service
    result = enable("placement", genestack_root, dry_run=True)
    assert result.get("ok") is True, result
    assert result.get("service") == "placement" or "placement" in str(result).lower()


def test_enable_service_forwards_extra_env(monkeypatch, genestack_root):
    """enable_service threads extra_env through to run_command."""
    from app.services import genestack_bridge as bridge

    captured = {}

    def fake_run_command(cmd, **kwargs):
        captured.update(kwargs)
        return {"ok": True, "returncode": 0, "cmd": cmd}

    monkeypatch.setattr(bridge, "run_command", fake_run_command)
    extra = {"GENESTACK_CONFIG": "/etc/genestack-lab", "KUBECONFIG": "/tmp/kube"}
    result = bridge.enable_service(
        "keystone", genestack_root, dry_run=False, extra_env=extra
    )
    assert result.get("ok") is True
    assert captured.get("extra_env") == extra


def test_run_playbook_forwards_extra_env(monkeypatch, genestack_root, tmp_path):
    """run_playbook merges extra_env into the constructed subprocess env."""
    from app.services import genestack_bridge as bridge

    ansible_root = tmp_path / "ansible"
    ansible_root.mkdir()
    (ansible_root / "host_preflight.yml").write_text(
        "- hosts: all\n  tasks: []\n", encoding="utf-8"
    )
    monkeypatch.setattr(
        bridge.shutil, "which", lambda _name: "/usr/bin/ansible-playbook"
    )

    captured = {}

    def fake_run_command(cmd, **kwargs):
        captured.update(kwargs)
        return {"ok": True, "returncode": 0, "cmd": cmd}

    monkeypatch.setattr(bridge, "run_command", fake_run_command)
    extra = {"GENESTACK_CONFIG": "/etc/genestack-lab", "ANSIBLE_INVENTORY": "/inv"}
    result = bridge.run_playbook(
        "host_preflight.yml",
        ansible_root=ansible_root,
        genestack_root=genestack_root,
        dry_run=False,
        extra_env=extra,
    )
    assert result.get("ok") is True
    env_vars = captured.get("env") or {}
    for key, value in extra.items():
        assert env_vars.get(key) == value


def test_run_command_merges_extra_env(monkeypatch):
    """run_command merges extra_env over os.environ when env is not given."""
    from app.services import genestack_bridge as bridge

    captured = {}

    class _Proc:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, **kwargs):
        captured.update(kwargs)
        return _Proc()

    monkeypatch.setattr(bridge.subprocess, "run", fake_run)
    result = bridge.run_command(
        ["echo", "hi"], dry_run=False, extra_env={"GENESTACK_CONFIG": "/etc/gs"}
    )
    assert result["returncode"] == 0
    assert captured["env"]["GENESTACK_CONFIG"] == "/etc/gs"
    # base environment preserved
    assert "PATH" in captured["env"]


# ------------------------------------------------------------- playbook allowlist (B5)


def test_playbook_allowlist_rejects_removed_playbooks(genestack_root, tmp_path):
    """Dead site.yml/ping.yml are no longer allowlisted; allowed names pass."""
    from app.services import genestack_bridge as bridge

    for dead in ("ping.yml", "site.yml"):
        res = bridge.run_playbook(dead, ansible_root=tmp_path, dry_run=True)
        assert res.get("ok") is False, (dead, res)
        assert res.get("returncode") == 2
        assert "not in allowlist" in res.get("error", "")

    # An allowlisted name clears the allowlist (then hits the not-found dry-run
    # fallback, which is ok) — proving the boundary moved, not that it vanished.
    res = bridge.run_playbook("host_preflight.yml", ansible_root=tmp_path, dry_run=True)
    assert res.get("ok") is True, res


# ------------------------------------------------------------- playbook --check (B7)


def test_run_playbook_check_flag(monkeypatch, genestack_root, tmp_path):
    """check=True appends --check to the ansible-playbook argv (and only then)."""
    from app.services import genestack_bridge as bridge

    ansible_root = tmp_path / "ansible"
    ansible_root.mkdir()
    (ansible_root / "host_preflight.yml").write_text(
        "- hosts: all\n  tasks: []\n", encoding="utf-8"
    )
    monkeypatch.setattr(
        bridge.shutil, "which", lambda _name: "/usr/bin/ansible-playbook"
    )

    captured: dict = {}

    def fake_run_command(cmd, **kwargs):
        captured["cmd"] = cmd
        return {"ok": True, "returncode": 0, "cmd": cmd}

    monkeypatch.setattr(bridge, "run_command", fake_run_command)

    res = bridge.run_playbook(
        "host_preflight.yml",
        ansible_root=ansible_root,
        genestack_root=genestack_root,
        dry_run=False,
        check=True,
    )
    assert res.get("ok") is True
    assert "--check" in captured["cmd"]

    res = bridge.run_playbook(
        "host_preflight.yml",
        ansible_root=ansible_root,
        genestack_root=genestack_root,
        dry_run=False,
        check=False,
    )
    assert res.get("ok") is True
    assert "--check" not in captured["cmd"]


# ------------------------------------------------------------- enable_service missing script (B8)


def test_enable_service_missing_script_dry_run_fails(monkeypatch, genestack_root):
    """A discovered-but-missing install script must fail even in dry-run."""
    from app.services import genestack_bridge as bridge

    # 'ghost' clears the deployable-name gate but has no install script on disk.
    monkeypatch.setattr(
        bridge, "discover_deployable_services", lambda _root: frozenset({"ghost"})
    )

    res = bridge.enable_service("ghost", genestack_root, dry_run=True)
    assert res.get("ok") is False, res
    assert res.get("dry_run") is True
    assert res.get("returncode") == 1
    assert "script missing" in res.get("message", "")

    res = bridge.enable_service("ghost", genestack_root, dry_run=False)
    assert res.get("ok") is False
    assert res.get("returncode") == 1
    assert "error" in res
