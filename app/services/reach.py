"""How this deploy host reaches an environment.

Three paths, all off until an operator turns them on:

* WireGuard. This host is the server. Peers get an address in the
  configured network. The client config, including the peer private key,
  is returned once.
* Tailscale. ``tailscale up`` joins the tailnet. Each environment stores
  the address the console should use. This host does not invent it.
* Cloudflare Tunnel. ``cloudflared tunnel run`` uses a token saved here.
  An environment stores the hostname that site published.

Apply is explicit. Nothing starts when the process starts. A missing
program is a status, and the config file is still written. Secrets are
Fernet-encrypted. Reads do not return them.

``tool_path``, ``run_command``, ``spawn_process``, and ``stop_process``
are module globals so tests can replace them.
"""

from __future__ import annotations

import base64
import ipaddress
import os
import re
import shutil
import signal
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, _is_placeholder, get_settings
from app.models import AgentCredential, Environment, ReachHub, ReachLink
from app.services.crypto import decrypt_secret, encrypt_secret
from app.services.env_field_guards import refuse_dangerous_ssh_host

KINDS = ("wireguard", "tailscale", "cloudflare")

_IFACE_RE = re.compile(r"^[a-zA-Z0-9_-]{1,15}$")
_LABEL_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
_HOST_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def tool_path(name: str) -> str | None:
    return shutil.which(name)


