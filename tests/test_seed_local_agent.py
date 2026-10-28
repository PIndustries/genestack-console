"""Tenancy-safe local-agent seed (M5).

Covers app.services.agents.seed_default_local_agent:
  * multi-env + no override     -> skip, no credential, warning logged
  * single env + no override    -> seeded for that env
  * override (valid)            -> seeded for the chosen env
  * override (unknown id)       -> skip, no credential, warning logged
  * existing cred + missing file-> NOT rotated (raw token unrecoverable)
  * existing cred + present file-> idempotent no-op

The seed function is tenancy-blind only if it guesses; it must never do so.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.db import Base
from app.models import AgentCredential, Environment, ReachHub
from app.services import agents


def _suffix() -> str:
    return uuid.uuid4().hex[:8]


@pytest.fixture
def seed_db():
    """Fresh in-memory SQLite with only the tables the seed touches."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(
        bind=engine,
        tables=[
            Environment.__table__,
            AgentCredential.__table__,
            ReachHub.__table__,
        ],
    )
    TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = TestingSession()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()


def _make_env(name: str) -> Environment:
    env = Environment(id=f"env-{_suffix()}", name=name)
    return env


def _settings(**kw) -> Settings:
    base = dict(
        agent_default_environment_id=kw.pop("agent_default_environment_id", ""),
    )
    base.update(kw)
    return Settings(**base)


def _cred_count(db, env_id: str) -> int:
    return len(_cred_count_rows(db, env_id))


# ---------------------------------------------------------------------------
# Environment selection
# ---------------------------------------------------------------------------


def test_multi_env_no_override_skips_seed(seed_db, tmp_path, caplog):
    a = _make_env(f"env-a-{_suffix()}")
    b = _make_env(f"env-b-{_suffix()}")
    seed_db.add_all([a, b])
    seed_db.commit()

    import logging

    with caplog.at_level(logging.WARNING, logger="app.services.agents"):
        agents.seed_default_local_agent(seed_db, tmp_path, settings=_settings())

    # No credential was created for either env.
    assert _cred_count(seed_db, a.id) == 0
    assert _cred_count(seed_db, b.id) == 0
    # Token file not written.
    assert not (tmp_path / "local-agent-token").exists()
    # Operator was told to set the override.
    assert any("agent.default_environment_id" in r.message for r in caplog.records)


def test_single_env_no_override_seeds(seed_db, tmp_path):
    a = _make_env(f"env-solo-{_suffix()}")
    seed_db.add(a)
    seed_db.commit()

    agents.seed_default_local_agent(seed_db, tmp_path, settings=_settings())
    seed_db.commit()

    assert _cred_count(seed_db, a.id) == 1
    token_file = tmp_path / "local-agent-token"
    assert token_file.is_file()
    token = token_file.read_text(encoding="utf-8")
    assert token.startswith(agents.TOKEN_PREFIX)
    # Hash matches what's stored.
    cred = _local_credential(seed_db, a.id)
    assert cred is not None
    assert cred.token_hash == _sha256(token)


def test_override_seeds_chosen_env(seed_db, tmp_path):
    a = _make_env(f"env-ovr-a-{_suffix()}")
    b = _make_env(f"env-ovr-b-{_suffix()}")
    seed_db.add_all([a, b])
    seed_db.commit()

    agents.seed_default_local_agent(
        seed_db, tmp_path, settings=_settings(agent_default_environment_id=b.id)
    )
    seed_db.commit()

    assert _cred_count(seed_db, b.id) == 1
    assert _cred_count(seed_db, a.id) == 0
    assert (tmp_path / "local-agent-token").is_file()


