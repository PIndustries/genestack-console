"""Start the lab guests and record them on one environment.

What: download the Ubuntu cloud image, create thin disks, start qemu, and
write the environment the deploy jobs already understand.
Where: app/services/lab_launch.py. hostvm.lab is the only caller.
Why: the guests exist because the console job ran. A later run leaves a
guest that is already up alone.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import Environment
from app.services import envconfig as envconfig_service
from app.services import ssh_keys
from app.services.hypervisor import _roots
from app.services.lab_cluster import (
    IMAGE_URL,
    QCOW_MAGIC,
    LabNode,
    cidata_iso,
    confirmed,
    guest_paths,
    image_path,
    linux_argv,
    meta_data,
    nodes_for,
    parse_count,
    parse_name,
    qemu_img_argv,
    refusal,
    user_data,
)

LogFn = Callable[[str], None]
CancelFn = Callable[[], None]

_IMAGE_MIN_BYTES = 50_000_000
_PID_WAIT_SECONDS = 15.0


def hypervisor_root(settings: Settings) -> Path | None:
    roots = _roots(settings)
    return roots[0] if roots else None


def kvm_present() -> bool:
    return Path("/dev/kvm").exists()


def qemu_bin() -> str | None:
    return shutil.which("qemu-system-x86_64")


def qemu_img_bin() -> str | None:
    return shutil.which("qemu-img")


def ensure_qemu(log: LogFn) -> None:
    """Install the same qemu packages the installer uses for a local guest.

    A host that already has the binaries is left alone. A host without
    apt-get is left for the refusal message.
    """
    if qemu_bin() and qemu_img_bin():
        return
    apt = shutil.which("apt-get")
    if not apt:
        return
    log("[lab] installing qemu-system-x86 and qemu-utils")
    env = dict(os.environ)
    env["DEBIAN_FRONTEND"] = "noninteractive"
    update = subprocess.run(
        [apt, "update"],
        check=False,
        capture_output=True,
        timeout=300,
        env=env,
    )
    if update.returncode != 0:
        log("[lab] apt-get update did not finish cleanly. Trying the install anyway.")
    completed = subprocess.run(
        [
            apt,
            "install",
            "-y",
            "--no-install-recommends",
            "qemu-system-x86",
            "qemu-utils",
        ],
        check=False,
        capture_output=True,
        timeout=900,
        env=env,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or b"").decode(
            "utf-8", errors="replace"
        ).strip()
        tail = detail[-400:] if detail else "apt-get install failed"
        raise RuntimeError(f"could not install qemu: {tail}")


def memory_available_mb() -> int:
    text = Path("/proc/meminfo").read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) // 1024
    raise OSError("MemAvailable is missing from /proc/meminfo")


def disk_free_bytes(root: Path) -> int:
    usage = os.statvfs(root)
    return usage.f_bavail * usage.f_frsize


def pid_alive(path: Path) -> bool:
    try:
        pid = int(path.read_text(encoding="utf-8").split()[0])
    except (OSError, ValueError, IndexError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def port_busy(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return True
    return False


def ensure_image(dest: Path, url: str, log: LogFn) -> None:
    """Cache the qcow2 cloud image. A short or non-qcow2 file is fetched again."""
    if _qcow_cached(dest):
        log(f"[lab] cloud image cached: {dest}")
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_name(dest.name + ".partial")
    log(f"[lab] downloading {url}")
    request = urllib.request.Request(url, headers={"User-Agent": "genestack-console"})
    try:
        with urllib.request.urlopen(request, timeout=120) as response, partial.open("wb") as out:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
    except (OSError, urllib.error.URLError) as exc:
        partial.unlink(missing_ok=True)
        raise RuntimeError(f"cloud image download failed: {exc}") from exc
    if not _qcow_file(partial):
        partial.unlink(missing_ok=True)
        raise RuntimeError("downloaded cloud image is not a qcow2 file")
    partial.replace(dest)
    log(f"[lab] cloud image saved: {dest}")


def _qcow_cached(path: Path) -> bool:
    try:
        if path.stat().st_size < _IMAGE_MIN_BYTES:
            return False
    except OSError:
        return False
    return _qcow_file(path)


def _qcow_file(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return handle.read(4) == QCOW_MAGIC
    except OSError:
        return False


def create_overlay(base: Path, disk: Path) -> None:
    argv = qemu_img_argv(base, disk)
    completed = subprocess.run(
        argv,
        check=False,
        capture_output=True,
        timeout=120,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or b"").decode("utf-8", errors="replace").strip()
        raise RuntimeError(detail or "qemu-img create failed")


def write_seed(user_text: str, meta_text: str, dest: Path) -> str:
    """Write a CIDATA seed. Returns the tool that wrote it."""
    stage = Path(tempfile.mkdtemp(prefix="gsc-cidata-"))
    try:
        user_file = stage / "user-data"
        meta_file = stage / "meta-data"
        user_file.write_text(user_text, encoding="utf-8")
        meta_file.write_text(meta_text, encoding="utf-8")
        commands: list[list[str]] = []
        if shutil.which("cloud-localds"):
            commands.append(
                ["cloud-localds", str(dest), str(user_file), str(meta_file)]
            )
        for tool in ("genisoimage", "mkisofs"):
            if shutil.which(tool):
                commands.append(
                    [
                        tool,
                        "-output",
                        str(dest),
                        "-volid",
                        "cidata",
                        "-joliet",
                        "-rock",
                        str(stage),
                    ]
                )
        if shutil.which("xorriso"):
            commands.append(
                [
                    "xorriso",
                    "-as",
                    "mkisofs",
                    "-output",
                    str(dest),
                    "-volid",
                    "cidata",
                    "-joliet",
                    "-rock",
                    str(stage),
                ]
            )
        for command in commands:
            try:
                completed = subprocess.run(
                    command,
                    check=False,
                    capture_output=True,
                    timeout=60,
                )
            except (OSError, subprocess.TimeoutExpired):
                continue
            if completed.returncode == 0 and dest.is_file():
                return command[0]
        dest.write_bytes(cidata_iso(user_text.encode(), meta_text.encode()))
        return "python"
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def start_qemu(argv: list[str], cwd: Path) -> None:
    pidfile = _pidfile_from_argv(argv)
    # argv is built in lab_cluster. There is no shell.
    subprocess.Popen(
        argv,
        cwd=str(cwd),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    if pidfile is None:
        raise RuntimeError("qemu command has no pidfile")
    deadline = time.monotonic() + _PID_WAIT_SECONDS
    while time.monotonic() < deadline:
        if pid_alive(pidfile):
            return
        time.sleep(0.2)
    raise RuntimeError(f"qemu did not stay up ({pidfile})")


def _pidfile_from_argv(argv: list[str]) -> Path | None:
    try:
        index = argv.index("-pidfile")
    except ValueError:
        return None
    if index + 1 >= len(argv):
        return None
    return Path(argv[index + 1])


def _plan_lines(nodes: list[LabNode]) -> list[str]:
    lines = [
        "[lab] 4 vCPU, 16 GiB, thin 80 GB disk on each guest.",
        "[lab] Not a production workload. The console host stays outside the cluster.",
        "[lab] OpenStack stays rehearsed until you Apply.",
    ]
    for node in nodes:
        roles = ", ".join(node.roles)
        lines.append(
            f"[lab] {node.name} ssh ubuntu@127.0.0.1:{node.ssh_port} roles {roles}"
        )
    return lines


def create_lab(
    db: Session,
    settings: Settings,
    job,
    log: LogFn,
    params: dict[str, Any],
    check_cancel: CancelFn | None = None,
) -> dict[str, Any]:
    """Plan, or start, the lab. Confirm is required before any guest starts."""
    try:
        count = parse_count(params.get("count"))
        name = parse_name(params.get("name"))
    except ValueError as exc:
        log(f"[lab] {exc}")
        return {"ok": False, "error": str(exc), "returncode": 2, "dry_run": False}
    nodes = nodes_for(count)
    for line in _plan_lines(nodes):
        log(line)
    if not confirmed(params.get("confirm")):
        log("[lab] Plan only. Pass confirm true to start the guests.")
        return {
            "ok": True,
            "dry_run": True,
            "count": count,
            "message": "Plan only. The guests were not started.",
        }
    if check_cancel is not None:
        check_cancel()
    root = hypervisor_root(settings)
    if root is None:
        message = "No hypervisor root is configured. The lab was not started."
        log(f"[lab] {message}")
        return {"ok": False, "error": message, "returncode": 2, "dry_run": False}
    try:
        root.mkdir(parents=True, exist_ok=True)
        if not qemu_bin() or not qemu_img_bin():
            ensure_qemu(log)
        reason = refusal(
            count,
            mem_available_mb=memory_available_mb(),
            disk_free_bytes=disk_free_bytes(root),
            kvm=kvm_present(),
            qemu=bool(qemu_bin()),
            qemu_img=bool(qemu_img_bin()),
        )
    except OSError as exc:
        message = f"This host could not be measured: {exc}"
        log(f"[lab] {message}")
        return {"ok": False, "error": message, "returncode": 1, "dry_run": False}
    except RuntimeError as exc:
        log(f"[lab] {exc}")
        return {"ok": False, "error": str(exc), "returncode": 1, "dry_run": False}
    if reason:
        log(f"[lab] {reason}")
        return {"ok": False, "error": reason, "returncode": 2, "dry_run": False}
    if not os.access(root, os.W_OK):
        message = f"{root} is not writable by this console. The lab was not started."
        log(f"[lab] {message}")
        return {"ok": False, "error": message, "returncode": 1, "dry_run": False}
    try:
        ensure_image(image_path(root), IMAGE_URL, log)
        env = _environment(db, settings, name, count)
        pubkey = (env.ssh_public_key or "").strip()
        if not pubkey.startswith("ssh-"):
            raise RuntimeError("The lab environment has no SSH public key.")
        guest_rows = _ensure_guests(root, nodes, pubkey, log, check_cancel)
        _write_servers(db, env, job, guest_rows)
    except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
        log(f"[lab] {exc}")
        return {"ok": False, "error": str(exc), "returncode": 1, "dry_run": False}
    failed = [row for row in guest_rows if row["state"] == "failed"]
    if failed:
        detail = "; ".join(f"{row['name']}: {row['error']}" for row in failed)
        log(f"[lab] {detail}")
        return {
            "ok": False,
            "error": detail,
            "returncode": 1,
            "dry_run": False,
            "count": count,
            "environment_id": env.id,
            "environment_name": env.name,
        }
    message = (
        f"Lab {env.name} has {count} guests. "
        "Not a production workload. OpenStack stays rehearsed until you Apply."
    )
    log(f"[lab] {message}")
    return {
        "ok": True,
        "dry_run": False,
        "count": count,
        "environment_id": env.id,
        "environment_name": env.name,
        "message": message,
    }


def _environment(
    db: Session,
    settings: Settings,
    name: str,
    count: int,
) -> Environment:
    env = db.scalar(select(Environment).where(Environment.name == name))
    if env is not None:
        meta = dict(env.metadata_json or {})
        if not meta.get("lab"):
            raise RuntimeError(
                f"An environment named {name} already exists and is not this lab."
            )
        meta["lab"] = True
        meta["nodes"] = count
        env.metadata_json = meta
    else:
        config_dir = str(
            Path(settings.data_dir) / "environments" / Path(name).name / "etc-genestack"
        )
        Path(config_dir).mkdir(parents=True, exist_ok=True)
        env = Environment(
            name=name,
            region="local",
            tier="dev",
            description=(
                "Lab on this console host. Not a production workload. "
                "The console stays outside the cluster. "
                "OpenStack stays rehearsed until you Apply."
            ),
            genestack_path=str(settings.genestack_root),
            genestack_config_dir=config_dir,
            dry_run=True,
            metadata_json={"lab": True, "nodes": count},
        )
        db.add(env)
        db.flush()
    if not (env.ssh_public_key or "").strip():
        ssh_keys.store_key_pair(env, comment=f"genestack/{env.name}")
        db.flush()
    return env


def _ensure_guests(
    root: Path,
    nodes: list[LabNode],
    pubkey: str,
    log: LogFn,
    check_cancel: CancelFn | None,
) -> list[dict[str, Any]]:
    base = image_path(root)
    rows: list[dict[str, Any]] = []
    for node in nodes:
        if check_cancel is not None:
            check_cancel()
        paths = guest_paths(root, node)
        paths["guest"].mkdir(parents=True, exist_ok=True)
        row: dict[str, Any] = {
            "name": node.name,
            "ssh_port": node.ssh_port,
            "roles": list(node.roles),
            "state": "started",
            "error": "",
        }
        if pid_alive(paths["pidfile"]):
            row["state"] = "already-running"
            log(f"[lab] {node.name} already running")
            rows.append(row)
            continue
        if port_busy(node.ssh_port):
            row["state"] = "failed"
            row["error"] = f"127.0.0.1:{node.ssh_port} is in use"
            log(f"[lab] {node.name}: {row['error']}")
            rows.append(row)
            continue
        try:
            if not paths["disk"].is_file():
                create_overlay(base, paths["disk"])
                log(f"[lab] thin disk {paths['disk']}")
            if not paths["seed"].is_file():
                paths["user_data"].write_text(user_data(node, pubkey), encoding="utf-8")
                paths["meta_data"].write_text(meta_data(node), encoding="utf-8")
                tool = write_seed(
                    paths["user_data"].read_text(encoding="utf-8"),
                    paths["meta_data"].read_text(encoding="utf-8"),
                    paths["seed"],
                )
                log(f"[lab] cloud-init seed for {node.name} ({tool})")
            paths["pidfile"].unlink(missing_ok=True)
            argv = linux_argv(
                node,
                disk=paths["disk"],
                seed=paths["seed"],
                serial=paths["serial"],
                pidfile=paths["pidfile"],
            )
            start_qemu(argv, paths["guest"])
            log(
                f"[lab] {node.name} started "
                f"(ssh ubuntu@127.0.0.1:{node.ssh_port})"
            )
        except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
            row["state"] = "failed"
            row["error"] = str(exc)
            log(f"[lab] {node.name}: {exc}")
        rows.append(row)
    return rows


def _write_servers(db: Session, env: Environment, job, rows: list[dict[str, Any]]) -> None:
    current = envconfig_service.get_current(db, env)
    doc: dict[str, Any] = dict(current[0]) if current else {}
    doc["provider"] = "kubespray"
    servers = dict(doc.get("servers") or {})
    for row in rows:
        if row["state"] == "failed":
            continue
        entry = dict(servers.get(row["name"]) or {})
        entry.update(
            {
                "ip": "127.0.0.1",
                "ssh_user": "ubuntu",
                "ssh_port": row["ssh_port"],
                "source": "static",
                "roles": list(row["roles"]),
            }
        )
        servers[row["name"]] = entry
    doc["servers"] = servers
    actor = getattr(job, "created_by", None) or "system"
    envconfig_service.put_version(db, env, envconfig_service._dump(doc), actor)
