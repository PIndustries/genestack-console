"""SQLAlchemy engine and session management."""

from __future__ import annotations

import logging
from collections.abc import Generator
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings

log = logging.getLogger(__name__)

# Columns added after the initial schema; created via lightweight ALTER TABLE
# for existing SQLite databases (create_all only handles fresh databases).
_ENVIRONMENT_COLUMN_MIGRATIONS = {
    "genestack_config_dir": "VARCHAR(512)",
    "state_repo_path": "VARCHAR(512)",
    "state_repo_remote": "VARCHAR(512)",
    "kubeconfig_data": "TEXT",
    "dry_run": "BOOLEAN",
    "tenant_id": "VARCHAR(36) REFERENCES tenants(id)",
    "ssh_private_key_encrypted": "TEXT",
    "ssh_public_key": "TEXT",
    "ovh_consumer_key_encrypted": "TEXT",
    "ovh_account_id": "VARCHAR(36)",
}

_USER_COLUMN_MIGRATIONS = {
    "password_hash": "TEXT",
    "platform_admin": "BOOLEAN DEFAULT 0 NOT NULL",
}

_JOB_COLUMN_MIGRATIONS = {
    "dry_run": "BOOLEAN",
    "secret_params": "JSON",
    "cancel_requested": "BOOLEAN DEFAULT 0 NOT NULL",
    "user_step": "JSON",
}

_OVH_ACCOUNT_COLUMN_MIGRATIONS = {
    "consumer_key_encrypted": "TEXT",
}

_AGENT_CREDENTIAL_COLUMN_MIGRATIONS = {
    "wg_address": "VARCHAR(64)",
    "wg_public_key": "VARCHAR(64)",
    "wg_private_key_encrypted": "TEXT",
}

_HARDWARE_ACCOUNT_COLUMN_MIGRATIONS = {
    "tenant_id": "VARCHAR(36) REFERENCES tenants(id)",
}

_ALERT_RULE_COLUMN_MIGRATIONS = {
    "channel_id": "VARCHAR(36)",
}

_SESSION_TOKEN_COLUMN_MIGRATIONS = {
    "refresh_token_hash": "VARCHAR(64)",
    "refresh_expires_at": "TIMESTAMP",
}

_BAREMETAL_NODE_COLUMN_MIGRATIONS = {
    "next_boot": "VARCHAR(16) DEFAULT 'disk' NOT NULL",
    "boot_stage": "VARCHAR(32) DEFAULT 'new' NOT NULL",
    "commission_token": "VARCHAR(64)",
    "commission_report": "TEXT",
    "boot_log": "TEXT",
    "wiped_at": "TIMESTAMP",
    "talos_served_at": "TIMESTAMP",
}


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


def _sqlite_path_from_url(url: str) -> Path | None:
    """Return the filesystem path for a SQLite URL, or None if not a file DB."""
    if not url.startswith("sqlite"):
        return None
    # sqlite:///./data/console.db or sqlite:////data/console.db
    raw = url.split("sqlite:///", 1)[-1] if "sqlite:///" in url else ""
    if not raw or raw == ":memory:":
        return None
    path = Path(raw)
    if not path.is_absolute():
        # relative to CWD (typically genestack-console/)
        path = Path.cwd() / path
    return path


def _chmod_owner_only(path: Path) -> None:
    """Best-effort mode 0600 on console.db (and companions if present)."""
    for candidate in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
        if candidate.is_file():
            try:
                candidate.chmod(0o600)
            except OSError:
                pass


def _ensure_sqlite_parent(url: str) -> None:
    """Create parent directory for SQLite file URLs if needed."""
    path = _sqlite_path_from_url(url)
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    # Restrict the data dir so newly created DBs are not world-readable.
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass


def create_db_engine(database_url: str | None = None):
    settings = get_settings()
    url = database_url or settings.database_url
    _ensure_sqlite_parent(url)

    # SQLite-only connect args and pragmas; nothing driver-specific leaks
    # into other dialects (e.g. postgresql+psycopg://).
    engine_kwargs: dict = {"pool_pre_ping": True}
    if url.startswith("sqlite"):
        engine_kwargs["connect_args"] = {"check_same_thread": False}

    engine = create_engine(url, **engine_kwargs)

    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _set_sqlite_pragma(dbapi_conn, connection_record):  # noqa: ARG001
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            # Tolerate brief write contention between the API, the worker
            # daemon, and the DB relays instead of failing immediately with
            # "database is locked".
            cursor.execute("PRAGMA busy_timeout=5000")
            # WAL: the API, the worker daemon, and the DB relays share one
            # SQLite file; in the default rollback-journal mode a writer
            # blocks all readers. WAL keeps readers flowing while a write is
            # in flight (the busy_timeout above covers the remaining
            # writer-vs-writer window). journal_mode is persistent per file,
            # so setting it on every connect is a cheap no-op after first use.
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

        db_path = _sqlite_path_from_url(url)
        if db_path is not None:
            # Engine connect may create the file; lock it down once it exists.
            # If it does not exist yet, the first request creates it — harden
            # again via the connect hook below.
            _chmod_owner_only(db_path)

            @event.listens_for(engine, "connect")
            def _chmod_sqlite_file(dbapi_conn, connection_record):  # noqa: ARG001
                _chmod_owner_only(db_path)

    return engine


