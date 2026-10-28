"""Activate staged Console updates between jobs in the managed root worker.

The API can download into its writable program directory. It cannot restart
system services. The existing privileged worker verifies and activates that
one program, then restarts the two fixed Console units outside its process.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable

from app.config import Settings
from app.paths import frozen, host_prefix

log = logging.getLogger(__name__)
_PROTOCOL = 1
_STAGE = re.compile(r"gsc-update-[A-Za-z0-9_-]{1,80}")
_VERSION = re.compile(r"\d{4}\.\d{2}\.\d{2}(?:\.\d+)?(?:-[A-Za-z0-9][A-Za-z0-9_.-]{0,24})?")
_UNIT_DIRS = ("/etc/systemd/system", "/usr/lib/systemd/system", "/lib/systemd/system")


class ActivationError(ValueError):
    pass


def service_installed(name: str) -> bool:
    return any((Path(directory) / name).is_file() for directory in _UNIT_DIRS)


def _path(settings: Settings, kind: str) -> Path:
    return Path(settings.data_dir) / f"console-update-{kind}.json"


def _binary() -> Path:
    return host_prefix() / "bin" / "genestack-console"


def _in_worker_unit(pid: int | str) -> bool:
    try:
        return any(
            line.rsplit(":", 1)[-1].rstrip("/").endswith("/genestack-console-worker.service")
            for line in Path(f"/proc/{pid}/cgroup").read_text().splitlines()
        )
    except OSError:
        return False


def _read(path: Path) -> tuple[dict[str, Any], os.stat_result]:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "r", encoding="utf-8") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 4096:
            raise ActivationError("Invalid update control file")
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ActivationError("Invalid update control document")
    return value, info


def _publish(path: Path, value: dict[str, Any], mode: int, *, exclusive: bool = False) -> None:
    fd, name = tempfile.mkstemp(prefix=".console-update-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), mode)
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        if exclusive:
            os.link(temporary, path)
        else:
            os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def publish_worker(settings: Settings) -> bool:
    if not frozen() or os.geteuid() != 0:
        return False
    if not all(shutil.which(tool) for tool in ("systemctl", "systemd-run")):
        return False
    if not _in_worker_unit("self"):
        return False
    if not all(
        service_installed(n)
        for n in ("genestack-console.service", "genestack-console-worker.service")
    ):
        return False
    try:
        _publish(
            _path(settings, "worker"),
            {
                "protocol": _PROTOCOL,
                "pid": os.getpid(),
                "binary": str(_binary().resolve()),
            },
            0o644,
        )
    except OSError:
        log.warning("Could not advertise managed update activation")
        return False
    return True


def withdraw_worker(settings: Settings) -> None:
    try:
        value, _ = _read(_path(settings, "worker"))
        if value.get("pid") == os.getpid():
            _path(settings, "worker").unlink(missing_ok=True)
    except (OSError, ValueError):
        pass


def worker_available(settings: Settings) -> bool:
    if not frozen():
        return False
    try:
        value, info = _read(_path(settings, "worker"))
        pid = value.get("pid")
        return (
            info.st_uid == 0
            and not info.st_mode & 0o022
            and value.get("protocol") == _PROTOCOL
            and type(pid) is int
            and pid > 1
            and value.get("binary") == str(_binary().resolve())
            and Path(f"/proc/{pid}").stat().st_uid == 0
            and _in_worker_unit(pid)
        )
    except (OSError, ValueError, AttributeError, TypeError):
        return False


def pending(settings: Settings) -> bool:
    try:
        return _path(settings, "request").exists() or _path(settings, "request").is_symlink()
    except (AttributeError, TypeError):
        return False


def _digest(path: Path) -> str:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_mode & 0o002
            or info.st_uid not in {0, path.parent.stat().st_uid}
            or stream.read(4) != b"\x7fELF"
        ):
            raise ActivationError("Invalid staged Console program")
        stream.seek(0)
        return hashlib.file_digest(stream, "sha256").hexdigest()


def queue(settings: Settings, staged: Path, version: str) -> None:
    if not worker_available(settings):
        raise ActivationError("The managed update worker is not ready")
    if staged.parent.resolve() != _binary().parent.resolve() or not _STAGE.fullmatch(staged.name):
        raise ActivationError("Refused a program outside this installation")
    if not _VERSION.fullmatch(version):
        raise ActivationError("Invalid target Console version")
    _publish(
        _path(settings, "request"),
        {
            "protocol": _PROTOCOL,
            "staged": staged.name,
            "sha256": _digest(staged),
            "version": version,
        },
        0o600,
        exclusive=True,
    )


def running_jobs() -> int:
    from app.db import SessionLocal
    from app.models import Job, JobStatus

    with SessionLocal() as db:
        return int(db.query(Job).filter(Job.status == JobStatus.running).count())


def _owns_control(settings: Settings) -> bool:
    try:
        value, info = _read(_path(settings, "worker"))
        return (
            info.st_uid == 0
            and value.get("pid") == os.getpid()
            and value.get("protocol") == _PROTOCOL
            and value.get("binary") == str(_binary().resolve())
            and _in_worker_unit("self")
        )
    except (OSError, ValueError):
        return False


def _services_running(expected: str) -> bool:
    """Verify the actual installed ELF parents, including Nuitka onefile hosts."""
    try:
        for name in ("genestack-console.service", "genestack-console-worker.service"):
            pid = int(
                subprocess.check_output(
                    ["systemctl", "show", name, "-p", "MainPID", "--value"], text=True, timeout=5
                ).strip()
            )
            if pid <= 1:
                return False
            # These are kernel-owned links for the two fixed managed units.
            with Path(f"/proc/{pid}/exe").open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != expected:
                    return False
        return True
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def activate_if_idle(
    settings: Settings,
    *,
    busy: Callable[[], int] | None = None,
    restart: Callable[[], tuple[bool, str, bool]] | None = None,
) -> bool:
    """True means restart was scheduled; the daemon must stop claiming jobs.

    Queued work is retained for the next worker. Running work always finishes
    first. A failed restart keeps the request so it can be retried without
    requiring the staged file to survive an already-completed atomic replace.
    """
    if not frozen() or os.geteuid() != 0 or not pending(settings) or not _owns_control(settings):
        return False
    request = _path(settings, "request")
    try:
        value, info = _read(request)
        if (
            info.st_mode & 0o022
            or value.get("protocol") != _PROTOCOL
            or set(value) != {"protocol", "staged", "sha256", "version"}
            or not isinstance(value.get("staged"), str)
            or not _STAGE.fullmatch(value["staged"])
            or not isinstance(value.get("sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", value["sha256"])
            or not isinstance(value.get("version"), str)
            or not _VERSION.fullmatch(value["version"])
        ):
            raise ActivationError("Invalid managed update request")
        staged = _binary().parent / value["staged"]
        if staged.exists() or staged.is_symlink():
            if _digest(staged) != value["sha256"]:
                raise ActivationError("Staged program changed")
        elif _digest(_binary()) != value["sha256"]:
            raise ActivationError("Staged program is missing")
    except (OSError, ValueError, TypeError):
        log.warning("Rejected an invalid managed update request")
        request.unlink(missing_ok=True)
        return False
    if _services_running(value["sha256"]):
        staged.unlink(missing_ok=True)
        request.unlink(missing_ok=True)
        log.info("Managed Console update is running in both services")
        return False
    try:
        if (busy or running_jobs)():
            return False
    except Exception:  # The worker cannot safely activate without job state.
        log.warning("Managed update is waiting for readable job state")
        return False
    try:
        if staged.exists():
            os.replace(staged, _binary())
        if restart is None:
            from app.services.updatecheck import _restart_services

            restart = lambda: _restart_services(immediate=True)
        scheduled, _message, ok = restart()
        if not scheduled or not ok:
            log.warning("Managed update activation is waiting for a service restart")
            return False
        # Keep the claim gate closed until the next worker verifies that both
        # managed services actually execute the expected bytes.
        log.info("Console update %s activated; services restarting", value["version"])
        return True
    except OSError:
        log.warning("Managed update activation will retry")
        return False
