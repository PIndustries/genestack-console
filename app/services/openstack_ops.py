"""OpenStack operations for a genestack environment's cloud.

Primary path: the console process calls OpenStack REST (Keystone token,
then Nova/Neutron/Cinder/Glance) through the Kubernetes apiserver service
proxy — see ``app.services.osclient``. That is the Cloud tab backend.

Fallback: ``kubectl exec`` into ``openstack-admin-client`` running the CLI,
used when the env has no kubeconfig or the API proxy is unreachable.

Read paths never raise: failures return source="unavailable" with a short
error string. Mutating actions validate ids/names before any call.
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Callable

from app.config import Settings, get_settings
from app.models import Environment
from app.services import genestack_bridge as bridge
from app.services.envcontext import EnvContext, build_context
from app.services.executors import pick_executor
from app.services.osclient import OpenStackClient, OpenStackError

LogFn = Callable[[str], None]

# Genestack's admin tooling pod (bin/setup-openstack-rc.sh equivalent in-cluster).
OS_NAMESPACE = "openstack"
ADMIN_CLIENT_POD = "openstack-admin-client"

READ_TIMEOUT = 15
INVENTORY_TIMEOUT = 25
INVENTORY_CACHE_TTL = 20.0
SERVER_ACTIONS = (
    "start",
    "stop",
    "reboot",
    "hard-reboot",
    "delete",
    "pause",
    "unpause",
    "suspend",
    "resume",
    "lock",
    "unlock",
    "rescue",
    "unrescue",
    "shelve",
    "unshelve",
    "shelve-offload",
    "confirm-resize",
    "revert-resize",
)
_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}

# Nova server ids are UUIDs; reject everything else before touching a shell.
SERVER_ID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-" r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
# Image/flavor/network names may include spaces (e.g. "Cirros 0.6.2 64-bit").
REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 .:_-]{0,80}$")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,62}$")
CIDR_RE = re.compile(r"^(\d{1,3}\.){3}\d{1,3}/(\d|[12]\d|3[0-2])$")
IPV6_CIDR_RE = re.compile(r"^[0-9a-fA-F:]*:[0-9a-fA-F:]+/(\d|[1-9]\d|1[0-2]\d)$")
SG_DIRECTIONS = ("ingress", "egress")
SG_ETHERTYPES = ("IPv4", "IPv6")
SG_PROTOCOLS = ("tcp", "udp", "icmp")
QUOTA_COMPUTE_KEYS = ("instances", "cores", "ram")
QUOTA_NETWORK_KEYS = (
    "network",
    "subnet",
    "router",
    "floatingip",
    "security_group",
    "port",
)
QUOTA_CLI_FLAGS = {
    "instances": "--instances",
    "cores": "--cores",
    "ram": "--ram",
    "network": "--networks",
    "subnet": "--subnets",
    "router": "--routers",
    "floatingip": "--floating-ips",
    "security_group": "--secgroups",
    "port": "--ports",
}
DISK_FORMATS = (
    "ami",
    "ari",
    "aki",
    "vhd",
    "vhdx",
    "vmdk",
    "raw",
    "qcow2",
    "vdi",
    "ploop",
    "iso",
)
CONTAINER_FORMATS = ("ami", "ari", "aki", "bare", "ovf", "ova", "docker")
IMAGE_VISIBILITIES = ("public", "private", "shared", "community")
DESC_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._:,/@()\[\]+-]{0,254}$")
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s.][^@\s]{0,253}$")
PUBKEY_RE = re.compile(
    r"^(ssh-(rsa|ed25519|dss)|ecdsa-sha2-nistp(256|384|521)|"
    r"sk-ssh-ed25519@openssh\.com|sk-ecdsa-sha2-nistp256@openssh\.com)"
    r" [A-Za-z0-9+/=]+(?: [^\n]*)?$"
)


def _log(log: LogFn | None, msg: str) -> None:
    if log:
        log(msg)


def _run_admin(
    ctx: EnvContext,
    inner: list[str],
    *,
    dry_run: bool = False,
    timeout: int = READ_TIMEOUT,
    log: LogFn | None = None,
    input_text: str | None = None,
) -> dict[str, Any]:
    """kubectl exec into the admin client pod. ``inner`` is the argv after ``--``."""
    argv = [
        "kubectl",
        "exec",
        "-n",
        OS_NAMESPACE,
        ADMIN_CLIENT_POD,
        "--",
        *inner,
    ]
    if input_text is not None:
        argv = [
            "kubectl",
            "exec",
            "-i",
            "-n",
            OS_NAMESPACE,
            ADMIN_CLIENT_POD,
            "--",
            *inner,
        ]
    agent_env_id = pick_executor(ctx.environment, ctx).agent_env_id
    return bridge.run_command(
        argv,
        dry_run=dry_run,
        timeout=timeout,
        extra_env=ctx.subprocess_env(),
        ssh_target=ctx.ssh_target,
        remote_env=ctx.remote_env(),
        agent_env_id=agent_env_id,
        input_text=input_text,
        log=log,
    )


def _run_openstack(
    ctx: EnvContext,
    args: list[str],
    *,
    dry_run: bool = False,
    timeout: int = READ_TIMEOUT,
    log: LogFn | None = None,
) -> dict[str, Any]:
    """Run ``openstack <args>`` in the env's admin client pod via the bridge.

    The kubeconfig travels via KUBECONFIG in subprocess_env (local) or
    remote_env (ssh deploy host) — never as a console-local path in argv.
    """
    return _run_admin(
        ctx, ["openstack", *args], dry_run=dry_run, timeout=timeout, log=log
    )


def _short_error(result: dict[str, Any]) -> str:
    detail = (
        (result.get("stderr") or "").strip()
        or (result.get("stdout") or "").strip()
        or str(result.get("message") or "").strip()
        or f"exit code {result.get('returncode')}"
    )
    return detail.splitlines()[0][:200]


def _norm_keys(row: dict[str, Any]) -> dict[str, Any]:
    """Lowercase/underscore openstack CLI JSON keys ('Power State' -> power_state)."""
    return {str(k).strip().lower().replace(" ", "_"): v for k, v in row.items()}


def _server_row(raw: dict[str, Any]) -> dict[str, Any]:
    r = _norm_keys(raw)
    project_id = r.get("project_id") or r.get("tenant_id")
    project_name = r.get("project_name")
    if project_id is not None:
        project_id = str(project_id).strip() or None
    if isinstance(project_name, str):
        project_name = project_name.strip() or None
    else:
        project_name = None
    return {
        "id": r.get("id"),
        "name": r.get("name"),
        "status": r.get("status"),
        "power_state": r.get("power_state"),
        "flavor": r.get("flavor_name") or r.get("flavor") or r.get("flavor_id"),
        "image": r.get("image_name") or r.get("image") or r.get("image_id"),
        "addresses": r.get("networks") or r.get("addresses") or {},
        "created": r.get("created"),
        "host": r.get("host") or r.get("hypervisor_hostname") or r.get("compute_host"),
        "project_id": project_id,
        "project_name": project_name,
    }


def list_servers(
    env: Environment,
    settings: Settings | None = None,
    *,
    log: LogFn | None = None,
) -> dict[str, Any]:
    """List Nova servers in the env's cloud. Never raises.

    Prefers OpenStack REST (same kube-apiserver proxy as ``cloud_inventory``)
    when a kubeconfig exists. Falls back to ``kubectl exec`` of the CLI when
    there is no kubeconfig or the proxy fails.

    Returns {vms: [...], source: "openstack-api"|"live", error: None} on
    success, {vms: [], source: "unavailable", error: "<short msg>"} on failure.
    """
    settings = settings or get_settings()
    ctx = build_context(env, settings)
    try:
        native = _servers_via_api(ctx)
        if native is not None:
            return {"vms": native, "source": "openstack-api", "error": None}
        result = _run_openstack(
            ctx,
            ["server", "list", "--all-projects", "--long", "-f", "json"],
            timeout=READ_TIMEOUT,
            log=log,
        )
    except Exception as exc:  # noqa: BLE001 — read path must never raise
        return {"vms": [], "source": "unavailable", "error": str(exc)[:200]}
    finally:
        ctx.cleanup()

    if result.get("returncode") != 0:
        return {"vms": [], "source": "unavailable", "error": _short_error(result)}
    try:
        rows = json.loads(result.get("stdout") or "[]")
    except json.JSONDecodeError as exc:
        return {
            "vms": [],
            "source": "unavailable",
            "error": f"invalid openstack output: {exc}"[:200],
        }
    if not isinstance(rows, list):
        return {
            "vms": [],
            "source": "unavailable",
            "error": "unexpected openstack output",
        }
    return {
        "vms": [_server_row(r) for r in rows if isinstance(r, dict)],
        "source": "live",
        "error": None,
    }


def validate_server_id(server_id: Any) -> str | None:
    """Return an error message when server_id is not a UUID, else None."""
    value = str(server_id or "").strip()
    if not SERVER_ID_RE.match(value):
        return f"invalid server_id {value!r} — must be a UUID"
    return None


def server_action(
    env: Environment,
    settings: Settings | None = None,
    action: str = "",
    server_id: Any = "",
    *,
    dry_run: bool = True,
    timeout: int = 600,
    log: LogFn | None = None,
) -> dict[str, Any]:
    """Run ``openstack server <action> <server_id>`` for one VM.

    The server id is validated (UUID) before any subprocess is built, and the
    command is always an argv list — no shell interpolation. Honors dry_run:
    logs what would run and executes nothing.
    """
    settings = settings or get_settings()
    if action not in SERVER_ACTIONS:
        return {
            "ok": False,
            "error": f"unknown server action {action!r} (valid: {', '.join(SERVER_ACTIONS)})",
            "returncode": 2,
        }
    server_id = str(server_id or "").strip()
    error = validate_server_id(server_id)
    if error:
        _log(log, f"[denied] {error}")
        return {
            "ok": False,
            "error": error,
            "returncode": 2,
            "action": action,
            "server_id": server_id,
        }

    ctx = build_context(env, settings)
    try:
        if not dry_run:

            def _do(client: OpenStackClient) -> dict[str, Any]:
                if action == "delete":
                    client.server_delete(server_id)
                else:
                    client.server_action(server_id, action)
                return {}

            native = _api_call(ctx, _do)
            if native is not None:
                native["action"] = action
                native["server_id"] = server_id
                if native.get("ok"):
                    native["message"] = f"server {action} {server_id} accepted"
                return native
        argv = _server_cli_args(action, server_id)
        result = _run_openstack(
            ctx,
            argv,
            dry_run=dry_run,
            timeout=timeout,
            log=log,
        )
    finally:
        ctx.cleanup()

    rc = result.get("returncode")
    ok = bool(result.get("dry_run")) or rc == 0
    result["ok"] = ok
    result["action"] = action
    result["server_id"] = server_id
    if not result.get("message") or not ok:
        result["message"] = (
            f"server {action} {server_id} accepted"
            if ok
            else f"server {action} {server_id} failed (rc={rc})"
        )
    return result


def validate_uuid(value: Any, *, field: str = "id") -> str | None:
    text = str(value or "").strip()
    if not SERVER_ID_RE.match(text):
        return f"invalid {field} {text!r} — must be a UUID"
    return None


def validate_ref(value: Any, *, field: str) -> str | None:
    text = str(value or "").strip()
    if not text or not REF_RE.match(text):
        return f"invalid {field} {text!r}"
    return None


def validate_name(value: Any, *, field: str = "name") -> str | None:
    text = str(value or "").strip()
    if not text or not NAME_RE.match(text):
        return f"invalid {field} {text!r}"
    return None


def validate_desc(
    value: Any, *, field: str = "description", required: bool = False
) -> str | None:
    text = str(value or "").strip()
    if not text:
        return f"invalid {field} {text!r}" if required else None
    if not DESC_RE.match(text):
        return f"invalid {field} {text!r}"
    return None


def validate_http_url(
    value: Any, *, field: str = "url", required: bool = False
) -> str | None:
    text = str(value or "").strip()
    if not text:
        return f"invalid {field} {text!r}" if required else None
    if not (text.startswith("https://") or text.startswith("http://")):
        return f"invalid {field} — must be http(s) URL"
    if any(ch.isspace() for ch in text) or len(text) > 2048:
        return f"invalid {field}"
    return None


def validate_public_key(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if "\n" in text or "\r" in text or len(text) > 16384 or not PUBKEY_RE.match(text):
        return "invalid public_key"
    return None


def validate_password(value: Any) -> str | None:
    text = str(value or "")
    if len(text) < 8 or len(text) > 128:
        return "invalid password — must be 8..128 characters"
    return None


def validate_email(value: Any, *, field: str = "email") -> str | None:
    text = str(value or "").strip()
    if not text or not EMAIL_RE.match(text) or len(text) > 254:
        return f"invalid {field} {text!r}"
    return None


def _server_cli_args(action: str, server_id: str) -> list[str]:
    if action == "hard-reboot":
        return ["server", "reboot", "--hard", server_id]
    if action == "confirm-resize":
        return ["server", "resize", "--confirm", server_id]
    if action == "revert-resize":
        return ["server", "resize", "--revert", server_id]
    if action == "shelve-offload":
        return ["server", "shelve", server_id]
    return ["server", action, server_id]


def validate_cidr(
    value: Any, *, field: str = "cidr", required: bool = True
) -> str | None:
    text = str(value or "").strip()
    if not text:
        return f"invalid {field} {text!r}" if required else None
    if CIDR_RE.match(text):
        try:
            parts = text.split("/", 1)[0].split(".")
            if any(not (0 <= int(p) <= 255) for p in parts):
                return f"invalid {field} {text!r}"
        except ValueError:
            return f"invalid {field} {text!r}"
        return None
    if IPV6_CIDR_RE.match(text):
        try:
            prefix = int(text.rsplit("/", 1)[1])
        except (TypeError, ValueError):
            return f"invalid {field} {text!r}"
        if prefix > 128:
            return f"invalid {field} {text!r}"
        return None
    return f"invalid {field} {text!r}"


def _opt_port(value: Any, *, field: str) -> tuple[int | None, str | None]:
    if value is None or value == "":
        return None, None
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None, f"invalid {field} {value!r}"
    if n < 0 or n > 65535:
        return None, f"invalid {field} {value!r}"
    return n, None


def _opt_quota_int(value: Any, *, field: str) -> tuple[int | None, str | None]:
    if value is None or value == "":
        return None, None
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None, f"invalid {field} {value!r}"
    if n < -1 or n > 1_000_000:
        return None, f"invalid {field} {value!r}"
    return n, None


def _parse_rows(
    result: dict[str, Any], mapper
) -> tuple[list[dict[str, Any]], str | None]:
    if result.get("returncode") != 0:
        return [], _short_error(result)
    try:
        rows = json.loads(result.get("stdout") or "[]")
    except json.JSONDecodeError as exc:
        return [], f"invalid openstack output: {exc}"[:200]
    if not isinstance(rows, list):
        return [], "unexpected openstack output"
    return [mapper(r) for r in rows if isinstance(r, dict)], None


def _image_row(raw: dict[str, Any]) -> dict[str, Any]:
    r = _norm_keys(raw)
    return {
        "id": r.get("id"),
        "name": r.get("name"),
        "status": r.get("status"),
        "size": r.get("size") or r.get("disk_format"),
    }


def _flavor_row(raw: dict[str, Any]) -> dict[str, Any]:
    r = _norm_keys(raw)
    return {
        "id": r.get("id"),
        "name": r.get("name"),
        "vcpus": r.get("vcpus"),
        "ram": r.get("ram"),
        "disk": r.get("disk"),
        "public": r.get("is_public"),
    }


def _volume_row(raw: dict[str, Any]) -> dict[str, Any]:
    r = _norm_keys(raw)
    return {
        "id": r.get("id"),
        "name": r.get("name"),
        "status": r.get("status"),
        "size": r.get("size"),
        "attachments": r.get("attached_to") or r.get("attachments") or [],
    }


def _network_row(raw: dict[str, Any]) -> dict[str, Any]:
    r = _norm_keys(raw)
    return {
        "id": r.get("id"),
        "name": r.get("name"),
        "subnets": r.get("subnets") or [],
        "shared": r.get("shared"),
        "external": r.get("router_external") or r.get("external"),
        "status": r.get("status"),
        "project_id": r.get("project_id"),
    }


def _subnet_row(raw: dict[str, Any]) -> dict[str, Any]:
    r = _norm_keys(raw)
    return {
        "id": r.get("id"),
        "name": r.get("name"),
        "network": r.get("network") or r.get("network_id"),
        "cidr": r.get("subnet") or r.get("cidr"),
        "project_id": r.get("project_id"),
    }


def _router_row(raw: dict[str, Any]) -> dict[str, Any]:
    r = _norm_keys(raw)
    return {
        "id": r.get("id"),
        "name": r.get("name"),
        "status": r.get("status"),
        "external_gateway": r.get("external_gateway") or r.get("external_gateway_info"),
        "project_id": r.get("project_id"),
    }


def _port_row(raw: dict[str, Any]) -> dict[str, Any]:
    r = _norm_keys(raw)
    fixed_raw = r.get("fixed_ips") or r.get("fixed_ip_addresses") or []
    fixed = []
    if isinstance(fixed_raw, list):
        for item in fixed_raw:
            if isinstance(item, dict):
                ir = _norm_keys(item)
                fixed.append(
                    {
                        "ip": ir.get("ip") or ir.get("ip_address"),
                        "subnet_id": ir.get("subnet_id"),
                    }
                )
            elif isinstance(item, str):
                fixed.append({"ip": item, "subnet_id": None})
    sgs = r.get("security_groups") or r.get("security_group_ids") or []
    if not isinstance(sgs, list):
        sgs = []
    return {
        "id": r.get("id"),
        "name": r.get("name"),
        "status": r.get("status"),
        "network_id": r.get("network_id") or r.get("network"),
        "device_id": r.get("device_id"),
        "device_owner": r.get("device_owner"),
        "project_id": r.get("project_id") or r.get("tenant_id"),
        "mac": r.get("mac") or r.get("mac_address"),
        "fixed_ips": fixed,
        "security_groups": sgs,
    }


def _fip_row(raw: dict[str, Any]) -> dict[str, Any]:
    r = _norm_keys(raw)
    return {
        "id": r.get("id") or r.get("floating_ip_address"),
        "ip": r.get("floating_ip_address") or r.get("ip"),
        "fixed_ip": r.get("fixed_ip_address") or r.get("fixed_ip"),
        "port": r.get("port") or r.get("port_id"),
        "port_id": r.get("port_id") or r.get("port"),
        "status": r.get("status"),
        "floating_network_id": r.get("floating_network_id")
        or r.get("floating_network"),
        "router_id": r.get("router_id") or r.get("router"),
        "project_id": r.get("project_id") or r.get("tenant_id"),
    }


def _sg_rule_row(raw: dict[str, Any]) -> dict[str, Any]:
    r = _norm_keys(raw)
    return {
        "id": r.get("id"),
        "direction": r.get("direction"),
        "ethertype": r.get("ethertype") or r.get("ether_type"),
        "protocol": r.get("protocol"),
        "port_range_min": r.get("port_range_min"),
        "port_range_max": r.get("port_range_max"),
        "remote_ip_prefix": r.get("remote_ip_prefix"),
        "remote_group_id": r.get("remote_group_id"),
    }


def _sg_row(raw: dict[str, Any]) -> dict[str, Any]:
    r = _norm_keys(raw)
    rules_raw = r.get("rules") or r.get("security_group_rules") or []
    rules = [_sg_rule_row(item) for item in rules_raw if isinstance(item, dict)]
    return {
        "id": r.get("id"),
        "name": r.get("name"),
        "description": r.get("description"),
        "rules": rules,
    }


def _keypair_row(raw: dict[str, Any]) -> dict[str, Any]:
    r = _norm_keys(raw)
    return {
        "name": r.get("name"),
        "type": r.get("type") or r.get("fingerprint"),
        "fingerprint": r.get("fingerprint"),
    }


def _snapshot_row(raw: dict[str, Any]) -> dict[str, Any]:
    r = _norm_keys(raw)
    return {
        "id": r.get("id"),
        "name": r.get("name"),
        "status": r.get("status"),
        "size": r.get("size"),
        "volume_id": r.get("volume_id") or r.get("volume"),
        "created": r.get("created") or r.get("created_at"),
    }


def _project_row(raw: dict[str, Any]) -> dict[str, Any]:
    r = _norm_keys(raw)
    return {
        "id": r.get("id"),
        "name": r.get("name"),
        "enabled": r.get("enabled"),
        "domain": r.get("domain_id") or r.get("domain"),
    }


def _user_row(raw: dict[str, Any]) -> dict[str, Any]:
    r = _norm_keys(raw)
    return {
        "id": r.get("id"),
        "name": r.get("name"),
        "enabled": r.get("enabled"),
        "email": r.get("email"),
    }


# One OpenStack SDK process inside the admin-client pod (~2s) instead of
# twelve CLI invocations (~60s) that trip the browser's 30s fetch timeout.
_SDK_INVENTORY = r"""
import json
from openstack import connection

