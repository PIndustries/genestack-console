"""In-process PXE: DHCP + boot-file HTTP, owned by the Console.

Operators get a compiled binary. PXE configuration, leases, and runtime
state live in Python — not a docker sidecar. The console renders
``boot.ipxe`` / assets under ``<data_dir>/pxe/``, serves them over HTTP,
and answers DHCP on the provisioning NIC so iPXE clients chainload those
files.

DHCP is a focused PXE subset (DISCOVER/OFFER/REQUEST/ACK, static
reservations, iPXE user-class → HTTP boot URL). Bind failures are
reported in status; they do not crash the API.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import socket
import struct
import threading
import time
from dataclasses import dataclass, field
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

BOOTREQUEST = 1
BOOTREPLY = 2
DHCPDISCOVER = 1
DHCPOFFER = 2
DHCPREQUEST = 3
DHCPACK = 5
DHCPNAK = 6
DHCP_MAGIC = b"\x63\x82\x53\x63"
OPT_SUBNET = 1
OPT_ROUTER = 3
OPT_DNS = 6
OPT_LEASE = 51
OPT_MSGTYPE = 53
OPT_SERVER_ID = 54
OPT_REQUESTED_IP = 50
OPT_VENDOR = 60
OPT_CLIENT_ID = 61
OPT_TFTP = 66
OPT_BOOTFILE = 67
OPT_USER_CLASS = 77
OPT_END = 255

SO_BINDTODEVICE = 25


def _ip_to_bytes(ip: str) -> bytes:
    return socket.inet_aton(ip)


def _bytes_to_ip(raw: bytes) -> str:
    return socket.inet_ntoa(raw)


def _mac_to_str(chaddr: bytes, hlen: int) -> str:
    return ":".join(f"{b:02x}" for b in chaddr[: max(0, min(hlen, 16))])


def _parse_options(blob: bytes) -> dict[int, bytes]:
    opts: dict[int, bytes] = {}
    i = 0
    while i < len(blob):
        tag = blob[i]
        i += 1
        if tag == 0:
            continue
        if tag == OPT_END:
            break
        if i >= len(blob):
            break
        length = blob[i]
        i += 1
        opts[tag] = blob[i : i + length]
        i += length
    return opts


def _pack_options(opts: list[tuple[int, bytes]]) -> bytes:
    out = bytearray()
    for tag, val in opts:
        out.append(tag)
        out.append(len(val))
        out.extend(val)
    out.append(OPT_END)
    return bytes(out)


@dataclass
class DhcpPacket:
    op: int
    htype: int
    hlen: int
    hops: int
    xid: int
    secs: int
    flags: int
    ciaddr: str
    yiaddr: str
    siaddr: str
    giaddr: str
    chaddr: bytes
    sname: bytes
    file: bytes
    options: dict[int, bytes]

    @property
    def mac(self) -> str:
        return _mac_to_str(self.chaddr, self.hlen)

    @property
    def msg_type(self) -> int:
        raw = self.options.get(OPT_MSGTYPE, b"\x00")
        return raw[0] if raw else 0

    @property
    def is_ipxe(self) -> bool:
        user = self.options.get(OPT_USER_CLASS, b"")
        vendor = self.options.get(OPT_VENDOR, b"")
        return b"iPXE" in user or b"iPXE" in vendor

    def to_bytes(self) -> bytes:
        buf = struct.pack(
            "!BBBBIHH4s4s4s4s",
            self.op,
            self.htype,
            self.hlen,
            self.hops,
            self.xid,
            self.secs,
            self.flags,
            _ip_to_bytes(self.ciaddr),
            _ip_to_bytes(self.yiaddr),
            _ip_to_bytes(self.siaddr),
            _ip_to_bytes(self.giaddr),
        )
        chaddr = (self.chaddr + b"\x00" * 16)[:16]
        sname = (self.sname + b"\x00" * 64)[:64]
        file = (self.file + b"\x00" * 128)[:128]
        opts = _pack_options([(k, v) for k, v in self.options.items()])
        return buf + chaddr + sname + file + DHCP_MAGIC + opts


def parse_dhcp(data: bytes) -> DhcpPacket | None:
    if len(data) < 240:
        return None
    op, htype, hlen, hops, xid, secs, flags = struct.unpack("!BBBBIHH", data[:12])
    ciaddr = _bytes_to_ip(data[12:16])
    yiaddr = _bytes_to_ip(data[16:20])
    siaddr = _bytes_to_ip(data[20:24])
    giaddr = _bytes_to_ip(data[24:28])
    chaddr = data[28:44]
    sname = data[44:108]
    file = data[108:236]
    rest = data[236:]
    if rest[:4] == DHCP_MAGIC:
        options = _parse_options(rest[4:])
    else:
        options = {}
    return DhcpPacket(
        op=op,
        htype=htype,
        hlen=hlen,
        hops=hops,
        xid=xid,
        secs=secs,
        flags=flags,
        ciaddr=ciaddr,
        yiaddr=yiaddr,
        siaddr=siaddr,
        giaddr=giaddr,
        chaddr=chaddr,
        sname=sname,
        file=file,
        options=options,
    )


def _iter_pool(start: str, end: str) -> list[str]:
    a = ipaddress.IPv4Address(start)
    b = ipaddress.IPv4Address(end)
    if int(b) < int(a):
        a, b = b, a
    return [str(ipaddress.IPv4Address(i)) for i in range(int(a), int(b) + 1)]


@dataclass
class Lease:
    mac: str
    ip: str
    hostname: str = ""
    expires: float = 0.0
    last_seen: float = 0.0
    ipxe: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "mac": self.mac,
            "ip": self.ip,
            "hostname": self.hostname,
            "expires": int(self.expires),
            "last_seen": int(self.last_seen),
            "ipxe": self.ipxe,
        }


@dataclass
class PxeNetConfig:
    interface: str
    range_start: str
    range_end: str
    netmask: str = "255.255.255.0"
    gateway: str = ""
    dns: str = ""
    next_server: str = ""
    http_port: int = 8088
    dhcp_port: int = 67
    http_bind: str = ""
    root: Path = field(default_factory=lambda: Path("/opt/genestack-console/data/pxe"))
    reservations: dict[str, tuple[str, str]] = field(default_factory=dict)
    lease_seconds: int = 3600

    @classmethod
    def from_pxe_cfg(
        cls,
        pxe_cfg: dict[str, Any],
        nodes: list[dict[str, Any]],
        root: Path,
        *,
        dhcp_port: int = 67,
    ) -> PxeNetConfig:
        reservations: dict[str, tuple[str, str]] = {}
        for node in nodes:
            mac = str(node.get("pxe_mac") or "").strip().lower()
            ip = str(node.get("expected_ip") or node.get("ip") or "").strip()
            name = str(node.get("name") or "").strip()
            if mac and ip:
                reservations[mac] = (ip, name)
        next_server = str(
            pxe_cfg.get("next_server") or pxe_cfg.get("gateway") or ""
        ).strip()
        http_port = int(pxe_cfg.get("http_port") or 8088)
        return cls(
            interface=str(pxe_cfg.get("interface") or "").strip(),
            range_start=str(pxe_cfg.get("range_start") or "").strip(),
            range_end=str(pxe_cfg.get("range_end") or "").strip(),
            netmask=str(pxe_cfg.get("netmask") or "255.255.255.0").strip(),
            gateway=str(pxe_cfg.get("gateway") or "").strip(),
            dns=str(pxe_cfg.get("dns") or pxe_cfg.get("gateway") or "").strip(),
            next_server=next_server,
            http_port=http_port,
            dhcp_port=int(pxe_cfg.get("dhcp_port") or dhcp_port),
            http_bind=str(pxe_cfg.get("http_bind") or next_server or "0.0.0.0").strip(),
            root=root,
            reservations=reservations,
        )


class _PxeHttpHandler(SimpleHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        log.info("pxe-http " + fmt, *args)


class PxeHttpServer:
    def __init__(self, bind: str, port: int, root: Path) -> None:
        self.bind = bind
        self.port = port
        self.root = root
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.error: str | None = None

    @property
    def running(self) -> bool:
        return (
            self._httpd is not None
            and self._thread is not None
            and self._thread.is_alive()
        )

    def start(self) -> None:
        root = self.root

        class Handler(_PxeHttpHandler):
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                super().__init__(*args, directory=str(root), **kwargs)

        try:
            self._httpd = ThreadingHTTPServer((self.bind, self.port), Handler)
            self._httpd.daemon_threads = True
        except OSError as exc:
            self.error = f"http bind {self.bind}:{self.port}: {exc}"
            log.warning("pxe http: %s", self.error)
            self._httpd = None
            return
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            name=f"pxe-http-{self.port}",
            daemon=True,
        )
        self._thread.start()
        log.info("pxe http listening on %s:%s root=%s", self.bind, self.port, self.root)

    def stop(self) -> None:
        if self._httpd is not None:
            with threading.Lock():
                try:
                    self._httpd.shutdown()
                except Exception:
                    pass
                try:
                    self._httpd.server_close()
                except Exception:
                    pass
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None


class DhcpServer:
    def __init__(self, cfg: PxeNetConfig) -> None:
        self.cfg = cfg
        self.leases: dict[str, Lease] = {}
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.error: str | None = None
        self._pool = (
            _iter_pool(cfg.range_start, cfg.range_end)
            if cfg.range_start and cfg.range_end
            else []
        )
        self._reserved_ips = {ip for ip, _ in cfg.reservations.values()}
        self._lock = threading.Lock()
        self._load_leases()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _leases_path(self) -> Path:
        return self.cfg.root / "leases.json"

    def _load_leases(self) -> None:
        path = self._leases_path()
        if not path.is_file():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        now = time.time()
        for item in raw if isinstance(raw, list) else []:
            if not isinstance(item, dict):
                continue
            mac = str(item.get("mac") or "").lower()
            ip = str(item.get("ip") or "")
            if not mac or not ip:
                continue
            expires = float(item.get("expires") or 0)
            if expires and expires < now:
                continue
            self.leases[mac] = Lease(
                mac=mac,
                ip=ip,
                hostname=str(item.get("hostname") or ""),
                expires=expires,
                last_seen=float(item.get("last_seen") or 0),
                ipxe=bool(item.get("ipxe")),
            )

    def _save_leases(self) -> None:
        path = self._leases_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    [lease.as_dict() for lease in self.leases.values()], indent=2
                ),
                encoding="utf-8",
            )
        except OSError as exc:
            log.warning("pxe dhcp: could not persist leases: %s", exc)

    def _allocate(self, mac: str, hostname: str) -> str | None:
        mac = mac.lower()
        reserved = self.cfg.reservations.get(mac)
        if reserved:
            return reserved[0]
        existing = self.leases.get(mac)
        now = time.time()
        if existing and (not existing.expires or existing.expires > now):
            return existing.ip
        used = {
            lease.ip
            for lease in self.leases.values()
            if not lease.expires or lease.expires > now
        }
        used |= self._reserved_ips
        for ip in self._pool:
            if ip not in used:
                return ip
        return None

    def _boot_file(self, ipxe: bool) -> str:
        next_server = self.cfg.next_server or self.cfg.gateway
        if ipxe and next_server:
            return f"http://{next_server}:{self.cfg.http_port}/boot.ipxe"
        return "undionly.kpxe"

    def _reply(self, req: DhcpPacket, msg_type: int, yiaddr: str) -> DhcpPacket:
        ipxe = req.is_ipxe
        boot = self._boot_file(ipxe)
        server = self.cfg.next_server or self.cfg.gateway or "0.0.0.0"
        opts: dict[int, bytes] = {
            OPT_MSGTYPE: bytes([msg_type]),
            OPT_SERVER_ID: (
                _ip_to_bytes(server) if server != "0.0.0.0" else _ip_to_bytes("0.0.0.0")
            ),
            OPT_LEASE: struct.pack("!I", self.cfg.lease_seconds),
            OPT_SUBNET: _ip_to_bytes(self.cfg.netmask),
            OPT_BOOTFILE: boot.encode("ascii", "replace"),
        }
        if self.cfg.gateway:
            opts[OPT_ROUTER] = _ip_to_bytes(self.cfg.gateway)
        if self.cfg.dns:
            opts[OPT_DNS] = _ip_to_bytes(self.cfg.dns)
        if server and server != "0.0.0.0":
            opts[OPT_TFTP] = server.encode("ascii", "replace")
        file_field = boot.encode("ascii", "replace")[:127]
        return DhcpPacket(
            op=BOOTREPLY,
            htype=1,
            hlen=6,
            hops=0,
            xid=req.xid,
            secs=0,
            flags=req.flags,
            ciaddr="0.0.0.0",
            yiaddr=yiaddr,
            siaddr=server if server != "0.0.0.0" else "0.0.0.0",
            giaddr=req.giaddr,
            chaddr=req.chaddr,
            sname=b"",
            file=file_field,
            options=opts,
        )

    def handle(self, data: bytes, addr: tuple[str, int]) -> bytes | None:
        req = parse_dhcp(data)
        if req is None or req.op != BOOTREQUEST:
            return None
        msg = req.msg_type
        if msg not in (DHCPDISCOVER, DHCPREQUEST):
            return None
        mac = req.mac.lower()
        hostname = ""
        reserved = self.cfg.reservations.get(mac)
        if reserved:
            hostname = reserved[1]
        yiaddr = self._allocate(mac, hostname)
        if not yiaddr:
            log.warning("pxe dhcp: pool exhausted for mac=%s", mac)
            return None
        if msg == DHCPREQUEST:
            requested = req.options.get(OPT_REQUESTED_IP)
            if requested and len(requested) == 4 and _bytes_to_ip(requested) != yiaddr:
                nak = self._reply(req, DHCPNAK, "0.0.0.0")
                nak.yiaddr = "0.0.0.0"
                return nak.to_bytes()
            now = time.time()
            with self._lock:
                self.leases[mac] = Lease(
                    mac=mac,
                    ip=yiaddr,
                    hostname=hostname,
                    expires=now + self.cfg.lease_seconds,
                    last_seen=now,
                    ipxe=req.is_ipxe,
                )
                self._save_leases()
            log.info("pxe dhcp ACK mac=%s ip=%s ipxe=%s", mac, yiaddr, req.is_ipxe)
            return self._reply(req, DHCPACK, yiaddr).to_bytes()
        log.info("pxe dhcp OFFER mac=%s ip=%s", mac, yiaddr)
        return self._reply(req, DHCPOFFER, yiaddr).to_bytes()

    def start(self) -> None:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            if self.cfg.interface:
                try:
                    sock.setsockopt(
                        socket.SOL_SOCKET,
                        SO_BINDTODEVICE,
                        self.cfg.interface.encode("ascii") + b"\x00",
                    )
                except OSError as exc:
                    log.warning(
                        "pxe dhcp: SO_BINDTODEVICE %s failed (%s); binding 0.0.0.0",
                        self.cfg.interface,
                        exc,
                    )
            sock.bind(("0.0.0.0", self.cfg.dhcp_port))
            sock.settimeout(0.5)
        except OSError as exc:
            self.error = f"dhcp bind :{self.cfg.dhcp_port}: {exc}"
            log.warning("pxe dhcp: %s", self.error)
            return
        self._sock = sock
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name=f"pxe-dhcp-{self.cfg.dhcp_port}", daemon=True
        )
        self._thread.start()
        log.info(
            "pxe dhcp listening on 0.0.0.0:%s iface=%s pool=%s-%s",
            self.cfg.dhcp_port,
            self.cfg.interface or "*",
            self.cfg.range_start,
            self.cfg.range_end,
        )

    def _loop(self) -> None:
        assert self._sock is not None
        while not self._stop.is_set():
            try:
                data, addr = self._sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                if self._stop.is_set():
                    break
                continue
            try:
                reply = self.handle(data, addr)
            except Exception:
                log.exception("pxe dhcp: handler failed")
                continue
            if not reply:
                continue
            dest = ("255.255.255.255", 68)
            try:
                self._sock.sendto(reply, dest)
            except OSError as exc:
                log.warning("pxe dhcp: sendto failed: %s", exc)

    def stop(self) -> None:
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None


class PxeRuntime:
    """One DHCP+HTTP pair for a provisioning interface."""

    def __init__(self, cfg: PxeNetConfig) -> None:
        self.cfg = cfg
        self.http = PxeHttpServer(cfg.http_bind or "0.0.0.0", cfg.http_port, cfg.root)
        self.dhcp = DhcpServer(cfg)

    def start(self) -> dict[str, Any]:
        self.cfg.root.mkdir(parents=True, exist_ok=True)
        self.http.start()
        self.dhcp.start()
        return self.status()

    def stop(self) -> None:
        self.http.stop()
        self.dhcp.stop()

    def status(self) -> dict[str, Any]:
        return {
            "interface": self.cfg.interface,
            "next_server": self.cfg.next_server,
            "http_bind": self.cfg.http_bind,
            "http_port": self.cfg.http_port,
            "dhcp_port": self.cfg.dhcp_port,
            "http_running": self.http.running,
            "dhcp_running": self.dhcp.running,
            "http_error": self.http.error,
            "dhcp_error": self.dhcp.error,
            "root": str(self.cfg.root),
            "leases": [lease.as_dict() for lease in self.dhcp.leases.values()],
            "reservations": {
                mac: {"ip": ip, "name": name}
                for mac, (ip, name) in self.cfg.reservations.items()
            },
        }


class PxeManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._runtimes: dict[str, PxeRuntime] = {}

    def apply(self, cfg: PxeNetConfig) -> dict[str, Any]:
        key = cfg.interface or cfg.next_server or "default"
        with self._lock:
            existing = self._runtimes.pop(key, None)
            if existing is not None:
                existing.stop()
            runtime = PxeRuntime(cfg)
            self._runtimes[key] = runtime
            return runtime.start()

    def stop(self, key: str | None = None) -> None:
        with self._lock:
            if key is None:
                items = list(self._runtimes.items())
                self._runtimes.clear()
            else:
                rt = self._runtimes.pop(key, None)
                items = [(key, rt)] if rt is not None else []
        for _, runtime in items:
            if runtime is not None:
                runtime.stop()

    def status(self) -> dict[str, Any]:
        with self._lock:
            runtimes = {k: v.status() for k, v in self._runtimes.items()}
        any_http = any(item.get("http_running") for item in runtimes.values())
        any_dhcp = any(item.get("dhcp_running") for item in runtimes.values())
        leases: list[dict[str, Any]] = []
        for item in runtimes.values():
            leases.extend(item.get("leases") or [])
        return {
            "running": any_http or any_dhcp,
            "http_running": any_http,
            "dhcp_running": any_dhcp,
            "sidecar": False,
            "runtimes": runtimes,
            "leases": leases,
        }


_MANAGER: PxeManager | None = None
_MANAGER_LOCK = threading.Lock()


def get_manager() -> PxeManager:
    global _MANAGER
    with _MANAGER_LOCK:
        if _MANAGER is None:
            _MANAGER = PxeManager()
        return _MANAGER


def apply_pxe_cfg(
    pxe_cfg: dict[str, Any],
    nodes: list[dict[str, Any]],
    root: Path,
    *,
    dhcp_port: int = 67,
) -> dict[str, Any]:
    cfg = PxeNetConfig.from_pxe_cfg(pxe_cfg, nodes, root, dhcp_port=dhcp_port)
    return get_manager().apply(cfg)


def start_from_db() -> dict[str, Any]:
    """Apply PXE runtime from the current env-config docs (API process boot)."""
    from app.config import get_settings
    from app.db import SessionLocal
    from app.models import Environment
    from app.services import envconfig as envconfig_service
    from app.services.pxe import _baremetal_nodes

    settings = get_settings()
    db = SessionLocal()
    applied: list[dict[str, Any]] = []
    try:
        envs = db.query(Environment).all()
        for env in envs:
            current = envconfig_service.get_current(db, env)
            doc = current[0] if current else {}
            pxe_cfg = doc.get("pxe") if isinstance(doc, dict) else None
            if not isinstance(pxe_cfg, dict) or not pxe_cfg:
                continue
            root = Path(settings.data_dir) / "pxe"
            nodes = _baremetal_nodes(doc)
            result = apply_pxe_cfg(pxe_cfg, nodes, root)
            applied.append({"environment": env.name, **result})
            log.info(
                "pxe runtime applied for env=%s dhcp=%s http=%s",
                env.name,
                result.get("dhcp_running"),
                result.get("http_running"),
            )
    except Exception:
        log.exception("pxe runtime start_from_db failed (non-fatal)")
    finally:
        db.close()
    return {"applied": applied, **get_manager().status()}


def stop_all() -> None:
    get_manager().stop()
