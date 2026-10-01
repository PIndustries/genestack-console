"""PXE/DHCP — console-owned, in-process, for bare-metal Talos provisioning.

The compiled Console binary owns DHCP and boot-file HTTP on the provisioning
network (Python: ``app.services.pxe_runtime``). This module renders
``boot.ipxe``, static reservations, and Talos assets under
``<settings.data_dir>/pxe/`` or ``<settings.data_dir>/pxe/{agent_id}/``
(per-agent). After a write, the in-process runtime is reloaded so dnsmasq /
a docker sidecar is not required::

    <data_dir>/pxe/dnsmasq.conf               env-wide DHCP + iPXE chainload
    <data_dir>/pxe/boot.ipxe                  env-wide iPXE script
    <data_dir>/pxe/assets/vmlinuz             talos kernel
    <data_dir>/pxe/assets/initramfs.xz        talos initramfs

Per-agent (each agent = one L2 network = one PXE/DHCP)::

    <data_dir>/pxe/{agent_id}/dnsmasq.conf
    <data_dir>/pxe/{agent_id}/boot.ipxe
    <data_dir>/pxe/{agent_id}/assets/vmlinuz
    <data_dir>/pxe/{agent_id}/assets/initramfs.xz

The env config doc may drive it via the legacy ``pxe:`` section (validated in
app/services/envconfig.py). The new model stores PXE config on
``AgentCredential.pxe_config`` — a JSON dict per agent.

    pxe:
      interface: eth1            # provisioning-network NIC (required)
      range_start: 10.10.0.100   # DHCP pool start (required)
      range_end: 10.10.0.199     # DHCP pool end (required)
      netmask: 255.255.255.0     # optional
      gateway: 10.10.0.1         # optional; also default dns/next_server
      dns: 10.10.0.1             # optional
      next_server: 10.10.0.1     # optional; agent IP on the prov net
      http_port: 8088            # optional; in-process boot-file HTTP port
      image_url: https://...     # optional; falls back to talos.image_url

Nodes with ``source: baremetal`` in the doc's ``servers:`` section get static
``dhcp-host`` reservations from their ``pxe_mac``/``ip`` entries.

After a config change ``ensure_assets_and_config`` reloads the in-process
PXE runtime (DHCP + HTTP) so leases and boot files pick up immediately.
"""

from __future__ import annotations

import io
import tarfile
from pathlib import Path
from typing import Any, Callable

import httpx

from app.config import Settings
from app.services.job_runner import MaasDownloadError, download_factory_image

LogFn = Callable[[str], None]

DEFAULT_HTTP_PORT = 8080
DEFAULT_NETMASK = "255.255.255.0"


class PxeError(RuntimeError):
    """PXE config/asset preparation failed (bad section, unusable image)."""


