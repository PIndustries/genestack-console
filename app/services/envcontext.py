"""Per-environment execution context for subprocesses.

Builds the environment variables Genestack scripts and kubectl honor
(GENESTACK_CONFIG, GENESTACK_OVERRIDES_DIR, GENESTACK_BASE_DIR,
ANSIBLE_INVENTORY, KUBECONFIG) plus the per-env dry_run override, and stages
DB-stored kubeconfig blobs to disk (0600) for the duration of a job.

When the environment sets ``deployer_ssh_host`` (Phase 4 SSH executor), the
context also exposes ``ssh_target`` / ``is_remote`` and a ``remote_env()``
helper holding exactly the genestack-scoped variables that are safe to export
on the remote deploy host (a staged, console-local kubeconfig is excluded).
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from app.config import Settings, get_settings
from app.models import Environment
from app.services import genestack_bridge as bridge
from app.services.crypto import decrypt_secret
from app.services.request_safety import configured_path

log = logging.getLogger(__name__)


@dataclass
class EnvContext:
    """Resolved execution context for one job against one environment."""

    environment: Environment | None
    genestack_root: Path
    config_dir: Path | None  # env.genestack_config_dir (expanded) or None
    dry_run: bool  # env.dry_run if set else settings.dry_run
    kubeconfig: str | None  # staged temp path, env.kubeconfig_path, or None
    ssh_target: str | None = None  # user@host / host for remote execution, None = local
    _staged_kubeconfig: str | None = field(default=None, repr=False)

    @property
    def is_remote(self) -> bool:
        """True when bridge commands for this env run over ssh on a deploy host."""
        return self.ssh_target is not None

    def subprocess_env(self) -> dict[str, str]:
        """os.environ plus the env-scoped overrides (when available)."""
        env = dict(os.environ)
        env["GENESTACK_BASE_DIR"] = str(self.genestack_root)
        if self.config_dir is not None:
            env["GENESTACK_CONFIG"] = str(self.config_dir)
            env["GENESTACK_OVERRIDES_DIR"] = str(self.config_dir)
            inventory = self.config_dir / "inventory"
            if inventory.is_dir():
                env["ANSIBLE_INVENTORY"] = str(inventory)
        if self.kubeconfig:
            env["KUBECONFIG"] = self.kubeconfig
        return env

    def remote_env(self) -> dict[str, str]:
        """Genestack-scoped variables safe to export on the remote deploy host.

        Unlike subprocess_env() this is only the scoped keys (never the whole
        os.environ), and a staged kubeconfig (console-local temp file under
        data_dir) is excluded — only a host-path kubeconfig is meaningful
        on the deploy host.
        """
        env: dict[str, str] = {"GENESTACK_BASE_DIR": str(self.genestack_root)}
        if self.config_dir is not None:
            env["GENESTACK_CONFIG"] = str(self.config_dir)
            env["GENESTACK_OVERRIDES_DIR"] = str(self.config_dir)
            inventory = self.config_dir / "inventory"
            # The inventory exists on the deployer, not necessarily this host.
            # Send the configured path; the remote command validates its files.
            env["ANSIBLE_INVENTORY"] = str(inventory)
        if self.kubeconfig and self._staged_kubeconfig is None:
            env["KUBECONFIG"] = self.kubeconfig
        return env

    def cleanup(self) -> None:
        """Remove a staged kubeconfig temp file (never env.kubeconfig_path)."""
        if self._staged_kubeconfig:
            try:
                Path(self._staged_kubeconfig).unlink(missing_ok=True)
            except OSError as exc:
                log.warning(
                    "failed to remove staged kubeconfig %s: %s",
                    self._staged_kubeconfig,
                    exc,
                )
            self._staged_kubeconfig = None

    def ensure_kubeconfig(self) -> str | None:
        """Return a kubeconfig path, staging a new file when the old one is gone.

        An API restart sweeps decrypted kubeconfig files. A deploy that is
        still running keeps the old path in its environment, and the next
        kubectl then falls through to http://localhost:8080. Write a new
        file from the stored blob when that path is missing.
        """
        path = (self.kubeconfig or "").strip()
        if path and Path(path).is_file():
            return path
        env = self.environment
        if env is None or not (env.kubeconfig_data or "").strip():
            return path or None
        fresh = _stage_kubeconfig(env, get_settings())
        self.kubeconfig = fresh
        self._staged_kubeconfig = fresh
        return fresh


def _stage_kubeconfig(env: Environment, settings: Settings) -> str:
    """Decrypt env.kubeconfig_data to a private file under kubeconfigs/ (0600).

    The staging directory is 0700: it holds decrypted cluster credentials for
    every environment, so only the console user may list/read it even while a
    job is mid-flight and the file is on disk.

    One file per context. The collector and every cluster read stage a
    kubeconfig and delete it when they return. A shared name is rewritten or
    unlinked while a deploy is still running, and the next kubectl falls
    through to http://localhost:8080.
    """
    raw = decrypt_secret(env.kubeconfig_data, settings) or ""
    target_dir = settings.data_dir / "kubeconfigs"
    os.makedirs(target_dir, mode=0o700, exist_ok=True)
    # makedirs applies the mode subject to umask, and does not touch a
    # pre-existing dir — chmod explicitly so the 0700 guarantee holds either way.
    os.chmod(target_dir, 0o700)
    target = target_dir / f"{env.id}.{uuid.uuid4().hex}.yaml"
    target.write_text(raw, encoding="utf-8")
    os.chmod(target, 0o600)
    return str(target)


def sweep_staged_kubeconfigs(
    data_dir: Path | str, *, older_than_seconds: float | None = None
) -> int:
    """Delete leftover staged kubeconfig files under <data_dir>/kubeconfigs.

    Per-job cleanup (EnvContext.cleanup) removes the file it staged, but a
    crash between staging and cleanup leaks the decrypted credentials on disk.
    Called at console startup so a restart never boots with stale kubeconfigs
    present. Best-effort: returns the number of files removed; a missing dir
    is a no-op.

    ``older_than_seconds`` keeps a file a running deploy still has open.
    Startup passes the max job timeout. A file newer than that belongs to a
    job that may still be running in the worker, which is a separate process.
    Deleting it makes the next kubectl fall through to http://localhost:8080.
    """
    kube_dir = Path(data_dir) / "kubeconfigs"
    if not kube_dir.is_dir():
        return 0
    cutoff = None
    if older_than_seconds is not None and older_than_seconds > 0:
        cutoff = time.time() - older_than_seconds
    removed = 0
    for entry in kube_dir.iterdir():
        if not entry.is_file():
            continue
        try:
            if cutoff is not None and entry.stat().st_mtime >= cutoff:
                continue
            entry.unlink()
            removed += 1
        except OSError as exc:
            log.warning("failed to sweep staged kubeconfig %s: %s", entry, exc)
    return removed


def build_context(
    env: Environment | None, settings: Settings | None = None
) -> EnvContext:
    """Resolve the execution context for a job; env=None is a no-op global context."""
    settings = settings or get_settings()
    ctx = EnvContext(
        environment=env,
        genestack_root=bridge.resolve_genestack_root(settings, env),
        config_dir=None,
        dry_run=settings.dry_run,
        kubeconfig=None,
    )
    if env is None:
        return ctx
    if env.dry_run is not None:
        ctx.dry_run = bool(env.dry_run)
    if env.genestack_config_dir:
        ctx.config_dir = configured_path(
            env.genestack_config_dir, remote=bool(env.deployer_ssh_host)
        )
    # Remote execution: commands for this env run over ssh on its deploy host
    host = (env.deployer_ssh_host or "").strip()
    user = (env.deployer_ssh_user or "").strip()
    if host:
        ctx.ssh_target = f"{user}@{host}" if user else host
    # Kubeconfig resolution order: DB blob (staged) -> host path -> kubectl default
    if env.kubeconfig_data:
        ctx.kubeconfig = _stage_kubeconfig(env, settings)
        ctx._staged_kubeconfig = ctx.kubeconfig
    elif env.kubeconfig_path:
        ctx.kubeconfig = str(Path(env.kubeconfig_path).expanduser())
    return ctx
