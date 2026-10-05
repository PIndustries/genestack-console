"""Join Talos + Kubernetes + OpenStack into one node fabric view."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models import BaremetalNode, Environment
from app.services import envconfig
from app.services.cluster import _kube_env, _node_roles, _node_status
from app.services.envcontext import build_context
from app.services.livestate import _mem_to_gi, _run
from app.services.osclient import OpenStackClient
from app.services.talos import (
    DEFAULT_TALOS_INSTALL_IMAGE,
    node_apply_ip,
    node_cluster_ip,
    valid_install_image,
    valid_node_endpoint,
)

TALOS_TIMEOUT = 12
DMESG_LINES = 80
LOG_LINES = 200
UPGRADE_TIMEOUT = 60
APPLY_TIMEOUT = 60
EVENTS_TIMEOUT = 15
APPLY_MODES = frozenset({"auto", "staged", "no-reboot", "reboot"})
SERVICE_ACTIONS = frozenset({"start", "stop", "restart"})
_SERVICE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")
_SINCE_RE = re.compile(r"^(?:\d+[smhd]|\d{4}-\d{2}-\d{2}T[\d:.]+Z?)$")
APPLY_YAML_MAX = 512_000


def _clean_ip(value: Any) -> str:
    return str(value or "").split("/", 1)[0].strip()


def _entry_os(entry: dict[str, Any], ubuntu_names: set[str], hostname: str) -> str:
    """Ubuntu when the row is recorded that way. Otherwise Talos.

    ``adopt: kubespray`` is the stored mark for a machine that already has
    Ubuntu. A bare-metal next boot of ubuntu is the same operating system.
    """
    if str(entry.get("adopt") or "").strip() == "kubespray":
        return "ubuntu"
    if hostname in ubuntu_names:
        return "ubuntu"
    return "talos"


def _ubuntu_hostnames(db: Session | None, environment_id: str) -> set[str]:
    if db is None or not environment_id:
        return set()
    rows = (
        db.query(BaremetalNode.name, BaremetalNode.next_boot, BaremetalNode.boot_stage)
        .filter(BaremetalNode.environment_id == environment_id)
        .all()
    )
    names: set[str] = set()
    for name, nxt, stage in rows:
        if str(nxt or "") == "ubuntu" or str(stage or "") == "ubuntu":
            names.add(str(name))
    return names


def _node_talos_ip(entry: dict[str, Any], hostname: str) -> str:
    """Address for day-2 ``talosctl --nodes``.

    After dual-NIC lockdown, public :50000 is only reachable on the
    talosconfig endpoint (the console/fleet host). Other nodes are reached
    by targeting their VLAN/cluster IP so the endpoint proxies over the
    private fabric.
    """
    private = _clean_ip(entry.get("private_ip") or entry.get("ip") or "")
    if private and valid_node_endpoint(private):
        return private
    public = node_apply_ip(entry, hostname)
    if public and valid_node_endpoint(public) and public != hostname:
        return _clean_ip(public)
    return ""


def _k8s_nodes(ctx) -> list[dict[str, Any]]:
    kubectl = shutil.which("kubectl")
    if not kubectl or not ctx.kubeconfig:
        return []
    env = _kube_env(ctx.kubeconfig)
    raw, err = _run([kubectl, "get", "nodes", "-o", "json"], env=env)
    if err:
        return []
    try:
        data = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return []
    rows = []
    for n in data.get("items") or []:
        addrs = (n.get("status") or {}).get("addresses") or []
        internal = next(
            (a.get("address") for a in addrs if a.get("type") == "InternalIP"), None
        )
        external = next(
            (a.get("address") for a in addrs if a.get("type") == "ExternalIP"), None
        )
        hostname = next(
            (a.get("address") for a in addrs if a.get("type") == "Hostname"), None
        )
        mem = ((n.get("status") or {}).get("capacity") or {}).get("memory")
        cpu = ((n.get("status") or {}).get("capacity") or {}).get("cpu")
        rows.append(
            {
                "name": (n.get("metadata") or {}).get("name"),
                "roles": _node_roles(n),
                "status": _node_status(n),
                "unschedulable": bool((n.get("spec") or {}).get("unschedulable")),
                "version": ((n.get("status") or {}).get("nodeInfo") or {}).get(
                    "kubeletVersion"
                ),
                "cpu_capacity": cpu,
                "mem_gi": _mem_to_gi(mem),
                "internal_ip": internal,
                "external_ip": external,
                "hostname": hostname,
            }
        )
    return rows


def _talos_version(talosconfig: str, node_ip: str) -> dict[str, Any]:
    talosctl = shutil.which("talosctl")
    if not talosctl or not node_ip:
        return {
            "reachable": False,
            "version": None,
            "error": "talosctl missing" if not talosctl else "no ip",
        }
    if not valid_node_endpoint(node_ip):
        return {"reachable": False, "version": None, "error": "invalid node address"}
    try:
        proc = subprocess.run(
            [
                talosctl,
                "version",
                "--nodes",
                node_ip,
                "--talosconfig",
                talosconfig,
                "--short",
            ],
            capture_output=True,
            text=True,
            timeout=TALOS_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"reachable": False, "version": None, "error": str(exc)[:160]}
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        return {
            "reachable": False,
            "version": None,
            "error": (detail[-1] if detail else f"exit {proc.returncode}")[:160],
        }
    version = None
    after_server = False
    for line in (proc.stdout or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("Server:"):
            rest = stripped.split(":", 1)[-1].strip()
            if rest.startswith("v"):
                version = rest.split()[0]
                break
            after_server = True
            continue
        if after_server and ("Tag:" in stripped or stripped.startswith("v")):
            token = stripped.split()[-1]
            if token.startswith("v"):
                version = token
                break
        if stripped.startswith("v"):
            version = stripped.split()[0]
            break
    return {"reachable": True, "version": version, "error": None, "hostname": None}


def _match_k8s(
    k8s: list[dict[str, Any]], private_ip: str, public_ip: str
) -> dict[str, Any] | None:
    for n in k8s:
        if private_ip and n.get("internal_ip") == private_ip:
            return n
        if public_ip and n.get("external_ip") == public_ip:
            return n
    return None


def _match_nova(
    computes: list[dict[str, Any]], k8s_name: str | None
) -> dict[str, Any] | None:
    if not k8s_name:
        return None
    for c in computes:
        if c.get("host") == k8s_name:
            return c
    return None


def _doc_install_image(doc: Any) -> str:
    try:
        talos_cfg = doc.get("talos") if isinstance(doc, dict) else None
        raw = talos_cfg.get("install_image") if isinstance(talos_cfg, dict) else None
        image = str(raw or "").strip()
        if image:
            return image
    except Exception:  # noqa: BLE001
        pass
    return DEFAULT_TALOS_INSTALL_IMAGE


def _role_tokens(roles: Any) -> list[str]:
    if isinstance(roles, str):
        return [p.strip() for p in roles.replace(",", " ").split() if p.strip()]
    if isinstance(roles, (list, tuple, set)):
        return [str(r).strip() for r in roles if str(r).strip()]
    return []


def _is_control_plane(roles: Any) -> bool:
    return any(token.lower() == "k8s_control_plane" for token in _role_tokens(roles))


def _cluster_summary(
    env: Environment, nodes: list[dict[str, Any]], install_image: str
) -> dict[str, Any]:
    """Roll up inventory rows into Omni-like cluster counts."""
    machines = len(nodes)
    control_planes = 0
    ready = 0
    not_ready = 0
    talos_reachable = 0
    talos_versions: set[str] = set()
    kubernetes_versions: set[str] = set()
    for node in nodes:
        if not isinstance(node, dict):
            continue
        if _is_control_plane(node.get("roles")):
            control_planes += 1
        talos = node.get("talos") if isinstance(node.get("talos"), dict) else {}
        if talos.get("reachable"):
            talos_reachable += 1
        talos_ver = str(talos.get("version") or "").strip()
        if talos_ver:
            talos_versions.add(talos_ver)
        kn = node.get("kubernetes")
        if isinstance(kn, dict):
            if str(kn.get("status") or "").lower() == "ready":
                ready += 1
            else:
                not_ready += 1
            kube_ver = str(kn.get("version") or "").strip()
            if kube_ver:
                kubernetes_versions.add(kube_ver)
    return {
        "name": str(getattr(env, "name", "") or ""),
        "machines": machines,
        "control_planes": control_planes,
        "workers": machines - control_planes,
        "ready": ready,
        "not_ready": not_ready,
        "talos_reachable": talos_reachable,
        "talos_versions": sorted(talos_versions),
        "kubernetes_versions": sorted(kubernetes_versions),
        "install_image": install_image,
    }


def platform_overview(
    env: Environment, settings: Settings | None = None, db: Session | None = None
) -> dict[str, Any]:
    """One row per inventory machine: Talos + Kubernetes + Nova compute."""
    settings = settings or get_settings()
    ctx = build_context(env, settings)
    nodes: list[dict[str, Any]] = []
    error = None
    install_image = DEFAULT_TALOS_INSTALL_IMAGE

    def _payload(err: str | None) -> dict[str, Any]:
        return {
            "nodes": nodes,
            "error": err,
            "install_image": install_image,
            "cluster": _cluster_summary(env, nodes, install_image),
        }

    try:
        doc: dict[str, Any] = {}
        if db is not None:
            current = envconfig.get_current(db, env)
            if current:
                doc = current[0] if isinstance(current[0], dict) else {}
        install_image = _doc_install_image(doc)
        servers = doc.get("servers") if isinstance(doc.get("servers"), dict) else {}
        k8s = _k8s_nodes(ctx)
        computes: list[dict[str, Any]] = []
        if ctx.kubeconfig:
            try:
                with OpenStackClient(ctx.kubeconfig) as client:
                    computes = client.list_compute_services()
            except Exception as exc:  # noqa: BLE001
                error = f"openstack: {exc}"[:160]

        talosconfig = None
        if ctx.config_dir is not None:
            candidate = ctx.config_dir / "talos" / "talosconfig"
            if candidate.is_file():
                talosconfig = str(candidate)

        if not servers:
            # No inventory doc: still show k8s nodes so the fabric isn't empty.
            for kn in k8s:
                nova = _match_nova(computes, kn.get("name"))
                nodes.append(
                    {
                        "name": kn.get("name"),
                        "public_ip": kn.get("external_ip"),
                        "private_ip": kn.get("internal_ip"),
                        "roles": kn.get("roles"),
                        "os": "talos",
                        "talos": {
                            "reachable": None,
                            "version": None,
                            "error": "no inventory",
                        },
                        "kubernetes": kn,
                        "openstack": nova,
                    }
                )
            return _payload(error)

        pending: list[tuple[str, dict[str, Any], str, str]] = []
        for hostname, entry in servers.items():
            if not isinstance(entry, dict):
                continue
            public_ip = node_apply_ip(entry, hostname)
            private_ip = node_cluster_ip(entry, hostname)
            pending.append((hostname, entry, public_ip, private_ip))

        ubuntu_names = _ubuntu_hostnames(db, str(getattr(env, "id", "") or ""))
        talos_by_host: dict[str, dict[str, Any]] = {}
        if talosconfig:
            with ThreadPoolExecutor(max_workers=6) as pool:
                futs = {
                    pool.submit(_talos_version, talosconfig, talos_ip): hostname
                    for hostname, entry, _pub, _priv in pending
                    if _entry_os(entry, ubuntu_names, hostname) == "talos"
                    and (talos_ip := _node_talos_ip(entry, hostname))
                }
                for fut in as_completed(futs):
                    talos_by_host[futs[fut]] = fut.result()

        for hostname, entry, public_ip, private_ip in pending:
            kn = _match_k8s(k8s, _clean_ip(private_ip), _clean_ip(public_ip))
            nova = _match_nova(computes, (kn or {}).get("name"))
            os_name = _entry_os(entry, ubuntu_names, hostname)
            if os_name == "ubuntu":
                talos = {"reachable": None, "version": None, "error": None}
            else:
                talos = talos_by_host.get(hostname) or {
                    "reachable": False,
                    "version": None,
                    "error": "no talosconfig" if not talosconfig else "no ip",
                }
            nodes.append(
                {
                    "name": hostname,
                    "public_ip": _clean_ip(entry.get("public_ip") or public_ip),
                    "private_ip": _clean_ip(entry.get("private_ip") or private_ip),
                    "roles": entry.get("roles"),
                    "os": os_name,
                    "talos": talos,
                    "kubernetes": kn,
                    "openstack": nova,
                }
            )
        return _payload(error)
    except Exception as exc:  # noqa: BLE001
        return _payload(str(exc)[:200])
    finally:
        ctx.cleanup()


def _node_public_ip(
    env: Environment, settings: Settings, db: Session | None, name: str
) -> tuple[str | None, str | None]:
    ctx = build_context(env, settings)
    try:
        talosconfig = None
        if ctx.config_dir is not None:
            candidate = ctx.config_dir / "talos" / "talosconfig"
            if candidate.is_file():
                talosconfig = str(candidate)
        doc: dict[str, Any] = {}
        if db is not None:
            current = envconfig.get_current(db, env)
            if current and isinstance(current[0], dict):
                doc = current[0]
        servers = doc.get("servers") if isinstance(doc.get("servers"), dict) else {}
        entry = servers.get(name) if isinstance(servers.get(name), dict) else None
        if entry is None:
            return talosconfig, None
        ip = _node_talos_ip(entry, name)
        if not ip:
            return talosconfig, None
        return talosconfig, ip
    finally:
        ctx.cleanup()


def talos_dmesg(
    env: Environment,
    name: str,
    settings: Settings | None = None,
    db: Session | None = None,
) -> dict[str, Any]:
    settings = settings or get_settings()
    talosconfig, node_ip = _node_public_ip(env, settings, db, name)
    if not talosconfig or not node_ip:
        return {"ok": False, "error": "talosconfig or node IP missing", "text": ""}
    talosctl = shutil.which("talosctl")
    if not talosctl:
        return {"ok": False, "error": "talosctl not found", "text": ""}
    try:
        proc = subprocess.run(
            [talosctl, "dmesg", "--nodes", node_ip, "--talosconfig", talosconfig],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "error": str(exc)[:160], "text": ""}
    text = proc.stdout or ""
    lines = text.splitlines()
    if len(lines) > DMESG_LINES:
        text = "\n".join(lines[-DMESG_LINES:])
    if proc.returncode != 0 and not text:
        return {
            "ok": False,
            "error": (proc.stderr or f"exit {proc.returncode}")[:200],
            "text": "",
        }
    return {"ok": True, "error": None, "text": text, "node": name, "ip": node_ip}


def _parse_services(text: str) -> list[dict[str, str]]:
    """Parse ``talosctl services`` table rows; never raises."""
    rows: list[dict[str, str]] = []
    try:
        for line in (text or "").splitlines():
            parts = line.split()
            if not parts:
                continue
            if parts[0].lower() in {"node", "id", "service"}:
                continue
            if "." in parts[0] or ":" in parts[0]:
                parts = parts[1:]
            if len(parts) < 3:
                continue
            rows.append(
                {
                    "id": parts[0],
                    "state": parts[1],
                    "health": parts[2],
                    "last_event": " ".join(parts[3:]) if len(parts) > 3 else "",
                }
            )
    except Exception:  # noqa: BLE001
        return []
    return rows


def talos_services(
    env: Environment,
    name: str,
    settings: Settings | None = None,
    db: Session | None = None,
) -> dict[str, Any]:
    settings = settings or get_settings()
    talosconfig, node_ip = _node_public_ip(env, settings, db, name)
    if not talosconfig or not node_ip:
        return {
            "ok": False,
            "error": "talosconfig or node IP missing",
            "text": "",
            "services": [],
            "node": name,
            "ip": node_ip,
        }
    talosctl = shutil.which("talosctl")
    if not talosctl:
        return {
            "ok": False,
            "error": "talosctl not found",
            "text": "",
            "services": [],
            "node": name,
            "ip": node_ip,
        }
    try:
        proc = subprocess.run(
            [talosctl, "services", "--nodes", node_ip, "--talosconfig", talosconfig],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "ok": False,
            "error": str(exc)[:160],
            "text": "",
            "services": [],
            "node": name,
            "ip": node_ip,
        }
    text = proc.stdout or ""
    if proc.returncode != 0 and not text:
        return {
            "ok": False,
            "error": (proc.stderr or f"exit {proc.returncode}")[:200],
            "text": "",
            "services": [],
            "node": name,
            "ip": node_ip,
        }
    return {
        "ok": True,
        "error": None,
        "text": text,
        "services": _parse_services(text),
        "node": name,
        "ip": node_ip,
    }


def talos_reboot(
    env: Environment,
    name: str,
    settings: Settings | None = None,
    db: Session | None = None,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    settings = settings or get_settings()
    talosconfig, node_ip = _node_public_ip(env, settings, db, name)
    if not talosconfig or not node_ip:
        return {"ok": False, "dry_run": dry_run, "error": "talosconfig or node IP missing"}
    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "error": None,
            "message": f"[dry-run] would reboot {name} ({node_ip})",
            "node": name,
            "ip": node_ip,
        }
    talosctl = shutil.which("talosctl")
    if not talosctl:
        return {"ok": False, "dry_run": False, "error": "talosctl not found"}
    try:
        proc = subprocess.run(
            [
                talosctl,
                "reboot",
                "--nodes",
                node_ip,
                "--talosconfig",
                talosconfig,
                "--wait=false",
            ],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "dry_run": False, "error": str(exc)[:160]}
    if proc.returncode != 0:
        return {
            "ok": False,
            "dry_run": False,
            "error": (proc.stderr or proc.stdout or f"exit {proc.returncode}")[:200],
        }
    return {
        "ok": True,
        "dry_run": False,
        "error": None,
        "message": f"reboot requested for {name} ({node_ip})",
        "node": name,
        "ip": node_ip,
    }


def talos_upgrade(
    env: Environment,
    name: str,
    settings: Settings | None = None,
    db: Session | None = None,
    image: str | None = None,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    settings = settings or get_settings()
    requested = str(image or "").strip()
    if requested:
        if not valid_install_image(requested):
            return {
                "ok": False,
                "error": "invalid image",
                "image": requested,
                "node": name,
                "ip": None,
            }
        install_image = requested
    else:
        try:
            current = envconfig.get_current(db, env) if db else None
            doc = current[0] if current else {}
            talos_cfg = doc.get("talos") if isinstance(doc, dict) else None
            raw = (
                talos_cfg.get("install_image") if isinstance(talos_cfg, dict) else None
            )
            install_image = str(raw or "").strip() or DEFAULT_TALOS_INSTALL_IMAGE
        except Exception:  # noqa: BLE001
            install_image = DEFAULT_TALOS_INSTALL_IMAGE
        if not valid_install_image(install_image):
            install_image = DEFAULT_TALOS_INSTALL_IMAGE
    talosconfig, node_ip = _node_public_ip(env, settings, db, name)
    if not talosconfig or not node_ip:
        return {
            "ok": False,
            "dry_run": dry_run,
            "error": "talosconfig or node IP missing",
            "image": install_image,
            "node": name,
            "ip": node_ip,
        }
    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "error": None,
            "message": (
                f"[dry-run] would upgrade {name} ({node_ip}) to {install_image}"
            ),
            "image": install_image,
            "node": name,
            "ip": node_ip,
        }
    talosctl = shutil.which("talosctl")
    if not talosctl:
        return {
            "ok": False,
            "dry_run": False,
            "error": "talosctl not found",
            "image": install_image,
            "node": name,
            "ip": node_ip,
        }
    try:
        proc = subprocess.run(
            [
                talosctl,
                "upgrade",
                "--nodes",
                node_ip,
                "--image",
                install_image,
                "--preserve",
                "--wait=false",
                "--talosconfig",
                talosconfig,
            ],
            capture_output=True,
            text=True,
            timeout=UPGRADE_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "ok": False,
            "error": str(exc)[:160],
            "image": install_image,
            "node": name,
            "ip": node_ip,
        }
    if proc.returncode != 0:
        return {
            "ok": False,
            "error": (proc.stderr or proc.stdout or f"exit {proc.returncode}")[:200],
            "image": install_image,
            "node": name,
            "ip": node_ip,
        }
    return {
        "ok": True,
        "dry_run": False,
        "error": None,
        "message": f"upgrade requested for {name} ({node_ip}) to {install_image}",
        "image": install_image,
        "node": name,
        "ip": node_ip,
    }


def talos_upgrade_many(
    env: Environment,
    settings: Settings | None = None,
    db: Session | None = None,
    *,
    image: str | None = None,
    mode: str = "rolling",
    names: list[str] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Upgrade many Talos nodes (parallel / sequential / rolling).

    ``rolling`` is one machine at a time. This path does not evacuate
    workloads before the upgrade.
    """
    settings = settings or get_settings()
    mode_norm = str(mode or "rolling").strip().lower() or "rolling"
    if mode_norm not in {"parallel", "sequential", "rolling"}:
        return {
            "ok": False,
            "dry_run": dry_run,
            "error": "invalid mode",
            "mode": mode_norm,
            "results": [],
        }

    targets: list[str] = []
    if names:
        targets = [str(n).strip() for n in names if str(n).strip()]
    else:
        try:
            current = envconfig.get_current(db, env) if db else None
            doc = current[0] if current else {}
            servers = doc.get("servers") if isinstance(doc, dict) else None
            if isinstance(servers, dict):
                targets = [str(h) for h in servers.keys()]
        except Exception:  # noqa: BLE001
            targets = []
    if not targets:
        return {
            "ok": False,
            "dry_run": dry_run,
            "error": "no nodes to upgrade",
            "mode": mode_norm,
            "results": [],
        }

    results: list[dict[str, Any]] = []
    if mode_norm == "parallel":
        with ThreadPoolExecutor(max_workers=min(16, len(targets))) as pool:
            futs = {
                pool.submit(
                    talos_upgrade,
                    env,
                    name,
                    settings,
                    db,
                    image,
                    dry_run=dry_run,
                ): name
                for name in targets
            }
            for fut in as_completed(futs):
                name = futs[fut]
                try:
                    results.append(fut.result())
                except Exception as exc:  # noqa: BLE001
                    results.append(
                        {
                            "ok": False,
                            "dry_run": dry_run,
                            "error": str(exc)[:160],
                            "node": name,
                        }
                    )
    else:
        for name in targets:
            results.append(
                talos_upgrade(
                    env, name, settings, db, image, dry_run=dry_run
                )
            )

    ok = all(bool(r.get("ok")) for r in results)
    failed = [r for r in results if not r.get("ok")]
    return {
        "ok": ok,
        "dry_run": dry_run,
        "error": None if ok else (failed[0].get("error") if failed else "upgrade failed"),
        "message": (
            f"{'[dry-run] would upgrade' if dry_run else 'upgrade'} "
            f"{len(targets)} node(s) mode={mode_norm}"
            + ("" if ok else f" ({len(failed)} failed)")
        ),
        "mode": mode_norm,
        "results": results,
    }