def _log(log: LogFn | None, msg: str) -> None:
    if log:
        log(msg)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_pxe_config(
    nodes: list[dict[str, Any]],
    pxe_cfg: dict[str, Any],
    settings: Settings | None = None,  # noqa: ARG001 — reserved for future tunables
) -> str:
    """Render the sidecar's dnsmasq.conf.

    ``pxe_cfg`` is the env doc's ``pxe:`` mapping (interface/range_start/
    range_end required — see module docstring). ``nodes`` is a list of
    ``{name, pxe_mac, expected_ip}`` dicts; nodes with both a MAC and an IP
    get a static ``dhcp-host`` reservation. Raises :class:`PxeError` when the
    required keys are missing.
    """
    interface = str(pxe_cfg.get("interface") or "").strip()
    range_start = str(pxe_cfg.get("range_start") or "").strip()
    range_end = str(pxe_cfg.get("range_end") or "").strip()
    missing = [
        key
        for key, value in (
            ("interface", interface),
            ("range_start", range_start),
            ("range_end", range_end),
        )
        if not value
    ]
    if missing:
        raise PxeError(f"pxe config missing required key(s): {', '.join(missing)}")

    netmask = str(pxe_cfg.get("netmask") or DEFAULT_NETMASK).strip()
    gateway = str(pxe_cfg.get("gateway") or "").strip()
    dns = str(pxe_cfg.get("dns") or gateway).strip()
    # next_server is where PXE clients fetch boot.ipxe from — the agent's
    # IP on the provisioning network (sidecar runs network_mode: host).
    next_server = str(pxe_cfg.get("next_server") or gateway).strip()
    http_port = int(pxe_cfg.get("http_port") or DEFAULT_HTTP_PORT)
    boot_url = f"http://{next_server}:{http_port}/boot.ipxe"

    lines = [
        "# Rendered by the Genestack Console (app/services/pxe.py) — do not edit.",
        "# Applied in-process by app.services.pxe_runtime (no docker sidecar).",
        "",
        "# Only serve DHCP on the provisioning interface; never answer elsewhere.",
        f"interface={interface}",
        "bind-interfaces",
        "",
        "# Authoritative pool for the provisioning network.",
        "dhcp-authoritative",
        f"dhcp-range={range_start},{range_end},{netmask},1h",
    ]
    if gateway:
        lines.append(f"dhcp-option=option:router,{gateway}")
    if dns:
        lines.append(f"dhcp-option=option:dns-server,{dns}")
    lines += [
        "",
        "# PXE chainload: clients with an iPXE user-class already run iPXE —",
        "# hand them boot.ipxe directly; BIOS PXE ROMs get undionly first.",
        'dhcp-match=set:ipxe,option:user-class,"iPXE"',
        f"dhcp-boot=tag:ipxe,{boot_url}",
        "# BIOS fallback: needs an undionly.kpxe binary served over TFTP —",
        "# drop it into /srv/pxe and uncomment the two lines below.",
        "# enable-tftp",
        "# tftp-root=/srv/pxe",
        "dhcp-boot=undionly.kpxe",
    ]
    reservations = [
        node
        for node in nodes
        if str(node.get("pxe_mac") or "").strip()
        and str(node.get("expected_ip") or "").strip()
    ]
    if reservations:
        lines += ["", "# Static reservations for registered bare-metal nodes."]
        for node in reservations:
            mac = str(node["pxe_mac"]).strip()
            name = str(node.get("name") or "").strip()
            ip = str(node["expected_ip"]).strip()
            host = f",{name}" if name else ""
            lines.append(f"dhcp-host={mac}{host},{ip}")
    return "\n".join(lines) + "\n"


def render_boot_ipxe(assets_base_url: str) -> str:
    """Render the iPXE script that chainloads the Talos kernel + initramfs.

    ``assets_base_url`` is the console's PXE HTTP base (e.g.
    ``http://10.10.0.1:8080``); assets live under ``<base>/assets/``.

    Kernel args are the documented Talos metal-platform set:

    - ``talos.platform=metal`` — bare-metal platform (disks from the machine).
    - ``ip=dhcp`` — interface configuration via DHCP (our dnsmasq lease).
    - ``console=tty0 console=ttyS0,115200`` — VGA + serial consoles.

    No ``talos.config=`` on purpose: without a machine-config source Talos
    boots into maintenance mode, and the console pushes the config over the
    Talos API (https://<node>:50000, insecure bootstrap) during provision.
    """
    base = assets_base_url.rstrip("/")
    return f"""#!ipxe
# Rendered by the Genestack Console (app/services/pxe.py) — do not edit.
# Talos metal maintenance-mode boot; the console configures the machine via
# the Talos API once it answers on https://<node-ip>:50000.
kernel {base}/assets/vmlinuz talos.platform=metal ip=dhcp console=tty0 console=ttyS0,115200
initrd {base}/assets/initramfs.xz
boot
"""


# ---------------------------------------------------------------------------
# Asset fetch (factory image -> kernel + initramfs)
# ---------------------------------------------------------------------------

# Member suffixes looked for inside a factory image archive/ISO. In a Talos
# metal ISO the kernel is /boot/vmlinuz and the initramfs /boot/initramfs.xz;
# tar-style factory assets use the same boot/ layout.
_KERNEL_SUFFIXES = ("boot/vmlinuz", "vmlinuz")
_INITRD_SUFFIXES = ("boot/initramfs.xz", "boot/initramfs", "initramfs.xz", "initramfs")


def _match_member(name: str, suffixes: tuple[str, ...]) -> bool:
    clean = name.lstrip("/").lower()
    # ISO9660 names may be mangled (uppercase, ";1" version suffix, 8.3).
    clean = clean.split(";")[0]
    return any(clean.endswith(suffix) for suffix in suffixes)


