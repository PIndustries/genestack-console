"""Native OpenStack (Horizon-like) management endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.config import Settings, get_settings
from app.deps import get_env_scoped
from app.models import Environment
from app.services import openstack_ops
from app.services.demo import canned_cloud_inventory, is_demo_env
from app.services.envcontext import build_context

router = APIRouter(prefix="/api/v1", tags=["cloud"])


class ServerCreateBody(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    image: str = Field(min_length=1, max_length=81)
    flavor: str = Field(min_length=1, max_length=81)
    network: str = Field(min_length=1, max_length=81)
    key_name: str | None = Field(default=None, max_length=81)


class VolumeCreateBody(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    size: int = Field(ge=1, le=16384)


class NetworkCreateBody(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    cidr: str | None = Field(default=None, max_length=32)
    external: bool = False


class SecurityGroupRuleBody(BaseModel):
    direction: str = Field(min_length=1, max_length=16)
    ethertype: str = Field(default="IPv4", min_length=1, max_length=8)
    protocol: str | None = Field(default=None, max_length=16)
    port_range_min: int | None = Field(default=None, ge=0, le=65535)
    port_range_max: int | None = Field(default=None, ge=0, le=65535)
    remote_ip_prefix: str | None = Field(default=None, max_length=64)


class RouterCreateBody(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    external_network: str = Field(min_length=1, max_length=36)


class RouterInterfaceBody(BaseModel):
    subnet_id: str = Field(min_length=1, max_length=36)


class QuotaComputeBody(BaseModel):
    instances: int | None = Field(default=None, ge=-1, le=1_000_000)
    cores: int | None = Field(default=None, ge=-1, le=1_000_000)
    ram: int | None = Field(default=None, ge=-1, le=1_000_000)


class QuotaNetworkBody(BaseModel):
    network: int | None = Field(default=None, ge=-1, le=1_000_000)
    subnet: int | None = Field(default=None, ge=-1, le=1_000_000)
    router: int | None = Field(default=None, ge=-1, le=1_000_000)
    floatingip: int | None = Field(default=None, ge=-1, le=1_000_000)
    security_group: int | None = Field(default=None, ge=-1, le=1_000_000)
    port: int | None = Field(default=None, ge=-1, le=1_000_000)


class QuotaUpdateBody(BaseModel):
    compute: QuotaComputeBody | None = None
    network: QuotaNetworkBody | None = None


class VolumeAttachBody(BaseModel):
    server_id: str = Field(min_length=1, max_length=36)
    volume_id: str = Field(min_length=1, max_length=36)


class FloatingIpCreateBody(BaseModel):
    network: str = Field(min_length=1, max_length=81)


class FloatingIpAssociateBody(BaseModel):
    server_id: str = Field(min_length=1, max_length=36)
    address: str = Field(min_length=7, max_length=15)


class FloatingIpDisassociateBody(BaseModel):
    address: str | None = Field(default=None, max_length=36)
    id: str | None = Field(default=None, max_length=36)


class ImageCreateBody(BaseModel):
    name: str = Field(min_length=1, max_length=81)
    disk_format: str = Field(default="qcow2", max_length=16)
    container_format: str = Field(default="bare", max_length=16)
    visibility: str = Field(default="private", max_length=16)
    url: str | None = Field(default=None, max_length=2048)
    copy_from: str | None = Field(default=None, max_length=2048)


class ImagePatchBody(BaseModel):
    name: str | None = Field(default=None, max_length=81)
    visibility: str | None = Field(default=None, max_length=16)


class FlavorCreateBody(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    vcpus: int = Field(ge=1, le=256)
    ram: int = Field(ge=1, le=1_048_576)
    disk: int = Field(ge=0, le=16384)


class KeypairCreateBody(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    public_key: str | None = Field(default=None, max_length=16384)


class SecurityGroupCreateBody(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    description: str | None = Field(default=None, max_length=255)


class VolumeExtendBody(BaseModel):
    size: int = Field(ge=1, le=16384)


class VolumeSnapshotBody(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    force: bool = False


class ServerResizeBody(BaseModel):
    flavor: str = Field(min_length=1, max_length=81)


class ServerRebuildBody(BaseModel):
    image: str = Field(min_length=1, max_length=81)


class ServerSnapshotBody(BaseModel):
    name: str = Field(min_length=1, max_length=81)


class ServerSecurityGroupBody(BaseModel):
    name: str = Field(min_length=1, max_length=81)


class SubnetCreateBody(BaseModel):
    network: str = Field(min_length=1, max_length=36)
    cidr: str = Field(min_length=1, max_length=80)
    name: str | None = Field(default=None, max_length=64)


class ProjectCreateBody(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    description: str | None = Field(default=None, max_length=255)
    enabled: bool = True


class ProjectPatchBody(BaseModel):
    name: str | None = Field(default=None, max_length=64)
    description: str | None = Field(default=None, max_length=255)
    enabled: bool | None = None


class UserCreateBody(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=8, max_length=128)
    project: str | None = Field(default=None, max_length=81)


class UserPatchBody(BaseModel):
    enabled: bool | None = None
    password: str | None = Field(default=None, min_length=8, max_length=128)
    name: str | None = Field(default=None, max_length=64)


class LoadBalancerCreateBody(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    vip_subnet_id: str = Field(min_length=1, max_length=36)


class DnsZoneCreateBody(BaseModel):
    name: str = Field(min_length=1, max_length=253)
    email: str = Field(min_length=3, max_length=254)
    type: str = Field(default="PRIMARY", max_length=16)


class SecretCreateBody(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    payload: str = Field(min_length=1, max_length=16384)


SERVER_POWER_ACTIONS = (
    "start",
    "stop",
    "reboot",
    "hard-reboot",
    "pause",
    "unpause",
    "suspend",
    "resume",
    "lock",
    "unlock",
    "rescue",
    "unrescue",
    "shelve",
    "unshelve",
    "shelve-offload",
    "confirm-resize",
    "revert-resize",
)


def _dry_run(env: Environment, settings: Settings) -> bool:
    ctx = build_context(env, settings)
    try:
        return bool(ctx.dry_run)
    finally:
        ctx.cleanup()


def _result(env: Environment, result: dict[str, Any]) -> dict[str, Any]:
    if result.get("ok"):
        openstack_ops.invalidate_cloud_cache(env.id)
    if result.get("returncode") == 2 and not result.get("ok"):
        raise HTTPException(
            status_code=400, detail=result.get("error") or "invalid request"
        )
    return {"environment_id": env.id, **result}


@router.get("/environments/{environment_id}/cloud")
def get_environment_cloud(
    refresh: bool = Query(default=False),
    env: Environment = Depends(get_env_scoped("viewer")),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Horizon-style inventory. HTTP 200 even when OpenStack is unreachable."""
    if is_demo_env(env):
        return {"environment_id": env.id, **canned_cloud_inventory()}
    result = openstack_ops.cloud_inventory(env, settings, refresh=refresh)
    return {"environment_id": env.id, **result}


