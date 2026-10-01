"""Local user accounts: password hashing, session tokens, tenant roles.

Stdlib-only password hashing (PBKDF2-HMAC-SHA256); no new dependencies.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Membership, SessionToken, User, UserRole

PBKDF2_ITERATIONS = 600_000


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def hash_password(password: str) -> str:
    """Hash a password as ``pbkdf2_sha256$<iters>$<salt_hex>$<hash_hex>``."""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS
    )
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str | None) -> bool:
    """Constant-time check of a password against a stored PBKDF2 hash."""
    if not stored:
        return False
    try:
        scheme, iters, salt_hex, hash_hex = stored.split("$")
        if scheme != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            bytes.fromhex(salt_hex),
            int(iters),
        )
    except (ValueError, AttributeError):
        return False
    return hmac.compare_digest(digest.hex(), hash_hex)


def create_user(
    db: Session,
    username: str,
    password: str,
    platform_admin: bool = False,
) -> User:
    """Create a local user with a hashed password. Caller commits."""
    user = User(
        username=username,
        password_hash=hash_password(password),
        platform_admin=platform_admin,
        active=True,
    )
    db.add(user)
    db.flush()
    return user


def create_session(db: Session, user: User) -> SessionToken:
    """Issue a new session token for the user (TTL from settings). Caller commits."""
    ttl_hours = get_settings().session_ttl_hours
    session = SessionToken(
        token=secrets.token_urlsafe(32),
        user_id=user.id,
        expires_at=_utcnow() + timedelta(hours=ttl_hours),
    )
    db.add(session)
    db.flush()
    return session


def resolve_session(db: Session, token: str) -> User | None:
    """Return the session's active user, or None; expired tokens are deleted."""
    session = db.get(SessionToken, token)
    if session is None:
        return None
    expires_at = session.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at <= _utcnow():
        db.delete(session)
        db.commit()
        return None
    user = db.get(User, session.user_id)
    if user is None or not user.active:
        return None
    return user


def delete_session(db: Session, token: str) -> None:
    """Delete a session token if it exists (logout). Caller commits."""
    session = db.get(SessionToken, token)
    if session is not None:
        db.delete(session)


def role_in_tenant(db: Session, user: User, tenant_id: str) -> UserRole | None:
    """The user's membership role in a tenant, or None if not a member."""
    return db.scalar(
        select(Membership.role).where(
            Membership.user_id == user.id,
            Membership.tenant_id == tenant_id,
        )
    )


def memberships_for_user(db: Session, user_id: str) -> list[Membership]:
    """All memberships for a user id."""
    return list(
        db.scalars(select(Membership).where(Membership.user_id == user_id)).all()
    )


def highest_role(db: Session, user: User) -> UserRole:
    """Highest role across all memberships; platform admins are global admins."""
    if user.platform_admin:
        return UserRole.admin
    best = UserRole.viewer
    rank = {UserRole.viewer: 1, UserRole.operator: 2, UserRole.admin: 3}
    for membership in memberships_for_user(db, user.id):
        if rank[membership.role] > rank[best]:
            best = membership.role
    return best