def _extract_from_tar(path: Path, log: LogFn | None) -> tuple[bytes, bytes] | None:
    """(kernel, initrd) from a tar/tar.xz/... factory asset file, or None.

    Opened from disk (tarfile streams the members); the multi-GB image is
    not held in process memory.
    """
    try:
        archive = tarfile.open(path)
    except tarfile.TarError:
        return None
    kernel = initrd = None
    with archive:
        for member in archive.getmembers():
            if not member.isreg():
                continue
            if kernel is None and _match_member(member.name, _KERNEL_SUFFIXES):
                extracted = archive.extractfile(member)
                kernel = extracted.read() if extracted else None
                _log(log, f"[pxe-assets] kernel from tar member {member.name}")
            elif initrd is None and _match_member(member.name, _INITRD_SUFFIXES):
                extracted = archive.extractfile(member)
                initrd = extracted.read() if extracted else None
                _log(log, f"[pxe-assets] initramfs from tar member {member.name}")
    if kernel and initrd:
        return kernel, initrd
    return None


def _extract_from_iso(path: Path, log: LogFn | None) -> tuple[bytes, bytes] | None:
    """(kernel, initrd) from a Talos metal ISO, or None when unsupported.

    Uses pycdlib when installed; ISO9660 names are matched loosely (8.3
    mangling tolerated) — /boot/vmlinuz and /boot/initramfs.xz in practice.
    Note: pycdlib reads the ISO into memory (its design); for the common
    tar-style factory asset the tar path above stays disk-streamed.
    """
    try:
        import pycdlib
    except ImportError:
        return None
    fp = path.open("rb")
    try:
        iso = pycdlib.PyCdlib()
        iso.open_fp(fp)
    except Exception:  # noqa: BLE001 — not an ISO after all
        fp.close()
        return None
    kernel = initrd = None
    try:
        for dirname, _dirs, files in iso.walk(iso_path="/"):
            for filename in files:
                full = f"{dirname}/{filename}"
                if kernel is None and _match_member(filename, _KERNEL_SUFFIXES):
                    buf = io.BytesIO()
                    iso.get_file_from_iso_fp(buf, iso_path=full)
                    kernel = buf.getvalue()
                    _log(log, f"[pxe-assets] kernel from ISO {full}")
                elif initrd is None and _match_member(filename, _INITRD_SUFFIXES):
                    buf = io.BytesIO()
                    iso.get_file_from_iso_fp(buf, iso_path=full)
                    initrd = buf.getvalue()
                    _log(log, f"[pxe-assets] initramfs from ISO {full}")
    finally:
        # pycdlib owns (and closes) the file handle opened via open_fp.
        iso.close()
    if kernel and initrd:
        return kernel, initrd
    return None


def fetch_talos_assets(
    image_url: str,
    dest_dir: str | Path,
    log: LogFn | None = None,
) -> dict[str, Any]:
    """Download the factory image once and place kernel+initrd under dest_dir.

    Writes ``<dest_dir>/assets/vmlinuz`` and ``<dest_dir>/assets/initramfs.xz``.
    Skips the download when both already exist (restart-safe). Extraction:
    tar-style factory assets first, then ISO9660 via pycdlib when installed —
    a Talos metal ISO keeps them at /boot/vmlinuz and /boot/initramfs.xz.
    Raises :class:`MaasDownloadError` on download failure, :class:`PxeError`
    when the image format cannot be mined.
    """
    if not image_url.lower().startswith("https://"):
        raise PxeError(f"pxe image_url must be an https:// URL (got: {image_url!r})")
    dest = Path(dest_dir)
    assets = dest / "assets"
    kernel_path = assets / "vmlinuz"
    initrd_path = assets / "initramfs.xz"
    if kernel_path.is_file() and initrd_path.is_file():
        _log(
            log,
            f"[pxe-assets] kernel+initramfs already present under {assets} — skipping",
        )
        return {
            "ok": True,
            "changed": False,
            "kernel": str(kernel_path),
            "initrd": str(initrd_path),
        }

    image_path, sha256 = download_factory_image(
        image_url, log, dest_dir=dest / "downloads"
    )
    image_size = image_path.stat().st_size

    pair = _extract_from_tar(image_path, log)
    if pair is None:
        pair = _extract_from_iso(image_path, log)
    image_path.unlink(missing_ok=True)
    if pair is None:
        raise PxeError(
            "could not extract kernel+initramfs from the factory image "
            "(tried tar members and ISO9660 via pycdlib; install pycdlib for "
            "factory ISOs, or point pxe.image_url at a tar asset)"
        )
    kernel, initrd = pair
    assets.mkdir(parents=True, exist_ok=True)
    kernel_path.write_bytes(kernel)
    initrd_path.write_bytes(initrd)
    _log(
        log,
        f"[pxe-assets] wrote {kernel_path} ({len(kernel)} bytes) and "
        f"{initrd_path} ({len(initrd)} bytes) sha256={sha256}",
    )
    return {
        "ok": True,
        "changed": True,
        "kernel": str(kernel_path),
        "initrd": str(initrd_path),
        "sha256": sha256,
        "bytes": image_size,
    }


