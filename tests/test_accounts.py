"""Accounts service: password hashing and session token lifecycle."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.services import accounts


@pytest.fixture
def db(client):  # noqa: ARG001 - client ensures the app/tables exist
    from app.db import SessionLocal

    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def test_password_hash_round_trip():
    stored = accounts.hash_password("s3cret-password")
    scheme, iters, salt_hex, hash_hex = stored.split("$")
    assert scheme == "pbkdf2_sha256"
    assert iters == str(accounts.PBKDF2_ITERATIONS)
    assert salt_hex and hash_hex
    assert accounts.verify_password("s3cret-password", stored) is True


def test_verify_password_rejects_wrong_password():
    stored = accounts.hash_password("s3cret-password")
    assert accounts.verify_password("wrong-password", stored) is False
    assert accounts.verify_password("s3cret-password", None) is False
    assert accounts.verify_password("s3cret-password", "not-a-hash") is False
    assert accounts.verify_password("s3cret-password", "bcrypt$1$2$3") is False


def test_session_create_and_resolve(db):
    username = f"acct-{uuid.uuid4().hex[:8]}"
    user = accounts.create_user(db, username, "pw")
    db.commit()

    session = accounts.create_session(db, user)
    # Capture before commit: SQLite returns naive datetimes on refresh
    expires_at = session.expires_at
    db.commit()

    assert session.token
    assert expires_at > datetime.now(timezone.utc)

    resolved = accounts.resolve_session(db, session.token)
    assert resolved is not None
    assert resolved.id == user.id

    assert accounts.resolve_session(db, "no-such-token") is None


def test_resolve_session_expires_and_deletes(db):
    username = f"acct-exp-{uuid.uuid4().hex[:8]}"
    user = accounts.create_user(db, username, "pw")
    db.commit()
    session = accounts.create_session(db, user)
    session.expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
    session.refresh_expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
    db.commit()
    token = session.token

    assert accounts.resolve_session(db, token) is None

    from app.models import SessionToken

    assert db.get(SessionToken, token) is None


def test_resolve_session_keeps_row_while_refresh_is_alive(db):
    """An expired bearer does not delete a refresh token that still works."""
    from app.models import SessionToken

    username = f"acct-hold-{uuid.uuid4().hex[:8]}"
    user = accounts.create_user(db, username, "pw")
    db.commit()
    session = accounts.create_session(db, user)
    session.expires_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    db.commit()
    token = session.token

    assert accounts.resolve_session(db, token) is None
    assert db.get(SessionToken, token) is not None


def test_role_helpers(db):
    from app.models import Membership, Tenant, UserRole

    username = f"acct-roles-{uuid.uuid4().hex[:8]}"
    user = accounts.create_user(db, username, "pw")
    tenant_a = Tenant(name=f"acct-tenant-a-{uuid.uuid4().hex[:8]}")
    tenant_b = Tenant(name=f"acct-tenant-b-{uuid.uuid4().hex[:8]}")
    db.add_all([tenant_a, tenant_b])
    db.flush()
    db.add(Membership(user_id=user.id, tenant_id=tenant_a.id, role=UserRole.viewer))
    db.add(Membership(user_id=user.id, tenant_id=tenant_b.id, role=UserRole.operator))
    db.commit()

    assert accounts.role_in_tenant(db, user, tenant_a.id) == UserRole.viewer
    assert accounts.role_in_tenant(db, user, tenant_b.id) == UserRole.operator
    assert accounts.role_in_tenant(db, user, "missing-tenant") is None
    assert accounts.highest_role(db, user) == UserRole.operator

    admin = accounts.create_user(
        db, f"acct-admin-{uuid.uuid4().hex[:8]}", "pw", platform_admin=True
    )
    db.commit()
    assert accounts.highest_role(db, admin) == UserRole.admin
