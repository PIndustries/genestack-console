"""Hypervisor tier tests: discovery, status, serial tail, actions, router, jobs.

Discovery tests run against a fake hypervisor root (tmpdir) with pidfiles
pointing at real live ``sleep`` processes; the qemu argv seen by the
discovery code is faked by patching ``_read_proc_argv``/``_scan_qemu_procs``
(the qemu-detection predicate itself stays stock and is exercised on the
fake qemu argv).
"""

from __future__ import annotations

import os
import signal
import subprocess
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.config import Settings, load_settings
from app.db import Base
from app.models import HostVM
from app.services import hypervisor
from app.services.catalog import get_operation, mutating_operation_ids

# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine, tables=[HostVM.__table__])
    with Session(engine) as session:
        yield session


@pytest.fixture
def root(tmp_path) -> Path:
    return tmp_path.resolve()


@pytest.fixture
def settings(root) -> Settings:
    return Settings(hypervisor_enabled=True, hypervisor_roots=[str(root)])


@pytest.fixture
def sleep_proc():
    proc = subprocess.Popen(
        ["sleep", "300"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL
    )
    try:
        yield proc
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()


def _qemu_argv(
    name: str = "node-t1",
    pidfile: str | None = "node-t1.pid",
    serial: str | None = "serial-t1.log",
    drive: str = "node-t1.qcow2",
    vnc: str | None = None,
) -> list[str]:
    argv = [
        "qemu-system-x86_64",
        "-machine",
        "q35,accel=kvm",
        "-m",
        "1024",
        "-drive",
        f"file={drive},if=virtio,format=qcow2",
        "-display",
        "none",
        "-daemonize",
    ]
    if serial:
        argv += ["-serial", f"file:{serial}"]
    if pidfile:
        argv += ["-pidfile", pidfile]
    if name:
        argv += ["-name", name]
    if vnc:
        argv += ["-vnc", vnc]
    return argv


def _patch_proc(monkeypatch, proc, argv: list[str], workdir: str | None):
    """Make the live `proc` look like a qemu process to the discovery code."""
    monkeypatch.setattr(hypervisor, "_read_proc_argv", lambda pid: list(argv))
    monkeypatch.setattr(hypervisor, "_proc_cwd", lambda pid: workdir)
    monkeypatch.setattr(hypervisor, "_scan_qemu_procs", lambda: [])


# ---------------------------------------------------------------------------
# Config parsing
# ---------------------------------------------------------------------------


def test_settings_defaults_have_hypervisor_section():
    s = Settings()
    assert s.hypervisor_enabled is True
    assert s.hypervisor_roots == ["/var/lib/genestack/vms"]


def test_load_settings_parses_hypervisor_section(tmp_path):
    cfg = {
        "data_dir": str(tmp_path / "data"),
        "hypervisor": {"enabled": False, "roots": ["/srv/vms-a", "/srv/vms-b"]},
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    s = load_settings(path)
    assert s.hypervisor_enabled is False
    assert s.hypervisor_roots == ["/srv/vms-a", "/srv/vms-b"]


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def test_discover_pidfile_live_process(db, settings, root, sleep_proc, monkeypatch):
    argv = _qemu_argv()
    (root / "node-t1.pid").write_text(str(sleep_proc.pid), encoding="utf-8")
    _patch_proc(monkeypatch, sleep_proc, argv, str(root))

    assert hypervisor.discover_vms(db, settings) == 1

    row = db.scalar(select(HostVM))
    assert row is not None
    assert row.name == "node-t1"
    assert row.workdir == str(root)
    assert row.pidfile_path == str(root / "node-t1.pid")
    assert row.serial_log_path == str(root / "serial-t1.log")
    assert row.cmdline == argv
    assert row.source == "discovered"
    # -display none, no -vnc flag: VNC fields stay null.
    assert row.vnc_host is None
    assert row.vnc_port is None
    assert row.novnc_port is None


def test_discover_vnc_flag_parsed(db, settings, root, sleep_proc, monkeypatch):
    argv = _qemu_argv(vnc="127.0.0.1:5")
    (root / "node-t1.pid").write_text(str(sleep_proc.pid), encoding="utf-8")
    _patch_proc(monkeypatch, sleep_proc, argv, str(root))

    assert hypervisor.discover_vms(db, settings) == 1
    row = db.scalar(select(HostVM))
    assert row.vnc_host == "127.0.0.1"
    assert row.vnc_port == 5905


def test_discover_unreadable_pidfile_correlates_by_pidfile_arg(
    db, settings, root, sleep_proc, monkeypatch
):
    """Pidfile present but unreadable (e.g. owned by another user): the live
    qemu process is still matched via its -pidfile argv basename."""
    argv = _qemu_argv()
    pidfile = root / "node-t1.pid"
    pidfile.write_text(str(sleep_proc.pid), encoding="utf-8")
    monkeypatch.setattr(hypervisor, "_read_pidfile", lambda path: None)
    monkeypatch.setattr(
        hypervisor,
        "_scan_qemu_procs",
        lambda: [{"pid": sleep_proc.pid, "argv": list(argv), "cwd": None}],
    )

    assert hypervisor.discover_vms(db, settings) == 1
    row = db.scalar(select(HostVM))
    assert row is not None
    assert row.name == "node-t1"
    # cwd unreadable -> workdir falls back to the pidfile's directory.
    assert row.workdir == str(root)
    assert row.pidfile_path == str(pidfile)


def test_discover_proc_scan_without_pidfile(
    db, settings, root, sleep_proc, monkeypatch
):
    """qemu process with no pidfile: matched by drive basename + cwd."""
    argv = _qemu_argv(name="node-t9", pidfile=None, drive="node-t9.qcow2")
    (root / "node-t9.qcow2").touch()
    monkeypatch.setattr(
        hypervisor,
        "_scan_qemu_procs",
        lambda: [{"pid": sleep_proc.pid, "argv": list(argv), "cwd": str(root)}],
    )

    assert hypervisor.discover_vms(db, settings) == 1
    row = db.scalar(select(HostVM))
    assert row.name == "node-t9"
    assert row.pidfile_path is None
    # Re-discovery upserts the same identity — no duplicate row.
    assert hypervisor.discover_vms(db, settings) == 1
    assert len(db.scalars(select(HostVM)).all()) == 1


def test_discover_stopped_vm_row_retained(db, settings, root, sleep_proc, monkeypatch):
    argv = _qemu_argv()
    (root / "node-t1.pid").write_text(str(sleep_proc.pid), encoding="utf-8")
    _patch_proc(monkeypatch, sleep_proc, argv, str(root))
    assert hypervisor.discover_vms(db, settings) == 1

    sleep_proc.terminate()
    sleep_proc.wait()
    assert hypervisor.discover_vms(db, settings) == 0
    # The row is never deleted — it just reports as stopped.
    rows = db.scalars(select(HostVM)).all()
    assert len(rows) == 1
    status = hypervisor.vm_status(db, settings)
    assert status[0]["running"] is False
    assert status[0]["pid"] is None
    assert status[0]["cpu_percent"] is None
    assert status[0]["rss_bytes"] is None
    assert status[0]["uptime_seconds"] is None


def test_discover_disabled_returns_zero(db, root, monkeypatch):
    settings = Settings(hypervisor_enabled=False, hypervisor_roots=[str(root)])
    monkeypatch.setattr(
        hypervisor,
        "_scan_qemu_procs",
        lambda: pytest.fail("scan must not run when disabled"),
    )
    assert hypervisor.discover_vms(db, settings) == 0


# ---------------------------------------------------------------------------
# vm_status
# ---------------------------------------------------------------------------


def test_vm_status_running_merges_live_stats(
    db, settings, root, sleep_proc, monkeypatch
):
    argv = _qemu_argv()
    (root / "node-t1.pid").write_text(str(sleep_proc.pid), encoding="utf-8")
    _patch_proc(monkeypatch, sleep_proc, argv, str(root))

    status = hypervisor.vm_status(db, settings)
    assert len(status) == 1
    vm = status[0]
    assert set(vm) == {
        "id",
        "name",
        "running",
        "pid",
        "cpu_percent",
        "rss_bytes",
        "uptime_seconds",
        "workdir",
        "serial_log_path",
        "vnc_host",
        "vnc_port",
        "novnc_port",
        "source",
    }
    assert vm["running"] is True
    assert vm["pid"] == sleep_proc.pid
    assert isinstance(vm["cpu_percent"], float)
    assert vm["cpu_percent"] >= 0.0
    assert vm["rss_bytes"] and vm["rss_bytes"] > 0
    assert vm["uptime_seconds"] is not None and vm["uptime_seconds"] >= 0
    assert vm["workdir"] == str(root)
    assert vm["source"] == "discovered"


# ---------------------------------------------------------------------------
# tail_serial
# ---------------------------------------------------------------------------


def _serial_row(db, root, name="node-t1") -> HostVM:
    row = HostVM(
        name=name,
        workdir=str(root),
        pidfile_path=str(root / f"{name}.pid"),
        serial_log_path=str(root / f"{name}.log"),
        cmdline=_qemu_argv(name=name, pidfile=f"{name}.pid", serial=f"{name}.log"),
        source="discovered",
    )
    db.add(row)
    db.commit()
    return row


def test_tail_serial_returns_last_lines(db, settings, root):
    row = _serial_row(db, root)
    log = root / "node-t1.log"
    log.write_text("".join(f"line-{i}\n" for i in range(1, 301)), encoding="utf-8")
    result = hypervisor.tail_serial(db, settings, row.id, lines=200)
    assert result["name"] == "node-t1"
    assert result["path"] == str(log)
    assert len(result["lines"]) == 200
    assert result["lines"][-1] == "line-300"
    assert result["lines"][0] == "line-101"


def test_tail_serial_caps_at_1000(db, settings, root):
    row = _serial_row(db, root)
    log = root / "node-t1.log"
    log.write_text("".join(f"line-{i}\n" for i in range(1, 1501)), encoding="utf-8")
    result = hypervisor.tail_serial(db, settings, row.id, lines=5000)
    assert len(result["lines"]) == 1000
    assert result["lines"][-1] == "line-1500"


def test_tail_serial_rejects_path_outside_workdir(db, settings, root):
    row = _serial_row(db, root)
    row.serial_log_path = str(root / ".." / ".." / "etc" / "passwd")
    db.commit()
    with pytest.raises(hypervisor.SerialPathError):
        hypervisor.tail_serial(db, settings, row.id)


def test_tail_serial_unknown_vm_and_missing_file(db, settings, root):
    with pytest.raises(hypervisor.VMNotFoundError):
        hypervisor.tail_serial(db, settings, str(uuid.uuid4()))
    row = _serial_row(db, root)  # log file never written
    with pytest.raises(hypervisor.VMNotFoundError):
        hypervisor.tail_serial(db, settings, row.id)


# ---------------------------------------------------------------------------
# vm_action
# ---------------------------------------------------------------------------


def _action_row(db, root, cmdline=None, pidfile=True, name=None) -> HostVM:
    name = name or f"node-{uuid.uuid4().hex[:6]}"
    row = HostVM(
        name=name,
        workdir=str(root),
        pidfile_path=str(root / f"{name}.pid") if pidfile else None,
        cmdline=cmdline if cmdline is not None else _qemu_argv(name=name),
        source="discovered",
    )
    db.add(row)
    db.commit()
    return row


def test_vm_action_dry_run_touches_nothing(db, settings, root, sleep_proc, monkeypatch):
    argv = _qemu_argv()
    row = _action_row(db, root, cmdline=argv, name="node-t1")
    (root / "node-t1.pid").write_text(str(sleep_proc.pid), encoding="utf-8")
    _patch_proc(monkeypatch, sleep_proc, argv, str(root))

    with patch.object(hypervisor.subprocess, "Popen") as mock_popen:
        stop = hypervisor.vm_action(db, settings, row, "stop", dry_run=True)
        # Start dry-run on the stopped twin (start on a running VM fails).
        stopped = _action_row(db, root, cmdline=argv, pidfile=False, name="node-t2")
        start = hypervisor.vm_action(db, settings, stopped, "start", dry_run=True)
    assert stop["ok"] is True and stop["dry_run"] is True
    assert "SIGTERM" in stop["detail"]
    assert start["ok"] is True and start["dry_run"] is True
    assert start["cmd"] == argv
    mock_popen.assert_not_called()
    assert sleep_proc.poll() is None  # process untouched


def test_vm_action_stop_real_process(db, settings, root, sleep_proc, monkeypatch):
    argv = _qemu_argv()
    row = _action_row(db, root, cmdline=argv, name="node-t1")
    (root / "node-t1.pid").write_text(str(sleep_proc.pid), encoding="utf-8")
    _patch_proc(monkeypatch, sleep_proc, argv, str(root))

    result = hypervisor.vm_action(db, settings, row, "stop", dry_run=False)
    assert result["ok"] is True
    assert result["returncode"] == 0
    assert sleep_proc.wait(timeout=5) is not None


def test_vm_action_stop_already_stopped(db, settings, root):
    row = _action_row(db, root, pidfile=False, name="node-dead")
    row.cmdline = []
    db.commit()
    result = hypervisor.vm_action(db, settings, row, "stop", dry_run=False)
    assert result["ok"] is True
    assert "already stopped" in result["detail"]


def test_vm_action_start_spawns_stored_cmdline(db, settings, root):
    row = _action_row(db, root, cmdline=["sleep", "300"], pidfile=False)
    result = hypervisor.vm_action(db, settings, row, "start", dry_run=False)
    assert result["ok"] is True, result
    pid = result["pid"]
    assert hypervisor._pid_alive(pid)
    os.kill(pid, signal.SIGKILL)
    os.waitpid(pid, 0)


def test_vm_action_start_refuses_workdir_outside_roots(db, settings, root):
    row = _action_row(db, root, cmdline=["sleep", "300"], pidfile=False)
    row.workdir = "/etc"
    db.commit()
    with patch.object(hypervisor.subprocess, "Popen") as mock_popen:
        result = hypervisor.vm_action(db, settings, row, "start", dry_run=False)
    assert result["ok"] is False
    assert result["returncode"] == 2
    assert "outside" in result["error"]
    mock_popen.assert_not_called()


def test_vm_action_start_without_cmdline_fails(db, settings, root):
    row = _action_row(db, root, cmdline=[], pidfile=False)
    result = hypervisor.vm_action(db, settings, row, "start", dry_run=False)
    assert result["ok"] is False
    assert result["returncode"] == 2


def test_vm_action_rejects_unknown_action(db, settings, root):
    row = _action_row(db, root, pidfile=False)
    result = hypervisor.vm_action(db, settings, row, "explode", dry_run=False)
    assert result["ok"] is False
    assert result["returncode"] == 2
    assert "unknown host VM action" in result["error"]


def test_vm_action_restart_dry_run(db, settings, root, sleep_proc, monkeypatch):
    argv = _qemu_argv()
    row = _action_row(db, root, cmdline=argv, name="node-t1")
    (root / "node-t1.pid").write_text(str(sleep_proc.pid), encoding="utf-8")
    _patch_proc(monkeypatch, sleep_proc, argv, str(root))

    result = hypervisor.vm_action(db, settings, row, "restart", dry_run=True)
    assert result["ok"] is True
    assert result["action"] == "restart"
    assert "[dry-run]" in result["detail"]
    assert sleep_proc.poll() is None


# ---------------------------------------------------------------------------
# vm_action: sudo fallback for root-owned VMs
# ---------------------------------------------------------------------------


def test_settings_sudo_helper_default_and_yaml(tmp_path):
    assert Settings().hypervisor_sudo_helper == "/usr/local/sbin/gsc-qemu-ctl"
    cfg = {
        "data_dir": str(tmp_path / "data"),
        "hypervisor": {"sudo_helper": "/opt/gsc/sbin/qemu-ctl"},
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    s = load_settings(path)
    assert s.hypervisor_sudo_helper == "/opt/gsc/sbin/qemu-ctl"


def _sudo_ok(args, **kwargs):
    return subprocess.CompletedProcess(args, 0, stderr=b"")


def test_vm_action_stop_foreign_uid_uses_sudo(
    db, settings, root, sleep_proc, monkeypatch
):
    """A pid owned by another uid (e.g. root) is signaled via the helper."""
    argv = _qemu_argv()
    row = _action_row(db, root, cmdline=argv, name="node-t1")
    (root / "node-t1.pid").write_text(str(sleep_proc.pid), encoding="utf-8")
    _patch_proc(monkeypatch, sleep_proc, argv, str(root))
    monkeypatch.setattr(hypervisor, "_pid_needs_sudo", lambda pid: True)
    monkeypatch.setattr(hypervisor, "_live_pid", lambda vm, procs=None: sleep_proc.pid)
    monkeypatch.setattr(hypervisor, "_pid_alive", lambda pid: False)
    calls: list[list[str]] = []
    monkeypatch.setattr(
        hypervisor,
        "_run_sudo_helper",
        lambda settings, args, env_extra=None: calls.append(args) or _sudo_ok(args),
    )

    result = hypervisor.vm_action(db, settings, row, "stop", dry_run=False)
    assert result["ok"] is True, result
    assert calls == [["signal", str(sleep_proc.pid), "TERM"]]
    assert sleep_proc.poll() is None  # the real process was never signaled


def test_vm_action_stop_direct_eperm_falls_back_to_sudo(
    db, settings, root, sleep_proc, monkeypatch
):
    """EPERM from the direct SIGTERM switches to the sudo path."""
    argv = _qemu_argv()
    row = _action_row(db, root, cmdline=argv, name="node-t1")
    (root / "node-t1.pid").write_text(str(sleep_proc.pid), encoding="utf-8")
    _patch_proc(monkeypatch, sleep_proc, argv, str(root))
    monkeypatch.setattr(hypervisor, "_pid_needs_sudo", lambda pid: False)
    monkeypatch.setattr(hypervisor, "_live_pid", lambda vm, procs=None: sleep_proc.pid)
    monkeypatch.setattr(hypervisor, "_pid_alive", lambda pid: False)

    def _eperm_kill(pid, sig):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(hypervisor.os, "kill", _eperm_kill)
    calls: list[list[str]] = []
    monkeypatch.setattr(
        hypervisor,
        "_run_sudo_helper",
        lambda settings, args, env_extra=None: calls.append(args) or _sudo_ok(args),
    )

    result = hypervisor.vm_action(db, settings, row, "stop", dry_run=False)
    assert result["ok"] is True, result
    assert calls == [["signal", str(sleep_proc.pid), "TERM"]]


def test_vm_action_stop_sudo_failure_is_clean_error(
    db, settings, root, sleep_proc, monkeypatch
):
    """A failing helper (missing sudoers rule) yields a clean error dict."""
    argv = _qemu_argv()
    row = _action_row(db, root, cmdline=argv, name="node-t1")
    (root / "node-t1.pid").write_text(str(sleep_proc.pid), encoding="utf-8")
    _patch_proc(monkeypatch, sleep_proc, argv, str(root))
    monkeypatch.setattr(hypervisor, "_pid_needs_sudo", lambda pid: True)
    monkeypatch.setattr(
        hypervisor,
        "_run_sudo_helper",
        lambda settings, args, env_extra=None: subprocess.CompletedProcess(
            args, 1, stderr=b"sudo: a password is required"
        ),
    )

    result = hypervisor.vm_action(db, settings, row, "stop", dry_run=False)
    assert result["ok"] is False
    assert result["returncode"] == 1
    assert "sudo helper failed" in result["error"]
    assert "NOPASSWD" in result["error"]
    assert settings.hypervisor_sudo_helper in result["error"]
    assert sleep_proc.poll() is None


def test_vm_action_stop_sudo_helper_missing_is_clean_error(
    db, settings, root, sleep_proc, monkeypatch
):
    """A missing helper binary raises OSError — mapped to a clean error."""
    argv = _qemu_argv()
    row = _action_row(db, root, cmdline=argv, name="node-t1")
    (root / "node-t1.pid").write_text(str(sleep_proc.pid), encoding="utf-8")
    _patch_proc(monkeypatch, sleep_proc, argv, str(root))
    monkeypatch.setattr(hypervisor, "_pid_needs_sudo", lambda pid: True)

    def _missing(settings, args, env_extra=None):
        raise FileNotFoundError(
            2, "No such file or directory", "/usr/local/sbin/gsc-qemu-ctl"
        )

    monkeypatch.setattr(hypervisor, "_run_sudo_helper", _missing)

    result = hypervisor.vm_action(db, settings, row, "stop", dry_run=False)
    assert result["ok"] is False
    assert "sudo helper failed" in result["error"]
    assert sleep_proc.poll() is None


def test_start_needs_sudo_on_unwritable_files(db, settings, root):
    """The pre-check: root-owned (unwritable) pidfile/serial or workdir."""
    row = _action_row(db, root, name="node-t1")
    assert hypervisor._start_needs_sudo(row, root) is False
    pidfile = root / "node-t1.pid"
    pidfile.write_text("1234", encoding="utf-8")
    pidfile.chmod(0o000)
    try:
        assert hypervisor._start_needs_sudo(row, root) is True
    finally:
        pidfile.chmod(0o644)


def test_vm_action_start_foreign_files_use_sudo(db, settings, root, monkeypatch):
    """Root-owned pidfile: start goes through the helper with GSC_QEMU_ROOTS."""
    argv = _qemu_argv(name="node-t1", pidfile="node-t1.pid", serial="serial-t1.log")
    row = _action_row(db, root, cmdline=argv, name="node-t1")
    row.serial_log_path = str(root / "serial-t1.log")
    db.commit()
    pidfile = root / "node-t1.pid"
    pidfile.write_text("999999", encoding="utf-8")
    pidfile.chmod(0o000)
    calls: list[tuple[list[str], dict | None]] = []
    monkeypatch.setattr(
        hypervisor,
        "_run_sudo_helper",
        lambda settings, args, env_extra=None: calls.append((args, env_extra))
        or _sudo_ok(args),
    )
    try:
        with patch.object(hypervisor.subprocess, "Popen") as mock_popen:
            result = hypervisor.vm_action(db, settings, row, "start", dry_run=False)
    finally:
        pidfile.chmod(0o644)
    assert result["ok"] is True, result
    assert "sudo helper" in result["detail"]
    mock_popen.assert_not_called()
    assert calls == [(["start", str(root), *argv[1:]], {"GSC_QEMU_ROOTS": str(root)})]


def test_vm_action_start_direct_eperm_falls_back_to_sudo(
    db, settings, root, monkeypatch
):
    row = _action_row(db, root, cmdline=_qemu_argv(), pidfile=False)
    calls: list[list[str]] = []
    monkeypatch.setattr(
        hypervisor,
        "_run_sudo_helper",
        lambda settings, args, env_extra=None: calls.append(args) or _sudo_ok(args),
    )
    with patch.object(
        hypervisor.subprocess,
        "Popen",
        side_effect=PermissionError(13, "Permission denied"),
    ):
        result = hypervisor.vm_action(db, settings, row, "start", dry_run=False)
    assert result["ok"] is True, result
    assert "sudo helper" in result["detail"]
    assert len(calls) == 1 and calls[0][0] == "start"


def test_vm_action_start_sudo_failure_is_clean_error(db, settings, root, monkeypatch):
    row = _action_row(db, root, cmdline=_qemu_argv(), pidfile=False)
    monkeypatch.setattr(
        hypervisor,
        "_run_sudo_helper",
        lambda settings, args, env_extra=None: subprocess.CompletedProcess(
            args, 2, stderr=b"gsc-qemu-ctl: workdir outside allowed roots"
        ),
    )
    with patch.object(
        hypervisor.subprocess,
        "Popen",
        side_effect=PermissionError(13, "Permission denied"),
    ):
        result = hypervisor.vm_action(db, settings, row, "start", dry_run=False)
    assert result["ok"] is False
    assert "sudo helper failed" in result["error"]
    assert "outside allowed roots" in result["error"]


def test_vm_action_dry_run_describes_sudo_path_without_executing(
    db, settings, root, sleep_proc, monkeypatch
):
    """Dry-run describes the sudo path and never executes anything."""
    argv = _qemu_argv()
    row = _action_row(db, root, cmdline=argv, name="node-t1")
    (root / "node-t1.pid").write_text(str(sleep_proc.pid), encoding="utf-8")
    _patch_proc(monkeypatch, sleep_proc, argv, str(root))
    monkeypatch.setattr(hypervisor, "_pid_needs_sudo", lambda pid: True)
    monkeypatch.setattr(
        hypervisor,
        "_run_sudo_helper",
        lambda *a, **k: pytest.fail("sudo helper must not run on dry-run"),
    )

    stop = hypervisor.vm_action(db, settings, row, "stop", dry_run=True)
    assert stop["ok"] is True and stop["dry_run"] is True
    assert "SIGTERM" in stop["detail"]
    assert "sudo helper" in stop["detail"]
    assert settings.hypervisor_sudo_helper in stop["detail"]

    # Start dry-run on an unwritable pidfile row describes the sudo path too.
    row2 = _action_row(db, root, cmdline=argv, name="node-t2")
    pidfile2 = root / "node-t2.pid"
    pidfile2.write_text("999999", encoding="utf-8")
    pidfile2.chmod(0o000)
    try:
        with patch.object(hypervisor.subprocess, "Popen") as mock_popen:
            start = hypervisor.vm_action(db, settings, row2, "start", dry_run=True)
    finally:
        pidfile2.chmod(0o644)
    assert start["ok"] is True and start["dry_run"] is True
    assert start["cmd"] == argv
    assert "sudo helper" in start["detail"]
    mock_popen.assert_not_called()
    assert sleep_proc.poll() is None


# ---------------------------------------------------------------------------
# Catalog registration
# ---------------------------------------------------------------------------


def test_catalog_registers_hostvm_ops():
    list_op = get_operation("hostvm.list")
    assert list_op is not None
    assert list_op.mutating is False
    assert list_op.required_role == "viewer"
    assert "hostvm.list" not in mutating_operation_ids()

    for op_id in ("hostvm.start", "hostvm.stop", "hostvm.restart"):
        op = get_operation(op_id)
        assert op is not None, op_id
        assert op.mutating is True
        assert op.required_role == "operator"
        assert op.timeout_seconds == 120
        assert op_id in mutating_operation_ids()
        required = {p.name for p in op.params if p.required}
        assert "vm_id" in required


# ---------------------------------------------------------------------------
# Router: /api/v1/hostvms
# ---------------------------------------------------------------------------


def _status_payload() -> dict:
    return {
        "vms": [
            {
                "id": str(uuid.uuid4()),
                "name": "node-1",
                "running": True,
                "pid": 1234,
                "cpu_percent": 12.5,
                "rss_bytes": 1024,
                "uptime_seconds": 60.0,
                "workdir": "/var/lib/genestack/vms",
                "serial_log_path": "/var/lib/genestack/vms/serial-1.log",
                "vnc_host": None,
                "vnc_port": None,
                "novnc_port": None,
                "source": "discovered",
            }
        ]
    }


def test_hostvms_endpoint_returns_status(client, viewer_headers):
    with patch.object(hypervisor, "vm_status", return_value=_status_payload()["vms"]):
        resp = client.get("/api/v1/hostvms", headers=viewer_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["vms"][0]["name"] == "node-1"
    assert body["vms"][0]["running"] is True


def test_hostvms_endpoint_requires_auth(client):
    assert client.get("/api/v1/hostvms").status_code in (401, 403)


def test_serial_endpoint_returns_lines(client, viewer_headers):
    payload = {"name": "node-1", "lines": ["a", "b"], "path": "/x/serial-1.log"}
    with patch.object(hypervisor, "tail_serial", return_value=payload) as mock_tail:
        resp = client.get(
            f"/api/v1/hostvms/{uuid.uuid4()}/serial?lines=50",
            headers=viewer_headers,
        )
    assert resp.status_code == 200, resp.text
    assert resp.json() == payload
    assert mock_tail.call_args.kwargs["lines"] == 50


def test_serial_endpoint_error_mapping(client, viewer_headers):
    vm_id = str(uuid.uuid4())
    with patch.object(
        hypervisor, "tail_serial", side_effect=hypervisor.VMNotFoundError("gone")
    ):
        resp = client.get(f"/api/v1/hostvms/{vm_id}/serial", headers=viewer_headers)
    assert resp.status_code == 404

    with patch.object(
        hypervisor,
        "tail_serial",
        side_effect=hypervisor.SerialPathError("outside workdir"),
    ):
        resp = client.get(f"/api/v1/hostvms/{vm_id}/serial", headers=viewer_headers)
    assert resp.status_code == 422

    # Query validation caps the range too.
    resp = client.get(f"/api/v1/hostvms/{vm_id}/serial?lines=0", headers=viewer_headers)
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Jobs: hostvm ops through the queue
# ---------------------------------------------------------------------------


def _submit_global_job(client, headers, operation, params):
    resp = client.post(
        "/api/v1/jobs",
        headers=headers,
        json={"operation": operation, "params": params, "run_sync": True},
    )
    assert resp.status_code in (200, 201, 202), resp.text
    return resp.json()


def test_job_hostvm_list_dispatches(client, operator_headers):
    with patch.object(
        hypervisor, "vm_status", return_value=_status_payload()["vms"]
    ) as mock_status:
        job = _submit_global_job(client, operator_headers, "hostvm.list", {})
    assert job["status"] == "success", job
    assert mock_status.called
    assert "1 host VMs" in job["log_text"]


def test_job_hostvm_action_unknown_vm_id_fails(client, operator_headers):
    job = _submit_global_job(
        client, operator_headers, "hostvm.stop", {"vm_id": "not-a-uuid"}
    )
    assert job["status"] == "failed", job
    assert "unknown host VM id" in (job["error"] or "")


def test_job_hostvm_stop_dry_run(client, operator_headers):
    from app.db import SessionLocal

    vm_id = None
    with SessionLocal() as session:
        row = HostVM(
            name=f"jobstop-{uuid.uuid4().hex[:8]}",
            workdir="/var/lib/genestack/vms",
            cmdline=["sleep", "300"],
            source="manual",
        )
        session.add(row)
        session.commit()
        vm_id = row.id
    try:
        job = _submit_global_job(
            client, operator_headers, "hostvm.stop", {"vm_id": vm_id}
        )
        # Global dry_run (test config): nothing executed, job succeeds.
        assert job["status"] == "success", job
        assert "already stopped" in job["log_text"]
    finally:
        with SessionLocal() as session:
            row = session.get(HostVM, vm_id)
            if row is not None:
                session.delete(row)
                session.commit()


def test_job_hostvm_start_dry_run_logs_argv(client, operator_headers):
    from app.db import SessionLocal

    vm_id = None
    with SessionLocal() as session:
        row = HostVM(
            name=f"jobstart-{uuid.uuid4().hex[:8]}",
            workdir="/var/lib/genestack/vms",
            cmdline=["qemu-system-x86_64", "-m", "1024", "-display", "none"],
            source="discovered",
        )
        session.add(row)
        session.commit()
        vm_id = row.id
    try:
        with patch.object(hypervisor.subprocess, "Popen") as mock_popen:
            job = _submit_global_job(
                client, operator_headers, "hostvm.start", {"vm_id": vm_id}
            )
        assert job["status"] == "success", job
        assert "[dry-run] would spawn qemu-system-x86_64" in job["log_text"]
        mock_popen.assert_not_called()
    finally:
        with SessionLocal() as session:
            row = session.get(HostVM, vm_id)
            if row is not None:
                session.delete(row)
                session.commit()


def test_job_hostvm_ops_require_operator(client, viewer_headers):
    resp = client.post(
        "/api/v1/jobs",
        headers=viewer_headers,
        json={"operation": "hostvm.stop", "params": {"vm_id": "x"}, "run_sync": True},
    )
    assert resp.status_code == 403


def test_hostvm_ops_not_environment_scoped():
    # Host VMs are host-level infra: catalog ops take no environment params.
    for op_id in ("hostvm.list", "hostvm.start", "hostvm.stop", "hostvm.restart"):
        op = get_operation(op_id)
        assert all(p.name != "environment_id" for p in op.params)
