"""Rack layout read and placement writes for one environment.

Viewer reads build the layout from the current config document. Operator
writes merge gear, planned links, and placements into ``doc["rack"]`` only.
The servers map and every other key are copied through unchanged.
"""

from __future__ import annotations

import re
import uuid
from typing import Any, Literal

import yaml
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.deps import get_db, get_env_scoped, require_operator
from app.models import Environment
from app.schemas import Principal
from app.services import envconfig as envconfig_service
from app.services import rack_layout
from app.services.job_runner import JobRunner

router = APIRouter(prefix="/api/v1/environments/{environment_id}", tags=["rack"])

_HOSTNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
GearKind = Literal["switch", "pdu", "layer"]


class PlacementPut(BaseModel):
    hostname: str = Field(min_length=1, max_length=253)
    rack: str | None = Field(default=None, max_length=64)
    u_start: int | None = Field(default=None, ge=1)
    u_height: int | None = Field(default=None, ge=1)


class GearPost(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    kind: GearKind
    u_height: int = Field(default=1, ge=1)
    u_start: int | None = Field(default=None, ge=1)
    rack: str | None = Field(default=None, max_length=64)


class LinkPost(BaseModel):
    source: str = Field(min_length=1, max_length=256)
    target: str = Field(min_length=1, max_length=256)
    label: str = Field(default="", max_length=256)


def _hostname(value: str) -> str:
    name = value.strip()
    if not _HOSTNAME_RE.match(name):
        raise HTTPException(status_code=422, detail=f"Invalid hostname '{value}'")
    return name


def _parsed_doc(db: Session, env: Environment) -> dict[str, Any]:
    """Normalized config. Used to read servers, not to write them back."""
    current = envconfig_service.get_current(db, env)
    if current is None:
        return {}
    return current[0]


def _raw_doc(db: Session, env: Environment) -> dict[str, Any]:
    """Stored document, not the normalized parse.

    ``get_current`` fills defaults such as ``source`` onto servers. Writing
    that copy would rewrite the servers map. Load the stored YAML instead and
    replace only ``rack``.
    """
    current = envconfig_service.get_current(db, env)
    if current is None:
        return {}
    _parsed, row = current
    loaded = yaml.safe_load(row.yaml_text) if row.yaml_text else {}
    return loaded if isinstance(loaded, dict) else {}


def _save_doc(
    db: Session,
    env: Environment,
    principal: Principal,
    doc: dict[str, Any],
    *,
    action: str,
    details: dict[str, Any],
) -> int:
    """Store ``doc`` as the next config version. Caller already set ``rack``."""
    try:
        row, _warnings = envconfig_service.put_version(
            db,
            env,
            yaml.safe_dump(doc, sort_keys=False, default_flow_style=False),
            principal.username,
        )
    except envconfig_service.ConfigValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (envconfig_service.ConfigConflictError, IntegrityError) as exc:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="Configuration changed; reload before saving",
        ) from exc
    JobRunner(db).write_audit(
        actor=principal.username,
        action=action,
        resource_type="environment",
        resource_id=env.id,
        environment_id=env.id,
        details=details,
    )
    db.commit()
    return row.version


def _ready_ips(env: Environment) -> set[str]:
    """Ready node internal IPs. Empty when the cluster cannot be listed."""
    try:
        from app.services.demo import canned_k8s_nodes, is_demo_env

        if is_demo_env(env):
            payload = canned_k8s_nodes()
        else:
            from app.services import k8s_ops

            payload = k8s_ops.list_nodes(env)
    except Exception:  # noqa: BLE001 — a failed node list must not fail the rack
        return set()
    if not isinstance(payload, dict):
        return set()
    ips: set[str] = set()
    for node in payload.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        if str(node.get("status") or "").lower() != "ready":
            continue
        ip = str(node.get("internal_ip") or "").strip()
        if ip:
            ips.add(ip)
    return ips


def _bmc_hosts(db: Session, env: Environment) -> set[str]:
    """Hostnames of stored bare-metal nodes that have a management address."""
    try:
        from app.services.baremetal import list_nodes

        rows = list_nodes(db, env)
    except Exception:  # noqa: BLE001 — rack read must not power or probe BMCs
        return set()
    hosts: set[str] = set()
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        if not str(row.get("bmc_host") or "").strip():
            continue
        name = str(row.get("name") or "").strip()
        if name:
            hosts.add(name)
    return hosts


