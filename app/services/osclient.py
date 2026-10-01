"""Native OpenStack REST from the console process.

The console host is not on the cluster fabric, so traffic goes:

    console httpx  →  kube-apiserver (public :6443)
                   →  Service proxy  →  keystone/nova/neutron/cinder/glance

That is still the OpenStack APIs (token, catalog, CRUD), not ``kubectl exec``
of the CLI. Credentials come from the ``keystone-admin`` secret via the same
Kubernetes API.
"""

from __future__ import annotations

import base64
import json
import re
import ssl
import tempfile
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import yaml

from app.services.envcontext import EnvContext

OPENSTACK_NS = "openstack"
TOKEN_SKEW = 60
HTTP_TIMEOUT = 15.0
# 2.47+ exposes flavor.original_name; 2.6+ exposes /remote-consoles.
NOVA_MICROVERSION = "2.79"

SERVICES = {
    "identity": ("keystone-api", 5000),
    "compute": ("nova-api", 8774),
    "network": ("neutron-server", 9696),
    "volume": ("cinder-api", 8776),
    "image": ("glance-api", 9292),
    "load-balancer": ("octavia-api", 9876),
    "dns": ("designate-api", 9001),
    "key-manager": ("barbican-api", 9311),
}

# Catalog type/name aliases → osclient service key.
OPTIONAL_CATALOG = {
    "load-balancer": ("load-balancer", "octavia"),
    "dns": ("dns", "designate"),
    "key-manager": ("key-manager", "keymanager", "barbican"),
}

SERVER_OS_ACTIONS: dict[str, dict[str, Any]] = {
    "start": {"os-start": None},
    "stop": {"os-stop": None},
    "reboot": {"reboot": {"type": "SOFT"}},
    "hard-reboot": {"reboot": {"type": "HARD"}},
    "pause": {"pause": None},
    "unpause": {"unpause": None},
    "suspend": {"suspend": None},
    "resume": {"resume": None},
    "lock": {"lock": None},
    "unlock": {"unlock": None},
    "rescue": {"rescue": None},
    "unrescue": {"unrescue": None},
    "shelve": {"shelve": None},
    "unshelve": {"unshelve": None},
    "shelve-offload": {"shelveOffload": None},
    "confirm-resize": {"confirmResize": None},
    "revert-resize": {"revertResize": None},
}

_SECRET_IN_TEXT = re.compile(
    r"(?i)\b(password|passwd|secret|private_key|api[_-]?key)(\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|\S+)"
)


class OpenStackError(RuntimeError):
    """OpenStack or Kubernetes API call failed."""


def _b64file(data: str, suffix: str) -> str:
    raw = base64.b64decode(data)
    fd, name = tempfile.mkstemp(prefix="gsc-os-", suffix=suffix)
    with open(fd, "wb") as fh:
        fh.write(raw)
    Path(name).chmod(0o600)
    return name


def load_kube_http(kubeconfig_path: str) -> tuple[httpx.Client, str, list[str]]:
    """HTTP client for the env kube-apiserver plus cleanup paths."""
    path = Path(kubeconfig_path)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise OpenStackError("invalid kubeconfig")
    clusters = data.get("clusters") or []
    users = data.get("users") or []
    if not clusters or not users:
        raise OpenStackError("kubeconfig missing cluster/user")
    cluster = (clusters[0] or {}).get("cluster") or {}
    user = (users[0] or {}).get("user") or {}
    server = str(cluster.get("server") or "").rstrip("/")
    if not server:
        raise OpenStackError("kubeconfig has no server")
    cleanup: list[str] = []
    ca = cluster.get("certificate-authority-data")
    if ca:
        ca_path = _b64file(ca, ".crt")
        cleanup.append(ca_path)
        ctx = ssl.create_default_context(cafile=ca_path)
    else:
        ctx = ssl._create_unverified_context()
    cert = user.get("client-certificate-data")
    key = user.get("client-key-data")
    if cert and key:
        cert_path = _b64file(cert, ".crt")
        key_path = _b64file(key, ".key")
        cleanup.extend([cert_path, key_path])
        ctx.load_cert_chain(cert_path, key_path)
    headers: dict[str, str] = {}
    token = user.get("token")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    client = httpx.Client(
        verify=ctx,
        timeout=HTTP_TIMEOUT,
        headers=headers,
        follow_redirects=False,
    )
    return client, server, cleanup


