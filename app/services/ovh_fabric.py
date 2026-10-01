"""OVH vRack fabric — attach dedicated/Rise NICs, VLAN, interconnect check.

Rise 1 boxes have a public NIC (internet) and a private NIC that only
carries cluster traffic once it is plugged into a vRack. VLAN 0 is
untagged. VLAN 1-4000 is 802.1q on the private NIC (Talos
machine.network.interfaces[].vlans). Greenfield starts at VLAN 100 so
the cluster fabric is not the untagged vRack.

Live OVH membership is slow (many sequential API calls). The console
keeps the last snapshot on ``Environment.metadata_json['ovh_fabric']`` so
the Inventory panel can render immediately and refresh in the background.

All mutating attach work goes through ``ovh.vrack.attach`` so it is audited.
"""

from __future__ import annotations

import ipaddress
from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.models import Environment, OvhAccount
from app.services import envconfig as envconfig_service
from app.services.crypto import decrypt_secret
from app.services.deploy import _ovh_resolve_service_names
from app.services.ovh import OvhClient, OvhError, env_is_ovh, pick_private_nic

LogFn = Callable[[str], None]

CACHE_KEY = "ovh_fabric"

# Greenfield dedicated/Rise fabric. VLAN 100 is tagged 802.1q on the private
# NIC (0 = untagged). Private IPs start at .11 so .1 stays free for a gateway.
DEFAULT_VLAN_ID = 100
DEFAULT_PRIVATE_CIDR = "10.10.0.0/24"


def ovh_section(doc: dict[str, Any] | None) -> dict[str, Any]:
    section = (doc or {}).get("ovh") if isinstance(doc, dict) else None
    return dict(section) if isinstance(section, dict) else {}


def vlan_id_from_doc(doc: dict[str, Any] | None) -> int:
    section = ovh_section(doc)
    if "vlan_id" not in section or section.get("vlan_id") is None:
        return DEFAULT_VLAN_ID
    try:
        vlan = int(section["vlan_id"])
    except (TypeError, ValueError):
        return DEFAULT_VLAN_ID
    return vlan if 0 <= vlan <= 4000 else DEFAULT_VLAN_ID


def private_cidr_from_doc(doc: dict[str, Any] | None) -> str:
    return str(ovh_section(doc).get("private_cidr") or "").strip()


def vrack_from_doc(doc: dict[str, Any] | None) -> str:
    return str(ovh_section(doc).get("vrack") or "").strip()


def _fold(value: Any) -> str:
    return "".join(ch.lower() for ch in str(value or "") if ch.isalnum())


def private_ips_from_cidr(cidr: str, hostnames: list[str]) -> dict[str, str]:
    """Sequential private IPs for a greenfield fabric (``.11``, ``.12``, …)."""
    try:
        net = ipaddress.ip_network(str(cidr or "").strip(), strict=False)
    except (ValueError, TypeError):
        return {}
    hosts = list(net.hosts())
    ordered = sorted(str(h) for h in hostnames if str(h).strip())
    if not hosts or not ordered:
        return {}
    # Prefer .11+ on a /24 so .1 can be a gateway; fall back to .2+.
    start = 10 if len(hosts) > 10 + len(ordered) else 1
    out: dict[str, str] = {}
    for i, name in enumerate(ordered):
        idx = start + i
        if idx >= len(hosts):
            break
        out[name] = str(hosts[idx])
    return out


def suggest_vrack(
    vracks: list[dict[str, Any]],
    hints: list[str],
) -> dict[str, Any] | None:
    """Pick the vRack whose name/description matches the cluster / account."""
    folded_hints = [h for h in (_fold(x) for x in hints) if len(h) >= 3]
    best: dict[str, Any] | None = None
    best_score = 0
    for raw in vracks:
        if not isinstance(raw, dict) or not raw.get("id"):
            continue
        blob = (
            _fold(raw.get("name"))
            + _fold(raw.get("description"))
            + _fold(raw.get("id"))
        )
        score = 0
        matched = ""
        for hint in folded_hints:
            if hint in blob:
                if len(hint) > len(matched):
                    matched = hint
                score += len(hint)
        if score > best_score:
            best_score = score
            best = {
                "id": raw["id"],
                "name": raw.get("name") or raw["id"],
                "description": raw.get("description") or "",
                "reason": f"matches '{matched}'" if matched else "best name match",
            }
    if best_score <= 0:
        return None
    return best


