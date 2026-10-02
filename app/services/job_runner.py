"""Job runner — executes catalog operations, sync (in-process) or via worker.

Each operation is a file under ``app/modules/``. The class file in that folder
lists the function files. ``_dispatch`` prepares the run and calls the one
function whose handler matches the job.

Jobs are queued by default (see execute_operation) and claimed by the worker
(app.worker.runner); explicit run_sync=True still executes inline for quick
ops and tests. Commands run locally on the console host by default; when the
job's environment sets deployer_ssh_host, bridge calls execute over ssh on
that environment's deploy host (see EnvContext.ssh_target / remote_env).

Timeouts: each catalog op may set timeout_seconds (clamped to
MAX_JOB_TIMEOUT_SECONDS); ops without one use settings.job_timeout_seconds.
recover_stale_jobs() fails *running* jobs past their deadline (started_at +
effective timeout); queued jobs are never expired on wait age.
recover_abandoned_running_jobs() fails all running jobs and is called once
at job-worker startup only — never from the API lifespan.
"""

from __future__ import annotations

import base64
import logging
import math
import os
import re
import shlex
import shutil
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import Settings, get_settings

if TYPE_CHECKING:
    from app.services.maas import MaasClient

from app.models import AuditLog, EnvMutex, Environment, HostVM, Job, JobStatus
from app.schemas import OperationSpec
from app.services import genestack_bridge as bridge
from app.services import agent_relay
from app.services import logredact
from app.services.catalog import (
    all_secret_param_names,
    get_operation,
    mutating_operation_ids,
    secret_param_names,
    validate_params,
)
from app.services.crypto import decrypt_secret, encrypt_secret
from app.services.envcontext import EnvContext, build_context

log = logging.getLogger(__name__)

# Hard ceiling for any per-op timeout (6h) — constant, not configurable.
MAX_JOB_TIMEOUT_SECONDS = 6 * 3600

# Job-log commit batching: append_log keeps the log in memory per job and
# commits once EITHER threshold is exceeded, or on an explicit flush_logs()
# at job finalization. This ends the O(n^2) pattern of a full log_text
# column rewrite plus commit/fsync per log line on long ops (deploy,
# tempest). Each commit stays short and bounded, so the SQLite write lock
# is released between bursts instead of being held across the dispatch.
LOG_FLUSH_BYTES = 4 * 1024
LOG_FLUSH_LINES = 4

RECOVERY_ERROR = "job expired/recovered after restart"
ABANDONED_ERROR = "worker restarted; in-flight job was abandoned"

# Placeholder persisted in Job.params (and audit details) in place of a
# catalog-marked secret param value; the real value lives fernet-encrypted
# in Job.secret_params and is merged back in memory only at execution time.
SECRET_PARAM_MASK = "***"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ConflictError(RuntimeError):
    """A mutating job is already queued/running for the environment."""

    def __init__(self, message: str, job_id: str | None = None):
        super().__init__(message)
        self.job_id = job_id


class MaasDownloadError(RuntimeError):
    """Factory image download failed (HTTP error or size bound exceeded)."""


class JobCancelledError(RuntimeError):
    """An operator cancelled the job while it was running (between commands)."""


class JobDeadlineExceededError(RuntimeError):
    """The per-job deadline (started_at + effective timeout) passed mid-loop."""


CANCELLED_ERROR = "cancelled by operator"


def effective_timeout_seconds(op: OperationSpec | None, settings: Settings) -> int:
    """The op's timeout (clamped to the hard ceiling), else the global default."""
    if op is not None and op.timeout_seconds:
        return min(op.timeout_seconds, MAX_JOB_TIMEOUT_SECONDS)
    return settings.job_timeout_seconds


def cancel_requested(db: Session, job_id: str) -> bool:
    """Fresh read of the cancel flag (set cross-process by the cancel API)."""
    return bool(db.scalar(select(Job.cancel_requested).where(Job.id == job_id)))


def clamp_deadline_timeout(timeout: int, deadline: float | None) -> int:
    """Per-command timeout clamped to the job's remaining deadline budget.

    The op timeout is per-command; the deadline caps the whole job. Raises
    JobDeadlineExceededError once the deadline has passed so multi-command
    loops (pipeline stages, deploy) stop instead of starting another item.
    """
    if deadline is None:
        return timeout
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise JobDeadlineExceededError("job deadline exceeded — stopping")
    return min(timeout, max(1, math.ceil(remaining)))


# Upper bound for a downloaded Talos factory image (2 GiB).
MAX_TALOS_IMAGE_BYTES = 2 * 1024 * 1024 * 1024


def download_factory_image(
    url: str,
    log,
    dest_dir: str | Path,
    filename: str | None = None,
    max_bytes: int = MAX_TALOS_IMAGE_BYTES,
) -> tuple[Path, str]:
    """Download a Talos factory image over HTTPS to ``dest_dir``; returns
    ``(path, sha256)``.

    The bytes stream straight to disk (1 MiB chunks): a 2 GiB factory image
    must not be held in process memory (the previous in-RAM version peaked
    near 4 GiB in the worker container). Raises :class:`MaasDownloadError`
    on HTTP errors, when the size bound is exceeded, or when the
    destination is not writable — the partial file is removed on failure.
    Shared by the MAAS image-upload op (``maas.talos.image_upload``) and the
    PXE asset fetch (app/services/pxe.py).
    """
    import hashlib

    import httpx

    dest = Path(dest_dir)
    if not filename:
        stem = Path(urlparse(url).path).name or "factory-image"
        filename = f"{stem}.img"
    dest_path = dest / filename
    dest.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    total = 0
    try:
        with dest_path.open("wb") as out:
            with httpx.Client(follow_redirects=True, timeout=600.0) as http:
                with http.stream("GET", url) as response:
                    if response.status_code >= 400:
                        raise MaasDownloadError(
                            f"download failed: HTTP {response.status_code} from {url}"
                        )
                    for chunk in response.iter_bytes(1024 * 1024):
                        total += len(chunk)
                        if total > max_bytes:
                            raise MaasDownloadError(
                                f"download exceeds {max_bytes} byte limit"
                            )
                        digest.update(chunk)
                        out.write(chunk)
    except httpx.HTTPError as exc:
        raise MaasDownloadError(f"download failed: {exc}") from exc
    finally:
        # A partial file must not be mistaken for a completed image by a
        # later run (the PXE fetcher skips downloads when assets exist).
        if not dest_path.exists() or dest_path.stat().st_size != total:
            dest_path.unlink(missing_ok=True)
    sha256 = digest.hexdigest()
    if log:
        log(f"[talos-image] downloaded {total} bytes to {dest_path} sha256={sha256}")
    return dest_path, sha256


class K8sUpgradeFileError(RuntimeError):
    """The k8s_cluster group_vars file is missing or has no kube_version line."""