def _sec(fn):
    try:
        return {"error": None, "items": fn()}
    except Exception as exc:
        return {"error": str(exc)[:200], "items": []}

c = connection.from_config()
out = {}

def servers():
    rows = []
    for s in c.compute.servers(all_projects=True):
        flav = s.flavor or {}
        if not isinstance(flav, dict):
            flav = {"id": getattr(flav, "id", None), "original_name": getattr(flav, "name", None)}
        img = s.image or {}
        if img in (None, ""):
            img = {}
        elif not isinstance(img, dict):
            img = {"id": getattr(img, "id", None), "name": getattr(img, "name", None)}
        rows.append({
            "id": s.id,
            "name": s.name,
            "status": s.status,
            "power_state": getattr(s, "power_state", None),
            "flavor": flav.get("original_name") or flav.get("name") or flav.get("id"),
            "image": img.get("name") or img.get("id"),
            "addresses": s.addresses or {},
            "host": getattr(s, "hypervisor_hostname", None) or getattr(s, "compute_host", None),
            "created": str(getattr(s, "created_at", None) or "") or None,
            "project_id": getattr(s, "project_id", None),
            "project_name": getattr(s, "project_name", None),
            "key_name": getattr(s, "key_name", None),
        })
    return rows

out["servers"] = _sec(servers)
out["images"] = _sec(lambda: [{"id": i.id, "name": i.name, "status": i.status, "size": getattr(i, "size", None)} for i in c.image.images()])
out["flavors"] = _sec(lambda: [{"id": f.id, "name": f.name, "vcpus": f.vcpus, "ram": f.ram, "disk": f.disk, "public": getattr(f, "is_public", None)} for f in c.compute.flavors()])
out["volumes"] = _sec(lambda: [{"id": v.id, "name": v.name, "status": v.status, "size": v.size, "attachments": list(v.attachments or [])} for v in c.block_storage.volumes(all_projects=True)])
out["networks"] = _sec(lambda: [{"id": n.id, "name": n.name, "status": n.status, "external": bool(getattr(n, "is_router_external", False)), "shared": bool(getattr(n, "is_shared", False)), "subnets": list(n.subnet_ids or []), "project_id": getattr(n, "project_id", None)} for n in c.network.networks()])
out["subnets"] = _sec(lambda: [{"id": s.id, "name": s.name, "cidr": s.cidr, "network": s.network_id, "project_id": getattr(s, "project_id", None)} for s in c.network.subnets()])
out["routers"] = _sec(lambda: [{"id": r.id, "name": r.name, "status": r.status, "external_gateway": getattr(r, "external_gateway_info", None), "project_id": getattr(r, "project_id", None)} for r in c.network.routers()])

