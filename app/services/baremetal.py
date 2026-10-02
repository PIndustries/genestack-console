"""Bare-metal orchestration. The console installs Talos from the network.

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

import json
import re
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import BaremetalNode, Environment
from app.services import redfish
from app.services.bootselect import (
    classify_boot,
    commission_summary,
    normalize_mac,
    profile_from_script,
    served_after_wipe,
    validate_report,
)
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
        "next_boot": node.next_boot or "disk",
        "boot_stage": node.boot_stage or "new",
        "wiped_at": node.wiped_at.isoformat() if node.wiped_at else None,
        "talos_served_at": (
            node.talos_served_at.isoformat() if node.talos_served_at else None
        ),
        "commission": commission_summary(node.commission_report),
        "boot_log": list(node.boot_log or [])[-5:],
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
    """One-shot PXE of this MAC's current next-boot image, then ForceRestart.

    The image is whatever ``next_boot`` already selected: commission, Talos,
    or an iPXE exit to the local disk. This does not change that choice.
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
        readback = ""
        try:
            readback = redfish.boot_override(
                node.bmc_host, node.bmc_username, password or ""
            )
        except redfish.RedfishError as exc:
            log(f"[baremetal] pxe override readback failed for {node.name}: {exc}")
        redfish.power(node.bmc_host, node.bmc_username, password or "", "restart")
    except redfish.RedfishError as exc:
        log(f"[baremetal] pxe_boot node={node.name} failed: {exc}")
        return {"ok": False, "error": str(exc), "node_id": node.id, "returncode": 2}
    node.state = "booting"
    db.add(node)
    db.flush()
    log(
        f"[baremetal] pxe_boot node={node.name} next={node.next_boot or 'disk'}: "
        f"one-shot PXE set (readback {readback or 'unknown'}), ForceRestart sent"
    )
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
) -> dict[str, Any]:
    """Render PXE files and reload the in-process DHCP/HTTP runtime.

    When the PXE module is absent this is a logged no-op so the rest of the
    flow still works.
    """
    try:
        from app.services import pxe as pxe_service
    except ImportError:
        log("[pxe] pxe module not present, skipping pxe prep")
        return {"ok": True, "skipped": True}
    from app.services import envconfig as envconfig_service

    settings = settings or Settings()
    current = envconfig_service.get_current(db, env)
    doc = current[0] if current else {}
    result = pxe_service.ensure_assets_and_config(
        env, doc, settings, log, profiles=boot_profiles(db, env)
    )
    if not result.get("ok"):
        log(f"[pxe] prep failed: {result.get('error')}")
    return result


def boot_profiles(db: Session, env: Environment) -> list[dict[str, Any]]:
    """Per-MAC PXE profiles for every registered node in the environment."""
    stmt = select(BaremetalNode).where(BaremetalNode.environment_id == env.id)
    profiles: list[dict[str, Any]] = []
    for node in db.scalars(stmt).all():
        profiles.append(
            {
                "name": node.name,
                "pxe_mac": node.pxe_mac,
                "expected_ip": node.expected_ip,
                "next_boot": node.next_boot or "disk",
                "token": node.commission_token or "",
            }
        )
    return profiles


def _append_boot_log(node: BaremetalNode, entry: dict[str, Any]) -> None:
    log_rows = list(node.boot_log or [])
    log_rows.append(entry)
    node.boot_log = log_rows[-20:]


def begin_commission(
    db: Session,
    env: Environment,
    node: BaremetalNode,
    *,
    log: LogFn,
    settings: Settings | None = None,
    boot_now: bool = False,
) -> dict[str, Any]:
    """Point this MAC at the wipe RAM disk and forget the previous report."""
    if not normalize_mac(node.pxe_mac):
        return {
            "ok": False,
            "error": f"{node.name} has no PXE MAC — commission cannot select a boot file",
            "node_id": node.id,
            "returncode": 2,
        }
    node.next_boot = "commission"
    node.boot_stage = "commissioning"
    node.commission_token = secrets.token_hex(16)
    node.commission_report = None
    node.wiped_at = None
    node.talos_served_at = None
    node.state = "booting"
    db.add(node)
    db.flush()
    prepared = _prepare_pxe(db, env, settings, log)
    if isinstance(prepared, dict) and prepared.get("ok") is False:
        return prepared
    log(f"[baremetal] {node.name} next boot is the commission RAM disk")
    if boot_now:
        return pxe_boot(db, node, dry_run=False, log=log, settings=settings)
    return {
        "ok": True,
        "node_id": node.id,
        "next_boot": "commission",
        "boot_stage": node.boot_stage,
        "message": f"{node.name} will commission on the next PXE boot",
    }


