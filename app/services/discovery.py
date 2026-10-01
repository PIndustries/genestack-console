"""Discovery inbox — agent-reported PXE/DHCP sightings and Redfish BMC finds.

The hub side of environment discovery: agents stream ``pxe_request`` and
``bmc_found`` events over the agent channel (ingested in
app.services.agents.handle_agent_event); this module reads the inbox and
performs the two operator actions on it — claiming a sighted node into the
env config doc servers section (source "baremetal", same hand-off as
baremetal.node.provision) and attaching credentials to a found BMC, which
creates/updates the linked :class:`BaremetalNode` registry row.

Every public function returns a plain dict; expected failures raise
:class:`DiscoveryError` with an HTTP status the router translates.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import BaremetalNode, DiscoveredBmc, DiscoveredNode, Environment
from app.services import baremetal as baremetal_service
from app.services import envconfig as envconfig_service
from app.services.crypto import encrypt_secret


class DiscoveryError(RuntimeError):
    """Expected discovery failure carrying an HTTP status for the router."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def node_payload(node: DiscoveredNode) -> dict[str, Any]:
    return {
        "id": node.id,
        "environment_id": node.environment_id,
        "mac": node.mac,
        "ip": node.ip,
        "hostname": node.hostname,
        "state": node.state,
        "first_seen": node.first_seen.isoformat() if node.first_seen else None,
        "last_seen": node.last_seen.isoformat() if node.last_seen else None,
    }


def bmc_payload(bmc: DiscoveredBmc) -> dict[str, Any]:
    return {
        "id": bmc.id,
        "environment_id": bmc.environment_id,
        "ip": bmc.ip,
        "vendor": bmc.vendor,
        "model": bmc.model,
        "state": bmc.state,
        "first_seen": bmc.first_seen.isoformat() if bmc.first_seen else None,
        "last_seen": bmc.last_seen.isoformat() if bmc.last_seen else None,
    }


def inbox(db: Session, env: Environment) -> dict[str, Any]:
    """The env's discovery inbox: hosts and BMCs, newest sighting first."""
    hosts = db.scalars(
        select(DiscoveredNode)
        .where(DiscoveredNode.environment_id == env.id)
        .order_by(DiscoveredNode.last_seen.desc())
    ).all()
    bmcs = db.scalars(
        select(DiscoveredBmc)
        .where(DiscoveredBmc.environment_id == env.id)
        .order_by(DiscoveredBmc.last_seen.desc())
    ).all()
    return {
        "hosts": [node_payload(n) for n in hosts],
        "bmcs": [bmc_payload(b) for b in bmcs],
    }


def claim_node(
    db: Session,
    env: Environment,
    *,
    mac: str,
    name: str,
    roles: list[str] | None = None,
    actor: str | None = None,
) -> dict[str, Any]:
    """Claim a discovered node: name it and hand it to the inventory flow.

    Marks the sighting ``claimed`` and upserts the env config doc servers
    section (source "baremetal", new config version) so the talos bootstrap
    flow sees it. Does not commit — the caller commits after auditing.
    """
    mac = (mac or "").strip().lower()
    name = (name or "").strip()
    node = db.scalar(
        select(DiscoveredNode).where(
            DiscoveredNode.environment_id == env.id,
            DiscoveredNode.mac == mac,
        )
    )
    if node is None:
        raise DiscoveryError(
            f"no discovered node with mac {mac!r} for this environment", 404
        )
    if not baremetal_service.NODE_NAME_RE.match(name):
        raise DiscoveryError(
            f"invalid name '{name}' (must match {baremetal_service.NODE_NAME_RE.pattern})"
        )
    roles = [str(r).strip().lower() for r in (roles or []) if str(r).strip()]
    invalid = [r for r in roles if r not in envconfig_service.VALID_SERVER_ROLES]
    if invalid:
        valid = ", ".join(sorted(envconfig_service.VALID_SERVER_ROLES))
        raise DiscoveryError(f"unknown role(s) {', '.join(invalid)} (valid: {valid})")

    node.state = "claimed"
    db.add(node)
    row, warnings = envconfig_service.assign_server(
        db,
        env,
        actor,
        hostname=name,
        roles=roles,
        ip=node.ip,
        source="baremetal",
    )
    db.flush()
    return {
        "ok": True,
        "node": node_payload(node),
        "name": name,
        "roles": roles,
        "config_version": row.version,
        "warnings": warnings,
        "message": f"claimed {mac} as {name} (config version {row.version})",
    }


def register_bmc(
    db: Session,
    env: Environment,
    *,
    bmc_id: str,
    name: str,
    username: str,
    password: str,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Attach credentials to a discovered BMC: upsert the BaremetalNode row.

    The new/updated registry node gets ``bmc_host`` = the discovered IP and
    the password fernet-encrypted at rest; the sighting moves to
    ``registered``. Does not commit — the caller commits after auditing.
    """
    bmc = db.get(DiscoveredBmc, str(bmc_id or "").strip())
    if bmc is None or bmc.environment_id != env.id:
        raise DiscoveryError(
            f"unknown discovered BMC id {bmc_id!r} for this environment", 404
        )
    name = (name or "").strip()
    username = (username or "").strip()
    if not baremetal_service.NODE_NAME_RE.match(name):
        raise DiscoveryError(
            f"invalid name '{name}' (must match {baremetal_service.NODE_NAME_RE.pattern})"
        )
    if not username:
        raise DiscoveryError("username is required")
    if not password:
        raise DiscoveryError("password is required")

    node = db.scalar(
        select(BaremetalNode).where(
            BaremetalNode.environment_id == env.id,
            BaremetalNode.name == name,
        )
    )
    created = node is None
    if created:
        node = BaremetalNode(environment_id=env.id, name=name, state="registered")
    node.bmc_host = bmc.ip
    node.bmc_username = username
    node.bmc_password = encrypt_secret(password, settings)
    db.add(node)

    bmc.state = "registered"
    db.add(bmc)
    db.flush()
    return {
        "ok": True,
        "created": created,
        "bmc": bmc_payload(bmc),
        "node": baremetal_service.node_payload(node),
        "message": (
            f"{'registered' if created else 'updated'} bare-metal node {name} "
            f"from discovered BMC {bmc.ip}"
        ),
    }