def _read_group_vars_text(
    file_path: Path, ssh_target: str | None, agent_env_id: str | None, log
) -> str | None:
    """Existing file content on the local FS or over ssh; None if absent."""
    if ssh_target:
        result = bridge.run_command(
            ["cat", str(file_path)],
            dry_run=False,
            ssh_target=ssh_target,
            agent_env_id=agent_env_id,
            log=None,
        )
        if result.get("returncode") != 0:
            return None
        return result.get("stdout") or ""
    if not file_path.exists():
        return None
    return file_path.read_text(encoding="utf-8")


def _kube_version_rewritten(
    file_path: Path,
    kube_version: str,
    ssh_target: str | None,
    agent_env_id: str | None,
    log,
) -> tuple[str, str]:
    """Return (old_version, new_file_text) with the kube_version line replaced.

    Rewrites only the first *active* (non-comment) ``kube_version:`` line;
    raises K8sUpgradeFileError when the file is missing or has no such line
    — never guesses.
    """
    text = _read_group_vars_text(file_path, ssh_target, agent_env_id, log)
    if text is None:
        raise K8sUpgradeFileError(
            f"kube_version source file not found: {file_path} — run the kubespray "
            "deployment (or bootstrap) for this environment first"
        )
    pattern = re.compile(r"^[ \t]*kube_version:[ \t]*\S+", re.MULTILINE)
    # Comment lines start with '#' so they cannot match this pattern — the
    # first match is the first active kube_version: line.
    match = pattern.search(text)
    if match is None:
        raise K8sUpgradeFileError(
            f"no active 'kube_version:' line in {file_path} — refusing to guess where "
            "to write the target version"
        )
    old = match.group(0).split(":", 1)[1].strip()
    new_text = (
        text[: match.start()] + f"kube_version: {kube_version}" + text[match.end() :]
    )
    log(f"[k8s-upgrade] kube_version {old} -> {kube_version} in {file_path}")
    return old, new_text


def _write_group_vars_with_backup(
    file_path: Path,
    new_text: str,
    ssh_target: str | None,
    agent_env_id: str | None,
    log,
) -> Path:
    """Write new_text to file_path, first copying the original next to it
    as k8s-cluster.yml.bak-<utc-ts> (envconfig push backup style)."""
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = file_path.with_name(file_path.name + f".bak-{timestamp}")
    if ssh_target:
        t = shlex.quote(str(file_path))
        b = shlex.quote(str(backup_path))
        b64 = base64.b64encode(new_text.encode("utf-8")).decode("ascii")
        script = (
            f"if [ -f {t} ]; then cp {t} {b}; fi && "
            f"echo {shlex.quote(b64)} | base64 -d > {t}"
        )
        result = bridge.run_command(
            ["bash", "-c", script],
            dry_run=False,
            ssh_target=ssh_target,
            agent_env_id=agent_env_id,
            log=log,
        )
        if result.get("returncode") not in (0, None):
            raise K8sUpgradeFileError(
                f"failed to write {file_path} over ssh: {result.get('message', 'unknown error')}"
            )
    else:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        if file_path.exists():
            shutil.copy2(file_path, backup_path)
        file_path.write_text(new_text, encoding="utf-8")
        os.chmod(file_path, 0o644)
    log(f"[backup] {file_path} -> {backup_path}")
    log(f"[write] {file_path} ({len(new_text)} bytes)")
    return backup_path


def find_mutating_conflict(
    db: Session,
    environment_id: str,
    exclude_job_id: str | None = None,
) -> Job | None:
    """Return a queued/running mutating job for the environment, if any."""
    mutating = mutating_operation_ids()
    stmt = (
        select(Job)
        .where(Job.environment_id == environment_id)
        .where(Job.status.in_([JobStatus.queued, JobStatus.running]))
        .order_by(Job.created_at.asc())
    )
    for job in db.scalars(stmt).all():
        if exclude_job_id and job.id == exclude_job_id:
            continue
        if job.operation in mutating:
            return job
    return None


def acquire_env_mutex(
    db: Session, environment_id: str, job_id: str, *, job_status: str | None = None
) -> None:
    """Atomically record that ``job_id`` holds the mutating lock for an env.

    The env_mutexes row (PRIMARY KEY environment_id) is the source of truth:
    a racing INSERT fails with IntegrityError (dialect-agnostic — works on
    both SQLite and Postgres) instead of letting two submitters into the
    critical section. The loser's transaction is rolled back, which also
    discards its own job row, and a ConflictError naming the real holder is
    raised. Callers commit afterwards on the winning path.
    """
    try:
        db.add(EnvMutex(environment_id=environment_id, job_id=job_id))
        db.flush()
    except IntegrityError:
        db.rollback()
        holder = db.get(EnvMutex, environment_id)
        conflict = db.get(Job, holder.job_id) if holder is not None else None
        if conflict is not None:
            status_part, op_part = conflict.status.value, f" ({conflict.operation})"
        else:
            status_part, op_part = (job_status or "running"), ""
        raise ConflictError(
            f"Environment already has a mutating job {status_part}: "
            f"{holder.job_id if holder else 'unknown'}{op_part}",
            job_id=holder.job_id if holder else None,
        ) from None


def release_env_mutex(db: Session, environment_id: str, job_id: str) -> None:
    """Drop the env's mutex row if this job still holds it (best-effort).

    Never raises: a release failure must not mask the job's own terminal
    commit. Called in the same transaction as the status write, so the mutex
    disappears atomically with the job going terminal.
    """
    if not environment_id:
        return
    try:
        row = db.get(EnvMutex, environment_id)
        if row is not None and row.job_id == job_id:
            db.delete(row)
    except Exception:  # noqa: BLE001
        log.warning("failed to release env mutex for %s", environment_id, exc_info=True)