def ports():
    rows = []
    for p in c.network.ports():
        ips = []
        for ip in (getattr(p, "fixed_ips", None) or []):
            if isinstance(ip, dict):
                ips.append({"ip": ip.get("ip_address") or ip.get("ip"), "subnet_id": ip.get("subnet_id")})
            else:
                ips.append({"ip": getattr(ip, "ip_address", None) or getattr(ip, "ip", None), "subnet_id": getattr(ip, "subnet_id", None)})
        rows.append({
            "id": p.id,
            "name": p.name,
            "status": p.status,
            "network_id": p.network_id,
            "device_id": getattr(p, "device_id", None),
            "device_owner": getattr(p, "device_owner", None),
            "project_id": getattr(p, "project_id", None),
            "mac": getattr(p, "mac_address", None),
            "fixed_ips": ips,
            "security_groups": list(getattr(p, "security_group_ids", None) or getattr(p, "security_groups", None) or []),
        })
    return rows

out["ports"] = _sec(ports)
out["floating_ips"] = _sec(lambda: [{"id": f.id, "ip": f.floating_ip_address, "fixed_ip": f.fixed_ip_address, "port": f.port_id, "port_id": f.port_id, "floating_network_id": getattr(f, "floating_network_id", None), "router_id": getattr(f, "router_id", None), "project_id": getattr(f, "project_id", None), "status": f.status} for f in c.network.ips()])
out["security_groups"] = _sec(lambda: [{"id": g.id, "name": g.name, "description": g.description, "rules": [{"id": getattr(r, "id", None), "direction": getattr(r, "direction", None), "ethertype": getattr(r, "ether_type", None) or getattr(r, "ethertype", None), "protocol": getattr(r, "protocol", None), "port_range_min": getattr(r, "port_range_min", None), "port_range_max": getattr(r, "port_range_max", None), "remote_ip_prefix": getattr(r, "remote_ip_prefix", None), "remote_group_id": getattr(r, "remote_group_id", None)} for r in (getattr(g, "security_group_rules", None) or [])]} for g in c.network.security_groups()])
out["keypairs"] = _sec(lambda: [{"name": k.name, "fingerprint": getattr(k, "fingerprint", None)} for k in c.compute.keypairs()])
out["projects"] = _sec(lambda: [{"id": p.id, "name": p.name, "enabled": getattr(p, "is_enabled", None)} for p in c.identity.projects()])
out["users"] = _sec(lambda: [{"id": u.id, "name": u.name, "enabled": getattr(u, "is_enabled", None)} for u in c.identity.users()])
out["volume_snapshots"] = _sec(lambda: [{"id": s.id, "name": s.name, "status": s.status, "size": getattr(s, "size", None), "volume_id": getattr(s, "volume_id", None)} for s in c.block_storage.snapshots(all_projects=True)])
print(json.dumps(out))
"""

INVENTORY_SPECS: tuple[tuple[str, list[str], Any], ...] = (
    ("servers", ["server", "list", "--all-projects", "--long"], _server_row),
    ("images", ["image", "list"], _image_row),
    ("flavors", ["flavor", "list"], _flavor_row),
    ("volumes", ["volume", "list", "--all-projects"], _volume_row),
    ("networks", ["network", "list"], _network_row),
    ("subnets", ["subnet", "list"], _subnet_row),
    ("routers", ["router", "list"], _router_row),
    ("ports", ["port", "list"], _port_row),
    ("floating_ips", ["floating", "ip", "list"], _fip_row),
    ("security_groups", ["security", "group", "list"], _sg_row),
    ("keypairs", ["keypair", "list"], _keypair_row),
    ("projects", ["project", "list"], _project_row),
    ("users", ["user", "list"], _user_row),
    (
        "volume_snapshots",
        ["volume", "snapshot", "list", "--all-projects"],
        _snapshot_row,
    ),
)


def _kube_path(ctx: EnvContext) -> str | None:
    kube = ctx.kubeconfig
    if kube and os.path.isfile(kube):
        return kube
    return None


def _inventory_via_api(ctx: EnvContext) -> dict[str, Any] | None:
    kube = _kube_path(ctx)
    if not kube:
        return None
    try:
        with OpenStackClient(kube) as client:
            return client.inventory()
    except Exception:  # noqa: BLE001 — fall back to in-cluster CLI
        return None


def _servers_via_api(ctx: EnvContext) -> list[dict[str, Any]] | None:
    """Nova server list via REST. None = no kubeconfig or proxy failure."""
    kube = _kube_path(ctx)
    if not kube:
        return None
    try:
        with OpenStackClient(kube) as client:
            return client.list_servers()
    except Exception:  # noqa: BLE001 — fall back to in-cluster CLI
        return None


def _api_call(ctx: EnvContext, fn: Any) -> dict[str, Any] | None:
    """Run ``fn(client)`` against native OpenStack REST. None = use CLI fallback."""
    kube = _kube_path(ctx)
    if not kube:
        return None
    try:
        with OpenStackClient(kube) as client:
            payload = fn(client)
    except OpenStackError as exc:
        return {
            "ok": False,
            "error": str(exc)[:200],
            "returncode": 1,
            "source": "openstack-api",
        }
    except Exception:  # noqa: BLE001
        return None
    result: dict[str, Any] = {
        "ok": True,
        "error": None,
        "returncode": 0,
        "source": "openstack-api",
        "dry_run": False,
    }
    if isinstance(payload, dict):
        result.update(payload)
    return result


def _mutate(
    env: Environment,
    settings: Settings | None,
    *,
    action: str,
    dry_run: bool | None,
    timeout: int,
    log: LogFn | None,
    extra: dict[str, Any] | None,
    api_fn: Any,
    cli_args: list[str] | None = None,
    rest_only: bool = False,
) -> dict[str, Any]:
    """Validate-then-call helper: native REST, optional CLI fallback, dry-run skip."""
    settings = settings or get_settings()
    extra = dict(extra or {})
    if dry_run is None:
        dry_run = _ctx_dry_run(env, settings)
    if dry_run and (rest_only or not cli_args):
        return {
            "ok": True,
            "dry_run": True,
            "returncode": 0,
            "action": action,
            "message": f"{action} accepted",
            "source": "dry-run",
            **extra,
        }
    ctx = build_context(env, settings)
    try:
        if not dry_run:
            native = _api_call(ctx, api_fn)
            if native is not None:
                native.setdefault("action", action)
                for key, value in extra.items():
                    native.setdefault(key, value)
                if native.get("ok") and not native.get("message"):
                    native["message"] = f"{action} accepted"
                return native
        if rest_only or not cli_args:
            return {
                "ok": False,
                "error": "openstack API unavailable",
                "returncode": 1,
                "action": action,
                **extra,
            }
        result = _run_openstack(
            ctx, cli_args, dry_run=dry_run, timeout=timeout, log=log
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "error": str(exc)[:200],
            "returncode": 1,
            "action": action,
            **extra,
        }
    finally:
        ctx.cleanup()
    return _finish_action(result, action=action, **extra)


def invalidate_cloud_cache(env_id: str | None) -> None:
    if env_id:
        _CACHE.pop(str(env_id), None)


def cloud_inventory(
    env: Environment,
    settings: Settings | None = None,
    *,
    log: LogFn | None = None,
    refresh: bool = False,
) -> dict[str, Any]:
    """Horizon-style inventory of the env's cloud. Never raises."""
    settings = settings or get_settings()
    cache_key = str(getattr(env, "id", "") or "")
    if cache_key and not refresh:
        hit = _CACHE.get(cache_key)
        if hit is not None:
            ts, cached = hit
            if time.monotonic() - ts < INVENTORY_CACHE_TTL:
                return {**cached, "cached": True}
    ctx = build_context(env, settings)
    out: dict[str, Any] = {
        "available": False,
        "source": "unavailable",
        "error": None,
        "cached": False,
    }
    for key, _args, _mapper in INVENTORY_SPECS:
        out[key] = []
        out[f"{key}_error"] = None
    out["quotas"] = {"compute": {}, "network": {}}
    out["quotas_error"] = None
    out["load_balancers"] = []
    out["load_balancers_error"] = None
    out["load_balancers_available"] = False
    out["dns_zones"] = []
    out["dns_zones_error"] = None
    out["dns_zones_available"] = False
    out["secrets"] = []
    out["secrets_error"] = None
    out["secrets_available"] = False
    try:
        native = _inventory_via_api(ctx)
        if native is not None:
            if cache_key and native.get("available"):
                _CACHE[cache_key] = (
                    time.monotonic(),
                    {k: v for k, v in native.items() if k != "cached"},
                )
            return native
        errors: list[str] = []
        bundled = _run_admin(
            ctx,
            ["python3", "-"],
            timeout=25,
            log=log,
            input_text=_SDK_INVENTORY,
        )
        parsed: dict[str, Any] | None = None
        if bundled.get("returncode") == 0:
            try:
                payload = json.loads(bundled.get("stdout") or "{}")
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, dict):
                parsed = payload
        if parsed is None:
            out["error"] = _short_error(bundled) or "openstack sdk inventory failed"
            return out
        mappers = {key: mapper for key, _args, mapper in INVENTORY_SPECS}
        for key, mapper in mappers.items():
            section = parsed.get(key) or {}
            items = section.get("items") if isinstance(section, dict) else []
            err = (
                section.get("error") if isinstance(section, dict) else "invalid section"
            )
            if not isinstance(items, list):
                items = []
            out[key] = [mapper(r) for r in items if isinstance(r, dict)]
            out[f"{key}_error"] = err
            if err:
                errors.append(f"{key}: {err}")
        if not errors or any(out[k] for k, _a, _m in INVENTORY_SPECS):
            out["available"] = True
            out["source"] = "live"
        out["error"] = (
            "; ".join(errors[:3]) if errors and not out["available"] else None
        )
        if errors and out["available"]:
            out["error"] = None
        if cache_key and out.get("available"):
            _CACHE[cache_key] = (
                time.monotonic(),
                {k: v for k, v in out.items() if k != "cached"},
            )
        return out
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)[:200]
        return out
    finally:
        ctx.cleanup()


def _denied(error: str, **extra: Any) -> dict[str, Any]:
    body = {"ok": False, "error": error, "returncode": 2}
    body.update(extra)
    return body


def _finish_action(
    result: dict[str, Any], *, action: str, **extra: Any
) -> dict[str, Any]:
    rc = result.get("returncode")
    ok = bool(result.get("dry_run")) or rc == 0
    result["ok"] = ok
    result["action"] = action
    result.update(extra)
    if not result.get("message") or not ok:
        result["message"] = f"{action} accepted" if ok else f"{action} failed (rc={rc})"
    if not ok and not result.get("error"):
        result["error"] = _short_error(result)
    return result


def _ctx_dry_run(env: Environment, settings: Settings) -> bool:
    ctx = build_context(env, settings)
    try:
        return bool(ctx.dry_run)
    finally:
        ctx.cleanup()


def server_create(
    env: Environment,
    settings: Settings | None = None,
    *,
    name: str,
    image: str,
    flavor: str,
    network: str,
    key_name: str | None = None,
    dry_run: bool | None = None,
    timeout: int = 600,
    log: LogFn | None = None,
) -> dict[str, Any]:
    """Create a Nova server. Validates every argument before exec."""
    settings = settings or get_settings()
    err = (
        validate_name(name)
        or validate_ref(image, field="image")
        or validate_ref(flavor, field="flavor")
        or validate_ref(network, field="network")
    )
    if key_name:
        err = err or validate_ref(key_name, field="key_name")
    if err:
        _log(log, f"[denied] {err}")
        return _denied(err, action="create")
    if dry_run is None:
        dry_run = _ctx_dry_run(env, settings)
    args = [
        "server",
        "create",
        "--image",
        str(image).strip(),
        "--flavor",
        str(flavor).strip(),
        "--network",
        str(network).strip(),
    ]
    if key_name:
        args.extend(["--key-name", str(key_name).strip()])
    args.append(str(name).strip())
    ctx = build_context(env, settings)
    try:
        if not dry_run:
            native = _api_call(
                ctx,
                lambda client: {
                    "server": client.server_create(
                        name=str(name).strip(),
                        image=str(image).strip(),
                        flavor=str(flavor).strip(),
                        network=str(network).strip(),
                        key_name=str(key_name).strip() if key_name else None,
                    )
                },
            )
            if native is not None:
                native["action"] = "create"
                native["name"] = str(name).strip()
                if native.get("ok"):
                    native["message"] = "create accepted"
                return native
        result = _run_openstack(ctx, args, dry_run=dry_run, timeout=timeout, log=log)
    finally:
        ctx.cleanup()
    return _finish_action(result, action="create", name=str(name).strip())


