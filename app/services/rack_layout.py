"""Rack layout derived from environment servers plus optional rack metadata.

Servers stay in the config document. Rack name, U position, gear, and planned
links live only under ``doc["rack"]``. ``layout`` does not mutate its inputs.

Returned shape::

    {
      "racks": [{"name": "Rack 1", "height": 48, "items": [item, ...]}],
      "links": [{"source", "target", "kind": "planned"|"seen", "label", "id"?}],
      "logical": [{
          "hostname", "rack", "u_start", "u_height", "side",
          "roles", "in_cluster", "vms"
      }]
    }

A machine or gear item has ``rack``, ``u_start``, ``u_height``, ``side``, and
``conflict``. Machines use ``type: "machine"`` and ``hostname``. Gear uses
``type: "gear"``, ``id``, ``name``, and ``kind`` (``switch``, ``pdu``, or
``layer``). Automatic overflow stays on Rack 1 with ``side: true`` and
``u_start: null`` instead of opening another rack. Seen links are only the
observed facts passed in: a ready internal IP, a BMC hostname, or a VM on a
host. Switch and PDU cables are never invented.
"""

from __future__ import annotations

from typing import Any

DEFAULT_RACK = "Rack 1"
RACK_HEIGHT = 48
GEAR_KINDS = frozenset({"switch", "pdu", "layer"})

_ADDRESS_KEYS = ("ip", "private_ip", "public_ip")


def layout(
    servers: dict[str, Any] | None,
    rack_doc: dict[str, Any] | None,
    observed: dict[str, Any] | None,
) -> dict[str, Any]:
    """Place servers and gear, then attach planned and seen links."""
    server_map = servers if isinstance(servers, dict) else {}
    rack = rack_doc if isinstance(rack_doc, dict) else {}
    facts = observed if isinstance(observed, dict) else {}
    placements = rack.get("placements")
    if not isinstance(placements, dict):
        placements = {}
    ready_ips = _string_set(facts.get("ready_ips"))
    bmc_hosts = _string_set(facts.get("bmc_hosts"))
    vm_rows = _vm_rows(facts.get("vms"))
    vms_by_host = _vms_by_host(vm_rows)

    machines: list[dict[str, Any]] = []
    auto_machines: list[dict[str, Any]] = []
    for raw_name, entry in server_map.items():
        hostname = str(raw_name or "").strip()
        if not hostname:
            continue
        body = entry if isinstance(entry, dict) else {}
        item: dict[str, Any] = {
            "type": "machine",
            "hostname": hostname,
            "rack": DEFAULT_RACK,
            "u_start": None,
            "u_height": 1,
            "side": False,
            "roles": _roles(body),
            "conflict": False,
            "in_cluster": bool(_addresses(body) & ready_ips),
        }
        saved = _explicit_placement(placements.get(hostname))
        if saved is None:
            auto_machines.append(item)
        else:
            item.update(saved)
        machines.append(item)
    auto_machines.sort(key=lambda row: row["hostname"])
    _auto_place(auto_machines, DEFAULT_RACK)

    gear_items: list[dict[str, Any]] = []
    auto_gear: dict[str, list[dict[str, Any]]] = {}
    raw_gear = rack.get("gear")
    if isinstance(raw_gear, list):
        for raw in raw_gear:
            if not isinstance(raw, dict):
                continue
            gear = {
                "type": "gear",
                "id": str(raw.get("id") or ""),
                "name": str(raw.get("name") or ""),
                "kind": str(raw.get("kind") or ""),
                "rack": _rack_name(raw.get("rack")),
                "u_start": None,
                "u_height": _height(raw.get("u_height")),
                "side": False,
                "conflict": False,
            }
            start = _positive_int(raw.get("u_start"))
            if start is None:
                auto_gear.setdefault(gear["rack"], []).append(gear)
            else:
                gear["u_start"] = start
            gear_items.append(gear)
    for rack_name in sorted(auto_gear):
        rows = auto_gear[rack_name]
        rows.sort(key=lambda row: (row["name"], row["id"]))
        _auto_place(rows, rack_name)

    items = [*machines, *gear_items]
    _mark_conflicts(items)
    rack_names = {DEFAULT_RACK, *(item["rack"] for item in items)}
    ordered = [DEFAULT_RACK, *sorted(name for name in rack_names if name != DEFAULT_RACK)]
    racks = []
    for name in ordered:
        housed = [item for item in items if item["rack"] == name]
        housed.sort(key=_item_sort)
        racks.append({"name": name, "height": RACK_HEIGHT, "items": housed})

    links = _planned_links(rack.get("links"))
    by_host = {row["hostname"]: row for row in machines}
    for hostname in sorted(by_host):
        if by_host[hostname]["in_cluster"]:
            links.append(
                {
                    "source": hostname,
                    "target": "kubernetes",
                    "kind": "seen",
                    "label": "kubernetes",
                }
            )
    for hostname in sorted(by_host):
        if hostname in bmc_hosts:
            links.append(
                {
                    "source": hostname,
                    "target": "management-port",
                    "kind": "seen",
                    "label": "management-port",
                }
            )
    for vm in vm_rows:
        links.append(
            {
                "source": vm["host"],
                "target": vm["name"],
                "kind": "seen",
                "label": "vm",
            }
        )

    logical = [
        {
            "hostname": hostname,
            "rack": by_host[hostname]["rack"],
            "u_start": by_host[hostname]["u_start"],
            "u_height": by_host[hostname]["u_height"],
            "side": by_host[hostname]["side"],
            "roles": list(by_host[hostname]["roles"]),
            "in_cluster": by_host[hostname]["in_cluster"],
            "vms": list(vms_by_host.get(hostname, [])),
        }
        for hostname in sorted(by_host)
    ]
    return {"racks": racks, "links": links, "logical": logical}


