"""Discovery inbox endpoints — agent-reported PXE sightings and BMC finds.

Agents report ``pxe_request`` / ``bmc_found`` events over the agent channel;
these endpoints expose the per-environment inbox and the two operator
actions on it (claim a node into inventory, attach credentials to a BMC).
Subnet sweeps are triggered via the ``baremetal.bmc_scan`` catalog op.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.auth import resolve_principal
from app.deps import get_db, get_env_scoped
from app.models import Environment
from app.schemas import DiscoveryBmcCredsRequest, DiscoveryClaimRequest, Principal
from app.services import discovery as discovery_service
from app.services.job_runner import JobRunner

router = APIRouter(prefix="/api/v1/environments/{environment_id}", tags=["discovery"])


@router.get("/discovery")
def get_discovery(
    db: Session = Depends(get_db),
    env: Environment = Depends(get_env_scoped("viewer")),
) -> dict[str, Any]:
    """The environment's discovery inbox: hosts and BMCs, newest-first."""
    return discovery_service.inbox(db, env)


@router.post("/discovery/claim")
def claim_discovered_node(
    body: DiscoveryClaimRequest,
    db: Session = Depends(get_db),
    env: Environment = Depends(get_env_scoped("operator")),
    principal: Principal = Depends(resolve_principal),
) -> dict[str, Any]:
    """Claim a sighted node: name it and upsert the env config doc servers."""
    try:
        result = discovery_service.claim_node(
            db,
            env,
            mac=body.mac,
            name=body.name,
            roles=body.roles,
            actor=principal.username,
        )
    except discovery_service.DiscoveryError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    JobRunner(db).write_audit(
        actor=principal.username,
        action="env.discovery.claim",
        resource_type="environment",
        resource_id=env.id,
        environment_id=env.id,
        details={
            "mac": result["node"]["mac"],
            "name": result["name"],
            "roles": result["roles"],
        },
        success=True,
    )
    db.commit()
    return result


@router.post("/discovery/bmc-creds")
def register_discovered_bmc(
    body: DiscoveryBmcCredsRequest,
    db: Session = Depends(get_db),
    env: Environment = Depends(get_env_scoped("operator")),
    principal: Principal = Depends(resolve_principal),
) -> dict[str, Any]:
    """Attach credentials to a discovered BMC (creates/updates a BaremetalNode)."""
    try:
        result = discovery_service.register_bmc(
            db,
            env,
            bmc_id=body.bmc_id,
            name=body.name,
            username=body.username,
            password=body.password,
        )
    except discovery_service.DiscoveryError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    JobRunner(db).write_audit(
        actor=principal.username,
        action="env.discovery.bmc_creds",
        resource_type="environment",
        resource_id=env.id,
        environment_id=env.id,
        details={
            "bmc_id": body.bmc_id,
            "ip": result["bmc"]["ip"],
            "name": body.name,
            "username": body.username,
        },
        success=True,
    )
    db.commit()
    return result
