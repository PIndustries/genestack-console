"""Process queued jobs — callable from API or CLI.

Jobs are queued by default (execute_operation); this worker claims them
oldest-first and runs them via JobRunner.

Claim semantics (poor-man's SKIP LOCKED for SQLite): a candidate is claimed
with a single ``UPDATE jobs SET status='running' WHERE id=? AND
status='queued'``; the worker proceeds only when rowcount == 1, so two
workers can never run the same job.

Per-env mutating lock at claim time: a queued mutating job whose
environment already has a *running* mutating job is skipped this tick (left
queued) and retried on a later pass — submission-time rejection
(ConflictError) lives in execute_operation.

Usage:
    python -m app.worker.runner
    python -m app.worker.runner --job-id <uuid>
    python -m app.worker.runner --once
    python -m app.worker.runner --daemon --interval 5 [--no-collector]
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import SessionLocal, init_db
from app.models import Environment, Job, JobStatus
from app.services import collector, events, metrics
from app.services.catalog import mutating_operation_ids
from app.services.job_runner import (
    JobRunner,
    recover_abandoned_running_jobs,
    recover_stale_jobs,
)

log = logging.getLogger(__name__)

# Probes run async to the job loop so a hung kubectl never stalls job draining.
_collector_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="collector")

_PRUNE_INTERVAL_SECONDS = 3600.0

# Retention sweep (jobs/audit/session_tokens/agent_commands/alert_events/
# env_config_versions) runs daily-ish, independent of the collector toggle —
# job and relay rows grow even when cluster probing is off.
_RETENTION_SWEEP_INTERVAL_SECONDS = 86400.0


def _publish_job_event(job: Job) -> None:
    """Fan a job status transition out to 'jobs' subscribers (no-op pre-init)."""
    status = job.status.value if isinstance(job.status, JobStatus) else str(job.status)
    events.publish_sync(
        "jobs",
        {
            "type": "job",
            "id": job.id,
            "environment_id": job.environment_id,
            "operation": job.operation,
            "status": status,
            "dry_run": job.dry_run,
        },
    )


def claim_job(db: Session, job_id: str) -> Job | None:
    """Atomically flip a queued job to running; None if already claimed."""
    result = db.execute(
        update(Job)
        .where(Job.id == job_id)
        .where(Job.status == JobStatus.queued)
        .values(status=JobStatus.running)
    )
    db.commit()
    if result.rowcount != 1:
        db.rollback()
        return None
    job = db.get(Job, job_id)
    if job is not None:
        _publish_job_event(job)
    return job


def running_mutating_conflict(db: Session, job: Job) -> Job | None:
    """Another *running* mutating job for the same environment, if any."""
    if not job.environment_id or job.operation not in mutating_operation_ids():
        return None
    stmt = (
        select(Job)
        .where(Job.environment_id == job.environment_id)
        .where(Job.status == JobStatus.running)
    )
    for other in db.scalars(stmt).all():
        if other.id != job.id and other.operation in mutating_operation_ids():
            return other
    return None


def _queued_candidate_ids(db: Session, limit: int) -> list[str]:
    stmt = (
        select(Job.id)
        .where(Job.status == JobStatus.queued)
        .order_by(Job.created_at.asc())
        .limit(limit)
    )
    return list(db.scalars(stmt).all())


def _run_one(job_id: str) -> str | None:
    """Claim and run one queued job in its own session.

    Returns the final status value, or None when the job was skipped
    (claimed by another worker, or blocked by a running mutating job for
    the same environment). Exceptions never propagate — one bad job must
    not kill the loop.
    """
    db = SessionLocal()
    try:
        candidate = db.get(Job, job_id)
        if candidate is None or candidate.status != JobStatus.queued:
            return None
        conflict = running_mutating_conflict(db, candidate)
        if conflict is not None:
            print(
                f"Skipping job {candidate.id}: env has running mutating job "
                f"{conflict.id} ({conflict.operation})"
            )
            return None
        job = claim_job(db, job_id)
        if job is None:
            print(f"Job {job_id} already claimed by another worker")
            return None
        try:
            JobRunner(db).run_job(job)
        except Exception as exc:  # noqa: BLE001
            print(f"Job {job_id} raised unexpectedly: {exc}", file=sys.stderr)
        _publish_job_event(job)
        print(f"Processed job {job.id} status={job.status.value}")
        return job.status.value
    finally:
        db.close()


def process_queued_jobs(
    *,
    job_id: str | None = None,
    limit: int = 10,
    once: bool = True,
    abandon_running: bool = True,
) -> int:
    """
    Process queued jobs. Returns number of jobs processed.

    When job_id is set, only that job is run (must be queued or re-runnable).

    ``abandon_running`` defaults True so a one-shot/loop worker fails any
    running jobs left by a previous process. The daemon passes False on
    subsequent ticks so it does not abandon jobs it just claimed.
    """
    init_db()

    # Restart recovery: expire timed-out *running* jobs; optionally abandon
    # all remaining running work from a previous worker process.
    db = SessionLocal()
    try:
        recovered = recover_stale_jobs(db)
        if recovered:
            print(f"Recovered {recovered} stale job(s)")
        if abandon_running:
            abandoned = recover_abandoned_running_jobs(db)
            if abandoned:
                print(f"Abandoned {abandoned} running job(s) from previous worker")
    finally:
        db.close()

    if job_id:
        db = SessionLocal()
        try:
            job = db.get(Job, job_id)
            if not job:
                print(f"Job not found: {job_id}", file=sys.stderr)
                return 0
            if job.status == JobStatus.running:
                print(f"Job already running: {job_id}", file=sys.stderr)
                return 0
            # Allow re-run of queued; if already finished, re-queue
            if job.status in (JobStatus.success, JobStatus.failed):
                job.status = JobStatus.queued
                job.error = None
                job.started_at = None
                job.finished_at = None
                db.commit()
            JobRunner(db).run_job(job)
            _publish_job_event(job)
            print(f"Processed job {job.id} status={job.status.value}")
            return 1
        finally:
            db.close()

    processed = 0
    while True:
        db = SessionLocal()
        try:
            candidate_ids = _queued_candidate_ids(db, limit)
        finally:
            db.close()
        if not candidate_ids:
            break
        claimed_this_pass = 0
        for cid in candidate_ids:
            if _run_one(cid) is not None:
                processed += 1
                claimed_this_pass += 1
        if once or claimed_this_pass == 0:
            # Loop mode stops when a full pass claims nothing (queue empty
            # or everything blocked on a running mutating job).
            break
    return processed


class CollectorScheduler:
    """Due-env cluster probing on the shared executor, async to the job loop.

    Every tick: reap finished probe futures, submit probes for environments
    whose last completed probe is >= collector_interval_seconds old (never
    re-submitting an env already in flight), and prune old snapshots hourly.
    """

    def __init__(
        self,
        settings: Settings,
        db_factory=SessionLocal,
        executor: ThreadPoolExecutor | None = None,
    ):
        self.settings = settings
        self.db_factory = db_factory
        self.executor = executor or _collector_executor
        self._in_flight: dict[str, Future] = {}
        self._last_probe: dict[str, float] = {}
        self._last_prune = -_PRUNE_INTERVAL_SECONDS  # first tick prunes

    def startup(self) -> None:
        """Seed default alert rules once; a broken alerts module is ignored."""
        db = self.db_factory()
        try:
            from app.services import alerts

            alerts.seed_default_rules(db)
        except Exception as exc:  # noqa: BLE001
            log.warning("alerts.seed_default_rules failed: %s", exc)
        finally:
            db.close()

    def _probe_task(self, env_id: str) -> None:
        db = self.db_factory()
        try:
            env = db.get(Environment, env_id)
            if env is not None:
                collector.probe_environment(db, env, self.settings)
        finally:
            db.close()

    def _reap(self, now: float) -> None:
        for env_id, future in list(self._in_flight.items()):
            if not future.done():
                continue
            self._in_flight.pop(env_id)
            self._last_probe[env_id] = now
            exc = future.exception()
            if exc is not None:
                log.warning("collector: probe of env %s raised: %s", env_id, exc)

    def _launch_due(self, now: float) -> None:
        db = self.db_factory()
        try:
            env_ids = list(db.scalars(select(Environment.id)).all())
        finally:
            db.close()
        interval = self.settings.collector_interval_seconds
        for env_id in env_ids:
            if env_id in self._in_flight:
                continue
            last = self._last_probe.get(env_id)
            if last is not None and now - last < interval:
                continue
            self._in_flight[env_id] = self.executor.submit(self._probe_task, env_id)

    def _prune(self, now: float) -> None:
        if now - self._last_prune < _PRUNE_INTERVAL_SECONDS:
            return
        self._last_prune = now
        db = self.db_factory()
        try:
            deleted = collector.prune_old_snapshots(db, self.settings)
            if deleted:
                log.info("collector: pruned %d old snapshot(s)", deleted)
            metrics_deleted = metrics.prune_old_metrics(db, self.settings)
            if metrics_deleted:
                log.info("collector: pruned %d old metric sample(s)", metrics_deleted)
        except Exception as exc:  # noqa: BLE001
            log.warning("collector: snapshot prune failed: %s", exc)
        finally:
            db.close()

    def tick(self) -> None:
        now = time.monotonic()
        self._reap(now)
        self._launch_due(now)
        self._prune(now)


def run_retention_sweep_safe(settings: Settings) -> None:
    """Run the daily retention sweep; a failed sweep never kills the loop."""
    from app.services.retention import run_retention_sweep

    db = SessionLocal()
    try:
        counts = run_retention_sweep(db, settings)
        total = sum(counts.values())
        if total:
            log.info("retention sweep deleted %d row(s): %s", total, counts)
    except Exception as exc:  # noqa: BLE001
        log.warning("retention sweep failed: %s", exc)
    finally:
        db.close()


def run_daemon(
    *,
    interval: float,
    limit: int,
    collector_enabled: bool,
    max_ticks: int | None = None,
) -> int:
    """Job-drain loop plus (optionally) the collector schedule.

    max_ticks is a test hook: stop after N ticks instead of forever.
    """
    settings = get_settings()
    scheduler = None
    if collector_enabled and settings.collector_enabled:
        scheduler = CollectorScheduler(settings)
        scheduler.startup()
        print(
            f"Collector enabled (probe every {settings.collector_interval_seconds}s, "
            f"retention {settings.collector_retention_hours}h)"
        )
    print(f"Worker daemon started (poll every {interval}s)")
    ticks = 0
    last_sweep = -_RETENTION_SWEEP_INTERVAL_SECONDS  # first tick sweeps
    abandon_once = True
    while True:
        # Abandon prior-worker running jobs on the first tick only; later
        # ticks must not fail jobs this process claimed.
        process_queued_jobs(
            limit=limit, once=False, abandon_running=abandon_once
        )
        abandon_once = False
        if scheduler is not None:
            scheduler.tick()
        now = time.monotonic()
        if now - last_sweep >= _RETENTION_SWEEP_INTERVAL_SECONDS:
            last_sweep = now
            run_retention_sweep_safe(settings)
        ticks += 1
        if max_ticks is not None and ticks >= max_ticks:
            return 0
        time.sleep(interval)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Genestack Console job worker")
    parser.add_argument("--job-id", help="Process a single job by id")
    parser.add_argument("--limit", type=int, default=10, help="Max jobs per batch")
    parser.add_argument(
        "--once",
        action="store_true",
        default=True,
        help="Process one batch and exit (default)",
    )
    parser.add_argument(
        "--loop",
        action="store_true",
        help="Process until no queued job can be claimed",
    )
    parser.add_argument(
        "--daemon",
        action="store_true",
        help="Run forever, polling the queue every --interval seconds",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=5.0,
        help="Seconds between queue polls in --daemon mode (default 5)",
    )
    parser.add_argument(
        "--no-collector",
        action="store_true",
        help="Disable cluster snapshot collection in --daemon mode",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify the worker can reach the job database, then exit 0 (container healthcheck)",
    )
    args = parser.parse_args(argv)

    if args.check:
        from sqlalchemy import text

        from app.db import SessionLocal

        db = SessionLocal()
        try:
            db.execute(text("SELECT 1"))
        finally:
            db.close()
        print("worker check ok")
        return 0

    if args.daemon:
        return run_daemon(
            interval=args.interval,
            limit=args.limit,
            collector_enabled=not args.no_collector,
        )

    once = not args.loop
    n = process_queued_jobs(job_id=args.job_id, limit=args.limit, once=once)
    print(f"Done. processed={n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
