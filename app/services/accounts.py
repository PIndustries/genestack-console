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


def _aware(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment


def _refresh_material() -> tuple[str, str, datetime]:
    """Raw refresh token, its SHA-256 hex, and when that token stops working."""
    raw = secrets.token_urlsafe(32)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    expires = _utcnow() + timedelta(hours=get_settings().refresh_ttl_hours)
    return raw, digest, expires


def _refresh_alive(session: SessionToken, now: datetime) -> bool:
    expires = session.refresh_expires_at
    if expires is None:
        return False
    return _aware(expires) > now


def create_session(db: Session, user: User) -> SessionToken:
    """Issue a new session for the user. Does not touch that user's other sessions.

    The raw refresh token is stashed on ``session.raw_refresh_token`` for the
    caller to return once. Only the hash is stored. Caller commits.
    """
    settings = get_settings()
    raw, digest, refresh_expires = _refresh_material()
    session = SessionToken(
        token=secrets.token_urlsafe(32),
        user_id=user.id,
        expires_at=_utcnow() + timedelta(hours=settings.session_ttl_hours),
        refresh_token_hash=digest,
        refresh_expires_at=refresh_expires,
    )
    session.raw_refresh_token = raw  # type: ignore[attr-defined]
    db.add(session)
    db.flush()
    return session


def resolve_session(db: Session, token: str) -> User | None:
    """Return the session's active user, or None.

    An expired bearer is not a reason to drop a refresh token that is still
    inside its own lifetime. The row is deleted when the refresh token is
    missing or also expired.
    """
    session = db.get(SessionToken, token)
    if session is None:
        return None
    now = _utcnow()
    if _aware(session.expires_at) <= now:
        if not _refresh_alive(session, now):
            db.delete(session)
            db.commit()
        return None
    user = db.get(User, session.user_id)
    if user is None or not user.active:
        return None
    return user


def refresh_session(db: Session, raw_refresh_token: str) -> SessionToken | None:
    """Exchange one refresh token for a new bearer and a new refresh token.

    The previous row for that login is deleted. Other logins for the same
    user are left in place. Returns None when the token is unknown, expired,
    or the user is inactive. Caller commits on success. An expired token is
    deleted here.
    """
    presented = (raw_refresh_token or "").strip()
    if not presented:
        return None
    digest = hashlib.sha256(presented.encode("utf-8")).hexdigest()
    session = db.scalar(
        select(SessionToken).where(SessionToken.refresh_token_hash == digest)
    )
    if session is None:
        return None
    now = _utcnow()
    if not _refresh_alive(session, now):
        db.delete(session)
        db.commit()
        return None
    user = db.get(User, session.user_id)
    if user is None or not user.active:
        return None
    settings = get_settings()
    raw, new_digest, refresh_expires = _refresh_material()
    replacement = SessionToken(
        token=secrets.token_urlsafe(32),
        user_id=session.user_id,
        expires_at=now + timedelta(hours=settings.session_ttl_hours),
        refresh_token_hash=new_digest,
        refresh_expires_at=refresh_expires,
    )
    replacement.raw_refresh_token = raw  # type: ignore[attr-defined]
    db.delete(session)
    db.add(replacement)
    db.flush()
    return replacement


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