class JobRunner:
    """Execute catalog operations, append logs, write audit entries.

    Each operation lives in its own file under app/modules/. This class sets
    up the environment and calls that file. It does not contain the steps.
    """

    def __init__(self, db: Session, settings: Settings | None = None):
        self.db = db
        self.settings = settings or get_settings()
        # Log lines/bytes buffered since the last commit, per job. Keyed by
        # job.id because one runner may serve several jobs
        # (recover_stale_jobs); a fresh runner typically serves one.
        self._log_lines: dict[str, int] = {}
        self._log_bytes: dict[str, int] = {}

    def append_log(self, job: Job, line: str) -> None:
        stamp = _utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        chunk = f"[{stamp}] {logredact.redact_secret_line(line)}\n"
        # Keep the in-memory column current on EVERY append so any commit —
        # including one made outside this runner (the cancel API calls
        # append_log and then commits its own session) — persists the full
        # log so far. Only the commit itself is throttled (flush_logs).
        job.log_text = (job.log_text or "") + chunk
        self._log_lines[job.id] = self._log_lines.get(job.id, 0) + 1
        self._log_bytes[job.id] = self._log_bytes.get(job.id, 0) + len(chunk)
        if (
            self._log_lines[job.id] >= LOG_FLUSH_LINES
            or self._log_bytes[job.id] >= LOG_FLUSH_BYTES
        ):
            self.flush_logs(job)

    def flush_logs(self, job: Job) -> None:
        """Commit buffered log lines for one job; no-op when nothing pending.

        Call at job finalization (success/failure/cancel/recovery) so the
        log_text column is complete right after the job ends — SSE/live log
        readers poll that column. Commits (not just flushes): a flush-only
        session holds the SQLite write lock for the whole dispatch, which
        deadlocks agent channel calls — agent_relay.agent_exec writes
        agent_commands rows from the same thread on a second connection
        while the job is still running. Threshold-bounded commits release
        the lock between bursts instead of holding it across the dispatch.
        """
        if self._log_lines.pop(job.id, 0) == 0:
            self._log_bytes.pop(job.id, None)
            return
        self._log_bytes.pop(job.id, None)
        self.db.add(job)
        self.db.commit()

    def write_audit(
        self,
        *,
        actor: str,
        action: str,
        resource_type: str | None = None,
        resource_id: str | None = None,
        environment_id: str | None = None,
        details: dict[str, Any] | None = None,
        success: bool = True,
    ) -> AuditLog:
        entry = AuditLog(
            actor=actor,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            environment_id=environment_id,
            details=details or {},
            success=success,
        )
        self.db.add(entry)
        self.db.flush()
        return entry

    def create_job(
        self,
        *,
        operation: str,
        params: dict[str, Any] | None = None,
        environment_id: str | None = None,
        created_by: str | None = None,
        secret_params: dict[str, str] | None = None,
    ) -> Job:
        """Create a queued job row.

        Catalog-marked secret params (OperationSpec.secret_params) are never
        persisted in plaintext: the stored ``params`` hold SECRET_PARAM_MASK
        in their place and the real values go fernet-encrypted into
        ``secret_params``, merged back into the in-memory params by run_job.
        ``secret_params`` accepts already-encrypted values carried over from
        a previous job (retry), so a retry survives its source's scrubbing.
        """
        scrubbed = dict(params or {})
        secrets: dict[str, str] = dict(secret_params or {})
        for name in secret_param_names(operation):
            value = scrubbed.get(name)
            # Missing/empty stays absent (validation reports it); a mask
            # sentinel (scrubbed source params on retry) is kept as-is.
            if not isinstance(value, str) or not value or value == SECRET_PARAM_MASK:
                continue
            secrets[name] = encrypt_secret(value, self.settings)
            scrubbed[name] = SECRET_PARAM_MASK
        job = Job(
            environment_id=environment_id,
            operation=operation,
            params=scrubbed,
            secret_params=secrets or None,
            status=JobStatus.queued,
            log_text="",
            created_by=created_by,
        )
        self.db.add(job)
        self.db.flush()
        return job

    @staticmethod
    def _execution_params(job: Job) -> dict[str, Any]:
        """In-memory params for dispatch: row params with secrets restored.

        The row's ``params`` hold SECRET_PARAM_MASK for catalog-marked secret
        params; the real values (fernet-encrypted in ``secret_params``) are
        decrypted here and never written back to the row.
        """
        params = dict(job.params or {})
        for name, stored in (job.secret_params or {}).items():
            plain = decrypt_secret(stored)
            if plain:
                params[name] = plain
        return params

    def run_job(self, job: Job) -> Job:
        """Execute a queued job synchronously."""
        op = get_operation(job.operation)
        actor = job.created_by or "system"

        if op is None:
            job.status = JobStatus.failed
            job.error = f"Unknown operation: {job.operation}"
            job.finished_at = _utcnow()
            release_env_mutex(self.db, job.environment_id, job.id)
            self.append_log(job, job.error)
            self.write_audit(
                actor=actor,
                action="job.failed",
                resource_type="job",
                resource_id=job.id,
                environment_id=job.environment_id,
                details={"operation": job.operation, "error": job.error},
                success=False,
            )
            self.flush_logs(job)
            self.db.commit()
            return job

        exec_params = self._execution_params(job)
        errors = validate_params(job.operation, exec_params)
        if errors:
            job.status = JobStatus.failed
            job.error = "; ".join(errors)
            job.finished_at = _utcnow()
            release_env_mutex(self.db, job.environment_id, job.id)
            self.append_log(job, f"Validation failed: {job.error}")
            self.write_audit(
                actor=actor,
                action="job.failed",
                resource_type="job",
                resource_id=job.id,
                environment_id=job.environment_id,
                details={"operation": job.operation, "errors": errors},
                success=False,
            )
            self.flush_logs(job)
            self.db.commit()
            return job

        job.status = JobStatus.running
        job.started_at = _utcnow()
        self.append_log(job, f"Starting operation={job.operation} handler={op.handler}")
        self.write_audit(
            actor=actor,
            action="job.started",
            resource_type="job",
            resource_id=job.id,
            environment_id=job.environment_id,
            details={"operation": job.operation, "params": job.params},
            success=True,
        )
        self.flush_logs(job)
        self.db.commit()

        env: Environment | None = None
        if job.environment_id:
            env = self.db.get(Environment, job.environment_id)

        def log(msg: str) -> None:
            self.append_log(job, msg)

        ctx = build_context(env, self.settings)
        # Per-job deadline: the op timeout is per-command; this caps the whole
        # job so multi-command loops cannot run N x the timeout.
        deadline = time.monotonic() + effective_timeout_seconds(op, self.settings)

        def check_cancel() -> None:
            if cancel_requested(self.db, job.id):
                raise JobCancelledError(CANCELLED_ERROR)

        try:
            # Backstop (worker mode): never run two mutating jobs for one env
            if op.mutating and job.environment_id:
                conflict = find_mutating_conflict(
                    self.db, job.environment_id, exclude_job_id=job.id
                )
                if conflict is not None:
                    raise ConflictError(
                        f"Environment already has a mutating job {conflict.status.value}: "
                        f"{conflict.id} ({conflict.operation})",
                        job_id=conflict.id,
                    )
            check_cancel()
            result = self._dispatch(
                op,
                job,
                env,
                log,
                ctx,
                params=exec_params,
                deadline=deadline,
                check_cancel=check_cancel,
            )
            # Persist the rehearsal marker so the UI can distinguish
            # "it worked" from "it would have worked".
            if isinstance(result, dict) and "dry_run" in result:
                job.dry_run = bool(result["dry_run"])
            ok = bool(result.get("ok", True)) if isinstance(result, dict) else True
            if isinstance(result, dict) and result.get("error") and not ok:
                raise RuntimeError(result["error"])

            # Treat non-zero returncode as failure when present
            if isinstance(result, dict) and "returncode" in result:
                rc = result["returncode"]
                if rc not in (0, None) and not result.get("dry_run"):
                    ok = False

            if ok:
                job.status = JobStatus.success
                job.error = None
                self.append_log(
                    job,
                    f"Completed successfully: {result.get('message', 'ok') if isinstance(result, dict) else 'ok'}",
                )
                if isinstance(result, dict):
                    # Keep result summary in log
                    summary_keys = (
                        "dry_run",
                        "count",
                        "service",
                        "playbook",
                        "path",
                        "script_count",
                        "kind",
                        "imported",
                        "timings",
                    )
                    summary = {k: result[k] for k in summary_keys if k in result}
                    if summary:
                        self.append_log(job, f"result_summary={summary}")
                self.write_audit(
                    actor=actor,
                    action="job.success",
                    resource_type="job",
                    resource_id=job.id,
                    environment_id=job.environment_id,
                    details={
                        "operation": job.operation,
                        "result_keys": (
                            list(result.keys()) if isinstance(result, dict) else []
                        ),
                    },
                    success=True,
                )
            else:
                err = (
                    (result.get("error") if isinstance(result, dict) else None)
                    or (result.get("message") if isinstance(result, dict) else None)
                    or "operation failed"
                )
                job.status = JobStatus.failed
                job.error = str(err)
                self.append_log(job, f"Failed: {job.error}")
                self.write_audit(
                    actor=actor,
                    action="job.failed",
                    resource_type="job",
                    resource_id=job.id,
                    environment_id=job.environment_id,
                    details={"operation": job.operation, "error": job.error},
                    success=False,
                )
        except Exception as exc:  # noqa: BLE001
            job.status = JobStatus.failed
            job.error = str(exc)
            self.append_log(job, f"Exception: {exc}")
            self.write_audit(
                actor=actor,
                action="job.failed",
                resource_type="job",
                resource_id=job.id,
                environment_id=job.environment_id,
                details={"operation": job.operation, "error": str(exc)},
                success=False,
            )
        finally:
            ctx.cleanup()
            job.finished_at = _utcnow()
            release_env_mutex(self.db, job.environment_id, job.id)
            self.flush_logs(job)
            self.db.add(job)
            self.db.commit()
            self.db.refresh(job)

        return job

    def _maas_creds(self, env: Environment | None) -> tuple[str, str]:
        if env:
            url = env.maas_url or self.settings.maas_url
            key = (
                decrypt_secret(env.maas_api_key_encrypted) or self.settings.maas_api_key
            )
            return url or "", key or ""
        return self.settings.maas_url or "", self.settings.maas_api_key or ""

    def _maas_client(self, env: Environment | None) -> MaasClient:
        """MAAS client for an env: per-env creds, global mock only as fallback.

        The dev-only mock inventory (``maas.mock: true``) applies only when no
        MAAS URL is configured for the env or globally — never silently.
        """
        from app.services.maas import MaasClient

        url, key = self._maas_creds(env)
        mock = bool(getattr(self.settings, "maas_mock", False)) and not url
        return MaasClient.from_settings(
            {"maas_url": url, "maas_api_key": key, "maas_mock": mock}
        )

    def _maas_machine_action(
        self,
        handler: str,
        job: Job,
        env: Environment | None,
        params: dict[str, Any],
        log,
        dry: bool,
    ) -> dict[str, Any]:
        """commission/deploy/release a MAAS machine (write ops, per-env creds)."""
        action = handler.removeprefix("maas_machine_")  # commission|deploy|release
        op_id = f"maas.machine.{action}"
        if env is None:
            return {
                "ok": False,
                "error": f"{op_id} requires an environment",
                "returncode": 2,
            }
        system_id = str(params.get("system_id", "")).strip()
        if not system_id:
            return {
                "ok": False,
                "error": f"{op_id}: system_id is required",
                "returncode": 2,
            }
        hostname = str(params.get("hostname") or "").strip() or None
        # Custom image name (uploaded boot-resource, e.g. a Talos factory
        # image); deploy-only.
        image = str(params.get("image") or "").strip() or None
        raw_roles = params.get("roles") or []
        if isinstance(raw_roles, str):
            raw_roles = raw_roles.split(",")
        roles = [str(r).strip().lower() for r in raw_roles if str(r).strip()]
        url, _ = self._maas_creds(env)  # url only feeds the dry-run log line

        from app.services import envconfig as envconfig_service

        if action == "deploy" and roles:
            invalid = [
                r for r in roles if r not in envconfig_service.VALID_SERVER_ROLES
            ]
            if invalid:
                valid = ", ".join(sorted(envconfig_service.VALID_SERVER_ROLES))
                return {
                    "ok": False,
                    "error": f"{op_id}: unknown role(s) {', '.join(invalid)} (valid: {valid})",
                    "returncode": 2,
                }

        if dry:
            log(
                f"[dry-run] would POST machines/{system_id}/ op={action} "
                f"hostname={hostname or '-'} roles={','.join(roles) or '-'} "
                f"image={image or '-'} "
                f"against {url or 'MAAS (not configured)'}"
            )
            return {
                "ok": True,
                "dry_run": True,
                "action": action,
                "system_id": system_id,
                "message": f"[dry-run] would {action} machine {system_id}",
            }

        from app.services.maas import MaasError

        client = self._maas_client(env)
        is_mock = client.mock
        try:
            # Resolve the machine first: validates system_id and provides the
            # fallback hostname for user-data rendering / doc upsert.
            machine = client.get_machine(system_id)
            target_hostname = hostname or str(machine.get("hostname") or system_id)
            user_data_b64 = None
            if action == "deploy" and image:
                # Custom-image deploys (talos) don't consume cloud-init
                # user-data — deploy with osystem=custom instead.
                log(
                    f"[maas] deploy system_id={system_id} image={image} "
                    "(osystem=custom) — skipping cloud-init user-data "
                    "(talos doesn't use it)"
                )
            elif action == "deploy" and (roles or hostname):
                from app.services.maas_userdata import render_userdata

                # GENESTACK_ENV / GENESTACK_ROLE markers land in
                # /etc/genestack/env on the node — provision_bridge.yml
                # (genestack-console/ansible/playbooks) reads them post-deploy.
                user_data = render_userdata(
                    hostname=target_hostname,
                    genestack_env=env.name,
                    genestack_role=",".join(roles) if roles else None,
                )
                user_data_b64 = base64.b64encode(user_data.encode()).decode()
            if action == "commission":
                machine = client.commission(system_id, user_data_b64=user_data_b64)
            elif action == "deploy":
                machine = client.deploy(
                    system_id,
                    user_data_b64=user_data_b64,
                    hostname=hostname,
                    image=image,
                )
            else:
                machine = client.release(system_id)
        except MaasError as exc:
            log(f"[maas] {action} system_id={system_id} failed: {exc}")
            return {
                "ok": False,
                "error": str(exc),
                "action": action,
                "system_id": system_id,
                "returncode": 2,
            }
        finally:
            client.close()

        status_name = machine.get("status_name")
        log(
            f"[maas] {action} system_id={system_id} -> status={status_name} mock={is_mock}"
        )
        result: dict[str, Any] = {
            "ok": True,
            "dry_run": False,
            "mock": is_mock,
            "action": action,
            "system_id": system_id,
            "status": status_name,
            "machine": machine,
            "message": f"{action} {system_id}: {status_name}",
        }

        if action == "deploy":
            # Deploy -> inventory in one action: upsert the machine into the
            # env config doc servers section (source maas, new config version).
            row, warnings = envconfig_service.assign_server(
                self.db,
                env,
                job.created_by or "system",
                system_id=system_id,
                hostname=target_hostname,
                roles=roles,
            )
            for warning in warnings:
                log(f"[deploy] config warning: {warning}")
            log(
                f"[deploy] upserted servers.{target_hostname} (source=maas) "
                f"in config version {row.version}"
            )
            result["hostname"] = target_hostname
            result["roles"] = roles
            result["config_version"] = row.version
            if image:
                result["image"] = image
        elif action == "release":
            # Release -> inventory cleanup: drop the machine from the env
            # config doc servers section (new config version). Best-effort —
            # a doc cleanup failure must not fail the release itself.
            try:
                removed = envconfig_service.remove_server(
                    self.db,
                    env,
                    job.created_by or "system",
                    hostname=target_hostname,
                )
                if removed is None:
                    log(
                        f"[release] servers.{target_hostname} already absent from env config doc"
                    )
                else:
                    log(
                        f"[release] removed servers.{target_hostname} from config version {removed.version}"
                    )
                    result["config_version"] = removed.version
            except Exception as exc:  # noqa: BLE001 — cleanup must not mask the release
                log(f"[release] env config cleanup for {target_hostname} failed: {exc}")
        return result

    # Upper bound for a downloaded Talos factory image (2 GiB).
    MAX_TALOS_IMAGE_BYTES = MAX_TALOS_IMAGE_BYTES

    def _download_factory_image(
        self, url: str, log, dest_dir: str | Path, filename: str | None = None
    ) -> tuple[Path, str]:
        """Download a Talos factory image to disk; returns (path, sha256)."""
        return download_factory_image(
            url,
            log,
            dest_dir=dest_dir,
            filename=filename,
            max_bytes=self.MAX_TALOS_IMAGE_BYTES,
        )

    @staticmethod
    def _talos_image_name(image_url: str) -> str:
        """Derive a MAAS boot-resource name from the factory image URL."""
        import re
        from urllib.parse import urlparse

        stem = Path(urlparse(image_url).path).name
        # Strip known image/archive extensions, longest suffix first.
        for ext in (
            ".tar.gz",
            ".raw.xz",
            ".tgz",
            ".raw",
            ".qcow2",
            ".img",
            ".xz",
            ".gz",
        ):
            if stem.endswith(ext):
                stem = stem[: -len(ext)]
                break
        stem = re.sub(r"[^a-z0-9._-]+", "-", stem.lower()).strip("-.")
        if not stem:
            stem = "talos-genestack"
        if not stem.startswith("talos"):
            stem = f"talos-{stem}"
        return stem

    def _maas_talos_image_upload(
        self,
        job: Job,
        env: Environment | None,
        params: dict[str, Any],
        log,
        dry: bool,
    ) -> dict[str, Any]:
        """Download a Talos factory image and upload it to the env's MAAS."""
        from urllib.parse import urlparse

        op_id = "maas.talos.image_upload"
        if env is None:
            return {
                "ok": False,
                "error": f"{op_id} requires an environment",
                "returncode": 2,
            }

        image_url = str(params.get("image_url") or "").strip()
        if not image_url:
            # Default from the env config doc talos.image_url (zero-touch chain).
            from app.services import envconfig as envconfig_service

            current = envconfig_service.get_current(self.db, env)
            if current is not None:
                talos = current[0].get("talos")
                if isinstance(talos, dict):
                    image_url = str(talos.get("image_url") or "").strip()
        if not image_url:
            return {
                "ok": False,
                "error": (
                    f"{op_id}: image_url is required "
                    "(param or env config doc talos.image_url)"
                ),
                "returncode": 2,
            }
        if urlparse(image_url).scheme != "https":
            return {
                "ok": False,
                "error": f"{op_id}: image_url must be an https:// URL",
                "returncode": 2,
            }
        name = self._talos_image_name(image_url)
        # Download to disk under the job's workspace so the 2 GiB factory
        # image never sits in process memory; uploaded by file handle.
        download_dir = Path(get_settings().data_dir) / "jobs" / job.id / "images"

        if dry:
            log(
                f"[dry-run] would download {image_url} as name={name}"
            )
            return {
                "ok": True,
                "dry_run": True,
                "image_url": image_url,
                "image": name,
                "message": f"[dry-run] would upload {name} from {image_url}",
            }

        try:
            image_path, sha256 = self._download_factory_image(
                image_url, log, download_dir
            )
        except (MaasDownloadError, OSError) as exc:
            log(f"[talos-image] {exc}")
            return {"ok": False, "error": str(exc), "returncode": 2}

        from app.services.maas import MaasError

        client = self._maas_client(env)
        is_mock = client.mock
        try:
            record = client.upload_image(
                name, image_path, title=f"Talos factory image ({env.name})"
            )
        except MaasError as exc:
            log(f"[talos-image] upload name={name} failed: {exc}")
            return {"ok": False, "error": str(exc), "image": name, "returncode": 2}
        finally:
            client.close()

        size = image_path.stat().st_size
        image_path.unlink(missing_ok=True)
        log(
            f"[talos-image] uploaded name={name} bytes={size} sha256={sha256} mock={is_mock}"
        )
        self.write_audit(
            actor=job.created_by or "system",
            action="env.talos_image_upload",
            resource_type="environment",
            resource_id=env.id,
            environment_id=env.id,
            details={
                "image": name,
                "image_url": image_url,
                "bytes": size,
                "sha256": sha256,
                "mock": is_mock,
                "dry_run": dry,
            },
            success=True,
        )
        return {
            "ok": True,
            "dry_run": False,
            "mock": is_mock,
            "image": name,
            "image_url": image_url,
            "bytes": size,
            "sha256": sha256,
            "record": record,
            "message": f"fetched {name} ({size} bytes)",
        }

    def _state_export_remote(
        self,
        env: Environment,
        job: Job,
        params: dict[str, Any],  # noqa: ARG002 — reserved; state.export takes no params
        ctx: EnvContext,
        log,
        dry: bool,
        timeout: int,
        deadline: float | None,
        executor,
        remote: str,
    ) -> dict[str, Any]:
        """genestack.state.export when the deploy host owns the checkout.

        Same flow as the local path (render -> ship files -> git add/commit/push
        -> audit) but every git invocation and file write runs on the deploy
        host through the executor — a connected agent takes precedence, else
        ssh. No local fcntl lock is taken: the lockfile and .git live remotely.
        """
        import subprocess

        from app.services import envconfig as envconfig_service
        from app.services.envconfig import _redacting_log

        env_name = env.name
        repo_root = Path(env.state_repo_path).expanduser()
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        state_dir = repo_root / "state" / env_name

        def audit(details: dict[str, Any], success: bool = True) -> None:
            self.write_audit(
                actor=job.created_by or "system",
                action="env.state.export",
                resource_type="environment",
                resource_id=env.id,
                environment_id=env.id,
                details=details,
                success=success,
            )

        current = envconfig_service.get_current(self.db, env)
        if current is None:
            msg = (
                "Cannot export state: no config document exists for environment '{name}'. "
                "Store a config first with PUT /api/v1/environments/{env_id}/config, "
                "then re-run this job."
            ).format(name=env_name, env_id=env.id)
            log(f"[state.export] {msg}")
            return {"ok": False, "error": msg, "returncode": 2}
        doc, row = current
        log(
            f"[state.export] Rendering config version {row.version} for environment '{env_name}'..."
        )
        try:
            files = envconfig_service.render_to_files(
                doc, env, self.settings, include_secrets=False
            )
        except envconfig_service.ConfigValidationError as exc:
            log(f"[state.export] Configuration error: {exc}")
            return {
                "ok": False,
                "error": f"Config validation error in version {row.version}: {exc}",
                "returncode": 2,
                "version": row.version,
            }
        excluded_secrets = (
            1 if isinstance(doc.get("secrets"), dict) and doc.get("secrets") else 0
        ) + (1 if env.ssh_private_key_encrypted else 0)
        if excluded_secrets:
            log(
                f"[state.export] excluded {excluded_secrets} secret file(s) from state export "
                "(kubesecrets.yaml / .ssh — secrets stay in the DB)"
            )
        log(
            f"[state.export] Rendered {len(files)} file(s) from config version {row.version}"
        )

        file_sizes = {
            path: len(content.encode("utf-8")) for path, content in files.items()
        }
        total_bytes = sum(file_sizes.values())
        commit_msg = f"state({env_name}): export config version {row.version}"
        via = "agent" if executor.agent_env_id else "ssh"

        def git(*argv: str) -> dict[str, Any]:
            result = bridge.run_command(
                list(argv),
                cwd=repo_root,
                timeout=clamp_deadline_timeout(timeout, deadline),
                dry_run=False,
                ssh_target=executor.ssh_target,
                remote_env=ctx.remote_env(),
                agent_env_id=executor.agent_env_id,
                log=log,
            )
            rc = result.get("returncode")
            if rc not in (0, None):
                err = (result.get("stderr") or result.get("error") or "").strip()
                raise RuntimeError(err or f"git {' '.join(argv[:2])} failed")
            return result

        def ship_file(relpath: str, target: Path, data: bytes) -> None:
            b64 = base64.b64encode(data).decode("ascii")
            backup = state_dir / ".console-backup" / ts / relpath
            t = shlex.quote(str(target))
            b = shlex.quote(str(backup))
            script = (
                f"mkdir -p {shlex.quote(str(target.parent))} && "
                f"if [ -f {t} ]; then mkdir -p {shlex.quote(str(backup.parent))} && cp -n {t} {b}; fi && "
                f"echo {shlex.quote(b64)} | base64 -d > {t} && chmod 0644 {t}"
            )
            result = bridge.run_command(
                ["bash", "-c", script],
                timeout=clamp_deadline_timeout(timeout, deadline),
                dry_run=False,
                ssh_target=executor.ssh_target,
                remote_env=ctx.remote_env(),
                agent_env_id=executor.agent_env_id,
                log=_redacting_log(log, b64),
            )
            if result.get("returncode") not in (0, None):
                raise RuntimeError(
                    f"failed to write {relpath} on deploy host: "
                    f"{result.get('message', 'unknown error')}"
                )

        if dry:
            # Rehearsal: preflight the remote checkout the same way the live
            # path would (reachable, is a git repo, on a branch), then preview
            # the writes/commit/push. A preflight failure is an honest
            # "will fail" signal, not a green rehearsal.
            probe = bridge.run_command(
                [
                    "bash",
                    "-c",
                    f"test -d {shlex.quote(str(repo_root / '.git'))} && "
                    f"git -C {shlex.quote(str(repo_root))} symbolic-ref -q HEAD",
                ],
                timeout=clamp_deadline_timeout(timeout, deadline),
                dry_run=False,
                ssh_target=executor.ssh_target,
                remote_env=ctx.remote_env(),
                agent_env_id=executor.agent_env_id,
                log=log,
            )
            if probe.get("returncode") not in (0, None):
                err = (probe.get("stderr") or probe.get("message") or "").strip()
                log(f"[state.export] remote preflight failed: {err}")
                return {
                    "ok": False,
                    "dry_run": True,
                    "error": (
                        f"state repo '{repo_root}' is not a usable git checkout on the "
                        f"deploy host (missing .git or detached HEAD) — {err}"
                    ),
                    "returncode": 2,
                }
            branch = probe.get("stdout", "").strip()
            log(f"[state.export] git: on branch '{branch or '-'}'")

            for relpath in sorted(files):
                log(
                    f"[state.export] dry-run: would write {state_dir / relpath} ({file_sizes[relpath]} bytes)"
                )
            log(f"[state.export] dry-run: would commit '{commit_msg}'")
            if remote:
                log("[state.export] dry-run: would push to remote (redacted)")
            else:
                log("[state.export] dry-run: no remote configured, commit only")
            audit(
                {
                    "version": row.version,
                    "files": len(files),
                    "bytes": total_bytes,
                    "commit": None,
                    "pushed": False,
                    "dry_run": True,
                    "remote_host": via,
                }
            )
            return {
                "ok": True,
                "dry_run": True,
                "version": row.version,
                "files": len(files),
                "bytes": total_bytes,
                "state_dir": str(state_dir),
            }

        try:
            for relpath, content in files.items():
                target = state_dir / relpath
                log(f"[state.export] shipping {relpath} ({file_sizes[relpath]} bytes)")
                ship_file(relpath, target, content.encode("utf-8"))

            branch = git("git", "rev-parse", "--abbrev-ref", "HEAD")["stdout"].strip()
            if branch == "HEAD":
                log(
                    f"[state.export] state repo '{repo_root}' is in detached HEAD state"
                )
                return {
                    "ok": False,
                    "error": (
                        f"state repo '{repo_root}' is in detached HEAD state — check out a branch "
                        "(e.g. 'git checkout main') and re-run"
                    ),
                    "returncode": 2,
                }
            log(f"[state.export] git: on branch '{branch}'")

            git("git", "add", "--", f"state/{env_name}")
            diff = bridge.run_command(
                ["git", "diff", "--cached", "--quiet", "--", f"state/{env_name}"],
                cwd=repo_root,
                timeout=clamp_deadline_timeout(timeout, deadline),
                dry_run=False,
                ssh_target=executor.ssh_target,
                remote_env=ctx.remote_env(),
                agent_env_id=executor.agent_env_id,
                log=log,
            )
            if diff.get("returncode") == 0:
                log("[state.export] no changes staged, skipping commit")
                audit(
                    {
                        "version": row.version,
                        "files": len(files),
                        "bytes": total_bytes,
                        "commit": None,
                        "pushed": False,
                        "dry_run": False,
                        "remote_host": via,
                    }
                )
                return {
                    "ok": True,
                    "dry_run": False,
                    "version": row.version,
                    "files": len(files),
                    "bytes": total_bytes,
                    "state_dir": str(state_dir),
                    "committed": False,
                    "commit": None,
                    "pushed": False,
                    "remote": remote or None,
                }

            git(
                "git",
                "-c",
                "user.name=genestack-console",
                "-c",
                "user.email=console@localhost",
                "commit",
                "-m",
                commit_msg,
                "--",
                f"state/{env_name}",
            )
            sha = git("git", "rev-parse", "HEAD")["stdout"].strip()
            log(f"[state.export] committed {sha} ({commit_msg})")

            pushed = False
            if remote:
                push = bridge.run_command(
                    ["git", "push", remote, branch],
                    cwd=repo_root,
                    timeout=clamp_deadline_timeout(timeout, deadline),
                    dry_run=False,
                    ssh_target=executor.ssh_target,
                    remote_env=ctx.remote_env(),
                    agent_env_id=executor.agent_env_id,
                    log=_redacting_log(log, remote),
                )
                if push.get("returncode") not in (0, None):
                    err = (push.get("stderr") or push.get("message") or "").strip()
                    log(f"[state.export] git push failed: {err}")
                    audit(
                        {
                            "version": row.version,
                            "files": len(files),
                            "bytes": total_bytes,
                            "commit": sha,
                            "pushed": False,
                            "dry_run": False,
                            "remote_host": via,
                        },
                        success=False,
                    )
                    return {
                        "ok": False,
                        "error": f"git push failed: {err}",
                        "returncode": push.get("returncode") or 1,
                        "version": row.version,
                        "committed": True,
                        "commit": sha,
                        "pushed": False,
                        "remote": remote,
                    }
                pushed = True
                log(f"[state.export] pushed {branch} to remote (redacted)")
            else:
                log("[state.export] no remote configured, commit only")

            audit(
                {
                    "version": row.version,
                    "files": len(files),
                    "bytes": total_bytes,
                    "commit": sha,
                    "pushed": pushed,
                    "dry_run": False,
                    "remote_host": via,
                }
            )
            return {
                "ok": True,
                "dry_run": False,
                "version": row.version,
                "files": len(files),
                "bytes": total_bytes,
                "state_dir": str(state_dir),
                "committed": True,
                "commit": sha,
                "pushed": pushed,
                "remote": remote or None,
            }
        except (subprocess.TimeoutExpired, OSError, RuntimeError) as exc:
            log(f"[state.export] git error: {exc}")
            audit(
                {
                    "version": row.version,
                    "files": len(files),
                    "bytes": total_bytes,
                    "commit": None,
                    "pushed": False,
                    "dry_run": dry,
                    "remote_host": via,
                },
                success=False,
            )
            return {"ok": False, "error": str(exc), "returncode": 1}

    def _dispatch(
        self,
        op: OperationSpec,
        job: Job,
        env: Environment | None,
        log,
        ctx: EnvContext,
        params: dict[str, Any] | None = None,
        deadline: float | None = None,
        check_cancel: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        handler = op.handler
        # Execution params come from run_job (secrets restored in memory);
        # the row's scrubbed copy is only a fallback for direct callers.
        params = params if params is not None else (job.params or {})
        dry = ctx.dry_run
        timeout = effective_timeout_seconds(op, self.settings)
        gs_root = ctx.genestack_root
        ans_root = bridge.resolve_ansible_root(self.settings)
        extra_env = ctx.subprocess_env()
        ssh_target = ctx.ssh_target
        remote_env = ctx.remote_env()
        if env is not None:
            # Doc-derived env (GATEWAY_DOMAIN from network.gateway_domain) for
            # genestack install scripts, both local and remote execution.
            from app.services import envconfig as envconfig_service

            current = envconfig_service.get_current(self.db, env)
            if current is not None:
                doc_vars = envconfig_service.doc_env(current[0])
                extra_env.update(doc_vars)
                remote_env.update(doc_vars)

        # Executor preference per env: connected agent -> ssh deploy host ->
        # local. Centralized so pipeline stages, enable_service, deploy, and
        # the config push all route the same way (app/services/executors.py).
        from app.services.executors import pick_executor

        executor = pick_executor(env, ctx, self.settings)
        agent_env_id = executor.agent_env_id
        if executor.kind == "agent":
            log(f"[agent] executor: commands run via agent for env {env.id}")
        elif ctx.is_remote:
            log(f"[ssh] deploy host: {ssh_target}")

        common = dict(
            handler=handler,
            op=op,
            job=job,
            env=env,
            log=log,
            ctx=ctx,
            params=params,
            deadline=deadline,
            check_cancel=check_cancel,
            dry=dry,
            timeout=timeout,
            gs_root=gs_root,
            ans_root=ans_root,
            extra_env=extra_env,
            ssh_target=ssh_target,
            remote_env=remote_env,
            executor=executor,
            agent_env_id=agent_env_id,
        )
        # Imported here so app.modules can load while this module is still
        # importing the catalog. The function takes this runner as self.
        from app.modules import handler_map

        fn = handler_map().get(handler)
        if fn is None:
            raise RuntimeError(f"No dispatcher for handler: {handler}")
        return fn(self, **common)


def execute_operation(
    db: Session,
    *,
    operation: str,
    params: dict[str, Any] | None = None,
    environment_id: str | None = None,
    created_by: str | None = None,
    run_sync: bool = False,
    secret_params: dict[str, str] | None = None,
) -> Job:
    """Create a job; run it inline only when run_sync=True.

    Default is queued-by-default: the job stays queued for the worker
    (app.worker.runner) so long-running deploy work never blocks the API.
    Submission-time per-env mutating lock: a second mutating job for the
    same environment is rejected with ConflictError while one is still
    queued/running.

    ``secret_params`` carries already-encrypted secret values from a source
    job (retry) so the new job can execute even though its params were
    scrubbed to SECRET_PARAM_MASK.
    """
    op = get_operation(operation)
    if op is not None and op.mutating and environment_id:
        # Fast pre-check for a clean ConflictError message; the authoritative
        # guard is the atomic env_mutexes INSERT below (this SELECT-then-act
        # is racy on its own).
        conflict = find_mutating_conflict(db, environment_id)
        if conflict is not None:
            raise ConflictError(
                f"Environment already has a mutating job {conflict.status.value}: "
                f"{conflict.id} ({conflict.operation})",
                job_id=conflict.id,
            )
    runner = JobRunner(db)
    job = runner.create_job(
        operation=operation,
        params=params,
        environment_id=environment_id,
        created_by=created_by,
        secret_params=secret_params,
    )
    if op is not None and op.mutating and environment_id:
        acquire_env_mutex(db, environment_id, job.id, job_status=job.status.value)
    db.commit()
    db.refresh(job)
    if run_sync:
        job = runner.run_job(job)
    return job


def scrub_stored_job_secrets(db: Session) -> int:
    """Scrub secret param values from historical job rows and audit entries.

    One-time migration for rows persisted before secret params were scrubbed
    at rest: any catalog-known secret param still stored in plaintext in
    ``jobs.params`` is moved (fernet-encrypted) into ``jobs.secret_params``
    and replaced with SECRET_PARAM_MASK, so the row — and any still-queued
    job — keeps working. ``audit_logs.details['params']`` copies are masked
    in place (audit never needs the real value). Idempotent; returns the
    number of rows changed. Caller owns the session.
    """
    known = all_secret_param_names()
    changed = 0
    for job in db.scalars(select(Job)).all():
        params = job.params
        if not isinstance(params, dict):
            continue
        hits = [
            name
            for name in known
            if isinstance(params.get(name), str)
            and params[name]
            and params[name] != SECRET_PARAM_MASK
        ]
        if not hits:
            continue
        scrubbed = dict(params)
        secrets = dict(job.secret_params or {})
        for name in hits:
            if name not in secrets:
                secrets[name] = encrypt_secret(params[name])
            scrubbed[name] = SECRET_PARAM_MASK
        job.params = scrubbed
        job.secret_params = secrets
        db.add(job)
        changed += 1
    for entry in db.scalars(select(AuditLog)).all():
        details = entry.details
        if not isinstance(details, dict):
            continue
        params = details.get("params")
        if not isinstance(params, dict):
            continue
        hits = [
            name
            for name in known
            if isinstance(params.get(name), str)
            and params[name]
            and params[name] != SECRET_PARAM_MASK
        ]
        if not hits:
            continue
        masked = {**params, **{name: SECRET_PARAM_MASK for name in hits}}
        entry.details = {**details, "params": masked}
        db.add(entry)
        changed += 1
    if changed:
        db.commit()
    return changed


def _as_utc(dt: datetime) -> datetime:
    """SQLite drops tzinfo; treat naive datetimes as UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def recover_abandoned_running_jobs(
    db: Session, settings: Settings | None = None
) -> int:
    """Fail jobs left ``running`` by a previous worker process.

    A new worker cannot inherit another process's helm/deploy child. API
    restart must not call this — only the job worker, once at daemon start —
    or a live deploy would be marked failed while still running.
    """
    settings = settings or get_settings()
    runner = JobRunner(db, settings)
    now = _utcnow()
    recovered = 0
    for job in db.scalars(select(Job).where(Job.status == JobStatus.running)).all():
        previous = job.status.value
        job.status = JobStatus.failed
        job.error = ABANDONED_ERROR
        job.finished_at = now
        runner.append_log(job, ABANDONED_ERROR)
        runner.write_audit(
            actor="system",
            action="job.abandoned",
            resource_type="job",
            resource_id=job.id,
            environment_id=job.environment_id,
            details={
                "operation": job.operation,
                "previous_status": previous,
            },
            success=False,
        )
        runner.flush_logs(job)
        if job.environment_id:
            release_env_mutex(db, job.environment_id, job.id)
        recovered += 1
    if recovered:
        db.commit()
    return recovered


def recover_stale_jobs(db: Session, settings: Settings | None = None) -> int:
    """Fail *running* jobs past their deadline; leave queued jobs alone.

    Queued jobs must not expire on wait age (``created_at``). Only jobs in
    ``running`` whose ``started_at`` is older than their effective timeout
    are marked failed with ``RECOVERY_ERROR`` and audited. Timeout source
    matches live job deadlines via ``effective_timeout_seconds`` (op
    timeout clamped to ``MAX_JOB_TIMEOUT_SECONDS``, else
    ``settings.job_timeout_seconds``).

    Safe to call from API lifespan and worker startup. Does **not** abandon
    fresh in-flight work — that is ``recover_abandoned_running_jobs``, which
    the job worker calls once at process start only.
    """
    settings = settings or get_settings()
    runner = JobRunner(db, settings)
    now = _utcnow()
    stmt = select(Job).where(Job.status == JobStatus.running)
    recovered = 0
    for job in db.scalars(stmt).all():
        op = get_operation(job.operation)
        timeout = effective_timeout_seconds(op, settings)
        ref = job.started_at
        if ref is None:
            # Running without started_at is anomalous; do not fall back to
            # created_at (that would punish wait time, same as the queued bug).
            continue
        age = (now - _as_utc(ref)).total_seconds()
        if age <= timeout:
            continue
        previous = job.status.value
        job.status = JobStatus.failed
        job.error = RECOVERY_ERROR
        job.finished_at = now
        runner.append_log(job, RECOVERY_ERROR)
        runner.write_audit(
            actor="system",
            action="job.recovered",
            resource_type="job",
            resource_id=job.id,
            environment_id=job.environment_id,
            details={
                "operation": job.operation,
                "previous_status": previous,
                "age_seconds": int(age),
                "timeout_seconds": timeout,
            },
            success=False,
        )
        # One runner serves every recovered job here, so flush each job's
        # buffered lines individually to keep the per-job counters correct.
        runner.flush_logs(job)
        if job.environment_id:
            release_env_mutex(db, job.environment_id, job.id)
        recovered += 1
    # Sweep env mutex rows whose holder no longer owns the lock: the jobs
    # marked terminal above, or orphaned by a crash between the terminal
    # commit and the release. Committed together with the recovery.
    swept = 0
    for mutex in db.scalars(select(EnvMutex)).all():
        holder = db.get(Job, mutex.job_id)
        if holder is None or holder.status in (JobStatus.success, JobStatus.failed):
            db.delete(mutex)
            swept += 1
    if recovered or swept:
        db.commit()
    return recovered
