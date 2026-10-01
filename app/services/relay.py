"""DB-backed event relay for the API process.

The event bus (app/services/events.py) is strictly in-process, but the
publishers — collector probes, alert transitions, job status changes — run in
the worker daemon process. Their publish_sync calls never reach the uvicorn
process where SSE subscribers live. SQLite is the shared medium, so this
relay polls it every few seconds and re-publishes new/changed rows onto the
local bus, mirroring the original payload shapes exactly.

Boot high-water marks (current table maxima) are captured at startup WITHOUT
publishing, so an API restart never storms subscribers with historical rows.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models import (
    AlertEvent,
    AlertRule,
    BaremetalNode,
    EnvConfigVersion,
    ClusterSnapshot,
    Environment,
    Job,
    JobStatus,
    MetricSample,
)
from app.services import events
from app.services.workflow import parse_deploy_log

log = logging.getLogger(__name__)

# Hard cap on the tracked {job_id: status} map so the jobs poll stays bounded
# as the table grows; least-recently-observed ids are evicted first.
_MAX_TRACKED_JOBS = 500


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _status_value(status: JobStatus | str) -> str:
    return status.value if isinstance(status, JobStatus) else str(status)


def _stem_item(raw: Any) -> str:
    s = str(raw or "").strip()
    s = s.replace("bin/", "").removeprefix("install-")
    if s.endswith(".sh"):
        s = s[:-3]
    return s


class DBRelay:
    """Poll the DB for new telemetry rows and re-publish them on the bus."""

    def __init__(self, session_factory: Callable[[], Session], interval: float = 3.0):
        self._session_factory = session_factory
        self._interval = interval
        self._snapshot_hwm = 0
        self._metric_hwm = 0
        self._topology_state: dict[str, tuple] = {}
        self._alert_hwm = 0
        # Status of alert events already announced, keyed by event id.
        self._alert_status: dict[int, str] = {}
        # Status of non-queued jobs observed so far, keyed by job id;
        # insertion-ordered and capped at _MAX_TRACKED_JOBS.
        self._job_status: dict[str, str] = {}
        self._job_log_len: dict[str, int] = {}
        self._job_activity: dict[str, dict[str, Any]] = {}
        self._live = False
        # "Recently touched" window for new-job discovery in the jobs poll.
        # Comfortably larger than the poll interval so rows committed between
        # polls are never missed.
        self._job_window_seconds = max(interval * 4, 30.0)
        self._live_interval = 0.25

    async def initialize(self) -> None:
        """Seed high-water marks from current table state; publishes nothing."""
        await asyncio.to_thread(self._initialize_sync)

    def _initialize_sync(self) -> None:
        db = self._session_factory()
        try:
            self._snapshot_hwm = db.scalar(select(func.max(ClusterSnapshot.id))) or 0
            self._metric_hwm = db.scalar(select(func.max(MetricSample.id))) or 0
            self._topology_state = self._topology_fingerprints(db)
            self._alert_hwm = db.scalar(select(func.max(AlertEvent.id))) or 0
            # Seed only the most recent non-queued jobs (capped) so boot state
            # stays bounded; nothing is published either way.
            recent = db.scalars(
                select(Job)
                .where(Job.status != JobStatus.queued)
                .order_by(Job.created_at.desc())
                .limit(_MAX_TRACKED_JOBS)
            ).all()
            self._job_status = {
                job.id: _status_value(job.status) for job in reversed(recent)
            }
            self._job_log_len = {
                job.id: len(job.log_text or "") for job in reversed(recent)
            }
        finally:
            db.close()

    async def run(self) -> None:
        """Poll loop: one iteration, sleep, repeat; cancel-clean."""
        await self.initialize()
        try:
            while True:
                await self.poll_once()
                delay = self._live_interval if self._live else self._interval
                await asyncio.sleep(delay)
        except asyncio.CancelledError:
            log.info("stream DB relay stopped")
            raise

    async def poll_once(self) -> None:
        """One poll iteration: collect changes off-loop, publish on-loop.

        A failed poll is logged and skipped — the relay keeps running.
        """
        try:
            pending = await asyncio.to_thread(self._poll_sync)
        except Exception:  # noqa: BLE001
            log.exception("stream DB relay poll failed")
            return
        for topic, payload in pending:
            await events.publish(topic, payload)

    # --------------------------------------------------------- sync DB work

    def _poll_sync(self) -> list[tuple[str, dict[str, Any]]]:
        out: list[tuple[str, dict[str, Any]]] = []
        db = self._session_factory()
        try:
            self._poll_snapshots(db, out)
            self._poll_alerts(db, out)
            self._poll_jobs(db, out)
            self._poll_topology(db, out)
            self._poll_metrics(db, out)
        finally:
            db.close()
        return out

    @staticmethod
    def _topology_fingerprints(db: Session) -> dict[str, tuple]:
        # Select safe metadata only; no secret-bearing model is serialized.
        configs = dict(
            db.execute(
                select(
                    EnvConfigVersion.environment_id, func.max(EnvConfigVersion.version)
                ).group_by(EnvConfigVersion.environment_id)
            ).all()
        )
        hardware: dict[str, list[tuple]] = {}
        for env_id, node_id, name, state, updated in db.execute(
            select(
                BaremetalNode.environment_id,
                BaremetalNode.id,
                BaremetalNode.name,
                BaremetalNode.state,
                BaremetalNode.updated_at,
            ).order_by(BaremetalNode.id)
        ):
            hardware.setdefault(env_id, []).append((node_id, name, state, updated))
        return {
            env_id: (
                name,
                updated,
                configs.get(env_id),
                tuple(hardware.get(env_id, [])),
            )
            for env_id, name, updated in db.execute(
                select(Environment.id, Environment.name, Environment.updated_at)
            )
        }

    def _poll_topology(self, db: Session, out: list[tuple[str, dict]]) -> None:
        current = self._topology_fingerprints(db)
        for env_id in sorted(current.keys() | self._topology_state.keys()):
            if current.get(env_id) != self._topology_state.get(env_id):
                payload = {
                    "type": "topology",
                    "environment_id": env_id,
                    "reason": "stored_inventory_changed",
                }
                out.append((f"env:{env_id}", payload))
                out.append(("fleet", payload))
        self._topology_state = current

    def _poll_metrics(self, db: Session, out: list[tuple[str, dict]]) -> None:
        rows = db.execute(
            select(
                MetricSample.environment_id,
                func.count(MetricSample.id),
                func.max(MetricSample.id),
            )
            .where(MetricSample.id > self._metric_hwm)
            .group_by(MetricSample.environment_id)
        ).all()
        for env_id, count, maximum in rows:
            out.append(
                (
                    "metrics",
                    {"type": "metrics", "environment_id": env_id, "samples": count},
                )
            )
            self._metric_hwm = max(self._metric_hwm, maximum)

    def _poll_snapshots(self, db: Session, out: list[tuple[str, dict]]) -> None:
        rows = db.scalars(
            select(ClusterSnapshot)
            .where(ClusterSnapshot.id > self._snapshot_hwm)
            .order_by(ClusterSnapshot.id.asc())
        ).all()
        for snap in rows:
            taken_at = (
                snap.taken_at.isoformat() if snap.taken_at else _utcnow().isoformat()
            )
            out.append(
                (
                    f"env:{snap.environment_id}",
                    {
                        "type": "snapshot",
                        "environment_id": snap.environment_id,
                        "health": snap.health,
                        "taken_at": taken_at,
                        "summary": snap.summary,
                    },
                )
            )
            out.append(
                (
                    "fleet",
                    {
                        "type": "fleet",
                        "environment_id": snap.environment_id,
                        "health": snap.health,
                        "taken_at": taken_at,
                    },
                )
            )
            self._snapshot_hwm = snap.id

    def _poll_alerts(self, db: Session, out: list[tuple[str, dict]]) -> None:
        # Tracked firing events (announced in earlier polls) that may have
        # resolved since; captured before this poll's new rows are added.
        tracked = [eid for eid, st in self._alert_status.items() if st == "firing"]

        new_events = db.scalars(
            select(AlertEvent)
            .where(AlertEvent.id > self._alert_hwm)
            .order_by(AlertEvent.id.asc())
        ).all()
        for event in new_events:
            self._alert_hwm = event.id
            out.append(("alerts", self._alert_payload(db, "alert_fired", event)))
            if event.status == "resolved":
                # Fired and resolved between polls: relay both transitions.
                out.append(("alerts", self._alert_payload(db, "alert_resolved", event)))
            else:
                self._alert_status[event.id] = event.status

        if not tracked:
            return
        current = {
            event.id: event
            for event in db.scalars(
                select(AlertEvent).where(AlertEvent.id.in_(tracked))
            ).all()
        }
        for event_id in tracked:
            event = current.get(event_id)
            if event is None:
                # Row deleted (e.g. rule cascade): stop tracking silently.
                self._alert_status.pop(event_id, None)
                continue
            if event.status == "resolved":
                out.append(("alerts", self._alert_payload(db, "alert_resolved", event)))
                self._alert_status.pop(event_id, None)

    def _alert_payload(
        self, db: Session, kind: str, event: AlertEvent
    ) -> dict[str, Any]:
        rule = db.get(AlertRule, event.rule_id)
        env = db.get(Environment, event.environment_id)
        return {
            "type": kind,
            "event_id": event.id,
            "rule_id": event.rule_id,
            "rule_name": rule.name if rule else None,
            "condition": rule.condition if rule else None,
            "severity": rule.severity if rule else None,
            "environment_id": event.environment_id,
            "environment_name": env.name if env else None,
            "fired_at": event.fired_at.isoformat() if event.fired_at else None,
            "resolved_at": event.resolved_at.isoformat() if event.resolved_at else None,
            "details": event.details,
        }

    def _poll_jobs(self, db: Session, out: list[tuple[str, dict]]) -> None:
        # Bound the scan as the jobs table grows: only rows we already track
        # (to catch their transitions) or rows touched within the recent
        # window (to discover new/late-starting jobs). The jobs table has no
        # updated_at column, so "touched" means created/started/finished
        # inside the window — every status transition stamps one of those.
        cutoff = _utcnow() - timedelta(seconds=self._job_window_seconds)
        conditions = [
            Job.created_at >= cutoff,
            Job.started_at >= cutoff,
            Job.finished_at >= cutoff,
        ]
        if self._job_status:
            conditions.append(Job.id.in_(list(self._job_status)))
        rows = db.scalars(
            select(Job)
            .where(Job.status != JobStatus.queued)
            .where(or_(*conditions))
            .order_by(Job.created_at.asc())
        ).all()
        live = False
        for job in rows:
            status = _status_value(job.status)
            if status == "running":
                live = True
            log_text = job.log_text or ""
            log_len = len(log_text)
            prev_status = self._job_status.get(job.id)
            prev_len = self._job_log_len.get(job.id)
            if prev_status != status:
                self._track_job(job.id, status)
                out.append(
                    (
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
                )
            if prev_len != log_len and log_len:
                delta = parse_deploy_log(log_text[int(prev_len or 0) :])
                prev_act = self._job_activity.get(job.id) or {}
                item = _stem_item(delta.get("item") or prev_act.get("item") or "")
                current = {
                    "stage": delta.get("stage") or prev_act.get("stage") or "",
                    "item": item,
                    "service": delta.get("service")
                    or item
                    or prev_act.get("service")
                    or "",
                    "pxe": bool(delta.get("pxe") or prev_act.get("pxe")),
                    "talos": bool(delta.get("talos") or prev_act.get("talos")),
                }
                self._job_activity[job.id] = current
                self._job_log_len[job.id] = log_len
                payload = {
                    "type": "job_log",
                    "id": job.id,
                    "environment_id": job.environment_id,
                    "operation": job.operation,
                    "status": status,
                    "log_len": log_len,
                    "log_tail": log_text[-4000:],
                    "current": current,
                    "error": job.error,
                }
                out.append(("jobs", payload))
                if job.environment_id:
                    out.append((f"env:{job.environment_id}", payload))
        self._live = live

    def _track_job(self, job_id: str, status: str) -> None:
        """Record job status, refreshing recency and enforcing the cap."""
        self._job_status.pop(job_id, None)
        self._job_status[job_id] = status
        while len(self._job_status) > _MAX_TRACKED_JOBS:
            # dicts are insertion-ordered: evict least-recently-observed.
            evicted = next(iter(self._job_status))
            self._job_status.pop(evicted)
            self._job_log_len.pop(evicted, None)
            self._job_activity.pop(evicted, None)


async def start_relay(
    app: Any, session_factory: Callable[[], Session], interval: float
) -> asyncio.Task:
    """Create the relay task and register it on app.state for shutdown.

    The lifespan shutdown path cancels ``app.state.stream_relay_task``.
    """
    relay = DBRelay(session_factory, interval)
    task = asyncio.create_task(relay.run(), name="stream-db-relay")
    app.state.stream_relay_task = task
    return task