def run_command(argv: list[str]) -> tuple[int, str]:
    """Run a short command. The caller must not log argv when it holds a key."""
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def spawn_process(argv: list[str], env: dict[str, str] | None = None) -> int:
    """Start a long-running process in its own session. Returns the pid."""
    proc = subprocess.Popen(
        argv,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return proc.pid


def stop_process(pid: int) -> None:
    """Stop a pid this console recorded. Ignore a pid that is already gone."""
    if not pid:
        return
    try:
        os.kill(int(pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        return


def generate_keypair() -> tuple[str, str]:
    """Return ``(private, public)`` as standard base64, matching ``wg genkey``."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

    private = X25519PrivateKey.generate()
    raw_private = private.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    raw_public = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return base64.b64encode(raw_private).decode(), base64.b64encode(raw_public).decode()


def public_from_private(private_b64: str) -> str:
    """Derive the WireGuard public key. Raises ValueError on a bad key."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

    raw = _b64_32(private_b64)
    private = X25519PrivateKey.from_private_bytes(raw)
    public = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return base64.b64encode(public).decode()


def parse_network(cidr: str) -> ipaddress.IPv4Network:
    try:
        net = ipaddress.ip_network(str(cidr).strip(), strict=False)
    except ValueError as exc:
        raise ValueError("wireguard network must be an IPv4 CIDR") from exc
    if not isinstance(net, ipaddress.IPv4Network):
        raise ValueError("wireguard network must be an IPv4 CIDR")
    if net.prefixlen > 30:
        raise ValueError("wireguard network must leave room for a hub and a peer")
    return net


def hub_address(network: str) -> str:
    """Hub address with the network prefix. ``10.67.67.0/24`` -> ``10.67.67.1/24``."""
    net = parse_network(network)
    host = next(net.hosts())
    return f"{host}/{net.prefixlen}"


def allocate_peer_address(network: str, used: set[str]) -> str:
    """Lowest free host after the hub, as a /32. A freed address is reused."""
    net = parse_network(network)
    taken = {str(item).split("/")[0] for item in used if item}
    first = True
    for host in net.hosts():
        if first:
            first = False
            continue
        if str(host) not in taken:
            return f"{host}/32"
    raise ValueError("wireguard network has no free addresses")


def get_or_seed_hub(db: Session, kind: str, settings: Settings | None = None) -> ReachHub:
    """Return the hub row. The first call copies config.yaml. Later calls keep the row."""
    _require_kind(kind)
    row = db.get(ReachHub, kind)
    if row is not None:
        return row
    settings = settings or get_settings()
    row = ReachHub(kind=kind, enabled=False, status="idle")
    if kind == "wireguard":
        row.enabled = bool(settings.wg_enabled)
        row.network = settings.wg_network or "10.67.67.0/24"
        row.listen_port = settings.wg_listen_port or 51820
        row.endpoint = settings.wg_endpoint or None
        iface = (settings.wg_interface or "wg-gsc").strip() or "wg-gsc"
        row.iface = iface if _IFACE_RE.fullmatch(iface) else "wg-gsc"
        if row.enabled:
            _ensure_wg_material(row, settings)
    elif kind == "tailscale":
        row.enabled = bool(settings.ts_enabled)
        try:
            row.hostname = _tailscale_hostname(settings.ts_hostname)
        except ValueError:
            row.hostname = "genestack-console"
        _store_bootstrap_secret(row, settings.ts_auth_key, settings)
    else:
        row.enabled = bool(settings.cf_enabled)
        row.hostname = settings.cf_hostname or None
        _store_bootstrap_secret(row, settings.cf_tunnel_token, settings)
    _touch(row)
    db.add(row)
    db.flush()
    return row


def hub_view(row: ReachHub) -> dict:
    return {
        "kind": row.kind,
        "enabled": bool(row.enabled),
        "status": row.status,
        "detail": row.detail,
        "address": row.address,
        "public_key": row.public_key,
        "listen_port": row.listen_port,
        "endpoint": row.endpoint,
        "network": row.network,
        "interface": row.iface,
        "hostname": row.hostname,
        "secret_configured": bool(row.secret_encrypted),
        "config_path": row.config_path,
        "pid": row.pid,
        "updated_at": row.updated_at,
    }


def update_hub(db: Session, kind: str, fields: dict) -> ReachHub:
    row = get_or_seed_hub(db, kind)
    if kind == "wireguard":
        _update_wireguard(db, row, fields)
    elif kind == "tailscale":
        _update_tailscale(row, fields)
    else:
        _update_cloudflare(row, fields)
    _touch(row)
    db.flush()
    return row


def apply_hub(db: Session, kind: str) -> ReachHub:
    row = get_or_seed_hub(db, kind)
    if not row.enabled:
        raise ValueError(f"turn {kind} on before applying it")
    if kind == "wireguard":
        _apply_wireguard(db, row)
    elif kind == "tailscale":
        _apply_tailscale(row)
    else:
        _apply_cloudflare(row)
    _touch(row)
    db.flush()
    return row


def stop_hub(db: Session, kind: str) -> ReachHub:
    row = get_or_seed_hub(db, kind)
    if kind == "tailscale":
        raise ValueError("tailscale is left up; disconnect it on the host if you need to")
    if kind == "wireguard":
        _stop_wireguard(row)
    else:
        if row.pid:
            stop_process(row.pid)
        row.pid = None
        row.status = "idle"
        row.detail = "cloudflared stopped"
    _touch(row)
    db.flush()
    return row


def list_links(db: Session, environment_id: str) -> list[ReachLink]:
    stmt = (
        select(ReachLink)
        .where(ReachLink.environment_id == environment_id)
        .order_by(ReachLink.kind, ReachLink.name)
    )
    return list(db.scalars(stmt).all())


def link_view(row: ReachLink) -> dict:
    return {
        "id": row.id,
        "environment_id": row.environment_id,
        "kind": row.kind,
        "name": row.name,
        "address": row.address,
        "public_key": row.public_key,
        "local_port": row.local_port,
        "use_for_ssh": bool(row.use_for_ssh),
        "status": row.status,
        "detail": row.detail,
        "pid": row.pid,
        "created_at": row.created_at,
    }


def upsert_link(
    db: Session,
    env: Environment,
    kind: str,
    *,
    name: str = "default",
    address: str | None = None,
    local_port: int | None = None,
    use_for_ssh: bool = False,
) -> tuple[ReachLink, str | None]:
    """Create or replace one link. The WireGuard client config is returned once."""
    _require_kind(kind)
    link_name = _clean_name(name)
    if kind == "wireguard":
        link, client_config = _mint_wireguard_peer(
            db, environment_id=env.id, name=link_name
        )
    else:
        host = _clean_host(address, what="address")
        port = _clean_port(local_port) if local_port is not None else None
        if kind == "cloudflare" and local_port is not None:
            port = _clean_port(local_port)
        existing = _find_link(db, env.id, kind, link_name)
        if existing is not None:
            if existing.pid:
                stop_process(existing.pid)
            db.delete(existing)
            db.flush()
        link = ReachLink(
            environment_id=env.id,
            kind=kind,
            name=link_name,
            address=host,
            local_port=port,
            use_for_ssh=bool(use_for_ssh),
            status="ready",
        )
        db.add(link)
        client_config = None
    link.use_for_ssh = bool(use_for_ssh)
    note = _maybe_set_deploy_host(env, link.address, use_for_ssh)
    if note:
        link.detail = note
    _touch(link)
    db.flush()
    return link, client_config


def delete_link(db: Session, environment_id: str, kind: str, name: str) -> None:
    _require_kind(kind)
    link = _find_link(db, environment_id, kind, _clean_name(name))
    if link is None:
        raise LookupError("reach link not found")
    if link.pid:
        stop_process(link.pid)
    if kind == "wireguard":
        cred = _credential(db, environment_id, link.name)
        if cred is not None:
            cred.wg_address = None
            cred.wg_public_key = None
            cred.wg_private_key_encrypted = None
    db.delete(link)
    db.flush()


def forward_link(db: Session, environment_id: str, name: str) -> ReachLink:
    """Start ``cloudflared access tcp`` for a saved hostname. Records the pid."""
    link = _find_link(db, environment_id, "cloudflare", _clean_name(name))
    if link is None:
        raise LookupError("reach link not found")
    if not link.address or not link.local_port:
        raise ValueError("save a hostname and a local port before forwarding")
    if tool_path("cloudflared") is None:
        link.status = "tool_missing"
        link.detail = "cloudflared is not installed on this deploy host"
        _touch(link)
        db.flush()
        return link
    if link.pid:
        stop_process(link.pid)
    argv = [
        "cloudflared",
        "access",
        "tcp",
        "--hostname",
        link.address,
        "--url",
        f"127.0.0.1:{link.local_port}",
    ]
    link.pid = spawn_process(argv)
    link.status = "applied"
    link.detail = f"forwarding 127.0.0.1:{link.local_port} to {link.address}"
    _touch(link)
    db.flush()
    return link


def attach_credential_peer(db: Session, cred: AgentCredential) -> str | None:
    """Mint a WireGuard peer for this credential when the hub is on.

    Returns the client config once, or None when WireGuard is off. Default
    installs leave it off, so enrollment does not change. Once a hub row
    exists, that row decides. The config file does not turn it back on.
    """
    settings = get_settings()
    hub = db.get(ReachHub, "wireguard")
    # A real hub row decides. Anything else (no row, or a stand-in session
    # that does not return ReachHub) leaves enrollment on the dial-out path.
    if not isinstance(hub, ReachHub):
        hub = None
    if hub is None:
        if not settings.wg_enabled:
            return None
    elif not hub.enabled:
        return None
    _link, client_config = _mint_wireguard_peer(
        db, environment_id=cred.environment_id, name=cred.name, credential=cred
    )
    return client_config


def _mint_wireguard_peer(
    db: Session,
    *,
    environment_id: str,
    name: str,
    credential: AgentCredential | None = None,
) -> tuple[ReachLink, str]:
    settings = get_settings()
    hub = get_or_seed_hub(db, "wireguard", settings)
    if not hub.enabled:
        raise ValueError("turn wireguard on before adding a peer")
    _ensure_wg_material(hub, settings)
    existing = _find_link(db, environment_id, "wireguard", name)
    kept = _kept_address(existing, hub) if existing is not None else None
    if existing is not None:
        db.delete(existing)
        db.flush()
    address = kept or allocate_peer_address(hub.network or "", _used_peer_ips(db))
    private, public = generate_keypair()
    encrypted = encrypt_secret(private)
    link = ReachLink(
        environment_id=environment_id,
        kind="wireguard",
        name=name,
        address=address,
        public_key=public,
        secret_encrypted=encrypted,
        status="ready",
    )
    db.add(link)
    cred = credential if credential is not None else _credential(db, environment_id, name)
    if cred is not None:
        cred.wg_address = address
        cred.wg_public_key = public
        cred.wg_private_key_encrypted = encrypted
    client = _client_config(
        private_key=private,
        address=address,
        hub_public=hub.public_key or "",
        network=hub.network or "",
        endpoint=hub.endpoint or "",
    )
    db.flush()
    return link, client


def _update_wireguard(db: Session, row: ReachHub, fields: dict) -> None:
    if "network" in fields and fields["network"] is not None:
        net = parse_network(str(fields["network"]))
        new = str(net)
        if row.network and new != str(parse_network(row.network)) and _peer_count(db):
            raise ValueError("wireguard network cannot change while peers exist")
        row.network = new
        row.address = hub_address(new)
    if "interface" in fields and fields["interface"] is not None:
        name = str(fields["interface"]).strip()
        if not _IFACE_RE.fullmatch(name):
            raise ValueError(
                "interface must be 1-15 letters, digits, underscore, or hyphen"
            )
        row.iface = name
    if "listen_port" in fields and fields["listen_port"] is not None:
        row.listen_port = _clean_port(fields["listen_port"])
    if "endpoint" in fields:
        row.endpoint = _clean_endpoint(fields.get("endpoint") or "") or None
    if "enabled" in fields and fields["enabled"] is not None:
        row.enabled = bool(fields["enabled"])
    if "secret" in fields and fields["secret"]:
        private = str(fields["secret"]).strip()
        if _is_placeholder(private):
            raise ValueError("secret is a placeholder; leave it blank to keep the saved value")
        row.public_key = public_from_private(private)
        row.secret_encrypted = encrypt_secret(private)
    if row.enabled:
        _ensure_wg_material(row)


def _update_tailscale(row: ReachHub, fields: dict) -> None:
    if "hostname" in fields and fields["hostname"] is not None:
        row.hostname = _tailscale_hostname(str(fields["hostname"]))
    if "enabled" in fields and fields["enabled"] is not None:
        row.enabled = bool(fields["enabled"])
    _take_secret(row, fields)


def _update_cloudflare(row: ReachHub, fields: dict) -> None:
    if "hostname" in fields and fields["hostname"] is not None:
        text = str(fields["hostname"]).strip()
        row.hostname = _clean_host(text, what="hostname") if text else None
    if "enabled" in fields and fields["enabled"] is not None:
        row.enabled = bool(fields["enabled"])
    _take_secret(row, fields)


def _take_secret(row: ReachHub, fields: dict) -> None:
    if "secret" not in fields or not fields["secret"]:
        return
    raw = str(fields["secret"]).strip()
    if _is_placeholder(raw):
        raise ValueError("secret is a placeholder; leave it blank to keep the saved value")
    if len(raw) > 4096:
        raise ValueError("secret is too long")
    row.secret_encrypted = encrypt_secret(raw)


def _apply_wireguard(db: Session, row: ReachHub) -> None:
    _ensure_wg_material(row)
    private = decrypt_secret(row.secret_encrypted) or ""
    peers = list(
        db.scalars(select(ReachLink).where(ReachLink.kind == "wireguard")).all()
    )
    text = _hub_config(row, peers, private)
    path = _write_secret_file(_wg_path(row), text)
    row.config_path = str(path)
    endpoint_note = ""
    if not (row.endpoint or "").strip():
        endpoint_note = "peers cannot dial in until endpoint is set"
    if tool_path("wg-quick") is None:
        row.status = "tool_missing"
        row.detail = "wg-quick is not installed; the config file was written"
        if endpoint_note:
            row.detail = f"{row.detail}; {endpoint_note}"
        return
    iface = row.iface or "wg-gsc"
    run_command(["wg-quick", "down", iface])
    rc, _out = run_command(["wg-quick", "up", str(path)])
    if rc != 0:
        row.status = "error"
        row.detail = "wg-quick up failed"
        return
    row.status = "applied"
    row.detail = "wireguard is up"
    if endpoint_note:
        row.detail = f"wireguard is up; {endpoint_note}"


def _stop_wireguard(row: ReachHub) -> None:
    if tool_path("wg-quick") is None:
        row.status = "tool_missing"
        row.detail = "wg-quick is not installed"
        return
    run_command(["wg-quick", "down", row.iface or "wg-gsc"])
    row.status = "idle"
    row.detail = "wireguard stopped"


def _apply_tailscale(row: ReachHub) -> None:
    if tool_path("tailscale") is None:
        row.status = "tool_missing"
        row.detail = "tailscale is not installed on this deploy host"
        return
    hostname = row.hostname or "genestack-console"
    argv = ["tailscale", "up", "--hostname", hostname, "--timeout", "15s"]
    auth = decrypt_secret(row.secret_encrypted) if row.secret_encrypted else ""
    if auth:
        # tailscale up takes the key as an argument. It is not written to the
        # API response or to a log line.
        argv.extend(["--auth-key", auth])
    rc, _out = run_command(argv)
    if rc != 0:
        row.status = "error"
        row.detail = "tailscale up failed"
        return
    ip_rc, ip_out = run_command(["tailscale", "ip", "-4"])
    if ip_rc == 0:
        first = ""
        for line in ip_out.splitlines():
            line = line.strip()
            if line:
                first = line
                break
        if first and _HOST_RE.fullmatch(first):
            row.address = first
    row.status = "applied"
    row.detail = "tailscale is up"


def _apply_cloudflare(row: ReachHub) -> None:
    token = decrypt_secret(row.secret_encrypted) if row.secret_encrypted else ""
    if not token:
        raise ValueError("save a tunnel token before applying cloudflare")
    path = _write_secret_file(_cf_token_path(), token + "\n")
    row.config_path = str(path)
    if tool_path("cloudflared") is None:
        row.status = "tool_missing"
        row.detail = "cloudflared is not installed; the token file was written"
        return
    if row.pid:
        stop_process(row.pid)
        row.pid = None
    env = os.environ.copy()
    env["TUNNEL_TOKEN"] = token
    row.pid = spawn_process(["cloudflared", "tunnel", "run"], env)
    row.status = "applied"
    row.detail = "cloudflared tunnel run started"


def _ensure_wg_material(row: ReachHub, settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    if not row.network:
        row.network = settings.wg_network or "10.67.67.0/24"
    row.network = str(parse_network(row.network))
    if not row.iface or not _IFACE_RE.fullmatch(row.iface):
        row.iface = "wg-gsc"
    if not row.listen_port:
        row.listen_port = settings.wg_listen_port or 51820
    if not row.secret_encrypted:
        private = (settings.wg_private_key or "").strip()
        if not private or _is_placeholder(private):
            private, public = generate_keypair()
        else:
            public = public_from_private(private)
        row.secret_encrypted = encrypt_secret(private, settings)
        row.public_key = public
    if not row.address:
        row.address = hub_address(row.network)


def _store_bootstrap_secret(row: ReachHub, value: str, settings: Settings) -> None:
    text = (value or "").strip()
    if not text or _is_placeholder(text):
        return
    row.secret_encrypted = encrypt_secret(text, settings)


def _hub_config(hub: ReachHub, peers: list[ReachLink], private_key: str) -> str:
    lines = [
        "[Interface]",
        f"PrivateKey = {private_key}",
        f"Address = {hub.address}",
        f"ListenPort = {hub.listen_port}",
        "",
    ]
    for peer in peers:
        if not peer.public_key or not peer.address:
            continue
        ip = peer.address.split("/")[0]
        lines.extend(
            [
                "[Peer]",
                f"PublicKey = {peer.public_key}",
                f"AllowedIPs = {ip}/32",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def _client_config(
    *,
    private_key: str,
    address: str,
    hub_public: str,
    network: str,
    endpoint: str,
) -> str:
    lines = [
        "[Interface]",
        f"PrivateKey = {private_key}",
        f"Address = {address}",
        "",
        "[Peer]",
        f"PublicKey = {hub_public}",
        f"AllowedIPs = {network}",
        "PersistentKeepalive = 25",
    ]
    if endpoint:
        lines.append(f"Endpoint = {endpoint}")
    return "\n".join(lines).rstrip() + "\n"


def _used_peer_ips(db: Session) -> set[str]:
    used: set[str] = set()
    links = db.scalars(select(ReachLink).where(ReachLink.kind == "wireguard")).all()
    for link in links:
        if link.address:
            used.add(link.address.split("/")[0])
    creds = db.scalars(
        select(AgentCredential).where(AgentCredential.wg_address.is_not(None))
    ).all()
    for cred in creds:
        if cred.wg_address:
            used.add(cred.wg_address.split("/")[0])
    return used


def _kept_address(existing: ReachLink, hub: ReachHub) -> str | None:
    if not existing.address or not hub.network or not hub.address:
        return None
    ip = existing.address.split("/")[0]
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return None
    net = parse_network(hub.network)
    hub_ip = hub.address.split("/")[0]
    if addr in net and str(addr) != hub_ip:
        return f"{addr}/32"
    return None


def _peer_count(db: Session) -> int:
    stmt = select(ReachLink.id).where(ReachLink.kind == "wireguard")
    return len(list(db.scalars(stmt).all()))


def _find_link(db: Session, environment_id: str, kind: str, name: str) -> ReachLink | None:
    stmt = select(ReachLink).where(
        ReachLink.environment_id == environment_id,
        ReachLink.kind == kind,
        ReachLink.name == name,
    )
    return db.scalars(stmt).first()


def _credential(db: Session, environment_id: str, name: str) -> AgentCredential | None:
    stmt = select(AgentCredential).where(
        AgentCredential.environment_id == environment_id,
        AgentCredential.name == name,
    )
    return db.scalars(stmt).first()


def _maybe_set_deploy_host(env: Environment, address: str | None, use: bool) -> str | None:
    if not use or not address:
        return None
    if (env.deployer_ssh_host or "").strip():
        return "deploy host already set; left it unchanged"
    host = address.split("/")[0].strip()
    try:
        env.deployer_ssh_host = refuse_dangerous_ssh_host(host)
    except ValueError:
        return "address was not saved as the deploy host"
    return None


def _wg_path(row: ReachHub) -> Path:
    iface = row.iface or "wg-gsc"
    return get_settings().data_dir / "wireguard" / f"{iface}.conf"


def _cf_token_path() -> Path:
    return get_settings().data_dir / "cloudflared" / "tunnel.token"


def _write_secret_file(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    path.write_text(text, encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path


def _require_kind(kind: str) -> str:
    if kind not in KINDS:
        raise ValueError("kind must be wireguard, tailscale, or cloudflare")
    return kind


def _clean_name(name: str | None) -> str:
    text = (name or "default").strip() or "default"
    if not _NAME_RE.fullmatch(text):
        raise ValueError("name must be letters, digits, dot, underscore, or hyphen")
    return text


def _clean_host(value: str | None, *, what: str) -> str:
    text = (value or "").strip()
    if not text or not _HOST_RE.fullmatch(text):
        raise ValueError(f"{what} must be a hostname or IP address")
    return text


def _clean_port(value: object) -> int:
    try:
        port = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValueError("port must be 1-65535") from exc
    if port < 1 or port > 65535:
        raise ValueError("port must be 1-65535")
    return port


def _clean_endpoint(value: str) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    if len(text) > 253 or any(ch.isspace() for ch in text):
        raise ValueError("endpoint must be a host or host:port")
    if any(ch in text for ch in ";|&$`<>\\"):
        raise ValueError("endpoint must be a host or host:port")
    return text


def _tailscale_hostname(value: str) -> str:
    text = (value or "").strip() or "genestack-console"
    if not _LABEL_RE.fullmatch(text):
        raise ValueError("tailscale hostname must be a DNS label")
    return text


def _b64_32(text: str) -> bytes:
    padded = text.strip()
    padded += "=" * ((-len(padded)) % 4)
    try:
        raw = base64.b64decode(padded, validate=False)
    except Exception as exc:  # noqa: BLE001
        raise ValueError("wireguard private key must be base64") from exc
    if len(raw) != 32:
        raise ValueError("wireguard private key must be 32 bytes")
    return raw


def _touch(row: ReachHub | ReachLink) -> None:
    row.updated_at = datetime.now(timezone.utc)