def valid_service_id(value: str) -> bool:
    raw = str(value or "").strip()
    return bool(raw) and _SERVICE_RE.match(raw) is not None


def valid_events_since(value: str) -> bool:
    raw = str(value or "").strip()
    return bool(raw) and _SINCE_RE.match(raw) is not None


def _clip_lines(text: str, limit: int) -> str:
    lines = (text or "").splitlines()
    if len(lines) > limit:
        return "\n".join(lines[-limit:])
    return text or ""


def _proc_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def _prepare_talos(
    env: Environment,
    name: str,
    settings: Settings | None,
    db: Session | None,
) -> dict[str, Any]:
    """Resolve talosctl + node endpoint. Never raises."""
    try:
        settings = settings or get_settings()
        talosconfig, node_ip = _node_public_ip(env, settings, db, name)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:160], "node": name, "ip": None}
    if not talosconfig or not node_ip:
        return {
            "ok": False,
            "error": "talosconfig or node IP missing",
            "node": name,
            "ip": node_ip,
        }
    if not valid_node_endpoint(node_ip):
        return {
            "ok": False,
            "error": "invalid node address",
            "node": name,
            "ip": node_ip,
        }
    talosctl = shutil.which("talosctl")
    if not talosctl:
        return {
            "ok": False,
            "error": "talosctl not found",
            "node": name,
            "ip": node_ip,
        }
    return {
        "ok": True,
        "error": None,
        "node": name,
        "ip": node_ip,
        "talosctl": talosctl,
        "talosconfig": talosconfig,
    }


