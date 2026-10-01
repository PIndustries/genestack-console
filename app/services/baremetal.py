"""Bare-metal orchestration — the MAAS-free provisioning path.

Coordinates the registered :class:`BaremetalNode` rows, the Redfish client
(BMC power/boot), the console-owned in-process PXE runtime, and the talos
maintenance-mode probe, then hands the node to the existing talos bootstrap
flow by upserting it into the env config doc's servers section with
``source: baremetal``.

Every public function returns a result dict (``{"ok": ..., "error": ...}``)
and never raises into the job runner. The BMC password is only ever
decrypted in memory for the Redfish call — never logged.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from typing import Any, Callable

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import BaremetalNode, Environment
from app.services import redfish
from app.services.crypto import decrypt_secret, encrypt_secret

LogFn = Callable[[str], None]

# Same hostname-safe character set as the env doc servers keys.
NODE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")

NODE_STATES = ("registered", "booting", "talos-ready", "failed")

TALOS_API_PORT = 50000
TALOS_PROBE_TIMEOUT = 5.0
DEFAULT_PROVISION_TIMEOUT = 900
DEFAULT_POLL_INTERVAL = 5.0


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def node_payload(node: BaremetalNode) -> dict[str, Any]:
    """API-safe serialization — never includes the BMC password."""
    return {
        "id": node.id,
        "environment_id": node.environment_id,
        "name": node.name,
        "bmc_host": node.bmc_host,
        "bmc_username": node.bmc_username,
        "pxe_mac": node.pxe_mac,
        "expected_ip": node.expected_ip,
        "state": node.state,
        "last_seen": node.last_seen.isoformat() if node.last_seen else None,
        "created_at": node.created_at.isoformat() if node.created_at else None,
        "updated_at": node.updated_at.isoformat() if node.updated_at else None,
    }


def get_node(db: Session, env: Environment, node_id: Any) -> BaremetalNode | None:
    """Fetch a node by id, scoped to the environment (None when not found)."""
    node = db.get(BaremetalNode, str(node_id or "").strip())
    if node is None or node.environment_id != env.id:
        return None
    return node


def list_nodes(db: Session, env: Environment) -> list[dict[str, Any]]:
    stmt = (
        select(BaremetalNode)
        .where(BaremetalNode.environment_id == env.id)
        .order_by(BaremetalNode.name.asc())
    )
    return [node_payload(node) for node in db.scalars(stmt).all()]


def register_node(
    db: Session,
    env: Environment,
    *,
    name: str,
    bmc_host: str,
    bmc_username: str,
    bmc_password: str,
    pxe_mac: str | None = None,
    dry_run: bool,
    log: LogFn,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Validate and upsert a bare-metal node (keyed by env + name).

    The BMC password is fernet-encrypted at rest. When ``pxe_mac`` is absent
    the node's ethernet MACs are probed over Redfish to autofill it (the
    first interface MAC wins); the probe is skipped on dry-run and a probe
    failure only logs a warning — registration still succeeds without a MAC.
    """
    op_id = "baremetal.node.register"
    name = name.strip()
    bmc_host = bmc_host.strip()
    bmc_username = bmc_username.strip()
    if not NODE_NAME_RE.match(name):
        return {
            "ok": False,
            "error": (
                f"{op_id}: invalid name '{name}' "
                f"(must match {NODE_NAME_RE.pattern})"
            ),
            "returncode": 2,
        }
    for field, value in (
        ("bmc_host", bmc_host),
        ("bmc_username", bmc_username),
        ("bmc_password", bmc_password),
    ):
        if not value:
            return {
                "ok": False,
                "error": f"{op_id}: {field} is required",
                "returncode": 2,
            }

    if dry_run:
        log(
            f"[dry-run] would register bare-metal node name={name} bmc={bmc_host} "
            f"user={bmc_username} pxe_mac={pxe_mac or '(probe via redfish)'}"
        )
        return {
            "ok": True,
            "dry_run": True,
            "name": name,
            "bmc_host": bmc_host,
            "message": f"[dry-run] would register bare-metal node {name}",
        }

    node = db.scalar(
        select(BaremetalNode).where(
            BaremetalNode.environment_id == env.id,
            BaremetalNode.name == name,
        )
    )
    created = node is None
    if created:
        node = BaremetalNode(environment_id=env.id, name=name, state="registered")
    node.bmc_host = bmc_host
    node.bmc_username = bmc_username
    # Idempotent: already-encrypted values pass through unchanged
    node.bmc_password = encrypt_secret(bmc_password, settings)
    if pxe_mac:
        node.pxe_mac = pxe_mac
    elif not node.pxe_mac:
        try:
            macs = redfish.system_macs(bmc_host, bmc_username, bmc_password)
        except redfish.RedfishError as exc:
            log(
                f"[baremetal] mac autofill probe failed for {name}: {exc} — continuing without"
            )
        else:
            if macs:
                node.pxe_mac = macs[0]
                log(
                    f"[baremetal] autofilled pxe_mac={node.pxe_mac} from redfish ({len(macs)} found)"
                )
            else:
                log(f"[baremetal] redfish reported no ethernet MACs for {name}")
    db.add(node)
    db.flush()
    log(
        f"[baremetal] {'registered' if created else 'updated'} node {name} "
        f"({node.id}) bmc={bmc_host} pxe_mac={node.pxe_mac or '-'}"
    )
    return {
        "ok": True,
        "dry_run": False,
        "created": created,
        "node": node_payload(node),
        "message": f"{'registered' if created else 'updated'} bare-metal node {name}",
    }