class OpenStackClient:
    """Keystone-authenticated OpenStack REST client (via kube service proxy)."""

    def __init__(self, kubeconfig_path: str) -> None:
        self._kubeconfig = kubeconfig_path
        self._http, self._apiserver, self._cleanup = load_kube_http(kubeconfig_path)
        self._lock = threading.Lock()
        self._token: str | None = None
        self._expires = 0.0
        self._project_id: str | None = None
        self._catalog: list[dict[str, Any]] = []
        self._username = "admin"
        self._project = "admin"
        self._domain = "default"

    def close(self) -> None:
        try:
            self._http.close()
        finally:
            for p in self._cleanup:
                try:
                    Path(p).unlink(missing_ok=True)
                except OSError:
                    pass

    def __enter__(self) -> OpenStackClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _proxy(self, service: str, path: str) -> str:
        if service not in SERVICES:
            raise OpenStackError(f"unknown service {service}")
        name, port = SERVICES[service]
        if not path.startswith("/"):
            path = "/" + path
        return (
            f"{self._apiserver}/api/v1/namespaces/{OPENSTACK_NS}"
            f"/services/http:{name}:{port}/proxy{path}"
        )

    def _k8s(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        url = f"{self._apiserver}{path}"
        resp = self._http.request(method, url, **kwargs)
        return resp

    def _admin_password(self) -> str:
        resp = self._k8s(
            "GET",
            f"/api/v1/namespaces/{OPENSTACK_NS}/secrets/keystone-admin",
        )
        if resp.status_code != 200:
            raise OpenStackError(f"keystone-admin secret: HTTP {resp.status_code}")
        data = (resp.json() or {}).get("data") or {}
        raw = data.get("password")
        if not raw:
            raise OpenStackError("keystone-admin secret has no password")
        return base64.b64decode(raw).decode("utf-8")

    def _ensure_token(self) -> str:
        with self._lock:
            if self._token and time.time() < self._expires - TOKEN_SKEW:
                return self._token
            password = self._admin_password()
            body = {
                "auth": {
                    "identity": {
                        "methods": ["password"],
                        "password": {
                            "user": {
                                "name": self._username,
                                "domain": {"name": self._domain},
                                "password": password,
                            }
                        },
                    },
                    "scope": {
                        "project": {
                            "name": self._project,
                            "domain": {"name": self._domain},
                        }
                    },
                }
            }
            resp = self._http.post(
                self._proxy("identity", "/v3/auth/tokens"),
                json=body,
                headers={"Content-Type": "application/json"},
            )
            if resp.status_code not in (200, 201):
                raise OpenStackError(
                    f"keystone auth HTTP {resp.status_code}: {resp.text[:200]}"
                )
            token = resp.headers.get("X-Subject-Token")
            if not token:
                raise OpenStackError("keystone auth returned no X-Subject-Token")
            payload = resp.json() or {}
            tok = payload.get("token") or {}
            self._project_id = ((tok.get("project") or {}).get("id")) or None
            catalog = tok.get("catalog")
            self._catalog = catalog if isinstance(catalog, list) else []
            exp = tok.get("expires_at")
            self._expires = _parse_expiry(exp)
            self._token = token
            return token

    def request(
        self,
        method: str,
        service: str,
        path: str,
        *,
        json_body: Any | None = None,
        content: bytes | None = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        token = self._ensure_token()
        hdrs = {"X-Auth-Token": token}
        if service == "compute":
            hdrs["X-OpenStack-Nova-API-Version"] = NOVA_MICROVERSION
            hdrs["OpenStack-API-Version"] = f"compute {NOVA_MICROVERSION}"
        if headers:
            hdrs.update(headers)
        kwargs: dict[str, Any] = {"params": params, "headers": hdrs}
        if content is not None:
            kwargs["content"] = content
        elif json_body is not None:
            kwargs["json"] = json_body
        resp = self._http.request(method, self._proxy(service, path), **kwargs)
        if resp.status_code == 401:
            with self._lock:
                self._token = None
            token = self._ensure_token()
            hdrs["X-Auth-Token"] = token
            kwargs["headers"] = hdrs
            resp = self._http.request(method, self._proxy(service, path), **kwargs)
        return resp

    def json(
        self,
        method: str,
        service: str,
        path: str,
        *,
        json_body: Any | None = None,
        content: bytes | None = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        ok: tuple[int, ...] = (200, 201, 202, 204),
    ) -> Any:
        resp = self.request(
            method,
            service,
            path,
            json_body=json_body,
            content=content,
            params=params,
            headers=headers,
        )
        if resp.status_code not in ok:
            raise OpenStackError(
                f"{service} {method} {path} HTTP {resp.status_code}: "
                f"{_redact_text(resp.text[:200])}"
            )
        if resp.status_code == 204 or not resp.content:
            return {}
        try:
            return resp.json()
        except json.JSONDecodeError as exc:
            raise OpenStackError(f"invalid json from {service} {path}: {exc}") from exc

    def catalog_aliases(self) -> set[str]:
        """Lowercased service types and names from the Keystone token catalog."""
        self._ensure_token()
        found: set[str] = set()
        for entry in self._catalog:
            if not isinstance(entry, dict):
                continue
            for key in ("type", "name"):
                val = str(entry.get(key) or "").strip().lower()
                if val:
                    found.add(val)
        return found

    def has_service(self, *aliases: str) -> bool:
        found = self.catalog_aliases()
        return any(str(a).strip().lower() in found for a in aliases)

    def optional_available(self) -> dict[str, bool]:
        return {
            key: self.has_service(*names) for key, names in OPTIONAL_CATALOG.items()
        }

    def _cinder_path(self, tail: str) -> str:
        pid = self._project_id or ""
        tail = tail.lstrip("/")
        return f"/v3/{pid}/{tail}" if pid else f"/v3/{tail}"

    def inventory(self) -> dict[str, Any]:
        """Full Cloud-tab payload. Raises OpenStackError on auth/proxy failure."""
        self._ensure_token()
        out: dict[str, Any] = {
            "available": True,
            "source": "openstack-api",
            "error": None,
            "cached": False,
        }

        def grab(key: str, fn: Any) -> None:
            try:
                out[key] = fn()
                out[f"{key}_error"] = None
            except Exception as exc:  # noqa: BLE001
                out[key] = []
                out[f"{key}_error"] = str(exc)[:200]

        grab("images", self.list_images)
        grab("flavors", self.list_flavors)
        grab("projects", self.list_projects)
        grab(
            "servers",
            lambda: self.list_servers(
                image_names=_name_map(out.get("images") or []),
                flavor_names=_name_map(out.get("flavors") or []),
                project_names=_name_map(out.get("projects") or []),
            ),
        )
        grab("volumes", self.list_volumes)
        grab("networks", self.list_networks)
        grab("subnets", self.list_subnets)
        grab("routers", self.list_routers)
        grab("ports", self.list_ports)
        grab("floating_ips", self.list_floating_ips)
        grab("security_groups", self.list_security_groups)
        grab("keypairs", self.list_keypairs)
        grab("users", self.list_users)
        grab("volume_snapshots", self.list_volume_snapshots)
        optional = self.optional_available()
        out["load_balancers_available"] = bool(optional.get("load-balancer"))
        out["dns_zones_available"] = bool(optional.get("dns"))
        out["secrets_available"] = bool(optional.get("key-manager"))
        if out["load_balancers_available"]:
            grab("load_balancers", self.list_load_balancers)
        else:
            out["load_balancers"] = []
            out["load_balancers_error"] = None
        if out["dns_zones_available"]:
            grab("dns_zones", self.list_dns_zones)
        else:
            out["dns_zones"] = []
            out["dns_zones_error"] = None
        if out["secrets_available"]:
            grab("secrets", self.list_secrets)
        else:
            out["secrets"] = []
            out["secrets_error"] = None
        try:
            out["quotas"] = self.get_quotas()
            out["quotas_error"] = None
        except Exception as exc:  # noqa: BLE001
            out["quotas"] = {"compute": {}, "network": {}}
            out["quotas_error"] = str(exc)[:200]
        return out

    def list_servers(
        self,
        *,
        image_names: dict[str, str] | None = None,
        flavor_names: dict[str, str] | None = None,
        project_names: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        data = self.json(
            "GET", "compute", "/v2.1/servers/detail", params={"all_tenants": True}
        )
        # Nova detail often returns image/flavor as {id} only.
        if image_names is None:
            image_names = self._optional_name_map(self.list_images)
        if flavor_names is None:
            flavor_names = self._optional_name_map(self.list_flavors)
        if project_names is None:
            project_names = self._optional_name_map(self.list_projects)
        rows = []
        for s in data.get("servers") or []:
            flavor = s.get("flavor") or {}
            image = s.get("image") or {}
            fault = s.get("fault") if isinstance(s.get("fault"), dict) else {}
            project_id = s.get("tenant_id") or s.get("project_id")
            pid = str(project_id) if project_id else ""
            rows.append(
                {
                    "id": s.get("id"),
                    "name": s.get("name"),
                    "status": s.get("status"),
                    "power_state": s.get("OS-EXT-STS:power_state"),
                    "flavor": _ref_label(flavor, flavor_names),
                    "image": _ref_label(image, image_names),
                    "addresses": s.get("addresses") or {},
                    "host": s.get("OS-EXT-SRV-ATTR:host")
                    or s.get("OS-EXT-SRV-ATTR:hypervisor_hostname"),
                    "created": s.get("created"),
                    "project_id": project_id,
                    "project_name": (project_names or {}).get(pid) or None,
                    "key_name": s.get("key_name"),
                    "fault": fault.get("message") if fault else None,
                }
            )
        return rows

    def _optional_name_map(self, fn: Any) -> dict[str, str]:
        try:
            return _name_map(fn())
        except Exception:  # noqa: BLE001 — list still succeeds with raw ids
            return {}

    def list_images(self) -> list[dict[str, Any]]:
        data = self.json("GET", "image", "/v2/images")
        return [
            {
                "id": i.get("id"),
                "name": i.get("name"),
                "status": i.get("status"),
                "size": i.get("size"),
                "visibility": i.get("visibility"),
                "disk_format": i.get("disk_format"),
                "container_format": i.get("container_format"),
            }
            for i in data.get("images") or []
        ]

    def list_compute_services(self) -> list[dict[str, Any]]:
        data = self.json("GET", "compute", "/v2.1/os-services")
        rows = []
        for s in data.get("services") or []:
            if (s.get("binary") or "") != "nova-compute":
                continue
            rows.append(
                {
                    "host": s.get("host"),
                    "binary": s.get("binary"),
                    "state": s.get("state"),
                    "status": s.get("status"),
                    "zone": s.get("zone"),
                }
            )
        return rows

    def list_flavors(self) -> list[dict[str, Any]]:
        data = self.json("GET", "compute", "/v2.1/flavors/detail")
        return [
            {
                "id": f.get("id"),
                "name": f.get("name"),
                "vcpus": f.get("vcpus"),
                "ram": f.get("ram"),
                "disk": f.get("disk"),
                "public": f.get("os-flavor-access:is_public", True),
            }
            for f in data.get("flavors") or []
        ]

    def list_volumes(self) -> list[dict[str, Any]]:
        pid = self._project_id or ""
        path = f"/v3/{pid}/volumes/detail" if pid else "/v3/volumes/detail"
        data = self.json(
            "GET",
            "volume",
            path,
            params={"all_tenants": True},
        )
        return [
            {
                "id": v.get("id"),
                "name": v.get("name"),
                "status": v.get("status"),
                "size": v.get("size"),
                "attachments": v.get("attachments") or [],
            }
            for v in data.get("volumes") or []
        ]

    def list_networks(self) -> list[dict[str, Any]]:
        data = self.json("GET", "network", "/v2.0/networks")
        return [
            {
                "id": n.get("id"),
                "name": n.get("name"),
                "status": n.get("status"),
                "external": bool((n.get("router:external"))),
                "shared": bool(n.get("shared")),
                "subnets": n.get("subnets") or [],
                "project_id": n.get("project_id") or n.get("tenant_id"),
            }
            for n in data.get("networks") or []
        ]

    def list_subnets(self) -> list[dict[str, Any]]:
        data = self.json("GET", "network", "/v2.0/subnets")
        return [
            {
                "id": s.get("id"),
                "name": s.get("name"),
                "cidr": s.get("cidr"),
                "network": s.get("network_id"),
                "project_id": s.get("project_id") or s.get("tenant_id"),
            }
            for s in data.get("subnets") or []
        ]

    def list_routers(self) -> list[dict[str, Any]]:
        data = self.json("GET", "network", "/v2.0/routers")
        return [
            {
                "id": r.get("id"),
                "name": r.get("name"),
                "status": r.get("status"),
                "external_gateway": r.get("external_gateway_info"),
                "project_id": r.get("project_id") or r.get("tenant_id"),
            }
            for r in data.get("routers") or []
        ]

    def list_ports(self) -> list[dict[str, Any]]:
        # Admin tokens see every project; Neutron has no all_tenants flag.
        data = self.json("GET", "network", "/v2.0/ports")
        rows = []
        for p in data.get("ports") or []:
            fixed = []
            for ip in p.get("fixed_ips") or []:
                if not isinstance(ip, dict):
                    continue
                fixed.append(
                    {
                        "ip": ip.get("ip_address") or ip.get("ip"),
                        "subnet_id": ip.get("subnet_id"),
                    }
                )
            rows.append(
                {
                    "id": p.get("id"),
                    "name": p.get("name"),
                    "status": p.get("status"),
                    "network_id": p.get("network_id"),
                    "device_id": p.get("device_id"),
                    "device_owner": p.get("device_owner"),
                    "project_id": p.get("project_id") or p.get("tenant_id"),
                    "mac": p.get("mac_address") or p.get("mac"),
                    "fixed_ips": fixed,
                    "security_groups": p.get("security_groups") or [],
                }
            )
        return rows

    def list_floating_ips(self) -> list[dict[str, Any]]:
        data = self.json("GET", "network", "/v2.0/floatingips")
        return [
            {
                "id": f.get("id"),
                "ip": f.get("floating_ip_address"),
                "fixed_ip": f.get("fixed_ip_address"),
                "port": f.get("port_id"),
                "port_id": f.get("port_id"),
                "status": f.get("status"),
                "floating_network_id": f.get("floating_network_id"),
                "router_id": f.get("router_id"),
                "project_id": f.get("project_id") or f.get("tenant_id"),
            }
            for f in data.get("floatingips") or []
        ]

    def list_security_groups(self) -> list[dict[str, Any]]:
        data = self.json("GET", "network", "/v2.0/security-groups")
        return [
            {
                "id": g.get("id"),
                "name": g.get("name"),
                "description": g.get("description"),
                "rules": _sg_rules(g.get("security_group_rules") or g.get("rules")),
            }
            for g in data.get("security_groups") or []
        ]

    def list_keypairs(self) -> list[dict[str, Any]]:
        data = self.json("GET", "compute", "/v2.1/os-keypairs")
        rows = []
        for item in data.get("keypairs") or []:
            k = item.get("keypair") if isinstance(item, dict) else item
            if not isinstance(k, dict):
                continue
            rows.append({"name": k.get("name"), "fingerprint": k.get("fingerprint")})
        return rows

    def list_projects(self) -> list[dict[str, Any]]:
        data = self.json("GET", "identity", "/v3/projects")
        return [
            {
                "id": p.get("id"),
                "name": p.get("name"),
                "enabled": p.get("enabled"),
                "description": p.get("description"),
            }
            for p in data.get("projects") or []
        ]

    def list_users(self) -> list[dict[str, Any]]:
        data = self.json("GET", "identity", "/v3/users")
        return [
            {
                "id": u.get("id"),
                "name": u.get("name"),
                "enabled": u.get("enabled"),
                "email": u.get("email"),
            }
            for u in data.get("users") or []
        ]

    def server_action(self, server_id: str, action: str) -> None:
        body = SERVER_OS_ACTIONS.get(action)
        if body is None:
            raise OpenStackError(f"unknown server action {action}")
        self.json(
            "POST",
            "compute",
            f"/v2.1/servers/{server_id}/action",
            json_body=body,
            ok=(200, 202, 204),
        )

    def server_delete(self, server_id: str) -> None:
        self.json("DELETE", "compute", f"/v2.1/servers/{server_id}", ok=(204, 202))

    def server_create(
        self,
        *,
        name: str,
        image: str,
        flavor: str,
        network: str,
        key_name: str | None = None,
    ) -> dict[str, Any]:
        server: dict[str, Any] = {
            "name": name,
            "imageRef": image,
            "flavorRef": flavor,
            "networks": [{"uuid": network}],
        }
        if key_name:
            server["key_name"] = key_name
        data = self.json(
            "POST",
            "compute",
            "/v2.1/servers",
            json_body={"server": server},
            ok=(200, 202),
        )
        return data.get("server") or data

    def server_console(self, server_id: str) -> dict[str, Any]:
        try:
            data = self.json(
                "POST",
                "compute",
                f"/v2.1/servers/{server_id}/remote-consoles",
                json_body={"remote_console": {"protocol": "vnc", "type": "novnc"}},
                ok=(200, 201),
            )
        except OpenStackError:
            data = self.json(
                "POST",
                "compute",
                f"/v2.1/servers/{server_id}/action",
                json_body={"os-getVNCConsole": {"type": "novnc"}},
                ok=(200, 201),
            )
        cons = data.get("remote_console") or data.get("console") or data
        return {"url": cons.get("url"), "type": cons.get("type") or "novnc"}

    def volume_create(self, *, name: str, size: int) -> dict[str, Any]:
        pid = self._project_id or ""
        path = f"/v3/{pid}/volumes" if pid else "/v3/volumes"
        data = self.json(
            "POST",
            "volume",
            path,
            json_body={"volume": {"name": name, "size": size}},
            ok=(200, 201, 202),
        )
        return data.get("volume") or data

    def volume_delete(self, volume_id: str) -> None:
        pid = self._project_id or ""
        path = f"/v3/{pid}/volumes/{volume_id}" if pid else f"/v3/volumes/{volume_id}"
        self.json("DELETE", "volume", path, ok=(202, 204))

    def volume_attach(self, *, server_id: str, volume_id: str) -> None:
        self.json(
            "POST",
            "compute",
            f"/v2.1/servers/{server_id}/os-volume_attachments",
            json_body={"volumeAttachment": {"volumeId": volume_id}},
            ok=(200, 202),
        )

    def volume_detach(self, *, server_id: str, volume_id: str) -> None:
        self.json(
            "DELETE",
            "compute",
            f"/v2.1/servers/{server_id}/os-volume_attachments/{volume_id}",
            ok=(202, 204),
        )

    def network_create(
        self, *, name: str, cidr: str | None = None, external: bool = False
    ) -> dict[str, Any]:
        network_body: dict[str, Any] = {"name": name, "admin_state_up": True}
        if external:
            network_body["router:external"] = True
        net = self.json(
            "POST",
            "network",
            "/v2.0/networks",
            json_body={"network": network_body},
            ok=(201, 200),
        )
        network = net.get("network") or net
        net_id = (network.get("id") if isinstance(network, dict) else "") or ""
        cidr_s = str(cidr or "").strip()
        subnet: Any = None
        if cidr_s and net_id:
            sub = self.json(
                "POST",
                "network",
                "/v2.0/subnets",
                json_body={
                    "subnet": {
                        "name": f"{name}-subnet",
                        "network_id": net_id,
                        "ip_version": 4,
                        "cidr": cidr_s,
                    }
                },
                ok=(201, 200),
            )
            subnet = sub.get("subnet") or sub
        return {"network": network, "subnet": subnet}

    def security_group_rule_create(
        self,
        *,
        sg_id: str,
        direction: str,
        ethertype: str = "IPv4",
        protocol: str | None = None,
        port_range_min: int | None = None,
        port_range_max: int | None = None,
        remote_ip_prefix: str | None = None,
    ) -> dict[str, Any]:
        rule: dict[str, Any] = {
            "security_group_id": sg_id,
            "direction": direction,
            "ethertype": ethertype or "IPv4",
        }
        if protocol:
            rule["protocol"] = protocol
        if port_range_min is not None:
            rule["port_range_min"] = port_range_min
        if port_range_max is not None:
            rule["port_range_max"] = port_range_max
        if remote_ip_prefix:
            rule["remote_ip_prefix"] = remote_ip_prefix
        data = self.json(
            "POST",
            "network",
            "/v2.0/security-group-rules",
            json_body={"security_group_rule": rule},
            ok=(201, 200),
        )
        return data.get("security_group_rule") or data

    def security_group_rule_delete(self, rule_id: str) -> None:
        self.json(
            "DELETE", "network", f"/v2.0/security-group-rules/{rule_id}", ok=(204, 200)
        )

    def router_create(self, *, name: str, external_network: str) -> dict[str, Any]:
        data = self.json(
            "POST",
            "network",
            "/v2.0/routers",
            json_body={
                "router": {
                    "name": name,
                    "admin_state_up": True,
                    "external_gateway_info": {"network_id": external_network},
                }
            },
            ok=(201, 200),
        )
        return data.get("router") or data

    def router_add_interface(self, *, router_id: str, subnet_id: str) -> dict[str, Any]:
        data = self.json(
            "PUT",
            "network",
            f"/v2.0/routers/{router_id}/add_router_interface",
            json_body={"subnet_id": subnet_id},
            ok=(200, 201),
        )
        return data if isinstance(data, dict) else {"subnet_id": subnet_id}

    def get_quotas(self) -> dict[str, Any]:
        pid = self._project_id or ""
        if not pid:
            raise OpenStackError("no project id")
        nova = self.json("GET", "compute", f"/v2.1/os-quota-sets/{pid}")
        qs = nova.get("quota_set") or {}
        neutron = self.json("GET", "network", f"/v2.0/quotas/{pid}")
        nq = neutron.get("quota") or {}
        return {
            "compute": {
                "instances": qs.get("instances"),
                "cores": qs.get("cores"),
                "ram": qs.get("ram"),
            },
            "network": {
                "network": nq.get("network"),
                "subnet": nq.get("subnet"),
                "router": nq.get("router"),
                "floatingip": nq.get("floatingip"),
                "security_group": nq.get("security_group"),
                "port": nq.get("port"),
            },
        }

    def quotas_update(
        self,
        *,
        compute: dict[str, Any] | None = None,
        network: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        pid = self._project_id or ""
        if not pid:
            raise OpenStackError("no project id")
        compute_body = _quota_ints(compute, ("instances", "cores", "ram"))
        network_body = _quota_ints(
            network,
            ("network", "subnet", "router", "floatingip", "security_group", "port"),
        )
        if compute_body:
            self.json(
                "PUT",
                "compute",
                f"/v2.1/os-quota-sets/{pid}",
                json_body={"quota_set": compute_body},
                ok=(200, 201, 202),
            )
        if network_body:
            self.json(
                "PUT",
                "network",
                f"/v2.0/quotas/{pid}",
                json_body={"quota": network_body},
                ok=(200, 201, 202),
            )
        return self.get_quotas()

    def floating_ip_create(self, *, network: str) -> dict[str, Any]:
        data = self.json(
            "POST",
            "network",
            "/v2.0/floatingips",
            json_body={"floatingip": {"floating_network_id": network}},
            ok=(201, 200),
        )
        return data.get("floatingip") or data

    def floating_ip_associate(self, *, server_id: str, address: str) -> None:
        # Neutron: find FIP id by address, find server port, update FIP port_id.
        fips = self.list_floating_ips()
        fip = next((f for f in fips if f.get("ip") == address), None)
        if not fip or not fip.get("id"):
            raise OpenStackError(f"floating IP {address} not found")
        ports = self.json(
            "GET",
            "network",
            "/v2.0/ports",
            params={"device_id": server_id},
        )
        port_list = ports.get("ports") or []
        if not port_list:
            raise OpenStackError("server has no ports")
        self.json(
            "PUT",
            "network",
            f"/v2.0/floatingips/{fip['id']}",
            json_body={"floatingip": {"port_id": port_list[0].get("id")}},
            ok=(200,),
        )

    def floating_ip_delete(self, fip_id: str) -> None:
        self.json("DELETE", "network", f"/v2.0/floatingips/{fip_id}", ok=(204, 200))

    def floating_ip_disassociate(
        self, *, address: str | None = None, fip_id: str | None = None
    ) -> None:
        fid = str(fip_id or "").strip()
        addr = str(address or "").strip()
        if not fid:
            fips = self.list_floating_ips()
            fip = next(
                (f for f in fips if f.get("id") == addr or f.get("ip") == addr), None
            )
            if not fip or not fip.get("id"):
                raise OpenStackError(f"floating IP {addr or fid} not found")
            fid = str(fip["id"])
        self.json(
            "PUT",
            "network",
            f"/v2.0/floatingips/{fid}",
            json_body={"floatingip": {"port_id": None}},
            ok=(200,),
        )

    def image_create(
        self,
        *,
        name: str,
        disk_format: str = "qcow2",
        container_format: str = "bare",
        visibility: str = "private",
        url: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "name": name,
            "disk_format": disk_format,
            "container_format": container_format,
            "visibility": visibility,
        }
        data = self.json("POST", "image", "/v2/images", json_body=body, ok=(201, 200))
        image = (
            data.get("image") if isinstance(data, dict) and "image" in data else data
        )
        if not isinstance(image, dict):
            image = {"name": name}
        image_id = image.get("id")
        location = str(url or "").strip()
        if location and image_id:
            self.json(
                "POST",
                "image",
                f"/v2/images/{image_id}/import",
                json_body={"method": {"name": "web-download", "uri": location}},
                ok=(202, 200, 201),
            )
            image["import"] = "web-download"
        return image

    def image_delete(self, image_id: str) -> None:
        self.json("DELETE", "image", f"/v2/images/{image_id}", ok=(204, 200, 202))

    def image_patch(
        self,
        image_id: str,
        *,
        name: str | None = None,
        visibility: str | None = None,
    ) -> dict[str, Any]:
        ops: list[dict[str, Any]] = []
        if name:
            ops.append({"op": "replace", "path": "/name", "value": name})
        if visibility:
            ops.append({"op": "replace", "path": "/visibility", "value": visibility})
        if not ops:
            raise OpenStackError("no image fields to patch")
        payload = json.dumps(ops).encode("utf-8")
        data = self.json(
            "PATCH",
            "image",
            f"/v2/images/{image_id}",
            content=payload,
            headers={"Content-Type": "application/openstack-images-v2.1-json-patch"},
            ok=(200, 201),
        )
        return data if isinstance(data, dict) else {"id": image_id}

    def flavor_create(
        self, *, name: str, vcpus: int, ram: int, disk: int, public: bool = True
    ) -> dict[str, Any]:
        data = self.json(
            "POST",
            "compute",
            "/v2.1/flavors",
            json_body={
                "flavor": {
                    "name": name,
                    "vcpus": vcpus,
                    "ram": ram,
                    "disk": disk,
                    "os-flavor-access:is_public": bool(public),
                }
            },
            ok=(200, 201),
        )
        return data.get("flavor") or data

    def flavor_delete(self, flavor_id: str) -> None:
        self.json(
            "DELETE",
            "compute",
            f"/v2.1/flavors/{quote(str(flavor_id), safe='')}",
            ok=(202, 204),
        )

    def keypair_create(
        self, *, name: str, public_key: str | None = None
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"name": name}
        if public_key:
            body["public_key"] = public_key
        data = self.json(
            "POST",
            "compute",
            "/v2.1/os-keypairs",
            json_body={"keypair": body},
            ok=(200, 201),
        )
        kp = data.get("keypair") or data
        return kp if isinstance(kp, dict) else {"name": name}

    def keypair_delete(self, name: str) -> None:
        self.json(
            "DELETE",
            "compute",
            f"/v2.1/os-keypairs/{quote(name, safe='')}",
            ok=(202, 204),
        )

    def security_group_create(
        self, *, name: str, description: str | None = None
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"name": name}
        if description is not None:
            body["description"] = description
        data = self.json(
            "POST",
            "network",
            "/v2.0/security-groups",
            json_body={"security_group": body},
            ok=(201, 200),
        )
        return data.get("security_group") or data

    def security_group_delete(self, sg_id: str) -> None:
        self.json("DELETE", "network", f"/v2.0/security-groups/{sg_id}", ok=(204, 200))

    def volume_extend(self, volume_id: str, size: int) -> None:
        self.json(
            "POST",
            "volume",
            self._cinder_path(f"volumes/{volume_id}/action"),
            json_body={"os-extend": {"new_size": size}},
            ok=(202, 200, 204),
        )

    def volume_snapshot_create(
        self, *, volume_id: str, name: str, force: bool = False
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"volume_id": volume_id, "name": name}
        if force:
            body["force"] = True
        data = self.json(
            "POST",
            "volume",
            self._cinder_path("snapshots"),
            json_body={"snapshot": body},
            ok=(202, 201, 200),
        )
        return data.get("snapshot") or data

    def list_volume_snapshots(self) -> list[dict[str, Any]]:
        data = self.json(
            "GET",
            "volume",
            self._cinder_path("snapshots/detail"),
            params={"all_tenants": True},
        )
        return [
            {
                "id": s.get("id"),
                "name": s.get("name"),
                "status": s.get("status"),
                "size": s.get("size"),
                "volume_id": s.get("volume_id"),
                "created": s.get("created_at"),
            }
            for s in data.get("snapshots") or []
        ]

    def volume_snapshot_delete(self, snapshot_id: str) -> None:
        self.json(
            "DELETE",
            "volume",
            self._cinder_path(f"snapshots/{snapshot_id}"),
            ok=(202, 204),
        )

    def server_resize(self, server_id: str, flavor: str) -> None:
        self.json(
            "POST",
            "compute",
            f"/v2.1/servers/{server_id}/action",
            json_body={"resize": {"flavorRef": flavor}},
            ok=(202, 204, 200),
        )

    def server_rebuild(self, server_id: str, image: str) -> None:
        self.json(
            "POST",
            "compute",
            f"/v2.1/servers/{server_id}/action",
            json_body={"rebuild": {"imageRef": image}},
            ok=(202, 200),
        )

    def server_snapshot(self, server_id: str, name: str) -> dict[str, Any]:
        data = self.json(
            "POST",
            "compute",
            f"/v2.1/servers/{server_id}/action",
            json_body={"createImage": {"name": name}},
            ok=(202, 200),
        )
        return data if isinstance(data, dict) else {"name": name}

    def server_add_security_group(self, server_id: str, name: str) -> None:
        self.json(
            "POST",
            "compute",
            f"/v2.1/servers/{server_id}/action",
            json_body={"addSecurityGroup": {"name": name}},
            ok=(202, 200, 204),
        )

    def server_remove_security_group(self, server_id: str, name: str) -> None:
        self.json(
            "POST",
            "compute",
            f"/v2.1/servers/{server_id}/action",
            json_body={"removeSecurityGroup": {"name": name}},
            ok=(202, 200, 204),
        )

    def network_delete(self, network_id: str) -> None:
        self.json("DELETE", "network", f"/v2.0/networks/{network_id}", ok=(204, 200))

    def subnet_create(
        self,
        *,
        network: str,
        cidr: str,
        name: str | None = None,
        ip_version: int = 4,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "network_id": network,
            "cidr": cidr,
            "ip_version": ip_version,
        }
        if name:
            body["name"] = name
        data = self.json(
            "POST",
            "network",
            "/v2.0/subnets",
            json_body={"subnet": body},
            ok=(201, 200),
        )
        return data.get("subnet") or data

    def subnet_delete(self, subnet_id: str) -> None:
        self.json("DELETE", "network", f"/v2.0/subnets/{subnet_id}", ok=(204, 200))

    def router_delete(self, router_id: str) -> None:
        self.json("DELETE", "network", f"/v2.0/routers/{router_id}", ok=(204, 200))

    def router_remove_interface(
        self, *, router_id: str, subnet_id: str
    ) -> dict[str, Any]:
        data = self.json(
            "PUT",
            "network",
            f"/v2.0/routers/{router_id}/remove_router_interface",
            json_body={"subnet_id": subnet_id},
            ok=(200, 201),
        )
        return data if isinstance(data, dict) else {"subnet_id": subnet_id}

    def project_create(
        self, *, name: str, description: str | None = None, enabled: bool = True
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"name": name, "enabled": bool(enabled)}
        if description is not None:
            body["description"] = description
        data = self.json(
            "POST",
            "identity",
            "/v3/projects",
            json_body={"project": body},
            ok=(201, 200),
        )
        return data.get("project") or data

    def project_update(
        self,
        project_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        enabled: bool | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {}
        if name is not None:
            body["name"] = name
        if description is not None:
            body["description"] = description
        if enabled is not None:
            body["enabled"] = bool(enabled)
        if not body:
            raise OpenStackError("no project fields to update")
        data = self.json(
            "PATCH",
            "identity",
            f"/v3/projects/{project_id}",
            json_body={"project": body},
            ok=(200, 201),
        )
        return data.get("project") or data

    def user_create(
        self,
        *,
        name: str,
        password: str,
        project: str | None = None,
        enabled: bool = True,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "name": name,
            "password": password,
            "enabled": bool(enabled),
        }
        project_id = str(project or "").strip() or None
        if project_id:
            project_id = self._resolve_project_id(project_id)
            body["default_project_id"] = project_id
        data = self.json(
            "POST", "identity", "/v3/users", json_body={"user": body}, ok=(201, 200)
        )
        user = data.get("user") or data
        if not isinstance(user, dict):
            user = {"name": name}
        user.pop("password", None)
        if project_id and user.get("id"):
            try:
                self._assign_member_role(str(user["id"]), project_id)
            except OpenStackError:
                user["role_warning"] = "created without member role assignment"
        return user

    def user_update(
        self,
        user_id: str,
        *,
        enabled: bool | None = None,
        password: str | None = None,
        name: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {}
        if enabled is not None:
            body["enabled"] = bool(enabled)
        if password:
            body["password"] = password
        if name:
            body["name"] = name
        if not body:
            raise OpenStackError("no user fields to update")
        data = self.json(
            "PATCH",
            "identity",
            f"/v3/users/{user_id}",
            json_body={"user": body},
            ok=(200, 201),
        )
        user = data.get("user") or data
        if isinstance(user, dict):
            user.pop("password", None)
            return user
        return {"id": user_id}

    def _resolve_project_id(self, project: str) -> str:
        text = str(project).strip()
        if re.fullmatch(
            r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
            text,
        ):
            return text
        for row in self.list_projects():
            if row.get("name") == text or row.get("id") == text:
                pid = row.get("id")
                if pid:
                    return str(pid)
        raise OpenStackError(f"project {text!r} not found")

    def _assign_member_role(self, user_id: str, project_id: str) -> None:
        roles = self.json("GET", "identity", "/v3/roles", params={"name": "member"})
        role_id = None
        for role in roles.get("roles") or []:
            if str(role.get("name") or "").lower() in ("member", "reader"):
                role_id = role.get("id")
                if str(role.get("name") or "").lower() == "member":
                    break
        if not role_id:
            all_roles = self.json("GET", "identity", "/v3/roles")
            for role in all_roles.get("roles") or []:
                if str(role.get("name") or "").lower() == "member":
                    role_id = role.get("id")
                    break
        if not role_id:
            raise OpenStackError("member role not found")
        self.json(
            "PUT",
            "identity",
            f"/v3/projects/{project_id}/users/{user_id}/roles/{role_id}",
            ok=(204, 200, 201),
        )

    def _require_optional(self, service_key: str) -> None:
        aliases = OPTIONAL_CATALOG.get(service_key) or (service_key,)
        if not self.has_service(*aliases):
            raise OpenStackError(f"{service_key} not in service catalog")

    def list_load_balancers(self) -> list[dict[str, Any]]:
        self._require_optional("load-balancer")
        data = self.json("GET", "load-balancer", "/v2/lbaas/loadbalancers")
        return [
            {
                "id": lb.get("id"),
                "name": lb.get("name"),
                "provisioning_status": lb.get("provisioning_status"),
                "operating_status": lb.get("operating_status"),
                "vip_address": lb.get("vip_address"),
                "vip_subnet_id": lb.get("vip_subnet_id"),
            }
            for lb in data.get("loadbalancers") or []
        ]

    def load_balancer_create(self, *, name: str, vip_subnet_id: str) -> dict[str, Any]:
        self._require_optional("load-balancer")
        data = self.json(
            "POST",
            "load-balancer",
            "/v2/lbaas/loadbalancers",
            json_body={"loadbalancer": {"name": name, "vip_subnet_id": vip_subnet_id}},
            ok=(201, 200, 202),
        )
        return data.get("loadbalancer") or data

    def load_balancer_delete(self, lb_id: str) -> None:
        self._require_optional("load-balancer")
        self.json(
            "DELETE",
            "load-balancer",
            f"/v2/lbaas/loadbalancers/{lb_id}",
            ok=(204, 202, 200),
        )

    def list_dns_zones(self) -> list[dict[str, Any]]:
        self._require_optional("dns")
        data = self.json("GET", "dns", "/v2/zones")
        return [
            {
                "id": z.get("id"),
                "name": z.get("name"),
                "status": z.get("status"),
                "email": z.get("email"),
                "type": z.get("type"),
                "serial": z.get("serial"),
            }
            for z in data.get("zones") or []
        ]

    def dns_zone_create(
        self, *, name: str, email: str, zone_type: str = "PRIMARY"
    ) -> dict[str, Any]:
        self._require_optional("dns")
        zone_name = name if name.endswith(".") else f"{name}."
        data = self.json(
            "POST",
            "dns",
            "/v2/zones",
            json_body={
                "name": zone_name,
                "email": email,
                "type": zone_type or "PRIMARY",
            },
            ok=(201, 202, 200),
        )
        return data if isinstance(data, dict) else {"name": zone_name}

    def dns_zone_delete(self, zone_id: str) -> None:
        self._require_optional("dns")
        self.json("DELETE", "dns", f"/v2/zones/{zone_id}", ok=(204, 202, 200))

    def list_secrets(self) -> list[dict[str, Any]]:
        self._require_optional("key-manager")
        data = self.json("GET", "key-manager", "/v1/secrets")
        rows = []
        for s in data.get("secrets") or []:
            ref = s.get("secret_ref") or s.get("secret_href") or s.get("id")
            rows.append(
                {
                    "id": _barbican_id(ref),
                    "name": s.get("name"),
                    "status": s.get("status"),
                    "created": s.get("created") or s.get("created_at"),
                    "secret_type": s.get("secret_type"),
                }
            )
        return rows

    def secret_create(self, *, name: str, payload: str) -> dict[str, Any]:
        self._require_optional("key-manager")
        data = self.json(
            "POST",
            "key-manager",
            "/v1/secrets",
            json_body={
                "name": name,
                "payload": payload,
                "payload_content_type": "text/plain",
            },
            ok=(201, 202, 200),
        )
        out = data if isinstance(data, dict) else {}
        ref = out.get("secret_ref") or out.get("secret_href")
        return {
            "id": _barbican_id(ref or out.get("id")),
            "name": name,
            "secret_ref": ref,
        }

    def secret_delete(self, secret_id: str) -> None:
        self._require_optional("key-manager")
        sid = _barbican_id(secret_id)
        self.json("DELETE", "key-manager", f"/v1/secrets/{sid}", ok=(204, 200, 202))


def _barbican_id(ref: Any) -> str:
    text = str(ref or "").strip().rstrip("/")
    if not text:
        return ""
    return text.rsplit("/", 1)[-1]


def _redact_text(text: str) -> str:
    return _SECRET_IN_TEXT.sub(r"\1\2<redacted>", text)


def _sg_rules(raw: Any) -> list[dict[str, Any]]:
    rows = raw if isinstance(raw, list) else []
    out: list[dict[str, Any]] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        out.append(
            {
                "id": item.get("id"),
                "direction": item.get("direction"),
                "ethertype": item.get("ethertype") or item.get("ether_type"),
                "protocol": item.get("protocol"),
                "port_range_min": item.get("port_range_min"),
                "port_range_max": item.get("port_range_max"),
                "remote_ip_prefix": item.get("remote_ip_prefix"),
                "remote_group_id": item.get("remote_group_id"),
            }
        )
    return out


def _quota_ints(
    payload: dict[str, Any] | None, keys: tuple[str, ...]
) -> dict[str, int]:
    if not isinstance(payload, dict):
        return {}
    out: dict[str, int] = {}
    for key in keys:
        if key not in payload or payload[key] is None or payload[key] == "":
            continue
        try:
            out[key] = int(payload[key])
        except (TypeError, ValueError):
            continue
    return out


def _name_map(rows: list[Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        rid, name = row.get("id"), row.get("name")
        if rid and name:
            out[str(rid)] = str(name)
    return out


def _ref_label(ref: Any, names: dict[str, str] | None) -> Any:
    names = names or {}
    if isinstance(ref, dict):
        rid = ref.get("id")
        name = ref.get("name") or ref.get("original_name")
        if name:
            return name
        if rid:
            return names.get(str(rid), rid)
        return None
    if ref in (None, "", []):
        return None
    key = str(ref)
    return names.get(key, ref)


def _parse_expiry(value: Any) -> float:
    if not value:
        return time.time() + 3600
    text = str(value).replace("Z", "+00:00")
    try:
        from datetime import datetime

        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return time.time() + 3600


def client_from_context(ctx: EnvContext) -> OpenStackClient:
    kube = ctx.kubeconfig
    if not kube:
        raise OpenStackError("no kubeconfig")
    return OpenStackClient(kube)
