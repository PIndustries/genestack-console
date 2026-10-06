"""Read-only check for a saved inventory address.

Machines asks four questions: is it up, is it running, can we connect, and
can we authenticate. A closed port or a refused login is a status, not a job.
Nothing here installs, reboots, or writes to the host.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from app.models import Environment
from app.services.crypto import decrypt_secret
from app.services.env_field_guards import refuse_dangerous_ssh_host, refuse_dangerous_ssh_user
from app.services.ssh_keys import get_decrypted_private_key

_MAX_HOSTS = 40
_TCP_TIMEOUT = 2.0
_SSH_TIMEOUT = 6


def _blank(hostname: str, ip: str | None, detail: str) -> dict[str, Any]:
    return {
        "hostname": hostname,
        "ip": ip,
        "up": False,
        "running": False,
        "connect": False,
        "authenticated": False,
        "detail": detail,
    }


def _tcp_closed(host: str, port: int, timeout: float) -> str:
    """Empty string when the port accepts a connection. Otherwise a short reason."""
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
    except TimeoutError:
        return "timed out"
    except ConnectionRefusedError:
        return "connection refused"
    except OSError as exc:
        text = str(exc).lower()
        if "timed out" in text or "timeout" in text:
            return "timed out"
        if "refused" in text:
            return "connection refused"
        return "no route"
    else:
        try:
            sock.close()
        except OSError:
            pass
        return ""


def _ssh_detail(returncode: int, err: str) -> tuple[bool, str]:
    if returncode == 0:
        return True, ""
    low = (err or "").lower()
    if "permission denied" in low or "authentication failed" in low:
        return False, "authentication failed"
    if "connection refused" in low:
        return False, "connection refused"
    if "timed out" in low or "timeout" in low:
        return False, "timed out"
    if "no route" in low or "unreachable" in low:
        return False, "no route"
    if returncode == 255:
        return False, "authentication failed"
    return False, (err or "ssh failed")[:120]


def _run_ssh(argv: list[str], env: dict[str, str] | None = None) -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=_SSH_TIMEOUT,
            env=env,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, "timed out"
    except OSError:
        return False, "ssh failed"
    err = (proc.stderr or proc.stdout or "").strip().replace("\n", " ")
    return _ssh_detail(proc.returncode, err)


def _ssh_argv(ssh: str, user: str, host: str, extra: list[str]) -> list[str]:
    return [
        ssh,
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=3",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "UserKnownHostsFile=/dev/null",
        "-o",
        "IdentitiesOnly=yes",
        *extra,
        f"{user}@{host}",
        "true",
    ]


def _ssh_with_key(ssh: str, user: str, host: str, key_text: str) -> tuple[bool, str]:
    fd, name = tempfile.mkstemp(prefix="gsc-reach-")
    try:
        os.write(fd, key_text.encode())
        os.fchmod(fd, 0o600)
        os.close(fd)
        fd = -1
        return _run_ssh(_ssh_argv(ssh, user, host, ["-i", name]))
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(name)
        except OSError:
            pass


def _ssh_with_password(
    sshpass: str, ssh: str, user: str, host: str, password: str
) -> tuple[bool, str]:
    # BatchMode would refuse the password prompt. sshpass supplies it from
    # the environment so the password is not a process argument.
    argv = [
        sshpass,
        "-e",
        ssh,
        "-o",
        "PreferredAuthentications=password",
        "-o",
        "PubkeyAuthentication=no",
        "-o",
        "NumberOfPasswordPrompts=1",
        "-o",
        "ConnectTimeout=3",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "UserKnownHostsFile=/dev/null",
        f"{user}@{host}",
        "true",
    ]
    env = os.environ.copy()
    env["SSHPASS"] = password
    return _run_ssh(argv, env)


def probe_address(
    ip: str,
    user: str,
    *,
    key_text: str | None,
    password: str | None,
) -> dict[str, Any]:
    """One address. The result never includes the key or the password."""
    try:
        host = refuse_dangerous_ssh_host(ip)
        login = refuse_dangerous_ssh_user(user) or "root"
    except ValueError:
        return _blank("", ip or None, "address refused")
    if not host:
        return _blank("", None, "no address")
    closed = _tcp_closed(host, 22, _TCP_TIMEOUT)
    if closed:
        row = _blank("", host, closed)
        return row
    row = _blank("", host, "")
    row["up"] = True
    row["connect"] = True
    ssh = shutil.which("ssh")
    if not ssh:
        row["detail"] = "port 22 is open; ssh is not installed on this console"
        return row
    key = (key_text or "").strip()
    if key:
        ok, detail = _ssh_with_key(ssh, login, host, key)
        if ok:
            row["running"] = True
            row["authenticated"] = True
            row["detail"] = ""
            return row
    else:
        detail = ""
    secret = (password or "").strip()
    if secret:
        sshpass = shutil.which("sshpass")
        if not sshpass:
            row["detail"] = "port 22 is open; a password is saved and sshpass is not installed"
            return row
        ok, pw_detail = _ssh_with_password(sshpass, ssh, login, host, secret)
        if ok:
            row["running"] = True
            row["authenticated"] = True
            row["detail"] = ""
            return row
        row["detail"] = pw_detail or "authentication failed"
        return row
    if not key:
        row["detail"] = "port 22 is open; no key or password is saved"
        return row
    row["detail"] = detail or "authentication failed"
    return row


def _saved_password(entry: dict[str, Any]) -> str | None:
    raw = entry.get("ssh_password")
    if not raw or not isinstance(raw, str):
        return None
    if raw.strip() in {"", "***"}:
        return None
    try:
        plain = decrypt_secret(raw)
    except Exception:
        return None
    if not plain or plain.strip() in {"", "***"}:
        return None
    return plain


def probe_environment(db: Any, env: Environment) -> dict[str, Any]:
    """Probe each saved address. Viewers can read the result."""
    from app.services import envconfig

    current = envconfig.get_current(db, env)
    servers: dict[str, Any] = {}
    if current and isinstance(current[0], dict):
        raw = current[0].get("servers")
        if isinstance(raw, dict):
            servers = raw
    key_text = None
    try:
        key_text = get_decrypted_private_key(env)
    except Exception:
        key_text = None
    default_user = str(getattr(env, "deployer_ssh_user", None) or "root")
    work: list[tuple[str, str, str, str | None]] = []
    for hostname, entry in servers.items():
        if not isinstance(entry, dict):
            continue
        ip = str(entry.get("private_ip") or entry.get("ip") or "").strip()
        user = str(entry.get("ssh_user") or default_user or "root").strip() or "root"
        work.append((str(hostname), ip, user, _saved_password(entry)))
        if len(work) >= _MAX_HOSTS:
            break

    def one(item: tuple[str, str, str, str | None]) -> dict[str, Any]:
        hostname, ip, user, password = item
        if not ip:
            return _blank(hostname, None, "no address")
        try:
            row = probe_address(ip, user, key_text=key_text, password=password)
        except Exception:
            row = _blank(hostname, ip, "check failed")
        row["hostname"] = hostname
        return row

    if not work:
        return {"hosts": []}
    with ThreadPoolExecutor(max_workers=6) as pool:
        hosts = list(pool.map(one, work))
    return {"hosts": hosts}
