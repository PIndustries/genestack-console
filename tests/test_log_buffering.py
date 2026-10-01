"""Job-log commit buffering tests (append_log / flush_logs).

Proves the batched-commit redesign:
  (a) N appends produce far fewer commits than N,
  (b) all lines are present and ordered after the final flush,
  (c) a dispatch failure still flushes the full log,
  (d) the cancel-API pattern — a fresh runner appends once and the
      caller's own db.commit() (no flush_logs) — persists the line.
"""

from __future__ import annotations


def _log_lines(log_text: str) -> list[str]:
    """Strip the '[{iso}] ' prefix from each stored log line."""
    out = []
    for ln in (log_text or "").splitlines():
        if "] " in ln:
            out.append(ln.split("] ", 1)[1])
        else:
            out.append(ln)
    return out


def _fresh_job(db) -> "tuple":
    from app.services.job_runner import JobRunner

    runner = JobRunner(db)
    job = runner.create_job(operation="internal.health", created_by="logbuf-test")
    db.commit()
    return runner, job


def _job_row(job_id: str):
    from app.db import SessionLocal
    from app.models import Job

    db = SessionLocal()
    try:
        row = db.get(Job, job_id)
        db.expunge(row)
        return row
    finally:
        db.close()


def _finalize(job_id: str, db) -> None:
    """Mark a leftover job failed so no worker test can claim it.

    The suite shares one session-scoped SQLite DB and the worker polls by
    created_at, so these unit tests must not leak queued/running jobs.
    """
    from app.models import Job, JobStatus

    row = db.get(Job, job_id)
    if row is not None and row.status in (JobStatus.queued, JobStatus.running):
        row.status = JobStatus.failed
        db.commit()


# ------------------------------------------------------------- (a) batching


def test_appends_commit_far_less_often_than_lines():
    from app.db import SessionLocal
    from app.services.job_runner import LOG_FLUSH_LINES

    db = SessionLocal()
    job_id = ""
    try:
        runner, job = _fresh_job(db)
        job_id = job.id
        commits = 0
        real_commit = db.commit

        def counting_commit(*args, **kwargs):
            nonlocal commits
            commits += 1
            return real_commit(*args, **kwargs)

        db.commit = counting_commit
        n = LOG_FLUSH_LINES * 4
        for i in range(n):
            runner.append_log(job, f"line-{i}")
        # One commit per batch; n = 4 * LOG_FLUSH_LINES => n // LOG_FLUSH_LINES = 4
        assert commits == n // LOG_FLUSH_LINES
        # Product: 4 commits for 4 batches, relaxed threshold
        assert commits <= n // 4
        # Counters reset after each threshold flush.
        assert runner._log_lines.get(job_id, 0) == 0
        assert runner._log_bytes.get(job_id, 0) == 0
        _finalize(job_id, db)
    finally:
        db.close()


def test_single_huge_line_hits_byte_threshold():
    from app.db import SessionLocal
    from app.services.job_runner import LOG_FLUSH_BYTES

    db = SessionLocal()
    job_id = ""
    try:
        runner, job = _fresh_job(db)
        job_id = job.id
        commits = 0
        real_commit = db.commit

        def counting_commit(*args, **kwargs):
            nonlocal commits
            commits += 1
            return real_commit(*args, **kwargs)

        db.commit = counting_commit
        runner.append_log(job, "x" * (LOG_FLUSH_BYTES + 4096))
        assert commits == 1
        row = _job_row(job_id)
        assert "x" * 1000 in (row.log_text or "")
        _finalize(job_id, db)
    finally:
        db.close()


def test_flush_logs_noop_when_nothing_pending():
    from app.db import SessionLocal

    db = SessionLocal()
    job_id = ""
    try:
        runner, job = _fresh_job(db)
        job_id = job.id
        commits = 0
        real_commit = db.commit

        def counting_commit(*args, **kwargs):
            nonlocal commits
            commits += 1
            return real_commit(*args, **kwargs)

        db.commit = counting_commit
        runner.flush_logs(job)
        runner.flush_logs(job)
        assert commits == 0
        _finalize(job_id, db)
    finally:
        db.close()


# ------------------------------------------- (b) completeness after flush


def test_all_lines_present_and_ordered_after_final_flush():
    from app.db import SessionLocal

    db = SessionLocal()
    job_id = ""
    try:
        runner, job = _fresh_job(db)
        job_id = job.id
        n = 120  # spans two threshold flushes plus a partial tail
        for i in range(n):
            runner.append_log(job, f"line-{i:04d}")
        runner.flush_logs(job)
        _finalize(job_id, db)
    finally:
        db.close()

    row = _job_row(job_id)
    assert _log_lines(row.log_text) == [f"line-{i:04d}" for i in range(n)]


def test_partial_buffer_persists_only_after_flush():
    """Below-threshold lines stay uncommitted until flush_logs runs."""
    from app.db import SessionLocal

    db = SessionLocal()
    job_id = ""
    try:
        runner, job = _fresh_job(db)
        job_id = job.id
        runner.append_log(job, "pending-1")
        runner.append_log(job, "pending-2")
        assert runner._log_lines[job_id] == 2
        row = _job_row(job_id)
        assert row.log_text in (None, "")  # nothing committed yet
        runner.flush_logs(job)
        _finalize(job_id, db)
    finally:
        db.close()

    row = _job_row(job_id)
    assert _log_lines(row.log_text) == ["pending-1", "pending-2"]


# ------------------------------------------- (c) failure path flushes


def test_dispatch_failure_still_flushes_full_log(monkeypatch):
    from app.db import SessionLocal
    from app.models import JobStatus

    db = SessionLocal()
    job_id = ""
    try:
        runner, job = _fresh_job(db)
        job_id = job.id
        emitted: list[str] = []

        def fake_dispatch(op, j, env, log, ctx, **kwargs):  # noqa: ARG001
            for i in range(60):
                msg = f"boom-{i:04d}"
                emitted.append(msg)
                log(msg)
            raise RuntimeError("dispatch exploded")

        monkeypatch.setattr(runner, "_dispatch", fake_dispatch)
        job = runner.run_job(job)
        assert job.status == JobStatus.failed
    finally:
        db.close()

    row = _job_row(job_id)
    lines = _log_lines(row.log_text)
    assert lines[0] == "Starting operation=internal.health handler=internal_health"
    assert lines[1:61] == emitted
    assert any("dispatch exploded" in ln for ln in lines)
    # The 60 emitted lines include a full threshold batch (50) plus a tail
    # that only the finalization flush can persist.
    assert len(lines) >= 62


# ------------------------------------------- (d) cancel-API pattern


def test_external_commit_after_single_append_persists_line():
    """A fresh runner appends once; the caller's own db.commit() (no
    flush_logs) must still persist the line — routers/jobs.py cancel path.
    """
    from app.db import SessionLocal
    from app.services.job_runner import JobRunner

    db = SessionLocal()
    job_id = ""
    try:
        runner, job = _fresh_job(db)
        job_id = job.id
        runner.append_log(job, "earlier line")
        runner.flush_logs(job)

        # New runner over the same session, as the cancel endpoint does.
        other = JobRunner(db)
        other.append_log(job, "cancel line")
        db.commit()  # external commit — no flush_logs call
        _finalize(job_id, db)
    finally:
        db.close()

    row = _job_row(job_id)
    lines = _log_lines(row.log_text)
    assert "earlier line" in lines
    assert "cancel line" in lines
    assert lines.index("earlier line") < lines.index("cancel line")
