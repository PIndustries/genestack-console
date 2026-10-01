"""Per-environment mutating-job mutex (env_mutexes) tests.

The mutex row (PRIMARY KEY environment_id) is the authoritative guard: two
racing submitters can both pass the SELECT pre-check, but only one INSERT
wins; the loser gets a ConflictError naming the real holder.
"""

from __future__ import annotations

import threading
import uuid

import pytest


@pytest.fixture(autouse=True, scope="module")
def _ensure_tables():
    """Create tables so the file also passes when run standalone."""
    from app.db import init_db

    init_db()


def _make_env(name_prefix: str = "mutex-env") -> str:
    from app.db import SessionLocal
    from app.models import Environment

    db = SessionLocal()
    try:
        env = Environment(name=f"{name_prefix}-{uuid.uuid4().hex[:8]}")
        db.add(env)
        db.commit()
        db.refresh(env)
        return env.id
    finally:
        db.close()


def _mutex_row(environment_id: str):
    from app.db import SessionLocal
    from app.models import EnvMutex

    db = SessionLocal()
    try:
        return db.get(EnvMutex, environment_id)
    finally:
        db.close()


def _seed_terminal_mutex_job(environment_id: str, status: str) -> str:
    from app.db import SessionLocal
    from app.models import Job, JobStatus

    db = SessionLocal()
    try:
        job = Job(
            environment_id=environment_id,
            operation="host.basic_ops",
            params={"action": "ping"},
            status=JobStatus(status),
            log_text="",
            created_by="mutex-test",
        )
        db.add(job)
        db.commit()
        return job.id
    finally:
        db.close()


def _insert_mutex(environment_id: str, job_id: str) -> None:
    from app.db import SessionLocal
    from app.models import EnvMutex

    db = SessionLocal()
    try:
        db.add(EnvMutex(environment_id=environment_id, job_id=job_id))
        db.commit()
    finally:
        db.close()


def test_terminal_holder_blocks_via_mutex_row():
    """A mutex row outlives the SELECT pre-check and is enforced atomically.

    The holder job is already terminal (failed), so find_mutating_conflict —
    the fast pre-check — sees nothing; the authoritative PK insert in
    acquire_env_mutex is what rejects the second submitter with a
    ConflictError naming the real holder.
    """
    from app.db import SessionLocal
    from app.services.job_runner import ConflictError, execute_operation

    env_id = _make_env()
    holder = _seed_terminal_mutex_job(env_id, "failed")
    _insert_mutex(env_id, holder)

    db = SessionLocal()
    try:
        with pytest.raises(ConflictError) as exc:
            execute_operation(
                db,
                operation="host.basic_ops",
                params={"action": "ping"},
                environment_id=env_id,
            )
        assert exc.value.job_id == holder
        assert _mutex_row(env_id).job_id == holder  # holder's row untouched
    finally:
        db.close()


def test_run_sync_releases_mutex(monkeypatch):
    """A synchronous job releases its mutex atomically with the terminal row."""
    from app.db import SessionLocal
    from app.services import genestack_bridge as bridge
    from app.services.job_runner import execute_operation

    def fake_run_playbook(*args, **kwargs):
        return {"ok": True, "returncode": 0, "message": "fake", "dry_run": True}

    monkeypatch.setattr(bridge, "run_playbook", fake_run_playbook)

    env_id = _make_env()
    db = SessionLocal()
    try:
        job = execute_operation(
            db,
            operation="host.basic_ops",
            params={"action": "ping"},
            environment_id=env_id,
            run_sync=True,
        )
        assert job.status.value == "success", job.error
    finally:
        db.close()
    assert _mutex_row(env_id) is None


def test_run_sync_failure_releases_mutex(monkeypatch):
    """A failing synchronous job still releases its mutex."""
    from app.db import SessionLocal
    from app.services import genestack_bridge as bridge
    from app.services.job_runner import execute_operation

    def fake_run_playbook(*args, **kwargs):
        return {"ok": False, "returncode": 1, "message": "boom", "dry_run": True}

    monkeypatch.setattr(bridge, "run_playbook", fake_run_playbook)

    env_id = _make_env()
    db = SessionLocal()
    try:
        job = execute_operation(
            db,
            operation="host.basic_ops",
            params={"action": "ping"},
            environment_id=env_id,
            run_sync=True,
        )
        assert job.status.value == "failed"
    finally:
        db.close()
    assert _mutex_row(env_id) is None


def test_concurrent_submit_exactly_one_winner(monkeypatch):
    """Two racing submitters: exactly one job wins; the other gets ConflictError."""
    from app.db import SessionLocal
    from app.services import genestack_bridge as bridge
    from app.services.job_runner import ConflictError, execute_operation

    def slow_run_playbook(*args, **kwargs):
        return {"ok": True, "returncode": 0, "message": "fake", "dry_run": True}

    monkeypatch.setattr(bridge, "run_playbook", slow_run_playbook)

    env_id = _make_env()
    barrier = threading.Barrier(2)
    outcomes: list = []

    def submit():
        barrier.wait()
        db = SessionLocal()
        try:
            outcomes.append(
                (
                    "ok",
                    execute_operation(
                        db,
                        operation="host.basic_ops",
                        params={"action": "ping"},
                        environment_id=env_id,
                        run_sync=True,
                    ),
                )
            )
        except ConflictError as exc:
            outcomes.append(("conflict", exc))
        finally:
            db.close()

    threads = [threading.Thread(target=submit) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(outcomes) == 2
    oks = [j for kind, j in outcomes if kind == "ok"]
    conflicts = [exc for kind, exc in outcomes if kind == "conflict"]
    assert len(oks) == 1
    assert len(conflicts) == 1
    # The loser is pointed at the real winning job.
    assert conflicts[0].job_id == oks[0].id
    # The winner ran to success and released the lock.
    assert oks[0].status.value == "success", oks[0].error
    assert _mutex_row(env_id) is None


def test_recover_stale_jobs_sweeps_mutex():
    """A mutex row whose holder is terminal is removed by startup recovery.

    Uses an isolated in-memory DB so recovery never touches the shared
    session database (it would mark unrelated stale jobs failed there).
    """
    from sqlalchemy.orm import Session

    from app.db import create_db_engine
    from app.models import Base, EnvMutex, Environment, Job, JobStatus
    from app.services import job_runner as jr

    engine = create_db_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    db = Session(engine, expire_on_commit=False)
    try:
        env = Environment(name="mutex-sweep-env")
        db.add(env)
        db.commit()
        db.refresh(env)
        job = Job(
            environment_id=env.id,
            operation="host.basic_ops",
            params={"action": "ping"},
            status=JobStatus.failed,
            log_text="",
            created_by="mutex-sweep-test",
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        db.add(EnvMutex(environment_id=env.id, job_id=job.id))
        db.commit()

        assert db.get(EnvMutex, env.id) is not None
        recovered = jr.recover_stale_jobs(db)
        # The job is already terminal, so nothing is "recovered"…
        assert recovered == 0
        # …but the stale mutex row is swept in the same transaction.
        assert db.get(EnvMutex, env.id) is None
    finally:
        db.close()
        engine.dispose()
