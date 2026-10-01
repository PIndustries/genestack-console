"""Job-log credential redaction (logredact.redact_secret_line + append_log).

Every line the job runner persists to ``jobs.log_text`` is scrubbed first, so
a command that echoes a password, API key, token, or enrollment credential
cannot leave the secret in the stored log.
"""

from __future__ import annotations

import pytest

from app.services.logredact import redact_secret_line

# ------------------------------------------------------------ unit: the fn


@pytest.mark.parametrize(
    "line",
    [
        "password=Hunter2! connecting",
        "PASSWD: s3cr3t",
        "api_key=abcdef123456",
        "api-key: sk-live-123",
        "apikey = 12345",
        "secret=deadbeef",
        "SECRET: mysecretvalue",
        "token=abc123",
        "authorization: Basic dXNlcjpwYXNz",
        "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig",
        "set Bearer xyz.token.here",
    ],
)
def test_known_secret_values_are_redacted(line):
    out = redact_secret_line(line)
    assert "<redacted>" in out
    # The raw secret value must not survive.
    assert "Hunter2!" not in out and "s3cr3t" not in out
    assert "abcdef123456" not in out and "sk-live-123" not in out
    assert "12345" not in out and "deadbeef" not in out
    assert "mysecretvalue" not in out and "abc123" not in out
    assert "dXNlcjpwYXNz" not in out
    assert "eyJhbGciOiJIUzI1NiJ9" not in out and "xyz.token.here" not in out


def test_bearer_credential_collapses():
    """`Authorization: Bearer <token>` collapses to a single placeholder."""
    out = redact_secret_line("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig")
    assert "eyJhbGciOiJIUzI1NiJ9" not in out
    assert "payload.sig" not in out
    assert out.count("<redacted>") == 1
    assert out == "Authorization: <redacted>"


def test_basic_credential_collapses():
    out = redact_secret_line("Authorization: Basic dXNlcjpwYXNz")
    assert "dXNlcjpwYXNz" not in out
    assert out.count("<redacted>") == 1
    assert out == "Authorization: <redacted>"


def test_enrollment_token_masked():
    out = redact_secret_line("curl -H 'X-Enroll: gsca_abc123DEF456._-='")
    assert "gsca_abc123DEF456" not in out
    assert "gsca_***" in out


def test_fernet_ciphertext_masked():
    out = redact_secret_line("staged=fernet:gAAAAAB1234567890== token")
    assert "gAAAAAB1234567890" not in out
    assert "fernet:***" in out


@pytest.mark.parametrize(
    "line",
    [
        "max_token=5 per call",  # count, not a credential
        "token is required to proceed",  # prose, no assignment
        "the password for the vault",  # prose, no assignment
        "retrying in 3 seconds",
        "ok: healthy",
    ],
)
def test_non_secret_lines_unchanged(line):
    assert redact_secret_line(line) == line


def test_redaction_is_idempotent():
    line = "password=Hunter2! and gsca_abc token=xyz"
    once = redact_secret_line(line)
    assert redact_secret_line(once) == once
    assert "Hunter2!" not in once and "gsca_abc" not in once and "token=xyz" not in once


def test_empty_line():
    assert redact_secret_line("") == ""
    assert redact_secret_line("no secrets here") == "no secrets here"


# ------------------------------------------------- integration: append_log


def _finalize(db, job_id: str) -> None:
    """Mark a leftover queued job failed so a worker poll cannot claim it."""
    from app.models import Job, JobStatus

    row = db.get(Job, job_id)
    if row is not None and row.status in (JobStatus.queued, JobStatus.running):
        row.status = JobStatus.failed
        db.commit()


def test_append_log_redacts_before_persist():
    """A secret echoed by a command never reaches the stored job log."""
    from app.db import SessionLocal
    from app.models import Job
    from app.services.job_runner import JobRunner

    db = SessionLocal()
    job_id = ""
    try:
        runner = JobRunner(db)
        job = runner.create_job(operation="internal.health", created_by="redact-test")
        job_id = job.id
        db.commit()
        # Simulate a command whose output leaks a credential.
        runner.append_log(job, "deploy: password=SuperSecret1! done")
        runner.append_log(job, "enroll token gsca_liveabc123xyz issued")
        runner.flush_logs(job)
        db.commit()
        assert "SuperSecret1!" not in job.log_text
        assert "gsca_liveabc123xyz" not in job.log_text
        assert "<redacted>" in job.log_text
        assert "gsca_***" in job.log_text
        _finalize(db, job_id)
    finally:
        db.close()

    # Re-read from a fresh session to prove the stored row is redacted.
    db = SessionLocal()
    try:
        row = db.get(Job, job_id)
        assert "SuperSecret1!" not in row.log_text
        assert "gsca_liveabc123xyz" not in row.log_text
    finally:
        db.close()


def test_append_log_preserves_normal_lines():
    from app.db import SessionLocal
    from app.services.job_runner import JobRunner

    db = SessionLocal()
    job_id = ""
    try:
        runner = JobRunner(db)
        job = runner.create_job(operation="internal.health", created_by="redact-test")
        job_id = job.id
        db.commit()
        runner.append_log(job, "Starting operation=internal.health")
        runner.append_log(job, "ok: healthy, 3 hosts")
        runner.flush_logs(job)
        db.commit()
        assert "Starting operation=internal.health" in job.log_text
        assert "ok: healthy, 3 hosts" in job.log_text
        assert "<redacted>" not in job.log_text
        _finalize(db, job_id)
    finally:
        db.close()