def _run_talosctl(
    talosctl: str,
    talosconfig: str,
    node_ip: str,
    args: list[str],
    *,
    timeout: int = 20,
    strict: bool = False,
) -> dict[str, Any]:
    """Run one talosctl command. Never raises.

    Reads treat any stdout as success (matching dmesg). Mutates pass
    ``strict=True`` so a non-zero exit is always ``ok: false``.
    """
    argv = [talosctl, *args, "--nodes", node_ip, "--talosconfig", talosconfig]
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        text = _proc_text(exc.stdout)
        if text.strip() and not strict:
            return {"ok": True, "error": None, "text": text, "timed_out": True}
        return {"ok": False, "error": f"timeout after {timeout}s", "text": text}
    except OSError as exc:
        return {"ok": False, "error": str(exc)[:160], "text": ""}
    text = proc.stdout or ""
    err = (proc.stderr or "").strip()
    if proc.returncode != 0 and (strict or not text.strip()):
        return {
            "ok": False,
            "error": (err or f"exit {proc.returncode}")[:200],
            "text": text,
        }
    return {
        "ok": True,
        "error": (
            None if proc.returncode == 0 else (err or f"exit {proc.returncode}")[:200]
        ),
        "text": text,
    }


def _setup_error(result: dict[str, Any]) -> bool:
    err = str(result.get("error") or "")
    return err in {
        "talosctl not found",
        "talosconfig or node IP missing",
        "invalid node address",
    }