def copy_rack(rack_doc: dict[str, Any] | None) -> dict[str, Any]:
    """Shallow copy of the rack section with placements, gear, and links."""
    raw = rack_doc if isinstance(rack_doc, dict) else {}
    placements_in = raw.get("placements")
    gear_in = raw.get("gear")
    links_in = raw.get("links")
    placements: dict[str, Any] = {}
    if isinstance(placements_in, dict):
        for key, value in placements_in.items():
            name = str(key).strip()
            if not name:
                continue
            placements[name] = dict(value) if isinstance(value, dict) else value
    if isinstance(gear_in, list):
        gear = [dict(item) if isinstance(item, dict) else item for item in gear_in]
    else:
        gear = []
    if isinstance(links_in, list):
        links = [dict(item) if isinstance(item, dict) else item for item in links_in]
    else:
        links = []
    out = {
        key: value
        for key, value in raw.items()
        if key not in {"placements", "gear", "links"}
    }
    out["placements"] = placements
    out["gear"] = gear
    out["links"] = links
    return out


def with_placement(
    rack_doc: dict[str, Any] | None,
    hostname: str,
    *,
    rack_name: str | None,
    u_start: int | None,
    u_height: int | None,
) -> dict[str, Any]:
    """Merge one machine placement. ``u_start`` None returns it to automatic."""
    rack = copy_rack(rack_doc)
    name = hostname.strip()
    if u_start is None:
        rack["placements"].pop(name, None)
        return rack
    rack["placements"][name] = {
        "rack": _rack_name(rack_name),
        "u_start": u_start,
        "u_height": u_height if isinstance(u_height, int) and u_height >= 1 else 1,
    }
    return rack


