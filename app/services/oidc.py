"""Optional OIDC/SSO login flow (authorization-code + id_token verification).

Config-driven (``oidc:`` section of config.yaml) and off by default. Local
accounts and static API keys always remain available as a fallback — OIDC is
purely additive.

Flow:
  1. ``build_authorize_url`` — redirect the browser to the provider, storing a
     short-lived state + nonce server-side (in-memory, one-time use).
  2. The provider redirects back to ``/api/v1/auth/oidc/callback``; the router
     pops the state, calls ``exchange_code`` (joserfc — the authlib project's
     JWT engine — verifies the id_token signature via the provider JWKS;
     iss/aud/exp checked here), and matches the nonce.
  3. ``provision_user`` maps the identity to a local ``User`` (auto-creating
     one on first login), and the router issues a normal console session token
     via ``accounts.create_session``.

Security notes:
  - State and nonce are single-use and expire after STATE_TTL_SECONDS.
  - Tokens, codes, and claim payloads are never logged; error paths raise
    generic HTTP errors without embedding provider responses.
  - The session token is handed to the UI via a URL fragment (``#token=…``),
    which browsers do not send to servers or proxies, so it cannot appear in
    access logs.
"""

from __future__ import annotations

import secrets
import threading
import time
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx
from joserfc import jwt
from joserfc.jwk import KeySet
from joserfc.jwt import JWTClaimsRegistry
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import Membership, Tenant, User, UserRole

STATE_TTL_SECONDS = 600
_HTTP_TIMEOUT = 10.0
_SUPPORTED_ALGS = ["RS256", "RS384", "RS512", "ES256", "ES384", "ES512"]

_state_lock = threading.Lock()
_states: dict[str, dict[str, Any]] = {}  # state -> {"nonce": str, "created": float}

_metadata_lock = threading.Lock()
_metadata_cache: dict[str, dict[str, Any]] = {}  # issuer -> discovery document


def oidc_enabled(settings: Settings) -> bool:
    """Feature gate: enabled flag AND a non-empty issuer URL are both required."""
    return bool(settings.oidc_enabled and settings.oidc_issuer_url.strip())


NATIVE_SESSION_COOKIE = "gsc_console"


# Portal doors that may mint gsc_console. my.genestack.dev is the current door.
# app.genestack.dev remains accepted for consoles still configured on the old host.
HOSTED_PORTAL_ORIGINS = (
    "https://my.genestack.dev",
    "https://app.genestack.dev",
)


def native_cookie_origin(settings: Settings) -> str | None:
    """Trusted hosted origin from configuration, never Host/forwarded headers.

    Hosted native apps and ``/go/{slug}`` expect the issuer and the OIDC
    redirect to be the same portal origin:

      - https://my.genestack.dev/api/v1/auth/oidc/callback
      - https://app.genestack.dev/api/v1/auth/oidc/callback

    The console itself may still listen on loopback. Only this redirect
    enables the ``gsc_console`` session cookie.
    """
    if not oidc_enabled(settings):
        return None
    issuer = settings.oidc_issuer_url.rstrip("/")
    if issuer not in HOSTED_PORTAL_ORIGINS:
        return None
    try:
        redirect = urlsplit(settings.oidc_redirect_url)
        redirect_origin = f"{redirect.scheme}://{redirect.hostname}"
        if (
            redirect_origin != issuer
            or redirect.port not in (None, 443)
            or redirect.username
            or redirect.password
            or redirect.query
            or redirect.fragment
            or redirect.path != "/api/v1/auth/oidc/callback"
        ):
            return None
    except ValueError:
        return None
    return issuer


def fetch_metadata(settings: Settings) -> dict[str, Any]:
    """OIDC discovery document for the configured issuer (cached per issuer)."""
    issuer = settings.oidc_issuer_url.rstrip("/")
    with _metadata_lock:
        cached = _metadata_cache.get(issuer)
        if cached is not None:
            return cached
    resp = httpx.get(
        f"{issuer}/.well-known/openid-configuration", timeout=_HTTP_TIMEOUT
    )
    resp.raise_for_status()
    doc = resp.json()
    if not isinstance(doc, dict) or not doc.get("authorization_endpoint"):
        raise ValueError("Invalid OIDC discovery document")
    with _metadata_lock:
        _metadata_cache[issuer] = doc
    return doc


def _purge_states(now: float) -> None:
    expired = [s for s, v in _states.items() if now - v["created"] > STATE_TTL_SECONDS]
    for s in expired:
        del _states[s]


def _new_state(*, native: bool = False) -> tuple[str, str]:
    """Store a fresh (state, nonce) pair server-side."""
    state = secrets.token_urlsafe(24)
    nonce = secrets.token_urlsafe(24)
    now = time.time()
    with _state_lock:
        _purge_states(now)
        _states[state] = {"nonce": nonce, "created": now, "native": native}
    return state, nonce


def pop_state_details(state: str) -> dict[str, Any] | None:
    """Consume nonce and native mode together; callback parameters cannot change mode."""
    with _state_lock:
        entry = _states.pop(state, None)
    if entry is None or time.time() - entry["created"] > STATE_TTL_SECONDS:
        return None
    return {"nonce": str(entry["nonce"]), "native": bool(entry.get("native", False))}


