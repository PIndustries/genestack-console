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
