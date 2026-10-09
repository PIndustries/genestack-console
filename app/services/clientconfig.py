"""Client talosconfig and kubeconfig copies in the console vault.

Download returns the copy saved for this environment. The first download
files the on-disk copy into the vault. Regenerate asks the cluster for a
new client certificate, valid for one year, and replaces the vault copy.
The on-disk talosconfig is left in place.
"""

from __future__ import annotations

import ipaddress
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import select

from app.models import VaultItem
from app.services.crypto import decrypt_secret, encrypt_secret
from app.services.envcontext import EnvContext
from app.services.talos import talosctl_bin, valid_node_endpoint

CLIENT_CERT_TTL = "8760h"
ISSUE_TIMEOUT = 30
_MAX_BYTES = 1_000_000
_GEN_CONFIG_NAME = "cluster"
# These stay in the console vault.
VAULT_ONLY_KINDS = frozenset({"kubeconfig", "talosconfig"})

_Noop = Callable[[], None]


def _noop() -> None:
    return None


def _stored_kubeconfig(ctx: EnvContext) -> Path | None:
    if not ctx.kubeconfig:
        return None
    path = Path(ctx.kubeconfig)
    return path if path.is_file() else None


def _talosconfig_path(ctx: EnvContext) -> Path | None:
    if ctx.config_dir is None:
        return None
    path = ctx.config_dir / "talos" / "talosconfig"
    return path if path.is_file() else None


def _read_text(path: Path) -> str:
    try:
        if path.stat().st_size > _MAX_BYTES:
            return ""
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return ""


def _endpoints(path: Path) -> list[str]:
    """Endpoint hosts from the current talosconfig context. No certificate text."""
    try:
        data = yaml.safe_load(_read_text(path))
    except yaml.YAMLError:
        return []
    if not isinstance(data, dict):
        return []
    contexts = data.get("contexts")
    current = contexts.get(data.get("context")) if isinstance(contexts, dict) else None
    raw: Any = current.get("endpoints") if isinstance(current, dict) else None
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    found: list[str] = []
    for item in raw:
        host = str(item).strip()
        if valid_node_endpoint(host):
            found.append(host)
    return found


def _https_endpoint(host: str) -> str:
    try:
        parsed = ipaddress.ip_address(host)
    except ValueError:
        return f"https://{host}:6443"
    if isinstance(parsed, ipaddress.IPv6Address):
        return f"https://[{host}]:6443"
    return f"https://{host}:6443"


def _temp_dir() -> Path:
    root = Path(tempfile.mkdtemp(prefix="gsc-client-"))
    os.chmod(root, 0o700)
    return root


def _discard(path: Path | None) -> None:
    if path is None:
        return
    parent = path.parent
    if parent.name.startswith("gsc-client-"):
        shutil.rmtree(parent, ignore_errors=True)
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        return


def _discard_later(path: Path) -> _Noop:
    done = False

    def cleanup() -> None:
        nonlocal done
        if done:
            return
        done = True
        _discard(path)

    return cleanup