def power_action(
    db: Session,
    node: BaremetalNode,
    action: str,
    *,
    dry_run: bool,
    log: LogFn,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Redfish power action (on|off|restart) against the node's BMC."""
    action = str(action or "").strip().lower()
    if dry_run:
        log(
            f"[dry-run] would redfish power {action or '?'} node={node.name} bmc={node.bmc_host}"
        )
        return {
            "ok": True,
            "dry_run": True,
            "action": action,
            "node_id": node.id,
            "message": f"[dry-run] would power {action} {node.name}",
        }
    password = decrypt_secret(node.bmc_password, settings)
    try:
        reset_type = redfish.power(
            node.bmc_host, node.bmc_username, password or "", action
        )
    except redfish.RedfishError as exc:
        log(f"[baremetal] power {action} node={node.name} failed: {exc}")
        return {"ok": False, "error": str(exc), "node_id": node.id, "returncode": 2}
    log(
        f"[baremetal] power {action} ({reset_type}) node={node.name} bmc={node.bmc_host}"
    )
    return {
        "ok": True,
        "dry_run": False,
        "action": action,
        "reset_type": reset_type,
        "node_id": node.id,
        "message": f"power {action} sent to {node.name}",
    }


def pxe_boot(
    db: Session,
    node: BaremetalNode,
    *,
    dry_run: bool,
    log: LogFn,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """One-shot PXE boot + force restart; node state moves to ``booting``.

    The console-owned in-process PXE runtime (DHCP + HTTP) then assigns the
    reserved IP and chainloads the Talos boot assets.
    """
    if dry_run:
        log(
            f"[dry-run] would set one-shot PXE boot and force-restart node={node.name} "
            f"bmc={node.bmc_host}; state -> booting"
        )
        return {
            "ok": True,
            "dry_run": True,
            "node_id": node.id,
            "state": "booting",
            "message": f"[dry-run] would PXE-boot {node.name}",
        }
    password = decrypt_secret(node.bmc_password, settings)
    try:
        redfish.set_pxe_boot(node.bmc_host, node.bmc_username, password or "")
        redfish.power(node.bmc_host, node.bmc_username, password or "", "restart")
    except redfish.RedfishError as exc:
        log(f"[baremetal] pxe_boot node={node.name} failed: {exc}")
        return {"ok": False, "error": str(exc), "node_id": node.id, "returncode": 2}
    node.state = "booting"
    db.add(node)
    db.flush()
    log(f"[baremetal] pxe_boot node={node.name}: one-shot PXE set, ForceRestart sent")
    return {
        "ok": True,
        "dry_run": False,
        "node_id": node.id,
        "state": node.state,
        "message": f"PXE boot initiated for {node.name}",
    }


def talos_api_ready(ip: str, log: LogFn | None = None) -> bool:
    """Probe the talos maintenance API at ``https://<ip>:50000`` (insecure).

    Ready means the talos API server is accepting TLS connections. A gRPC
    endpoint does not speak HTTP/1.1, so any transport success that ends in a
    protocol-level error still counts as ready; only connect-level failures
    (nothing listening / unreachable) count as not ready.
    """
    try:
        with httpx.Client(verify=False, timeout=TALOS_PROBE_TIMEOUT) as client:
            client.get(f"https://{ip}:{TALOS_API_PORT}/")
            return True
    except (httpx.ConnectError, httpx.ConnectTimeout):
        return False
    except httpx.HTTPError:
        # TCP + TLS established; the gRPC API just doesn't speak HTTP/1.1
        return True


def k8s_ready_for_ip(ip: str, kubeconfig: str | None) -> bool | None:
    """Check if a k8s node with this internal IP exists and is Ready.

    Returns:
        True if node exists and status is Ready
        False if node exists but is not Ready
        None if no node found with this IP or kubeconfig is empty/None
    """
    if not kubeconfig:
        return None

    try:
        from app.services.k8sclient import K8sClient

        with K8sClient(kubeconfig) as client:
            nodes = client.list_nodes()
            for node in nodes:
                if node.get("internal_ip") == ip:
                    return node.get("status") == "Ready"
        return None
    except Exception:
        return None


def _prepare_pxe(
    db: Session, env: Environment, settings: Settings | None, log: LogFn
) -> None:
    """Render PXE files and reload the in-process DHCP/HTTP runtime.

    When the PXE module is absent this is a logged no-op so the rest of the
    flow still works.
    """
    try:
        from app.services import pxe as pxe_service
    except ImportError:
        log("[pxe] pxe module not present, skipping pxe prep")
        return
    from app.services import envconfig as envconfig_service

    current = envconfig_service.get_current(db, env)
    doc = current[0] if current else {}
    pxe_service.ensure_assets_and_config(env, doc, settings, log)


def provision(
    db: Session,
    env: Environment,
    node: BaremetalNode,
    *,
    roles: list[str] | None = None,
    actor: str | None = None,
    dry_run: bool,
    log: LogFn,
    settings: Settings | None = None,
    timeout_seconds: int = DEFAULT_PROVISION_TIMEOUT,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
) -> dict[str, Any]:
    """Full zero-touch provisioning of one node.

    pxe_boot → wait for the talos maintenance API at the node's expected_ip
    (bounded by ``timeout_seconds``) → state ``talos-ready`` → upsert the
    node into the env config doc servers section (source "baremetal") so the
    talos bootstrap flow sees it. On timeout the node state moves to
    ``failed``. dry_run logs the whole plan and touches nothing.
    """
    op_id = "baremetal.node.provision"
    roles = list(roles or [])
    if dry_run:
        log(
            f"[dry-run] would provision node={node.name}: pxe boot (one-shot + "
            f"ForceRestart), wait for talos maintenance API at "
            f"{node.expected_ip or '?'}:{TALOS_API_PORT} (timeout {timeout_seconds}s), "
            f"then upsert servers.{node.name} (source=baremetal, roles={roles or []})"
        )
        return {
            "ok": True,
            "dry_run": True,
            "node_id": node.id,
            "message": f"[dry-run] would provision {node.name}",
        }

    if not node.expected_ip:
        return {
            "ok": False,
            "error": (
                f"{op_id}: node {node.name} has no expected_ip — assign one from "
                "the PXE pool first"
            ),
            "node_id": node.id,
            "returncode": 2,
        }

    try:
        _prepare_pxe(db, env, settings, log)
    except Exception as exc:  # noqa: BLE001 — never raise into the job runner
        log(f"[pxe] prep failed: {exc}")
        return {
            "ok": False,
            "error": f"pxe preparation failed: {exc}",
            "node_id": node.id,
            "returncode": 2,
        }

    boot = pxe_boot(db, node, dry_run=False, log=log, settings=settings)
    if not boot.get("ok"):
        node.state = "failed"
        db.add(node)
        db.flush()
        return {**boot, "error": f"pxe boot failed: {boot.get('error')}"}

    ip = str(node.expected_ip)
    log(
        f"[talos] waiting for maintenance API at {ip}:{TALOS_API_PORT} "
        f"(timeout {timeout_seconds}s)"
    )
    deadline = time.monotonic() + timeout_seconds
    attempt = 0
    ready = False
    while True:
        attempt += 1
        if talos_api_ready(ip, log):
            ready = True
            break
        if time.monotonic() >= deadline:
            break
        if attempt % 12 == 0:
            remaining = int(deadline - time.monotonic())
            log(f"[talos] still waiting for {ip}:{TALOS_API_PORT} ({remaining}s left)")
        time.sleep(poll_interval)

    if not ready:
        node.state = "failed"
        db.add(node)
        db.flush()
        log(f"[talos] timed out waiting for {ip}:{TALOS_API_PORT} — node marked failed")
        return {
            "ok": False,
            "error": (
                f"timed out waiting for talos maintenance API at "
                f"{ip}:{TALOS_API_PORT} after {timeout_seconds}s"
            ),
            "node_id": node.id,
            "returncode": 2,
        }

    node.state = "talos-ready"
    node.last_seen = _utcnow()
    db.add(node)
    db.flush()
    log(f"[talos] maintenance API ready at {ip}:{TALOS_API_PORT} — node is talos-ready")

    # Hand the node to the talos bootstrap flow: env doc servers upsert.
    from app.services import envconfig as envconfig_service

    row, warnings = envconfig_service.assign_server(
        db,
        env,
        actor,
        hostname=node.name,
        roles=roles,
        ip=ip,
        source="baremetal",
    )
    for warning in warnings:
        log(f"[provision] config warning: {warning}")
    log(
        f"[provision] upserted servers.{node.name} (source=baremetal, ip={ip}) "
        f"in config version {row.version}"
    )
    return {
        "ok": True,
        "dry_run": False,
        "node_id": node.id,
        "state": node.state,
        "ip": ip,
        "roles": roles,
        "config_version": row.version,
        "message": f"provisioned {node.name}: talos-ready at {ip}",
    }


def _cold_cycle(bmc_host: str, username: str, password: str):
    """Cold-cycle (power off, then on) the node via Redfish BMC."""
    from app.services import redfish

    redfish.force_restart(bmc_host, username, password)


def pxe_traffic_seen(node, now: float) -> bool:
    """Check if PXE DHCP traffic was recently observed for this node's MAC.

    Returns True if DHCP traffic exists within the recency window.
    """
    if not node.pxe_mac:
        return False
    try:
        from app.services import pxe_runtime

        mgr = pxe_runtime.get_manager()
        status = mgr.status()
        for iface_data in status.get("runtimes", {}).values():
            for lease in iface_data.get("leases", []):
                if lease.get("mac") == node.pxe_mac:
                    last_seen = lease.get("last_seen", 0)
                    if now - last_seen < 600:
                        return True
        return False
    except (ImportError, AttributeError):
        return False


def iso_image_url(env: Environment, doc: dict, settings: Settings | None = None) -> str:
    """Return the Talos ISO image URL for this environment.

    Uses pxe.image_url from config doc if present, otherwise falls back to default.
    """
    pxe_config = doc.get("pxe") if isinstance(doc, dict) else None
    if isinstance(pxe_config, dict) and pxe_config.get("image_url"):
        return str(pxe_config["image_url"])

    settings = settings or Settings()
    return getattr(
        settings,
        "talos_iso_url",
        "https://factory.talos.dev/image/376567988ad370138ad8b2698212367b8edcb69b5fd68c80be1f2ec7d603b4ba/v1.6.0/metal-amd64.iso",
    )


def iso_boot(
    db: Session,
    env: Environment,
    node: BaremetalNode,
    *,
    dry_run: bool,
    log: LogFn,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Boot node via Redfish virtual CD (Talos ISO).

    Returns {"ok": bool, "image_url": str, ...}.
    """
    settings = settings or Settings()

    from app.services import envconfig as envconfig_service

    current = envconfig_service.get_current(db, env)
    doc = current[0] if current else {}

    image_url = iso_image_url(env, doc, settings)

    if dry_run:
        log(f"[dry-run] would mount {image_url} on {node.name} and reboot")
        return {"ok": True, "dry_run": True, "image_url": image_url}

    password = (
        decrypt_secret(node.bmc_password_encrypted)
        if node.bmc_password_encrypted
        else None
    )

    try:
        from app.services import events

        redfish.insert_virtual_media(
            node.bmc_host, node.bmc_username, password or "", image_url
        )
        _cold_cycle(node.bmc_host, node.bmc_username, password or "")
        log(f"[baremetal] iso_boot node={node.name}: mounted {image_url}, reboot sent")
        events.publish_sync(
            "baremetal.metal",
            {
                "type": "metal",
                "kind": "iso",
                "host": node.name,
                "image_url": image_url,
                "message": f"inserting virtual CD {image_url} for {node.name}",
            },
        )
        return {"ok": True, "image_url": image_url}
    except Exception as exc:
        log(f"[baremetal] iso_boot node={node.name} failed: {exc}")
        return {"ok": False, "error": str(exc), "image_url": image_url}


def host_boot_state(
    ip: str,
    kubeconfig: str | None,
    *,
    was_ready: set[str] | None = None,
    saw_down: bool = True,
) -> str:
    """Determine boot/readiness state for a baremetal host.

    Returns: "old-os", "maintenance", "down"
    - old-os: talos API up + k8s Ready (or was_ready without saw_down)
    - maintenance: talos API up but not k8s Ready
    - down: talos API not responding
    """
    if not talos_api_ready(ip):
        return "down"

    k8s_status = k8s_ready_for_ip(ip, kubeconfig)
    if k8s_status is True:
        return "old-os"

    if was_ready and ip in was_ready and not saw_down:
        return "old-os"

    return "maintenance"


def boot_for_talos(
    db: Session | None,
    env: Environment,
    node: BaremetalNode,
    *,
    boot: str = "auto",
    dry_run: bool,
    log: LogFn,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Orchestrate PXE or ISO boot for Talos provisioning.

    boot="auto": Try PXE first, fall back to ISO if no DHCP traffic after delay.
    boot="pxe": PXE only.
    boot="iso": ISO only.

    Returns {"ok": bool, ...}.
    """
    import time

    settings = settings or Settings()

    if boot == "iso":
        return iso_boot(db, env, node, dry_run=dry_run, log=log, settings=settings)

    # PXE first
    pxe_result = pxe_boot(db, node, dry_run=dry_run, log=log, settings=settings)
    if not pxe_result.get("ok"):
        return pxe_result

    if boot == "pxe":
        return pxe_result

    # boot="auto": check for PXE traffic, fall back to ISO if silent
    now = time.time()
    if not pxe_traffic_seen(node, now):
        log(f"[baremetal] PXE not coming through for {node.name}, falling back to ISO")
        return iso_boot(db, env, node, dry_run=dry_run, log=log, settings=settings)

    return pxe_result