# ---------------------------------------------------------------------------
# Kickstart / cloud-init / autoinstall templates
# ---------------------------------------------------------------------------


def render_cloudinit_userdata(
    ssh_public_key: str,
    username: str = "genestack",
) -> str:
    """Render a cloud-init user-data file with SSH authorized keys.

    Injects ``ssh_public_key`` so the PXE-provisioned node grants access to
    the environment's SSH identity. ``username`` is the default login user
    created during automated install (``genestack`` or ``ubuntu``).
    """
    return (
        "#cloud-config\n"
        f"username: {username}\n"
        "ssh_authorized_keys:\n"
        f"  - {ssh_public_key}\n"
        "ssh_pwauth: false\n"
        "disable_root: true\n"
    )


def render_cloudinit_metadata(
    hostname: str = "pxe-node",
    instance_id: str = "pxe-00000000-0000-0000-0000-000000000000",
) -> str:
    """Render a minimal cloud-init meta-data file for PXE boot."""
    return "instance-id: " + instance_id + "\n" "local-hostname: " + hostname + "\n"


def render_kickstart(
    ssh_public_key: str,
    username: str = "genestack",
) -> str:
    """Render a Fedora/RHEL kickstart (ks.cfg) with SSH key injection.

    Uses a %post script to place the environment's public key into
    ``~/.ssh/authorized_keys`` for the default user after install.
    """
    return (
        "# Kickstart auto-install — generated by Genestack Console\n"
        "# Do not edit by hand.\n"
        "text\n"
        "keyboard us\n"
        "lang en_US\n"
        "timezone UTC\n"
        f"user --name={username} --groups=wheel --password=disabled --iscrypted\n"
        "firewall --disabled\n"
        "selinux --disabled\n"
        "reboot\n"
        "%post\n"
        f"useradd -m -s /bin/bash -G wheel {username}\n"
        f"mkdir -p /home/{username}/.ssh\n"
        f'echo "{ssh_public_key}" > /home/{username}/.ssh/authorized_keys\n'
        f"chown -R {username}:{username} /home/{username}/.ssh\n"
        "chmod 700 /home/{username}/.ssh\n"
        "chmod 600 /home/{username}/.ssh/authorized_keys\n"
        "%end\n"
    )


def render_autoinstall(
    ssh_public_key: str,
    username: str = "genestack",
) -> str:
    """Render an Ubuntu autoinstall (autoinstall.yaml) with SSH authorized keys.

    Used when PXE-booting an Ubuntu installer kernel/initrd. The ``ssh``
    section ensures the environment's public key is installed for the
    default user on first boot.
    """
    return (
        "# Autoinstall config — generated by Genestack Console\n"
        "# Do not edit by hand.\n"
        "version: 1\n"
        "early_command:\n"
        "ssh:\n"
        "  install-server: false\n"
        "  authorized_keys:\n"
        f"    - {ssh_public_key}\n"
        "storage:\n"
        "  layout:\n"
        "    match:\n"
        "      serial: .+\n"
        "    name: /\n"
        "    overwrite: true\n"
        "identity:\n"
        f"  hostname: {username}-pxe-node\n"
        f"  username: {username}\n"
        "  password: disabled\n"
    )


# ---------------------------------------------------------------------------
# Integration entry point
# ---------------------------------------------------------------------------


def _write_if_changed(path: Path, text: str, log: LogFn | None) -> bool:
    """Write text to path only when different; returns the changed flag."""
    if path.is_file() and path.read_text(encoding="utf-8") == text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    _log(log, f"[pxe] wrote {path} ({len(text)} bytes)")
    return True


def _baremetal_nodes(doc: dict[str, Any]) -> list[dict[str, Any]]:
    """``{name, pxe_mac, expected_ip}`` entries for ``source: baremetal`` servers."""
    nodes: list[dict[str, Any]] = []
    servers = doc.get("servers")
    if not isinstance(servers, dict):
        return nodes
    for hostname, entry in servers.items():
        if not isinstance(entry, dict) or entry.get("source") != "baremetal":
            continue
        nodes.append(
            {
                "name": str(hostname),
                "pxe_mac": entry.get("pxe_mac"),
                "expected_ip": entry.get("ip") or entry.get("expected_ip"),
            }
        )
    return nodes


