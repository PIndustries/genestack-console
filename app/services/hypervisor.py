"""Hypervisor tier — discovery and management of host-local QEMU VMs.

The console host runs raw ``qemu-system-*`` processes that host genestack
lab/dev/AIO nodes: launched by hand with ``-daemonize -pidfile <name>.pid``,
no libvirt and no management scripts. Discovery scans each configured
hypervisor root for ``*.pid`` files and cross-references a /proc scan of
qemu-system processes, upserting one ``HostVM`` row per VM. The argv
captured at discovery becomes the stored (re)start command — ``start``
never re-derives a command from untrusted input.

Reality notes (these shape the fallbacks below):
  * The console process may run as a different (unprivileged) user than the
    qemu processes, so pidfiles and ``/proc/<pid>/cwd`` can be unreadable.
    Every /proc and pidfile read is wrapped; when cwd is unreadable the
    workdir falls back to the directory of the pidfile whose name matches
    the qemu ``-pidfile`` argv value.
  * A dying pid can vanish mid-read — no read path may raise.
  * Actions on root-owned VMs go through the NOPASSWD sudo helper
    (``hypervisor.sudo_helper``, default ``/usr/local/sbin/gsc-qemu-ctl``):
    direct first, ``sudo -n <helper> signal|start`` on EPERM/foreign uid.

VNC info comes only from the qemu ``-vnc`` flag when present; the lab VMs
use ``-display none`` + serial logs, so the vnc/novnc columns stay null.
"""

from __future__ import annotations

import glob
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models import HostVM

LogFn = Callable[[str], None]

VM_ACTIONS = ("start", "stop", "restart")
SOURCES = ("discovered", "manual")

STOP_TERM_TIMEOUT_SECONDS = 10.0
STOP_KILL_TIMEOUT_SECONDS = 2.0
SUDO_TIMEOUT_SECONDS = 30.0
CPU_SAMPLE_SECONDS = 0.1
# Read window for tail_serial (serial logs can grow unbounded).
TAIL_READ_BYTES = 1_000_000
MAX_TAIL_LINES = 1000


class VMNotFoundError(LookupError):
    """The HostVM row (or its serial log file) does not exist."""


class SerialPathError(ValueError):
    """The stored serial log path fails validation (outside the VM workdir)."""


# ---------------------------------------------------------------------------
# /proc helpers — every read is wrapped; a dying pid must never raise.
# ---------------------------------------------------------------------------


def _read_proc_argv(pid: int) -> list[str] | None:
    """NUL-separated /proc/<pid>/cmdline as an argv list, or None."""
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return None
    parts = [p.decode("utf-8", errors="replace") for p in raw.split(b"\0") if p]
    return parts or None


def _proc_cwd(pid: int) -> str | None:
    """Process cwd via /proc/<pid>/cwd; None when unreadable (e.g. other user)."""
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        return None


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by another user
    except OSError:
        return False
    # A zombie still accepts signal 0 but is dead for our purposes.
    fields = _proc_stat_fields(pid)
    if fields and fields[0] == "Z":
        return False
    return True


def _is_qemu_argv(argv: list[str] | None) -> bool:
    """QEMU detection predicate (monkeypatchable in tests)."""
    if not argv:
        return False
    return "qemu-system" in os.path.basename(argv[0])


def _scan_qemu_procs() -> list[dict[str, Any]]:
    """All live qemu-system processes: [{pid, argv, cwd}] (cwd may be None)."""
    procs: list[dict[str, Any]] = []
    for entry in glob.glob("/proc/[0-9]*"):
        try:
            pid = int(entry.rsplit("/", 1)[-1])
        except ValueError:
            continue
        argv = _read_proc_argv(pid)
        if not _is_qemu_argv(argv):
            continue
        procs.append({"pid": pid, "argv": argv, "cwd": _proc_cwd(pid)})
    return procs


