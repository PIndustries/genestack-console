"""RAM inventory is separate from commissioning and never authorizes installation."""
from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from app.services.bootselect import normalize_mac


def validate_report(body: Any, node: Any) -> tuple[bool, str]:
    if not isinstance(body, dict) or body.get("wipe") is not False:
        return False, "inspection must report that no disk wipe was performed"
    serial = str((node.hardware_report or {}).get("serial") or "").strip()
    if serial.lower() in ("", "unknown", "none", "null") or body.get("serial") != serial:
        return False, "inspection serial does not match discovered management hardware"
    nics = body.get("nics")
    mac = normalize_mac(node.pxe_mac)
    if not mac or not isinstance(nics, list) or mac not in {
        normalize_mac(n.get("mac", "")) for n in nics if isinstance(n, dict)
    }:
        return False, "inspection NICs do not match the provisioning interface"
    if any(not isinstance(n, dict) or not normalize_mac(n.get("mac"))
           or n.get("link_up") not in (True, False, None) for n in nics):
        return False, "inspection network facts are invalid"
    disks = body.get("disks")
    if not isinstance(disks, list) or not disks or len(disks) > 256:
        return False, "inspection has no bounded disk inventory"
    names, identities = set(), set()
    for disk in disks:
        if not isinstance(disk, dict) or disk.get("wiped") is not False:
            return False, "inspection reported a disk write"
        name, sectors, identity = disk.get("name"), disk.get("size_sectors"), disk.get("wwid")
        if not isinstance(name, str) or not name or name in names:
            return False, "inspection disk names are invalid or duplicated"
        names.add(name)
        if isinstance(sectors, bool) or not isinstance(sectors, int) or not 0 < sectors < 2**63:
            return False, "inspection disk capacity is invalid"
        if not isinstance(disk.get("remote"), bool) or not isinstance(disk.get("readonly"), bool):
            return False, "inspection must identify remote and read-only disks"
        canonical = re.sub(r"^(?:naa\.|eui\.|0x)", "", identity.lower()) if isinstance(identity, str) else ""
        if not isinstance(identity, str) or len(identity) > 256 or (identity and canonical in identities):
            return False, "inspection disk identity is invalid or ambiguous"
        if identity:
            identities.add(canonical)
    return True, ""


def ingest(db, node, body):
    """Caller resolves the active unguessable token, without logging it."""
    if node.next_boot != "inspect" or node.boot_stage != "inspecting":
        return {"ok": False, "error": "inspection token is no longer active"}
    ok, error = validate_report(body, node)
    if not ok:
        return {"ok": False, "error": error, "node_id": node.id}
    stored = {k: v for k, v in body.items() if k != "token"}
    stored.update(source="ram-inspection", observed_at=datetime.now(UTC).isoformat(), hardware_modified=False)
    node.os_inventory_report = stored
    node.next_boot = "disk"
    node.boot_stage = "inspected"
    node.commission_token = None
    # Never mark read-only inventory as commissioned, installed or Talos-ready.
    db.add(node)
    db.commit()
    return {"ok": True, "node_id": node.id, "boot_stage": "inspected", "hardware_modified": False}


def begin(db, env, node, *, dry_run: bool, log, settings=None, boot_now=False):
    import secrets

    from app.services import baremetal

    report = node.hardware_report or {}
    mac = normalize_mac(node.pxe_mac)
    advertised = {normalize_mac(n.get("mac", "")) for n in report.get("nics", []) if isinstance(n, dict)}
    if not mac or mac not in advertised or str(report.get("serial") or "").lower() in ("", "unknown", "none", "null"):
        return {"ok": False, "returncode": 2, "error": "Discover hardware and select an advertised provisioning MAC before inspection"}
    if dry_run:
        return {"ok": True, "dry_run": True, "node_id": node.id, "message": "Would boot RAM inventory without disk writes or installation"}
    node.next_boot = "inspect"
    node.boot_stage = "inspecting"
    node.commission_token = secrets.token_hex(16)
    node.commission_wipe_wwids = None
    node.os_inventory_report = None
    node.wiped_at = None
    node.talos_served_at = None
    db.add(node)
    db.flush()
    try:
        prepared = baremetal._prepare_pxe(db, env, settings, log)
    except Exception:  # noqa: BLE001 -- revoke the RAM profile on every preparation failure
        prepared = {"ok": False, "error": "PXE preparation failed; inspection was disarmed", "returncode": 2}
    if isinstance(prepared, dict) and prepared.get("ok") is False:
        node.next_boot = "disk"
        node.boot_stage = "failed"
        node.commission_token = None
        db.add(node)
        db.flush()
        return prepared
    if boot_now:
        return baremetal.pxe_boot(db, node, dry_run=False, log=log, settings=settings)
    return {"ok": True, "node_id": node.id, "next_boot": "inspect", "message": "RAM inspection prepared; no reboot or disk write performed"}


def evidence(node: dict[str, Any], *, now: datetime):
    """Only current server-stamped RAM facts can reconcile OS-visible identities."""
    from datetime import timedelta
    from types import SimpleNamespace

    body = node.get("os_inventory")
    try:
        if not isinstance(body, dict) or body.get("source") != "ram-inspection" or body.get("hardware_modified") is not False:
            return None
        observed = datetime.fromisoformat(body["observed_at"])
        if observed.tzinfo is None or not timedelta(0) <= now - observed <= timedelta(hours=24):
            return None
        valid, _ = validate_report(body, SimpleNamespace(hardware_report=node.get("hardware"), pxe_mac=node.get("mac")))
        return body if valid else None
    except (TypeError, ValueError, KeyError):
        return None