@router.post("/environments/{environment_id}/cloud/servers")
def create_environment_server(
    body: ServerCreateBody,
    env: Environment = Depends(get_env_scoped("operator")),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    result = openstack_ops.server_create(
        env,
        settings,
        name=body.name,
        image=body.image,
        flavor=body.flavor,
        network=body.network,
        key_name=body.key_name,
        dry_run=_dry_run(env, settings),
    )
    return _result(env, result)


@router.post("/environments/{environment_id}/cloud/servers/{server_id}/resize")
def resize_environment_server(
    server_id: str,
    body: ServerResizeBody,
    env: Environment = Depends(get_env_scoped("operator")),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    result = openstack_ops.server_resize(
        env, settings, server_id, flavor=body.flavor, dry_run=_dry_run(env, settings)
    )
    return _result(env, result)


@router.post("/environments/{environment_id}/cloud/servers/{server_id}/rebuild")
def rebuild_environment_server(
    server_id: str,
    body: ServerRebuildBody,
    env: Environment = Depends(get_env_scoped("operator")),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    result = openstack_ops.server_rebuild(
        env, settings, server_id, image=body.image, dry_run=_dry_run(env, settings)
    )
    return _result(env, result)


@router.post("/environments/{environment_id}/cloud/servers/{server_id}/snapshot")
def snapshot_environment_server(
    server_id: str,
    body: ServerSnapshotBody,
    env: Environment = Depends(get_env_scoped("operator")),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    result = openstack_ops.server_snapshot(
        env, settings, server_id, name=body.name, dry_run=_dry_run(env, settings)
    )
    return _result(env, result)


@router.post("/environments/{environment_id}/cloud/servers/{server_id}/security-groups")
def add_environment_server_security_group(
    server_id: str,
    body: ServerSecurityGroupBody,
    env: Environment = Depends(get_env_scoped("operator")),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    result = openstack_ops.server_security_group_add(
        env, settings, server_id, name=body.name, dry_run=_dry_run(env, settings)
    )
    return _result(env, result)


@router.delete(
    "/environments/{environment_id}/cloud/servers/{server_id}/security-groups/{name}"
)
def remove_environment_server_security_group(
    server_id: str,
    name: str,
    env: Environment = Depends(get_env_scoped("operator")),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    result = openstack_ops.server_security_group_remove(
        env, settings, server_id, name=name, dry_run=_dry_run(env, settings)
    )
    return _result(env, result)


@router.post("/environments/{environment_id}/cloud/servers/{server_id}/{action}")
def act_environment_server(
    server_id: str,
    action: str,
    env: Environment = Depends(get_env_scoped("operator")),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    if action not in SERVER_POWER_ACTIONS:
        if action == "delete":
            raise HTTPException(status_code=405, detail="DELETE the server instead")
        raise HTTPException(status_code=400, detail=f"unknown action {action!r}")
    result = openstack_ops.server_action(
        env,
        settings,
        action,
        server_id,
        dry_run=_dry_run(env, settings),
    )
    return _result(env, result)


@router.delete("/environments/{environment_id}/cloud/servers/{server_id}")
def delete_environment_server(
    server_id: str,
    env: Environment = Depends(get_env_scoped("admin")),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    result = openstack_ops.server_action(
        env,
        settings,
        "delete",
        server_id,
        dry_run=_dry_run(env, settings),
    )
    return _result(env, result)


@router.get("/environments/{environment_id}/cloud/servers/{server_id}/console")
def get_environment_server_console(
    server_id: str,
    env: Environment = Depends(get_env_scoped("operator")),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    result = openstack_ops.server_console(env, settings, server_id)
    return {"environment_id": env.id, **result}


@router.post("/environments/{environment_id}/cloud/volumes")
def create_environment_volume(
    body: VolumeCreateBody,
    env: Environment = Depends(get_env_scoped("operator")),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    result = openstack_ops.volume_create(
        env,
        settings,
        name=body.name,
        size=body.size,
        dry_run=_dry_run(env, settings),
    )
    return _result(env, result)


@router.delete("/environments/{environment_id}/cloud/volumes/{volume_id}")
def delete_environment_volume(
    volume_id: str,
    env: Environment = Depends(get_env_scoped("operator")),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    result = openstack_ops.volume_delete(
        env, settings, volume_id, dry_run=_dry_run(env, settings)
    )
    return _result(env, result)


@router.post("/environments/{environment_id}/cloud/networks")
def create_environment_network(
    body: NetworkCreateBody,
    env: Environment = Depends(get_env_scoped("operator")),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    result = openstack_ops.network_create(
        env,
        settings,
        name=body.name,
        cidr=body.cidr,
        external=body.external,
        dry_run=_dry_run(env, settings),
    )
    return _result(env, result)


@router.post("/environments/{environment_id}/cloud/security-groups/{sg_id}/rules")
def create_environment_security_group_rule(
    sg_id: str,
    body: SecurityGroupRuleBody,
    env: Environment = Depends(get_env_scoped("operator")),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    result = openstack_ops.security_group_rule_create(
        env,
        settings,
        sg_id=sg_id,
        direction=body.direction,
        ethertype=body.ethertype,
        protocol=body.protocol,
        port_range_min=body.port_range_min,
        port_range_max=body.port_range_max,
        remote_ip_prefix=body.remote_ip_prefix,
        dry_run=_dry_run(env, settings),
    )
    return _result(env, result)


@router.delete(
    "/environments/{environment_id}/cloud/security-groups/{sg_id}/rules/{rule_id}"
)
def delete_environment_security_group_rule(
    sg_id: str,
    rule_id: str,
    env: Environment = Depends(get_env_scoped("operator")),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    result = openstack_ops.security_group_rule_delete(
        env, settings, sg_id=sg_id, rule_id=rule_id, dry_run=_dry_run(env, settings)
    )
    return _result(env, result)


@router.post("/environments/{environment_id}/cloud/routers")
def create_environment_router(
    body: RouterCreateBody,
    env: Environment = Depends(get_env_scoped("operator")),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    result = openstack_ops.router_create(
        env,
        settings,
        name=body.name,
        external_network=body.external_network,
        dry_run=_dry_run(env, settings),
    )
    return _result(env, result)


@router.post("/environments/{environment_id}/cloud/routers/{router_id}/interfaces")
def add_environment_router_interface(
    router_id: str,
    body: RouterInterfaceBody,
    env: Environment = Depends(get_env_scoped("operator")),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    result = openstack_ops.router_add_interface(
        env,
        settings,
        router_id=router_id,
        subnet_id=body.subnet_id,
        dry_run=_dry_run(env, settings),
    )
    return _result(env, result)


@router.put("/environments/{environment_id}/cloud/quotas")
def update_environment_quotas(
    body: QuotaUpdateBody,
    env: Environment = Depends(get_env_scoped("operator")),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    result = openstack_ops.quotas_update(
        env,
        settings,
        compute=(
            body.compute.model_dump(exclude_unset=True)
            if body.compute is not None
            else None
        ),
        network=(
            body.network.model_dump(exclude_unset=True)
            if body.network is not None
            else None
        ),
        dry_run=_dry_run(env, settings),
    )
    return _result(env, result)


@router.post("/environments/{environment_id}/cloud/volumes/attach")
def attach_environment_volume(
    body: VolumeAttachBody,
    env: Environment = Depends(get_env_scoped("operator")),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    result = openstack_ops.volume_attach(
        env,
        settings,
        server_id=body.server_id,
        volume_id=body.volume_id,
        dry_run=_dry_run(env, settings),
    )
    return _result(env, result)


@router.post("/environments/{environment_id}/cloud/volumes/detach")
def detach_environment_volume(
    body: VolumeAttachBody,
    env: Environment = Depends(get_env_scoped("operator")),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    result = openstack_ops.volume_detach(
        env,
        settings,
        server_id=body.server_id,
        volume_id=body.volume_id,
        dry_run=_dry_run(env, settings),
    )
    return _result(env, result)


@router.post("/environments/{environment_id}/cloud/floating-ips")
def create_environment_floating_ip(
    body: FloatingIpCreateBody,
    env: Environment = Depends(get_env_scoped("operator")),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    result = openstack_ops.floating_ip_create(
        env, settings, network=body.network, dry_run=_dry_run(env, settings)
    )
    return _result(env, result)


@router.post("/environments/{environment_id}/cloud/floating-ips/associate")
def associate_environment_floating_ip(
    body: FloatingIpAssociateBody,
    env: Environment = Depends(get_env_scoped("operator")),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    result = openstack_ops.floating_ip_associate(
        env,
        settings,
        server_id=body.server_id,
        address=body.address,
        dry_run=_dry_run(env, settings),
    )
    return _result(env, result)


@router.post("/environments/{environment_id}/cloud/floating-ips/disassociate")
def disassociate_environment_floating_ip(
    body: FloatingIpDisassociateBody,
    env: Environment = Depends(get_env_scoped("operator")),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    result = openstack_ops.floating_ip_disassociate(
        env,
        settings,
        address=body.address,
        fip_id=body.id,
        dry_run=_dry_run(env, settings),
    )
    return _result(env, result)


@router.delete("/environments/{environment_id}/cloud/floating-ips/{fip_id}")
def delete_environment_floating_ip(
    fip_id: str,
    env: Environment = Depends(get_env_scoped("operator")),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    result = openstack_ops.floating_ip_delete(
        env, settings, fip_id, dry_run=_dry_run(env, settings)
    )
    return _result(env, result)


@router.post("/environments/{environment_id}/cloud/images")
def create_environment_image(
    body: ImageCreateBody,
    env: Environment = Depends(get_env_scoped("operator")),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    result = openstack_ops.image_create(
        env,
        settings,
        name=body.name,
        disk_format=body.disk_format,
        container_format=body.container_format,
        visibility=body.visibility,
        url=body.url,
        copy_from=body.copy_from,
        dry_run=_dry_run(env, settings),
    )
    return _result(env, result)


@router.delete("/environments/{environment_id}/cloud/images/{image_id}")
def delete_environment_image(
    image_id: str,
    env: Environment = Depends(get_env_scoped("operator")),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    result = openstack_ops.image_delete(
        env, settings, image_id, dry_run=_dry_run(env, settings)
    )
    return _result(env, result)


@router.patch("/environments/{environment_id}/cloud/images/{image_id}")
def patch_environment_image(
    image_id: str,
    body: ImagePatchBody,
    env: Environment = Depends(get_env_scoped("operator")),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    result = openstack_ops.image_patch(
        env,
        settings,
        image_id,
        name=body.name,
        visibility=body.visibility,
        dry_run=_dry_run(env, settings),
    )
    return _result(env, result)


@router.post("/environments/{environment_id}/cloud/flavors")
def create_environment_flavor(
    body: FlavorCreateBody,
    env: Environment = Depends(get_env_scoped("operator")),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    result = openstack_ops.flavor_create(
        env,
        settings,
        name=body.name,
        vcpus=body.vcpus,
        ram=body.ram,
        disk=body.disk,
        dry_run=_dry_run(env, settings),
    )
    return _result(env, result)


@router.delete("/environments/{environment_id}/cloud/flavors/{flavor_id}")
def delete_environment_flavor(
    flavor_id: str,
    env: Environment = Depends(get_env_scoped("operator")),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    result = openstack_ops.flavor_delete(
        env, settings, flavor_id, dry_run=_dry_run(env, settings)
    )
    return _result(env, result)


@router.post("/environments/{environment_id}/cloud/keypairs")
def create_environment_keypair(
    body: KeypairCreateBody,
    env: Environment = Depends(get_env_scoped("operator")),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    result = openstack_ops.keypair_create(
        env,
        settings,
        name=body.name,
        public_key=body.public_key,
        dry_run=_dry_run(env, settings),
    )
    return _result(env, result)


@router.delete("/environments/{environment_id}/cloud/keypairs/{name}")
def delete_environment_keypair(
    name: str,
    env: Environment = Depends(get_env_scoped("operator")),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    result = openstack_ops.keypair_delete(
        env, settings, name, dry_run=_dry_run(env, settings)
    )
    return _result(env, result)


@router.post("/environments/{environment_id}/cloud/security-groups")
def create_environment_security_group(
    body: SecurityGroupCreateBody,
    env: Environment = Depends(get_env_scoped("operator")),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    result = openstack_ops.security_group_create(
        env,
        settings,
        name=body.name,
        description=body.description,
        dry_run=_dry_run(env, settings),
    )
    return _result(env, result)


@router.delete("/environments/{environment_id}/cloud/security-groups/{sg_id}")
def delete_environment_security_group(
    sg_id: str,
    env: Environment = Depends(get_env_scoped("operator")),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    result = openstack_ops.security_group_delete(
        env, settings, sg_id, dry_run=_dry_run(env, settings)
    )
    return _result(env, result)


@router.post("/environments/{environment_id}/cloud/volumes/{volume_id}/extend")
def extend_environment_volume(
    volume_id: str,
    body: VolumeExtendBody,
    env: Environment = Depends(get_env_scoped("operator")),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    result = openstack_ops.volume_extend(
        env, settings, volume_id, size=body.size, dry_run=_dry_run(env, settings)
    )
    return _result(env, result)


@router.post("/environments/{environment_id}/cloud/volumes/{volume_id}/snapshot")
def snapshot_environment_volume(
    volume_id: str,
    body: VolumeSnapshotBody,
    env: Environment = Depends(get_env_scoped("operator")),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    result = openstack_ops.volume_snapshot_create(
        env,
        settings,
        volume_id,
        name=body.name,
        force=body.force,
        dry_run=_dry_run(env, settings),
    )
    return _result(env, result)


@router.get("/environments/{environment_id}/cloud/volume-snapshots")
def list_environment_volume_snapshots(
    env: Environment = Depends(get_env_scoped("viewer")),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    result = openstack_ops.volume_snapshots_list(env, settings)
    return {"environment_id": env.id, **result}


@router.delete("/environments/{environment_id}/cloud/volume-snapshots/{snapshot_id}")
def delete_environment_volume_snapshot(
    snapshot_id: str,
    env: Environment = Depends(get_env_scoped("operator")),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    result = openstack_ops.volume_snapshot_delete(
        env, settings, snapshot_id, dry_run=_dry_run(env, settings)
    )
    return _result(env, result)


@router.delete("/environments/{environment_id}/cloud/networks/{network_id}")
def delete_environment_network(
    network_id: str,
    env: Environment = Depends(get_env_scoped("operator")),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    result = openstack_ops.network_delete(
        env, settings, network_id, dry_run=_dry_run(env, settings)
    )
    return _result(env, result)


@router.post("/environments/{environment_id}/cloud/subnets")
def create_environment_subnet(
    body: SubnetCreateBody,
    env: Environment = Depends(get_env_scoped("operator")),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    result = openstack_ops.subnet_create(
        env,
        settings,
        network=body.network,
        cidr=body.cidr,
        name=body.name,
        dry_run=_dry_run(env, settings),
    )
    return _result(env, result)


@router.delete("/environments/{environment_id}/cloud/subnets/{subnet_id}")
def delete_environment_subnet(
    subnet_id: str,
    env: Environment = Depends(get_env_scoped("operator")),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    result = openstack_ops.subnet_delete(
        env, settings, subnet_id, dry_run=_dry_run(env, settings)
    )
    return _result(env, result)


@router.delete("/environments/{environment_id}/cloud/routers/{router_id}")
def delete_environment_router(
    router_id: str,
    env: Environment = Depends(get_env_scoped("operator")),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    result = openstack_ops.router_delete(
        env, settings, router_id, dry_run=_dry_run(env, settings)
    )
    return _result(env, result)


@router.delete(
    "/environments/{environment_id}/cloud/routers/{router_id}/interfaces/{subnet_id}"
)
def remove_environment_router_interface(
    router_id: str,
    subnet_id: str,
    env: Environment = Depends(get_env_scoped("operator")),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    result = openstack_ops.router_remove_interface(
        env,
        settings,
        router_id=router_id,
        subnet_id=subnet_id,
        dry_run=_dry_run(env, settings),
    )
    return _result(env, result)


@router.post("/environments/{environment_id}/cloud/projects")
def create_environment_project(
    body: ProjectCreateBody,
    env: Environment = Depends(get_env_scoped("operator")),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    result = openstack_ops.project_create(
        env,
        settings,
        name=body.name,
        description=body.description,
        enabled=body.enabled,
        dry_run=_dry_run(env, settings),
    )
    return _result(env, result)


@router.patch("/environments/{environment_id}/cloud/projects/{project_id}")
def patch_environment_project(
    project_id: str,
    body: ProjectPatchBody,
    env: Environment = Depends(get_env_scoped("operator")),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    result = openstack_ops.project_update(
        env,
        settings,
        project_id,
        name=body.name,
        description=body.description,
        enabled=body.enabled,
        dry_run=_dry_run(env, settings),
    )
    return _result(env, result)


@router.post("/environments/{environment_id}/cloud/users")
def create_environment_user(
    body: UserCreateBody,
    env: Environment = Depends(get_env_scoped("operator")),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    result = openstack_ops.user_create(
        env,
        settings,
        name=body.name,
        password=body.password,
        project=body.project,
        dry_run=_dry_run(env, settings),
    )
    return _result(env, result)


@router.patch("/environments/{environment_id}/cloud/users/{user_id}")
def patch_environment_user(
    user_id: str,
    body: UserPatchBody,
    env: Environment = Depends(get_env_scoped("operator")),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    result = openstack_ops.user_update(
        env,
        settings,
        user_id,
        enabled=body.enabled,
        password=body.password,
        name=body.name,
        dry_run=_dry_run(env, settings),
    )
    return _result(env, result)


@router.get("/environments/{environment_id}/cloud/load-balancers")
def list_environment_load_balancers(
    env: Environment = Depends(get_env_scoped("viewer")),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    result = openstack_ops.load_balancers_list(env, settings)
    return {"environment_id": env.id, **result}


@router.post("/environments/{environment_id}/cloud/load-balancers")
def create_environment_load_balancer(
    body: LoadBalancerCreateBody,
    env: Environment = Depends(get_env_scoped("operator")),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    result = openstack_ops.load_balancer_create(
        env,
        settings,
        name=body.name,
        vip_subnet_id=body.vip_subnet_id,
        dry_run=_dry_run(env, settings),
    )
    return _result(env, result)


@router.delete("/environments/{environment_id}/cloud/load-balancers/{lb_id}")
def delete_environment_load_balancer(
    lb_id: str,
    env: Environment = Depends(get_env_scoped("operator")),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    result = openstack_ops.load_balancer_delete(
        env, settings, lb_id, dry_run=_dry_run(env, settings)
    )
    return _result(env, result)


@router.get("/environments/{environment_id}/cloud/dns-zones")
def list_environment_dns_zones(
    env: Environment = Depends(get_env_scoped("viewer")),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    result = openstack_ops.dns_zones_list(env, settings)
    return {"environment_id": env.id, **result}


@router.post("/environments/{environment_id}/cloud/dns-zones")
def create_environment_dns_zone(
    body: DnsZoneCreateBody,
    env: Environment = Depends(get_env_scoped("operator")),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    result = openstack_ops.dns_zone_create(
        env,
        settings,
        name=body.name,
        email=body.email,
        zone_type=body.type,
        dry_run=_dry_run(env, settings),
    )
    return _result(env, result)


@router.delete("/environments/{environment_id}/cloud/dns-zones/{zone_id}")
def delete_environment_dns_zone(
    zone_id: str,
    env: Environment = Depends(get_env_scoped("operator")),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    result = openstack_ops.dns_zone_delete(
        env, settings, zone_id, dry_run=_dry_run(env, settings)
    )
    return _result(env, result)


@router.get("/environments/{environment_id}/cloud/secrets")
def list_environment_secrets(
    env: Environment = Depends(get_env_scoped("viewer")),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    result = openstack_ops.secrets_list(env, settings)
    return {"environment_id": env.id, **result}


@router.post("/environments/{environment_id}/cloud/secrets")
def create_environment_secret(
    body: SecretCreateBody,
    env: Environment = Depends(get_env_scoped("operator")),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    result = openstack_ops.secret_create(
        env,
        settings,
        name=body.name,
        payload=body.payload,
        dry_run=_dry_run(env, settings),
    )
    return _result(env, result)


@router.delete("/environments/{environment_id}/cloud/secrets/{secret_id}")
def delete_environment_secret(
    secret_id: str,
    env: Environment = Depends(get_env_scoped("operator")),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    result = openstack_ops.secret_delete(
        env, settings, secret_id, dry_run=_dry_run(env, settings)
    )
    return _result(env, result)
