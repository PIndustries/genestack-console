"""Dry-run marker on jobs: column migration, persistence, and JobRead exposure."""

from __future__ import annotations

import uuid

import app.db as app_db
from app.db import Base, SessionLocal, create_db_engine
from app.models import Job

# Old jobs schema (pre dry_run column) for the existing-database migration test.
_OLD_JOBS_DDL = """
CREATE TABLE jobs (
    id VARCHAR(36) PRIMARY KEY,
    environment_id VARCHAR(36) REFERENCES environments(id) ON DELETE SET NULL,
    operation VARCHAR(128) NOT NULL,
    params JSON,
    status VARCHAR(7) NOT NULL,
    log_text TEXT NOT NULL,
    created_by VARCHAR(128),
    started_at DATETIME,
    finished_at DATETIME,
    error TEXT,
    created_at DATETIME NOT NULL
)
"""


def _job_columns(engine) -> set[str]:
    with engine.begin() as conn:
        rows = conn.exec_driver_sql("PRAGMA table_info(jobs)").fetchall()
    return {row[1] for row in rows}


def _create_env(client, headers, prefix: str, dry_run: bool | None = None) -> str:
    name = f"{prefix}-{uuid.uuid4().hex[:8]}"
    body: dict = {"name": name, "description": f"{prefix} test"}
    if dry_run is not None:
        body["dry_run"] = dry_run
    resp = client.post("/api/v1/environments", headers=headers, json=body)
    assert resp.status_code in (200, 201), resp.text
    eid = resp.json().get("id")
    assert eid, resp.text
    return eid


def _submit_enable(client, headers, environment_id: str):
    resp = client.post(
        f"/api/v1/environments/{environment_id}/jobs",
        headers=headers,
        json={
            "operation": "genestack.service.enable",
            "params": {"service": "keystone"},
            "run_sync": True,
        },
    )
    assert resp.status_code in (200, 201), resp.text
    return resp.json()


def _db_dry_run(job_id: str) -> bool | None:
    db = SessionLocal()
    try:
        row = db.get(Job, job_id)
        assert row is not None, job_id
        return row.dry_run
    finally:
        db.close()


# ------------------------------------------------------------ column migration


def test_migration_adds_dry_run_on_fresh_db(tmp_path, monkeypatch):
    """create_all on a fresh database includes jobs.dry_run."""
    engine = create_db_engine(f"sqlite:///{tmp_path}/fresh.db")
    monkeypatch.setattr(app_db, "engine", engine)
    Base.metadata.create_all(bind=engine)
    app_db._ensure_columns()  # idempotent no-op here
    assert "dry_run" in _job_columns(engine)


def test_migration_adds_dry_run_on_existing_db(tmp_path, monkeypatch):
    """ALTER TABLE path: a jobs table from before the column gains dry_run."""
    engine = create_db_engine(f"sqlite:///{tmp_path}/existing.db")
    monkeypatch.setattr(app_db, "engine", engine)
    # Other tables already at the current schema; jobs predates dry_run.
    Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE jobs")
        conn.exec_driver_sql(_OLD_JOBS_DDL)
        conn.exec_driver_sql(
            "INSERT INTO jobs (id, operation, status, log_text, created_at)"
            " VALUES ('old-job', 'internal.health', 'success', '', '2024-01-01')"
        )
    assert "dry_run" not in _job_columns(engine)

    app_db._ensure_columns()
    app_db._ensure_columns()  # second run is a no-op
    assert "dry_run" in _job_columns(engine)
    with engine.begin() as conn:
        row = conn.exec_driver_sql(
            "SELECT operation, dry_run FROM jobs WHERE id = 'old-job'"
        ).fetchone()
    assert row == ("internal.health", None)  # pre-existing rows stay NULL


# ----------------------------------------------------------------- persistence


def test_dry_run_job_persisted_and_exposed(client, admin_headers):
    """Global test config is dry_run=True: the rehearsal marker lands on the job."""
    env_id = _create_env(client, admin_headers, "dryrun-enable")
    job = _submit_enable(client, admin_headers, env_id)
    assert job["status"] == "success", job
    assert job["dry_run"] is True, job  # JobRead exposes it
    assert job["error"] is None, job  # rehearsal is not an error
    assert _db_dry_run(job["id"]) is True

    fetched = client.get(f"/api/v1/jobs/{job['id']}", headers=admin_headers)
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["dry_run"] is True


def test_real_job_dry_run_null(client, admin_headers, monkeypatch):
    """A real execution whose result carries no dry_run key leaves it NULL."""
    from app.services import genestack_bridge as bridge

    def fake_run_command(cmd, **kwargs):  # noqa: ARG001
        return {"ok": True, "returncode": 0, "message": "did it"}

    monkeypatch.setattr(bridge, "run_command", fake_run_command)
    env_id = _create_env(client, admin_headers, "real-enable", dry_run=False)
    job = _submit_enable(client, admin_headers, env_id)
    assert job["status"] == "success", job
    assert job["dry_run"] is None, job
    assert _db_dry_run(job["id"]) is None