def _node_exec(
    env: Environment,
    name: str,
    settings: Settings | None,
    db: Session | None,
    args: list[str],
    *,
    timeout: int = 20,
    strict: bool = False,
) -> dict[str, Any]:
    ctx = _prepare_talos(env, name, settings, db)
    base = {"node": name, "ip": ctx.get("ip")}
    if not ctx.get("ok"):
        return {**base, "ok": False, "error": ctx.get("error"), "text": ""}
    result = _run_talosctl(
        ctx["talosctl"],
        ctx["talosconfig"],
        ctx["ip"],
        args,
        timeout=timeout,
        strict=strict,
    )
    return {**base, **result}


def talos_health(
    env: Environment,
    name: str,
    settings: Settings | None = None,
    db: Session | None = None,
) -> dict[str, Any]:
    return _node_exec(env, name, settings, db, ["health", "--wait=false"], timeout=30)


def talos_etcd(
    env: Environment,
    name: str,
    settings: Settings | None = None,
    db: Session | None = None,
) -> dict[str, Any]:
    members = _node_exec(env, name, settings, db, ["etcd", "members"])
    if not members.get("ok") and _setup_error(members):
        return {**members, "members": "", "status": ""}
    status = _node_exec(env, name, settings, db, ["etcd", "status"])
    parts = []
    if members.get("text"):
        parts.append(members["text"].rstrip())
    if status.get("ok") and status.get("text"):
        parts.append(status["text"].rstrip())
    ok = bool(members.get("ok"))
    error = None if ok else members.get("error")
    if ok and not status.get("ok") and status.get("error"):
        # members worked; status is optional (workers, older talosctl)
        extra = str(status.get("error") or "")
        error = f"etcd status: {extra}"[:200] if extra else None
    return {
        "ok": ok,
        "error": error,
        "text": "\n".join(parts),
        "members": members.get("text") or "",
        "status": (status.get("text") or "") if status.get("ok") else "",
        "status_error": None if status.get("ok") else status.get("error"),
        "node": members.get("node") or name,
        "ip": members.get("ip"),
    }