def fabric_hints(
    db: Session, env: Environment, doc: dict[str, Any] | None
) -> list[str]:
    talos = (
        (doc or {}).get("talos") if isinstance((doc or {}).get("talos"), dict) else {}
    )
    hints = [
        env.name,
        talos.get("cluster_name") if isinstance(talos, dict) else None,
    ]
    if env.ovh_account_id:
        account = db.get(OvhAccount, env.ovh_account_id)
        if account is not None:
            hints.append(account.name)
    return [str(h) for h in hints if h]


def address_with_prefix(ip: str, cidr: str) -> str:
    """``10.10.0.11`` + ``10.10.0.0/24`` → ``10.10.0.11/24``."""
    addr = str(ip or "").split("/", 1)[0].strip()
    if not addr:
        return ""
    if "/" in str(cidr or ""):
        prefix = str(cidr).split("/", 1)[1].strip()
        if prefix.isdigit():
            return f"{addr}/{prefix}"
    return addr


def _utcnow() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def read_fabric_cache(env: Environment) -> dict[str, Any]:
    meta = env.metadata_json if isinstance(env.metadata_json, dict) else {}
    raw = meta.get(CACHE_KEY)
    return dict(raw) if isinstance(raw, dict) else {}


def write_fabric_cache(db: Session, env: Environment, payload: dict[str, Any]) -> None:
    meta = dict(env.metadata_json) if isinstance(env.metadata_json, dict) else {}
    stored = {
        "checked_at": payload.get("checked_at"),
        "vrack": payload.get("vrack"),
        "vlan_id": payload.get("vlan_id"),
        "private_cidr": payload.get("private_cidr"),
        "vracks": payload.get("vracks") or [],
        "servers": payload.get("servers") or [],
        "missing": payload.get("missing") or [],
        "attached": payload.get("attached") or [],
        "ip_blocks": payload.get("ip_blocks") or [],
        "discovered_vlans": payload.get("discovered_vlans") or [],
        "interconnect": payload.get("interconnect") or "unknown",
        "error": payload.get("error"),
        "suggested_vrack": payload.get("suggested_vrack"),
        "proposed_ips": payload.get("proposed_ips") or {},
    }
    meta[CACHE_KEY] = stored
    env.metadata_json = meta
    flag_modified(env, "metadata_json")
    db.add(env)


def _client_for_env(
    db: Session, env: Environment
) -> tuple[OvhClient | None, str | None]:
    if not env.ovh_account_id:
        return None, "environment is not bound to an OVH account"
    account = db.get(OvhAccount, env.ovh_account_id)
    if account is None:
        return None, "bound OVH account not found"
    consumer_key = decrypt_secret(account.consumer_key_encrypted) or ""
    if not consumer_key:
        return None, "OVH account has no approved consumer key — re-run Connect"
    client = OvhClient(
        endpoint=account.endpoint,
        app_key=account.app_key,
        app_secret=decrypt_secret(account.app_secret_encrypted) or "",
        consumer_key=consumer_key,
    )
    return client, None


def _role_for_nic(
    nic: dict[str, Any], private_mac: str | None, private_vni: str | None
) -> str:
    mac = str(nic.get("mac") or "").strip()
    vni = str(nic.get("vni") or "").strip()
    link = str(nic.get("link_type") or "").lower()
    if private_mac and mac.lower() == private_mac.lower():
        return "private"
    if private_vni and vni and vni == private_vni:
        return "private"
    if link in ("private", "vrack", "isolated"):
        return "private"
    if link in ("public",):
        return "public"
    return "unknown"