def _layout_for(db: Session, env: Environment, doc: dict[str, Any]) -> dict[str, Any]:
    servers = doc.get("servers")
    rack = doc.get("rack")
    observed = {
        "ready_ips": _ready_ips(env),
        "bmc_hosts": _bmc_hosts(db, env),
        # Nova's server list probes the cloud. There is no stored VM table.
        "vms": [],
    }
    return rack_layout.layout(
        servers if isinstance(servers, dict) else {},
        rack if isinstance(rack, dict) else {},
        observed,
    )


@router.get("/rack")
def get_rack(
    db: Session = Depends(get_db),
    env: Environment = Depends(get_env_scoped("viewer")),
) -> dict[str, Any]:
    """Layout for this environment. Servers without a hostname are skipped."""
    return _layout_for(db, env, _parsed_doc(db, env))


@router.put("/rack/placement")
def put_placement(
    body: PlacementPut,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_operator),
    env: Environment = Depends(get_env_scoped("operator")),
) -> dict[str, Any]:
    """Save one machine's U position. ``u_start`` null returns it to automatic."""
    hostname = _hostname(body.hostname)
    doc = _raw_doc(db, env)
    rack = rack_layout.with_placement(
        doc.get("rack") if isinstance(doc.get("rack"), dict) else {},
        hostname,
        rack_name=body.rack,
        u_start=body.u_start,
        u_height=body.u_height,
    )
    doc["rack"] = rack
    version = _save_doc(
        db,
        env,
        principal,
        doc,
        action="env.rack.placement",
        details={"hostname": hostname, "u_start": body.u_start},
    )
    stored = rack["placements"].get(hostname)
    return {"version": version, "hostname": hostname, "placement": stored}


@router.post("/rack/gear", status_code=201)
def post_gear(
    body: GearPost,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_operator),
    env: Environment = Depends(get_env_scoped("operator")),
) -> dict[str, Any]:
    """Append a switch, PDU, or layer. Gear is not a machine."""
    doc = _raw_doc(db, env)
    rack, gear = rack_layout.add_gear(
        doc.get("rack") if isinstance(doc.get("rack"), dict) else {},
        gear_id=str(uuid.uuid4()),
        name=body.name,
        kind=body.kind,
        rack_name=body.rack,
        u_start=body.u_start,
        u_height=body.u_height,
    )
    doc["rack"] = rack
    version = _save_doc(
        db,
        env,
        principal,
        doc,
        action="env.rack.gear.add",
        details={"gear_id": gear["id"], "kind": gear["kind"]},
    )
    return {"version": version, "gear": gear}


@router.delete("/rack/gear/{gear_id}")
def delete_gear(
    gear_id: str,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_operator),
    env: Environment = Depends(get_env_scoped("operator")),
) -> dict[str, Any]:
    doc = _raw_doc(db, env)
    rack = rack_layout.remove_gear(
        doc.get("rack") if isinstance(doc.get("rack"), dict) else {},
        gear_id,
    )
    if rack is None:
        raise HTTPException(status_code=404, detail=f"Gear '{gear_id}' not found")
    doc["rack"] = rack
    version = _save_doc(
        db,
        env,
        principal,
        doc,
        action="env.rack.gear.remove",
        details={"gear_id": gear_id},
    )
    return {"version": version, "deleted": gear_id}


@router.post("/rack/links", status_code=201)
def post_link(
    body: LinkPost,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_operator),
    env: Environment = Depends(get_env_scoped("operator")),
) -> dict[str, Any]:
    """Append a planned connection. Seen cables are not stored here."""
    doc = _raw_doc(db, env)
    rack, link = rack_layout.add_link(
        doc.get("rack") if isinstance(doc.get("rack"), dict) else {},
        link_id=str(uuid.uuid4()),
        source=body.source,
        target=body.target,
        label=body.label,
    )
    doc["rack"] = rack
    version = _save_doc(
        db,
        env,
        principal,
        doc,
        action="env.rack.link.add",
        details={"link_id": link["id"]},
    )
    return {"version": version, "link": link}


@router.delete("/rack/links/{link_id}")
def delete_link(
    link_id: str,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_operator),
    env: Environment = Depends(get_env_scoped("operator")),
) -> dict[str, Any]:
    doc = _raw_doc(db, env)
    rack = rack_layout.remove_link(
        doc.get("rack") if isinstance(doc.get("rack"), dict) else {},
        link_id,
    )
    if rack is None:
        raise HTTPException(status_code=404, detail=f"Link '{link_id}' not found")
    doc["rack"] = rack
    version = _save_doc(
        db,
        env,
        principal,
        doc,
        action="env.rack.link.remove",
        details={"link_id": link_id},
    )
    return {"version": version, "deleted": link_id}