def add_gear(
    rack_doc: dict[str, Any] | None,
    *,
    gear_id: str,
    name: str,
    kind: str,
    rack_name: str | None,
    u_start: int | None,
    u_height: int | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Append one gear row. Returns the new rack section and the stored gear."""
    rack = copy_rack(rack_doc)
    gear: dict[str, Any] = {
        "id": gear_id,
        "name": name.strip(),
        "kind": kind,
        "rack": _rack_name(rack_name),
        "u_height": u_height if isinstance(u_height, int) and u_height >= 1 else 1,
    }
    if isinstance(u_start, int) and u_start >= 1:
        gear["u_start"] = u_start
    rack["gear"].append(gear)
    return rack, gear


def remove_gear(rack_doc: dict[str, Any] | None, gear_id: str) -> dict[str, Any] | None:
    """Drop one gear id. None when that id is not stored."""
    rack = copy_rack(rack_doc)
    kept = []
    found = False
    for item in rack["gear"]:
        if not found and isinstance(item, dict) and str(item.get("id") or "") == gear_id:
            found = True
            continue
        kept.append(item)
    if not found:
        return None
    rack["gear"] = kept
    return rack


def add_link(
    rack_doc: dict[str, Any] | None,
    *,
    link_id: str,
    source: str,
    target: str,
    label: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Append one planned link. Kind is applied when the layout is built."""
    rack = copy_rack(rack_doc)
    link = {
        "id": link_id,
        "source": source.strip(),
        "target": target.strip(),
        "label": label,
    }
    rack["links"].append(link)
    return rack, link


def remove_link(rack_doc: dict[str, Any] | None, link_id: str) -> dict[str, Any] | None:
    """Drop one planned link id. None when that id is not stored."""
    rack = copy_rack(rack_doc)
    kept = []
    found = False
    for item in rack["links"]:
        if not found and isinstance(item, dict) and str(item.get("id") or "") == link_id:
            found = True
            continue
        kept.append(item)
    if not found:
        return None
    rack["links"] = kept
    return rack


def _auto_place(items: list[dict[str, Any]], rack_name: str) -> None:
    """Stack items from U1. A span past the top stays on this rack, on the side."""
    cursor = 1
    for item in items:
        height = _height(item.get("u_height"))
        item["u_height"] = height
        item["rack"] = rack_name
        if cursor + height - 1 > RACK_HEIGHT:
            item["side"] = True
            item["u_start"] = None
            continue
        item["side"] = False
        item["u_start"] = cursor
        cursor += height


def _explicit_placement(saved: Any) -> dict[str, Any] | None:
    if not isinstance(saved, dict):
        return None
    start = _positive_int(saved.get("u_start"))
    if start is None:
        return None
    return {
        "rack": _rack_name(saved.get("rack")),
        "u_start": start,
        "u_height": _height(saved.get("u_height")),
        "side": False,
    }


def _mark_conflicts(items: list[dict[str, Any]]) -> None:
    spans: list[tuple[int, int] | None] = [_span(item) for item in items]
    for item in items:
        item["conflict"] = False
    for index, left in enumerate(items):
        left_span = spans[index]
        if left_span is None:
            continue
        for follow in range(index + 1, len(items)):
            right_span = spans[follow]
            if right_span is None or items[follow]["rack"] != left["rack"]:
                continue
            if left_span[0] <= right_span[1] and right_span[0] <= left_span[1]:
                left["conflict"] = True
                items[follow]["conflict"] = True


def _span(item: dict[str, Any]) -> tuple[int, int] | None:
    if item.get("side"):
        return None
    start = item.get("u_start")
    height = item.get("u_height")
    if not isinstance(start, int) or start < 1:
        return None
    if not isinstance(height, int) or height < 1:
        height = 1
    return start, start + height - 1


def _planned_links(raw_links: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_links, list):
        return []
    links = []
    for raw in raw_links:
        if not isinstance(raw, dict):
            continue
        source = str(raw.get("source") or "").strip()
        target = str(raw.get("target") or "").strip()
        if not source or not target:
            continue
        links.append(
            {
                "id": str(raw.get("id") or ""),
                "source": source,
                "target": target,
                "label": str(raw.get("label") or ""),
                "kind": "planned",
            }
        )
    return links


def _item_sort(item: dict[str, Any]) -> tuple[Any, ...]:
    side = 1 if item.get("side") else 0
    start = item.get("u_start")
    u_start = start if isinstance(start, int) else 10**6
    label = str(item.get("hostname") or item.get("name") or "")
    return (side, u_start, label, str(item.get("id") or ""))


def _roles(entry: dict[str, Any]) -> list[str]:
    raw = entry.get("roles")
    if not isinstance(raw, list):
        return []
    return [str(role) for role in raw if role is not None and str(role) != ""]


def _addresses(entry: dict[str, Any]) -> set[str]:
    found: set[str] = set()
    for key in _ADDRESS_KEYS:
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            found.add(value.strip())
    return found


def _vms_by_host(rows: list[dict[str, str]]) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    for row in rows:
        names = grouped.setdefault(row["host"], [])
        if row["name"] not in names:
            names.append(row["name"])
    return grouped


def _vm_rows(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    rows = []
    for item in value:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        host = str(item.get("host") or "").strip()
        if name and host:
            rows.append({"name": name, "host": host})
    return rows


def _string_set(value: Any) -> set[str]:
    if value is None or isinstance(value, bool):
        return set()
    if isinstance(value, str):
        text = value.strip()
        return {text} if text else set()
    if isinstance(value, (set, list, tuple)):
        found = set()
        for item in value:
            if item is None or isinstance(item, bool):
                continue
            text = str(item).strip()
            if text:
                found.add(text)
        return found
    return set()


def _rack_name(value: Any) -> str:
    text = str(value or "").strip()
    return text or DEFAULT_RACK


def _height(value: Any) -> int:
    parsed = _positive_int(value)
    return parsed if parsed is not None else 1


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if value >= 1 else None
    if isinstance(value, float) and value.is_integer():
        number = int(value)
        return number if number >= 1 else None
    if isinstance(value, str) and value.strip().isdigit():
        number = int(value.strip())
        return number if number >= 1 else None
    return None
