"""Copy the console database onto SQLite or Postgres.

The process-global engine is left alone. The operator restarts after the
YAML ``database_url`` changes. Row contents are not logged; the database
holds Fernet secrets.
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

import yaml
from sqlalchemy import func, inspect, insert, select
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.sql.schema import Column

from app.db import Base, _column_migrations, _generic_column_ddl, create_db_engine
from app.db import engine as live_engine

log = logging.getLogger(__name__)

_BATCH = 200
_POSTGRES = frozenset({"postgresql", "postgres"})


def database_kind(url: str) -> str:
    """Return ``sqlite`` or ``postgresql``. Any other scheme is rejected."""
    driver = _driver(url)
    if driver == "sqlite":
        return "sqlite"
    if driver in _POSTGRES:
        return "postgresql"
    raise ValueError(f"unsupported database scheme: {driver}")


def mask_database_url(url: str) -> str:
    """Return ``url`` with the password replaced by ``***``."""
    parsed = _parse_url(url)
    if parsed.password is None:
        return parsed.render_as_string(hide_password=False)
    # hide_password writes *** without percent-encoding the asterisks.
    return parsed.render_as_string(hide_password=True)


def move_database(source_url: str, target_url: str, config_path: Path) -> dict:
    """Copy ``source_url`` onto ``target_url`` and set ``database_url``."""
    source_url = source_url.strip()
    target_url = target_url.strip()
    if not target_url:
        raise ValueError("target URL is required")
    database_kind(source_url)
    database_kind(target_url)
    if "\n" in target_url or "\r" in target_url:
        raise ValueError("target URL must be one line")
    if _same_url(source_url, target_url):
        raise ValueError("target URL is the same as the current database URL")

    # Populate metadata. init_db() also seeds and scrubs on the live engine.
    from app import models  # noqa: F401

    source_engine: Engine | None = None
    target_engine: Engine | None = None
    try:
        source_engine = create_db_engine(source_url)
        target_engine = create_db_engine(target_url)
        _refuse_live(source_engine, target_engine)
        Base.metadata.create_all(bind=target_engine)
        _apply_column_migrations(target_engine)
        if _target_has_rows(target_engine):
            raise ValueError("target database already has rows")
        tables, rows = _copy_tables(source_engine, target_engine)
        _write_database_url(config_path, target_url)
    finally:
        _dispose(source_engine)
        _dispose(target_engine)
    log.info("database move copied %s tables, %s rows", tables, rows)
    return {
        "restart_required": True,
        "source": mask_database_url(source_url),
        "target": mask_database_url(target_url),
        "tables": tables,
        "rows": rows,
    }


def _driver(url: str) -> str:
    return _parse_url(url).drivername.split("+", 1)[0].lower()


def _parse_url(url: str):
    try:
        return make_url(url)
    except ArgumentError as exc:
        raise ValueError("invalid database URL") from exc


def _same_url(left: str, right: str) -> bool:
    if left == right:
        return True
    return _parse_url(left) == _parse_url(right)


def _refuse_live(*engines: Engine) -> None:
    for engine in engines:
        if engine is live_engine:
            raise RuntimeError("refusing to use the process database engine")


def _dispose(engine: Engine | None) -> None:
    if engine is None or engine is live_engine:
        return
    engine.dispose()


def _apply_column_migrations(target: Engine) -> None:
    """Apply ``_column_migrations`` on ``target``.

    ``_ensure_columns`` reads the process engine, so it is not called.
    """
    if str(target.url).startswith("sqlite"):
        _migrate_sqlite(target)
        return
    _migrate_generic(target)


def _migrate_sqlite(target: Engine) -> None:
    with target.begin() as conn:
        for table, migrations in _column_migrations():
            rows = conn.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()
            existing = {row[1] for row in rows}
            for name, ddl in migrations.items():
                if name in existing:
                    continue
                log.info("ALTER TABLE %s ADD COLUMN %s %s", table, name, ddl)
                conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def _migrate_generic(target: Engine) -> None:
    for table, migrations in _column_migrations():
        try:
            with target.begin() as conn:
                rows = conn.exec_driver_sql(
                    "SELECT column_name FROM information_schema.columns "
                    f"WHERE table_name = '{table}'"
                ).fetchall()
                existing = {str(row[0]) for row in rows}
                for name, ddl in migrations.items():
                    if name in existing:
                        continue
                    stmt = (
                        f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS "
                        f"{name} {_generic_column_ddl(ddl)}"
                    )
                    log.info("%s", stmt)
                    conn.exec_driver_sql(stmt)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "column auto-migration failed for table %s (%s)",
                table,
                type(exc).__name__,
            )


def _copy_tables(source_engine: Engine, target_engine: Engine) -> tuple[int, int]:
    plan = _copy_plan(source_engine, target_engine)
    table_count = 0
    row_count = 0
    with source_engine.connect() as source, target_engine.begin() as target:
        for table, columns in plan:
            row_count += _copy_table(source, target, table, columns)
            table_count += 1
        _sync_postgres_sequences(target, [table for table, _columns in plan])
    return table_count, row_count


def _copy_plan(source_engine: Engine, target_engine: Engine) -> list[tuple]:
    source_inspector = inspect(source_engine)
    target_inspector = inspect(target_engine)
    plan: list[tuple] = []
    for table in Base.metadata.sorted_tables:
        if not source_inspector.has_table(table.name):
            continue
        if not target_inspector.has_table(table.name):
            continue
        source_names = {
            col["name"] for col in source_inspector.get_columns(table.name)
        }
        target_names = [
            col["name"] for col in target_inspector.get_columns(table.name)
        ]
        by_name = {col.name: col for col in table.columns}
        columns = [
            by_name[name]
            for name in target_names
            if name in source_names and name in by_name
        ]
        if not columns:
            continue
        plan.append((table, columns))
    return plan


def _copy_table(source, target, table, columns: list[Column]) -> int:
    """Insert shared columns in target column order. Returns the row count."""
    result = source.execute(select(*columns))
    copied = 0
    while True:
        chunk = result.fetchmany(_BATCH)
        if not chunk:
            return copied
        payload = [
            {column.key: value for column, value in zip(columns, row)}
            for row in chunk
        ]
        target.execute(insert(table), payload)
        copied += len(payload)


def _sync_postgres_sequences(conn, tables) -> None:
    """Point serial sequences past copied integer primary keys.

    SQLite has no sequences. String UUID keys are left alone. Values are
    not logged.
    """
    if conn.dialect.name != "postgresql":
        return
    preparer = conn.dialect.identifier_preparer
    for table in tables:
        quoted_table = preparer.quote(table.name)
        for column in table.primary_key.columns:
            seq = conn.exec_driver_sql(
                "SELECT pg_get_serial_sequence(%s, %s)",
                (table.name, column.name),
            ).scalar()
            if not seq:
                continue
            quoted_column = preparer.quote(column.name)
            conn.exec_driver_sql(
                "SELECT setval(%s, "
                f"COALESCE((SELECT MAX({quoted_column}) FROM {quoted_table}), 1), "
                f"(SELECT MAX({quoted_column}) FROM {quoted_table}) IS NOT NULL)",
                (seq,),
            )


def _target_has_rows(engine: Engine) -> bool:
    """True when any console table on ``engine`` already has a row."""
    inspector = inspect(engine)
    with engine.connect() as conn:
        for table in Base.metadata.sorted_tables:
            if not inspector.has_table(table.name):
                continue
            count = conn.execute(
                select(func.count()).select_from(table)
            ).scalar_one()
            if count:
                return True
    return False


def _write_database_url(config_path: Path, target_url: str) -> None:
    """Replace the top-level ``database_url`` line. The rest of the file stays.

    A full YAML dump would drop comments. The key is one line in config.yaml.
    """
    # Quote the URL ourselves. safe_dump wraps past 80 columns and splits the line.
    escaped = target_url.replace("\\", "\\\\").replace('"', '\\"')
    new_line = f'database_url: "{escaped}"\n'
    if config_path.is_file():
        text = config_path.read_text(encoding="utf-8")
        mode = config_path.stat().st_mode & 0o777
    else:
        text = ""
        mode = 0o600
    if text and not isinstance(yaml.safe_load(text) or {}, dict):
        raise ValueError("config root must be a mapping")
    replaced = False
    out: list[str] = []
    for raw in text.splitlines(keepends=True):
        body = raw.rstrip("\r\n")
        if not replaced and body == body.lstrip() and body.startswith("database_url:"):
            out.append(new_line)
            replaced = True
            continue
        out.append(raw if raw.endswith("\n") else raw + "\n")
    if not replaced:
        if out and not out[-1].endswith("\n"):
            out[-1] = out[-1] + "\n"
        out.append(new_line)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=".database-url-",
        suffix=".yaml",
        dir=config_path.parent,
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("".join(out))
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, config_path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