def talos_machineconfig(
    env: Environment,
    name: str,
    settings: Settings | None = None,
    db: Session | None = None,
) -> dict[str, Any]:
    result = _node_exec(env, name, settings, db, ["get", "machineconfig", "-o", "yaml"])
    if not result.get("ok") and not _setup_error(result):
        fallback = _node_exec(env, name, settings, db, ["get", "mc", "-o", "yaml"])
        if fallback.get("ok"):
            result = fallback
    yaml_text = result.get("text") or ""
    return {
        "ok": result.get("ok"),
        "error": result.get("error"),
        "text": yaml_text,
        "yaml": yaml_text,
        "node": result.get("node") or name,
        "ip": result.get("ip"),
    }


def talos_disks(
    env: Environment,
    name: str,
    settings: Settings | None = None,
    db: Session | None = None,
) -> dict[str, Any]:
    disks = _node_exec(env, name, settings, db, ["get", "disks", "-o", "yaml"])
    if _setup_error(disks):
        return {
            "ok": False,
            "error": disks.get("error"),
            "text": "",
            "disks": "",
            "volumes": "",
            "node": disks.get("node") or name,
            "ip": disks.get("ip"),
        }
    if not disks.get("ok"):
        alt = _node_exec(env, name, settings, db, ["get", "disks"])
        if alt.get("ok") or alt.get("text"):
            disks = alt
    volumes = _node_exec(
        env, name, settings, db, ["get", "discoveredvolumes", "-o", "yaml"]
    )
    if not volumes.get("ok"):
        alt_vol = _node_exec(env, name, settings, db, ["get", "discoveredvolumes"])
        if alt_vol.get("ok") or alt_vol.get("text"):
            volumes = alt_vol
    parts = []
    if disks.get("text"):
        parts.append(disks["text"].rstrip())
    if volumes.get("ok") and volumes.get("text"):
        parts.append(volumes["text"].rstrip())
    ok = bool(disks.get("ok") or volumes.get("ok"))
    error = None
    if not ok:
        error = disks.get("error") or volumes.get("error")
    return {
        "ok": ok,
        "error": error,
        "text": "\n".join(parts),
        "disks": disks.get("text") or "",
        "volumes": (volumes.get("text") or "") if volumes.get("ok") else "",
        "volumes_error": None if volumes.get("ok") else volumes.get("error"),
        "node": disks.get("node") or name,
        "ip": disks.get("ip"),
    }