def set_next_boot(
    db: Session,
    env: Environment,
    node: BaremetalNode,
    target: str,
    *,
    boot_now: bool,
    dry_run: bool,
    log: LogFn,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Choose the next image for one MAC. ``boot_now`` power-cycles into it."""
    choice = str(target or "").strip().lower()
    if choice not in ("commission", "talos", "disk"):
        return {
            "ok": False,
            "error": f"next_boot must be commission, talos, or disk (got {target!r})",
            "returncode": 2,
        }
    if dry_run:
        log(
            f"[dry-run] would set next_boot={choice} on {node.name} boot_now={boot_now}"
        )
        return {
            "ok": True,
            "dry_run": True,
            "next_boot": choice,
            "message": f"[dry-run] would set {node.name} next boot to {choice}",
        }
    if choice == "commission":
        return begin_commission(
            db, env, node, log=log, settings=settings, boot_now=boot_now
        )
    if choice == "talos" and node.wiped_at is None:
        return {
            "ok": False,
            "error": (
                f"{node.name} has no accepted commission wipe — "
                "Talos would boot the old disks"
            ),
            "node_id": node.id,
            "returncode": 2,
        }
    node.next_boot = choice
    if choice == "talos":
        node.boot_stage = "talos"
    db.add(node)
    db.flush()
    prepared = _prepare_pxe(db, env, settings, log)
    if isinstance(prepared, dict) and prepared.get("ok") is False:
        return prepared
    log(f"[baremetal] {node.name} next boot is {choice}")
    if boot_now and choice == "talos":
        booted = pxe_boot(db, node, dry_run=False, log=log, settings=settings)
        if not booted.get("ok"):
            return booted
        # Stamp only after the one-shot PXE was accepted. A failed power
        # cycle must not look like Talos was served.
        node.talos_served_at = _utcnow()
        db.add(node)
        db.flush()
        return booted
    if boot_now and choice == "disk":
        return power_action(
            db, node, "restart", dry_run=False, log=log, settings=settings
        )
    return {
        "ok": True,
        "node_id": node.id,
        "next_boot": choice,
        "message": f"{node.name} next boot is {choice}",
    }


def _find_node_by_mac(db: Session, mac: str) -> BaremetalNode | None:
    wanted = normalize_mac(mac)
    if not wanted:
        return None
    stmt = select(BaremetalNode).where(BaremetalNode.pxe_mac.is_not(None))
    for node in db.scalars(stmt).all():
        if normalize_mac(node.pxe_mac) == wanted:
            return node
    return None


def record_boot_fetch(root: str, url_path: str) -> None:
    """Remember which profile a MAC fetched. Called from the PXE HTTP server."""
    from app.db import SessionLocal

    filename = url_path.rstrip("/").rsplit("/", 1)[-1]
    mac = filename[: -len(".ipxe")] if filename.endswith(".ipxe") else ""
    profile = "unknown"
    script_path = Path(root) / "mac" / filename
    if script_path.is_file():
        profile = profile_from_script(script_path.read_text(encoding="utf-8"))
    with SessionLocal() as db:
        node = _find_node_by_mac(db, mac.replace("-", ":"))
        if node is None:
            return
        _append_boot_log(
            node,
            {
                "at": _utcnow().isoformat(),
                "mac": normalize_mac(mac.replace("-", ":")),
                "profile": profile,
                "path": url_path,
            },
        )
        if profile == "talos" and node.wiped_at is not None and not served_after_wipe(
            node.talos_served_at, node.wiped_at
        ):
            node.talos_served_at = _utcnow()
            if node.boot_stage in ("commissioned", "new", "commissioning"):
                node.boot_stage = "talos"
        db.add(node)
        db.commit()


def ingest_commission_bytes(token: str, raw: bytes) -> dict[str, Any]:
    """Accept a commission POST from the RAM disk."""
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"ok": False, "error": "commission report is not JSON"}
    return ingest_commission(str(token or ""), body)


def ingest_commission(token: str, body: Any) -> dict[str, Any]:
    """Store a wipe report and switch that MAC's next profile to Talos.

    Does not power-cycle. The provision or greenfield job, or an operator
    Serve Talos action, sends the second one-shot PXE.
    """
    from app.db import SessionLocal

    token = str(token or "").strip()
    if not token:
        return {"ok": False, "error": "missing commission token"}
    with SessionLocal() as db:
        node = db.scalar(
            select(BaremetalNode).where(BaremetalNode.commission_token == token)
        )
        if node is None:
            return {"ok": False, "error": "unknown commission token"}
        if node.wiped_at is not None and node.boot_stage in (
            "commissioned",
            "talos",
            "fresh-maintenance",
            "installed",
        ):
            return {"ok": True, "duplicate": True, "node_id": node.id}
        ok, err = validate_report(body, node.pxe_mac)
        _append_boot_log(
            node,
            {
                "at": _utcnow().isoformat(),
                "profile": "commission-report",
                "ok": ok,
                "error": err,
            },
        )
        stored = _report_without_token(body)
        if not ok:
            node.commission_report = stored
            db.add(node)
            db.commit()
            return {"ok": False, "error": err, "node_id": node.id}
        node.commission_report = stored
        node.wiped_at = _utcnow()
        node.boot_stage = "commissioned"
        node.next_boot = "talos"
        node.state = "booting"
        db.add(node)
        db.commit()
        env = db.get(Environment, node.environment_id)
        if env is not None:
            from app.config import get_settings

            _prepare_pxe(db, env, get_settings(), lambda _msg: None)
        return {
            "ok": True,
            "node_id": node.id,
            "boot_stage": "commissioned",
            "disks": commission_summary(body).get("disk_count"),
        }


def _report_without_token(body: Any) -> dict[str, Any] | None:
    """Drop the commission token before the report is stored."""
    if not isinstance(body, dict):
        return None
    return {key: value for key, value in body.items() if key != "token"}


def probe_addresses(node: BaremetalNode, fallback: str | None = None) -> list[str]:
    """IPs to probe: the reservation, then a DHCP lease for this MAC."""
    ips: list[str] = []
    expected = str(node.expected_ip or "").strip()
    if expected:
        ips.append(expected)
    lease = _lease_ip(node.pxe_mac)
    if lease and lease not in ips:
        ips.append(lease)
    extra = str(fallback or "").strip()
    if extra and extra not in ips:
        ips.append(extra)
    return ips


def _lease_ip(mac: str | None) -> str:
    wanted = normalize_mac(mac)
    if not wanted:
        return ""
    try:
        from app.services.pxe_runtime import get_manager

        status = get_manager().status()
    except Exception:
        return ""
    for iface in (status.get("runtimes") or {}).values():
        if not isinstance(iface, dict):
            continue
        for lease in iface.get("leases") or []:
            if not isinstance(lease, dict):
                continue
            if normalize_mac(str(lease.get("mac") or "")) == wanted:
                return str(lease.get("ip") or "")
    return ""


def _serve_talos(
    db: Session,
    env: Environment,
    node: BaremetalNode,
    *,
    log: LogFn,
    settings: Settings | None,
) -> dict[str, Any]:
    """Second boot: the MAC already wiped, so PXE may serve Talos."""
    if node.wiped_at is None:
        return {
            "ok": False,
            "error": f"{node.name} is not commissioned",
            "node_id": node.id,
            "returncode": 2,
        }
    node.next_boot = "talos"
    node.boot_stage = "talos"
    db.add(node)
    db.flush()
    prepared = _prepare_pxe(db, env, settings, log)
    if isinstance(prepared, dict) and prepared.get("ok") is False:
        return prepared
    booted = pxe_boot(db, node, dry_run=False, log=log, settings=settings)
    if not booted.get("ok"):
        return booted
    node.talos_served_at = _utcnow()
    db.add(node)
    db.flush()
    log(f"[baremetal] {node.name} commission accepted — serving Talos")
    return booted


def _wait_until(
    db: Session,
    node: BaremetalNode,
    predicate,
    *,
    log: LogFn,
    timeout_seconds: int,
    poll_interval: float,
    waiting: str,
) -> bool:
    deadline = time.monotonic() + timeout_seconds
    attempt = 0
    while True:
        attempt += 1
        db.refresh(node)
        if predicate(node):
            return True
        if time.monotonic() >= deadline:
            return False
        if attempt % 12 == 0:
            remaining = int(deadline - time.monotonic())
            log(f"[baremetal] {node.name} still {waiting} ({remaining}s left)")
        time.sleep(poll_interval)


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
    stop_after: str = "",
) -> dict[str, Any]:
    """Commission (wipe + report), then PXE Talos, then accept fresh maintenance.

    A Talos API answer on port 50000 is not success until this attempt has a
    wipe report and the Talos profile was served after that wipe. ``stop_after``
    ``commission`` returns once the report is stored and does not serve Talos.
    """
    op_id = "baremetal.node.provision"
    roles = list(roles or [])
    stop = str(stop_after or "").strip().lower()
    if stop not in ("", "commission", "talos"):
        return {
            "ok": False,
            "error": f"{op_id}: stop_after must be commission or talos",
            "returncode": 2,
        }
    if dry_run:
        log(
            f"[dry-run] would provision node={node.name}: commission RAM disk "
            f"(wipe fixed disks, report), then Talos, then wait for fresh "
            f"maintenance at {node.expected_ip or '?'}:{TALOS_API_PORT} "
            f"stop_after={stop or 'talos'}"
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

    started = begin_commission(
        db, env, node, log=log, settings=settings, boot_now=False
    )
    if not started.get("ok"):
        return started
    boot = pxe_boot(db, node, dry_run=False, log=log, settings=settings)
    if not boot.get("ok"):
        node.state = "failed"
        db.add(node)
        db.flush()
        return {**boot, "error": f"pxe boot failed: {boot.get('error')}"}

    commissioned = _wait_until(
        db,
        node,
        lambda current: current.boot_stage
        in ("commissioned", "talos", "fresh-maintenance", "installed"),
        log=log,
        timeout_seconds=timeout_seconds,
        poll_interval=poll_interval,
        waiting="waiting for the commission report",
    )
    if not commissioned:
        node.state = "failed"
        node.boot_stage = "failed"
        db.add(node)
        db.flush()
        return {
            "ok": False,
            "error": (
                f"timed out waiting for a commission wipe report from {node.name}"
            ),
            "node_id": node.id,
            "returncode": 2,
        }
    if stop == "commission":
        log(f"[baremetal] {node.name} commissioned — stopped before Talos")
        return {
            "ok": True,
            "dry_run": False,
            "node_id": node.id,
            "boot_stage": node.boot_stage,
            "stopped_after": "commission",
            "commission": commission_summary(node.commission_report),
            "message": f"commissioned {node.name}; Talos was not served",
        }

    if not served_after_wipe(node.talos_served_at, node.wiped_at):
        served = _serve_talos(db, env, node, log=log, settings=settings)
        if not served.get("ok"):
            node.state = "failed"
            db.add(node)
            db.flush()
            return served

    fresh = False
    matched_ip = str(node.expected_ip)
    deadline = time.monotonic() + timeout_seconds
    attempt = 0
    while time.monotonic() < deadline:
        attempt += 1
        db.refresh(node)
        for ip in probe_addresses(node, matched_ip):
            probe = host_boot_state(
                ip,
                getattr(env, "kubeconfig_data", None),
                require_fresh=True,
                wiped_at=node.wiped_at,
                talos_served_at=node.talos_served_at,
            )
            if probe == "fresh-maintenance":
                fresh = True
                matched_ip = ip
                break
            if probe == "old-os":
                log(
                    f"[baremetal] {node.name} at {ip} is still the old OS "
                    "(k8s Ready)"
                )
            elif probe == "old-maintenance":
                log(
                    f"[baremetal] {node.name} at {ip} answered Talos but the "
                    "wipe for this attempt is not the boot it is running"
                )
        if fresh:
            break
        if attempt % 12 == 0:
            remaining = int(deadline - time.monotonic())
            log(
                f"[baremetal] {node.name} still waiting for fresh Talos "
                f"({remaining}s left)"
            )
        time.sleep(poll_interval)

    if not fresh:
        node.state = "failed"
        node.boot_stage = "failed"
        db.add(node)
        db.flush()
        return {
            "ok": False,
            "error": (
                f"timed out waiting for fresh Talos maintenance on {node.name} "
                "after the commission wipe"
            ),
            "node_id": node.id,
            "returncode": 2,
        }

    ip = matched_ip
    node.state = "talos-ready"
    node.boot_stage = "fresh-maintenance"
    node.last_seen = _utcnow()
    if ip and ip != node.expected_ip:
        log(f"[baremetal] {node.name} answered on lease {ip}")
        node.expected_ip = ip
    db.add(node)
    db.flush()
    log(
        f"[talos] fresh maintenance at {ip}:{TALOS_API_PORT} — "
        f"{node.name} is talos-ready"
    )
    if stop == "talos":
        log(
            f"[baremetal] {node.name} in fresh maintenance — "
            "stopped before inventory"
        )
        return {
            "ok": True,
            "dry_run": False,
            "node_id": node.id,
            "state": node.state,
            "boot_stage": node.boot_stage,
            "ip": ip,
            "stopped_after": "talos",
            "commission": commission_summary(node.commission_report),
            "message": (
                f"{node.name} is in fresh maintenance; inventory was not updated"
            ),
        }

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
    require_fresh: bool = False,
    wiped_at: datetime | None = None,
    talos_served_at: datetime | None = None,
) -> str:
    """Determine boot/readiness state for a baremetal host.

    Returns ``down``, ``old-os``, ``maintenance``, and when ``require_fresh``
    is set, ``fresh-maintenance`` or ``old-maintenance``. Fresh means the
    Talos profile was served after this attempt's wipe. An API on port 50000
    without that ordering is still the old machine.
    """
    api_up = talos_api_ready(ip)
    k8s_status = k8s_ready_for_ip(ip, kubeconfig) if api_up else None
    return classify_boot(
        api_up,
        k8s_status,
        wiped_at=wiped_at,
        talos_served_at=talos_served_at,
        require_fresh=require_fresh,
        was_ready=bool(was_ready and ip in was_ready),
        saw_down=saw_down,
    )


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