engine = create_db_engine()
SessionLocal = sessionmaker(
    bind=engine, autocommit=False, autoflush=False, class_=Session
)


def init_db() -> None:
    """Create all tables, apply lightweight column migrations, backfill tenancy."""
    # Import models so metadata is populated
    from app import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    _ensure_columns()
    _backfill_default_tenant()
    _seed_default_ovh_account()
    _backfill_ovh_consumer_keys()
    _scrub_historical_job_secrets()
    _seed_demo_if_enabled()


def _scrub_historical_job_secrets() -> None:
    """One-time scrub of secret job params persisted before at-rest scrubbing.

    Idempotent: rows already scrubbed (secret params == "***") are skipped.
    Runs exactly once per database (marker table) — it was historically
    executed on every ``init_db()`` call (i.e. every worker invocation),
    which meant full-table loads of jobs and audit logs on each.
    Best-effort: failures are logged, never fatal to startup.
    """
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql(
                "CREATE TABLE IF NOT EXISTS _gsc_migrations (name TEXT PRIMARY KEY)"
            )
            done = conn.exec_driver_sql(
                "SELECT 1 FROM _gsc_migrations WHERE name = 'scrub_job_secrets'"
            ).fetchone()
        if done:
            return
        from app.services.job_runner import scrub_stored_job_secrets

        with SessionLocal() as db:
            scrubbed = scrub_stored_job_secrets(db)
        with engine.begin() as conn:
            conn.exec_driver_sql(
                "INSERT INTO _gsc_migrations (name) VALUES ('scrub_job_secrets')"
            )
        if scrubbed:
            log.info("scrubbed secret job params on %d historical row(s)", scrubbed)
    except Exception:  # noqa: BLE001
        log.warning("historical job secret scrub failed (continuing)", exc_info=True)


def _ensure_columns() -> None:
    """Add newer columns to existing databases (idempotent).

    Proper Alembic migrations are out of scope for this phase; this shim
    covers SQLite (PRAGMA table_info) and Postgres-style databases
    (information_schema + ADD COLUMN IF NOT EXISTS). Both paths are
    best-effort: failures are logged as warnings, never fatal.
    """
    if str(engine.url).startswith("sqlite"):
        _ensure_columns_sqlite()
    else:
        _ensure_columns_generic()


def _column_migrations() -> tuple[tuple[str, dict[str, str]], ...]:
    return (
        ("environments", _ENVIRONMENT_COLUMN_MIGRATIONS),
        ("users", _USER_COLUMN_MIGRATIONS),
        ("jobs", _JOB_COLUMN_MIGRATIONS),
        ("ovh_accounts", _OVH_ACCOUNT_COLUMN_MIGRATIONS),
        ("agent_credentials", _AGENT_CREDENTIAL_COLUMN_MIGRATIONS),
        ("hardware_accounts", _HARDWARE_ACCOUNT_COLUMN_MIGRATIONS),
        ("baremetal_nodes", _BAREMETAL_NODE_COLUMN_MIGRATIONS),
        ("alert_rules", _ALERT_RULE_COLUMN_MIGRATIONS),
        ("session_tokens", _SESSION_TOKEN_COLUMN_MIGRATIONS),
    )


def _ensure_columns_sqlite() -> None:
    with engine.begin() as conn:
        for table, migrations in _column_migrations():
            rows = conn.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()
            existing = {row[1] for row in rows}
            for name, ddl in migrations.items():
                if name not in existing:
                    log.info("ALTER TABLE %s ADD COLUMN %s %s", table, name, ddl)
                    conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def _generic_column_ddl(ddl: str) -> str:
    """Adjust SQLite-flavored column DDL for standard SQL (Postgres)."""
    # Postgres rejects integer 0/1 as a boolean column default.
    if ddl.upper().startswith("BOOLEAN"):
        return ddl.replace("DEFAULT 0", "DEFAULT FALSE").replace(
            "DEFAULT 1", "DEFAULT TRUE"
        )
    return ddl


def _ensure_columns_generic() -> None:
    """Non-SQLite variant (Postgres): information_schema + IF NOT EXISTS."""
    for table, migrations in _column_migrations():
        try:
            with engine.begin() as conn:
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
        except Exception:  # noqa: BLE001
            log.warning(
                "column auto-migration failed for table %s (continuing)",
                table,
                exc_info=True,
            )