def _read_pidfile(path: Path) -> int | None:
    """Pid from a pidfile; None when unreadable (permissions) or malformed."""
    try:
        return int(path.read_text(encoding="utf-8").strip().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def _proc_stat_fields(pid: int) -> list[str] | None:
    """/proc/<pid>/stat fields after the comm column (state is index 0)."""
    try:
        data = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        return data[data.rindex(")") + 2 :].split()
    except (ValueError, IndexError):
        return None


def _proc_jiffies(pid: int) -> int | None:
    """utime + stime (clock ticks) for cpu sampling."""
    fields = _proc_stat_fields(pid)
    if fields is None or len(fields) < 13:
        return None
    try:
        return int(fields[11]) + int(fields[12])
    except (ValueError, IndexError):
        return None


def _proc_starttime(pid: int) -> int | None:
    """Process start time in clock ticks since boot."""
    fields = _proc_stat_fields(pid)
    if fields is None or len(fields) < 20:
        return None
    try:
        return int(fields[19])
    except (ValueError, IndexError):
        return None


def _proc_rss_bytes(pid: int) -> int | None:
    """VmRSS from /proc/<pid>/status in bytes."""
    try:
        with Path(f"/proc/{pid}/status").open("r", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024  # kB -> bytes
    except (OSError, ValueError, IndexError):
        return None
    return None


def _boot_time() -> float | None:
    try:
        with Path("/proc/stat").open("r", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("btime "):
                    return float(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None


def _cpu_percent(pid: int, sample: float = CPU_SAMPLE_SECONDS) -> float | None:
    """CPU usage over a short sampling window.

    Percent of one logical CPU: (utime+stime delta in ticks / hz) divided by
    wall-clock elapsed, * 100. Multi-threaded qemu can exceed 100%.
    """
    hz = os.sysconf("SC_CLK_TCK")
    start = _proc_jiffies(pid)
    if start is None:
        return None
    t0 = time.monotonic()
    time.sleep(sample)
    end = _proc_jiffies(pid)
    elapsed = time.monotonic() - t0
    if end is None or elapsed <= 0:
        return None
    return round(max(0.0, (end - start) / hz / elapsed * 100.0), 1)


def _uptime_seconds(pid: int) -> float | None:
    """Wall-clock age of the process from /proc uptime vs starttime."""
    boot = _boot_time()
    start_ticks = _proc_starttime(pid)
    if boot is None or start_ticks is None:
        return None
    hz = os.sysconf("SC_CLK_TCK")
    return round(max(0.0, time.time() - (boot + start_ticks / hz)), 1)


# ---------------------------------------------------------------------------
# argv parsing
# ---------------------------------------------------------------------------


def _argv_value(argv: list[str], flag: str) -> str | None:
    """Value following ``flag`` in argv (first occurrence)."""
    try:
        idx = argv.index(flag)
    except ValueError:
        return None
    if idx + 1 < len(argv):
        return argv[idx + 1]
    return None


def _primary_drive_basename(argv: list[str]) -> str | None:
    """Basename of the first ``-drive file=...`` disk (cdroms skipped)."""
    for i, arg in enumerate(argv):
        if arg != "-drive" or i + 1 >= len(argv):
            continue
        parts = argv[i + 1].split(",")
        file_part = next((p for p in parts if p.startswith("file=")), None)
        if file_part and "media=cdrom" not in parts:
            return os.path.basename(file_part[5:])
    return None


def _serial_log_path(argv: list[str], workdir: str) -> str | None:
    """Absolute path from ``-serial file:<path>`` resolved against workdir."""
    value = _argv_value(argv, "-serial")
    if not value or not value.startswith("file:"):
        return None
    raw = value[len("file:") :]
    path = Path(raw)
    if not path.is_absolute():
        path = Path(workdir) / path
    return str(path)


def _vnc_settings(argv: list[str]) -> tuple[str | None, int | None]:
    """(vnc_host, vnc_port) from ``-vnc <host>:<display>``; port = 5900+display."""
    value = _argv_value(argv, "-vnc")
    if not value:
        return None, None
    host, _, display = value.rpartition(":")
    try:
        port = 5900 + int(display)
    except ValueError:
        return None, None
    return host or None, port


def _vm_name(argv: list[str], pid: int, pidfile: Path | None) -> str:
    """Name from -name, else pidfile stem, else drive basename, else qemu-<pid>."""
    name = _argv_value(argv, "-name")
    if name:
        # "-name guest=node-1,debug-threads=on" style: take the guest= value.
        for part in name.split(","):
            if part.startswith("guest="):
                return part[len("guest=") :]
        return name.split(",")[0]
    if pidfile is not None:
        return pidfile.stem
    drive = _primary_drive_basename(argv)
    if drive:
        return os.path.splitext(drive)[0]
    return f"qemu-{pid}"


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def _roots(settings: Settings) -> list[Path]:
    roots: list[Path] = []
    for raw in settings.hypervisor_roots or []:
        try:
            roots.append(Path(raw).expanduser().resolve())
        except OSError:
            continue
    return roots


def _identity(pidfile_path: str | None, workdir: str | None, argv: list[str]) -> tuple:
    """Stable upsert key: pidfile path, else primary drive basename + workdir."""
    if pidfile_path:
        return ("pidfile", pidfile_path)
    drive = _primary_drive_basename(argv)
    if drive and workdir:
        return ("drive", f"{workdir}/{drive}")
    return ("anon", None)


def _row_identity(row: HostVM) -> tuple:
    return _identity(row.pidfile_path, row.workdir, list(row.cmdline or []))


def _record_from_proc(
    proc: dict[str, Any],
    *,
    workdir: str,
    pidfile: Path | None,
) -> dict[str, Any]:
    argv = list(proc["argv"])
    vnc_host, vnc_port = _vnc_settings(argv)
    return {
        "name": _vm_name(argv, proc["pid"], pidfile),
        "workdir": workdir,
        "pidfile_path": str(pidfile) if pidfile is not None else None,
        "serial_log_path": _serial_log_path(argv, workdir),
        "cmdline": argv,
        "vnc_host": vnc_host,
        "vnc_port": vnc_port,
        "pid": proc["pid"],
    }


def _find_live_vms(settings: Settings) -> list[dict[str, Any]]:
    """Scan roots (pidfiles) + /proc (qemu procs) for live VMs."""
    roots = _roots(settings)
    procs = _scan_qemu_procs()
    matched_pids: set[int] = set()
    found: list[dict[str, Any]] = []

    def _take(proc: dict[str, Any]) -> dict[str, Any]:
        matched_pids.add(proc["pid"])
        return proc

    # Pass 1: pidfile-driven. The pidfile pins workdir even when the pid or
    # /proc/<pid>/cwd is unreadable (qemu running as another user).
    for root in roots:
        for name in sorted(glob.glob(str(root / "*.pid"))):
            pidfile = Path(name)
            pid = _read_pidfile(pidfile)
            proc = None
            if pid is not None:
                argv = _read_proc_argv(pid)
                if _pid_alive(pid) and _is_qemu_argv(argv):
                    proc = next((p for p in procs if p["pid"] == pid), None) or {
                        "pid": pid,
                        "argv": argv,
                        "cwd": _proc_cwd(pid),
                    }
            if proc is None:
                # Pid unreadable/stale: correlate by the -pidfile argv value.
                proc = next(
                    (
                        p
                        for p in procs
                        if p["pid"] not in matched_pids
                        and os.path.basename(_argv_value(p["argv"], "-pidfile") or "")
                        == pidfile.name
                    ),
                    None,
                )
            if proc is None:
                continue  # stale pidfile, nothing live to capture
            proc = _take(proc)
            workdir = proc.get("cwd") or str(root)
            found.append(_record_from_proc(proc, workdir=workdir, pidfile=pidfile))

    # Pass 2: qemu processes with no pidfile match — keep only those tied to
    # a configured root (cwd inside it, or primary drive file present in it).
    for proc in procs:
        if proc["pid"] in matched_pids:
            continue
        argv = proc["argv"]
        cwd = proc.get("cwd")
        workdir: str | None = None
        if cwd:
            for root in roots:
                try:
                    if Path(cwd).resolve().is_relative_to(root):
                        workdir = str(Path(cwd).resolve())
                        break
                except OSError:
                    continue
        if workdir is None:
            drive = _primary_drive_basename(argv)
            if drive:
                for root in roots:
                    if (root / drive).is_file():
                        workdir = str(root)
                        break
        if workdir is None:
            continue  # not related to any configured root — not ours
        found.append(_record_from_proc(_take(proc), workdir=workdir, pidfile=None))

    return found


def discover_vms(db: Session, settings: Settings | None = None) -> int:
    """Upsert HostVM rows for live QEMU VMs under the configured roots.

    Matching is by pidfile path, else primary drive basename + workdir. Rows
    for VMs that disappear are never deleted — they report as stopped.
    Returns the number of live VMs discovered.
    """
    settings = settings or get_settings()
    if not settings.hypervisor_enabled:
        return 0
    found = _find_live_vms(settings)
    rows = list(db.scalars(select(HostVM)).all())
    by_identity = {_row_identity(row): row for row in rows}
    taken_names = {row.name for row in rows}

    for record in found:
        identity = _identity(
            record["pidfile_path"], record["workdir"], record["cmdline"]
        )
        row = by_identity.get(identity)
        if row is None:
            name = record["name"]
            if name in taken_names:
                name = f"{name}-{record['pid']}"
            row = HostVM(name=name, workdir=record["workdir"], source="discovered")
            db.add(row)
            taken_names.add(name)
        row.workdir = record["workdir"]
        row.pidfile_path = record["pidfile_path"]
        row.serial_log_path = record["serial_log_path"]
        row.cmdline = record["cmdline"]
        row.vnc_host = record["vnc_host"]
        row.vnc_port = record["vnc_port"]
        db.add(row)
    db.commit()
    return len(found)


# ---------------------------------------------------------------------------
# Live status
# ---------------------------------------------------------------------------


def _live_pid(row: HostVM, procs: list[dict[str, Any]] | None = None) -> int | None:
    """Best-effort live pid for a row: pidfile first, then a /proc match."""
    if row.pidfile_path:
        pid = _read_pidfile(Path(row.pidfile_path))
        if pid is not None and _pid_alive(pid):
            argv = _read_proc_argv(pid)
            if argv is None or _is_qemu_argv(argv):
                return pid
    # Fall back to a /proc scan matched on this row's identity.
    identity = _row_identity(row)
    if identity[1] is None:
        return None
    for proc in procs if procs is not None else _scan_qemu_procs():
        workdir = proc.get("cwd") or row.workdir
        if _identity(None, workdir, proc["argv"]) == identity or (
            row.pidfile_path
            and os.path.basename(_argv_value(proc["argv"], "-pidfile") or "")
            == os.path.basename(row.pidfile_path)
        ):
            return proc["pid"]
    return None


def vm_status(db: Session, settings: Settings | None = None) -> list[dict[str, Any]]:
    """Discover, then merge live state onto every HostVM row. Never raises."""
    settings = settings or get_settings()
    try:
        discover_vms(db, settings)
    except Exception:  # noqa: BLE001 — status read path must not fail
        db.rollback()
    result: list[dict[str, Any]] = []
    rows = list(db.scalars(select(HostVM).order_by(HostVM.name)).all())
    for row in rows:
        try:
            pid = _live_pid(row)
        except Exception:  # noqa: BLE001
            pid = None
        running = pid is not None
        result.append(
            {
                "id": row.id,
                "name": row.name,
                "running": running,
                "pid": pid,
                "cpu_percent": _cpu_percent(pid) if running else None,
                "rss_bytes": _proc_rss_bytes(pid) if running else None,
                "uptime_seconds": _uptime_seconds(pid) if running else None,
                "workdir": row.workdir,
                "serial_log_path": row.serial_log_path,
                "vnc_host": row.vnc_host,
                "vnc_port": row.vnc_port,
                "novnc_port": row.novnc_port,
                "source": row.source,
            }
        )
    return result


# ---------------------------------------------------------------------------
# Serial log
# ---------------------------------------------------------------------------


def tail_serial(
    db: Session,
    settings: Settings | None,
    vm_id: str,
    lines: int = 200,
) -> dict[str, Any]:
    """Last ``lines`` of a VM's serial log: {name, lines: [...], path}.

    The resolved log path must stay inside the row's workdir (traversal
    guard); line count is capped at MAX_TAIL_LINES.
    """
    vm = db.get(HostVM, str(vm_id or ""))
    if vm is None:
        raise VMNotFoundError(f"host VM not found: {vm_id}")
    if not vm.serial_log_path:
        raise VMNotFoundError(f"host VM {vm.name} has no serial log path")
    if not vm.workdir:
        raise SerialPathError(f"host VM {vm.name} has no workdir")

    workdir = Path(vm.workdir).resolve()
    path = Path(vm.serial_log_path).resolve()
    if not path.is_relative_to(workdir):
        raise SerialPathError(
            f"serial log path {path} is outside the VM workdir {workdir}"
        )
    if not path.is_file():
        raise VMNotFoundError(f"serial log not found: {path}")

    count = max(1, min(int(lines), MAX_TAIL_LINES))
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            fh.seek(max(0, size - TAIL_READ_BYTES))
            data = fh.read()
    except OSError as exc:
        raise VMNotFoundError(f"serial log not readable: {path} ({exc})") from exc
    text = data.decode("utf-8", errors="replace")
    return {"name": vm.name, "lines": text.splitlines()[-count:], "path": str(path)}


# ---------------------------------------------------------------------------
# Actions (start/stop/restart) — always via the job queue; never raise.
# ---------------------------------------------------------------------------


def _pid_owner_uid(pid: int) -> int | None:
    """Owning uid of a live pid via /proc stat ownership; None when unreadable."""
    try:
        return os.stat(f"/proc/{pid}").st_uid
    except OSError:
        return None


def _pid_needs_sudo(pid: int) -> bool:
    """True when the pid is owned by a different uid than ours (e.g. root)."""
    uid = _pid_owner_uid(pid)
    return uid is not None and uid != os.geteuid()


def _run_sudo_helper(
    settings: Settings,
    args: list[str],
    env_extra: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """Invoke the root helper via ``sudo -n`` (never prompts for a password).

    sudo strips the process environment for the child, so extra variables
    (e.g. GSC_QEMU_ROOTS) are passed as command-line ``VAR=value`` args,
    which sudo forwards (subject to its standard env_delete deny-list).
    """
    cmd = ["sudo", "-n"]
    if env_extra:
        cmd += [f"{key}={value}" for key, value in env_extra.items()]
    cmd += [settings.hypervisor_sudo_helper, *args]
    return subprocess.run(  # noqa: S603 — fixed sudo+helper path, no shell
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        timeout=SUDO_TIMEOUT_SECONDS,
        check=False,
    )


def _sudo_error(
    action: str,
    vm: HostVM,
    settings: Settings,
    exc: BaseException | None,
    stderr: bytes | None,
) -> dict[str, Any]:
    """Clean error for a missing/failing sudo rule or helper — never a hang."""
    if exc is not None:
        detail = str(exc)
    else:
        detail = (stderr or b"").decode(
            "utf-8", errors="replace"
        ).strip() or "no output"
    return {
        "ok": False,
        "action": action,
        "error": (
            f"{vm.name}: sudo helper failed: {detail} — check the NOPASSWD "
            f"sudoers rule for {settings.hypervisor_sudo_helper} "
            f"(e.g. /etc/sudoers.d/gsc-qemu)"
        ),
        "returncode": 1,
    }


def _sudo_signal(
    vm: HostVM, settings: Settings, action: str, pid: int, sig: str
) -> dict[str, Any] | None:
    """Signal a root-owned qemu pid via the helper; None on success, error dict otherwise."""
    try:
        completed = _run_sudo_helper(settings, ["signal", str(pid), sig])
    except (OSError, subprocess.TimeoutExpired) as exc:
        return _sudo_error(action, vm, settings, exc, None)
    if completed.returncode != 0:
        return _sudo_error(action, vm, settings, None, completed.stderr)
    return None


def _start_needs_sudo(vm: HostVM, workdir: Path) -> bool:
    """True when files qemu must create/truncate are not writable by us.

    Lab VMs' pidfiles/serial logs are root-owned; a direct spawn would die
    deep inside qemu. Detect that up front and use the sudo helper instead.
    """
    if not os.access(workdir, os.W_OK):
        return True
    for raw in (vm.pidfile_path, vm.serial_log_path):
        if raw and Path(raw).exists() and not os.access(raw, os.W_OK):
            return True
    return False


def _stop(
    vm: HostVM, settings: Settings, pid: int | None, dry_run: bool
) -> dict[str, Any]:
    if pid is None:
        return {
            "ok": True,
            "action": "stop",
            "detail": f"{vm.name}: already stopped",
            "returncode": 0,
        }
    use_sudo = _pid_needs_sudo(pid)
    if dry_run:
        via = (
            f" via sudo helper {settings.hypervisor_sudo_helper}"
            if use_sudo
            else " directly"
        )
        return {
            "ok": True,
            "action": "stop",
            "dry_run": True,
            "detail": (
                f"[dry-run] would SIGTERM pid {pid}{via} ({vm.name}), "
                f"SIGKILL after {STOP_TERM_TIMEOUT_SECONDS:.0f}s if still alive"
            ),
            "returncode": 0,
        }
    if not use_sudo:
        try:
            os.kill(pid, signal.SIGTERM)
        except PermissionError:
            use_sudo = True  # owned by another user after all
        except OSError as exc:
            return {
                "ok": False,
                "action": "stop",
                "error": f"{vm.name}: SIGTERM pid {pid} failed: {exc}",
                "returncode": 1,
            }
    if use_sudo:
        failure = _sudo_signal(vm, settings, "stop", pid, "TERM")
        if failure is not None:
            return failure
    deadline = time.monotonic() + STOP_TERM_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            break
        time.sleep(0.1)
    if _pid_alive(pid):
        if not use_sudo:
            try:
                os.kill(pid, signal.SIGKILL)
            except PermissionError:
                use_sudo = True
            except OSError as exc:
                return {
                    "ok": False,
                    "action": "stop",
                    "error": f"{vm.name}: SIGKILL pid {pid} failed: {exc}",
                    "returncode": 1,
                }
        if use_sudo:
            failure = _sudo_signal(vm, settings, "stop", pid, "KILL")
            if failure is not None:
                return failure
        deadline = time.monotonic() + STOP_KILL_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if not _pid_alive(pid):
                break
            time.sleep(0.1)
    if _pid_alive(pid):
        return {
            "ok": False,
            "action": "stop",
            "error": f"{vm.name}: pid {pid} still alive after SIGKILL",
            "returncode": 1,
        }
    return {
        "ok": True,
        "action": "stop",
        "detail": f"{vm.name}: stopped (pid {pid})",
        "returncode": 0,
    }


def _workdir_guard(vm: HostVM, settings: Settings) -> Path | dict[str, Any]:
    """Resolved workdir, or an error dict when outside the hypervisor roots."""
    if not vm.workdir:
        return {
            "ok": False,
            "action": "start",
            "error": f"{vm.name}: no workdir",
            "returncode": 2,
        }
    workdir = Path(vm.workdir).resolve()
    for root in _roots(settings):
        if workdir.is_relative_to(root):
            return workdir
    return {
        "ok": False,
        "action": "start",
        "error": (
            f"{vm.name}: workdir {workdir} is outside the configured "
            "hypervisor roots — refusing to start"
        ),
        "returncode": 2,
    }


def _start(
    vm: HostVM, settings: Settings, pid: int | None, dry_run: bool
) -> dict[str, Any]:
    if pid is not None:
        return {
            "ok": False,
            "action": "start",
            "error": f"{vm.name}: already running (pid {pid})",
            "returncode": 1,
        }
    argv = list(vm.cmdline or [])
    if not argv or not all(isinstance(a, str) and a for a in argv):
        return {
            "ok": False,
            "action": "start",
            "error": f"{vm.name}: no stored cmdline — cannot start",
            "returncode": 2,
        }
    guard = _workdir_guard(vm, settings)
    if isinstance(guard, dict):
        return guard
    workdir = guard
    use_sudo = _start_needs_sudo(vm, workdir)
    if dry_run:
        via = f" via sudo helper {settings.hypervisor_sudo_helper}" if use_sudo else ""
        return {
            "ok": True,
            "action": "start",
            "dry_run": True,
            "cmd": argv,
            "detail": (
                f"[dry-run] would spawn {argv[0]} (cwd={workdir}){via} for {vm.name}"
            ),
            "returncode": 0,
        }
    if not use_sudo:
        try:
            proc = subprocess.Popen(  # noqa: S603 — argv from discovery, no shell
                argv,
                cwd=str(workdir),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except PermissionError:
            use_sudo = True  # root-owned pidfile/serial/qcow2 paths
        except OSError as exc:
            return {
                "ok": False,
                "action": "start",
                "error": f"{vm.name}: spawn failed: {exc}",
                "returncode": 1,
            }
        else:
            return {
                "ok": True,
                "action": "start",
                "pid": proc.pid,
                "detail": f"{vm.name}: started (pid {proc.pid})",
                "returncode": 0,
            }
    # Root-owned VM: the helper re-execs qemu as root inside the workdir.
    roots_env = ":".join(str(root) for root in _roots(settings))
    try:
        completed = _run_sudo_helper(
            settings,
            ["start", str(workdir), *argv[1:]],
            env_extra={"GSC_QEMU_ROOTS": roots_env},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return _sudo_error("start", vm, settings, exc, None)
    if completed.returncode != 0:
        return _sudo_error("start", vm, settings, None, completed.stderr)
    return {
        "ok": True,
        "action": "start",
        "detail": (
            f"{vm.name}: started via sudo helper "
            f"{settings.hypervisor_sudo_helper} (cwd={workdir})"
        ),
        "returncode": 0,
    }


def vm_action(
    db: Session,
    settings: Settings | None,
    vm: HostVM,
    action: str,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Run start|stop|restart for one HostVM. Returns a result dict; never raises.

    ``start`` replays the stored discovery cmdline as-is (never user input)
    with cwd=workdir, detached (start_new_session, all fds to DEVNULL).
    ``stop`` SIGTERMs the live pid, escalating to SIGKILL after 10s.

    Root-owned VMs (pid/files owned by another uid) are handled through the
    NOPASSWD sudo helper ``settings.hypervisor_sudo_helper``: the direct
    operation is attempted first, and EPERM/foreign-uid falls back to the
    helper. Sudo is always invoked with ``-n`` — never a password prompt.
    """
    settings = settings or get_settings()
    if action not in VM_ACTIONS:
        return {
            "ok": False,
            "action": action,
            "error": f"unknown host VM action {action!r} (valid: {', '.join(VM_ACTIONS)})",
            "returncode": 2,
        }
    try:
        pid = _live_pid(vm)
    except Exception:  # noqa: BLE001
        pid = None

    if action == "stop":
        result = _stop(vm, settings, pid, dry_run)
    elif action == "start":
        result = _start(vm, settings, pid, dry_run)
    else:  # restart
        stop = _stop(vm, settings, pid, dry_run)
        if not stop.get("ok"):
            stop["action"] = "restart"
            return stop
        result = _start(vm, settings, None, dry_run)
        result["action"] = "restart"
        result["detail"] = f"{stop.get('detail')} ; {result.get('detail')}"

    # Normalize for the job runner's message/error handling.
    if result.get("ok"):
        result.setdefault("message", result.get("detail"))
    else:
        result.setdefault("message", result.get("error"))
    return result