def talos_logs(
    env: Environment,
    name: str,
    settings: Settings | None = None,
    db: Session | None = None,
    service: str = "kubelet",
) -> dict[str, Any]:
    svc = str(service or "").strip() or "kubelet"
    if not valid_service_id(svc):
        return {
            "ok": False,
            "error": "invalid service",
            "text": "",
            "service": svc,
            "node": name,
            "ip": None,
        }
    result = _node_exec(
        env, name, settings, db, ["logs", svc, "--tail", str(LOG_LINES)]
    )
    if not result.get("ok") and not _setup_error(result):
        # older talosctl: no --tail
        alt = _node_exec(env, name, settings, db, ["logs", svc])
        if alt.get("ok") or alt.get("text"):
            result = alt
    text = _clip_lines(result.get("text") or "", LOG_LINES)
    return {
        "ok": result.get("ok"),
        "error": result.get("error"),
        "text": text,
        "service": svc,
        "node": result.get("node") or name,
        "ip": result.get("ip"),
    }


def talos_resources(
    env: Environment,
    name: str,
    settings: Settings | None = None,
    db: Session | None = None,
) -> dict[str, Any]:
    combined = _node_exec(
        env,
        name,
        settings,
        db,
        ["get", "meminfo,cpustat,runtimes,networkstatus", "-o", "yaml"],
        timeout=15,
    )
    results: dict[str, Any] = {}
    parts: list[str] = []
    if combined.get("ok") and combined.get("text"):
        results["resources"] = {
            "ok": True,
            "error": None,
            "text": combined["text"],
        }
        parts.append(combined["text"].rstrip())
    else:
        if _setup_error(combined):
            return {
                "ok": False,
                "error": combined.get("error"),
                "text": "",
                "results": {},
                "node": combined.get("node") or name,
                "ip": combined.get("ip"),
            }
        for key, args in (
            ("meminfo", ["get", "meminfo", "-o", "yaml"]),
            ("cpustat", ["get", "cpustat", "-o", "yaml"]),
            ("runtimes", ["get", "runtimes", "-o", "yaml"]),
            ("networkstatus", ["get", "networkstatus", "-o", "yaml"]),
        ):
            item = _node_exec(env, name, settings, db, args, timeout=8)
            results[key] = {
                "ok": bool(item.get("ok")),
                "error": item.get("error"),
                "text": item.get("text") or "",
            }
            if item.get("ok") and item.get("text"):
                parts.append(item["text"].rstrip())
        if not results.get("meminfo", {}).get("ok"):
            mem = _node_exec(env, name, settings, db, ["memory"], timeout=8)
            results["memory"] = {
                "ok": bool(mem.get("ok")),
                "error": mem.get("error"),
                "text": mem.get("text") or "",
            }
            if mem.get("ok") and mem.get("text"):
                parts.append(mem["text"].rstrip())
        if not results.get("networkstatus", {}).get("ok"):
            for key, args in (("netstat", ["netstat"]), ("interfaces", ["interfaces"])):
                item = _node_exec(env, name, settings, db, args, timeout=8)
                results[key] = {
                    "ok": bool(item.get("ok")),
                    "error": item.get("error"),
                    "text": item.get("text") or "",
                }
                if item.get("ok") and item.get("text"):
                    parts.append(item["text"].rstrip())
    ok = any(isinstance(v, dict) and v.get("ok") for v in results.values())
    error = None if ok else (combined.get("error") or "no resource commands available")
    return {
        "ok": ok,
        "error": error,
        "text": "\n".join(parts),
        "results": results,
        "node": combined.get("node") or name,
        "ip": combined.get("ip"),
    }