def server_console(
    env: Environment,
    settings: Settings | None = None,
    server_id: Any = "",
    *,
    log: LogFn | None = None,
) -> dict[str, Any]:
    """Return the noVNC console URL for a server. Never raises."""
    settings = settings or get_settings()
    server_id = str(server_id or "").strip()
    error = validate_server_id(server_id)
    if error:
        return {"url": None, "type": None, "error": error, "server_id": server_id}
    ctx = build_context(env, settings)
    try:
        native = _api_call(ctx, lambda client: client.server_console(server_id))
        if native is not None:
            if native.get("ok"):
                return {
                    "url": native.get("url"),
                    "type": native.get("type") or "novnc",
                    "error": None,
                    "server_id": server_id,
                    "source": "openstack-api",
                }
            return {
                "url": None,
                "type": None,
                "error": native.get("error"),
                "server_id": server_id,
                "source": "openstack-api",
            }
        result = _run_openstack(
            ctx,
            ["console", "url", "show", server_id, "--novnc", "-f", "json"],
            timeout=READ_TIMEOUT,
            log=log,
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "url": None,
            "type": None,
            "error": str(exc)[:200],
            "server_id": server_id,
        }
    finally:
        ctx.cleanup()
    if result.get("returncode") != 0:
        return {
            "url": None,
            "type": None,
            "error": _short_error(result),
            "server_id": server_id,
        }
    try:
        payload = json.loads(result.get("stdout") or "{}")
    except json.JSONDecodeError as exc:
        return {
            "url": None,
            "type": None,
            "error": f"invalid openstack output: {exc}"[:200],
            "server_id": server_id,
        }
    if not isinstance(payload, dict):
        payload = {}
    row = _norm_keys(payload)
    return {
        "url": row.get("url") or row.get("remote_console") or row.get("console_url"),
        "type": row.get("type") or "novnc",
        "error": None,
        "server_id": server_id,
    }


def volume_create(
    env: Environment,
    settings: Settings | None = None,
    *,
    name: str,
    size: Any,
    dry_run: bool | None = None,
    timeout: int = 300,
    log: LogFn | None = None,
) -> dict[str, Any]:
    settings = settings or get_settings()
    err = validate_name(name)
    try:
        size_n = int(size)
    except (TypeError, ValueError):
        size_n = 0
    if size_n < 1 or size_n > 16384:
        err = err or f"invalid size {size!r} — must be 1..16384 GiB"
    if err:
        _log(log, f"[denied] {err}")
        return _denied(err, action="volume_create")
    if dry_run is None:
        dry_run = _ctx_dry_run(env, settings)
    ctx = build_context(env, settings)
    try:
        if not dry_run:
            native = _api_call(
                ctx,
                lambda client: {
                    "volume": client.volume_create(name=str(name).strip(), size=size_n)
                },
            )
            if native is not None:
                native["action"] = "volume_create"
                native["name"] = str(name).strip()
                native["size"] = size_n
                if native.get("ok"):
                    native["message"] = "volume_create accepted"
                return native
        result = _run_openstack(
            ctx,
            ["volume", "create", "--size", str(size_n), str(name).strip()],
            dry_run=dry_run,
            timeout=timeout,
            log=log,
        )
    finally:
        ctx.cleanup()
    return _finish_action(
        result, action="volume_create", name=str(name).strip(), size=size_n
    )


def volume_delete(
    env: Environment,
    settings: Settings | None = None,
    volume_id: Any = "",
    *,
    dry_run: bool | None = None,
    timeout: int = 300,
    log: LogFn | None = None,
) -> dict[str, Any]:
    settings = settings or get_settings()
    volume_id = str(volume_id or "").strip()
    error = validate_uuid(volume_id, field="volume_id")
    if error:
        _log(log, f"[denied] {error}")
        return _denied(error, action="volume_delete", volume_id=volume_id)
    if dry_run is None:
        dry_run = _ctx_dry_run(env, settings)
    ctx = build_context(env, settings)
    try:
        if not dry_run:
            native = _api_call(
                ctx, lambda client: client.volume_delete(volume_id) or {}
            )
            if native is not None:
                native["action"] = "volume_delete"
                native["volume_id"] = volume_id
                if native.get("ok"):
                    native["message"] = "volume_delete accepted"
                return native
        result = _run_openstack(
            ctx,
            ["volume", "delete", volume_id],
            dry_run=dry_run,
            timeout=timeout,
            log=log,
        )
    finally:
        ctx.cleanup()
    return _finish_action(result, action="volume_delete", volume_id=volume_id)


def network_create(
    env: Environment,
    settings: Settings | None = None,
    *,
    name: str,
    cidr: str | None = None,
    external: bool = False,
    dry_run: bool | None = None,
    timeout: int = 300,
    log: LogFn | None = None,
) -> dict[str, Any]:
    """Create a tenant or external network. Subnet is created when CIDR is set."""
    settings = settings or get_settings()
    err = validate_name(name)
    cidr_s = str(cidr or "").strip()
    external = bool(external)
    if cidr_s:
        err = err or validate_cidr(cidr_s)
    elif not external:
        err = err or f"invalid cidr {cidr_s!r}"
    if err:
        _log(log, f"[denied] {err}")
        return _denied(err, action="network_create")
    if dry_run is None:
        dry_run = _ctx_dry_run(env, settings)
    net_name = str(name).strip()
    subnet_name = f"{net_name}-subnet"
    ctx = build_context(env, settings)
    try:
        if not dry_run:
            native = _api_call(
                ctx,
                lambda client: client.network_create(
                    name=net_name, cidr=cidr_s or None, external=external
                )
                or {},
            )
            if native is not None:
                native["action"] = "network_create"
                native["name"] = net_name
                native["cidr"] = cidr_s or None
                native["external"] = external
                if native.get("ok"):
                    if cidr_s:
                        native["message"] = (
                            f"network {net_name} + subnet {cidr_s} accepted"
                        )
                    else:
                        native["message"] = f"network {net_name} accepted"
                return native
        net_args = ["network", "create"]
        if external:
            net_args.append("--external")
        net_args.append(net_name)
        net = _run_openstack(
            ctx,
            net_args,
            dry_run=dry_run,
            timeout=timeout,
            log=log,
        )
        if not (net.get("dry_run") or net.get("returncode") == 0):
            return _finish_action(
                net, action="network_create", name=net_name, external=external
            )
        if not cidr_s:
            result = _finish_action(
                net, action="network_create", name=net_name, external=external
            )
            if result.get("ok"):
                result["message"] = f"network {net_name} accepted"
            return result
        sub = _run_openstack(
            ctx,
            [
                "subnet",
                "create",
                "--network",
                net_name,
                "--subnet-range",
                cidr_s,
                subnet_name,
            ],
            dry_run=dry_run,
            timeout=timeout,
            log=log,
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "error": str(exc)[:200],
            "returncode": 1,
            "action": "network_create",
            "name": net_name,
        }
    finally:
        ctx.cleanup()
    result = _finish_action(
        sub, action="network_create", name=net_name, cidr=cidr_s, external=external
    )
    if result.get("ok"):
        result["message"] = f"network {net_name} + subnet {cidr_s} accepted"
    return result


def volume_attach(
    env: Environment,
    settings: Settings | None = None,
    *,
    server_id: Any,
    volume_id: Any,
    dry_run: bool | None = None,
    timeout: int = 300,
    log: LogFn | None = None,
) -> dict[str, Any]:
    settings = settings or get_settings()
    server_id = str(server_id or "").strip()
    volume_id = str(volume_id or "").strip()
    err = validate_server_id(server_id) or validate_uuid(volume_id, field="volume_id")
    if err:
        return _denied(err, action="volume_attach")
    if dry_run is None:
        dry_run = _ctx_dry_run(env, settings)
    ctx = build_context(env, settings)
    try:
        if not dry_run:
            native = _api_call(
                ctx,
                lambda client: client.volume_attach(
                    server_id=server_id, volume_id=volume_id
                )
                or {},
            )
            if native is not None:
                native["action"] = "volume_attach"
                if native.get("ok"):
                    native["message"] = "volume_attach accepted"
                return native
        result = _run_openstack(
            ctx,
            ["server", "add", "volume", server_id, volume_id],
            dry_run=dry_run,
            timeout=timeout,
            log=log,
        )
    finally:
        ctx.cleanup()
    return _finish_action(
        result, action="volume_attach", server_id=server_id, volume_id=volume_id
    )


def volume_detach(
    env: Environment,
    settings: Settings | None = None,
    *,
    server_id: Any,
    volume_id: Any,
    dry_run: bool | None = None,
    timeout: int = 300,
    log: LogFn | None = None,
) -> dict[str, Any]:
    settings = settings or get_settings()
    server_id = str(server_id or "").strip()
    volume_id = str(volume_id or "").strip()
    err = validate_server_id(server_id) or validate_uuid(volume_id, field="volume_id")
    if err:
        return _denied(err, action="volume_detach")
    if dry_run is None:
        dry_run = _ctx_dry_run(env, settings)
    ctx = build_context(env, settings)
    try:
        if not dry_run:
            native = _api_call(
                ctx,
                lambda client: client.volume_detach(
                    server_id=server_id, volume_id=volume_id
                )
                or {},
            )
            if native is not None:
                native["action"] = "volume_detach"
                if native.get("ok"):
                    native["message"] = "volume_detach accepted"
                return native
        result = _run_openstack(
            ctx,
            ["server", "remove", "volume", server_id, volume_id],
            dry_run=dry_run,
            timeout=timeout,
            log=log,
        )
    finally:
        ctx.cleanup()
    return _finish_action(
        result, action="volume_detach", server_id=server_id, volume_id=volume_id
    )


def floating_ip_create(
    env: Environment,
    settings: Settings | None = None,
    *,
    network: str,
    dry_run: bool | None = None,
    timeout: int = 120,
    log: LogFn | None = None,
) -> dict[str, Any]:
    settings = settings or get_settings()
    err = validate_ref(network, field="network")
    if err:
        return _denied(err, action="floating_ip_create")
    if dry_run is None:
        dry_run = _ctx_dry_run(env, settings)
    ctx = build_context(env, settings)
    try:
        if not dry_run:
            native = _api_call(
                ctx,
                lambda client: {
                    "floating_ip": client.floating_ip_create(
                        network=str(network).strip()
                    )
                },
            )
            if native is not None:
                native["action"] = "floating_ip_create"
                if native.get("ok"):
                    native["message"] = "floating_ip_create accepted"
                return native
        result = _run_openstack(
            ctx,
            ["floating", "ip", "create", str(network).strip()],
            dry_run=dry_run,
            timeout=timeout,
            log=log,
        )
    finally:
        ctx.cleanup()
    return _finish_action(
        result, action="floating_ip_create", network=str(network).strip()
    )


def floating_ip_associate(
    env: Environment,
    settings: Settings | None = None,
    *,
    server_id: Any,
    address: str,
    dry_run: bool | None = None,
    timeout: int = 120,
    log: LogFn | None = None,
) -> dict[str, Any]:
    settings = settings or get_settings()
    server_id = str(server_id or "").strip()
    address = str(address or "").strip()
    err = validate_server_id(server_id)
    if not CIDR_RE.match(address + "/32") and not re.match(
        r"^(\d{1,3}\.){3}\d{1,3}$", address
    ):
        err = err or f"invalid address {address!r}"
    if err:
        return _denied(err, action="floating_ip_associate")
    if dry_run is None:
        dry_run = _ctx_dry_run(env, settings)
    ctx = build_context(env, settings)
    try:
        if not dry_run:
            native = _api_call(
                ctx,
                lambda client: client.floating_ip_associate(
                    server_id=server_id, address=address
                )
                or {},
            )
            if native is not None:
                native["action"] = "floating_ip_associate"
                if native.get("ok"):
                    native["message"] = "floating_ip_associate accepted"
                return native
        result = _run_openstack(
            ctx,
            ["server", "add", "floating", "ip", server_id, address],
            dry_run=dry_run,
            timeout=timeout,
            log=log,
        )
    finally:
        ctx.cleanup()
    return _finish_action(
        result, action="floating_ip_associate", server_id=server_id, address=address
    )


