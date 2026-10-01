"""Environment CRUD endpoints."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.deps import (
    check_tenant_access,
    get_db,
    get_env_scoped,
    require_admin,
    require_operator,
    require_viewer,
)
from app.models import Environment, Job, Membership, Tenant
from app.config import get_settings
from app.schemas import (
    MASKED_KUBECONFIG_DATA,
    MASKED_MAAS_API_KEY,
    EnvironmentCreate,
    EnvironmentRead,
    EnvironmentUpdate,
    Principal,
)
from app.services.crypto import encrypt_secret
from app.services import envconfig as envconfig_service
from app.services.inventory import build_inventory_from_environment
from app.services.job_runner import JobRunner
from app.services.ssh_keys import store_key_pair

router = APIRouter(prefix="/api/v1/environments", tags=["environments"])


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


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
    maas_api_key_encrypted = body.maas_api_key_encrypted
    if maas_api_key_encrypted:
        maas_api_key_encrypted = encrypt_secret(maas_api_key_encrypted)
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
        maas_url=body.maas_url,
        maas_api_key_encrypted=maas_api_key_encrypted,
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
    # Masked sentinel means "unchanged" — keep the stored key
    if data.get("maas_api_key_encrypted") == MASKED_MAAS_API_KEY:
        data.pop("maas_api_key_encrypted")
    if data.get("kubeconfig_data") == MASKED_KUBECONFIG_DATA:
        data.pop("kubeconfig_data")
    # Encrypt secrets at rest before storing
    for key in ("maas_api_key_encrypted", "kubeconfig_data"):
        if data.get(key):
            data[key] = encrypt_secret(data[key])
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
    db.delete(env)
    db.commit()


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