def pop_state(state: str) -> str | None:
    """Backward-compatible nonce-only consumer for ordinary web flows."""
    entry = pop_state_details(state)
    return entry["nonce"] if entry else None


def build_authorize_url(settings: Settings, *, native: bool = False) -> str:
    """Provider authorize URL with fresh state + nonce (stored server-side)."""
    meta = fetch_metadata(settings)
    if native and native_cookie_origin(settings) is None:
        raise ValueError("Hosted native sign-in is not configured")
    state, nonce = _new_state(native=native)
    params = {
        "response_type": "code",
        "client_id": settings.oidc_client_id,
        "redirect_uri": settings.oidc_redirect_url,
        "scope": "openid email profile",
        "state": state,
        "nonce": nonce,
    }
    return f"{meta['authorization_endpoint']}?{urlencode(params)}"


def exchange_code(settings: Settings, code: str) -> dict[str, Any]:
    """Exchange an authorization code and return validated id_token claims.

    Signature is verified against the provider JWKS via joserfc; exp/nbf/iat
    via ``JWTClaimsRegistry.validate``; iss and aud are checked explicitly here. The
    caller must still match ``claims['nonce']`` against the stored state.
    """
    meta = fetch_metadata(settings)
    resp = httpx.post(
        meta["token_endpoint"],
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": settings.oidc_redirect_url,
            "client_id": settings.oidc_client_id,
            "client_secret": settings.oidc_client_secret,
        },
        timeout=_HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    token = resp.json()
    id_token = token.get("id_token")
    if not id_token:
        raise ValueError("Token response missing id_token")

    jwks_resp = httpx.get(meta["jwks_uri"], timeout=_HTTP_TIMEOUT)
    jwks_resp.raise_for_status()
    key_set = KeySet.import_key_set(jwks_resp.json())
    # joserfc>=1.x returns a Token; claims live on Token.claims and time
    # checks go through JWTClaimsRegistry.validate (Token has no .validate).
    token = jwt.decode(id_token, key_set, algorithms=_SUPPORTED_ALGS)
    claims = dict(token.claims)
    JWTClaimsRegistry().validate(claims)

    issuer = settings.oidc_issuer_url.rstrip("/")
    if claims.get("iss") != issuer:
        raise ValueError("id_token issuer mismatch")
    aud = claims.get("aud")
    if isinstance(aud, str):
        aud = [aud]
    if settings.oidc_client_id not in (aud or []):
        raise ValueError("id_token audience mismatch")
    return claims


def identity_from_claims(claims: dict[str, Any]) -> str:
    """Local username for an OIDC identity: email, then preferred_username, then sub."""
    for key in ("email", "preferred_username", "sub"):
        value = str(claims.get(key) or "").strip()
        if value:
            return value[:64]  # User.username is String(64)
    return ""


def provision_user(
    db: Session, settings: Settings, claims: dict[str, Any]
) -> User | None:
    """Look up or auto-provision the local user for an OIDC identity.

    Existing users (matched by username) are reused untouched. New users get a
    password-less account (OIDC-only; local password login is impossible until
    an admin sets one) and receive tenant memberships based on OIDC claims or
    config defaults.

    OIDC claim handling (new users only):
      - ``gsc_tenant_id``: if present and valid, the user is added to that tenant
        with ``gsc_tenant_role`` (defaults to viewer) — the OIDC provider fully
        controls tenant assignment. ``oidc_default_tenant`` is ignored.
      - ``gsc_tenant_role``: role within the ``gsc_tenant_id`` tenant (viewer,
        operator, or admin). Defaults to viewer if missing or invalid.
      - If ``gsc_tenant_id`` is absent, falls back to ``oidc_default_tenant``
        from config (backwards-compatible).

    Without either claim or config default, new users get no memberships and
    see nothing until an admin adds them. Caller commits.
    """
    username = identity_from_claims(claims)
    if not username:
        return None
    user = db.scalar(select(User).where(User.username == username))
    if user is not None:
        return user
    user = User(
        username=username, password_hash=None, platform_admin=False, active=True
    )
    db.add(user)
    db.flush()

    claim_tenant_id = str(claims.get("gsc_tenant_id") or "").strip()
    claim_role_str = str(claims.get("gsc_tenant_role") or "viewer").strip().lower()

    try:
        claim_role = UserRole(claim_role_str)
    except ValueError:
        claim_role = UserRole.viewer

    if claim_tenant_id:
        tenant = db.get(Tenant, claim_tenant_id)
        if tenant is not None:
            db.add(
                Membership(
                    user_id=user.id,
                    tenant_id=tenant.id,
                    role=claim_role,
                )
            )
            db.flush()
            return user

    tenant_name = settings.oidc_default_tenant.strip()
    if tenant_name:
        tenant = db.scalar(select(Tenant).where(Tenant.name == tenant_name))
        if tenant is None:
            tenant = Tenant(
                name=tenant_name,
                description="Auto-created as the OIDC default tenant",
            )
            db.add(tenant)
            db.flush()
        db.add(
            Membership(
                user_id=user.id,
                tenant_id=tenant.id,
                role=UserRole(settings.oidc_default_role),
            )
        )
        db.flush()
    return user