def security_group_rule_create(
    env: Environment,
    settings: Settings | None = None,
    *,
    sg_id: Any,
    direction: str,
    ethertype: str | None = "IPv4",
    protocol: str | None = None,
    port_range_min: Any = None,
    port_range_max: Any = None,
    remote_ip_prefix: str | None = None,
    dry_run: bool | None = None,
    timeout: int = 120,
    log: LogFn | None = None,
) -> dict[str, Any]:
    settings = settings or get_settings()
    sg_id = str(sg_id or "").strip()
    direction = str(direction or "").strip().lower()
    ethertype_s = str(ethertype or "IPv4").strip() or "IPv4"
    proto = str(protocol or "").strip().lower()
    if proto in ("", "any", "null", "none"):
        proto = ""
    remote = str(remote_ip_prefix or "").strip()
    err = validate_uuid(sg_id, field="sg_id")
    if direction not in SG_DIRECTIONS:
        err = err or f"invalid direction {direction!r}"
    if ethertype_s not in SG_ETHERTYPES:
        err = err or f"invalid ethertype {ethertype_s!r}"
    if proto and proto not in SG_PROTOCOLS:
        err = err or f"invalid protocol {proto!r}"
    port_min, port_err = _opt_port(port_range_min, field="port_range_min")
    err = err or port_err
    port_max, port_err = _opt_port(port_range_max, field="port_range_max")
    err = err or port_err
    if remote:
        err = err or validate_cidr(remote, field="remote_ip_prefix")
    if err:
        _log(log, f"[denied] {err}")
        return _denied(err, action="security_group_rule_create", sg_id=sg_id)
    if dry_run is None:
        dry_run = _ctx_dry_run(env, settings)
    extra = {
        "sg_id": sg_id,
        "direction": direction,
        "ethertype": ethertype_s,
        "protocol": proto or None,
    }
    ctx = build_context(env, settings)
    try:
        if not dry_run:
            native = _api_call(
                ctx,
                lambda client: {
                    "rule": client.security_group_rule_create(
                        sg_id=sg_id,
                        direction=direction,
                        ethertype=ethertype_s,
                        protocol=proto or None,
                        port_range_min=port_min,
                        port_range_max=port_max,
                        remote_ip_prefix=remote or None,
                    )
                },
            )
            if native is not None:
                native["action"] = "security_group_rule_create"
                native.update(extra)
                if native.get("ok"):
                    native["message"] = "security_group_rule_create accepted"
                return native
        args = [
            "security",
            "group",
            "rule",
            "create",
            f"--{direction}",
            "--ethertype",
            ethertype_s,
        ]
        if proto:
            args.extend(["--protocol", proto])
        if port_min is not None or port_max is not None:
            lo = port_min if port_min is not None else port_max
            hi = port_max if port_max is not None else port_min
            if lo is not None and hi is not None and lo != hi:
                args.extend(["--dst-port", f"{lo}:{hi}"])
            elif lo is not None:
                args.extend(["--dst-port", str(lo)])
        if remote:
            args.extend(["--remote-ip", remote])
        args.append(sg_id)
        result = _run_openstack(ctx, args, dry_run=dry_run, timeout=timeout, log=log)
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "error": str(exc)[:200],
            "returncode": 1,
            "action": "security_group_rule_create",
            **extra,
        }
    finally:
        ctx.cleanup()
    return _finish_action(result, action="security_group_rule_create", **extra)


def security_group_rule_delete(
    env: Environment,
    settings: Settings | None = None,
    *,
    sg_id: Any = "",
    rule_id: Any,
    dry_run: bool | None = None,
    timeout: int = 120,
    log: LogFn | None = None,
) -> dict[str, Any]:
    settings = settings or get_settings()
    sg_id = str(sg_id or "").strip()
    rule_id = str(rule_id or "").strip()
    err = validate_uuid(rule_id, field="rule_id")
    if sg_id:
        err = err or validate_uuid(sg_id, field="sg_id")
    if err:
        _log(log, f"[denied] {err}")
        return _denied(
            err, action="security_group_rule_delete", sg_id=sg_id, rule_id=rule_id
        )
    if dry_run is None:
        dry_run = _ctx_dry_run(env, settings)
    ctx = build_context(env, settings)
    try:
        if not dry_run:
            native = _api_call(
                ctx, lambda client: client.security_group_rule_delete(rule_id) or {}
            )
            if native is not None:
                native["action"] = "security_group_rule_delete"
                native["sg_id"] = sg_id
                native["rule_id"] = rule_id
                if native.get("ok"):
                    native["message"] = "security_group_rule_delete accepted"
                return native
        result = _run_openstack(
            ctx,
            ["security", "group", "rule", "delete", rule_id],
            dry_run=dry_run,
            timeout=timeout,
            log=log,
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "error": str(exc)[:200],
            "returncode": 1,
            "action": "security_group_rule_delete",
            "sg_id": sg_id,
            "rule_id": rule_id,
        }
    finally:
        ctx.cleanup()
    return _finish_action(
        result, action="security_group_rule_delete", sg_id=sg_id, rule_id=rule_id
    )


def router_create(
    env: Environment,
    settings: Settings | None = None,
    *,
    name: str,
    external_network: Any,
    dry_run: bool | None = None,
    timeout: int = 300,
    log: LogFn | None = None,
) -> dict[str, Any]:
    settings = settings or get_settings()
    net_id = str(external_network or "").strip()
    err = validate_name(name) or validate_uuid(net_id, field="external_network")
    if err:
        _log(log, f"[denied] {err}")
        return _denied(err, action="router_create")
    if dry_run is None:
        dry_run = _ctx_dry_run(env, settings)
    net_name = str(name).strip()
    ctx = build_context(env, settings)
    try:
        if not dry_run:
            native = _api_call(
                ctx,
                lambda client: {
                    "router": client.router_create(
                        name=net_name, external_network=net_id
                    )
                },
            )
            if native is not None:
                native["action"] = "router_create"
                native["name"] = net_name
                native["external_network"] = net_id
                if native.get("ok"):
                    native["message"] = "router_create accepted"
                return native
        result = _run_openstack(
            ctx,
            ["router", "create", "--external-gateway", net_id, net_name],
            dry_run=dry_run,
            timeout=timeout,
            log=log,
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "error": str(exc)[:200],
            "returncode": 1,
            "action": "router_create",
            "name": net_name,
        }
    finally:
        ctx.cleanup()
    return _finish_action(
        result, action="router_create", name=net_name, external_network=net_id
    )


def router_add_interface(
    env: Environment,
    settings: Settings | None = None,
    *,
    router_id: Any,
    subnet_id: Any,
    dry_run: bool | None = None,
    timeout: int = 300,
    log: LogFn | None = None,
) -> dict[str, Any]:
    settings = settings or get_settings()
    router_id = str(router_id or "").strip()
    subnet_id = str(subnet_id or "").strip()
    err = validate_uuid(router_id, field="router_id") or validate_uuid(
        subnet_id, field="subnet_id"
    )
    if err:
        _log(log, f"[denied] {err}")
        return _denied(
            err, action="router_add_interface", router_id=router_id, subnet_id=subnet_id
        )
    if dry_run is None:
        dry_run = _ctx_dry_run(env, settings)
    ctx = build_context(env, settings)
    try:
        if not dry_run:
            native = _api_call(
                ctx,
                lambda client: {
                    "interface": client.router_add_interface(
                        router_id=router_id, subnet_id=subnet_id
                    )
                },
            )
            if native is not None:
                native["action"] = "router_add_interface"
                native["router_id"] = router_id
                native["subnet_id"] = subnet_id
                if native.get("ok"):
                    native["message"] = "router_add_interface accepted"
                return native
        result = _run_openstack(
            ctx,
            ["router", "add", "subnet", router_id, subnet_id],
            dry_run=dry_run,
            timeout=timeout,
            log=log,
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "error": str(exc)[:200],
            "returncode": 1,
            "action": "router_add_interface",
            "router_id": router_id,
            "subnet_id": subnet_id,
        }
    finally:
        ctx.cleanup()
    return _finish_action(
        result, action="router_add_interface", router_id=router_id, subnet_id=subnet_id
    )


def quotas_update(
    env: Environment,
    settings: Settings | None = None,
    *,
    compute: dict[str, Any] | None = None,
    network: dict[str, Any] | None = None,
    dry_run: bool | None = None,
    timeout: int = 120,
    log: LogFn | None = None,
) -> dict[str, Any]:
    settings = settings or get_settings()
    compute_body: dict[str, int] = {}
    network_body: dict[str, int] = {}
    err: str | None = None
    if compute is not None and not isinstance(compute, dict):
        err = "invalid compute quotas"
    if network is not None and not isinstance(network, dict):
        err = err or "invalid network quotas"
    if isinstance(compute, dict):
        for key in QUOTA_COMPUTE_KEYS:
            if key not in compute:
                continue
            n, qerr = _opt_quota_int(compute.get(key), field=key)
            err = err or qerr
            if n is not None:
                compute_body[key] = n
    if isinstance(network, dict):
        for key in QUOTA_NETWORK_KEYS:
            if key not in network:
                continue
            n, qerr = _opt_quota_int(network.get(key), field=key)
            err = err or qerr
            if n is not None:
                network_body[key] = n
    if not compute_body and not network_body:
        err = err or "no quota fields to update"
    if err:
        _log(log, f"[denied] {err}")
        return _denied(err, action="quotas_update")
    if dry_run is None:
        dry_run = _ctx_dry_run(env, settings)
    ctx = build_context(env, settings)
    try:
        if not dry_run:
            native = _api_call(
                ctx,
                lambda client: {
                    "quotas": client.quotas_update(
                        compute=compute_body or None, network=network_body or None
                    )
                },
            )
            if native is not None:
                native["action"] = "quotas_update"
                if native.get("ok"):
                    native["message"] = "quotas_update accepted"
                return native
        args = ["quota", "set"]
        for key, value in {**compute_body, **network_body}.items():
            flag = QUOTA_CLI_FLAGS.get(key)
            if flag:
                args.extend([flag, str(value)])
        args.append("admin")
        result = _run_openstack(ctx, args, dry_run=dry_run, timeout=timeout, log=log)
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "error": str(exc)[:200],
            "returncode": 1,
            "action": "quotas_update",
        }
    finally:
        ctx.cleanup()
    return _finish_action(result, action="quotas_update")


def image_create(
    env: Environment,
    settings: Settings | None = None,
    *,
    name: str,
    disk_format: str = "qcow2",
    container_format: str = "bare",
    visibility: str = "private",
    url: str | None = None,
    copy_from: str | None = None,
    dry_run: bool | None = None,
    timeout: int = 300,
    log: LogFn | None = None,
) -> dict[str, Any]:
    settings = settings or get_settings()
    disk = str(disk_format or "qcow2").strip().lower() or "qcow2"
    container = str(container_format or "bare").strip().lower() or "bare"
    vis = str(visibility or "private").strip().lower() or "private"
    location = str(url or copy_from or "").strip() or None
    err = validate_ref(name, field="name")
    if disk not in DISK_FORMATS:
        err = err or f"invalid disk_format {disk!r}"
    if container not in CONTAINER_FORMATS:
        err = err or f"invalid container_format {container!r}"
    if vis not in IMAGE_VISIBILITIES:
        err = err or f"invalid visibility {vis!r}"
    err = err or validate_http_url(location, field="url")
    if err:
        _log(log, f"[denied] {err}")
        return _denied(err, action="image_create")
    args = [
        "image",
        "create",
        "--disk-format",
        disk,
        "--container-format",
        container,
        f"--{vis}" if vis in ("public", "private") else "--private",
        str(name).strip(),
    ]
    return _mutate(
        env,
        settings,
        action="image_create",
        dry_run=dry_run,
        timeout=timeout,
        log=log,
        extra={"name": str(name).strip()},
        api_fn=lambda client: {
            "image": client.image_create(
                name=str(name).strip(),
                disk_format=disk,
                container_format=container,
                visibility=vis,
                url=location,
            )
        },
        cli_args=None if location else args,
        rest_only=bool(location),
    )


