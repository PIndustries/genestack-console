"""Local network suggestions and validation; never changes an interface or starts DHCP."""

from __future__ import annotations

import ipaddress
import json
import re
import subprocess
from pathlib import Path
from typing import Any

KEYS = frozenset(
    {
        "interface",
        "range_start",
        "range_end",
        "netmask",
        "gateway",
        "dns",
        "next_server",
        "http_port",
    }
)


def _json_command(argv: list[str]) -> list[dict[str, Any]]:
    result = subprocess.run(argv, capture_output=True, text=True, timeout=5, check=False)
    if result.returncode:
        return []
    value = json.loads(result.stdout)
    return value if isinstance(value, list) else []


def dns_servers() -> list[str]:
    for path in ["/run/systemd/resolve/resolv.conf", "/etc/resolv.conf"]:
        try:
            values = []
            for line in Path(path).read_text().splitlines():
                parts = line.split()
                if len(parts) != 2 or parts[0] != "nameserver":
                    continue
                try:
                    ip = ipaddress.IPv4Address(parts[1])
                except ValueError:
                    continue
                if not ip.is_loopback and not ip.is_link_local and not ip.is_unspecified:
                    values.append(str(ip))
            if values:
                return list(dict.fromkeys(values))[:3]
        except OSError:
            continue
    return []


def interfaces() -> list[dict[str, Any]]:
    """Only connected Ethernet networks; no VPN or container-only interface guess."""
    try:
        addresses = _json_command(["ip", "-j", "-d", "address", "show"])
        routes = _json_command(["ip", "-j", "route", "show", "default"])
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return []
    dns = dns_servers()
    found = []
    for interface in addresses:
        name = interface.get("ifname", "")
        kind = interface.get("linkinfo", {}).get("info_kind", "")
        if (
            interface.get("link_type") != "ether"
            or "UP" not in interface.get("flags", [])
            or kind in ["wireguard", "tun", "veth"]
            or name.startswith(("docker", "veth", "cni", "flannel", "kube", "virbr"))
            or re.fullmatch(r"br-[0-9a-f]{12}", name)
        ):
            continue
        for item in interface.get("addr_info", []):
            if item.get("family") != "inet":
                continue
            try:
                address = ipaddress.IPv4Interface(str(item["local"]) + "/" + str(item["prefixlen"]))
            except (KeyError, ValueError):
                continue
            if (
                address.ip.is_global
                or address.ip.is_link_local
                or address.ip.is_loopback
                or address.ip.is_multicast
                or address.ip.is_unspecified
                or address.ip.is_reserved
            ):
                continue
            gateways = [
                r.get("gateway") for r in routes if r.get("dev") == name and r.get("gateway")
            ]
            gateway = next((g for g in gateways if ipaddress.IPv4Address(g) in address.network), "")
            low, high = (
                max(
                    int(address.network.network_address) + 1,
                    int(address.network.broadcast_address) - 64,
                ),
                int(address.network.broadcast_address) - 1,
            )
            reserved = sorted(
                {int(address.ip)} | ({int(ipaddress.IPv4Address(gateway))} if gateway else set())
            )
            segments = []
            for ip in reserved:
                if low <= ip <= high:
                    if low < ip:
                        segments.append((low, ip - 1))
                    low = ip + 1
            if low <= high:
                segments.append((low, high))
            pool = max(segments, key=lambda p: p[1] - p[0], default=None)
            suggested = (
                None
                if pool is None
                else {
                    "interface": name,
                    "next_server": str(address.ip),
                    "netmask": str(address.network.netmask),
                    "range_start": str(ipaddress.IPv4Address(pool[0])),
                    "range_end": str(ipaddress.IPv4Address(pool[1])),
                    "gateway": gateway,
                    "dns": ",".join(dns) or gateway,
                    "http_port": 8088,
                }
            )
            found.append(
                {
                    "id": name + ":" + str(address),
                    "name": name,
                    "address": str(address),
                    "mac": interface.get("address", ""),
                    "suggested_config": suggested,
                }
            )
    return found[:32]


def validate(
    config: dict[str, Any], choices: list[dict[str, Any]], api_port: int
) -> dict[str, Any]:
    if not isinstance(config, dict) or set(config) - KEYS:
        raise ValueError("Unknown provisioning network setting")
    result = {k: v.strip() if isinstance(v, str) else v for k, v in config.items()}
    for name in sorted(KEYS - {"http_port"}):
        if not isinstance(result.get(name), str) or not result[name]:
            raise ValueError("Enter " + name.replace("_", " ") + " for the provisioning network")
    port = result.get("http_port")
    if (
        isinstance(port, bool)
        or not isinstance(port, int)
        or not 1024 <= port <= 65535
        or port == api_port
    ):
        raise ValueError(
            "Choose a provisioning port from 1024 to 65535, distinct from the console API"
        )
    try:
        server = ipaddress.IPv4Address(result["next_server"])
        network = ipaddress.IPv4Network(str(server) + "/" + result["netmask"], strict=False)
        start, end, gateway = [
            ipaddress.IPv4Address(result[k]) for k in ["range_start", "range_end", "gateway"]
        ]
        dns = [ipaddress.IPv4Address(part.strip()) for part in result["dns"].split(",")]
    except ValueError as exc:
        raise ValueError("Enter valid IPv4 addresses and a contiguous network mask") from exc
    if (
        server.is_global
        or server.is_loopback
        or server.is_link_local
        or server.is_unspecified
        or server.is_multicast
        or server.is_reserved
    ):
        raise ValueError("Choose a private Ethernet provisioning network")
    if not any(
        c["name"] == result["interface"]
        and ipaddress.IPv4Interface(c["address"]).ip == server
        and ipaddress.IPv4Interface(c["address"]).network == network
        for c in choices
    ):
        raise ValueError(
            "The serving address and network mask must match the selected connected interface"
        )
    if any(
        ip not in network or ip in [network.network_address, network.broadcast_address]
        for ip in [start, end, gateway, server]
    ):
        raise ValueError(
            "Machine addresses and gateway must be usable addresses on the selected network"
        )
    if start > end or start <= server <= end or start <= gateway <= end:
        raise ValueError(
            "The reserved machine range must be ordered and exclude the deployer and gateway"
        )
    if not 1 <= len(dns) <= 3 or any(
        ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast for ip in dns
    ):
        raise ValueError("Enter up to three reachable DNS server addresses")
    result["netmask"] = str(network.netmask)
    result["dns"] = ",".join(str(ip) for ip in dns)
    return result
