"""Environment CRUD endpoints."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.deps import (
    check_tenant_access,
    get_db,
    get_env_scoped,
    require_admin,
    require_operator,
    require_viewer,
)
from app.models import Environment, Job, Membership, Tenant
from app.schemas import (
    MASKED_KUBECONFIG_DATA,
    EnvironmentCreate,
    EnvironmentRead,
    EnvironmentUpdate,
    Principal,
)
from app.services import envconfig as envconfig_service
from app.services.crypto import encrypt_secret
from app.services.events import publish_sync
from app.services.inventory import build_inventory_from_environment
from app.services.job_runner import JobRunner
from app.services.ssh_keys import store_key_pair

router = APIRouter(prefix="/api/v1/environments", tags=["environments"])
log = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _publish_environment(
    action: str, environment_id: str, name: str, tenant_id: str | None
) -> None:
    """Publish one lifecycle event after the row is committed.

    The fleet board, the sidebar, and the environment selectors all read
    this event. A closed event loop must not fail the request.
    """
    try:
        publish_sync(
            "environments",
            {
                "type": "environment",
                "action": action,
                "environment_id": environment_id,
                "name": name,
                "tenant_id": tenant_id,
            },
        )
    except RuntimeError:
        log.warning("environment lifecycle event was not published", exc_info=True)


def _default_genestack_config_dir(data_dir: Path, name: str) -> str:
    """Local-hub config dir: ``{data_dir}/environments/{name}/etc-genestack``."""
    safe_name = Path(str(name)).name
    return str(Path(data_dir) / "environments" / safe_name / "etc-genestack")


@router.get("", response_model=list[EnvironmentRead])
def list_environments(
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_viewer),
) -> list[EnvironmentRead]:
    stmt = select(Environment).order_by(Environment.name)
    if not principal.platform_admin:
        # Session users only see environments in tenants they belong to
        member_tenants = select(Membership.tenant_id).where(
            Membership.user_id == principal.user_id
        )
        stmt = stmt.where(Environment.tenant_id.in_(member_tenants))
    rows = db.scalars(stmt).all()
    return [EnvironmentRead.from_orm_env(e) for e in rows]


@router.post("", response_model=EnvironmentRead, status_code=status.HTTP_201_CREATED)
def create_environment(
    body: EnvironmentCreate,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_operator),
) -> EnvironmentRead:
    existing = db.scalar(select(Environment).where(Environment.name == body.name))
    if existing:
        raise HTTPException(
            status_code=409, detail=f"Environment name already exists: {body.name}"
        )

    if body.tenant_id:
        tenant = db.get(Tenant, body.tenant_id)
        if tenant is None:
            raise HTTPException(
                status_code=404,
                detail=f"Tenant '{body.tenant_id}' not found. Verify the tenant exists and the ID is correct.",
            )
        check_tenant_access(db, principal, tenant.id, "operator")
    elif not principal.platform_admin:
        raise HTTPException(
            status_code=403,
            detail=(
                "tenant_id is required when creating an environment without platform-admin privileges. "
                "Provide the ID of a tenant you have operator access to, or escalate your role."
            ),
        )

    settings = get_settings()

    # Encrypt secrets at rest before storing (same treatment as update)
    kubeconfig_data = body.kubeconfig_data
    if kubeconfig_data:
        kubeconfig_data = encrypt_secret(kubeconfig_data)

    config_dir = (body.genestack_config_dir or "").strip()
    if not config_dir:
        config_dir = _default_genestack_config_dir(settings.data_dir, body.name)
        try:
            Path(config_dir).mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

    env = Environment(
        name=body.name,
        region=body.region,
        tier=body.tier,
        description=body.description,
        kubeconfig_path=body.kubeconfig_path,
        deployer_ssh_host=body.deployer_ssh_host,
        deployer_ssh_user=body.deployer_ssh_user,
        genestack_path=body.genestack_path or str(settings.genestack_root),
        genestack_config_dir=config_dir,
        kubeconfig_data=kubeconfig_data,
        dry_run=body.dry_run,
        tenant_id=body.tenant_id,
        metadata_json=body.metadata,
    )
    db.add(env)
    db.flush()
    store_key_pair(env, comment=f"genestack/{body.name}")

    runner = JobRunner(db)
    runner.write_audit(
        actor=principal.username,
        action="environment.create",
        resource_type="environment",
        resource_id=env.id,
        environment_id=env.id,
        details={"name": env.name},
        success=True,
    )
    db.commit()
    db.refresh(env)
    _publish_environment("created", env.id, env.name, env.tenant_id)
    return EnvironmentRead.from_orm_env(env)


@router.get("/{environment_id}", response_model=EnvironmentRead)
def get_environment(
    env: Environment = Depends(get_env_scoped("viewer")),
) -> EnvironmentRead:
    return EnvironmentRead.from_orm_env(env)


@router.get("/{environment_id}/inventory")
def get_environment_inventory(
    db: Session = Depends(get_db),
    env: Environment = Depends(get_env_scoped("viewer")),
) -> dict[str, Any]:
    current = envconfig_service.get_current(db, env)
    servers = current[0].get("servers") if current else None
    return build_inventory_from_environment(env, servers=servers)


@router.patch("/{environment_id}", response_model=EnvironmentRead)
def update_environment(
    body: EnvironmentUpdate,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_operator),
    env: Environment = Depends(get_env_scoped("operator")),
) -> EnvironmentRead:

    data = body.model_dump(exclude_unset=True, by_alias=False)
    # Masked read-back: drop a redacted remote so the stored URL isn't clobbered with its '***' form
    if (
        isinstance(data.get("state_repo_remote"), str)
        and ":***@" in data["state_repo_remote"]
    ):
        data.pop("state_repo_remote", None)
    # Map metadata -> metadata_json
    if "metadata" in data:
        env.metadata_json = data.pop("metadata")
    # Masked sentinel means "unchanged" — keep the stored kubeconfig
    if data.get("kubeconfig_data") == MASKED_KUBECONFIG_DATA:
        data.pop("kubeconfig_data")
    if data.get("kubeconfig_data"):
        data["kubeconfig_data"] = encrypt_secret(data["kubeconfig_data"])
    if "name" in data and data["name"] != env.name:
        clash = db.scalar(
            select(Environment).where(
                Environment.name == data["name"], Environment.id != env.id
            )
        )
        if clash:
            raise HTTPException(
                status_code=409,
                detail=f"Environment name '{data['name']}' already exists (env {clash.id}). Choose a unique name.",
            )

    for key, value in data.items():
        if hasattr(env, key):
            setattr(env, key, value)

    env.updated_at = _utcnow()
    db.add(env)
    db.flush()

    runner = JobRunner(db)
    runner.write_audit(
        actor=principal.username,
        action="environment.update",
        resource_type="environment",
        resource_id=env.id,
        environment_id=env.id,
        details={"fields": list(data.keys())},
        success=True,
    )
    db.commit()
    db.refresh(env)
    _publish_environment("updated", env.id, env.name, env.tenant_id)
    return EnvironmentRead.from_orm_env(env)


@router.delete("/{environment_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_environment(
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_admin),
    env: Environment = Depends(get_env_scoped("admin")),
) -> None:

    # Job -> Environment FK is ondelete="SET NULL", but the ORM relationship
    # cascades delete-orphan; delete dependent jobs explicitly so the history
    # removal is deliberate rather than a side effect of the cascade.
    jobs = db.scalars(select(Job).where(Job.environment_id == env.id)).all()
    for job in jobs:
        db.delete(job)

    runner = JobRunner(db)
    runner.write_audit(
        actor=principal.username,
        action="environment.delete",
        resource_type="environment",
        resource_id=env.id,
        environment_id=env.id,
        details={"name": env.name, "jobs_deleted": len(jobs)},
        success=True,
    )
    deleted_id, deleted_name, deleted_tenant = env.id, env.name, env.tenant_id
    db.delete(env)
    db.commit()
    _publish_environment("deleted", deleted_id, deleted_name, deleted_tenant)


@router.get("/{environment_id}/registry")
def get_environment_registry(
    db: Session = Depends(get_db),
    env: Environment = Depends(get_env_scoped("viewer")),
) -> dict[str, Any]:
    """Pull-through caches and Helm charts for this environment.

    A viewer can read it. Docker or catalog trouble comes back as an
    ``error`` string with empty caches. It does not start a job.
    """
    from app.services import image_registry

    try:
        return image_registry.for_environment(db, env, get_settings())
    except Exception as exc:  # noqa: BLE001
        from app.services.image_registry import default_upstream_rows

        defaults = default_upstream_rows()
        return {
            "environment_id": env.id,
            "environment_name": env.name,
            "bind": "",
            "running": False,
            "ready": False,
            "caches": [],
            "ready_count": 0,
            "cache_count": 0,
            "image_count": 0,
            "last_mirror": None,
            "charts": [],
            "host_source": "",
            "configured_host": "",
            "upstreams": defaults,
            "defaults": defaults,
            "required_images": image_registry.required_image_refs(),
            "extra_images": [],
            "error": str(exc)[:240],
        }


class RegistryUpstreamIn(BaseModel):
    name: str
    remote: str
    port: int
    enabled: bool = True


class RegistryConfigIn(BaseModel):
    host: str = ""
    upstreams: list[RegistryUpstreamIn] = Field(default_factory=list)
    images: list[str] = Field(default_factory=list)


@router.put("/{environment_id}/registry")
def put_environment_registry(
    body: RegistryConfigIn,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_operator),
    env: Environment = Depends(get_env_scoped("operator")),
) -> dict[str, Any]:
    """Save the image-cache address and which registries are mirrored.

    An operator can save it. Saving does not start a container or pull an
    image. Starting the caches is the registry.mirror job.
    """
    from app.services import image_registry

    host = body.host or ""
    upstreams = [row.model_dump() for row in body.upstreams]
    images = list(body.images or [])
    try:
        image_registry.save_registry_config(
            db, env, principal.username, host, upstreams, images
        )
    except envconfig_service.ConfigValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (envconfig_service.ConfigConflictError, IntegrityError) as exc:
        db.rollback()
        raise HTTPException(
            status_code=409, detail="Configuration changed; reload before saving"
        ) from exc
    names = [row["name"] for row in upstreams]
    JobRunner(db).write_audit(
        actor=principal.username,
        action="env.registry.configure",
        resource_type="environment",
        resource_id=env.id,
        environment_id=env.id,
        details={"host": host.strip(), "registries": names, "images": images},
    )
    db.commit()
    return image_registry.for_environment(db, env, get_settings())


def _key_fingerprint(public_key: str) -> Optional[str]:
    """Compute SSH key fingerprint from public key."""
    import subprocess

    if not public_key:
        return None
    try:
        result = subprocess.run(
            ["ssh-keygen", "-l", "-f", "-"],
            input=public_key,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return None


@router.get("/{environment_id}/ssh-key/public", response_model=dict)
def get_ssh_public_key(
    env: Environment = Depends(get_env_scoped("viewer")),
) -> dict:
    """Return the environment's SSH public key."""
    return {
        "public_key": env.ssh_public_key,
        "has_key": bool(env.ssh_public_key),
        "fingerprint": (
            _key_fingerprint(env.ssh_public_key) if env.ssh_public_key else None
        ),
    }


@router.post("/{environment_id}/ssh-key/regenerate", response_model=dict)
def regenerate_ssh_key(
    env: Environment = Depends(get_env_scoped("operator")),
    db: Session = Depends(get_db),
) -> dict:
    """Generate a new SSH key pair for this environment."""
    from app.services.ssh_keys import store_key_pair

    store_key_pair(env, comment=f"genestack/{env.name}")
    db.commit()
    return {
        "public_key": env.ssh_public_key,
        "fingerprint": _key_fingerprint(env.ssh_public_key),
        "message": "SSH key pair regenerated",
    }


@router.get("/{environment_id}/ssh-key/private", response_model=dict)
def download_ssh_private_key(
    env: Environment = Depends(get_env_scoped("operator")),
) -> dict:
    """Return the decrypted private key for download. Operator+ only."""
    from app.services.ssh_keys import get_decrypted_private_key

    private = get_decrypted_private_key(env)
    if not private:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No SSH key pair generated for environment '{env.name}' ({env.id}). "
                "Run the SSH key regeneration endpoint first."
            ),
        )
    return {
        "private_key": private,
        "public_key": env.ssh_public_key,
    }