def _nics_view(
    nics: list[dict[str, Any]] | None,
    *,
    private_mac: str | None = None,
    vrack_vni: str | None = None,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for nic in nics or []:
        if not isinstance(nic, dict):
            continue
        mac = str(nic.get("mac") or "").strip()
        if not mac:
            continue
        row = {
            "mac": mac,
            "link_type": str(nic.get("link_type") or nic.get("linkType") or ""),
            "vni": str(nic.get("vni") or nic.get("virtualNetworkInterface") or "")
            or None,
        }
        row["role"] = _role_for_nic(row, private_mac, vrack_vni)
        out.append(row)
    return out


def _public_mac(nics: list[dict[str, Any]], private_mac: str | None) -> str | None:
    for nic in nics:
        if nic.get("role") == "public" and nic.get("mac"):
            return str(nic["mac"])
    for nic in nics:
        mac = str(nic.get("mac") or "")
        if mac and (not private_mac or mac.lower() != private_mac.lower()):
            return mac
    return None


def _doc_servers(doc: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw = doc.get("servers") if isinstance(doc, dict) else None
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for hostname, entry in raw.items():
        if isinstance(entry, dict):
            out[str(hostname)] = entry
    return out


def _is_ovh_host(entry: dict[str, Any]) -> bool:
    source = str(entry.get("source") or "")
    return source == "ovh" or bool(entry.get("service_name"))


def _interconnect(
    targets: list[dict[str, Any]],
    missing: list[str],
    attached_ok: list[str],
    configured: str,
    *,
    checked: bool,
) -> str:
    if not targets:
        return "empty"
    if not checked:
        return "unknown"
    if missing and not attached_ok:
        return "unattached"
    if missing:
        return "partial"
    if configured:
        return "ok"
    return "unconfigured"


def _status_payload(
    *,
    ok: bool,
    configured: str,
    vlan_id: int,
    private_cidr: str,
    vracks: list[dict[str, Any]],
    server_rows: list[dict[str, Any]],
    missing: list[str],
    attached_ok: list[str],
    ip_blocks: list[dict[str, Any]],
    discovered_vlans: list[int],
    interconnect: str,
    source: str,
    checked_at: str | None,
    error: str | None = None,
    suggested_vrack: dict[str, Any] | None = None,
    proposed_ips: dict[str, str] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "ok": ok,
        "vrack": configured or None,
        "vlan_id": vlan_id,
        "private_cidr": private_cidr or None,
        "vracks": vracks,
        "servers": server_rows,
        "missing": missing,
        "attached": attached_ok,
        "ip_blocks": ip_blocks,
        "discovered_vlans": discovered_vlans,
        "interconnect": interconnect,
        "source": source,
        "checked_at": checked_at,
        "stale": source != "live",
        "defaults": {
            "vlan_id": DEFAULT_VLAN_ID,
            "private_cidr": DEFAULT_PRIVATE_CIDR,
        },
        "suggested_vrack": suggested_vrack,
        "proposed_ips": proposed_ips or {},
    }
    if error:
        body["error"] = error
    return body


def _rows_from_doc(
    doc: dict[str, Any],
    cache: dict[str, Any],
) -> list[dict[str, Any]]:
    cached_by_host = {
        str(s.get("hostname")): s
        for s in (cache.get("servers") or [])
        if isinstance(s, dict) and s.get("hostname")
    }
    rows: list[dict[str, Any]] = []
    for hostname, entry in _doc_servers(doc).items():
        if not _is_ovh_host(entry):
            continue
        cached = cached_by_host.get(hostname) or {}
        private_mac = entry.get("private_mac") or cached.get("private_mac")
        vrack_vni = entry.get("vrack_vni") or cached.get("vrack_vni")
        nics = _nics_view(
            (
                entry.get("nics")
                if isinstance(entry.get("nics"), list)
                else cached.get("nics")
            ),
            private_mac=private_mac,
            vrack_vni=vrack_vni,
        )
        rows.append(
            {
                "hostname": hostname,
                "service_name": entry.get("service_name") or cached.get("service_name"),
                "vrack_vni": vrack_vni,
                "private_mac": private_mac,
                "public_mac": entry.get("public_mac")
                or cached.get("public_mac")
                or _public_mac(nics, private_mac),
                "private_ip": entry.get("private_ip") or cached.get("private_ip"),
                "public_ip": entry.get("public_ip") or cached.get("public_ip"),
                "nics": nics,
                "attached_to": cached.get("attached_to"),
                "error": cached.get("error"),
            }
        )
    return rows


def local_fabric_status(db: Session, env: Environment) -> dict[str, Any]:
    """Instant picture from the env doc + last OVH snapshot. No OVH HTTP."""
    current = envconfig_service.get_current(db, env)
    doc = current[0] if current else {}
    configured = vrack_from_doc(doc)
    vlan_id = vlan_id_from_doc(doc)
    private_cidr = private_cidr_from_doc(doc)
    cache = read_fabric_cache(env)
    server_rows = _rows_from_doc(doc, cache)
    missing = [
        r["hostname"]
        for r in server_rows
        if not r.get("attached_to")
        or (configured and r.get("attached_to") != configured)
    ]
    attached_ok = [
        r["hostname"]
        for r in server_rows
        if r.get("attached_to")
        and (not configured or r.get("attached_to") == configured)
    ]
    checked = bool(cache.get("checked_at"))
    if checked:
        missing = list(cache.get("missing") or missing)
        attached_ok = list(cache.get("attached") or attached_ok)
        interconnect = str(cache.get("interconnect") or "") or _interconnect(
            server_rows, missing, attached_ok, configured, checked=True
        )
    else:
        interconnect = _interconnect(
            server_rows, missing, attached_ok, configured, checked=False
        )
    vracks = list(cache.get("vracks") or [])
    if configured and not any(
        v.get("id") == configured for v in vracks if isinstance(v, dict)
    ):
        vracks = [
            {"id": configured, "name": configured, "description": "", "selected": True},
            *vracks,
        ]
    for item in vracks:
        if isinstance(item, dict):
            item["selected"] = item.get("id") == configured
    suggested = (
        cache.get("suggested_vrack")
        if isinstance(cache.get("suggested_vrack"), dict)
        else None
    )
    if not suggested:
        suggested = suggest_vrack(vracks, fabric_hints(db, env, doc))
    cidr_for_ips = private_cidr or DEFAULT_PRIVATE_CIDR
    proposed = private_ips_from_cidr(cidr_for_ips, [r["hostname"] for r in server_rows])
    return _status_payload(
        ok=True,
        configured=configured,
        vlan_id=vlan_id,
        private_cidr=private_cidr,
        vracks=vracks,
        server_rows=server_rows,
        missing=missing if checked else [],
        attached_ok=attached_ok if checked else [],
        ip_blocks=list(cache.get("ip_blocks") or []),
        discovered_vlans=list(cache.get("discovered_vlans") or []),
        interconnect=interconnect,
        source="cache" if checked else "local",
        checked_at=cache.get("checked_at"),
        error=cache.get("error"),
        suggested_vrack=suggested,
        proposed_ips=proposed,
    )


def _fill_nic(client: OvhClient, target: dict[str, Any]) -> dict[str, Any]:
    """Look up NICs (MAC + vRack VNI) when inventory doesn't have them."""
    service = str(target.get("service_name") or "").strip()
    if not service or target.get("error"):
        return target
    have_private = bool(target.get("vrack_vni") and target.get("private_mac"))
    have_nics = bool(target.get("nics"))
    if have_private and have_nics:
        return target
    nics = client.list_nics(service)
    raw: dict[str, Any] = {}
    if not have_private:
        try:
            raw = client.get_server(service) or {}
        except OvhError:
            raw = {}
    private = pick_private_nic(nics, raw)
    filled = dict(target)
    if private:
        if not filled.get("vrack_vni") and private.get("vni"):
            filled["vrack_vni"] = private.get("vni")
        if not filled.get("private_mac") and private.get("mac"):
            filled["private_mac"] = private.get("mac")
    view = _nics_view(
        nics, private_mac=filled.get("private_mac"), vrack_vni=filled.get("vrack_vni")
    )
    if view:
        filled["nics"] = view
        if not filled.get("public_mac"):
            filled["public_mac"] = _public_mac(view, filled.get("private_mac"))
    return filled


def _env_targets(
    client: OvhClient,
    doc: dict[str, Any],
    env: Environment,
    log: LogFn,
    *,
    live_nics: bool = True,
    hostname_filter: frozenset[str] | None = None,
) -> list[dict[str, Any]]:
    resolved = _ovh_resolve_service_names(
        client, doc, hostname_filter=hostname_filter, log=log, ovh_env=env_is_ovh(env)
    )
    servers = _doc_servers(doc)
    out: list[dict[str, Any]] = []
    for item in resolved:
        hostname = item.get("hostname")
        entry = servers.get(hostname) if hostname else {}
        if not isinstance(entry, dict):
            entry = {}
        nics = entry.get("nics") if isinstance(entry.get("nics"), list) else []
        target = {
            **item,
            "vrack_vni": entry.get("vrack_vni"),
            "private_mac": entry.get("private_mac"),
            "public_mac": entry.get("public_mac"),
            "private_ip": entry.get("private_ip"),
            "public_ip": entry.get("public_ip"),
            "nics": _nics_view(
                nics,
                private_mac=entry.get("private_mac"),
                vrack_vni=entry.get("vrack_vni"),
            ),
        }
        if live_nics:
            target = _fill_nic(client, target)
        out.append(target)
    return out


def _persist_discovered_nics(
    db: Session,
    env: Environment,
    targets: list[dict[str, Any]],
) -> None:
    """Write newly looked-up MAC/VNI onto the inventory so Talos can tag."""
    current = envconfig_service.get_current(db, env)
    if current is None:
        return
    doc = dict(current[0])
    servers = dict(doc.get("servers") or {})
    changed = False
    for target in targets:
        hostname = target.get("hostname")
        entry = servers.get(hostname)
        if not hostname or not isinstance(entry, dict):
            continue
        updated = dict(entry)
        for key in (
            "private_mac",
            "vrack_vni",
            "public_mac",
            "private_ip",
            "public_ip",
        ):
            val = target.get(key)
            if val and not updated.get(key):
                updated[key] = val
                changed = True
        nics = target.get("nics")
        if nics and not updated.get("nics"):
            updated["nics"] = nics
            changed = True
        servers[hostname] = updated
    if not changed:
        return
    doc["servers"] = servers
    envconfig_service.put_version(db, env, envconfig_service._dump(doc), "ovh-fabric")


def fabric_status(
    db: Session,
    env: Environment,
    *,
    log: LogFn | None = None,
    refresh: bool = False,
) -> dict[str, Any]:
    """vRack membership + VLAN + interconnect.

    ``refresh=False`` (default) is local/cache only — safe to call on every
    Inventory load. ``refresh=True`` talks to OVH and updates the snapshot.
    """
    if not refresh:
        return local_fabric_status(db, env)
    return refresh_fabric_status(db, env, log=log)


def refresh_fabric_status(
    db: Session,
    env: Environment,
    *,
    log: LogFn | None = None,
) -> dict[str, Any]:
    """Hit OVH, persist the snapshot, return the live picture."""
    _log = log or (lambda _m: None)
    current = envconfig_service.get_current(db, env)
    doc = current[0] if current else {}
    configured = vrack_from_doc(doc)
    vlan_id = vlan_id_from_doc(doc)
    private_cidr = private_cidr_from_doc(doc)
    client, err = _client_for_env(db, env)
    if err or client is None:
        local = local_fabric_status(db, env)
        local["ok"] = False
        local["error"] = err
        return local
    try:
        names = client.list_vracks()
        vracks = []
        for name in names:
            info = client.get_vrack(name) or {}
            label = str(info.get("name") or "").strip() or name
            vracks.append(
                {
                    "id": name,
                    "name": label,
                    "description": str(info.get("description") or "").strip(),
                    "selected": name == configured,
                }
            )
        suggested = suggest_vrack(vracks, fabric_hints(db, env, doc))
        targets = _env_targets(client, doc, env, _log, live_nics=True)
        attached_map: dict[str, set[str]] = {}
        ip_blocks: list[dict[str, Any]] = []
        discovered_vlans: list[int] = []
        scan_id = configured or (suggested or {}).get("id")
        scan = [scan_id] if scan_id else names[:1]
        for vrack in scan:
            if not vrack:
                continue
            members = set(client.vrack_dedicated_servers(vrack))
            details = client.vrack_interface_details(vrack)
            for row in details:
                if row.get("server"):
                    members.add(str(row["server"]))
                if row.get("interface"):
                    members.add(str(row["interface"]))
            for iface in client.vrack_dedicated_interfaces(vrack):
                members.add(iface)
            attached_map[vrack] = members
            for block in client.list_vrack_ips(vrack):
                item = dict(block)
                item["vrack"] = vrack
                ip_blocks.append(item)
                vlan = block.get("vlan")
                if isinstance(vlan, int) and vlan not in discovered_vlans:
                    discovered_vlans.append(vlan)
        discovered_vlans.sort()

        server_rows: list[dict[str, Any]] = []
        missing: list[str] = []
        attached_ok: list[str] = []
        for target in targets:
            hostname = target["hostname"]
            service = target.get("service_name")
            vni = target.get("vrack_vni")
            error = target.get("error")
            attached_to = None
            for vrack, members in attached_map.items():
                if (service and service in members) or (vni and vni in members):
                    attached_to = vrack
                    break
            nics = list(target.get("nics") or [])
            row = {
                "hostname": hostname,
                "service_name": service,
                "vrack_vni": vni,
                "private_mac": target.get("private_mac"),
                "public_mac": target.get("public_mac")
                or _public_mac(nics, target.get("private_mac")),
                "private_ip": target.get("private_ip"),
                "public_ip": target.get("public_ip"),
                "nics": nics,
                "attached_to": attached_to,
                "error": error,
            }
            server_rows.append(row)
            if error:
                continue
            if attached_to and (not configured or attached_to == configured):
                attached_ok.append(hostname)
            else:
                missing.append(hostname)

        interconnect = _interconnect(
            targets, missing, attached_ok, configured, checked=True
        )
        cidr_for_ips = private_cidr or DEFAULT_PRIVATE_CIDR
        proposed = private_ips_from_cidr(
            cidr_for_ips, [r["hostname"] for r in server_rows]
        )
        payload = _status_payload(
            ok=True,
            configured=configured,
            vlan_id=vlan_id,
            private_cidr=private_cidr,
            vracks=vracks,
            server_rows=server_rows,
            missing=missing,
            attached_ok=attached_ok,
            ip_blocks=ip_blocks,
            discovered_vlans=discovered_vlans,
            interconnect=interconnect,
            source="live",
            checked_at=_utcnow(),
            suggested_vrack=suggested,
            proposed_ips=proposed,
        )
        write_fabric_cache(db, env, payload)
        db.commit()
        return payload
    except OvhError as exc:
        local = local_fabric_status(db, env)
        local["ok"] = False
        local["error"] = str(exc)
        return local
    finally:
        client.close()


def attach_env_to_vrack(
    db: Session,
    env: Environment,
    *,
    vrack: str | None = None,
    dry_run: bool = False,
    log: LogFn | None = None,
    hostname_filter: frozenset[str] | None = None,
) -> dict[str, Any]:
    """Attach every OVH-owned env server to the configured (or given) vRack."""
    _log = log or (lambda _m: None)
    current = envconfig_service.get_current(db, env)
    if current is None:
        return {
            "ok": False,
            "error": "environment has no config document",
            "servers": [],
        }
    doc = current[0]
    vrack_id = str(vrack or vrack_from_doc(doc) or "").strip()
    if not vrack_id:
        return {
            "ok": False,
            "error": "no vRack selected — set ovh.vrack on the environment first",
            "servers": [],
        }
    client, err = _client_for_env(db, env)
    if err or client is None:
        return {"ok": False, "error": err, "servers": []}
    results: list[dict[str, Any]] = []
    try:
        already_servers = set(client.vrack_dedicated_servers(vrack_id))
        already_ifaces = set(client.vrack_dedicated_interfaces(vrack_id))
        for row in client.vrack_interface_details(vrack_id):
            if row.get("server"):
                already_servers.add(str(row["server"]))
            if row.get("interface"):
                already_ifaces.add(str(row["interface"]))
        eligible = client.vrack_eligible_services(vrack_id)
        eligible_servers = {
            str(x) for x in (eligible.get("dedicatedServer") or []) if x
        }
        eligible_ifaces = {
            str(x) for x in (eligible.get("dedicatedServerInterface") or []) if x
        }

        targets = _env_targets(
            client, doc, env, _log, live_nics=True, hostname_filter=hostname_filter
        )
        for target in targets:
            hostname = target["hostname"]
            service = target.get("service_name")
            vni = str(target.get("vrack_vni") or "").strip() or None
            if target.get("error"):
                results.append(
                    {"hostname": hostname, "ok": False, "error": target["error"]}
                )
                continue
            if (vni and vni in already_ifaces) or (
                service and service in already_servers
            ):
                _log(f"[vrack] {hostname} already on {vrack_id}")
                results.append(
                    {"hostname": hostname, "ok": True, "skipped": "already attached"}
                )
                continue
            use_iface = bool(vni) and (not eligible_ifaces or vni in eligible_ifaces)
            if dry_run:
                how = f"interface {vni}" if use_iface else f"server {service}"
                _log(f"[dry-run] would attach {hostname} to {vrack_id} via {how}")
                results.append({"hostname": hostname, "ok": True, "dry_run": True})
                continue
            try:
                if use_iface:
                    task = client.attach_interface_to_vrack(vrack_id, vni)
                    _log(
                        f"[vrack] attached {hostname} interface {vni} -> {vrack_id} task={task}"
                    )
                elif service and (not eligible_servers or service in eligible_servers):
                    task = client.attach_server_to_vrack(vrack_id, service)
                    _log(
                        f"[vrack] attached {hostname} server {service} -> {vrack_id} task={task}"
                    )
                else:
                    raise OvhError(
                        f"{hostname} is not eligible for vRack {vrack_id} "
                        f"(vni={vni or '-'} server={service or '-'})"
                    )
                results.append(
                    {
                        "hostname": hostname,
                        "ok": True,
                        "task_id": str(task) if task else None,
                    }
                )
            except OvhError as exc:
                _log(f"[vrack] attach {hostname} failed: {exc}")
                results.append({"hostname": hostname, "ok": False, "error": str(exc)})
        failed = [r for r in results if not r.get("ok")]
        ok = not failed
        message = (
            f"[dry-run] would attach {len(results)} server(s) to {vrack_id}"
            if dry_run
            else f"attached {len(results) - len(failed)}/{len(results)} server(s) to {vrack_id}"
        )
        _log(f"[vrack] {message}")
        if not dry_run:
            _persist_discovered_nics(db, env, targets)
            cache = read_fabric_cache(env)
            by_host = {
                str(s.get("hostname")): dict(s)
                for s in (cache.get("servers") or [])
                if isinstance(s, dict) and s.get("hostname")
            }
            for row in results:
                if not row.get("ok"):
                    continue
                host = row["hostname"]
                entry = by_host.get(host) or {"hostname": host}
                entry["attached_to"] = vrack_id
                by_host[host] = entry
            cache["servers"] = list(by_host.values())
            cache["checked_at"] = cache.get("checked_at") or _utcnow()
            cache["vrack"] = vrack_id
            write_fabric_cache(db, env, cache)
            db.commit()
        return {
            "ok": ok,
            "vrack": vrack_id,
            "count": len(results),
            "servers": results,
            "dry_run": dry_run,
            "message": message,
            **(
                {}
                if ok
                else {
                    "error": message
                    + ": "
                    + "; ".join(f"{r['hostname']}: {r.get('error')}" for r in failed)
                }
            ),
        }
    except OvhError as exc:
        _log(f"[vrack] {exc}")
        return {"ok": False, "error": str(exc), "servers": results, "vrack": vrack_id}
    finally:
        client.close()


def provision_greenfield(
    db: Session,
    env: Environment,
    actor: str | None,
    *,
    vrack: str | None = None,
    vlan_id: int | None = None,
    private_cidr: str | None = None,
    assign_ips: bool = True,
    overwrite_ips: bool = False,
    attach: bool = False,
    dry_run: bool = False,
    log: LogFn | None = None,
) -> dict[str, Any]:
    """Turnkey fabric: pick vRack, VLAN, CIDR, assign private IPs, optional attach."""
    _log = log or (lambda _m: None)
    current = envconfig_service.get_current(db, env)
    doc: dict[str, Any] = dict(current[0]) if current else {}
    cache = read_fabric_cache(env)
    vracks = list(cache.get("vracks") or [])
    suggested = suggest_vrack(vracks, fabric_hints(db, env, doc))
    vrack_id = str(
        vrack or vrack_from_doc(doc) or (suggested or {}).get("id") or ""
    ).strip()
    if not vrack_id:
        return {
            "ok": False,
            "error": "no vRack selected — pick one (or name it so we can match the cluster)",
        }
    cidr = str(
        private_cidr or private_cidr_from_doc(doc) or DEFAULT_PRIVATE_CIDR
    ).strip()
    vlan = int(vlan_id) if vlan_id is not None else vlan_id_from_doc(doc)
    if vlan < 0 or vlan > 4000:
        return {"ok": False, "error": "vlan_id must be 0 (untagged) or 1–4000"}
    hostnames = [
        hostname for hostname, entry in _doc_servers(doc).items() if _is_ovh_host(entry)
    ]
    ips = private_ips_from_cidr(cidr, hostnames) if assign_ips else {}
    _log(
        f"[fabric] provision vrack={vrack_id} vlan={vlan} cidr={cidr} "
        f"ips={len(ips)} attach={attach} dry_run={dry_run}"
    )
    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "vrack": vrack_id,
            "vlan_id": vlan,
            "private_cidr": cidr,
            "ips": ips,
            "attach": attach,
            "message": (
                f"[dry-run] would set vRack {vrack_id}, VLAN {vlan}, CIDR {cidr}"
                + (f", {len(ips)} private IPs" if ips else "")
                + (" and attach NICs" if attach else "")
            ),
        }

    envconfig_service.set_ovh_fabric(
        db, env, actor, vrack=vrack_id, vlan_id=vlan, private_cidr=cidr
    )
    assigned: dict[str, str] = {}
    if ips:
        current = envconfig_service.get_current(db, env)
        doc = dict(current[0]) if current else doc
        servers = dict(doc.get("servers") or {})
        changed = False
        for hostname, ip in ips.items():
            entry = servers.get(hostname)
            if not isinstance(entry, dict):
                continue
            if entry.get("private_ip") and not overwrite_ips:
                continue
            updated = dict(entry)
            updated["private_ip"] = ip
            updated["ip"] = ip
            servers[hostname] = updated
            assigned[hostname] = ip
            changed = True
        if changed:
            doc["servers"] = servers
            envconfig_service.put_version(db, env, envconfig_service._dump(doc), actor)
            _log(f"[fabric] assigned private IPs: {assigned}")

    attach_result: dict[str, Any] | None = None
    if attach:
        attach_result = attach_env_to_vrack(
            db, env, vrack=vrack_id, dry_run=False, log=_log
        )
        db.commit()
    else:
        db.commit()
    ok = True if not attach else bool((attach_result or {}).get("ok"))
    message = (
        f"fabric set: vRack {vrack_id}, VLAN {vlan}, {cidr}"
        + (f", {len(assigned)} private IPs" if assigned else "")
        + (f"; attach {(attach_result or {}).get('message')}" if attach_result else "")
    )
    return {
        "ok": ok,
        "vrack": vrack_id,
        "vlan_id": vlan,
        "private_cidr": cidr,
        "ips": assigned or ips,
        "attach": attach_result,
        "message": message,
        **({} if ok else {"error": (attach_result or {}).get("error") or message}),
    }
