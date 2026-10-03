"""Postgres support: engine setup and the column-migration shim.

All unit-level and fully mocked — no live Postgres required. An optional
smoke test runs only when GSC_TEST_PG_DSN points at a reachable server
(e.g. `docker run -p 5432:5432 -e POSTGRES_PASSWORD=x postgres:16-alpine`);
it is skipped otherwise and never fails when docker/PG is absent.
"""

from __future__ import annotations

import logging
import os
from types import SimpleNamespace

import pytest

import app.db as app_db

PG_URL = "postgresql+psycopg://console:secret@db:5432/console"


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return list(self._rows)


class _FakeConn:
    """Records exec_driver_sql statements; answers information_schema queries."""

    def __init__(self, existing: dict[str, set[str]] | None = None):
        self.statements: list[str] = []
        self._existing = existing or {}

    def exec_driver_sql(self, sql, parameters=None):  # noqa: ARG002
        self.statements.append(sql)
        if "information_schema.columns" in sql:
            for table, columns in self._existing.items():
                if f"'{table}'" in sql:
                    return _FakeResult([(c,) for c in columns])
            return _FakeResult([])
        return _FakeResult([])


class _FakeBegin:
    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self._conn

    def __exit__(self, *exc):
        return False


class _FakeEngine:
    def __init__(self, url: str, existing: dict[str, set[str]] | None = None):
        self.url = url
        self.conn = _FakeConn(existing)

    def begin(self):
        return _FakeBegin(self.conn)


def _capture_engine(monkeypatch):
    """Patch app.db.create_engine / event; return (captured_kwargs, listeners)."""
    captured: dict = {}
    listeners: list = []

    def fake_create_engine(url, **kwargs):
        captured["url"] = url
        captured["kwargs"] = kwargs
        return SimpleNamespace(url=url)

    def fake_listens_for(target, name):
        listeners.append((target, name))
        return lambda fn: fn

    monkeypatch.setattr(app_db, "create_engine", fake_create_engine)
    monkeypatch.setattr(app_db, "event", SimpleNamespace(listens_for=fake_listens_for))
    return captured, listeners


def test_postgres_engine_gets_no_sqlite_connect_args(monkeypatch):
    captured, _ = _capture_engine(monkeypatch)
    app_db.create_db_engine(PG_URL)
    assert captured["url"] == PG_URL
    connect_args = captured["kwargs"].get("connect_args", {})
    assert "check_same_thread" not in connect_args
    # Nothing sqlite-specific leaks: pre-ping is the only universal kwarg.
    assert captured["kwargs"]["pool_pre_ping"] is True


def test_postgres_engine_registers_no_pragma_listener(monkeypatch):
    _, listeners = _capture_engine(monkeypatch)
    app_db.create_db_engine(PG_URL)
    assert listeners == []


def test_sqlite_engine_keeps_connect_args_and_pragmas(monkeypatch, tmp_path):
    captured, listeners = _capture_engine(monkeypatch)
    url = f"sqlite:///{tmp_path}/console.db"
    app_db.create_db_engine(url)
    assert captured["kwargs"]["connect_args"] == {"check_same_thread": False}
    # One listener sets SQLite pragmas. The second locks the database file
    # down to mode 600 after connect creates it.
    assert [name for _target, name in listeners] == ["connect", "connect"]


def test_ensure_columns_postgres_uses_information_schema_and_if_not_exists(monkeypatch):
    fake = _FakeEngine(
        PG_URL,
        existing={
            "environments": {"genestack_config_dir", "dry_run"},
            "users": {"password_hash"},
        },
    )
    monkeypatch.setattr(app_db, "engine", fake)

    app_db._ensure_columns()

    stmts = fake.conn.statements
    # PG-compatible introspection, never SQLite PRAGMAs.
    assert any("information_schema.columns" in s for s in stmts)
    assert not any("PRAGMA" in s for s in stmts)
    alters = [s for s in stmts if s.startswith("ALTER TABLE")]
    assert alters
    assert all("ADD COLUMN IF NOT EXISTS" in s for s in alters)
    # Already-present columns are not re-added.
    assert not any("ADD COLUMN IF NOT EXISTS genestack_config_dir" in s for s in alters)
    assert not any("ADD COLUMN IF NOT EXISTS password_hash" in s for s in alters)
    # Missing columns are added on the right tables.
    assert any(
        s == "ALTER TABLE environments ADD COLUMN IF NOT EXISTS kubeconfig_data TEXT"
        for s in alters
    )
    assert any(
        s == "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS dry_run BOOLEAN" for s in alters
    )
    assert any(
        s
        == "ALTER TABLE ovh_accounts ADD COLUMN IF NOT EXISTS consumer_key_encrypted TEXT"
        for s in alters
    )
    # Integer boolean defaults are translated for Postgres.
    assert any(
        s == "ALTER TABLE users ADD COLUMN IF NOT EXISTS "
        "platform_admin BOOLEAN DEFAULT FALSE NOT NULL"
        for s in alters
    )


def test_ensure_columns_postgres_idempotent_when_all_columns_exist(monkeypatch):
    fake = _FakeEngine(
        PG_URL,
        existing={
            table: set(columns) for table, columns in app_db._column_migrations()
        },
    )
    monkeypatch.setattr(app_db, "engine", fake)

    app_db._ensure_columns()

    assert not [s for s in fake.conn.statements if s.startswith("ALTER TABLE")]


def test_ensure_columns_postgres_best_effort_on_failure(monkeypatch, caplog):
    class _BrokenEngine:
        url = PG_URL

        def begin(self):
            raise RuntimeError("connection refused")

    monkeypatch.setattr(app_db, "engine", _BrokenEngine())
    with caplog.at_level(logging.WARNING, logger="app.db"):
        app_db._ensure_columns()  # must not raise
    warnings = [
        r for r in caplog.records if "column auto-migration failed" in r.message
    ]
    assert len(warnings) == len(app_db._column_migrations())


def test_ensure_columns_sqlite_still_uses_pragma(monkeypatch, tmp_path):
    engine = app_db.create_db_engine(f"sqlite:///{tmp_path}/console.db")
    from app import models  # noqa: F401

    app_db.Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(app_db, "engine", engine)
    app_db._ensure_columns()  # idempotent no-op on a fresh schema
    app_db._ensure_columns()


@pytest.mark.skipif(
    not os.environ.get("GSC_TEST_PG_DSN"),
    reason="no live Postgres (set GSC_TEST_PG_DSN=postgresql+psycopg://… to run)",
)
def test_init_db_against_live_postgres():
    """Optional smoke: real schema creation + shim against a live server."""
    dsn = os.environ["GSC_TEST_PG_DSN"]
    engine = app_db.create_db_engine(dsn)
    from app import models  # noqa: F401

    app_db.Base.metadata.create_all(bind=engine)
    import app.db as db_module

    original = db_module.engine
    try:
        db_module.engine = engine
        app_db._ensure_columns()
        app_db._ensure_columns()  # second run is a no-op
    finally:
        db_module.engine = original
        app_db.Base.metadata.drop_all(bind=engine)
        engine.dispose()
