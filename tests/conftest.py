"""Pytest fixtures for Genestack Console API tests.

Uses a temporary config.yaml via CONSOLE_CONFIG (single override).
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest
import yaml

_CONSOLE_ROOT = Path(__file__).resolve().parents[1]

if str(_CONSOLE_ROOT) not in sys.path:
    sys.path.insert(0, str(_CONSOLE_ROOT))

from tests.fake_genestack import write_fake_genestack_root  # noqa: E402

# Hermetic fake genestack checkout (install scripts, versions, components).
_GENESTACK_ROOT = write_fake_genestack_root(
    Path(tempfile.mkdtemp(prefix="console-test-genestack-"))
)

_DB_FD, _DB_PATH = tempfile.mkstemp(prefix="console-test-", suffix=".db")
os.close(_DB_FD)

_CFG_FD, _CFG_PATH = tempfile.mkstemp(prefix="console-test-", suffix=".yaml")
os.close(_CFG_FD)

_test_config = {
    "dry_run": True,
    "data_dir": str(Path(_DB_PATH).parent / "console-test-data"),
    "database_url": f"sqlite:///{_DB_PATH}",
    "auth": {
        "api_keys": {
            "dev-admin-key": "admin",
            "dev-operator-key": "operator",
            "dev-viewer-key": "viewer",
        }
    },
    "genestack": {"root": str(_GENESTACK_ROOT)},
    "ansible": {"root": str(_CONSOLE_ROOT / "ansible")},
    # Older files may still contain this block. The loader ignores it.
    "maas": {"url": "http://unused.example", "api_key": "unused", "mock": True},
    "server": {"host": "127.0.0.1", "port": 8080},
    # The session client runs the app lifespan. Leave the update watcher off
    # so the suite does not call GitHub or try to replace a binary.
    "update": {"watch": False, "auto": False},
    "jobs": {"timeout_seconds": 600},
    # The session-scoped TestClient runs the app lifespan for the whole suite.
    # Its DB relay tasks would poll the shared test database and republish on
    # the global event bus, racing test_relay.py's strict boot/drain asserts
    # (which construct their own DBRelay instances). Those relays are covered
    # directly by their own test modules, so keep them off here.
    "stream": {"relay_enabled": False},
    "agent": {"relay_enabled": False},
}
Path(_CFG_PATH).write_text(yaml.safe_dump(_test_config), encoding="utf-8")

# Only env var tests need: path to config file
os.environ["CONSOLE_CONFIG"] = _CFG_PATH
# Clear any leftover CONSOLE_* noise from the shell
for key in list(os.environ):
    if key.startswith("CONSOLE_") and key != "CONSOLE_CONFIG":
        del os.environ[key]


@pytest.fixture(scope="session", autouse=True)
def _init_test_db():
    """Create DB tables once before any test runs.

    Required for tests that use SessionLocal() directly (without the
    app/client fixtures) — e.g. pick_executor and SSH config-push tests.
    """
    from app.db import init_db

    init_db()


def _import_app():
    """Import FastAPI app; reload settings from test config."""
    errors: list[str] = []
    try:
        from app.config import get_settings, reload_settings

        get_settings.cache_clear()
        reload_settings(Path(_CFG_PATH))
    except Exception as exc:  # noqa: BLE001
        errors.append(f"settings: {exc}")

    for module_path, attr in (
        ("app.main", "app"),
        ("app", "app"),
    ):
        try:
            mod = __import__(module_path, fromlist=[attr])
            application = getattr(mod, attr, None)
            if application is not None:
                return application
            errors.append(f"{module_path}.{attr} is None")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{module_path}: {exc}")
    pytest.skip(
        "FastAPI app not importable yet " f"(tried app.main:app): {'; '.join(errors)}"
    )


@pytest.fixture(scope="session")
def app():
    return _import_app()


@pytest.fixture(scope="session")
def client(app):
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        yield c


@pytest.fixture
def admin_headers() -> dict[str, str]:
    return {"X-API-Key": "dev-admin-key"}


@pytest.fixture
def operator_headers() -> dict[str, str]:
    return {"X-API-Key": "dev-operator-key"}


@pytest.fixture
def viewer_headers() -> dict[str, str]:
    return {"X-API-Key": "dev-viewer-key"}


@pytest.fixture
def genestack_root() -> Path:
    return _GENESTACK_ROOT


@pytest.fixture
def console_root() -> Path:
    return _CONSOLE_ROOT


def pytest_sessionfinish(session, exitstatus):  # noqa: ARG001
    import shutil

    for path in (_DB_PATH, _CFG_PATH):
        try:
            os.unlink(path)
        except OSError:
            pass
    shutil.rmtree(_GENESTACK_ROOT, ignore_errors=True)
