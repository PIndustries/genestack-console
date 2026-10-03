"""Secret files a job wrote on the executor, removed when the job ends.

The console database holds the secrets. This module is not HashiCorp Vault,
OpenBao, or 1Password. It only remembers paths this process materialized
(kubesecrets.yaml, .ssh files written under the genestack config directory,
and a kubeconfig this job created) and deletes those paths. It never removes
a tree, helm-chart-versions.yaml, the inventory, the push manifest, the
deploy host's ~/.ssh, or a kubeconfig path this job did not create.

A dry run records nothing and deletes nothing.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

LogFn = Callable[[str], None]

# Basenames that are never secret-lease targets, even if a caller asks.
_PROTECTED_NAMES = frozenset(
    {
        "helm-chart-versions.yaml",
        ".genestack-manifest.yaml",
        "inventory",
        "inventory.yaml",
        "inventory.yml",
    }
)

_current: ContextVar[SecretLease | None] = ContextVar("gsc_secret_lease", default=None)


def _posix(path: str) -> str:
    return str(path).replace("\\", "/").rstrip("/")


def _under(path: str, root: str | None) -> bool:
    if not root:
        return False
    path_n = _posix(path)
    root_n = _posix(root)
    return path_n == root_n or path_n.startswith(root_n + "/")


def _allowed(path: str, config_dir: Path | None) -> bool:
    """True when ``path`` is a single file this lease may remove."""
    text = str(path).strip()
    if not text or "\x00" in text:
        return False
    expanded = _posix(str(Path(text).expanduser())) if text.startswith("~") else _posix(text)
    if not expanded or expanded == "/":
        return False
    parts = [part for part in expanded.split("/") if part]
    if not parts or ".." in parts:
        return False
    name = parts[-1]
    if name in _PROTECTED_NAMES:
        return False
    # Staged console kubeconfigs under data_dir/kubeconfigs are EnvContext's.
    if len(parts) >= 2 and parts[-2] == "kubeconfigs":
        return False
    cfg = _posix(str(config_dir)) if config_dir is not None else None
    home_ssh = _posix(str(Path.home() / ".ssh"))
    if expanded == home_ssh or expanded.startswith(home_ssh + "/"):
        return False
    if ".ssh" in parts and not _under(expanded, cfg):
        return False
    return True


def _report(path: str, error: object, log_fn: LogFn | None) -> None:
    """Log a delete failure. ``error`` must not be file contents."""
    log.warning("failed to remove secret file %s: %s", path, error)
    if log_fn is None:
        return
    try:
        log_fn(f"[secret-lease] failed to remove {path}: {error}")
    except Exception:  # noqa: BLE001 — a log callback must not mask cleanup
        log.warning("secret lease log callback failed for %s", path)


def _rm_argv(path: str) -> list[str]:
    """One file. Never ``rm -rf`` and never a shell string."""
    return ["rm", "-f", "--", path]


@dataclass
class SecretLease:
    """Paths materialized for the current job, deleted by :meth:`release`."""

    dry_run: bool = False
    config_dir: Path | None = None
    _entries: list[_Entry] = field(default_factory=list)
    _token: Token[SecretLease | None] | None = field(default=None, repr=False)
    _released: bool = False

    def note(
        self,
        path: str | Path,
        *,
        kind: str,
        agent_env_id: str | None = None,
        ssh_target: str | None = None,
    ) -> None:
        """Remember one file. A dry run remembers nothing."""
        if self.dry_run or self._released:
            return
        text = str(path)
        if not _allowed(text, self.config_dir):
            log.warning("secret lease refused %s", text)
            return
        if any(entry.path == text for entry in self._entries):
            return
        if kind not in {"agent", "ssh", "local"}:
            log.warning("secret lease refused %s: unknown executor", text)
            return
        self._entries.append(
            _Entry(
                path=text,
                kind=kind,
                agent_env_id=agent_env_id,
                ssh_target=ssh_target,
            )
        )

    def release(self, log_fn: LogFn | None = None) -> None:
        """Delete remembered files. A dry run deletes nothing."""
        if self._released:
            return
        self._released = True
        if self.dry_run:
            return
        for entry in list(self._entries):
            try:
                _delete_entry(self, entry, log_fn)
            except Exception as exc:  # noqa: BLE001 — keep going for the rest
                _report(entry.path, exc, log_fn)


@dataclass
class _Entry:
    path: str
    kind: str
    agent_env_id: str | None = None
    ssh_target: str | None = None


def current() -> SecretLease | None:
    return _current.get()


def activate(lease: SecretLease) -> None:
    lease._token = _current.set(lease)


def deactivate(lease: SecretLease) -> None:
    token = lease._token
    lease._token = None
    if token is not None:
        _current.reset(token)


def begin(ctx: Any) -> SecretLease:
    """Open the job lease and make it current. Pair with :func:`end`."""
    config_dir = getattr(ctx, "config_dir", None)
    lease = SecretLease(
        dry_run=bool(getattr(ctx, "dry_run", False)),
        config_dir=config_dir,
    )
    activate(lease)
    return lease


def end(lease: SecretLease | None) -> None:
    if lease is not None:
        deactivate(lease)


def remember(
    path: str | Path,
    *,
    agent_env_id: str | None = None,
    ssh_target: str | None = None,
    config_dir: Path | None = None,
    log_fn: LogFn | None = None,
) -> None:
    """Record ``path`` on the job lease, or delete it now when no job is open.

    The executor is the agent when ``agent_env_id`` is set, else SSH when
    ``ssh_target`` is set, else a local unlink. That is the executor that
    wrote the file. No secret bytes are put on the command line.
    """
    if agent_env_id:
        kind, agent, ssh = "agent", agent_env_id, None
    elif ssh_target:
        kind, agent, ssh = "ssh", None, ssh_target
    else:
        kind, agent, ssh = "local", None, None
    lease = current()
    if lease is not None:
        lease.note(path, kind=kind, agent_env_id=agent, ssh_target=ssh)
        return
    scratch = SecretLease(dry_run=False, config_dir=config_dir)
    scratch.note(path, kind=kind, agent_env_id=agent, ssh_target=ssh)
    scratch.release(log_fn)


def open_push_scope(
    config_dir: Path,
    *,
    dry_run: bool,
    log_fn: LogFn | None = None,
) -> Callable[[], None]:
    """Hold secret files until the push returns, when this push is not a job.

    Inside a job the job lease is already current and this is a no-op: the
    job ``finally`` releases. A dry run does not open a scope.
    """
    if dry_run or current() is not None:
        return lambda: None
    lease = SecretLease(dry_run=False, config_dir=config_dir)
    activate(lease)

    def close() -> None:
        try:
            lease.release(log_fn)
        finally:
            deactivate(lease)

    return close


def store_kubeconfig(env: Any, text: str, db: Any) -> None:
    """Encrypt ``text`` onto ``env.kubeconfig_data`` with the existing helper.

    No-op when a session or the text is missing, or the environment already
    has kubeconfig text. Does not log ``text``.
    """
    if db is None or env is None or not text or not str(text).strip():
        return
    if getattr(env, "kubeconfig_data", None):
        return
    from app.services.crypto import encrypt_secret

    stored = encrypt_secret(text)
    if not stored:
        return
    env.kubeconfig_data = stored
    db.add(env)
    db.flush()


def _delete_entry(lease: SecretLease, entry: _Entry, log_fn: LogFn | None) -> None:
    if not _allowed(entry.path, lease.config_dir):
        _report(entry.path, "refusing to remove this path", log_fn)
        return
    if entry.kind == "local":
        _delete_local(entry.path, log_fn)
    elif entry.kind == "ssh":
        if not entry.ssh_target:
            _report(entry.path, "no ssh target", log_fn)
            return
        _delete_ssh(entry.path, entry.ssh_target, log_fn)
    elif entry.kind == "agent":
        if not entry.agent_env_id:
            _report(entry.path, "no agent", log_fn)
            return
        _delete_agent(entry.path, entry.agent_env_id, log_fn)
    else:
        _report(entry.path, "unknown executor", log_fn)


def _delete_local(path: str, log_fn: LogFn | None) -> None:
    target = Path(path)
    if target.is_dir():
        _report(path, "refusing to remove a directory", log_fn)
        return
    try:
        target.unlink(missing_ok=True)
    except OSError as exc:
        _report(path, exc, log_fn)


def _delete_ssh(path: str, ssh_target: str, log_fn: LogFn | None) -> None:
    from app.services import genestack_bridge as bridge

    # log=None: do not copy command output into the job log. The argv is only
    # the path. remote_env is empty so nothing is interpolated into the ssh line.
    result = bridge.run_command(
        _rm_argv(path),
        timeout=60,
        dry_run=False,
        ssh_target=ssh_target,
        remote_env=None,
        log=None,
    )
    if result.get("returncode") not in (0, None):
        err = result.get("stderr") or result.get("message") or "rm failed"
        _report(path, err, log_fn)


def _delete_agent(path: str, agent_env_id: str, log_fn: LogFn | None) -> None:
    from app.services import agent_relay

    reply = agent_relay.agent_exec(
        agent_env_id,
        "run_command",
        {"cmd": _rm_argv(path), "cwd": None, "env": {}, "timeout": 60},
        timeout=60,
        log_cb=None,
    )
    error = reply.get("error")
    rc = reply.get("rc")
    if error or rc not in (0, None):
        _report(path, error or reply.get("stderr") or f"rc={rc}", log_fn)
