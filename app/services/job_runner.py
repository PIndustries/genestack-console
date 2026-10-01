"""Job runner — executes catalog operations, sync (in-process) or via worker.

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
from app.services import cluster as cluster_probe
from app.services import hypervisor as hypervisor_service
from app.services import openstack_ops
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
from app.services.service_registry import (
    PIPELINE_STAGES,
    build_service_registry,
    filter_stage_items,
    get_pipeline_stage,
)

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
    PXE sidecar asset fetch (app/services/pxe.py).
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
    """Execute catalog operations, append logs, write audit entries."""

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
                f"[dry-run] would download {image_url} and upload to MAAS "
                f"boot-resources as name={name}"
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
            "message": f"uploaded {name} ({size} bytes) to MAAS",
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

        if handler == "internal_health":
            log("internal.health")
            return {
                "ok": True,
                "message": "healthy",
                "dry_run": dry,
                "genestack_root": str(gs_root),
                "ansible_root": str(ans_root),
            }

        if handler == "maas_machines_list":
            url, key = self._maas_creds(env)
            try:
                client = self._maas_client(env)
                machines = client.list_machines()
                is_mock = client.mock
                client.close()
                log(f"[maas] listed {len(machines)} machines mock={is_mock}")
                return {
                    "ok": True,
                    "mock": is_mock,
                    "maas_configured": client.configured,
                    "dry_run": dry,
                    "machines": machines,
                    "count": len(machines),
                    "message": f"{len(machines)} machines",
                }
            except Exception as exc:  # noqa: BLE001
                log(f"[maas] MaasClient failed ({exc}); falling back to bridge")
                return bridge.maas_list_machines(url, key, dry_run=dry, log=log)

        if handler == "maas_machine_power_status":
            url, key = self._maas_creds(env)
            system_id = str(params.get("system_id", ""))
            try:
                client = self._maas_client(env)
                status = client.power_status(system_id)
                client.close()
                log(f"[maas] power_status system_id={system_id} -> {status}")
                return {"ok": True, "dry_run": dry, **status}
            except Exception as exc:  # noqa: BLE001
                log(f"[maas] power status via client failed ({exc}); bridge fallback")
                return bridge.maas_power_status(
                    url, key, system_id, dry_run=dry, log=log
                )

        if handler in (
            "maas_machine_commission",
            "maas_machine_deploy",
            "maas_machine_release",
        ):
            return self._maas_machine_action(handler, job, env, params, log, dry)

        if handler == "maas_talos_image_upload":
            return self._maas_talos_image_upload(job, env, params, log, dry)

        if handler == "openstack_servers_list":
            if env is None:
                return {
                    "ok": False,
                    "error": "openstack.servers.list requires an environment",
                    "returncode": 2,
                }
            result = openstack_ops.list_servers(env, self.settings, log=log)
            vms = result.get("vms") or []
            ok = result.get("source") in ("live", "openstack-api")
            log(
                f"[openstack] servers source={result.get('source')} "
                f"count={len(vms)} error={result.get('error')}"
            )
            return {
                "ok": ok,
                "source": result.get("source"),
                "vms": vms,
                "count": len(vms),
                "error": result.get("error"),
                "returncode": 0 if ok else 1,
                "message": (
                    f"{len(vms)} servers"
                    if ok
                    else f"openstack unavailable: {result.get('error')}"
                ),
            }

        if handler in (
            "openstack_server_start",
            "openstack_server_stop",
            "openstack_server_reboot",
            "openstack_server_delete",
        ):
            if env is None:
                return {
                    "ok": False,
                    "error": f"{op.id} requires an environment",
                    "returncode": 2,
                }
            action = handler.removeprefix("openstack_server_")
            server_id = str(params.get("server_id", ""))
            return openstack_ops.server_action(
                env,
                self.settings,
                action,
                server_id,
                dry_run=dry,
                timeout=timeout,
                log=log,
            )

        if handler == "host_preflight":
            return bridge.run_playbook(
                "host_preflight.yml",
                ansible_root=ans_root,
                genestack_root=gs_root,
                limit=params.get("limit"),
                extra_vars=(
                    params.get("extra_vars")
                    if isinstance(params.get("extra_vars"), dict)
                    else None
                ),
                dry_run=dry,
                timeout=timeout,
                extra_env=extra_env,
                ssh_target=ssh_target,
                remote_env=remote_env,
                log=log,
            )

        if handler == "host_basic_ops":
            action = params.get("action")
            extra = (
                params.get("extra_vars")
                if isinstance(params.get("extra_vars"), dict)
                else {}
            )
            extra = {**extra, "action": action}
            return bridge.run_playbook(
                "basic_ops.yml",
                ansible_root=ans_root,
                genestack_root=gs_root,
                limit=params.get("limit"),
                extra_vars=extra,
                dry_run=dry,
                timeout=timeout,
                extra_env=extra_env,
                ssh_target=ssh_target,
                remote_env=remote_env,
                log=log,
            )

        if handler == "genestack_components_desired":
            path, scope = bridge.resolve_components_path(self.settings, env)
            result = bridge.read_components_desired(path)
            log(
                f"components path={result.get('path')} exists={result.get('exists')} scope={scope}"
            )
            result["ok"] = True
            result["scope"] = scope
            result["message"] = (
                "components loaded"
                if result.get("exists")
                else "components file missing"
            )
            return result

        if handler == "genestack_components_reconcile":
            if env is None:
                return {
                    "ok": False,
                    "error": "genestack.components.reconcile requires an environment",
                    "returncode": 2,
                }
            from app.services import reconcile as reconcile_service

            return reconcile_service.run_reconcile(
                self.db,
                env,
                self.settings,
                apply=bool(params.get("apply", False)),
                log=log,
                timeout=timeout,
            )

        if handler == "genestack_service_enable":
            service = str(params.get("service", ""))
            return bridge.enable_service(
                service,
                gs_root,
                dry_run=dry,
                timeout=timeout,
                extra_env=extra_env,
                ssh_target=ssh_target,
                remote_env=remote_env,
                agent_env_id=agent_env_id,
                log=log,
            )

        if handler == "genestack_scripts_list":
            scripts = bridge.list_install_scripts(gs_root)
            log(f"found {len(scripts)} install scripts under {gs_root / 'bin'}")
            return {
                "ok": True,
                "scripts": scripts,
                "count": len(scripts),
                "genestack_root": str(gs_root),
                "message": f"{len(scripts)} scripts",
            }

        if handler == "genestack_repo_scripts_list":
            from app.services import repo_scripts

            inventory = repo_scripts.list_repo_scripts(gs_root)
            counts = inventory["counts"]
            log(
                f"repo scripts under {gs_root}: scripts={counts['scripts']} "
                f"maintenances={counts['maintenances']} ops_tools={counts['ops_tools']}"
            )
            return {
                "ok": True,
                **inventory,
                "count": sum(counts.values()),
                "message": (
                    f"{counts['scripts']} scripts, {counts['maintenances']} "
                    f"maintenances, {counts['ops_tools']} ops tools"
                ),
            }

        if handler == "genestack_repo_script_run":
            from app.services import repo_scripts

            script = str(params.get("script", "")).strip()
            name = Path(script).name
            if not name or name != script:
                msg = f"Invalid script name '{script}' — basename only (no path separators)"
                log(f"[denied] {msg}")
                return {"ok": False, "error": msg, "returncode": 2}
            discovered = {
                s["name"] for s in repo_scripts.list_repo_scripts(gs_root)["scripts"]
            }
            if name not in discovered or name not in repo_scripts.SAFE_REPO_SCRIPTS:
                allowed = ", ".join(sorted(repo_scripts.SAFE_REPO_SCRIPTS))
                msg = (
                    f"Script '{name}' is not runnable: it must exist under "
                    f"{gs_root / 'scripts'} and be in the allowlist: {allowed}"
                )
                log(f"[denied] {msg}")
                return {"ok": False, "error": msg, "returncode": 2}

            args_str = str(params.get("args") or "").strip()
            argv = ["bash", f"scripts/{name}"]
            if args_str:
                argv.extend(shlex.split(args_str))
            log(
                f"[repo-script] running scripts/{name} args={args_str or '-'} dry_run={dry}"
            )
            result = bridge.run_command(
                argv,
                cwd=gs_root,
                timeout=timeout,
                dry_run=dry,
                extra_env=extra_env,
                ssh_target=ssh_target,
                remote_env=remote_env,
                agent_env_id=agent_env_id,
                log=log,
            )
            rc = result.get("returncode")
            ok = bool(result.get("dry_run")) or rc == 0
            self.write_audit(
                actor=job.created_by or "system",
                action="env.repo_script_run",
                resource_type="environment" if env else "genestack_root",
                resource_id=env.id if env else str(gs_root),
                environment_id=env.id if env else None,
                details={
                    "script": f"scripts/{name}",
                    "args": args_str or None,
                    "returncode": rc,
                    "dry_run": dry,
                },
                success=ok,
            )
            return {
                "ok": ok,
                "script": name,
                "args": args_str or None,
                "returncode": rc,
                "dry_run": bool(result.get("dry_run", dry)),
                "message": (
                    f"repo script {name} completed"
                    if ok
                    else f"repo script {name} failed (rc={rc}) — see log"
                ),
            }

        if handler == "genestack_smoke":
            result = bridge.smoke_check(gs_root, ans_root)
            for c in result.get("checks", []):
                log(f"check {c['name']}: ok={c['ok']} {c.get('detail', '')}")
            result["message"] = "smoke ok" if result.get("ok") else "smoke failed"
            return result

        if handler == "ansible_playbook_run":
            playbook = str(params.get("playbook", ""))
            return bridge.run_playbook(
                playbook,
                ansible_root=ans_root,
                genestack_root=gs_root,
                limit=params.get("limit"),
                extra_vars=(
                    params.get("extra_vars")
                    if isinstance(params.get("extra_vars"), dict)
                    else None
                ),
                tags=params.get("tags"),
                dry_run=dry,
                timeout=timeout,
                extra_env=extra_env,
                ssh_target=ssh_target,
                remote_env=remote_env,
                log=log,
            )

        if handler == "genestack_host_prepare":
            if env is None:
                return {
                    "ok": False,
                    "error": "genestack.host_prepare requires an environment",
                    "returncode": 2,
                }
            from app.services import host_prepare as host_prepare_service

            result = host_prepare_service.run_host_prepare(
                env,
                ctx,
                log,
                params=params,
                dry_run=dry,
                timeout=timeout,
                extra_env=extra_env,
                ssh_target=ssh_target,
                remote_env=remote_env,
                agent_env_id=agent_env_id,
            )
            self.write_audit(
                actor=job.created_by or "system",
                action="env.host_prepare",
                resource_type="environment",
                resource_id=env.id,
                environment_id=env.id,
                details={
                    "repo_url": result.get("repo_url"),
                    "repo_ref": result.get("repo_ref"),
                    "genestack_path": result.get("genestack_path"),
                    "config_dir": result.get("config_dir"),
                    "steps_completed": len(result.get("steps") or []),
                    "dry_run": dry,
                },
                success=bool(result.get("ok")),
            )
            return result

        if handler == "genestack_host_setup":
            # Reuse core Genestack ansible/playbooks/host-setup.yml (same as setup-hosts.sh)
            check = params.get("check")
            check_mode = check is True or str(check).lower() in ("1", "true", "yes")
            inv = None
            if env and env.metadata and isinstance(env.metadata, dict):
                inv = env.metadata.get("ansible_inventory")
            result = bridge.run_playbook(
                "host-setup.yml",
                ansible_root=ans_root,
                genestack_root=gs_root,
                limit=params.get("limit"),
                inventory_path=inv,
                check=check_mode,
                dry_run=dry,
                timeout=timeout,
                extra_env=extra_env,
                ssh_target=ssh_target,
                remote_env=remote_env,
                log=log,
            )
            result["message"] = result.get("message") or "genestack host-setup"
            return result

        if handler == "genestack_services_list":
            registry = build_service_registry(gs_root)
            services = registry["services"]
            categories: dict[str, int] = {}
            for svc in services:
                categories[svc["category"]] = categories.get(svc["category"], 0) + 1
            log(f"registry: {len(services)} services, categories={categories}")
            return {
                "ok": True,
                "count": len(services),
                "categories": categories,
                "services": [svc["name"] for svc in services],
                "genestack_root": registry["genestack_root"],
                "message": f"{len(services)} services",
            }

        if handler == "genestack_cluster_status":
            result = cluster_probe.cluster_status(ctx.kubeconfig)
            log(
                f"cluster reachable={result.get('reachable')} "
                f"nodes={len(result.get('nodes') or [])} error={result.get('error')}"
            )
            result["ok"] = True
            result["message"] = (
                "cluster reachable"
                if result.get("reachable")
                else f"cluster unreachable: {result.get('error')}"
            )
            return result

        if handler == "genestack_pipeline_run":
            stage_id = str(params.get("stage", "")).strip()
            stage = get_pipeline_stage(stage_id)
            if stage is None:
                valid = ", ".join(s["id"] for s in PIPELINE_STAGES)
                msg = f"Unknown pipeline stage '{stage_id}'. Valid stages: {valid}"
                log(f"[denied] {msg}")
                return {"ok": False, "error": msg, "returncode": 2}

            # Component filter: drop services the env's config doc disables.
            components: dict[str, Any] | None = None
            if env is not None:
                from app.services import envconfig as envconfig_service

                current = envconfig_service.get_current(self.db, env)
                if current is not None and isinstance(
                    current[0].get("components"), dict
                ):
                    components = current[0]["components"]
            items = filter_stage_items(stage, components, log)
            if not items:
                log(
                    f"[pipeline] stage {stage['id']}: all items disabled in config doc — "
                    "marked complete"
                )
                return {
                    "ok": True,
                    "stage": stage["id"],
                    "results": [],
                    "count": 0,
                    "returncode": 0,
                    "message": f"stage {stage['id']}: 0 enabled item(s), ok=True",
                }

            log(f"pipeline stage={stage['id']} items={len(items)} dry_run={dry}")
            results: list[dict[str, Any]] = []
            ok = True
            for item in items:
                if check_cancel is not None:
                    check_cancel()
                r = bridge.run_command(
                    ["bash", item["script"]],
                    cwd=gs_root,
                    timeout=clamp_deadline_timeout(timeout, deadline),
                    dry_run=dry,
                    extra_env=extra_env,
                    ssh_target=ssh_target,
                    remote_env=remote_env,
                    agent_env_id=agent_env_id,
                    log=log,
                )
                results.append(
                    {
                        "item": item["name"],
                        "type": item["type"],
                        "script": item["script"],
                        "dry_run": r.get("dry_run"),
                        "returncode": r.get("returncode"),
                        "message": r.get("message"),
                    }
                )
                if r.get("returncode") not in (0, None) and not r.get("dry_run"):
                    ok = False
                    log(
                        f"[pipeline] stopping at {item['name']} rc={r.get('returncode')}"
                    )
                    break
            return {
                "ok": ok,
                "stage": stage["id"],
                "results": results,
                "count": len(results),
                "returncode": 0 if ok else 1,
                "message": f"stage {stage['id']}: {len(results)} item(s), ok={ok}",
            }

        if handler == "genestack_verify":
            level = str(params.get("level") or "standard").strip().lower()
            if level not in ("quick", "standard", "full"):
                msg = (
                    f"Invalid verify level '{level}' — "
                    "must be one of: quick, standard, full"
                )
                log(f"[denied] {msg}")
                return {"ok": False, "error": msg, "returncode": 2, "level": level}
            log(f"[verify] running genestack test suite level={level} dry_run={dry}")
            result = bridge.run_command(
                ["bash", "scripts/tests/run-all-tests.sh", level],
                cwd=gs_root,
                timeout=timeout,
                dry_run=dry,
                extra_env=extra_env,
                ssh_target=ssh_target,
                remote_env=remote_env,
                agent_env_id=agent_env_id,
                log=log,
            )
            rc = result.get("returncode")
            ok = bool(result.get("dry_run")) or rc == 0
            message = (
                f"verify {level} passed"
                if ok
                else f"verify {level} failed (rc={rc}) — see log"
            )
            return {
                "ok": ok,
                "level": level,
                "returncode": rc,
                "dry_run": bool(result.get("dry_run", dry)),
                "message": message,
            }

        if handler == "genestack_tempest":
            action = str(params.get("action") or "install-run").strip().lower()
            if action not in ("install", "run", "install-run"):
                msg = (
                    f"Invalid tempest action '{action}' — "
                    "must be one of: install, run, install-run"
                )
                log(f"[denied] {msg}")
                return {"ok": False, "error": msg, "returncode": 2, "action": action}
            suite = str(params.get("suite") or "").strip()
            suite_helm_args: list[str] = []
            if suite and suite != "full":
                # The run_tests job executes /tmp/run-tests.sh, whose body is
                # literally {{ .Values.conf.script }}. The deployed
                # conf.script (base-helm-configs/tempest) is:
                #   tempest run --exclude-list /etc/tempest/test-blacklist \
                #       --config-file /etc/tempest/tempest.conf -w 4 --smoke
                # i.e. it only consumes the *blacklist* file plus --smoke.
                # The chart separately renders conf.whitelist into the
                # tempest-etc secret and mounts it at
                # /etc/tempest/test-whitelist (job-run-tests.yaml), but the
                # script never points --include-list at it — so setting
                # conf.whitelist ALONE would mount the file but run the full
                # --smoke set (a silent no-op). Scoping a suite therefore
                # needs BOTH chart values, passed to the install phase
                # (install-tempest.sh forwards extra args to helm):
                #   conf.whitelist[0]  -> creates + mounts the include file
                #   conf.script        -> tempest run --include-list
                #                          /etc/tempest/test-whitelist
                # --include-list is the modern tempest flag for the include
                # file (tempest/cmd/run.py: --whitelist-file is deprecated
                # and ignored when --include-list is present). The regex is
                # applied by stestr on top of the still-mounted blacklist.
                # `full` (or an empty suite) keeps the chart default:
                # blacklist + --smoke.
                suite_helm_args = [
                    "--set",
                    f"conf.whitelist[0]={suite}",
                    "--set",
                    "conf.script=tempest run --include-list /etc/tempest/test-whitelist --exclude-list /etc/tempest/test-blacklist --config-file /etc/tempest/tempest.conf -w 4",
                ]
                log(
                    f"[tempest] suite={suite}: install phase passes "
                    f"conf.whitelist[0]={suite} and rewrites conf.script to "
                    "tempest run --include-list /etc/tempest/test-whitelist "
                    "(the mounted include file the chart would otherwise ignore)"
                )
            elif suite == "full":
                log(
                    "[tempest] suite=full: chart default runs (test-blacklist + --smoke)"
                )
            log(f"[tempest] action={action} dry_run={dry}")

            phases: list[tuple[str, list[str]]] = []
            if action in ("install", "install-run"):
                # Deploys the openstack-helm tempest chart without running the
                # suite (install-tempest.sh sets manifests.job_run_tests=false).
                phases.append(
                    ("install", ["bash", "bin/install-tempest.sh", *suite_helm_args])
                )
            if action in ("run", "install-run"):
                # job_run_tests is a post-install helm hook, not a `helm test`
                # suite — `helm test tempest` reports TEST SUITE: None. Re-run
                # the install script with the hook enabled so helm waits on
                # the real tempest-run-tests job. Trailing --set wins.
                phases.append(
                    (
                        "test",
                        [
                            "bash",
                            "bin/install-tempest.sh",
                            *suite_helm_args,
                            "--set",
                            "manifests.job_run_tests=true",
                        ],
                    )
                )

            rc: int | None = 0
            ok = True
            message = f"tempest {action} completed"
            for phase, cmd in phases:
                result = bridge.run_command(
                    cmd,
                    cwd=gs_root,
                    timeout=timeout,
                    dry_run=dry,
                    extra_env=extra_env,
                    ssh_target=ssh_target,
                    remote_env=remote_env,
                    agent_env_id=agent_env_id,
                    log=log,
                )
                rc = result.get("returncode")
                ok = bool(result.get("dry_run")) or rc == 0
                if not ok:
                    message = f"tempest {phase} failed rc={rc}"
                    log(f"[tempest] {message}")
                    break
            self.write_audit(
                actor=job.created_by or "system",
                action="env.tempest",
                resource_type="environment",
                resource_id=env.id if env else None,
                environment_id=env.id if env else None,
                details={
                    "action": action,
                    "suite": suite or None,
                    "returncode": rc,
                    "dry_run": dry,
                },
                success=ok,
            )
            out: dict[str, Any] = {
                "ok": ok,
                "action": action,
                "returncode": rc,
                "dry_run": dry,
                "message": message,
            }
            if suite:
                out["suite"] = suite
            return out

        if handler == "genestack_k8s_upgrade":
            if env is None or ctx.config_dir is None:
                return {
                    "ok": False,
                    "error": (
                        "genestack.k8s_upgrade requires an environment with a "
                        "genestack_config_dir (inventory source)"
                    ),
                    "returncode": 2,
                }
            kubespray_dir = gs_root / "submodules" / "kubespray"
            if ssh_target is None and not kubespray_dir.is_dir():
                # Local execution only — remote runs fail on the deploy host
                # and surface the ansible rc instead.
                msg = (
                    f"kubespray submodule not found at {kubespray_dir} — "
                    "init it first (git submodule update --init submodules/kubespray)"
                )
                log(f"[missing] {msg}")
                return {"ok": False, "error": msg, "returncode": 2}
            kube_version = str(params.get("kube_version") or "").strip()
            # Source of truth for the target version: the inventory
            # group_vars k8s_cluster.kube_version file. When kube_version is
            # given the console rewrites that line before running the
            # playbook, with a .bak-<ts> copy of the original next to it
            # (envconfig push backup style).
            group_vars_file = (
                ctx.config_dir
                / "inventory"
                / "group_vars"
                / "k8s_cluster"
                / "k8s-cluster.yml"
            )
            kube_version_old: str | None = None
            kube_version_backup: Path | None = None
            if kube_version:
                if dry:
                    log(
                        f"[k8s-upgrade] would set kube_version={kube_version} in {group_vars_file}"
                    )
                else:
                    try:
                        kube_version_old, new_text = _kube_version_rewritten(
                            group_vars_file, kube_version, ssh_target, agent_env_id, log
                        )
                        kube_version_backup = _write_group_vars_with_backup(
                            group_vars_file, new_text, ssh_target, agent_env_id, log
                        )
                    except K8sUpgradeFileError as exc:
                        log(f"[denied] {exc}")
                        return {"ok": False, "error": str(exc), "returncode": 2}
            inventory = ctx.config_dir / "inventory"
            log(
                f"[k8s-upgrade] kubespray upgrade-cluster.yml inventory={inventory} dry_run={dry}"
            )
            result = bridge.run_command(
                [
                    "ansible-playbook",
                    "upgrade-cluster.yml",
                    "--become",
                    "-i",
                    str(inventory),
                ],
                cwd=kubespray_dir,
                timeout=timeout,
                dry_run=dry,
                extra_env=extra_env,
                ssh_target=ssh_target,
                remote_env=remote_env,
                agent_env_id=agent_env_id,
                log=log,
            )
            rc = result.get("returncode")
            ok = bool(result.get("dry_run")) or rc == 0
            self.write_audit(
                actor=job.created_by or "system",
                action="env.k8s_upgrade",
                resource_type="environment",
                resource_id=env.id,
                environment_id=env.id,
                details={
                    "kube_version": kube_version or None,
                    "kube_version_previous": kube_version_old,
                    "group_vars_file": str(group_vars_file),
                    "group_vars_backup": (
                        str(kube_version_backup) if kube_version_backup else None
                    ),
                    "inventory": str(inventory),
                    "kubespray_dir": str(kubespray_dir),
                    "returncode": rc,
                    "dry_run": dry,
                },
                success=ok,
            )
            message = (
                "k8s upgrade completed"
                if ok
                else f"k8s upgrade failed (rc={rc}) — see log"
            )
            if kube_version and not dry and ok:
                message += f" (kube_version set {kube_version_old} -> {kube_version})"
            return {
                "ok": ok,
                "returncode": rc,
                "dry_run": bool(result.get("dry_run", dry)),
                "message": message,
            }

        if handler == "genestack_backup_mariadb":
            if env is None:
                return {
                    "ok": False,
                    "error": "genestack.backup_mariadb requires an environment",
                    "returncode": 2,
                }
            log(f"[backup] running scripts/backup-mariadb.sh dry_run={dry}")
            result = bridge.run_command(
                ["bash", "scripts/backup-mariadb.sh"],
                cwd=gs_root,
                timeout=timeout,
                dry_run=dry,
                extra_env=extra_env,
                ssh_target=ssh_target,
                remote_env=remote_env,
                agent_env_id=agent_env_id,
                log=log,
            )
            rc = result.get("returncode")
            ok = bool(result.get("dry_run")) or rc == 0
            # The script dumps each database (minus performance/information_schema)
            # into $HOME/backup/mariadb/<epoch>/ on the host it runs on.
            message = (
                "mariadb backup complete — dumps at $HOME/backup/mariadb/<timestamp> "
                "on the deploy host"
                if ok
                else f"mariadb backup failed (rc={rc}) — see log"
            )
            self.write_audit(
                actor=job.created_by or "system",
                action="env.backup_mariadb",
                resource_type="environment",
                resource_id=env.id,
                environment_id=env.id,
                details={
                    "script": "scripts/backup-mariadb.sh",
                    "returncode": rc,
                    "dry_run": dry,
                },
                success=ok,
            )
            return {
                "ok": ok,
                "returncode": rc,
                "dry_run": bool(result.get("dry_run", dry)),
                "message": message,
            }

        if handler == "genestack_hyperconverged_lab":
            # DESTRUCTIVE lab deployer (scripts/hyperconverged-lab.sh) — like
            # genestack_deploy, the env/global dry-run pin (ctx.dry_run) always
            # wins; params.dry_run may only force a rehearsal on top of it,
            # never LIVE execution on a dry-run-pinned environment.
            effective_dry = dry or bool(params.get("dry_run"))
            platform = str(params.get("platform") or "").strip().lower()
            if platform not in ("kubespray", "talos"):
                msg = (
                    f"Invalid hyperconverged-lab platform '{platform}' — "
                    "must be one of: kubespray, talos"
                )
                log(f"[denied] {msg}")
                return {
                    "ok": False,
                    "error": msg,
                    "returncode": 2,
                    "platform": platform,
                }
            include = str(params.get("include") or "").strip()
            extra_args = str(params.get("extra_args") or "").strip()
            argv = ["bash", "scripts/hyperconverged-lab.sh", platform]
            if include:
                argv += ["-i", include]
            if extra_args:
                argv += shlex.split(extra_args)
            log(
                f"[hyperconverged] platform={platform} include={include or '-'} extra_args={extra_args or '-'} dry_run={effective_dry}"
            )
            result = bridge.run_command(
                argv,
                cwd=gs_root,
                timeout=timeout,
                dry_run=effective_dry,
                extra_env=extra_env,
                ssh_target=ssh_target,
                remote_env=remote_env,
                agent_env_id=agent_env_id,
                log=log,
            )
            rc = result.get("returncode")
            ok = bool(result.get("dry_run")) or rc == 0
            self.write_audit(
                actor=job.created_by or "system",
                action="env.hyperconverged_lab",
                resource_type="environment" if env else "genestack_root",
                resource_id=env.id if env else str(gs_root),
                environment_id=env.id if env else None,
                details={
                    "platform": platform,
                    "include": include or None,
                    "extra_args": extra_args or None,
                    "script": "scripts/hyperconverged-lab.sh",
                    "returncode": rc,
                    "dry_run": effective_dry,
                },
                success=ok,
            )
            return {
                "ok": ok,
                "platform": platform,
                "include": include or None,
                "returncode": rc,
                "dry_run": bool(result.get("dry_run", effective_dry)),
                "message": (
                    "hyperconverged-lab completed"
                    if ok
                    else f"hyperconverged-lab failed (rc={rc}) — see log"
                ),
            }

        if handler == "genestack_deploy":
            if env is None:
                return {
                    "ok": False,
                    "error": (
                        "Cannot deploy: this job has no environment assigned. "
                        "Create the job under an environment with POST /api/v1/environments/<id>/jobs."
                    ),
                    "returncode": 2,
                }
            from app.services import deploy as deploy_service

            # Effective dry_run: the env/global dry-run pin always wins.
            # params.dry_run may only force a rehearsal on top of it — it can
            # never force LIVE execution on a dry-run-pinned environment.
            effective_dry = dry or bool(params.get("dry_run"))
            raw_from_stage = params.get("from_stage")
            from_stage = str(raw_from_stage).strip() if raw_from_stage else None
            raw_until = params.get("until_stage")
            until_stage = str(raw_until).strip() if raw_until else None
            include_testing = bool(params.get("include_testing"))
            parallelism = params.get("parallelism")
            log(
                f"[deploy] Starting deploy for environment '{env.name}' (dry_run={effective_dry})"
            )
            if from_stage:
                log(f"[deploy] Resuming from stage '{from_stage}'")
            if until_stage:
                log(f"[deploy] until_stage '{until_stage}'")
            result = deploy_service.run_deploy(
                self.db,
                env,
                ctx,
                log,
                dry_run=effective_dry,
                skip_push=bool(params.get("skip_push")),
                timeout=timeout,
                extra_env=extra_env,
                ssh_target=ssh_target,
                remote_env=remote_env,
                settings=self.settings,
                from_stage=from_stage,
                until_stage=until_stage,
                include_testing=include_testing,
                deadline=deadline,
                check_cancel=check_cancel,
                parallelism=parallelism,
            )
            self.write_audit(
                actor=job.created_by or "system",
                action="env.deploy",
                resource_type="environment",
                resource_id=env.id,
                environment_id=env.id,
                details={
                    "version": result.get("version"),
                    "stages_completed": result.get("stages_completed"),
                    "stages_total": result.get("stages_total"),
                    "failed_at": result.get("failed_at"),
                    "skip_push": bool(params.get("skip_push")),
                    "from_stage": from_stage,
                    "dry_run": effective_dry,
                    "parallelism": parallelism,
                },
                success=bool(result.get("ok")),
            )
            return result

        if handler == "genestack_greenfield":
            if env is None:
                return {
                    "ok": False,
                    "error": (
                        "Cannot greenfield: this job has no environment assigned. "
                        "Create the job under an environment with POST /api/v1/environments/<id>/jobs."
                    ),
                    "returncode": 2,
                }
            from app.services import greenfield as greenfield_service

            effective_dry = dry or bool(params.get("dry_run"))
            skip_push = params.get("skip_push")
            if skip_push is None:
                skip_push = True
            boot = str(params.get("boot") or "auto").strip() or "auto"
            stop_after = str(params.get("stop_after") or "").strip().lower()
            log(
                f"[greenfield] Starting greenfield redeploy for '{env.name}' "
                f"(dry_run={effective_dry} boot={boot} "
                f"stop_after={stop_after or 'deploy'})"
            )
            result = greenfield_service.run_greenfield(
                self.db,
                env,
                ctx,
                log,
                dry_run=effective_dry,
                skip_push=bool(skip_push),
                timeout=timeout,
                extra_env=extra_env,
                ssh_target=ssh_target,
                remote_env=remote_env,
                settings=self.settings,
                deadline=deadline,
                check_cancel=check_cancel,
                parallelism=params.get("parallelism"),
                boot=boot,
                stop_after=stop_after,
            )
            self.write_audit(
                actor=job.created_by or "system",
                action="env.greenfield",
                resource_type="environment",
                resource_id=env.id,
                environment_id=env.id,
                details={
                    "boot": boot,
                    "stop_after": stop_after,
                    "skip_push": bool(skip_push),
                    "dry_run": effective_dry,
                    "failed_at": result.get("failed_at"),
                    "stages_completed": result.get("stages_completed"),
                },
                success=bool(result.get("ok")),
            )
            return result

        if handler == "registry_mirror":
            from app.services import image_registry as image_registry_service

            if env is None:
                return {
                    "ok": False,
                    "error": "registry.mirror requires an environment",
                    "returncode": 2,
                }
            effective_dry = dry or bool(params.get("dry_run"))
            log("[registry] warming Console image cache from live cluster")
            result = image_registry_service.mirror_cluster(
                env,
                ctx.kubeconfig if ctx is not None else None,
                log,
                dry_run=effective_dry,
                settings=self.settings,
                check_cancel=check_cancel,
            )
            self.write_audit(
                actor=job.created_by or "system",
                action="env.registry.mirror",
                resource_type="environment",
                resource_id=env.id,
                environment_id=env.id,
                details={
                    "cached": result.get("cached"),
                    "failed": result.get("failed"),
                    "total": result.get("total"),
                    "dry_run": effective_dry,
                },
                success=bool(result.get("ok")),
            )
            return result

        if handler == "genestack_talos_bootstrap":
            if env is None:
                return {
                    "ok": False,
                    "error": "genestack.talos.bootstrap requires an environment",
                    "returncode": 2,
                }
            from app.services import envconfig as envconfig_service
            from app.services import talos as talos_service

            current = envconfig_service.get_current(self.db, env)
            if current is None:
                msg = (
                    "no config document yet — PUT "
                    f"/api/v1/environments/{env.id}/config first"
                )
                log(f"[talos] {msg}")
                return {"ok": False, "error": msg, "returncode": 2}
            try:
                result = talos_service.run_talos_bootstrap(
                    current[0],
                    env,
                    log,
                    dry_run=dry,
                    timeout=timeout,
                    extra_env=extra_env,
                    ssh_target=ssh_target,
                    remote_env=remote_env,
                    agent_env_id=agent_env_id,
                )
            except envconfig_service.ConfigValidationError as exc:
                log(f"[talos] {exc}")
                return {"ok": False, "error": str(exc), "returncode": 2}
            self.write_audit(
                actor=job.created_by or "system",
                action="env.talos_bootstrap",
                resource_type="environment",
                resource_id=env.id,
                environment_id=env.id,
                details={
                    "cluster_name": result.get("cluster_name"),
                    "install_disk": result.get("install_disk"),
                    "control_planes": result.get("control_planes"),
                    "workers": result.get("workers"),
                    "failed_phase": result.get("failed_phase"),
                    "dry_run": dry,
                },
                success=bool(result.get("ok")),
            )
            return result

        if handler == "genestack_config_push":
            if env is None:
                return {
                    "ok": False,
                    "error": "genestack.config.push requires an environment",
                    "returncode": 2,
                }
            from app.services import envconfig as envconfig_service
            from app.services.crypto import encrypt_secret

            current = envconfig_service.get_current(self.db, env)
            if current is None:
                msg = (
                    "Cannot push config: no config document exists for environment '{name}'. "
                    "Store a config first with PUT /api/v1/environments/{env_id}/config, "
                    "then re-run this job."
                ).format(name=env.name, env_id=env.id)
                log(f"[config.push] {msg}")
                return {"ok": False, "error": msg, "returncode": 2}
            doc, row = current
            log(
                f"[config.push] Rendering config version {row.version} for environment '{env.name}'..."
            )
            try:
                files = envconfig_service.render_to_files(doc, env, self.settings)
                log(
                    f"[config.push] Rendered {len(files)} file(s) from config version {row.version}"
                )
                result = envconfig_service.push_rendered(files, ctx, log, dry)
            except envconfig_service.ConfigValidationError as exc:
                log(f"[config.push] Configuration error: {exc}")
                return {
                    "ok": False,
                    "error": f"Config validation error in version {row.version}: {exc}",
                    "returncode": 2,
                    "version": row.version,
                }

            if not dry:
                # Sync deploy/maas doc sections onto the Environment row so the
                # execution context and MAAS client keep working.
                deploy = doc.get("deploy")
                if isinstance(deploy, dict):
                    if deploy.get("ssh_host") is not None:
                        env.deployer_ssh_host = deploy["ssh_host"]
                    if deploy.get("ssh_user") is not None:
                        env.deployer_ssh_user = deploy["ssh_user"]
                maas_doc = doc.get("maas")
                if isinstance(maas_doc, dict):
                    if maas_doc.get("url") is not None:
                        env.maas_url = maas_doc["url"]
                    if maas_doc.get("api_key"):
                        env.maas_api_key_encrypted = encrypt_secret(
                            maas_doc["api_key"], self.settings
                        )
                self.db.add(env)
                self.db.flush()

            self.write_audit(
                actor=job.created_by or "system",
                action="env.config.push",
                resource_type="environment",
                resource_id=env.id,
                environment_id=env.id,
                details={
                    "version": row.version,
                    "files": result["count"],
                    "bytes": result["bytes"],
                    "dry_run": dry,
                },
            )
            result["version"] = row.version
            return result

        if handler == "genestack_state_export":
            if env is None:
                return {
                    "ok": False,
                    "error": "genestack.state.export requires an environment",
                    "returncode": 2,
                }
            import fcntl
            import subprocess

            from app.services import envconfig as envconfig_service
            from app.services.executors import pick_executor

            state_repo_path = (env.state_repo_path or "").strip()
            if not state_repo_path:
                return {
                    "ok": False,
                    "error": "genestack.state.export requires state_repo_path to be set on the environment",
                    "returncode": 2,
                }
            if "/" in env.name or env.name.startswith("-") or env.name in (".", ".."):
                log(
                    f"[state.export] environment name '{env.name}' is not safe as a path segment"
                )
                return {
                    "ok": False,
                    "error": f"environment name '{env.name}' is not safe as a state repo path segment",
                    "returncode": 2,
                }

            executor = pick_executor(env, ctx, self.settings)
            agent_env_id = executor.agent_env_id
            repo_root = Path(state_repo_path).expanduser()

            # The checkout is owned by the deploy host (connected agent, else ssh):
            # run the whole git flow there. The local .git check / fcntl lock below
            # apply only when the console host owns the checkout.
            if ctx.ssh_target or agent_env_id:
                remote = (env.state_repo_remote or "").strip()
                return self._state_export_remote(
                    env,
                    job,
                    params,
                    ctx,
                    log,
                    dry,
                    timeout,
                    deadline,
                    executor,
                    remote,
                )

            if not repo_root.is_dir() or not (repo_root / ".git").exists():
                log(
                    f"[state.export] state_repo_path '{state_repo_path}' is not a git checkout"
                )
                return {
                    "ok": False,
                    "error": f"state_repo_path '{state_repo_path}' is not a git checkout (missing .git)",
                    "returncode": 2,
                }

            # Serialize exports across environments sharing this checkout:
            # concurrent git add/commit would race on .git/index.
            lockfile = open(repo_root / ".git" / "console-state-export.lock", "a+")
            try:
                fcntl.flock(lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (BlockingIOError, OSError):
                lockfile.close()
                log(
                    "[state.export] another state export is already running on this repo"
                )
                return {
                    "ok": False,
                    "error": "another state export is already running on this repo",
                    "returncode": 1,
                }
            try:
                proc = subprocess.run(
                    ["git", "symbolic-ref", "-q", "HEAD"],
                    cwd=repo_root,
                    capture_output=True,
                    text=True,
                    timeout=clamp_deadline_timeout(timeout, deadline),
                )
                if proc.returncode != 0:
                    log(
                        f"[state.export] state repo '{repo_root}' is in detached HEAD state"
                    )
                    return {
                        "ok": False,
                        "error": f"state repo '{repo_root}' is in detached HEAD state — check out a branch (e.g. 'git checkout main') and re-run",
                        "returncode": 2,
                    }

                current = envconfig_service.get_current(self.db, env)
                if current is None:
                    msg = (
                        "Cannot export state: no config document exists for environment '{name}'. "
                        "Store a config first with PUT /api/v1/environments/{env_id}/config, "
                        "then re-run this job."
                    ).format(name=env.name, env_id=env.id)
                    log(f"[state.export] {msg}")
                    return {"ok": False, "error": msg, "returncode": 2}
                doc, row = current
                log(
                    f"[state.export] Rendering config version {row.version} for environment '{env.name}'..."
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
                    1
                    if isinstance(doc.get("secrets"), dict) and doc.get("secrets")
                    else 0
                ) + (1 if env.ssh_private_key_encrypted else 0)
                if excluded_secrets:
                    log(
                        f"[state.export] excluded {excluded_secrets} secret file(s) from state export "
                        "(kubesecrets.yaml / .ssh — secrets stay in the DB)"
                    )
                log(
                    f"[state.export] Rendered {len(files)} file(s) from config version {row.version}"
                )

                state_dir = repo_root / "state" / env.name
                file_sizes = {
                    path: len(content.encode("utf-8"))
                    for path, content in files.items()
                }
                total_bytes = sum(file_sizes.values())
                commit_msg = f"state({env.name}): export config version {row.version}"

                if dry:
                    for path in sorted(files):
                        log(
                            f"[state.export] dry-run: would write {state_dir / path} ({file_sizes[path]} bytes)"
                        )
                    log(f"[state.export] dry-run: would commit '{commit_msg}'")
                    remote = (env.state_repo_remote or "").strip()
                    if remote:
                        log(f"[state.export] dry-run: would push to remote '{remote}'")
                    else:
                        log("[state.export] dry-run: no remote configured, commit only")
                    self.write_audit(
                        actor=job.created_by or "system",
                        action="env.state.export",
                        resource_type="environment",
                        resource_id=env.id,
                        environment_id=env.id,
                        details={
                            "version": row.version,
                            "files": len(files),
                            "bytes": total_bytes,
                            "commit": None,
                            "pushed": False,
                            "dry_run": True,
                        },
                    )
                    return {
                        "ok": True,
                        "dry_run": True,
                        "version": row.version,
                        "files": len(files),
                        "bytes": total_bytes,
                        "state_dir": str(state_dir),
                    }

                for path, content in files.items():
                    target = state_dir / path
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(content, encoding="utf-8")
                    target.chmod(0o644)
                    log(f"[state.export] wrote {target} ({file_sizes[path]} bytes)")

                try:
                    proc = subprocess.run(
                        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                        cwd=repo_root,
                        capture_output=True,
                        text=True,
                        timeout=clamp_deadline_timeout(timeout, deadline),
                    )
                    if proc.returncode != 0:
                        raise OSError(f"git rev-parse failed: {proc.stderr.strip()}")
                    branch = proc.stdout.strip()
                    log(f"[state.export] git: on branch '{branch}'")

                    proc = subprocess.run(
                        ["git", "add", "--", f"state/{env.name}"],
                        cwd=repo_root,
                        capture_output=True,
                        text=True,
                        timeout=clamp_deadline_timeout(timeout, deadline),
                    )
                    if proc.returncode != 0:
                        raise OSError(f"git add failed: {proc.stderr.strip()}")

                    proc = subprocess.run(
                        [
                            "git",
                            "diff",
                            "--cached",
                            "--quiet",
                            "--",
                            f"state/{env.name}",
                        ],
                        cwd=repo_root,
                        capture_output=True,
                        text=True,
                        timeout=clamp_deadline_timeout(timeout, deadline),
                    )
                    if proc.returncode == 0:
                        log("[state.export] no changes staged, skipping commit")
                        self.write_audit(
                            actor=job.created_by or "system",
                            action="env.state.export",
                            resource_type="environment",
                            resource_id=env.id,
                            environment_id=env.id,
                            details={
                                "version": row.version,
                                "files": len(files),
                                "bytes": total_bytes,
                                "commit": None,
                                "pushed": False,
                                "dry_run": False,
                            },
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
                            "remote": (env.state_repo_remote or "").strip() or None,
                        }

                    proc = subprocess.run(
                        [
                            "git",
                            "-c",
                            "user.name=genestack-console",
                            "-c",
                            "user.email=console@localhost",
                            "commit",
                            "-m",
                            commit_msg,
                            "--",
                            f"state/{env.name}",
                        ],
                        cwd=repo_root,
                        capture_output=True,
                        text=True,
                        timeout=clamp_deadline_timeout(timeout, deadline),
                    )
                    if proc.returncode != 0:
                        raise OSError(f"git commit failed: {proc.stderr.strip()}")

                    proc = subprocess.run(
                        ["git", "rev-parse", "HEAD"],
                        cwd=repo_root,
                        capture_output=True,
                        text=True,
                        timeout=clamp_deadline_timeout(timeout, deadline),
                    )
                    if proc.returncode != 0:
                        raise OSError(
                            f"git rev-parse HEAD failed: {proc.stderr.strip()}"
                        )
                    sha = proc.stdout.strip()
                    log(f"[state.export] committed {sha} ({commit_msg})")

                    pushed = False
                    remote = (env.state_repo_remote or "").strip()
                    if remote:
                        proc = subprocess.run(
                            ["git", "push", remote, branch],
                            cwd=repo_root,
                            capture_output=True,
                            text=True,
                            timeout=clamp_deadline_timeout(timeout, deadline),
                        )
                        if proc.returncode != 0:
                            log(
                                f"[state.export] git push failed: {proc.stderr.strip()}"
                            )
                            self.write_audit(
                                actor=job.created_by or "system",
                                action="env.state.export",
                                resource_type="environment",
                                resource_id=env.id,
                                environment_id=env.id,
                                details={
                                    "version": row.version,
                                    "files": len(files),
                                    "bytes": total_bytes,
                                    "commit": sha,
                                    "pushed": False,
                                    "dry_run": False,
                                },
                                success=False,
                            )
                            return {
                                "ok": False,
                                "error": f"git push failed: {proc.stderr.strip()}",
                                "returncode": proc.returncode,
                                "version": row.version,
                                "committed": True,
                                "commit": sha,
                                "pushed": False,
                                "remote": remote,
                            }
                        pushed = True
                        log(f"[state.export] pushed {branch} to remote '{remote}'")
                except (subprocess.TimeoutExpired, OSError) as exc:
                    log(f"[state.export] git error: {exc}")
                    self.write_audit(
                        actor=job.created_by or "system",
                        action="env.state.export",
                        resource_type="environment",
                        resource_id=env.id,
                        environment_id=env.id,
                        details={
                            "version": row.version,
                            "files": len(files),
                            "bytes": total_bytes,
                            "commit": None,
                            "pushed": False,
                            "dry_run": False,
                        },
                        success=False,
                    )
                    return {"ok": False, "error": str(exc), "returncode": 1}

                self.write_audit(
                    actor=job.created_by or "system",
                    action="env.state.export",
                    resource_type="environment",
                    resource_id=env.id,
                    environment_id=env.id,
                    details={
                        "version": row.version,
                        "files": len(files),
                        "bytes": total_bytes,
                        "commit": sha,
                        "pushed": pushed,
                        "dry_run": False,
                    },
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
                    "remote": remote,
                }
            finally:
                fcntl.flock(lockfile, fcntl.LOCK_UN)
                lockfile.close()

        if handler == "hostvm_list":
            vms = hypervisor_service.vm_status(self.db, self.settings)
            log(f"[hostvm] listed {len(vms)} host VMs")
            return {
                "ok": True,
                "vms": vms,
                "count": len(vms),
                "message": f"{len(vms)} host VMs",
            }

        if handler in ("hostvm_start", "hostvm_stop", "hostvm_restart"):
            # Host VMs are host-level infra — never environment-scoped.
            vm_id = str(params.get("vm_id", "")).strip()
            vm = self.db.get(HostVM, vm_id) if vm_id else None
            if vm is None:
                msg = f"{op.id}: unknown host VM id {vm_id!r}"
                log(f"[denied] {msg}")
                return {"ok": False, "error": msg, "returncode": 2}
            action = handler.removeprefix("hostvm_")
            result = hypervisor_service.vm_action(
                self.db, self.settings, vm, action, dry_run=dry
            )
            log(f"[hostvm] {action} {vm.name} ({vm.id}): {result.get('message')}")
            result.setdefault("vm_id", vm.id)
            result.setdefault("vm_name", vm.name)
            return result

        if handler == "baremetal_node_register":
            if env is None:
                return {
                    "ok": False,
                    "error": "baremetal.node.register requires an environment",
                    "returncode": 2,
                }
            from app.services import baremetal as baremetal_service

            name = str(params.get("name") or "").strip()
            result = baremetal_service.register_node(
                self.db,
                env,
                name=name,
                bmc_host=str(params.get("bmc_host") or ""),
                bmc_username=str(params.get("bmc_username") or ""),
                bmc_password=str(params.get("bmc_password") or ""),
                pxe_mac=str(params.get("pxe_mac") or "").strip() or None,
                dry_run=dry,
                log=log,
                settings=self.settings,
            )
            self.write_audit(
                actor=job.created_by or "system",
                action="env.baremetal.register",
                resource_type="environment",
                resource_id=env.id,
                environment_id=env.id,
                details={
                    "name": name,
                    "bmc_host": str(params.get("bmc_host") or "").strip(),
                    "dry_run": dry,
                },
                success=bool(result.get("ok")),
            )
            return result

        if handler == "baremetal_nodes_list":
            if env is None:
                return {
                    "ok": False,
                    "error": "baremetal.nodes.list requires an environment",
                    "returncode": 2,
                }
            from app.services import baremetal as baremetal_service

            nodes = baremetal_service.list_nodes(self.db, env)
            log(f"[baremetal] listed {len(nodes)} nodes")
            return {
                "ok": True,
                "dry_run": dry,
                "nodes": nodes,
                "count": len(nodes),
                "message": f"{len(nodes)} bare-metal nodes",
            }

        if handler == "baremetal_bmc_scan":
            if env is None:
                return {
                    "ok": False,
                    "error": "baremetal.bmc_scan requires an environment",
                    "returncode": 2,
                }
            import ipaddress

            from app.services import agents as agents_service

            subnet = str(params.get("subnet") or "").strip()
            try:
                subnet = str(ipaddress.ip_network(subnet, strict=False))
            except ValueError:
                msg = f"baremetal.bmc_scan: invalid CIDR subnet {subnet!r}"
                log(f"[denied] {msg}")
                return {"ok": False, "error": msg, "returncode": 2}

            if dry:
                log(
                    f"[dry-run] would scan {subnet} for BMCs via the env's connected agent"
                )
                return {
                    "ok": True,
                    "dry_run": True,
                    "subnet": subnet,
                    "message": f"[dry-run] would scan {subnet} for BMCs",
                }

            if not agents_service.agent_available(self.db, env.id):
                msg = "no agent connected for this env"
                log(f"[agent] {msg}")
                return {"ok": False, "error": msg, "returncode": 2}

            # Cross-process: the row is dispatched by the API-process relay,
            # which owns the agent's websocket (the worker's local registry
            # is always empty).
            result = agent_relay.agent_exec(
                env.id,
                "scan_bmc",
                {"subnet": subnet, "timeout": timeout},
                timeout=timeout,
                log_cb=lambda line: log(f"[agent] {line}"),
            )
            if result.get("error"):
                log(f"[agent] {result['error']}")
                return {"ok": False, "error": str(result["error"]), "returncode": 2}

            rc = result.get("rc")
            ok = rc in (0, None)
            found = result.get("found")
            # Audit the subnet and the found count — never credentials.
            self.write_audit(
                actor=job.created_by or "system",
                action="env.bmc_scan",
                resource_type="environment",
                resource_id=env.id,
                environment_id=env.id,
                details={"subnet": subnet, "found": found, "dry_run": dry},
                success=ok,
            )
            return {
                "ok": ok,
                "subnet": subnet,
                "found": found,
                "returncode": rc,
                "stdout": result.get("stdout"),
                "stderr": result.get("stderr"),
                "dry_run": False,
                "message": (
                    f"bmc scan of {subnet} complete: {found} found"
                    if ok
                    else f"bmc scan of {subnet} failed (rc={rc}) — see log"
                ),
            }

        if handler in (
            "baremetal_node_power",
            "baremetal_node_pxe_boot",
            "baremetal_node_next_boot",
            "baremetal_node_iso_boot",
            "baremetal_node_provision",
        ):
            if env is None:
                return {
                    "ok": False,
                    "error": f"{op.id} requires an environment",
                    "returncode": 2,
                }
            from app.services import baremetal as baremetal_service

            node = baremetal_service.get_node(self.db, env, params.get("node_id"))
            if node is None:
                msg = f"{op.id}: unknown node id {params.get('node_id')!r} for this environment"
                log(f"[denied] {msg}")
                return {"ok": False, "error": msg, "returncode": 2}

            audit_action = ""
            details: dict[str, Any] = {
                "node_id": node.id,
                "node": node.name,
                "dry_run": dry,
            }
            if handler == "baremetal_node_power":
                action = str(params.get("action") or "").strip().lower()
                result = baremetal_service.power_action(
                    self.db, node, action, dry_run=dry, log=log, settings=self.settings
                )
                audit_action = "env.baremetal.power"
                details["action"] = action
            elif handler == "baremetal_node_pxe_boot":
                result = baremetal_service.pxe_boot(
                    self.db, node, dry_run=dry, log=log, settings=self.settings
                )
                audit_action = "env.baremetal.pxe_boot"
            elif handler == "baremetal_node_next_boot":
                target = str(params.get("next_boot") or "").strip().lower()
                raw_now = params.get("boot_now")
                if isinstance(raw_now, str):
                    boot_now = raw_now.strip().lower() in ("1", "true", "yes", "on")
                else:
                    boot_now = bool(raw_now)
                result = baremetal_service.set_next_boot(
                    self.db,
                    env,
                    node,
                    target,
                    boot_now=boot_now,
                    dry_run=dry,
                    log=log,
                    settings=self.settings,
                )
                audit_action = "env.baremetal.next_boot"
                details["next_boot"] = target
                details["boot_now"] = boot_now
            elif handler == "baremetal_node_iso_boot":
                result = baremetal_service.iso_boot(
                    self.db,
                    env,
                    node,
                    dry_run=dry,
                    log=log,
                    settings=self.settings,
                    image_url=str(params.get("image_url") or "").strip() or None,
                )
                audit_action = "env.baremetal.iso_boot"
            else:
                raw_roles = params.get("roles") or []
                if isinstance(raw_roles, str):
                    raw_roles = raw_roles.split(",")
                roles = [str(r).strip().lower() for r in raw_roles if str(r).strip()]
                from app.services import envconfig as envconfig_service

                invalid = [
                    r for r in roles if r not in envconfig_service.VALID_SERVER_ROLES
                ]
                if invalid:
                    valid = ", ".join(sorted(envconfig_service.VALID_SERVER_ROLES))
                    return {
                        "ok": False,
                        "error": (
                            f"baremetal.node.provision: unknown role(s) "
                            f"{', '.join(invalid)} (valid: {valid})"
                        ),
                        "returncode": 2,
                    }
                stop_after = str(params.get("stop_after") or "").strip().lower()
                result = baremetal_service.provision(
                    self.db,
                    env,
                    node,
                    roles=roles,
                    actor=job.created_by or "system",
                    dry_run=dry,
                    log=log,
                    settings=self.settings,
                    timeout_seconds=timeout,
                    stop_after=stop_after,
                )
                audit_action = "env.baremetal.provision"
                details["roles"] = roles
                details["stop_after"] = stop_after
            self.write_audit(
                actor=job.created_by or "system",
                action=audit_action,
                resource_type="environment",
                resource_id=env.id,
                environment_id=env.id,
                details=details,
                success=bool(result.get("ok")),
            )
            return result

        if handler == "agent_status":
            if env is None:
                return {
                    "ok": False,
                    "error": "agent.status requires an environment",
                    "returncode": 2,
                }
            from app.services import agents as agents_service

            payload = agents_service.status_for_env(self.db, env.id)
            log(
                f"[agent] status enrolled={payload['enrolled']} "
                f"connected={payload['connected']} hostname={payload['hostname']}"
            )
            return {
                "ok": True,
                "dry_run": dry,
                **payload,
                "message": (
                    "agent connected" if payload["connected"] else "agent not connected"
                ),
            }

        if handler == "agent_command":
            if env is None:
                return {
                    "ok": False,
                    "error": "agent.command requires an environment",
                    "returncode": 2,
                }
            from app.services import agents as agents_service

            command = str(params.get("command") or "").strip()
            argv = agents_service.AGENT_COMMAND_ALLOWLIST.get(command)
            if argv is None:
                allowed = ", ".join(sorted(agents_service.AGENT_COMMAND_ALLOWLIST))
                msg = f"Command '{command}' is not allowlisted (allowed: {allowed})"
                log(f"[denied] {msg}")
                return {"ok": False, "error": msg, "returncode": 2}

            if dry:
                log(f"[dry-run] would run '{command}' on the env's connected agent")
                return {
                    "ok": True,
                    "dry_run": True,
                    "command": command,
                    "message": f"[dry-run] would run '{command}' on agent",
                }

            if not agents_service.agent_available(self.db, env.id):
                msg = "no agent connected for this env"
                log(f"[agent] {msg}")
                return {"ok": False, "error": msg, "returncode": 2}

            # Cross-process: the row is dispatched by the API-process relay,
            # which owns the agent's websocket (the worker's local registry
            # is always empty).
            result = agent_relay.agent_exec(
                env.id,
                "run_command",
                {"cmd": argv, "timeout": timeout},
                timeout=timeout,
                log_cb=lambda line: log(f"[agent] {line}"),
            )
            if result.get("error"):
                log(f"[agent] {result['error']}")
                return {"ok": False, "error": str(result["error"]), "returncode": 2}

            rc = result.get("rc")
            ok = rc == 0
            if result.get("stderr"):
                log(f"[agent] stderr: {result['stderr']}")
            self.write_audit(
                actor=job.created_by or "system",
                action="env.agent.command",
                resource_type="environment",
                resource_id=env.id,
                environment_id=env.id,
                details={"command": command, "returncode": rc, "dry_run": dry},
                success=ok,
            )
            return {
                "ok": ok,
                "command": command,
                "returncode": rc,
                "stdout": result.get("stdout"),
                "stderr": result.get("stderr"),
                "dry_run": False,
                "message": (
                    f"agent command '{command}' completed"
                    if ok
                    else f"agent command '{command}' failed (rc={rc}) — see log"
                ),
            }

        if handler == "agent_install":
            if env is None:
                return {
                    "ok": False,
                    "error": (
                        "Cannot install agent: this job has no environment assigned. "
                        "Create the job scoped to an environment with POST /api/v1/environments/<id>/jobs."
                    ),
                    "returncode": 2,
                }
            from app.services import agents as agents_service

            host = str(params.get("host") or "").strip()
            if not host:
                msg = (
                    "Cannot install agent: 'host' parameter is missing or empty. "
                    "Specify the target host as a hostname or IP address. "
                    'Example: {"operation": "agent.install", "params": {"host": "node01.example.com"}}'
                )
                log(f"[agent] {msg}")
                return {"ok": False, "error": msg, "returncode": 2}

            ssh_user = str(params.get("ssh_user") or "root").strip() or "root"
            try:
                ssh_port = int(params.get("ssh_port") or 22)
            except (TypeError, ValueError):
                ssh_port = 22
            name = str(params.get("name") or "").strip() or None
            log(
                f"[agent] Starting agent install on {ssh_user}@{host}:{ssh_port} for environment '{env.name}'"
            )
            result = agents_service.install_agent(
                self.db,
                env,
                self.settings,
                host=host,
                ssh_user=ssh_user,
                ssh_port=ssh_port,
                name=name,
                dry_run=dry,
                timeout=timeout,
                log=log,
            )
            # Audit carries the host, never the token.
            self.write_audit(
                actor=job.created_by or "system",
                action="env.agent.install",
                resource_type="environment",
                resource_id=env.id,
                environment_id=env.id,
                details={
                    "host": host,
                    "ssh_user": ssh_user,
                    "agent_name": result.get("agent_name"),
                    "dry_run": dry,
                },
                success=bool(result.get("ok")),
            )
            return result

        if handler == "ovh_byoi_reinstall":
            if env is None:
                return {
                    "ok": False,
                    "error": (
                        "Cannot reinstall OVH servers: this job has no environment assigned. "
                        "Create the job under an environment with POST /api/v1/environments/<id>/jobs."
                    ),
                    "returncode": 2,
                }
            from app.services import deploy as deploy_service

            operating_system = str(params.get("operating_system") or "").strip() or None
            image_url = str(params.get("image_url") or "").strip() or None
            raw_hostnames = params.get("server_hostnames") or []
            if isinstance(raw_hostnames, str):
                raw_hostnames = [h for h in raw_hostnames.split(",") if h.strip()]
            server_hostnames = [
                str(h).strip() for h in raw_hostnames if str(h).strip()
            ] or None
            # The env/global dry-run pin always wins; params.dry_run may only
            # force a rehearsal on top of it (same clamp as genestack.deploy).
            effective_dry = dry or bool(params.get("dry_run"))
            wait = params.get("wait") is not False
            log(
                f"[ovh] BYOI reinstall for environment '{env.name}' "
                f"operating_system={operating_system or '(from image)'} "
                f"server_hostnames={','.join(server_hostnames) if server_hostnames else 'all OVH-owned'} "
                f"(dry_run={effective_dry} wait={wait})"
            )
            result = deploy_service.ovh_byoi_reinstall_for_env(
                self.db,
                env,
                operating_system=operating_system,
                server_hostnames=server_hostnames,
                image_url=image_url,
                dry_run=effective_dry,
                wait=wait,
                log=log,
                deadline=deadline,
                check_cancel=check_cancel,
            )
            self.write_audit(
                actor=job.created_by or "system",
                action="env.ovh.byoi_reinstall",
                resource_type="environment",
                resource_id=env.id,
                environment_id=env.id,
                details={
                    "operating_system": operating_system,
                    "server_hostnames": server_hostnames,
                    "count": result.get("count"),
                    "dry_run": effective_dry,
                    "failed": [
                        s["hostname"]
                        for s in result.get("servers") or []
                        if s.get("error")
                    ],
                },
                success=bool(result.get("ok")),
            )
            return result

        if handler == "app_deploy":
            from app.models import App as AppRow
            from app.services import apps as apps_svc

            app_id = str((params or {}).get("app_id") or "").strip()
            force = bool((params or {}).get("force"))
            if env is None:
                return {
                    "ok": False,
                    "error": "app.deploy requires an environment",
                    "returncode": 2,
                }
            row = self.db.get(AppRow, app_id)
            if row is None or row.environment_id != env.id:
                return {"ok": False, "error": "app not found", "returncode": 2}
            log(f"[app] deploy {row.name} target={row.target} repo={row.repo_url}")
            result = apps_svc.deploy_app(
                row,
                env,
                kubeconfig=ctx.kubeconfig,
                force=force,
                log_fn=log,
                dry_run=dry,
            )
            row.last_sha = result.get("sha") or row.last_sha
            row.last_job_id = job.id
            row.last_status = "success" if result.get("ok") else "failed"
            row.last_error = (
                None if result.get("ok") else str(result.get("error") or "")[:400]
            )
            self.db.add(row)
            self.write_audit(
                actor=job.created_by or "system",
                action="env.app.deploy",
                resource_type="app",
                resource_id=row.id,
                environment_id=env.id,
                details={
                    "name": row.name,
                    "sha": result.get("sha"),
                    "ok": result.get("ok"),
                    "skipped": result.get("skipped"),
                },
                success=bool(result.get("ok")),
            )
            if not result.get("ok"):
                result.setdefault("returncode", 1)
            return result

        if handler == "ovh_vrack_attach":
            if env is None:
                return {
                    "ok": False,
                    "error": "ovh.vrack.attach requires an environment",
                    "returncode": 2,
                }
            from app.services import ovh_fabric as fabric

            vrack = str(params.get("vrack") or "").strip() or None
            hostnames = params.get("server_hostnames")
            hostname_filter = None
            if isinstance(hostnames, list) and hostnames:
                hostname_filter = frozenset(str(h) for h in hostnames if str(h).strip())
            effective_dry = dry or bool(params.get("dry_run"))
            log(
                f"[vrack] attach environment '{env.name}' vrack={vrack or '(from doc)'} "
                f"dry_run={effective_dry}"
            )
            result = fabric.attach_env_to_vrack(
                self.db,
                env,
                vrack=vrack,
                dry_run=effective_dry,
                log=log,
                hostname_filter=hostname_filter,
            )
            self.write_audit(
                actor=job.created_by or "system",
                action="env.ovh.vrack_attach",
                resource_type="environment",
                resource_id=env.id,
                environment_id=env.id,
                details={
                    "vrack": result.get("vrack") or vrack,
                    "count": result.get("count"),
                    "dry_run": effective_dry,
                },
                success=bool(result.get("ok")),
            )
            return result

        if handler in ("hardware_terraform_plan", "hardware_terraform_apply"):
            from app.services import terraform as terraform_service

            action = "apply" if handler == "hardware_terraform_apply" else "plan"
            effective_dry = dry or bool(params.get("dry_run"))
            result = terraform_service.run_terraform_job(
                self.db,
                env,
                action=action,
                params=params,
                dry_run=effective_dry,
                log=log,
                timeout=timeout,
                actor=job.created_by or "system",
                settings=self.settings,
            )
            self.write_audit(
                actor=job.created_by or "system",
                action=f"env.hardware.terraform.{action}",
                resource_type="environment",
                resource_id=env.id if env is not None else None,
                environment_id=env.id if env is not None else None,
                details={
                    "account_id": str(params.get("account_id") or ""),
                    "count": result.get("count"),
                    "kind": result.get("kind"),
                    "imported": result.get("imported") or [],
                    "dry_run": effective_dry,
                },
                success=bool(result.get("ok")),
            )
            return result

        if handler == "platform_talos_reboot":
            from app.services import platform as platform_svc

            name = str(params.get("name") or "").strip()
            if not name:
                return {"ok": False, "dry_run": dry, "error": "name is required"}
            effective_dry = dry or bool(params.get("dry_run"))
            log(f"platform.talos.reboot node={name} dry_run={effective_dry}")
            if env is None:
                return {"ok": False, "dry_run": effective_dry, "error": "environment required"}
            return platform_svc.talos_reboot(
                env, name, self.settings, self.db, dry_run=effective_dry
            )

        if handler == "platform_talos_shutdown":
            from app.services import platform as platform_svc

            name = str(params.get("name") or "").strip()
            if not name:
                return {"ok": False, "dry_run": dry, "error": "name is required"}
            effective_dry = dry or bool(params.get("dry_run"))
            log(f"platform.talos.shutdown node={name} dry_run={effective_dry}")
            if env is None:
                return {"ok": False, "dry_run": effective_dry, "error": "environment required"}
            return platform_svc.talos_shutdown(
                env, name, self.settings, self.db, dry_run=effective_dry
            )

        if handler == "platform_talos_reset":
            from app.services import platform as platform_svc

            name = str(params.get("name") or "").strip()
            if not name:
                return {"ok": False, "dry_run": dry, "error": "name is required"}
            graceful = params.get("graceful", True)
            reboot = params.get("reboot", False)
            wipe = params.get("wipe", True)
            effective_dry = dry or bool(params.get("dry_run"))
            log(
                f"platform.talos.reset node={name} graceful={graceful} "
                f"reboot={reboot} wipe={wipe} dry_run={effective_dry}"
            )
            if env is None:
                return {"ok": False, "dry_run": effective_dry, "error": "environment required"}
            return platform_svc.talos_reset(
                env,
                name,
                self.settings,
                self.db,
                graceful=bool(graceful),
                reboot=bool(reboot),
                wipe=bool(wipe),
                dry_run=effective_dry,
            )

        if handler == "platform_talos_upgrade":
            from app.services import platform as platform_svc

            name = str(params.get("name") or "").strip()
            if not name:
                return {"ok": False, "dry_run": dry, "error": "name is required"}
            image = params.get("image")
            image_s = str(image).strip() if image is not None else None
            if image_s == "":
                image_s = None
            effective_dry = dry or bool(params.get("dry_run"))
            log(f"platform.talos.upgrade node={name} image={image_s or '-'} dry_run={effective_dry}")
            if env is None:
                return {"ok": False, "dry_run": effective_dry, "error": "environment required"}
            return platform_svc.talos_upgrade(
                env, name, self.settings, self.db, image=image_s, dry_run=effective_dry
            )

        if handler == "platform_talos_upgrade_many":
            from app.services import platform as platform_svc

            image = params.get("image")
            image_s = str(image).strip() if image is not None else None
            if image_s == "":
                image_s = None
            mode = str(params.get("mode") or "rolling").strip().lower() or "rolling"
            names = params.get("names")
            name_list = None
            if isinstance(names, list):
                name_list = [str(n).strip() for n in names if str(n).strip()]
            effective_dry = dry or bool(params.get("dry_run"))
            log(
                f"platform.talos.upgrade_many mode={mode} "
                f"names={len(name_list) if name_list else 'all'} dry_run={effective_dry}"
            )
            if env is None:
                return {"ok": False, "dry_run": effective_dry, "error": "environment required"}
            return platform_svc.talos_upgrade_many(
                env,
                self.settings,
                self.db,
                image=image_s,
                mode=mode,
                names=name_list,
                dry_run=effective_dry,
            )

        if handler == "platform_talos_apply_config":
            from app.services import platform as platform_svc

            name = str(params.get("name") or "").strip()
            yaml_text = params.get("yaml")
            if not name:
                return {"ok": False, "dry_run": dry, "error": "name is required"}
            if not isinstance(yaml_text, str) or not yaml_text.strip():
                return {"ok": False, "dry_run": dry, "error": "yaml is required"}
            mode = str(params.get("mode") or "auto").strip().lower() or "auto"
            effective_dry = dry or bool(params.get("dry_run"))
            log(
                f"platform.talos.apply_config node={name} mode={mode} "
                f"bytes={len(yaml_text)} dry_run={effective_dry}"
            )
            if env is None:
                return {"ok": False, "dry_run": effective_dry, "error": "environment required"}
            return platform_svc.talos_apply_config(
                env,
                name,
                self.settings,
                self.db,
                yaml_text=yaml_text,
                mode=mode,
                dry_run=effective_dry,
            )

        if handler == "k8s_node_drain":
            from app.services import k8s_ops

            name = str(params.get("name") or "").strip()
            if not name:
                return {"ok": False, "dry_run": dry, "error": "name is required"}
            effective_dry = dry or bool(params.get("dry_run"))
            # Temporarily force env dry_run for this call when params/env pin it.
            # k8s_ops.drain_node reads EnvContext.dry_run from the environment row /
            # global settings; override via a shallow pin on the env object.
            ignore_daemonsets = params.get("ignore_daemonsets", True)
            delete_emptydir = params.get("delete_emptydir", False)
            grace_period = int(params.get("grace_period") or 30)
            timeout = int(params.get("timeout") or 90)
            log(f"k8s.node.drain node={name} dry_run={effective_dry}")
            if env is None:
                return {"ok": False, "dry_run": effective_dry, "error": "environment required"}
            prev = getattr(env, "dry_run", None)
            try:
                if effective_dry:
                    env.dry_run = True
                return k8s_ops.drain_node(
                    env,
                    self.settings,
                    name=name,
                    ignore_daemonsets=bool(ignore_daemonsets),
                    delete_emptydir=bool(delete_emptydir),
                    grace_period=grace_period,
                    timeout=timeout,
                )
            finally:
                env.dry_run = prev

        if handler == "k8s_apply":
            from app.services import k8s_ops

            yaml_text = params.get("yaml")
            if not isinstance(yaml_text, str) or not yaml_text.strip():
                return {"ok": False, "dry_run": dry, "error": "yaml is required"}
            effective_dry = dry or bool(params.get("dry_run"))
            log(f"k8s.apply bytes={len(yaml_text)} dry_run={effective_dry}")
            if env is None:
                return {"ok": False, "dry_run": effective_dry, "error": "environment required"}
            prev = getattr(env, "dry_run", None)
            try:
                if effective_dry:
                    env.dry_run = True
                return k8s_ops.apply_yaml(env, self.settings, text=yaml_text)
            finally:
                env.dry_run = prev

        if handler == "console_backup":
            from app.paths import host_prefix, package_root

            script = package_root() / "scripts" / "backup-console.sh"
            backup_dir = str(
                params.get("backup_dir") or (host_prefix() / "backups" / "console")
            ).strip()
            keep_raw = params.get("keep", 7)
            try:
                keep = int(keep_raw)
            except (TypeError, ValueError):
                return {
                    "ok": False,
                    "error": f"keep must be a positive integer (got: {keep_raw!r})",
                    "returncode": 2,
                }
            if keep < 1:
                return {
                    "ok": False,
                    "error": f"keep must be >= 1 (got: {keep})",
                    "returncode": 2,
                }
            config = str(self.settings.config_path)
            effective_dry = dry or bool(params.get("dry_run"))
            argv = [
                "bash",
                str(script),
                backup_dir,
                "--config",
                config,
                "--keep",
                str(keep),
            ]
            log(
                f"[console.backup] dir={backup_dir} keep={keep} "
                f"config={config} dry_run={effective_dry}"
            )
            if effective_dry:
                self.write_audit(
                    actor=job.created_by or "system",
                    action="console.backup",
                    resource_type="console",
                    resource_id="database",
                    environment_id=None,
                    details={
                        "script": "scripts/backup-console.sh",
                        "backup_dir": backup_dir,
                        "keep": keep,
                        "config": config,
                        "dry_run": True,
                    },
                    success=True,
                )
                return {
                    "ok": True,
                    "dry_run": True,
                    "backup_dir": backup_dir,
                    "keep": keep,
                    "config": config,
                    "message": (
                        f"would run backup-console.sh → {backup_dir} "
                        f"(keep={keep}); no files written"
                    ),
                }
            if not script.is_file():
                return {
                    "ok": False,
                    "error": f"backup script not found: {script}",
                    "returncode": 2,
                }
            result = bridge.run_command(
                argv,
                cwd=package_root(),
                timeout=timeout,
                dry_run=False,
                extra_env=extra_env,
                ssh_target=None,
                agent_env_id=None,
                log=log,
            )
            rc = result.get("returncode")
            ok = rc == 0
            self.write_audit(
                actor=job.created_by or "system",
                action="console.backup",
                resource_type="console",
                resource_id="database",
                environment_id=None,
                details={
                    "script": "scripts/backup-console.sh",
                    "backup_dir": backup_dir,
                    "keep": keep,
                    "returncode": rc,
                    "dry_run": False,
                },
                success=ok,
            )
            return {
                "ok": ok,
                "returncode": rc,
                "backup_dir": backup_dir,
                "keep": keep,
                "dry_run": False,
                "message": (
                    f"console backup complete → {backup_dir} (keep={keep})"
                    if ok
                    else f"console backup failed (rc={rc}) — see log"
                ),
            }

        if handler == "console_vacuum":
            from app.paths import package_root

            script = package_root() / "scripts" / "vacuum-console.sh"
            config = str(self.settings.config_path)
            effective_dry = dry or bool(params.get("dry_run"))
            argv = ["bash", str(script), "--config", config]
            if effective_dry:
                argv.append("--dry-run")
            log(
                f"[console.vacuum] config={config} dry_run={effective_dry} "
                f"script={script}"
            )
            if not script.is_file():
                return {
                    "ok": False,
                    "error": f"vacuum script not found: {script}",
                    "returncode": 2,
                    "dry_run": effective_dry,
                }
            # Script owns dry-run vs live: --dry-run is stats-only (no VACUUM).
            # Always invoke locally on the console host (never SSH/agent).
            result = bridge.run_command(
                argv,
                cwd=package_root(),
                timeout=timeout,
                dry_run=False,
                extra_env=extra_env,
                ssh_target=None,
                agent_env_id=None,
                log=log,
            )
            rc = result.get("returncode")
            ok = rc == 0
            self.write_audit(
                actor=job.created_by or "system",
                action="console.vacuum",
                resource_type="console",
                resource_id="database",
                environment_id=None,
                details={
                    "script": "scripts/vacuum-console.sh",
                    "config": config,
                    "returncode": rc,
                    "dry_run": effective_dry,
                },
                success=ok,
            )
            message = (
                "console vacuum dry-run (freelist/page stats only) — see log"
                if effective_dry and ok
                else (
                    "console vacuum complete"
                    if ok
                    else f"console vacuum failed (rc={rc}) — see log"
                )
            )
            return {
                "ok": ok,
                "returncode": rc,
                "dry_run": effective_dry,
                "message": message,
            }

        if handler == "console_release":
            from app.paths import package_root

            script = package_root() / "scripts" / "compile-console.sh"
            log(f"[release] compile + publish via {script}")
            if dry:
                return {
                    "ok": True,
                    "dry_run": True,
                    "message": "would compile the Console binary into dist/",
                }
            if not script.is_file():
                return {
                    "ok": False,
                    "error": f"compile script not found: {script}",
                    "returncode": 2,
                }
            result = bridge.run_command(
                ["bash", str(script)],
                cwd=package_root(),
                timeout=timeout,
                dry_run=False,
                extra_env=extra_env,
                ssh_target=None,
                agent_env_id=None,
                log=log,
            )
            rc = result.get("returncode")
            ok = rc == 0
            self.write_audit(
                actor=job.created_by or "system",
                action="console.release",
                resource_type="console",
                resource_id="binary",
                environment_id=None,
                details={"returncode": rc, "dry_run": False},
                success=ok,
            )
            return {
                "ok": ok,
                "returncode": rc,
                "message": (
                    "binary written under dist/"
                    if ok
                    else f"compile/publish failed (rc={rc}) — see log"
                ),
            }

        raise RuntimeError(f"No dispatcher for handler: {handler}")


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