def talos_events(
    env: Environment,
    name: str,
    settings: Settings | None = None,
    db: Session | None = None,
    since: str | None = None,
) -> dict[str, Any]:
    since_raw = str(since or "").strip()
    if since_raw and not valid_events_since(since_raw):
        return {
            "ok": False,
            "error": "invalid since",
            "text": "",
            "node": name,
            "ip": None,
        }
    args = ["events", "--tail", "80"]
    if since_raw:
        args.extend(["--since", since_raw])
    args.extend(["--duration", "1s"])
    result = _node_exec(env, name, settings, db, args, timeout=EVENTS_TIMEOUT)
    if not result.get("ok") and not _setup_error(result):
        retry_args = ["events", "--tail", "80"]
        if since_raw:
            retry_args.extend(["--since", since_raw])
        result = _node_exec(env, name, settings, db, retry_args, timeout=EVENTS_TIMEOUT)
    fallback = None
    if not result.get("ok") and not _setup_error(result):
        dmesg = _node_exec(env, name, settings, db, ["dmesg"], timeout=20)
        if dmesg.get("ok") or dmesg.get("text"):
            result = dmesg
            fallback = "dmesg"
    text = _clip_lines(result.get("text") or "", LOG_LINES)
    payload = {
        "ok": result.get("ok"),
        "error": result.get("error"),
        "text": text,
        "node": result.get("node") or name,
        "ip": result.get("ip"),
    }
    if fallback:
        payload["fallback"] = fallback
    return payload


def talos_containers(
    env: Environment,
    name: str,
    settings: Settings | None = None,
    db: Session | None = None,
) -> dict[str, Any]:
    result = _node_exec(env, name, settings, db, ["containers"])
    if not result.get("ok") and not _setup_error(result):
        alt = _node_exec(env, name, settings, db, ["get", "containers"])
        if alt.get("ok") or alt.get("text"):
            result = alt
    return {
        "ok": result.get("ok"),
        "error": result.get("error"),
        "text": result.get("text") or "",
        "node": result.get("node") or name,
        "ip": result.get("ip"),
    }