def image_delete(
    env: Environment,
    settings: Settings | None = None,
    image_id: Any = "",
    *,
    dry_run: bool | None = None,
    timeout: int = 300,
    log: LogFn | None = None,
) -> dict[str, Any]:
    image_id = str(image_id or "").strip()
    err = validate_uuid(image_id, field="image_id")
    if err:
        _log(log, f"[denied] {err}")
        return _denied(err, action="image_delete", image_id=image_id)
    return _mutate(
        env,
        settings,
        action="image_delete",
        dry_run=dry_run,
        timeout=timeout,
        log=log,
        extra={"image_id": image_id},
        api_fn=lambda client: client.image_delete(image_id) or {},
        cli_args=["image", "delete", image_id],
    )


def image_patch(
    env: Environment,
    settings: Settings | None = None,
    image_id: Any = "",
    *,
    name: str | None = None,
    visibility: str | None = None,
    dry_run: bool | None = None,
    timeout: int = 120,
    log: LogFn | None = None,
) -> dict[str, Any]:
    image_id = str(image_id or "").strip()
    name_s = str(name or "").strip() or None
    vis = str(visibility or "").strip().lower() or None
    err = validate_uuid(image_id, field="image_id")
    if name_s:
        err = err or validate_ref(name_s, field="name")
    if vis and vis not in IMAGE_VISIBILITIES:
        err = err or f"invalid visibility {vis!r}"
    if not name_s and not vis:
        err = err or "no image fields to patch"
    if err:
        _log(log, f"[denied] {err}")
        return _denied(err, action="image_patch", image_id=image_id)
    args = ["image", "set", image_id]
    if name_s:
        args.extend(["--name", name_s])
    if vis:
        args.append(f"--{vis}" if vis in ("public", "private") else "--private")
    return _mutate(
        env,
        settings,
        action="image_patch",
        dry_run=dry_run,
        timeout=timeout,
        log=log,
        extra={"image_id": image_id},
        api_fn=lambda client: {
            "image": client.image_patch(image_id, name=name_s, visibility=vis)
        },
        cli_args=args,
    )


def flavor_create(
    env: Environment,
    settings: Settings | None = None,
    *,
    name: str,
    vcpus: Any,
    ram: Any,
    disk: Any,
    dry_run: bool | None = None,
    timeout: int = 120,
    log: LogFn | None = None,
) -> dict[str, Any]:
    err = validate_name(name)
    try:
        vcpus_n = int(vcpus)
        ram_n = int(ram)
        disk_n = int(disk)
    except (TypeError, ValueError):
        vcpus_n = ram_n = disk_n = 0
        err = err or "invalid flavor size"
    if vcpus_n < 1 or vcpus_n > 256:
        err = err or f"invalid vcpus {vcpus!r}"
    if ram_n < 1 or ram_n > 1_048_576:
        err = err or f"invalid ram {ram!r}"
    if disk_n < 0 or disk_n > 16384:
        err = err or f"invalid disk {disk!r}"
    if err:
        _log(log, f"[denied] {err}")
        return _denied(err, action="flavor_create")
    return _mutate(
        env,
        settings,
        action="flavor_create",
        dry_run=dry_run,
        timeout=timeout,
        log=log,
        extra={"name": str(name).strip()},
        api_fn=lambda client: {
            "flavor": client.flavor_create(
                name=str(name).strip(), vcpus=vcpus_n, ram=ram_n, disk=disk_n
            )
        },
        cli_args=[
            "flavor",
            "create",
            "--vcpus",
            str(vcpus_n),
            "--ram",
            str(ram_n),
            "--disk",
            str(disk_n),
            str(name).strip(),
        ],
    )


def flavor_delete(
    env: Environment,
    settings: Settings | None = None,
    flavor_id: Any = "",
    *,
    dry_run: bool | None = None,
    timeout: int = 120,
    log: LogFn | None = None,
) -> dict[str, Any]:
    flavor_id = str(flavor_id or "").strip()
    err = validate_ref(flavor_id, field="flavor_id")
    if err:
        _log(log, f"[denied] {err}")
        return _denied(err, action="flavor_delete", flavor_id=flavor_id)
    return _mutate(
        env,
        settings,
        action="flavor_delete",
        dry_run=dry_run,
        timeout=timeout,
        log=log,
        extra={"flavor_id": flavor_id},
        api_fn=lambda client: client.flavor_delete(flavor_id) or {},
        cli_args=["flavor", "delete", flavor_id],
    )


def keypair_create(
    env: Environment,
    settings: Settings | None = None,
    *,
    name: str,
    public_key: str | None = None,
    dry_run: bool | None = None,
    timeout: int = 120,
    log: LogFn | None = None,
) -> dict[str, Any]:
    err = validate_name(name) or validate_public_key(public_key)
    if err:
        _log(log, f"[denied] {err}")
        return _denied(err, action="keypair_create")
    pubkey = str(public_key or "").strip() or None
    extra = {"name": str(name).strip()}
    if pubkey:
        return _mutate(
            env,
            settings,
            action="keypair_create",
            dry_run=dry_run,
            timeout=timeout,
            log=log,
            extra=extra,
            api_fn=lambda client: {
                "keypair": client.keypair_create(
                    name=str(name).strip(), public_key=pubkey
                )
            },
            rest_only=True,
        )
    return _mutate(
        env,
        settings,
        action="keypair_create",
        dry_run=dry_run,
        timeout=timeout,
        log=log,
        extra=extra,
        api_fn=lambda client: {
            "keypair": client.keypair_create(name=str(name).strip())
        },
        cli_args=["keypair", "create", str(name).strip()],
    )


def keypair_delete(
    env: Environment,
    settings: Settings | None = None,
    name: Any = "",
    *,
    dry_run: bool | None = None,
    timeout: int = 120,
    log: LogFn | None = None,
) -> dict[str, Any]:
    name = str(name or "").strip()
    err = validate_name(name)
    if err:
        _log(log, f"[denied] {err}")
        return _denied(err, action="keypair_delete", name=name)
    return _mutate(
        env,
        settings,
        action="keypair_delete",
        dry_run=dry_run,
        timeout=timeout,
        log=log,
        extra={"name": name},
        api_fn=lambda client: client.keypair_delete(name) or {},
        cli_args=["keypair", "delete", name],
    )


def security_group_create(
    env: Environment,
    settings: Settings | None = None,
    *,
    name: str,
    description: str | None = None,
    dry_run: bool | None = None,
    timeout: int = 120,
    log: LogFn | None = None,
) -> dict[str, Any]:
    desc = str(description or "").strip()
    err = validate_name(name) or validate_desc(desc)
    if err:
        _log(log, f"[denied] {err}")
        return _denied(err, action="security_group_create")
    args = ["security", "group", "create", str(name).strip()]
    if desc:
        args.extend(["--description", desc])
    return _mutate(
        env,
        settings,
        action="security_group_create",
        dry_run=dry_run,
        timeout=timeout,
        log=log,
        extra={"name": str(name).strip()},
        api_fn=lambda client: {
            "security_group": client.security_group_create(
                name=str(name).strip(), description=desc or None
            )
        },
        cli_args=args,
    )


def security_group_delete(
    env: Environment,
    settings: Settings | None = None,
    sg_id: Any = "",
    *,
    dry_run: bool | None = None,
    timeout: int = 120,
    log: LogFn | None = None,
) -> dict[str, Any]:
    sg_id = str(sg_id or "").strip()
    err = validate_uuid(sg_id, field="sg_id")
    if err:
        _log(log, f"[denied] {err}")
        return _denied(err, action="security_group_delete", sg_id=sg_id)
    return _mutate(
        env,
        settings,
        action="security_group_delete",
        dry_run=dry_run,
        timeout=timeout,
        log=log,
        extra={"sg_id": sg_id},
        api_fn=lambda client: client.security_group_delete(sg_id) or {},
        cli_args=["security", "group", "delete", sg_id],
    )


def floating_ip_delete(
    env: Environment,
    settings: Settings | None = None,
    fip_id: Any = "",
    *,
    dry_run: bool | None = None,
    timeout: int = 120,
    log: LogFn | None = None,
) -> dict[str, Any]:
    fip_id = str(fip_id or "").strip()
    err = validate_uuid(fip_id, field="floating_ip_id")
    if err:
        _log(log, f"[denied] {err}")
        return _denied(err, action="floating_ip_delete", floating_ip_id=fip_id)
    return _mutate(
        env,
        settings,
        action="floating_ip_delete",
        dry_run=dry_run,
        timeout=timeout,
        log=log,
        extra={"floating_ip_id": fip_id},
        api_fn=lambda client: client.floating_ip_delete(fip_id) or {},
        cli_args=["floating", "ip", "delete", fip_id],
    )


def floating_ip_disassociate(
    env: Environment,
    settings: Settings | None = None,
    *,
    address: str | None = None,
    fip_id: str | None = None,
    dry_run: bool | None = None,
    timeout: int = 120,
    log: LogFn | None = None,
) -> dict[str, Any]:
    fid = str(fip_id or "").strip()
    addr = str(address or "").strip()
    err: str | None = None
    if fid:
        err = validate_uuid(fid, field="id")
    elif addr:
        if not re.match(r"^(\d{1,3}\.){3}\d{1,3}$", addr) and not SERVER_ID_RE.match(
            addr
        ):
            err = f"invalid address {addr!r}"
    else:
        err = "address or id is required"
    if err:
        return _denied(err, action="floating_ip_disassociate")
    cli = ["floating", "ip", "unset", "--port", fid or addr]
    return _mutate(
        env,
        settings,
        action="floating_ip_disassociate",
        dry_run=dry_run,
        timeout=timeout,
        log=log,
        extra={"address": addr or None, "floating_ip_id": fid or None},
        api_fn=lambda client: client.floating_ip_disassociate(
            address=addr or None, fip_id=fid or None
        )
        or {},
        cli_args=cli,
    )


def volume_extend(
    env: Environment,
    settings: Settings | None = None,
    volume_id: Any = "",
    *,
    size: Any,
    dry_run: bool | None = None,
    timeout: int = 300,
    log: LogFn | None = None,
) -> dict[str, Any]:
    volume_id = str(volume_id or "").strip()
    err = validate_uuid(volume_id, field="volume_id")
    try:
        size_n = int(size)
    except (TypeError, ValueError):
        size_n = 0
    if size_n < 1 or size_n > 16384:
        err = err or f"invalid size {size!r} — must be 1..16384 GiB"
    if err:
        _log(log, f"[denied] {err}")
        return _denied(err, action="volume_extend", volume_id=volume_id)
    return _mutate(
        env,
        settings,
        action="volume_extend",
        dry_run=dry_run,
        timeout=timeout,
        log=log,
        extra={"volume_id": volume_id, "size": size_n},
        api_fn=lambda client: client.volume_extend(volume_id, size_n) or {},
        cli_args=["volume", "set", "--size", str(size_n), volume_id],
    )


def volume_snapshot_create(
    env: Environment,
    settings: Settings | None = None,
    volume_id: Any = "",
    *,
    name: str,
    force: bool = False,
    dry_run: bool | None = None,
    timeout: int = 300,
    log: LogFn | None = None,
) -> dict[str, Any]:
    volume_id = str(volume_id or "").strip()
    err = validate_uuid(volume_id, field="volume_id") or validate_name(name)
    if err:
        _log(log, f"[denied] {err}")
        return _denied(err, action="volume_snapshot")
    args = ["volume", "snapshot", "create", "--volume", volume_id, str(name).strip()]
    if force:
        args.insert(-1, "--force")
    return _mutate(
        env,
        settings,
        action="volume_snapshot",
        dry_run=dry_run,
        timeout=timeout,
        log=log,
        extra={"volume_id": volume_id, "name": str(name).strip()},
        api_fn=lambda client: {
            "snapshot": client.volume_snapshot_create(
                volume_id=volume_id, name=str(name).strip(), force=bool(force)
            )
        },
        cli_args=args,
    )