def _backfill_default_tenant() -> None:
    """Assign environments and hardware accounts without a tenant to a `default` tenant (idempotent)."""
    from sqlalchemy import func, select, update

    from app.models import Environment, HardwareAccount, Tenant

    with SessionLocal() as db:
        orphan_envs = db.scalar(
            select(func.count())
            .select_from(Environment)
            .where(Environment.tenant_id.is_(None))
        )
        orphan_accounts = db.scalar(
            select(func.count())
            .select_from(HardwareAccount)
            .where(HardwareAccount.tenant_id.is_(None))
        )
        if not orphan_envs and not orphan_accounts:
            return
        tenant = db.scalar(select(Tenant).where(Tenant.name == "default"))
        if tenant is None:
            tenant = Tenant(
                name="default",
                description="Default tenant for resources created before multi-tenancy",
            )
            db.add(tenant)
            db.flush()
        if orphan_envs:
            db.execute(
                update(Environment)
                .where(Environment.tenant_id.is_(None))
                .values(tenant_id=tenant.id)
            )
            log.info(
                "backfilled %d environment(s) into the 'default' tenant", orphan_envs
            )
        if orphan_accounts:
            db.execute(
                update(HardwareAccount)
                .where(HardwareAccount.tenant_id.is_(None))
                .values(tenant_id=tenant.id)
            )
            log.info(
                "backfilled %d hardware account(s) into the 'default' tenant",
                orphan_accounts,
            )
        db.commit()


def _seed_default_ovh_account() -> None:
    """Create a default OVH account from config.yaml when the table is empty.

    Lets existing single-account deployments (ovh: block in config) keep
    working without a manual API call: the config credentials become the
    "default" account on first boot. No-op when accounts already exist or
    config has no OVH app credentials.
    """
    from sqlalchemy import func, select

    from app.config import get_settings
    from app.models import OvhAccount
    from app.services.crypto import encrypt_secret

    settings = get_settings()
    app_key = (settings.ovh_app_key or "").strip()
    app_secret = (settings.ovh_app_secret or "").strip()
    if not app_key or not app_secret:
        return

    with SessionLocal() as db:
        existing = db.scalar(select(func.count()).select_from(OvhAccount))
        if existing:
            return
        account = OvhAccount(
            name="default",
            endpoint=(settings.ovh_endpoint or "").strip()
            or "https://eu.api.ovh.com/1.0",
            app_key=app_key,
            app_secret_encrypted=encrypt_secret(app_secret) or "",
        )
        db.add(account)
        db.commit()
        log.info("seeded default OVH account from config (app_key present)")


def _backfill_ovh_consumer_keys() -> None:
    """One-time move of per-environment OVH consumer keys onto their account.

    Consumer keys now live on OvhAccount (one read-only credential per OVH
    account). Any env that stored its own key (the earlier per-env wizard
    flow) has it copied to its bound account — only when the account has no
    key yet — and the env's copy is cleared. Marked in _gsc_migrations so it
    runs once per database; best-effort, never fatal to startup.
    """
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql(
                "CREATE TABLE IF NOT EXISTS _gsc_migrations (name TEXT PRIMARY KEY)"
            )
            done = conn.exec_driver_sql(
                "SELECT 1 FROM _gsc_migrations WHERE name = 'backfill_ovh_consumer_keys'"
            ).fetchone()
        if done:
            return
    except Exception:  # noqa: BLE001
        log.warning("ovh consumer key backfill marker check failed", exc_info=True)
        return

    from sqlalchemy import select

    from app.models import Environment, OvhAccount

    moved = 0
    with SessionLocal() as db:
        for env in db.scalars(select(Environment)).all():
            if not (env.ovh_consumer_key_encrypted or "").strip():
                continue
            account = (
                db.get(OvhAccount, env.ovh_account_id) if env.ovh_account_id else None
            )
            if (
                account is not None
                and not (account.consumer_key_encrypted or "").strip()
            ):
                account.consumer_key_encrypted = env.ovh_consumer_key_encrypted
                moved += 1
            env.ovh_consumer_key_encrypted = None
        db.commit()

    try:
        with engine.begin() as conn:
            conn.exec_driver_sql(
                "INSERT INTO _gsc_migrations (name) VALUES ('backfill_ovh_consumer_keys')"
            )
        log.info(
            "backfilled %d OVH consumer key(s) from environments to accounts", moved
        )
    except Exception:  # noqa: BLE001
        log.warning("ovh consumer key backfill marker insert failed", exc_info=True)


def _seed_demo_if_enabled() -> None:
    """Optional walkthrough tenant. No-op unless config seed_demo is true."""
    try:
        from app.services.demo import seed_demo_if_enabled

        with SessionLocal() as db:
            seed_demo_if_enabled(db)
    except Exception:  # noqa: BLE001
        log.warning("demo seed failed (continuing)", exc_info=True)


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency that yields a DB session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
