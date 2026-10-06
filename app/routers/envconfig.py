"""Environment config document and server assignment endpoints.

All routes are env-scoped via ``get_env_scoped`` (tenant membership enforced).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.deps import get_db, get_env_scoped, require_operator
from app.models import Environment
from app.schemas import Principal
from app.services import envconfig as envconfig_service
from app.services.job_runner import JobRunner

router = APIRouter(prefix="/api/v1/environments/{environment_id}", tags=["envconfig"])


class ConfigPut(BaseModel):
    yaml_text: str
    expected_version: int | None = Field(default=None, ge=0)


class ServerAssign(BaseModel):
    system_id: str = Field(min_length=1)
    hostname: str | None = None
    roles: list[str] = Field(default_factory=list)
    ip: str | None = None


class StaticServer(BaseModel):
    hostname: str = Field(min_length=1)
    ip: str | None = None
    ssh_user: str | None = None
    ssh_auth_method: str | None = None
    ssh_password: str | None = None
    roles: list[str] = Field(default_factory=list)
    source: str | None = None
    service_name: str | None = None
    public_ip: str | None = None
    private_ip: str | None = None
    private_mac: str | None = None
    vrack_vni: str | None = None
    public_mac: str | None = None


class ProviderPut(BaseModel):
    provider: str = Field(min_length=1)
    talos: dict[str, Any] | None = None
    deploy: dict[str, Any] | None = None


class ServerRemove(BaseModel):
    hostname: str = Field(min_length=1)


class ServerAdopt(BaseModel):
    hostnames: list[str] = Field(min_length=1)
    adopt: str = ""


def _version_payload(row) -> dict[str, Any]:
    return {
        "version": row.version,
        "supports_compare_and_swap": True,
        # secrets.*.data is stored encrypted; mask for the API
        "yaml": envconfig_service.mask_yaml_text(row.yaml_text),
        "created_by": row.created_by,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


# ---------------------------------------------------------------------------
# Config document
# ---------------------------------------------------------------------------


@router.get("/config")
def get_config(
    db: Session = Depends(get_db),
    env: Environment = Depends(get_env_scoped("viewer")),
) -> dict[str, Any]:
    current = envconfig_service.get_current(db, env)
    if current is None:
        return {"version": None, "yaml": None, "supports_compare_and_swap": True}
    _doc, row = current
    return _version_payload(row)


@router.put("/config", status_code=status.HTTP_201_CREATED)
def put_config(
    body: ConfigPut,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_operator),
    env: Environment = Depends(get_env_scoped("operator")),
) -> dict[str, Any]:
    try:
        row, warnings = envconfig_service.put_version(
            db,
            env,
            body.yaml_text,
            principal.username,
            expected_version=body.expected_version,
        )
    except (envconfig_service.ConfigConflictError, IntegrityError) as exc:
        db.rollback()
        raise HTTPException(
            status_code=409, detail="Configuration changed; reload before saving"
        ) from exc
    except envconfig_service.ConfigValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    JobRunner(db).write_audit(
        actor=principal.username,
        action="env.config.put",
        resource_type="environment",
        resource_id=env.id,
        environment_id=env.id,
        details={"version": row.version, "warnings": warnings},
    )
    db.commit()
    return {**_version_payload(row), "warnings": warnings}


@router.get("/config/versions")
def list_config_versions(
    db: Session = Depends(get_db),
    env: Environment = Depends(get_env_scoped("viewer")),
) -> list[dict[str, Any]]:
    return [
        {
            "version": row.version,
            "created_by": row.created_by,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }
        for row in envconfig_service.history(db, env)
    ]


@router.get("/config/versions/{version}")
def get_config_version(
    version: int,
    db: Session = Depends(get_db),
    env: Environment = Depends(get_env_scoped("viewer")),
) -> dict[str, Any]:
    row = envconfig_service.get_version(db, env, version)
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"Config version {version} not found for this environment. List versions with GET /config/versions.",
        )
    return _version_payload(row)


@router.get("/config/render")
def render_config(
    db: Session = Depends(get_db),
    env: Environment = Depends(get_env_scoped("viewer")),
) -> dict[str, Any]:
    """Preview: rendered config-dir-relative files for the current document."""
    current = envconfig_service.get_current(db, env)
    if current is None:
        return {"version": None, "files": {}}
    doc, row = current
    try:
        files = envconfig_service.render_to_files(doc, env, get_settings())
    except envconfig_service.ConfigValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    # The preview is an API response — never hand back plaintext secrets
    if envconfig_service.KUBESECRETS_FILENAME in files:
        files[envconfig_service.KUBESECRETS_FILENAME] = (
            envconfig_service.mask_kubesecrets(
                files[envconfig_service.KUBESECRETS_FILENAME]
            )
        )
    return {"version": row.version, "files": files}


@router.get("/config/provider")
def get_config_provider(
    db: Session = Depends(get_db),
    env: Environment = Depends(get_env_scoped("viewer")),
) -> dict[str, Any]:
    """Provider view of the config document (defaults when no version exists)."""
    return envconfig_service.get_provider(db, env)


@router.put("/config/provider")
def put_config_provider(
    body: ProviderPut,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_operator),
    env: Environment = Depends(get_env_scoped("operator")),
) -> dict[str, Any]:
    """Set the deployment provider (+ talos/deploy sections) — new config version."""
    try:
        row, warnings = envconfig_service.set_provider(
            db,
            env,
            principal.username,
            provider=body.provider,
            talos=body.talos,
            deploy=body.deploy,
        )
    except envconfig_service.ConfigValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    JobRunner(db).write_audit(
        actor=principal.username,
        action="env.config.provider",
        resource_type="environment",
        resource_id=env.id,
        environment_id=env.id,
        details={"version": row.version, "provider": body.provider},
    )
    db.commit()
    return {"version": row.version, "warnings": warnings, "provider": body.provider}


# ---------------------------------------------------------------------------
# Servers saved for this environment
# ---------------------------------------------------------------------------


def _validate_roles(roles: list[str]) -> None:
    invalid = [
        r for r in roles if r.lower() not in envconfig_service.VALID_SERVER_ROLES
    ]
    if invalid:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Unknown role(s): {', '.join(invalid)} "
                f"(valid: {', '.join(sorted(envconfig_service.VALID_SERVER_ROLES))})"
            ),
        )


@router.get("/servers")
def list_servers(
    db: Session = Depends(get_db),
    env: Environment = Depends(get_env_scoped("viewer")),
) -> dict[str, Any]:
    """List the servers saved for this environment."""
    current = envconfig_service.get_current(db, env)
    assignments = (current[0].get("servers") or {}) if current else {}

    servers: list[dict[str, Any]] = []
    for hostname, assignment in assignments.items():
        if not isinstance(assignment, dict):
            continue
        servers.append(
            {
                "system_id": assignment.get("system_id"),
                "hostname": hostname,
                "ip": assignment.get("ip"),
                "ssh_user": assignment.get("ssh_user"),
                "ssh_auth_method": assignment.get("ssh_auth_method"),
                "power_state": None,
                "status": None,
                "roles": assignment.get("roles") or [],
                "assigned": True,
                "source": assignment.get("source") or "static",
                "service_name": assignment.get("service_name"),
                "public_ip": assignment.get("public_ip"),
                "private_ip": assignment.get("private_ip"),
                "private_mac": assignment.get("private_mac"),
                "vrack_vni": assignment.get("vrack_vni"),
                "public_mac": assignment.get("public_mac"),
                "nics": assignment.get("nics") or [],
                "adopt": str(assignment.get("adopt") or ""),
            }
        )
    ovh_doc: dict[str, Any] = {}
    fabric: dict[str, Any] = {}
    if current:
        section = current[0].get("ovh")
        if isinstance(section, dict):
            ovh_doc = section
    if env.ovh_account_id:
        from app.services import ovh_fabric as ovh_fabric_service

        fabric = ovh_fabric_service.local_fabric_status(db, env)
        by_host = {
            str(s.get("hostname")): s
            for s in (fabric.get("servers") or [])
            if isinstance(s, dict) and s.get("hostname")
        }
        for row in servers:
            extra = by_host.get(str(row.get("hostname") or ""))
            if not extra:
                continue
            if extra.get("attached_to") and not row.get("attached_to"):
                row["attached_to"] = extra.get("attached_to")
            if extra.get("nics") and not row.get("nics"):
                row["nics"] = extra.get("nics")
            if extra.get("public_mac") and not row.get("public_mac"):
                row["public_mac"] = extra.get("public_mac")
    return {
        "ovh_bound": bool(env.ovh_account_id),
        "ovh": ovh_doc,
        "fabric": fabric,
        "count": len(servers),
        "servers": servers,
    }


@router.get("/servers/reach")
def server_reach(
    db: Session = Depends(get_db),
    env: Environment = Depends(get_env_scoped("viewer")),
) -> dict[str, Any]:
    """Whether each saved address is up, reachable, and authenticated.

    Read-only. It does not install, reboot, or change the inventory.
    """
    from app.services import host_reach

    return host_reach.probe_environment(db, env)


@router.post("/servers/assign", status_code=status.HTTP_201_CREATED)
def assign_server(
    body: ServerAssign,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_operator),
    env: Environment = Depends(get_env_scoped("operator")),
) -> dict[str, Any]:
    """Save a role assignment as a new config version."""
    _validate_roles(body.roles)
    try:
        row, warnings = envconfig_service.assign_server(
            db,
            env,
            principal.username,
            system_id=body.system_id,
            hostname=body.hostname,
            roles=[r.lower() for r in body.roles],
            ip=body.ip,
        )
    except envconfig_service.ConfigValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    JobRunner(db).write_audit(
        actor=principal.username,
        action="env.servers.assign",
        resource_type="environment",
        resource_id=env.id,
        environment_id=env.id,
        details={
            "system_id": body.system_id,
            "hostname": body.hostname,
            "roles": body.roles,
            "version": row.version,
        },
    )
    db.commit()
    return {
        "version": row.version,
        "warnings": warnings,
        "server": {
            "system_id": body.system_id,
            "hostname": body.hostname or body.system_id,
            "roles": [r.lower() for r in body.roles],
            "ip": body.ip,
            "source": "static",
            "assigned": True,
        },
    }


@router.post("/servers/static", status_code=status.HTTP_201_CREATED)
def upsert_static_server(
    body: StaticServer,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_operator),
    env: Environment = Depends(get_env_scoped("operator")),
) -> dict[str, Any]:
    """Add or update a host by address. Stored as a new config version."""
    _validate_roles(body.roles)
    try:
        row, warnings = envconfig_service.upsert_static_server(
            db,
            env,
            principal.username,
            hostname=body.hostname,
            ip=body.ip,
            ssh_user=body.ssh_user,
            ssh_auth_method=body.ssh_auth_method,
            ssh_password=body.ssh_password,
            roles=[r.lower() for r in body.roles],
            source=body.source,
            service_name=body.service_name,
            public_ip=body.public_ip,
            private_ip=body.private_ip,
            private_mac=body.private_mac,
            vrack_vni=body.vrack_vni,
            public_mac=body.public_mac,
        )
    except envconfig_service.ConfigValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    JobRunner(db).write_audit(
        actor=principal.username,
        action="env.servers.static",
        resource_type="environment",
        resource_id=env.id,
        environment_id=env.id,
        details={
            "hostname": body.hostname,
            "roles": body.roles,
            "version": row.version,
        },
    )
    db.commit()
    return {
        "version": row.version,
        "warnings": warnings,
        "server": {
            "system_id": None,
            "hostname": body.hostname,
            "ip": body.ip,
            "ssh_user": body.ssh_user,
            "ssh_auth_method": body.ssh_auth_method,
            "roles": [r.lower() for r in body.roles],
            "source": body.source or "static",
            "service_name": body.service_name,
            "public_ip": body.public_ip,
            "private_ip": body.private_ip,
            "private_mac": body.private_mac,
            "vrack_vni": body.vrack_vni,
            "assigned": True,
        },
    }


@router.post("/servers/remove")
def remove_server(
    body: ServerRemove,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_operator),
    env: Environment = Depends(get_env_scoped("operator")),
) -> dict[str, Any]:
    """Remove a server entry from the doc — stored as a new config version."""
    result = envconfig_service.remove_server(
        db, env, principal.username, hostname=body.hostname
    )
    if result is None:
        raise HTTPException(
            status_code=404,
            detail=f"Server '{body.hostname}' not found in this environment's config. List servers with GET /servers.",
        )
    row, warnings = result
    JobRunner(db).write_audit(
        actor=principal.username,
        action="env.servers.remove",
        resource_type="environment",
        resource_id=env.id,
        environment_id=env.id,
        details={"hostname": body.hostname, "version": row.version},
    )
    db.commit()
    return {"version": row.version, "warnings": warnings, "removed": body.hostname}


@router.post("/servers/adopt")
def adopt_servers(
    body: ServerAdopt,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_operator),
    env: Environment = Depends(get_env_scoped("operator")),
) -> dict[str, Any]:
    """Record hosts that already have an OS, or clear that record.

    ``adopt`` is ``kubespray`` or empty. This does not reboot, install, or
    start a playbook. Stored as a new config version when the mark changes.
    """
    try:
        row, warnings = envconfig_service.set_server_adopt(
            db,
            env,
            principal.username,
            hostnames=body.hostnames,
            adopt=body.adopt,
        )
    except envconfig_service.ConfigValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    JobRunner(db).write_audit(
        actor=principal.username,
        action="env.servers.adopt",
        resource_type="environment",
        resource_id=env.id,
        environment_id=env.id,
        details={
            "hostnames": body.hostnames,
            "adopt": body.adopt or "",
            "version": row.version,
            "warnings": warnings,
        },
    )
    db.commit()
    return {
        "version": row.version,
        "warnings": warnings,
        "adopt": (body.adopt or "").strip(),
    }