def volume_snapshots_list(
    env: Environment,
    settings: Settings | None = None,
    *,
    log: LogFn | None = None,
) -> dict[str, Any]:
    settings = settings or get_settings()
    ctx = build_context(env, settings)
    try:
        native = _api_call(
            ctx, lambda client: {"snapshots": client.list_volume_snapshots()}
        )
        if native is not None:
            if native.get("ok"):
                return {
                    "snapshots": native.get("snapshots") or [],
                    "error": None,
                    "source": "openstack-api",
                }
            return {
                "snapshots": [],
                "error": native.get("error"),
                "source": "openstack-api",
            }
        result = _run_openstack(
            ctx,
            ["volume", "snapshot", "list", "--all-projects", "-f", "json"],
            timeout=READ_TIMEOUT,
            log=log,
        )
        rows, err = _parse_rows(result, _snapshot_row)
        return {
            "snapshots": rows,
            "error": err,
            "source": "live" if err is None else "unavailable",
        }
    except Exception as exc:  # noqa: BLE001
        return {"snapshots": [], "error": str(exc)[:200], "source": "unavailable"}
    finally:
        ctx.cleanup()


def volume_snapshot_delete(
    env: Environment,
    settings: Settings | None = None,
    snapshot_id: Any = "",
    *,
    dry_run: bool | None = None,
    timeout: int = 300,
    log: LogFn | None = None,
) -> dict[str, Any]:
    snapshot_id = str(snapshot_id or "").strip()
    err = validate_uuid(snapshot_id, field="snapshot_id")
    if err:
        _log(log, f"[denied] {err}")
        return _denied(err, action="volume_snapshot_delete", snapshot_id=snapshot_id)
    return _mutate(
        env,
        settings,
        action="volume_snapshot_delete",
        dry_run=dry_run,
        timeout=timeout,
        log=log,
        extra={"snapshot_id": snapshot_id},
        api_fn=lambda client: client.volume_snapshot_delete(snapshot_id) or {},
        cli_args=["volume", "snapshot", "delete", snapshot_id],
    )


def server_resize(
    env: Environment,
    settings: Settings | None = None,
    server_id: Any = "",
    *,
    flavor: str,
    dry_run: bool | None = None,
    timeout: int = 600,
    log: LogFn | None = None,
) -> dict[str, Any]:
    server_id = str(server_id or "").strip()
    err = validate_server_id(server_id) or validate_ref(flavor, field="flavor")
    if err:
        _log(log, f"[denied] {err}")
        return _denied(err, action="resize", server_id=server_id)
    return _mutate(
        env,
        settings,
        action="resize",
        dry_run=dry_run,
        timeout=timeout,
        log=log,
        extra={"server_id": server_id, "flavor": str(flavor).strip()},
        api_fn=lambda client: client.server_resize(server_id, str(flavor).strip())
        or {},
        cli_args=["server", "resize", "--flavor", str(flavor).strip(), server_id],
    )


def server_rebuild(
    env: Environment,
    settings: Settings | None = None,
    server_id: Any = "",
    *,
    image: str,
    dry_run: bool | None = None,
    timeout: int = 600,
    log: LogFn | None = None,
) -> dict[str, Any]:
    server_id = str(server_id or "").strip()
    err = validate_server_id(server_id) or validate_ref(image, field="image")
    if err:
        _log(log, f"[denied] {err}")
        return _denied(err, action="rebuild", server_id=server_id)
    return _mutate(
        env,
        settings,
        action="rebuild",
        dry_run=dry_run,
        timeout=timeout,
        log=log,
        extra={"server_id": server_id, "image": str(image).strip()},
        api_fn=lambda client: client.server_rebuild(server_id, str(image).strip())
        or {},
        cli_args=["server", "rebuild", "--image", str(image).strip(), server_id],
    )


def server_snapshot(
    env: Environment,
    settings: Settings | None = None,
    server_id: Any = "",
    *,
    name: str,
    dry_run: bool | None = None,
    timeout: int = 600,
    log: LogFn | None = None,
) -> dict[str, Any]:
    server_id = str(server_id or "").strip()
    err = validate_server_id(server_id) or validate_ref(name, field="name")
    if err:
        _log(log, f"[denied] {err}")
        return _denied(err, action="server_snapshot", server_id=server_id)
    return _mutate(
        env,
        settings,
        action="server_snapshot",
        dry_run=dry_run,
        timeout=timeout,
        log=log,
        extra={"server_id": server_id, "name": str(name).strip()},
        api_fn=lambda client: {
            "image": client.server_snapshot(server_id, str(name).strip())
        },
        cli_args=["server", "image", "create", "--name", str(name).strip(), server_id],
    )


def server_security_group_add(
    env: Environment,
    settings: Settings | None = None,
    server_id: Any = "",
    *,
    name: str,
    dry_run: bool | None = None,
    timeout: int = 120,
    log: LogFn | None = None,
) -> dict[str, Any]:
    server_id = str(server_id or "").strip()
    err = validate_server_id(server_id) or validate_ref(name, field="name")
    if err:
        _log(log, f"[denied] {err}")
        return _denied(err, action="server_add_security_group", server_id=server_id)
    return _mutate(
        env,
        settings,
        action="server_add_security_group",
        dry_run=dry_run,
        timeout=timeout,
        log=log,
        extra={"server_id": server_id, "name": str(name).strip()},
        api_fn=lambda client: client.server_add_security_group(
            server_id, str(name).strip()
        )
        or {},
        cli_args=["server", "add", "security", "group", server_id, str(name).strip()],
    )


def server_security_group_remove(
    env: Environment,
    settings: Settings | None = None,
    server_id: Any = "",
    *,
    name: str,
    dry_run: bool | None = None,
    timeout: int = 120,
    log: LogFn | None = None,
) -> dict[str, Any]:
    server_id = str(server_id or "").strip()
    err = validate_server_id(server_id) or validate_ref(name, field="name")
    if err:
        _log(log, f"[denied] {err}")
        return _denied(err, action="server_remove_security_group", server_id=server_id)
    return _mutate(
        env,
        settings,
        action="server_remove_security_group",
        dry_run=dry_run,
        timeout=timeout,
        log=log,
        extra={"server_id": server_id, "name": str(name).strip()},
        api_fn=lambda client: client.server_remove_security_group(
            server_id, str(name).strip()
        )
        or {},
        cli_args=[
            "server",
            "remove",
            "security",
            "group",
            server_id,
            str(name).strip(),
        ],
    )


def network_delete(
    env: Environment,
    settings: Settings | None = None,
    network_id: Any = "",
    *,
    dry_run: bool | None = None,
    timeout: int = 300,
    log: LogFn | None = None,
) -> dict[str, Any]:
    network_id = str(network_id or "").strip()
    err = validate_uuid(network_id, field="network_id")
    if err:
        _log(log, f"[denied] {err}")
        return _denied(err, action="network_delete", network_id=network_id)
    return _mutate(
        env,
        settings,
        action="network_delete",
        dry_run=dry_run,
        timeout=timeout,
        log=log,
        extra={"network_id": network_id},
        api_fn=lambda client: client.network_delete(network_id) or {},
        cli_args=["network", "delete", network_id],
    )


def subnet_create(
    env: Environment,
    settings: Settings | None = None,
    *,
    network: str,
    cidr: str,
    name: str | None = None,
    dry_run: bool | None = None,
    timeout: int = 300,
    log: LogFn | None = None,
) -> dict[str, Any]:
    net = str(network or "").strip()
    cidr_s = str(cidr or "").strip()
    name_s = str(name or "").strip()
    err = validate_uuid(net, field="network") or validate_cidr(cidr_s)
    if name_s:
        err = err or validate_name(name_s)
    if err:
        _log(log, f"[denied] {err}")
        return _denied(err, action="subnet_create")
    ip_version = 6 if ":" in cidr_s else 4
    args = ["subnet", "create", "--network", net, "--subnet-range", cidr_s]
    if name_s:
        args.append(name_s)
    else:
        args.append(f"subnet-{cidr_s.replace('/', '-')}")
    return _mutate(
        env,
        settings,
        action="subnet_create",
        dry_run=dry_run,
        timeout=timeout,
        log=log,
        extra={"network": net, "cidr": cidr_s, "name": name_s or None},
        api_fn=lambda client: {
            "subnet": client.subnet_create(
                network=net, cidr=cidr_s, name=name_s or None, ip_version=ip_version
            )
        },
        cli_args=args,
    )


def subnet_delete(
    env: Environment,
    settings: Settings | None = None,
    subnet_id: Any = "",
    *,
    dry_run: bool | None = None,
    timeout: int = 300,
    log: LogFn | None = None,
) -> dict[str, Any]:
    subnet_id = str(subnet_id or "").strip()
    err = validate_uuid(subnet_id, field="subnet_id")
    if err:
        _log(log, f"[denied] {err}")
        return _denied(err, action="subnet_delete", subnet_id=subnet_id)
    return _mutate(
        env,
        settings,
        action="subnet_delete",
        dry_run=dry_run,
        timeout=timeout,
        log=log,
        extra={"subnet_id": subnet_id},
        api_fn=lambda client: client.subnet_delete(subnet_id) or {},
        cli_args=["subnet", "delete", subnet_id],
    )


def router_delete(
    env: Environment,
    settings: Settings | None = None,
    router_id: Any = "",
    *,
    dry_run: bool | None = None,
    timeout: int = 300,
    log: LogFn | None = None,
) -> dict[str, Any]:
    router_id = str(router_id or "").strip()
    err = validate_uuid(router_id, field="router_id")
    if err:
        _log(log, f"[denied] {err}")
        return _denied(err, action="router_delete", router_id=router_id)
    return _mutate(
        env,
        settings,
        action="router_delete",
        dry_run=dry_run,
        timeout=timeout,
        log=log,
        extra={"router_id": router_id},
        api_fn=lambda client: client.router_delete(router_id) or {},
        cli_args=["router", "delete", router_id],
    )


def router_remove_interface(
    env: Environment,
    settings: Settings | None = None,
    *,
    router_id: Any,
    subnet_id: Any,
    dry_run: bool | None = None,
    timeout: int = 300,
    log: LogFn | None = None,
) -> dict[str, Any]:
    router_id = str(router_id or "").strip()
    subnet_id = str(subnet_id or "").strip()
    err = validate_uuid(router_id, field="router_id") or validate_uuid(
        subnet_id, field="subnet_id"
    )
    if err:
        _log(log, f"[denied] {err}")
        return _denied(
            err,
            action="router_remove_interface",
            router_id=router_id,
            subnet_id=subnet_id,
        )
    return _mutate(
        env,
        settings,
        action="router_remove_interface",
        dry_run=dry_run,
        timeout=timeout,
        log=log,
        extra={"router_id": router_id, "subnet_id": subnet_id},
        api_fn=lambda client: {
            "interface": client.router_remove_interface(
                router_id=router_id, subnet_id=subnet_id
            )
        },
        cli_args=["router", "remove", "subnet", router_id, subnet_id],
    )


def project_create(
    env: Environment,
    settings: Settings | None = None,
    *,
    name: str,
    description: str | None = None,
    enabled: bool = True,
    dry_run: bool | None = None,
    timeout: int = 120,
    log: LogFn | None = None,
) -> dict[str, Any]:
    desc = str(description or "").strip()
    err = validate_name(name) or validate_desc(desc)
    if err:
        _log(log, f"[denied] {err}")
        return _denied(err, action="project_create")
    args = ["project", "create", str(name).strip()]
    if desc:
        args.extend(["--description", desc])
    if not enabled:
        args.append("--disable")
    return _mutate(
        env,
        settings,
        action="project_create",
        dry_run=dry_run,
        timeout=timeout,
        log=log,
        extra={"name": str(name).strip()},
        api_fn=lambda client: {
            "project": client.project_create(
                name=str(name).strip(), description=desc or None, enabled=bool(enabled)
            )
        },
        cli_args=args,
    )


