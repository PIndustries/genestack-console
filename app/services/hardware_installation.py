"""Review measured hardware before saving an installation configuration.

This module never boots, formats, installs, or submits a job. Disk targets are
reconciled against fresh read-only OS inventory, never a guessed device path.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from datetime import UTC, datetime
from typing import Any

from app.services import capacity
from app.services.hardware_inspection import evidence

_WWID = re.compile(r"^(?:(?:naa|eui)\.|0x)?[0-9a-fA-F]{16,64}$")


def canonical(identity: str) -> str:
    return re.sub(r"^(?:naa\.|eui\.|0x)", "", identity.lower())


def preview(
    nodes: list[dict[str, Any]],
    document: dict[str, Any],
    version: int,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    proposed = capacity.propose(nodes, now=now)
    blockers = list(proposed["blockers"])
    by_id = {n["id"]: n for n in nodes}
    machines: list[dict[str, Any]] = []
    used: set[str] = set()
    if len(proposed["allocations"]) != len(nodes):
        blockers.append(
            "Every registered machine must meet the minimum capacity before installation"
        )
    pxe = document.get("pxe") or {}
    try:
        subnet = ipaddress.IPv4Network(
            str(pxe["next_server"]) + "/" + str(pxe["netmask"]), strict=False
        )
        gateway = ipaddress.IPv4Address(pxe["gateway"])
        if gateway not in subnet or gateway in (subnet.network_address, subnet.broadcast_address):
            raise ValueError("invalid gateway")
    except (KeyError, TypeError, ValueError, ipaddress.AddressValueError):
        subnet = None
        blockers.append("Save a valid provisioning network and gateway before installation")
    for allocation in proposed["allocations"]:
        node = by_id[allocation["id"]]
        facts = evidence(node, now=now)
        if not facts:
            blockers.append(f"{node['name']}: inspect operating-system disk identities first")
            continue
        try:
            address = ipaddress.IPv4Address(allocation["ip"])
            if (
                subnet is None
                or address not in subnet
                or address
                in (
                    subnet.network_address,
                    subnet.broadcast_address,
                    gateway,
                    ipaddress.IPv4Address(pxe["next_server"]),
                )
            ):
                raise ValueError("unusable address")
        except (ValueError, TypeError):
            blockers.append(f"{node['name']}: a usable provisioning address is required")
        identifiers = [
            allocation.get("proposed_boot_disk_identifier")
            or allocation.get("proposed_boot_disk_serial")
        ]
        identifiers.extend(
            d.get("unique_identifier") or d.get("serial")
            for d in allocation["candidate_data_disks"]
        )
        disks = []
        local = [d for d in facts["disks"] if not d["remote"] and not d["readonly"]]
        for identity in identifiers:
            matches = [
                d
                for d in local
                if isinstance(identity, str)
                and identity
                and (
                    canonical(str(d.get("wwid") or "")) == canonical(identity)
                    if allocation["disk_identity_kind"] == "logical_volume"
                    else d.get("serial") == identity
                )
            ]
            if len(matches) != 1 or not _WWID.fullmatch(str(matches[0].get("wwid") or "")):
                blockers.append(
                    f"{node['name']}: each proposed disk needs one exact local writable OS WWID"
                )
                continue
            disk = matches[0]
            key = canonical(disk["wwid"])
            if key in used:
                blockers.append("Disk identities must be unique across all target machines")
            used.add(key)
            disks.append(
                {
                    "wwid": disk["wwid"],
                    "capacity_bytes": disk["size_sectors"] * 512,
                    "purpose": "boot" if not disks else "storage",
                }
            )
        if len(disks) != len(identifiers):
            continue
        if {canonical(d.get("wwid") or "") for d in local} != {canonical(d["wwid"]) for d in disks}:
            blockers.append(
                f"{node['name']}: additional local disks need review before automatic installation"
            )
        machines.append(
            {
                "id": node["id"],
                "name": node["name"],
                "ip": allocation["ip"],
                "mac": allocation["mac"],
                "roles": allocation["proposed_roles"],
                "cpu_threads": allocation["cpu_threads"],
                "memory_gib": allocation["memory_gib"],
                "disks": disks,
                "hardware_observed_at": node["hardware"]["observed_at"],
                "os_observed_at": facts["observed_at"],
            }
        )
    result = {
        "schema_version": 1,
        "config_version": version,
        "machines": machines,
        "network": {
            k: pxe.get(k)
            for k in ("next_server", "netmask", "gateway", "dns", "range_start", "range_end")
        },
        "blockers": sorted(set(blockers)),
        "warnings": proposed["warnings"],
        "hardware_modified": False,
        "configuration_changed": False,
    }
    result["fingerprint"] = hashlib.sha256(
        json.dumps(result, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return result


def for_environment(db, env) -> dict[str, Any]:
    from sqlalchemy import select

    from app.models import BaremetalNode
    from app.services import envconfig

    rows = db.scalars(
        select(BaremetalNode)
        .where(BaremetalNode.environment_id == env.id)
        .order_by(BaremetalNode.id)
    ).all()
    current = envconfig.get_current(db, env)
    result = preview(
        [
            {
                "id": n.id,
                "name": n.name,
                "ip": n.expected_ip,
                "mac": n.pxe_mac,
                "hardware": n.hardware_report,
                "os_inventory": n.os_inventory_report,
            }
            for n in rows
        ],
        current[0] if current else {},
        current[1].version if current else 0,
    )
    result["supports_plan_save"] = True
    result["saved_plan_matches"] = _saved_matches(current, result)
    return result


def _saved_matches(current, result) -> bool:
    if current is None:
        return False
    record = current[0].get("hardware_installation") or {}
    if (
        not isinstance(record, dict)
        or record.get("schema_version") != 1
        or record.get("execution_authorized") is not False
    ):
        return False
    original = record.get("reviewed_plan") or {}
    if not isinstance(original, dict):
        return False
    if record.get("saved_version") != current[1].version or not isinstance(
        original.get("config_version"), int
    ):
        return False
    candidate = {
        k: v
        for k, v in result.items()
        if k not in ("supports_plan_save", "saved_plan_matches", "fingerprint")
    }
    candidate["config_version"] = original["config_version"]
    digest = hashlib.sha256(
        json.dumps(candidate, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return not result["blockers"] and digest == record.get("reviewed_fingerprint")


def save_review(db, env, actor: str, fingerprint: str, expected_version: int):
    """Persist the reviewed proposal only; installation needs a separate action."""
    import yaml

    from app.services import envconfig

    current = envconfig.get_current(db, env)
    result = for_environment(db, env)
    version = current[1].version if current else 0
    record = (current[0].get("hardware_installation") or {}) if current else {}
    if not isinstance(record, dict):
        record = {}
    if (
        version == expected_version + 1
        and record.get("reviewed_fingerprint") == fingerprint
        and result["saved_plan_matches"]
    ):
        return current[1]
    if version != expected_version or result["fingerprint"] != fingerprint:
        raise envconfig.ConfigConflictError(
            "The configuration or hardware changed; review the plan again"
        )
    if result["blockers"]:
        raise envconfig.ConfigValidationError(
            "Resolve the installation plan blockers before saving"
        )
    doc = dict(current[0]) if current else {}
    doc["hardware_installation"] = {
        "schema_version": 1,
        "saved_version": expected_version + 1,
        "reviewed_fingerprint": fingerprint,
        "reviewed_plan": {
            k: v for k, v in result.items() if k not in ("supports_plan_save", "saved_plan_matches")
        },
        "reviewed_by": actor,
        "reviewed_at": datetime.now(UTC).isoformat(),
        "execution_authorized": False,
    }
    row, _ = envconfig.put_version(
        db, env, yaml.safe_dump(doc, sort_keys=False), actor, expected_version=expected_version
    )
    return row