def _run(argv: list[str], *, timeout: int = ISSUE_TIMEOUT) -> int:
    try:
        proc = subprocess.run(
            argv,
            timeout=timeout,
            capture_output=True,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 1
    return int(proc.returncode)


def _looks_like_talosconfig(path: Path) -> bool:
    text = _read_text(path)
    return "contexts:" in text and "ca:" in text


def _looks_like_kubeconfig(path: Path) -> bool:
    text = _read_text(path)
    return "clusters:" in text and ("apiVersion:" in text or "kind:" in text)


def _accept(path: Path, check) -> Path | None:
    if check(path):
        os.chmod(path, 0o600)
        return path
    _discard(path)
    return None


def _config_new(ctl: str, talos_path: Path, endpoint: str) -> Path | None:
    work = _temp_dir()
    dest = work / "talosconfig"
    argv = [
        ctl,
        "--nodes",
        endpoint,
        "--talosconfig",
        str(talos_path),
        "config",
        "new",
        str(dest),
        "--roles",
        "os:admin",
        "--crt-ttl",
        CLIENT_CERT_TTL,
    ]
    if _run(argv) != 0:
        _discard(dest)
        return None
    return _accept(dest, _looks_like_talosconfig)


def _from_secrets(ctl: str, talos_path: Path, endpoint: str) -> Path | None:
    secrets = talos_path.parent / "secrets.yaml"
    if not secrets.is_file():
        return None
    work = _temp_dir()
    argv = [
        ctl,
        "gen",
        "config",
        _GEN_CONFIG_NAME,
        _https_endpoint(endpoint),
        "--with-secrets",
        str(secrets),
        "--output-types",
        "talosconfig",
        "--output-dir",
        str(work),
    ]
    if _run(argv) != 0:
        _discard(work / "talosconfig")
        return None
    return _accept(work / "talosconfig", _looks_like_talosconfig)


def _kubeconfig_from(ctl: str, talos_path: Path, endpoint: str) -> Path | None:
    work = _temp_dir()
    dest = work / "kubeconfig"
    argv = [
        ctl,
        "kubeconfig",
        str(dest),
        "--nodes",
        endpoint,
        "--talosconfig",
        str(talos_path),
        "--force",
    ]
    if _run(argv) != 0:
        _discard(dest)
        return None
    return _accept(dest, _looks_like_kubeconfig)


def _issue_talosconfig(ctx: EnvContext) -> Path | None:
    talos_path = _talosconfig_path(ctx)
    ctl = talosctl_bin()
    if talos_path is None or not ctl:
        return None
    endpoints = _endpoints(talos_path)
    if not endpoints:
        return None
    endpoint = endpoints[0]
    issued = _config_new(ctl, talos_path, endpoint)
    if issued is not None:
        return issued
    return _from_secrets(ctl, talos_path, endpoint)


def _issue_kubeconfig(ctx: EnvContext) -> Path | None:
    talos_path = _talosconfig_path(ctx)
    ctl = talosctl_bin()
    if talos_path is None or not ctl:
        return None
    endpoints = _endpoints(talos_path)
    if not endpoints:
        return None
    endpoint = endpoints[0]
    issued = _kubeconfig_from(ctl, talos_path, endpoint)
    if issued is not None:
        return issued
    minted = _from_secrets(ctl, talos_path, endpoint)
    if minted is None:
        return None
    try:
        return _kubeconfig_from(ctl, minted, endpoint)
    finally:
        _discard(minted)


def _can_ask(ctx: EnvContext, kind: str) -> bool:
    if kind not in {"kubeconfig", "talosconfig"}:
        return False
    if ctx.dry_run or not talosctl_bin():
        return False
    talos_path = _talosconfig_path(ctx)
    return bool(talos_path is not None and _endpoints(talos_path))


def _acceptable(kind: str, text: str) -> bool:
    if kind == "kubeconfig":
        return "clusters:" in text and ("apiVersion:" in text or "kind:" in text)
    if kind == "talosconfig":
        return "contexts:" in text and "ca:" in text
    return False


def _write_secret(text: str, kind: str) -> tuple[Path, _Noop]:
    work = _temp_dir()
    dest = work / kind
    dest.write_text(text, encoding="utf-8")
    os.chmod(dest, 0o600)
    return dest, _discard_later(dest)


def remember_client_config(
    db: Any,
    env: Any,
    kind: str,
    text: str,
    *,
    commit: bool = False,
) -> bool:
    """Encrypt ``text`` into this environment's vault. The text is not logged.

    ``commit`` is for a request that would otherwise close the session.
    A job leaves the commit to the job runner.
    """
    if db is None or env is None or kind not in VAULT_ONLY_KINDS:
        return False
    tenant_id = getattr(env, "tenant_id", None)
    env_id = getattr(env, "id", None)
    if not tenant_id or not env_id or not str(text or "").strip():
        return False
    if not _acceptable(kind, text):
        return False
    stored = encrypt_secret(text)
    if not stored:
        return False
    row = db.scalar(
        select(VaultItem).where(
            VaultItem.tenant_id == tenant_id,
            VaultItem.environment_id == env_id,
            VaultItem.name == kind,
        )
    )
    if row is None:
        row = VaultItem(
            tenant_id=tenant_id,
            environment_id=env_id,
            name=kind,
            kind=kind,
            value_encrypted=stored,
        )
        db.add(row)
    else:
        row.kind = kind
        row.value_encrypted = stored
    db.flush()
    if commit:
        db.commit()
    return True


def read_client_config(db: Any, env: Any, kind: str) -> str | None:
    """Decrypt the vault copy. Returns None when it is missing or not a config."""
    if db is None or env is None or kind not in VAULT_ONLY_KINDS:
        return None
    tenant_id = getattr(env, "tenant_id", None)
    env_id = getattr(env, "id", None)
    if not tenant_id or not env_id:
        return None
    row = db.scalar(
        select(VaultItem).where(
            VaultItem.tenant_id == tenant_id,
            VaultItem.environment_id == env_id,
            VaultItem.name == kind,
        )
    )
    if row is None:
        return None
    text = decrypt_secret(row.value_encrypted) or ""
    if not _acceptable(kind, text):
        return None
    return text


def grab_client_config(
    ctx: EnvContext,
    env: Any,
    db: Any,
    kind: str,
) -> tuple[Path | None, str, _Noop]:
    """Return the vault copy. File the on-disk copy into the vault when it is empty."""
    if kind not in VAULT_ONLY_KINDS:
        return None, "disk", _noop
    saved = read_client_config(db, env, kind)
    if saved:
        path, cleanup = _write_secret(saved, kind)
        return path, "vault", cleanup
    disk = _stored_kubeconfig(ctx) if kind == "kubeconfig" else _talosconfig_path(ctx)
    disk_text = _read_text(disk) if disk is not None else ""
    if not disk_text or disk is None:
        return None, "disk", _noop
    # A file that is not a client config is still served. It is not stored.
    if not _acceptable(kind, disk_text):
        return disk, "disk", _noop
    filed = remember_client_config(db, env, kind, disk_text, commit=True)
    path, cleanup = _write_secret(disk_text, kind)
    return path, "filed" if filed else "disk", cleanup


def renew_client_config(
    ctx: EnvContext,
    env: Any,
    db: Any,
    kind: str,
) -> tuple[Path | None, str, _Noop]:
    """Ask the cluster for a new client certificate and replace the vault copy."""
    path, source, cleanup = issue_client_config(ctx, kind)
    if source != "issued" or path is None:
        cleanup()
        return None, source, _noop
    text = _read_text(path)
    if text:
        remember_client_config(db, env, kind, text, commit=True)
    return path, "issued", cleanup


def issue_client_config(
    ctx: EnvContext, kind: str
) -> tuple[Path | None, str, _Noop]:
    """Return ``(path, source, cleanup)``.

    ``source`` is ``issued`` when the cluster signed a new client certificate,
    ``stored`` when the cluster was asked and did not, and ``saved`` when this
    console did not ask. ``cleanup`` removes an issued temp file. It does not
    remove the console's saved talosconfig or kubeconfig.
    """
    stored = _stored_kubeconfig(ctx) if kind == "kubeconfig" else _talosconfig_path(ctx)
    if kind not in {"kubeconfig", "talosconfig"}:
        return None, "saved", _noop
    if not _can_ask(ctx, kind):
        return stored, "saved", _noop
    issued = _issue_kubeconfig(ctx) if kind == "kubeconfig" else _issue_talosconfig(ctx)
    if issued is not None:
        return issued, "issued", _discard_later(issued)
    return stored, "stored", _noop