def project_update(
    env: Environment,
    settings: Settings | None = None,
    project_id: Any = "",
    *,
    name: str | None = None,
    description: str | None = None,
    enabled: bool | None = None,
    dry_run: bool | None = None,
    timeout: int = 120,
    log: LogFn | None = None,
) -> dict[str, Any]:
    project_id = str(project_id or "").strip()
    name_s = str(name or "").strip() or None
    desc = None if description is None else str(description).strip()
    err = validate_uuid(project_id, field="project_id")
    if name_s:
        err = err or validate_name(name_s)
    if desc:
        err = err or validate_desc(desc)
    if not name_s and desc is None and enabled is None:
        err = err or "no project fields to update"
    if err:
        _log(log, f"[denied] {err}")
        return _denied(err, action="project_update", project_id=project_id)
    args = ["project", "set", project_id]
    if name_s:
        args.extend(["--name", name_s])
    if desc is not None:
        args.extend(["--description", desc])
    if enabled is True:
        args.append("--enable")
    if enabled is False:
        args.append("--disable")
    return _mutate(
        env,
        settings,
        action="project_update",
        dry_run=dry_run,
        timeout=timeout,
        log=log,
        extra={"project_id": project_id},
        api_fn=lambda client: {
            "project": client.project_update(
                project_id, name=name_s, description=desc, enabled=enabled
            )
        },
        cli_args=args,
    )


def user_create(
    env: Environment,
    settings: Settings | None = None,
    *,
    name: str,
    password: str,
    project: str | None = None,
    dry_run: bool | None = None,
    timeout: int = 120,
    log: LogFn | None = None,
) -> dict[str, Any]:
    proj = str(project or "").strip() or None
    err = validate_name(name) or validate_password(password)
    if proj:
        err = err or validate_ref(proj, field="project")
    if err:
        _log(log, f"[denied] {err}")
        return _denied(err, action="user_create")
    return _mutate(
        env,
        settings,
        action="user_create",
        dry_run=dry_run,
        timeout=timeout,
        log=log,
        extra={"name": str(name).strip(), "project": proj},
        api_fn=lambda client: {
            "user": client.user_create(
                name=str(name).strip(), password=str(password), project=proj
            )
        },
        rest_only=True,
    )


def user_update(
    env: Environment,
    settings: Settings | None = None,
    user_id: Any = "",
    *,
    enabled: bool | None = None,
    password: str | None = None,
    name: str | None = None,
    dry_run: bool | None = None,
    timeout: int = 120,
    log: LogFn | None = None,
) -> dict[str, Any]:
    user_id = str(user_id or "").strip()
    name_s = str(name or "").strip() or None
    pw = str(password or "") or None
    err = validate_uuid(user_id, field="user_id")
    if name_s:
        err = err or validate_name(name_s)
    if pw:
        err = err or validate_password(pw)
    if enabled is None and not pw and not name_s:
        err = err or "no user fields to update"
    if err:
        _log(log, f"[denied] {err}")
        return _denied(err, action="user_update", user_id=user_id)
    return _mutate(
        env,
        settings,
        action="user_update",
        dry_run=dry_run,
        timeout=timeout,
        log=log,
        extra={"user_id": user_id},
        api_fn=lambda client: {
            "user": client.user_update(
                user_id, enabled=enabled, password=pw, name=name_s
            )
        },
        rest_only=True,
    )


def load_balancers_list(
    env: Environment,
    settings: Settings | None = None,
    *,
    log: LogFn | None = None,
) -> dict[str, Any]:
    settings = settings or get_settings()
    ctx = build_context(env, settings)
    try:
        native = _api_call(
            ctx,
            lambda client: (
                {
                    "available": True,
                    "load_balancers": client.list_load_balancers(),
                }
                if client.has_service("load-balancer", "octavia")
                else {"available": False, "load_balancers": [], "ok": True}
            ),
        )
        if native is not None:
            if "not in service catalog" in str(native.get("error") or ""):
                return {
                    "available": False,
                    "load_balancers": [],
                    "error": None,
                    "source": "openstack-api",
                }
            return {
                "available": bool(native.get("available")),
                "load_balancers": native.get("load_balancers") or [],
                "error": native.get("error") if not native.get("ok") else None,
                "source": "openstack-api",
            }
        return {
            "available": False,
            "load_balancers": [],
            "error": None,
            "source": "unavailable",
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "available": False,
            "load_balancers": [],
            "error": str(exc)[:200],
            "source": "unavailable",
        }
    finally:
        ctx.cleanup()


def load_balancer_create(
    env: Environment,
    settings: Settings | None = None,
    *,
    name: str,
    vip_subnet_id: str,
    dry_run: bool | None = None,
    timeout: int = 300,
    log: LogFn | None = None,
) -> dict[str, Any]:
    subnet = str(vip_subnet_id or "").strip()
    err = validate_name(name) or validate_uuid(subnet, field="vip_subnet_id")
    if err:
        _log(log, f"[denied] {err}")
        return _denied(err, action="load_balancer_create")
    return _mutate(
        env,
        settings,
        action="load_balancer_create",
        dry_run=dry_run,
        timeout=timeout,
        log=log,
        extra={"name": str(name).strip()},
        api_fn=lambda client: {
            "load_balancer": client.load_balancer_create(
                name=str(name).strip(), vip_subnet_id=subnet
            ),
            "available": True,
        },
        rest_only=True,
    )


def load_balancer_delete(
    env: Environment,
    settings: Settings | None = None,
    lb_id: Any = "",
    *,
    dry_run: bool | None = None,
    timeout: int = 300,
    log: LogFn | None = None,
) -> dict[str, Any]:
    lb_id = str(lb_id or "").strip()
    err = validate_uuid(lb_id, field="id")
    if err:
        _log(log, f"[denied] {err}")
        return _denied(err, action="load_balancer_delete", id=lb_id)
    return _mutate(
        env,
        settings,
        action="load_balancer_delete",
        dry_run=dry_run,
        timeout=timeout,
        log=log,
        extra={"id": lb_id},
        api_fn=lambda client: client.load_balancer_delete(lb_id) or {"available": True},
        rest_only=True,
    )


def dns_zones_list(
    env: Environment,
    settings: Settings | None = None,
    *,
    log: LogFn | None = None,
) -> dict[str, Any]:
    settings = settings or get_settings()
    ctx = build_context(env, settings)
    try:
        native = _api_call(
            ctx,
            lambda client: (
                {"available": True, "zones": client.list_dns_zones()}
                if client.has_service("dns", "designate")
                else {"available": False, "zones": [], "ok": True}
            ),
        )
        if native is not None:
            if "not in service catalog" in str(native.get("error") or ""):
                return {
                    "available": False,
                    "zones": [],
                    "error": None,
                    "source": "openstack-api",
                }
            return {
                "available": bool(native.get("available")),
                "zones": native.get("zones") or [],
                "error": native.get("error") if not native.get("ok") else None,
                "source": "openstack-api",
            }
        return {"available": False, "zones": [], "error": None, "source": "unavailable"}
    except Exception as exc:  # noqa: BLE001
        return {
            "available": False,
            "zones": [],
            "error": str(exc)[:200],
            "source": "unavailable",
        }
    finally:
        ctx.cleanup()


def dns_zone_create(
    env: Environment,
    settings: Settings | None = None,
    *,
    name: str,
    email: str,
    zone_type: str = "PRIMARY",
    dry_run: bool | None = None,
    timeout: int = 120,
    log: LogFn | None = None,
) -> dict[str, Any]:
    zname = str(name or "").strip()
    ztype = str(zone_type or "PRIMARY").strip().upper() or "PRIMARY"
    err = None
    if not zname or any(ch.isspace() for ch in zname) or len(zname) > 253:
        err = f"invalid name {zname!r}"
    err = err or validate_email(email)
    if ztype not in ("PRIMARY", "SECONDARY"):
        err = err or f"invalid type {ztype!r}"
    if err:
        _log(log, f"[denied] {err}")
        return _denied(err, action="dns_zone_create")
    return _mutate(
        env,
        settings,
        action="dns_zone_create",
        dry_run=dry_run,
        timeout=timeout,
        log=log,
        extra={"name": zname},
        api_fn=lambda client: {
            "zone": client.dns_zone_create(
                name=zname, email=str(email).strip(), zone_type=ztype
            ),
            "available": True,
        },
        rest_only=True,
    )


def dns_zone_delete(
    env: Environment,
    settings: Settings | None = None,
    zone_id: Any = "",
    *,
    dry_run: bool | None = None,
    timeout: int = 120,
    log: LogFn | None = None,
) -> dict[str, Any]:
    zone_id = str(zone_id or "").strip()
    err = validate_uuid(zone_id, field="id")
    if err:
        _log(log, f"[denied] {err}")
        return _denied(err, action="dns_zone_delete", id=zone_id)
    return _mutate(
        env,
        settings,
        action="dns_zone_delete",
        dry_run=dry_run,
        timeout=timeout,
        log=log,
        extra={"id": zone_id},
        api_fn=lambda client: client.dns_zone_delete(zone_id) or {"available": True},
        rest_only=True,
    )


def secrets_list(
    env: Environment,
    settings: Settings | None = None,
    *,
    log: LogFn | None = None,
) -> dict[str, Any]:
    settings = settings or get_settings()
    ctx = build_context(env, settings)
    try:
        native = _api_call(
            ctx,
            lambda client: (
                {"available": True, "secrets": client.list_secrets()}
                if client.has_service("key-manager", "barbican")
                else {"available": False, "secrets": [], "ok": True}
            ),
        )
        if native is not None:
            if "not in service catalog" in str(native.get("error") or ""):
                return {
                    "available": False,
                    "secrets": [],
                    "error": None,
                    "source": "openstack-api",
                }
            return {
                "available": bool(native.get("available")),
                "secrets": native.get("secrets") or [],
                "error": native.get("error") if not native.get("ok") else None,
                "source": "openstack-api",
            }
        return {
            "available": False,
            "secrets": [],
            "error": None,
            "source": "unavailable",
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "available": False,
            "secrets": [],
            "error": str(exc)[:200],
            "source": "unavailable",
        }
    finally:
        ctx.cleanup()


def secret_create(
    env: Environment,
    settings: Settings | None = None,
    *,
    name: str,
    payload: str,
    dry_run: bool | None = None,
    timeout: int = 120,
    log: LogFn | None = None,
) -> dict[str, Any]:
    err = validate_name(name)
    body = str(payload or "")
    if not body or len(body) > 16384:
        err = err or "invalid payload"
    if err:
        _log(log, f"[denied] {err}")
        return _denied(err, action="secret_create")
    return _mutate(
        env,
        settings,
        action="secret_create",
        dry_run=dry_run,
        timeout=timeout,
        log=log,
        extra={"name": str(name).strip()},
        api_fn=lambda client: {
            "secret": client.secret_create(name=str(name).strip(), payload=body),
            "available": True,
        },
        rest_only=True,
    )


def secret_delete(
    env: Environment,
    settings: Settings | None = None,
    secret_id: Any = "",
    *,
    dry_run: bool | None = None,
    timeout: int = 120,
    log: LogFn | None = None,
) -> dict[str, Any]:
    secret_id = str(secret_id or "").strip()
    err = validate_uuid(secret_id, field="id")
    if err:
        _log(log, f"[denied] {err}")
        return _denied(err, action="secret_delete", id=secret_id)
    return _mutate(
        env,
        settings,
        action="secret_delete",
        dry_run=dry_run,
        timeout=timeout,
        log=log,
        extra={"id": secret_id},
        api_fn=lambda client: client.secret_delete(secret_id) or {"available": True},
        rest_only=True,
    )