def ensure_assets_and_config(
    env: Any,
    doc: dict[str, Any],
    settings: Settings,
    log: LogFn | None = None,
    agent_id: str | None = None,
    agent_pxe_config: dict | None = None,
    apply_runtime: bool = True,
) -> dict[str, Any]:
    """Render PXE files, fetch assets when missing, reload in-process runtime.

    When ``agent_id`` is provided, writes per-agent configs under
    ``<settings.data_dir>/pxe/{agent_id}/`` using ``agent_pxe_config``.
    Otherwise falls back to the flat env-wide ``pxe:`` section under
    ``<settings.data_dir>/pxe/``.

    Never raises — returns ``{"ok": False, "error": ...}`` on any problem.
    ``changed`` reports whether any file was written; the in-process PXE
    runtime is always reapplied so DHCP/HTTP match the files.
    """
    env_name = getattr(env, "name", None) or "<env>"
    try:
        if agent_id:
            pxe_cfg = agent_pxe_config
            if not isinstance(pxe_cfg, dict) or not pxe_cfg:
                return {
                    "ok": False,
                    "error": (
                        f"agent '{agent_id}' has no pxe_config — attach a PXE "
                        "config to the agent credential (pxe_config JSON) to "
                        "enable per-agent PXE provisioning"
                    ),
                }
            root = Path(settings.data_dir) / "pxe" / agent_id
        else:
            pxe_cfg = doc.get("pxe")
            if not isinstance(pxe_cfg, dict) or not pxe_cfg:
                return {
                    "ok": False,
                    "error": (
                        f"environment '{env_name}' doc has no pxe: section — add "
                        "pxe: {interface, range_start, range_end, ...} to the env "
                        "config to enable console-managed PXE provisioning"
                    ),
                }
            root = Path(settings.data_dir) / "pxe"

        nodes = _baremetal_nodes(doc)
        root.mkdir(parents=True, exist_ok=True)

        changed = _write_if_changed(
            root / "dnsmasq.conf", render_pxe_config(nodes, pxe_cfg), log
        )

        next_server = str(
            pxe_cfg.get("next_server") or pxe_cfg.get("gateway") or ""
        ).strip()
        http_port = int(pxe_cfg.get("http_port") or DEFAULT_HTTP_PORT)
        base_url = f"http://{next_server}:{http_port}"
        changed |= _write_if_changed(
            root / "boot.ipxe", render_boot_ipxe(base_url), log
        )

        image_url = str(pxe_cfg.get("image_url") or "").strip()
        if not image_url:
            talos = doc.get("talos")
            if isinstance(talos, dict):
                image_url = str(talos.get("image_url") or "").strip()
        assets: dict[str, Any] = {"changed": False}
        if image_url:
            assets = fetch_talos_assets(image_url, root, log)
            changed |= bool(assets.get("changed"))
        else:
            _log(log, "[pxe] no pxe.image_url / talos.image_url — skipping asset fetch")

        # --- Inject env SSH key into PXE boot configs ---------------------------
        ssh_key = getattr(env, "ssh_public_key", None) or ""
        if ssh_key:
            ssh_key = str(ssh_key).strip()
            if ssh_key:
                pxe_user = (
                    str(pxe_cfg.get("pxe_username") or "genestack").strip()
                    or "genestack"
                )

                changed |= _write_if_changed(
                    root / "user-data",
                    render_cloudinit_userdata(ssh_key, username=pxe_user),
                    log,
                )
                changed |= _write_if_changed(
                    root / "meta-data",
                    render_cloudinit_metadata(),
                    log,
                )
                changed |= _write_if_changed(
                    root / "ks.cfg",
                    render_kickstart(ssh_key, username=pxe_user),
                    log,
                )
                changed |= _write_if_changed(
                    root / "autoinstall.yaml",
                    render_autoinstall(ssh_key, username=pxe_user),
                    log,
                )
        else:
            _log(
                log,
                "[pxe] env has no ssh_public_key — skipping SSH key injection into PXE boot configs",
            )

        runtime: dict[str, Any] = {}
        if apply_runtime and not agent_id:
            # Hub is on this L2 — run DHCP/HTTP here.
            from app.services.pxe_runtime import apply_pxe_cfg

            runtime = apply_pxe_cfg(pxe_cfg, nodes, root)
            _log(
                log,
                "[pxe] hub L2 runtime applied "
                f"dhcp={runtime.get('dhcp_running')} http={runtime.get('http_running')}",
            )
        elif apply_runtime and agent_id:
            # Remote site: agent is the L2 proxy. Do not bind DHCP on the hub.
            runtime = {
                "ok": True,
                "proxy": "agent",
                "agent_id": agent_id,
                "dhcp_running": False,
                "http_running": False,
            }
            _log(
                log, f"[pxe] agent {agent_id} is the L2 proxy — hub will push pxe_apply"
            )
        elif changed:
            _log(log, "[pxe] config/assets changed — runtime apply skipped")
        return {
            "ok": True,
            "changed": changed,
            "config_path": str(root / "dnsmasq.conf"),
            "ipxe_path": str(root / "boot.ipxe"),
            "assets_dir": str(root / "assets"),
            "nodes": len(nodes),
            "image_url": image_url or None,
            "agent_id": agent_id,
            "assets": assets,
            "runtime": runtime,
            "boot_configs": {
                "user_data": str(root / "user-data"),
                "meta_data": str(root / "meta-data"),
                "ks_cfg": str(root / "ks.cfg"),
                "autoinstall": str(root / "autoinstall.yaml"),
            },
        }
    except (PxeError, MaasDownloadError) as exc:
        _log(log, f"[pxe] {exc}")
        return {"ok": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 — never-raise contract
        _log(log, f"[pxe] unexpected error: {exc}")
        return {"ok": False, "error": f"unexpected error: {exc}"}


def list_agent_pxe_configs(
    env: Any,
    db: Any,  # Session
) -> list[dict]:
    """Return [{agent_id, name, pxe_config, ...}] for all agents with PXE config."""
    from app.models import AgentCredential
    from sqlalchemy import select

    creds = db.scalars(
        select(AgentCredential)
        .where(
            AgentCredential.environment_id == env.id,
            AgentCredential.pxe_config.isnot(None),
        )
        .order_by(AgentCredential.created_at)
    ).all()
    result = []
    for cred in creds:
        result.append(
            {
                "agent_id": cred.id,
                "name": cred.name,
                "pxe_config": cred.pxe_config,
            }
        )
    return result


def render_env_pxe_status(
    env: Any,
    db: Any,
    doc: dict,
    settings: Settings,
) -> dict:
    """Render the PXE status for an env: lists all agent-level PXE configs + flat fallback."""
    from app.models import AgentCredential
    from sqlalchemy import select

    agent_configs = []
    creds = db.scalars(
        select(AgentCredential)
        .where(
            AgentCredential.environment_id == env.id,
            AgentCredential.pxe_config.isnot(None),
        )
        .order_by(AgentCredential.created_at)
    ).all()
    for cred in creds:
        cfg = cred.pxe_config or {}
        agent_id = cred.id
        root = Path(settings.data_dir) / "pxe" / agent_id
        http_port = int(cfg.get("http_port") or DEFAULT_HTTP_PORT)
        next_server = str(cfg.get("next_server") or cfg.get("gateway") or "").strip()
        running = False
        if next_server:
            try:
                with httpx.Client(verify=False, timeout=3) as c:
                    c.get(f"http://{next_server}:{http_port}/boot.ipxe")
                    running = True
            except Exception:
                running = False
        kernel = root / "assets" / "vmlinuz"
        initrd = root / "assets" / "initramfs.xz"
        agent_configs.append(
            {
                "agent_id": agent_id,
                "name": cred.name,
                "pxe_config": cfg,
                "runtime_http": running,
                "sidecar_running": running,  # alias: there is no sidecar; HTTP probe
                "assets": {
                    "kernel_ready": kernel.is_file(),
                    "kernel_size": kernel.stat().st_size if kernel.is_file() else None,
                    "initrd_ready": initrd.is_file(),
                    "initrd_size": initrd.stat().st_size if initrd.is_file() else None,
                    "image_url": str(cfg.get("image_url") or ""),
                },
                "dnsmasq_conf": (
                    (root / "dnsmasq.conf").read_text()
                    if (root / "dnsmasq.conf").is_file()
                    else None
                ),
                "boot_ipxe": (
                    (root / "boot.ipxe").read_text()
                    if (root / "boot.ipxe").is_file()
                    else None
                ),
            }
        )

    flat_pxe = doc.get("pxe") if isinstance(doc, dict) else None
    enabled = bool(flat_pxe) or bool(agent_configs)
    from app.services.pxe_runtime import get_manager

    runtime = get_manager().status()
    return {
        "enabled": enabled,
        "flat_config": flat_pxe,
        "agent_configs": agent_configs,
        "runtime": runtime,
    }
