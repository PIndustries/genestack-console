"""OAuth 2.0 authorization server for console logins.

Grants: authorization code with PKCE (S256), resource-owner password, and
refresh token. Each login is still one session row. Refresh and revoke touch
that row only.

The raw access token, refresh token, and authorization code are returned
once. The database stores SHA-256 for the refresh token and the code.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import OAuthAuthorizationCode, SessionToken, User
from app.services import accounts

SCOPE = "console"
CODE_TTL = timedelta(seconds=60)
_CLIENT_ID = re.compile(r"^[A-Za-z0-9._~-]{1,128}$")
_LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1"})
_NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}


@dataclass(frozen=True)
class IssuedToken:
    """Token values captured before the database commit expires them."""

    access_token: str
    expires_at: datetime
    refresh_token: str
    refresh_expires_at: datetime


def no_store_headers() -> dict[str, str]:
    return dict(_NO_STORE)


def _aware(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment


def expires_in(moment: datetime) -> int:
    """Whole seconds until a token expires. At least one."""
    seconds = int((_aware(moment) - datetime.now(timezone.utc)).total_seconds())
    return max(1, seconds)


def token_payload(issued: IssuedToken) -> dict[str, str | int]:
    """RFC 6749 token response. Extra refresh_expires_in is the refresh life."""
    return {
        "access_token": issued.access_token,
        "token_type": "Bearer",
        "expires_in": expires_in(issued.expires_at),
        "refresh_token": issued.refresh_token,
        "refresh_expires_in": expires_in(issued.refresh_expires_at),
        "scope": SCOPE,
    }


def scope_allowed(raw: str) -> bool:
    """The only scope is ``console``. An empty scope means that scope."""
    text = (raw or "").strip()
    if not text:
        return True
    return all(part == SCOPE for part in text.split())


def client_id_ok(value: str) -> bool:
    return bool(_CLIENT_ID.fullmatch(value or ""))


def redirect_allowed(redirect_uri: str, request_host: str) -> bool:
    """Allow this console's /ui, or a loopback redirect for a local program.

    Any other host is refused so the authorization endpoint cannot send the
    code to a site the operator did not mean.
    """
    if not redirect_uri or len(redirect_uri) > 512:
        return False
    parts = urlsplit(redirect_uri)
    if parts.scheme not in {"http", "https"}:
        return False
    if parts.username or parts.password or parts.fragment:
        return False
    host = (parts.hostname or "").lower().rstrip(".")
    if host in _LOOPBACK:
        return True
    request_netloc = (request_host or "").lower().rstrip(".")
    if parts.netloc.lower() != request_netloc:
        return False
    return parts.path in {"/ui", "/ui/"}


def pkce_ok(verifier: str, challenge: str) -> bool:
    """S256 PKCE. The verifier is 43 to 128 unreserved characters."""
    if not verifier or not 43 <= len(verifier) <= 128:
        return False
    if not re.fullmatch(r"[A-Za-z0-9._~-]+", verifier):
        return False
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    computed = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    if not challenge:
        return False
    return hmac.compare_digest(computed, challenge)


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def issue_session(db: Session, user: User) -> IssuedToken:
    """Insert one session and commit. Other sessions for this user stay."""
    session = accounts.create_session(db, user)
    issued = IssuedToken(
        access_token=session.token,
        expires_at=session.expires_at,
        refresh_token=session.raw_refresh_token,
        refresh_expires_at=session.refresh_expires_at,
    )
    db.commit()
    return issued


def issue_code(
    db: Session,
    user: User,
    client_id: str,
    redirect_uri: str,
    code_challenge: str,
) -> str:
    """Store a one-time code and return the raw value. Caller commits."""
    raw = secrets.token_urlsafe(32)
    db.add(
        OAuthAuthorizationCode(
            code_hash=_hash(raw),
            user_id=user.id,
            client_id=client_id,
            redirect_uri=redirect_uri,
            code_challenge=code_challenge,
            expires_at=datetime.now(timezone.utc) + CODE_TTL,
        )
    )
    db.flush()
    return raw


def exchange_code(
    db: Session,
    raw_code: str,
    client_id: str,
    redirect_uri: str,
    verifier: str,
) -> IssuedToken | None:
    """Trade a code for one new session. The code is deleted either way.

    A mismatch, an expired code, or a bad verifier does not touch any other
    login. Returns None when the code cannot be used.
    """
    presented = (raw_code or "").strip()
    if not presented:
        return None
    row = db.get(OAuthAuthorizationCode, _hash(presented))
    if row is None:
        return None
    now = datetime.now(timezone.utc)
    expired = _aware(row.expires_at) <= now
    user = db.get(User, row.user_id)
    mismatch = (
        expired
        or not client_id_ok(client_id)
        or client_id != row.client_id
        or redirect_uri != row.redirect_uri
        or not pkce_ok(verifier, row.code_challenge)
        or user is None
        or not user.active
    )
    db.delete(row)
    if mismatch or user is None:
        db.commit()
        return None
    return issue_session(db, user)


def revoke_token(db: Session, raw: str, hint: str) -> None:
    """Delete the one session this token belongs to. Unknown tokens are a no-op."""
    presented = (raw or "").strip()
    if not presented:
        return
    row: SessionToken | None = None
    if hint != "refresh_token":
        row = db.get(SessionToken, presented)
    if row is None:
        row = db.scalar(
            select(SessionToken).where(SessionToken.refresh_token_hash == _hash(presented))
        )
    if row is None and hint == "refresh_token":
        row = db.get(SessionToken, presented)
    if row is not None:
        db.delete(row)
        db.commit()


def introspect(db: Session, raw: str, hint: str) -> dict[str, object] | None:
    """Describe one token, or None when it is not an active credential.

    An expired access token is inactive even if its refresh token still works.
    The caller decides whether this principal may see the description.
    """
    presented = (raw or "").strip()
    if not presented:
        return None
    now = datetime.now(timezone.utc)
    if hint != "refresh_token":
        row = db.get(SessionToken, presented)
        if row is not None:
            if _aware(row.expires_at) <= now:
                return None
            user = db.get(User, row.user_id)
            if user is None or not user.active:
                return None
            return _active(user, "Bearer", row.expires_at)
    row = db.scalar(
        select(SessionToken).where(SessionToken.refresh_token_hash == _hash(presented))
    )
    if row is None and hint == "refresh_token":
        row = db.get(SessionToken, presented)
        if row is not None and _aware(row.expires_at) > now:
            user = db.get(User, row.user_id)
            if user is not None and user.active:
                return _active(user, "Bearer", row.expires_at)
        return None
    if row is None:
        return None
    refresh_expires = row.refresh_expires_at
    if refresh_expires is None or _aware(refresh_expires) <= now:
        return None
    user = db.get(User, row.user_id)
    if user is None or not user.active:
        return None
    return _active(user, "refresh_token", row.refresh_expires_at)


def _active(user: User, token_type: str, expires_at: datetime | None) -> dict[str, object]:
    exp = int(_aware(expires_at).timestamp()) if expires_at is not None else 0
    return {
        "active": True,
        "scope": SCOPE,
        "username": user.username,
        "token_type": token_type,
        "exp": exp,
        "sub": user.id,
    }
