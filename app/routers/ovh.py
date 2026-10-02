"""OVH dedicated-server import endpoints.

Scopes:

- Platform-scoped (``/api/v1/ovh*``): account management is a platform-admin
  action (app key/secret identify the console to an OVH account). The
  consumer key is stored per ACCOUNT (created/approved in the
  admin UI; list + BYOI reinstall + IPMI + vRack attach); the status
  endpoint reports account + key counts.
- Environment-scoped (``/api/v1/environments/{id}/ovh*``): account picker
  (viewer), server listing with role suggestion (viewer) — both use the
  bound account's consumer key.

Endpoint choices are the three OVHcloud regions OVH documents (EU/US/CA);
any value OVH itself accepts is still allowed for API users, the UI just
offers the dropdown. The app secret is encrypted on the OvhAccount row and
never returned by the API.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import ROLE_RANK, resolve_principal
from app.deps import (
    check_tenant_access,
    get_db,
    get_env_scoped,
    require_admin,
    require_operator,
    require_viewer,
)
from app.models import Environment, OvhAccount
from app.schemas import Principal
from app.services.crypto import decrypt_secret, encrypt_secret
from app.services import envconfig as envconfig_service
from app.services.ovh import (
    CONSUMER_KEY_RULES,
    OvhClient,
    OvhError,
    OVH_ENDPOINTS,
    assign_roles,
    planned_ovh_tags,
)
from app.services.job_runner import ConflictError, execute_operation

log = logging.getLogger(__name__)

router = APIRouter(tags=["ovh"])


def _validate_endpoint(value: str) -> str:
    """Normalise + validate the endpoint.

    Accepts the three OVH-documented regions either as a code (``eu``/``us``/
    ``ca``) or as a URL (canonicalised, e.g. trailing slash / missing ``/1.0``
    / ``http`` scheme all normalise). Any other URL is allowed custom — OVH
    may host further regions — and is canonicalised too. A dotless, schemeless
    value is treated as a region code, so typos are rejected instead of
    becoming ``https://typo/1.0``.
    """
    v = (value or "").strip().rstrip("/")
    if not v:
        raise ValueError("endpoint is required")
    if "://" not in v and "." not in v:
        # Region code form.
        for e in OVH_ENDPOINTS:
            if e["region"].lower() == v.lower():
                return e["endpoint"]
        raise ValueError(f"unknown OVH region '{v}' (expected eu, us or ca, or a URL)")
    # URL form: strip a trailing /1.0 for comparison; store the canonical form.
    base = v.removesuffix("/1.0")
    if not base.startswith(("http://", "https://")):
        base = "https://" + base
    base = base.replace("http://", "https://", 1)  # OVH API is HTTPS-only
    for e in OVH_ENDPOINTS:
        if base == e["endpoint"].removesuffix("/1.0"):
            return e["endpoint"]
    # Custom endpoint: accept it (OVH may host further regions), canonicalised.
    return base if base.endswith("/1.0") else base + "/1.0"


# ----------------------------------------------------------------------
# Request / response schemas
# ----------------------------------------------------------------------


class OvhAccountCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    endpoint: str = Field(min_length=1, max_length=512)
    app_key: str = Field(min_length=1, max_length=256)
    app_secret: str = Field(min_length=1)

    @field_validator("endpoint")
    @classmethod
    def _endpoint(cls, v: str) -> str:
        return _validate_endpoint(v)


class OvhAccountUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    endpoint: str | None = Field(default=None, min_length=1, max_length=512)
    app_key: str | None = Field(default=None, min_length=1, max_length=256)
    app_secret: str | None = Field(default=None, min_length=1)
    # Stored directly after the operator approves it in OVH (blank keeps
    # the current key).
    consumer_key: str | None = Field(default=None, min_length=1, max_length=256)

    @field_validator("endpoint")
    @classmethod
    def _endpoint(cls, v: str | None) -> str | None:
        return _validate_endpoint(v) if v is not None else None


class AccountConsumerKeySave(BaseModel):
    consumer_key: str = Field(min_length=1, max_length=256)


class OvhBind(BaseModel):
    account_id: str = Field(min_length=1, max_length=36)


class OvhFabricPut(BaseModel):
    vrack: str | None = Field(default=None, max_length=128)
    vlan_id: int | None = Field(default=None, ge=0, le=4000)
    private_cidr: str | None = Field(default=None, max_length=64)


class OvhVrackAttach(BaseModel):
    vrack: str | None = Field(default=None, max_length=128)
    server_hostnames: list[str] | None = None
    run_sync: bool = False


class OvhFabricProvision(BaseModel):
    vrack: str | None = Field(default=None, max_length=128)
    vlan_id: int | None = Field(default=None, ge=0, le=4000)
    private_cidr: str | None = Field(default=None, max_length=64)
    assign_ips: bool = True
    attach: bool = False


class OvhNicPut(BaseModel):
    hostname: str = Field(min_length=1, max_length=256)
    public_ip: str | None = Field(default=None, max_length=64)
    private_ip: str | None = Field(default=None, max_length=64)
    public_mac: str | None = Field(default=None, max_length=64)
    private_mac: str | None = Field(default=None, max_length=64)
    vrack_vni: str | None = Field(default=None, max_length=128)


class OvhByoiReinstall(BaseModel):
    operating_system: str | None = Field(
        default=None,
        max_length=256,
        description="OVH OS template; default byoi_64 when a Talos image URL is set",
    )
    server_hostnames: list[str] | None = Field(
        default=None,
        description="Only reinstall these config server hostnames (default: all OVH-owned servers)",
    )
    image_url: str | None = Field(
        default=None,
        max_length=2048,
        description="Override talos.image_url for this reinstall",
    )
    wait: bool = Field(
        default=True,
        description="Wait for OVH install/status and Talos :50000 after POST reinstall",
    )
    run_sync: bool = Field(
        default=False,
        description="Run the job inline and return it finished (default: queued for the worker)",
    )


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _account_payload(
    account: OvhAccount,
    *,
    include_app_key: bool = False,
    db: Session | None = None,
) -> dict[str, Any]:
    """Public shape: never includes the app secret or the consumer key."""
    payload: dict[str, Any] = {
        "id": account.id,
        "name": account.name,
        "endpoint": account.endpoint,
        "has_consumer_key": bool((account.consumer_key_encrypted or "").strip()),
        "created_at": account.created_at,
    }
    if include_app_key:
        payload["app_key"] = account.app_key
    if db is not None:
        payload["bound_environment_ids"] = list(
            db.scalars(
                select(Environment.id).where(Environment.ovh_account_id == account.id)
            ).all()
        )
    return payload


def _load_bound_account(db: Session, env: Environment) -> OvhAccount:
    """The account an env lists servers from, with its consumer key attached.

    404 when unbound; 503 when the account has no approved consumer key yet
    (the admin UI's Connect flow creates one).
    """
    if not env.ovh_account_id:
        raise HTTPException(
            status_code=404,
            detail="This environment is not bound to an OVH account (Admin → OVH).",
        )
    account = _load_account(db, env.ovh_account_id)
    if not (account.consumer_key_encrypted or "").strip():
        raise HTTPException(
            status_code=503,
            detail=(
                f"OVH account '{account.name}' has no approved consumer key yet — "
                "create one under Admin → OVH accounts → Connect."
            ),
        )
    return account


def _adopt_ovh_servers(
    db: Session,
    env: Environment,
    actor: str | None,
    account: OvhAccount,
) -> dict[str, Any]:
    """Tag config servers that match this account's live inventory as source:ovh.

    Best-effort helper: OVH API failures raise OvhError for the caller to
    decide whether to fail the request (adopt endpoint) or ignore (bind).
    """
    consumer_key = decrypt_secret(account.consumer_key_encrypted) or ""
    if not consumer_key:
        return {"adopted": [], "version": None, "skipped": "no consumer key"}
    current = envconfig_service.get_current(db, env)
    if current is None:
        return {"adopted": [], "version": None, "skipped": "no config document"}
    client = _client_for_account(account, consumer_key=consumer_key)
    try:
        inventory = client.list_dedicated_servers()
    finally:
        client.close()
    tags = planned_ovh_tags(current[0].get("servers") or {}, inventory)
    row, adopted = envconfig_service.adopt_ovh_identity(
        db, env, actor, tags=tags, inventory=inventory
    )
    return {
        "adopted": adopted,
        "version": row.version if row is not None else None,
        "skipped": None,
    }


def _load_account(db: Session, account_id: str) -> OvhAccount:
    account = db.get(OvhAccount, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="OVH account not found")
    return account


def _client_for_account(account: OvhAccount, consumer_key: str = "") -> OvhClient:
    client = OvhClient(
        endpoint=account.endpoint,
        app_key=account.app_key,
        app_secret=decrypt_secret(account.app_secret_encrypted) or "",
        consumer_key=consumer_key,
    )
    if not client.configured:
        client.close()
        raise HTTPException(
            status_code=503,
            detail=f"OVH account '{account.name}' has incomplete credentials",
        )
    return client


def _raise_ovh(exc: OvhError) -> None:
    raise HTTPException(status_code=exc.status_code or 502, detail=str(exc)) from exc


# ----------------------------------------------------------------------
# Status
# ----------------------------------------------------------------------


def _status_guard(
    environment_id: str | None = None,
    db: Session = Depends(get_db),
    principal: Principal = Depends(resolve_principal),
) -> Principal:
    """Admin for the platform-wide status; environment viewer when scoped."""
    if not environment_id:
        if ROLE_RANK[principal.role] < ROLE_RANK["admin"]:
            raise HTTPException(
                status_code=403, detail="Platform admin required for OVH status"
            )
        return principal
    env = db.get(Environment, environment_id)
    if not env:
        raise HTTPException(status_code=404, detail="Environment not found")
    if ROLE_RANK[principal.role] < ROLE_RANK["viewer"]:
        raise HTTPException(status_code=403, detail="Insufficient role for OVH status")
    check_tenant_access(db, principal, env.tenant_id, "viewer")
    return principal


@router.get("/api/v1/ovh")
def ovh_status(
    environment_id: str | None = None,
    principal: Principal = Depends(_status_guard),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Report account availability platform-wide, or the account binding for
    a given environment. ``has_consumer_key`` reflects the bound account's
    approved consumer key (keys live on the account, not the env)."""
    account_count = db.scalar(select(func.count()).select_from(OvhAccount)) or 0
    key_count = (
        db.scalar(
            select(func.count())
            .select_from(OvhAccount)
            .where(OvhAccount.consumer_key_encrypted.is_not(None))
        )
        or 0
    )
    out: dict[str, Any] = {
        "ovh_configured": account_count > 0,
        "account_count": account_count,
        "account_with_key_count": key_count,
        "has_consumer_key": False,
        "account_id": None,
        "ovh_environment": False,
    }
    if environment_id:
        env = db.get(Environment, environment_id)
        if not env:
            raise HTTPException(status_code=404, detail="Environment not found")
        out["account_id"] = env.ovh_account_id
        if env.ovh_account_id:
            account = db.get(OvhAccount, env.ovh_account_id)
            out["has_consumer_key"] = bool(
                account and (account.consumer_key_encrypted or "").strip()
            )
        out["ovh_environment"] = bool(env.ovh_account_id)
    return out


# ----------------------------------------------------------------------
# Account management (platform admin)
# ----------------------------------------------------------------------


@router.get("/api/v1/ovh/accounts")
def list_ovh_accounts(
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    """List stored OVH accounts (id/name/endpoint/app_key — never the secret)."""
    accounts = db.scalars(select(OvhAccount).order_by(OvhAccount.created_at)).all()
    return {
        "ok": True,
        "count": len(accounts),
        "accounts": [
            _account_payload(a, include_app_key=True, db=db) for a in accounts
        ],
    }


@router.get("/api/v1/ovh/accounts/overview")
def ovh_accounts_overview(
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_viewer),
) -> dict[str, Any]:
    """Account list + bound environments for non-admin views (Hardware page).

    Secrets and app keys are never included; each account's bound
    environment ids are filtered to what the caller may see (mirrors the
    environments list). Environments without a tenant are platform-admin
    only, so member users only ever see bound tenant environments.
    """
    from app.models import Membership

    env_stmt = select(Environment.id, Environment.tenant_id).order_by(Environment.name)
    if not principal.platform_admin:
        member_tenants = select(Membership.tenant_id).where(
            Membership.user_id == principal.user_id
        )
        env_stmt = env_stmt.where(Environment.tenant_id.in_(member_tenants))
    visible_envs = {eid for eid, _tid in db.execute(env_stmt).all()}

    def _visible_bound_ids(account: OvhAccount) -> list[str]:
        bound = db.scalars(
            select(Environment.id).where(Environment.ovh_account_id == account.id)
        ).all()
        return [eid for eid in bound if eid in visible_envs]

    accounts = db.scalars(select(OvhAccount).order_by(OvhAccount.created_at)).all()
    return {
        "ok": True,
        "count": len(accounts),
        "accounts": [
            {
                **_account_payload(a, include_app_key=True),
                "bound_environment_ids": _visible_bound_ids(a),
            }
            for a in accounts
        ],
    }


@router.post("/api/v1/ovh/accounts", status_code=201)
def create_ovh_account(
    body: OvhAccountCreate,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    """Store a new OVH application credential set."""
    name = body.name.strip()
    if db.scalar(select(OvhAccount).where(OvhAccount.name == name)) is not None:
        raise HTTPException(
            status_code=409, detail=f"OVH account '{name}' already exists"
        )
    account = OvhAccount(
        name=name,
        endpoint=body.endpoint.strip(),
        app_key=body.app_key.strip(),
        app_secret_encrypted=encrypt_secret(body.app_secret.strip()) or "",
    )
    db.add(account)
    db.commit()
    db.refresh(account)
    return _account_payload(account)


@router.put("/api/v1/ovh/accounts/{account_id}")
def update_ovh_account(
    account_id: str,
    body: OvhAccountUpdate,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    """Update name/endpoint/app_key; replace the app secret or consumer key
    when provided. Changing app credentials or endpoint invalidates the
    stored consumer key (it was minted under the old ones)."""
    account = _load_account(db, account_id)
    if body.name is not None:
        name = body.name.strip()
        clash = db.scalar(
            select(OvhAccount).where(
                OvhAccount.name == name, OvhAccount.id != account.id
            )
        )
        if clash is not None:
            raise HTTPException(
                status_code=409, detail=f"OVH account '{name}' already exists"
            )
        account.name = name
    creds_changed = False
    if body.endpoint is not None:
        endpoint = _validate_endpoint(body.endpoint)
        if endpoint != account.endpoint:
            creds_changed = True
        account.endpoint = endpoint
    if body.app_key is not None:
        app_key = body.app_key.strip()
        if app_key != account.app_key:
            creds_changed = True
        account.app_key = app_key
    if body.app_secret is not None:
        account.app_secret_encrypted = encrypt_secret(body.app_secret.strip()) or ""
        creds_changed = True
    if body.consumer_key is not None:
        account.consumer_key_encrypted = encrypt_secret(body.consumer_key.strip()) or ""
        creds_changed = False
    if creds_changed and (account.consumer_key_encrypted or "").strip():
        # The stored key was minted under the old app credentials/endpoint;
        # it will be rejected by OVH, so drop it. Re-run Connect to re-mint.
        account.consumer_key_encrypted = None
    db.commit()
    db.refresh(account)
    return _account_payload(account)


@router.delete("/api/v1/ovh/accounts/{account_id}")
def delete_ovh_account(
    account_id: str,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    """Remove an account; 409 while environments are bound to it."""
    account = _load_account(db, account_id)
    in_use = db.scalar(
        select(func.count())
        .select_from(Environment)
        .where(Environment.ovh_account_id == account.id)
    )
    if in_use:
        raise HTTPException(
            status_code=409,
            detail=f"Account '{account.name}' is in use by {in_use} environment(s)",
        )
    db.delete(account)
    db.commit()
    return {"ok": True}


@router.post("/api/v1/ovh/accounts/{account_id}/test")
def test_ovh_account(
    account_id: str,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    """Reachability probe (GET /auth/time): validates the endpoint URL.
    App-key validity surfaces at consumer-key request time (OVH 403)."""
    account = _load_account(db, account_id)
    client = _client_for_account(account)
    try:
        started = time.monotonic()
        client.ping()
        latency_ms = int((time.monotonic() - started) * 1000)
        return {"ok": True, "latency_ms": latency_ms}
    except OvhError as exc:
        _raise_ovh(exc)
    finally:
        client.close()


@router.get("/api/v1/ovh/accounts/{account_id}/servers")
def preview_ovh_account_servers(
    account_id: str,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    """Preview the account's dedicated servers from the admin accounts card.

    OVH requires an approved consumer key to list servers. The key lives on
    the account now; when one is missing the response carries
    ``consumer_key_required`` so the UI can offer the Connect flow.
    """
    account = _load_account(db, account_id)
    bound = db.scalars(
        select(Environment).where(Environment.ovh_account_id == account.id)
    ).all()
    if not (account.consumer_key_encrypted or "").strip():
        return {
            "ok": True,
            "consumer_key_required": True,
            "bound_environment_ids": [e.id for e in bound],
            "bound_environment_names": [e.name for e in bound],
            "servers": [],
        }
    client = _client_for_account(
        account,
        consumer_key=decrypt_secret(account.consumer_key_encrypted) or "",
    )
    try:
        servers = client.list_dedicated_servers()
    except OvhError as exc:
        _raise_ovh(exc)
    finally:
        client.close()
    return {
        "ok": True,
        "consumer_key_required": False,
        "bound_environment_ids": [e.id for e in bound],
        "bound_environment_names": [e.name for e in bound],
        "servers": assign_roles(servers),
    }


# ----------------------------------------------------------------------
# Account-level: consumer-key lifecycle (platform admin)
# ----------------------------------------------------------------------


@router.post("/api/v1/ovh/accounts/{account_id}/consumer-key/request")
def request_account_consumer_key(
    account_id: str,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    """Mint a consumer key for this OVH account (list + BYOI reinstall + IPMI).

    Returns ``consumer_key`` + ``validation_url``. The admin opens the URL,
    approves it in OVH, and the UI polls ``.../validate`` until ok before
    calling ``.../store``. Keys minted before POST reinstall was in the
    rule set cannot reinstall — Connect again.
    """
    account = _load_account(db, account_id)
    client = _client_for_account(account)
    try:
        data = client.request_consumer_key(access_rules=CONSUMER_KEY_RULES)
    except OvhError as exc:
        _raise_ovh(exc)
    finally:
        client.close()
    return {
        "consumer_key": data["consumerKey"],
        "validation_url": data.get("validationUrl"),
        "state": data.get("state"),
        "account_id": account.id,
        "access_rules": CONSUMER_KEY_RULES,
    }


@router.get("/api/v1/ovh/accounts/{account_id}/consumer-key/validate")
def validate_account_consumer_key(
    account_id: str,
    consumer_key: str,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    """Check ``validationStatus`` for a (not-yet-stored) account consumer key."""
    account = _load_account(db, account_id)
    client = _client_for_account(account, consumer_key=consumer_key)
    try:
        state = client.credential_state(consumer_key)
        return {
            "validation_status": state.get("validationStatus", "unknown"),
            "valid": state.get("validationStatus") == "ok",
        }
    except OvhError as exc:
        _raise_ovh(exc)
    finally:
        client.close()


@router.post("/api/v1/ovh/accounts/{account_id}/consumer-key/store")
def store_account_consumer_key(
    account_id: str,
    body: AccountConsumerKeySave,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    """Persist a validated consumer key on the account (encrypted)."""
    account = _load_account(db, account_id)
    client = _client_for_account(account, consumer_key=body.consumer_key)
    try:
        state = client.credential_state(body.consumer_key)
        if state.get("validationStatus") != "ok":
            raise HTTPException(
                status_code=409,
                detail=(
                    "Consumer key is not validated yet "
                    f"({state.get('validationStatus')}); approve it via the "
                    "validation URL first."
                ),
            )
    except OvhError as exc:
        _raise_ovh(exc)
    finally:
        client.close()
    account.consumer_key_encrypted = encrypt_secret(body.consumer_key.strip()) or ""
    db.commit()
    db.refresh(account)
    return {"ok": True, "has_consumer_key": True, "account_id": account.id}


@router.delete("/api/v1/ovh/accounts/{account_id}/consumer-key")
def delete_account_consumer_key(
    account_id: str,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    """Forget the stored consumer key for an account."""
    account = _load_account(db, account_id)
    if (account.consumer_key_encrypted or "").strip():
        account.consumer_key_encrypted = None
        db.commit()
    return {"ok": True, "has_consumer_key": False}


# ----------------------------------------------------------------------
# Environment-scoped: account binding + server listing
# ----------------------------------------------------------------------


@router.get("/api/v1/environments/{environment_id}/ovh/accounts")
def env_ovh_accounts(
    environment_id: str,
    env: Environment = Depends(get_env_scoped("viewer")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Accounts the wizard/detail-page dropdown offers for this environment.

    Each account now carries ``has_consumer_key`` so the caller can tell
    which can immediately list servers.
    """
    accounts = db.scalars(select(OvhAccount).order_by(OvhAccount.created_at)).all()
    return {
        "ok": True,
        "count": len(accounts),
        "accounts": [_account_payload(a) for a in accounts],
        "bound_account_id": env.ovh_account_id,
    }


@router.post("/api/v1/environments/{environment_id}/ovh/bind")
def bind_ovh_account(
    environment_id: str,
    body: OvhBind,
    env: Environment = Depends(get_env_scoped("operator")),
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_operator),
) -> dict[str, Any]:
    """Bind an environment to an OVH account (enables server import).

    When the account already has an approved consumer key and the env has a
    config document, matching static hosts are tagged ``source: ovh``.
    Adopt failures never fail the bind — the operator can retry via
    ``POST …/ovh/adopt``.
    """
    account = _load_account(db, body.account_id)
    env.ovh_account_id = account.id
    adopted: list[dict[str, str]] = []
    adopt_version = None
    if (account.consumer_key_encrypted or "").strip():
        try:
            result = _adopt_ovh_servers(db, env, principal.username, account)
            adopted = result.get("adopted") or []
            adopt_version = result.get("version")
        except OvhError as exc:
            log.warning("OVH adopt on bind failed for env %s: %s", env.id, exc)
    db.commit()
    db.refresh(env)
    return {
        "ok": True,
        "account_id": account.id,
        "has_consumer_key": bool((account.consumer_key_encrypted or "").strip()),
        "ovh_environment": True,
        "adopted": adopted,
        "adopt_version": adopt_version,
    }


@router.delete("/api/v1/environments/{environment_id}/ovh/bind")
def unbind_ovh_account(
    environment_id: str,
    env: Environment = Depends(get_env_scoped("operator")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Unbind an environment from its OVH account."""
    if env.ovh_account_id:
        env.ovh_account_id = None
        db.commit()
    return {"ok": True, "account_id": None}


@router.post("/api/v1/environments/{environment_id}/ovh/adopt")
def env_ovh_adopt(
    environment_id: str,
    env: Environment = Depends(get_env_scoped("operator")),
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_operator),
) -> dict[str, Any]:
    """Tag config servers that match the bound OVH inventory as ``source: ovh``.

    Matches by IP, then hostname. Rows that already name another source are left alone. No-op
    when every matching host is already tagged. Used by the Inventory tab so
    a fleet imported as ``source: static`` becomes an OVH Talos fleet.
    """
    account = _load_bound_account(db, env)
    try:
        result = _adopt_ovh_servers(db, env, principal.username, account)
    except OvhError as exc:
        _raise_ovh(exc)
    if result.get("version") is not None:
        db.commit()
    return {
        "ok": True,
        "ovh_environment": True,
        "adopted": result.get("adopted") or [],
        "version": result.get("version"),
        "skipped": result.get("skipped"),
    }


@router.get("/api/v1/environments/{environment_id}/ovh/servers")
def list_ovh_servers(
    environment_id: str,
    env: Environment = Depends(get_env_scoped("viewer")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """List the bound account's OVH dedicated servers with suggested roles."""
    account = _load_bound_account(db, env)
    client = _client_for_account(
        account, consumer_key=decrypt_secret(account.consumer_key_encrypted) or ""
    )
    try:
        servers = client.list_dedicated_servers()
    except OvhError as exc:
        _raise_ovh(exc)
    finally:
        client.close()
    return {
        "ok": True,
        "account_id": account.id,
        "count": len(servers),
        "servers": assign_roles(servers),
    }


@router.get("/api/v1/environments/{environment_id}/ovh/vrack")
def env_ovh_vrack_status(
    environment_id: str,
    refresh: bool = Query(
        False,
        description="Talk to OVH (slow). Default is the local/cache snapshot.",
    ),
    env: Environment = Depends(get_env_scoped("viewer")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """vRack membership + VLAN + interconnect for this environment's servers.

    Default is instant (env doc + last OVH snapshot). Pass ``refresh=true``
    to re-query OVH and update the snapshot — the Inventory UI does this
    in the background.
    """
    from app.services import ovh_fabric as fabric

    return fabric.fabric_status(db, env, refresh=refresh)


@router.put("/api/v1/environments/{environment_id}/ovh/vrack")
def env_ovh_vrack_save(
    environment_id: str,
    body: OvhFabricPut,
    env: Environment = Depends(get_env_scoped("operator")),
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_operator),
) -> dict[str, Any]:
    """Persist ovh.vrack / vlan_id / private_cidr as a new config version."""
    row, warnings = envconfig_service.set_ovh_fabric(
        db,
        env,
        principal.username,
        vrack=body.vrack,
        vlan_id=body.vlan_id,
        private_cidr=body.private_cidr,
    )
    db.commit()
    return {
        "ok": True,
        "version": row.version,
        "warnings": warnings,
        "ovh": (envconfig_service.get_current(db, env) or ({}, None))[0].get("ovh")
        or {},
    }


@router.post("/api/v1/environments/{environment_id}/ovh/vrack/attach")
def env_ovh_vrack_attach(
    environment_id: str,
    body: OvhVrackAttach,
    env: Environment = Depends(get_env_scoped("admin")),
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    """Enqueue ``ovh.vrack.attach`` for this environment's dedicated servers."""
    try:
        job = execute_operation(
            db,
            operation="ovh.vrack.attach",
            params={
                **({"vrack": body.vrack} if body.vrack else {}),
                **(
                    {"server_hostnames": body.server_hostnames}
                    if body.server_hostnames
                    else {}
                ),
            },
            environment_id=environment_id,
            created_by=principal.username,
            run_sync=body.run_sync,
        )
    except ConflictError as exc:
        detail: dict[str, Any] = {"message": str(exc)}
        if exc.job_id:
            detail["conflicting_job_id"] = exc.job_id
        raise HTTPException(status_code=409, detail=detail) from exc
    db.refresh(job)
    return {
        "ok": True,
        "job_id": job.id,
        "status": job.status.value,
        "operation": job.operation,
    }


@router.post("/api/v1/environments/{environment_id}/ovh/vrack/provision")
def env_ovh_vrack_provision(
    environment_id: str,
    body: OvhFabricProvision,
    env: Environment = Depends(get_env_scoped("operator")),
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_operator),
) -> dict[str, Any]:
    """Greenfield turnkey: vRack + VLAN + CIDR + private IPs, optional attach."""
    from app.services import ovh_fabric as fabric

    if body.attach:
        # Attach is admin-only (mutating OVH membership).
        if ROLE_RANK.get(getattr(principal, "role", None), 0) < ROLE_RANK.get(
            "admin", 2
        ):
            raise HTTPException(status_code=403, detail="attach requires admin")
    result = fabric.provision_greenfield(
        db,
        env,
        principal.username,
        vrack=body.vrack,
        vlan_id=body.vlan_id,
        private_cidr=body.private_cidr,
        assign_ips=body.assign_ips,
        attach=False,
    )
    if not result.get("ok"):
        raise HTTPException(
            status_code=422, detail=result.get("error") or "provision failed"
        )
    job_payload: dict[str, Any] = {}
    if body.attach:
        try:
            job = execute_operation(
                db,
                operation="ovh.vrack.attach",
                params={
                    **({"vrack": result.get("vrack")} if result.get("vrack") else {})
                },
                environment_id=environment_id,
                created_by=principal.username,
                run_sync=False,
            )
        except ConflictError as exc:
            detail: dict[str, Any] = {"message": str(exc)}
            if exc.job_id:
                detail["conflicting_job_id"] = exc.job_id
            raise HTTPException(status_code=409, detail=detail) from exc
        db.refresh(job)
        job_payload = {
            "job_id": job.id,
            "job_status": job.status.value,
            "operation": job.operation,
        }
    db.commit()
    return {"ok": True, **result, **job_payload}


@router.put("/api/v1/environments/{environment_id}/ovh/vrack/nics")
def env_ovh_vrack_nics(
    environment_id: str,
    body: OvhNicPut,
    env: Environment = Depends(get_env_scoped("operator")),
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_operator),
) -> dict[str, Any]:
    """Edit one server's public/private NIC fields (MAC, VNI, IPs)."""
    current = envconfig_service.get_current(db, env)
    if current is None:
        raise HTTPException(
            status_code=404, detail="environment has no config document"
        )
    servers = current[0].get("servers") or {}
    entry = servers.get(body.hostname) if isinstance(servers, dict) else None
    if not isinstance(entry, dict):
        raise HTTPException(
            status_code=404, detail=f"server '{body.hostname}' is not in inventory"
        )
    cluster_ip = body.private_ip or entry.get("private_ip") or entry.get("ip")
    try:
        row, warnings = envconfig_service.upsert_static_server(
            db,
            env,
            principal.username,
            hostname=body.hostname,
            ip=cluster_ip,
            ssh_user=entry.get("ssh_user"),
            ssh_auth_method=entry.get("ssh_auth_method"),
            roles=list(entry.get("roles") or []),
            source=entry.get("source") or "ovh",
            service_name=entry.get("service_name"),
            public_ip=(
                body.public_ip if body.public_ip is not None else entry.get("public_ip")
            ),
            private_ip=(
                body.private_ip
                if body.private_ip is not None
                else entry.get("private_ip")
            ),
            private_mac=(
                body.private_mac
                if body.private_mac is not None
                else entry.get("private_mac")
            ),
            vrack_vni=(
                body.vrack_vni if body.vrack_vni is not None else entry.get("vrack_vni")
            ),
            public_mac=(
                body.public_mac
                if body.public_mac is not None
                else entry.get("public_mac")
            ),
        )
    except envconfig_service.ConfigValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    db.commit()
    return {
        "ok": True,
        "version": row.version,
        "warnings": warnings,
        "hostname": body.hostname,
    }


# ----------------------------------------------------------------------
# Environment-scoped: BYOI (bring-your-own-image) reinstall
# ----------------------------------------------------------------------


@router.get("/api/v1/environments/{environment_id}/ovh/templates")
def env_ovh_templates(
    environment_id: str,
    server: str | None = None,
    env: Environment = Depends(get_env_scoped("viewer")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """OS install candidates for a bound OVH server.

    ``hardware_templates``: the account-level template list for the
    server's hardware reference (``GET /dedicated/server/osAvailabilities``)
    — the empty list is a legitimate OVH answer for unknown hardware.
    ``compatible``: the per-server ``install/compatibleTemplates`` mapping
    (OVH catalog OSes under ``ovh``; BYOI image data under ``byoi`` — the
    exact keys vary by region, so it is returned raw). ``status``: the
    server's current ``install/status``. Without ``server`` only
    ``configured`` is reported (the candidate lists are per-server).
    """
    account = _load_bound_account(db, env)
    client = _client_for_account(
        account, consumer_key=decrypt_secret(account.consumer_key_encrypted) or ""
    )
    out: dict[str, Any] = {"ok": True, "configured": True, "account_id": account.id}
    try:
        if not server:
            return out
        hardware = ""
        raw = client.get_server(server)
        if not raw:
            # get_server degrades a 404 to {} — an unknown service name.
            out["error"] = f"OVH server '{server}' not found on this account"
            return out
        hardware = str(
            raw.get("hardware")
            or raw.get("commercialRange")
            or raw.get("dedicatedServerName")
            or ""
        )
        try:
            out["hardware"] = hardware or None
            out["hardware_templates"] = (
                client.list_os_templates(hardware) if hardware else []
            )
        except OvhError as exc:
            # osAvailabilities is not guaranteed on every region; degrade
            # rather than hide the per-server lists.
            out["hardware_templates"] = []
            out["hardware_templates_error"] = str(exc)
        try:
            out["compatible"] = client.list_compatible_templates(server)
        except OvhError as exc:
            out["compatible"] = {}
            out["compatible_error"] = str(exc)
        try:
            out["status"] = client.install_status(server)
        except OvhError as exc:
            out["status"] = None
            out["status_error"] = str(exc)
        return out
    except OvhError as exc:
        _raise_ovh(exc)
    finally:
        client.close()


@router.post("/api/v1/environments/{environment_id}/ovh/byoi")
def env_ovh_byoi_reinstall(
    environment_id: str,
    body: OvhByoiReinstall,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_admin),
    env: Environment = Depends(get_env_scoped("admin")),
) -> dict[str, Any]:
    """Enqueue the ``ovh.byoi.reinstall`` job for this environment.

    The job reinstalls this OVH environment's dedicated servers from
    the given OS template (optionally restricted to ``server_hostnames``).
    Queued by default (poll ``GET /api/v1/jobs/{id}`` for the per-server
    task ids and errors); ``run_sync: true`` runs it inline instead.
    """
    hostnames = [h.strip() for h in (body.server_hostnames or []) if h.strip()] or None
    os_name = (body.operating_system or "").strip() or None
    image_url = (body.image_url or "").strip() or None
    try:
        job = execute_operation(
            db,
            operation="ovh.byoi.reinstall",
            params={
                **({"operating_system": os_name} if os_name else {}),
                **({"server_hostnames": hostnames} if hostnames else {}),
                **({"image_url": image_url} if image_url else {}),
                "wait": body.wait,
            },
            environment_id=environment_id,
            created_by=principal.username,
            run_sync=body.run_sync,
        )
    except ConflictError as exc:
        detail: dict[str, Any] = {"message": str(exc)}
        if exc.job_id:
            detail["conflicting_job_id"] = exc.job_id
        raise HTTPException(status_code=409, detail=detail) from exc
    db.refresh(job)
    return {
        "ok": True,
        "job_id": job.id,
        "status": job.status.value,
        "operation": job.operation,
    }