def talos_shutdown(
    env: Environment,
    name: str,
    settings: Settings | None = None,
    db: Session | None = None,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    if dry_run:
        ctx = _prepare_talos(env, name, settings, db)
        ip = ctx.get("ip")
        if not ctx.get("ok"):
            return {
                "ok": False,
                "dry_run": True,
                "error": ctx.get("error") or "shutdown failed",
                "node": name,
                "ip": ip,
            }
        return {
            "ok": True,
            "dry_run": True,
            "error": None,
            "message": f"[dry-run] would shutdown {name} ({ip})",
            "node": name,
            "ip": ip,
        }
    result = _node_exec(
        env, name, settings, db, ["shutdown", "--wait=false"], timeout=20, strict=True
    )
    if not result.get("ok"):
        return {
            "ok": False,
            "dry_run": False,
            "error": result.get("error") or "shutdown failed",
            "node": result.get("node") or name,
            "ip": result.get("ip"),
        }
    return {
        "ok": True,
        "dry_run": False,
        "error": None,
        "message": f"shutdown requested for {name} ({result.get('ip')})",
        "node": result.get("node") or name,
        "ip": result.get("ip"),
    }


def talos_reset(
    env: Environment,
    name: str,
    settings: Settings | None = None,
    db: Session | None = None,
    *,
    graceful: bool = True,
    reboot: bool = False,
    wipe: bool = True,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Map onto ``talosctl reset --wait=false``.

    - graceful: ``--graceful`` / ``--graceful=false`` (etcd leave if possible)
    - reboot: ``--reboot`` when true (otherwise the node halts)
    - wipe: ``--wipe-mode=system-disk`` when true, ``--wipe-mode=none`` when false
    """
    flags = {
        "graceful": graceful,
        "reboot": reboot,
        "wipe": wipe,
    }
    if dry_run:
        ctx = _prepare_talos(env, name, settings, db)
        ip = ctx.get("ip")
        if not ctx.get("ok"):
            return {
                "ok": False,
                "dry_run": True,
                "error": ctx.get("error") or "reset failed",
                "node": name,
                "ip": ip,
                **flags,
            }
        return {
            "ok": True,
            "dry_run": True,
            "error": None,
            "message": (
                f"[dry-run] would reset {name} ({ip}) "
                f"graceful={graceful} reboot={reboot} wipe={wipe}"
            ),
            "node": name,
            "ip": ip,
            **flags,
        }
    args = [
        "reset",
        "--wait=false",
        "--graceful=true" if graceful else "--graceful=false",
        "--wipe-mode=system-disk" if wipe else "--wipe-mode=none",
    ]
    if reboot:
        args.append("--reboot")
    result = _node_exec(env, name, settings, db, args, timeout=30, strict=True)
    if not result.get("ok"):
        return {
            "ok": False,
            "dry_run": False,
            "error": result.get("error") or "reset failed",
            "node": result.get("node") or name,
            "ip": result.get("ip"),
            **flags,
        }
    return {
        "ok": True,
        "dry_run": False,
        "error": None,
        "message": f"reset requested for {name} ({result.get('ip')})",
        "node": result.get("node") or name,
        "ip": result.get("ip"),
        **flags,
    }


def talos_apply_config(
    env: Environment,
    name: str,
    settings: Settings | None = None,
    db: Session | None = None,
    *,
    yaml_text: str,
    mode: str = "auto",
    dry_run: bool = False,
) -> dict[str, Any]:
    raw = str(yaml_text or "")
    if not raw.strip():
        return {
            "ok": False,
            "dry_run": dry_run,
            "error": "yaml is required",
            "node": name,
            "ip": None,
            "mode": mode,
        }
    if len(raw) > APPLY_YAML_MAX:
        return {
            "ok": False,
            "dry_run": dry_run,
            "error": "yaml too large",
            "node": name,
            "ip": None,
            "mode": mode,
        }
    mode_norm = str(mode or "auto").strip().lower() or "auto"
    if mode_norm not in APPLY_MODES:
        return {
            "ok": False,
            "dry_run": dry_run,
            "error": "invalid mode",
            "node": name,
            "ip": None,
            "mode": mode_norm,
        }
    ctx = _prepare_talos(env, name, settings, db)
    if not ctx.get("ok"):
        return {
            "ok": False,
            "dry_run": dry_run,
            "error": ctx.get("error"),
            "node": name,
            "ip": ctx.get("ip"),
            "mode": mode_norm,
        }
    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "error": None,
            "message": (
                f"[dry-run] would apply-config on {name} ({ctx.get('ip')}) "
                f"mode={mode_norm} ({len(raw)} bytes)"
            ),
            "node": name,
            "ip": ctx.get("ip"),
            "mode": mode_norm,
        }
    path = None
    try:
        fd, path = tempfile.mkstemp(prefix="talos-mc-", suffix=".yaml")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(raw)
        result = _run_talosctl(
            ctx["talosctl"],
            ctx["talosconfig"],
            ctx["ip"],
            ["apply-config", "--file", path, "--mode", mode_norm],
            timeout=APPLY_TIMEOUT,
            strict=True,
        )
    except OSError as exc:
        return {
            "ok": False,
            "error": str(exc)[:160],
            "node": name,
            "ip": ctx.get("ip"),
            "mode": mode_norm,
        }
    finally:
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass
    if not result.get("ok"):
        return {
            "ok": False,
            "dry_run": False,
            "error": result.get("error") or "apply-config failed",
            "node": name,
            "ip": ctx.get("ip"),
            "mode": mode_norm,
        }
    return {
        "ok": True,
        "dry_run": False,
        "error": None,
        "message": f"config applied on {name} ({ctx.get('ip')}) mode={mode_norm}",
        "node": name,
        "ip": ctx.get("ip"),
        "mode": mode_norm,
    }


def talos_service_action(
    env: Environment,
    name: str,
    service_id: str,
    action: str,
    settings: Settings | None = None,
    db: Session | None = None,
) -> dict[str, Any]:
    svc = str(service_id or "").strip()
    act = str(action or "").strip().lower()
    if not valid_service_id(svc):
        return {
            "ok": False,
            "error": "invalid service",
            "node": name,
            "ip": None,
            "service": svc,
            "action": act,
        }
    if act not in SERVICE_ACTIONS:
        return {
            "ok": False,
            "error": "invalid action",
            "node": name,
            "ip": None,
            "service": svc,
            "action": act,
        }
    result = _node_exec(env, name, settings, db, ["service", svc, act], strict=True)
    if not result.get("ok"):
        return {
            "ok": False,
            "error": result.get("error") or f"{act} failed",
            "node": result.get("node") or name,
            "ip": result.get("ip"),
            "service": svc,
            "action": act,
        }
    return {
        "ok": True,
        "error": None,
        "message": f"{act} {svc} on {name} ({result.get('ip')})",
        "node": result.get("node") or name,
        "ip": result.get("ip"),
        "service": svc,
        "action": act,
        "text": result.get("text") or "",
    }