def test_override_unknown_env_skips(seed_db, tmp_path, caplog):
    a = _make_env(f"env-unk-{_suffix()}")
    seed_db.add(a)
    seed_db.commit()

    import logging

    with caplog.at_level(logging.WARNING, logger="app.services.agents"):
        agents.seed_default_local_agent(
            seed_db, tmp_path, settings=_settings(agent_default_environment_id="nope")
        )

    assert _cred_count(seed_db, a.id) == 0
    assert not (tmp_path / "local-agent-token").exists()
    assert any("does not match" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Idempotency + no-rotation
# ---------------------------------------------------------------------------


def test_existing_credential_missing_file_not_rotated(seed_db, tmp_path, caplog):
    import logging

    a = _make_env(f"env-rot-{_suffix()}")
    seed_db.add(a)
    seed_db.commit()

    # First seed creates the credential + file.
    agents.seed_default_local_agent(seed_db, tmp_path, settings=_settings())
    seed_db.commit()
    first_cred = _local_credential(seed_db, a.id)
    first_hash = first_cred.token_hash

    # Simulate the token file being deleted (volume lost / cleaned).
    (tmp_path / "local-agent-token").unlink()

    with caplog.at_level(logging.ERROR, logger="app.services.agents"):
        agents.seed_default_local_agent(seed_db, tmp_path, settings=_settings())
    seed_db.commit()

    # Credential must be untouched (no rotation -> same hash, no new row).
    after = _local_credential(seed_db, a.id)
    assert after.token_hash == first_hash
    assert _cred_count(seed_db, a.id) == 1
    # No file was re-created (we can't recover the raw token).
    assert not (tmp_path / "local-agent-token").exists()
    # Operator was told what happened and how to fix it.
    assert any("refusing to rotate" in r.message for r in caplog.records)


def test_existing_credential_present_file_is_noop(seed_db, tmp_path):
    a = _make_env(f"env-noop-{_suffix()}")
    seed_db.add(a)
    seed_db.commit()

    agents.seed_default_local_agent(seed_db, tmp_path, settings=_settings())
    seed_db.commit()
    first_token = (tmp_path / "local-agent-token").read_text(encoding="utf-8")

    # Run again: nothing changes.
    agents.seed_default_local_agent(seed_db, tmp_path, settings=_settings())
    seed_db.commit()

    assert (tmp_path / "local-agent-token").read_text(encoding="utf-8") == first_token
    assert _cred_count(seed_db, a.id) == 1


def test_no_environments_noop(seed_db, tmp_path):
    # No environments at all -> nothing to do, no error.
    agents.seed_default_local_agent(seed_db, tmp_path, settings=_settings())
    assert not (tmp_path / "local-agent-token").exists()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sha256(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _local_credential(db, env_id: str) -> AgentCredential:
    from sqlalchemy import select

    return db.scalar(
        select(AgentCredential).where(
            AgentCredential.environment_id == env_id,
            AgentCredential.name == "local-agent",
        )
    )


def _cred_count_rows(db, env_id: str):
    from sqlalchemy import select

    return db.scalars(select(AgentCredential).where(AgentCredential.environment_id == env_id)).all()


def test_settings_default_is_empty():
    assert Settings().agent_default_environment_id == ""


def test_seed_writes_token_mode_0600(seed_db, tmp_path):
    """Raw local-agent-token on disk must be owner-only."""
    seed_db.add(_make_env("solo-mode"))
    seed_db.commit()

    agents.seed_default_local_agent(seed_db, tmp_path, settings=_settings())
    token_file = tmp_path / "local-agent-token"
    assert token_file.is_file()
    mode = token_file.stat().st_mode & 0o777
    assert mode == 0o600, f"expected 0600, got {oct(mode)}"


def test_seed_defaults_to_configured_writable_data_directory(seed_db, tmp_path):
    env = _make_env("appliance")
    seed_db.add(env)
    seed_db.commit()
    state = tmp_path / "writable-state"

    agents.seed_default_local_agent(seed_db, settings=_settings(data_dir=state))
    seed_db.commit()

    token_file = state / "local-agent-token"
    assert token_file.stat().st_mode & 0o777 == 0o600
    assert _local_credential(seed_db, env.id).token_hash == _sha256(token_file.read_text())


def test_failed_token_directory_does_not_commit_orphan_credential(seed_db, tmp_path, monkeypatch):
    from pathlib import Path

    env = _make_env("appliance")
    seed_db.add(env)
    seed_db.commit()
    state = tmp_path / "readonly-state"
    original = Path.mkdir

    def readonly(path, *args, **kwargs):
        if path == state:
            raise OSError("read-only test filesystem")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", readonly)
    with pytest.raises(OSError):
        agents.seed_default_local_agent(seed_db, state, settings=_settings())
    # Startup catches the seed error and commits other recovery work.
    seed_db.commit()
    assert _cred_count(seed_db, env.id) == 0


def test_failed_token_write_rolls_back_only_seed(seed_db, tmp_path, monkeypatch):
    env = _make_env("before-seed")
    seed_db.add(env)
    seed_db.commit()
    env.name = "unrelated-recovery-work"

    def fail_sync(_fd):
        raise OSError("injected write failure")

    monkeypatch.setattr(agents.os, "fsync", fail_sync)
    with pytest.raises(OSError):
        agents.seed_default_local_agent(seed_db, tmp_path, settings=_settings())
    seed_db.commit()
    assert _cred_count(seed_db, env.id) == 0
    assert seed_db.get(Environment, env.id).name == "unrelated-recovery-work"
    assert not (tmp_path / "local-agent-token").exists()



def test_existing_unclaimed_token_is_never_overwritten(seed_db, tmp_path):
    env = _make_env("appliance")
    seed_db.add(env)
    seed_db.commit()
    token_file = tmp_path / "local-agent-token"
    token_file.write_text("existing-private-token")
    with pytest.raises(FileExistsError):
        agents.seed_default_local_agent(seed_db, tmp_path, settings=_settings())
    seed_db.commit()
    assert token_file.read_text() == "existing-private-token"
    assert _cred_count(seed_db, env.id) == 0
    assert not list(tmp_path.glob(".local-agent-*"))


def test_api_cold_start_uses_writable_state_with_readonly_app(tmp_path):
    """Run the real lifespan in a separate process, preserving the suite's loop."""
    import os
    import subprocess
    import sys
    from pathlib import Path

    import yaml

    config = yaml.safe_load(Path(os.environ["CONSOLE_CONFIG"]).read_text())
    config["database_url"] = f"sqlite:///{tmp_path / 'cold.db'}"
    config["data_dir"] = str(tmp_path / "state")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config))
    script = r'''
import asyncio
from pathlib import Path
from unittest.mock import patch
from sqlalchemy import select
from app.config import get_settings
from app.db import init_db, SessionLocal
from app.main import app, lifespan
from app.models import AgentCredential, Environment
from app.services.agents import hash_token

init_db()
with SessionLocal() as db:
    if not db.scalar(select(Environment)):
        db.add(Environment(id="cold-start", name="cold-start"))
        db.commit()
original_mkdir = Path.mkdir

def immutable_app(path, *args, **kwargs):
    if str(path) == "/app" or str(path).startswith("/app/"):
        raise OSError("read-only appliance root")
    return original_mkdir(path, *args, **kwargs)

async def run():
    with patch.object(Path, "mkdir", immutable_app), \
         patch("app.services.talosctl_install.start_ensure"), \
         patch("app.services.pxe_runtime.start_from_db"), \
         patch("app.services.pxe_runtime.stop_all"):
        async with lifespan(app):
            token_file = get_settings().data_dir / "local-agent-token"
            assert token_file.is_file(), "startup did not publish local-agent state"
            assert token_file.stat().st_mode & 0o777 == 0o600
            with SessionLocal() as db:
                credential = db.scalar(select(AgentCredential).where(AgentCredential.name == "local-agent"))
                assert credential is not None
                assert credential.token_hash == hash_token(token_file.read_text())
asyncio.run(run())
'''
    result = subprocess.run(
        [sys.executable, "-c", script],
        env={**os.environ, "CONSOLE_CONFIG": str(config_path)},
        capture_output=True,
        text=True,
        timeout=40,
    )
    assert result.returncode == 0, result.stderr
