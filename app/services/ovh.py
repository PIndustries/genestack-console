"""OVH API client for dedicated-server import.

Talks to the OVHcloud REST API over HTTP (httpx). Request signing follows the
OVH v1 scheme (SHA-1 over app_secret + consumer_key + METHOD + full URL +
body + server-time-synced timestamp) and the consumer-key flow follows the
official ``ovh`` python reference client:

- ``POST /auth/credential`` (app-auth only) returns ``consumerKey`` +
  ``validationUrl``; the operator approves it once in their OVH account.
- ``GET /auth/credential/{key}`` reports ``validationStatus`` (ok/pendingValidation/...).
- ``GET /dedicated/server`` lists server ids; each id is then fetched for its
  attributes (cores, memory, datacenter, ips, ...).

The app key/secret AND the consumer key (list + reinstall + IPMI) live on
:class:`~app.models.OvhAccount` rows (one OVH account each, admin-managed);
environments bind to an account to reuse its key. No mock mode: an
unconfigured account reports errors (503) — we never fake servers.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Mapping
from urllib.parse import quote, urlencode

import httpx

log = logging.getLogger(__name__)

#: Default OVH endpoint used when an account omits one.
DEFAULT_ENDPOINT = "https://eu.api.ovh.com/1.0"

#: The three OVHcloud regions documented by OVH (order = dropdown order).
OVH_ENDPOINTS: list[dict[str, str]] = [
    {"region": "EU", "endpoint": "https://eu.api.ovh.com/1.0"},
    {"region": "US", "endpoint": "https://api.us.ovhcloud.com/1.0"},
    {"region": "CA", "endpoint": "https://ca.api.ovh.com/1.0"},
]

#: Consumer-key rules for Connect. Listing is GET; BYOI reinstall and IPMI
#: KVM need POST on the dedicated-server tree. Existing keys minted before
#: these POST rules must be re-Connected.
CONSUMER_KEY_RULES: list[dict[str, str]] = [
    {"method": "GET", "path": "/me"},
    {"method": "GET", "path": "/dedicated/server"},
    {"method": "GET", "path": "/dedicated/server/*"},
    {"method": "POST", "path": "/dedicated/server/*/reinstall"},
    {"method": "POST", "path": "/dedicated/server/*/features/ipmi*"},
    {"method": "GET", "path": "/vrack"},
    {"method": "GET", "path": "/vrack/*"},
    {"method": "POST", "path": "/vrack/*"},
    {"method": "DELETE", "path": "/vrack/*"},
    {"method": "GET", "path": "/order/catalog"},
]

#: OVH generic bring-your-own-image OS. Used when a Talos factory image URL
#: is supplied; catalog OSes (debian12_64, …) are for non-image reinstalls.
DEFAULT_BYOI_OS = "byoi_64"

#: Talos metal images boot via the generic EFI fallback path. OVH requires
#: this field on BYOI even though Talos also has a fallback bootloader.
DEFAULT_EFI_BOOTLOADER = r"\EFI\BOOT\BOOTX64.EFI"


class OvhError(Exception):
    """Raised when an OVH API call fails or configuration is invalid."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass
class OvhClient:
    """HTTP client for the OVH API with v1 request signing.

    - live: endpoint + app key/secret + consumer key — signed, authenticated
      calls (server listing).
    - app-only: endpoint + app key/secret — unsigned credential-flow calls
      (request_consumer_key).
    - unconfigured: missing endpoint/app credentials — reads return empty
      lists, ops raise :class:`OvhError` (503).
    """

    endpoint: str
    app_key: str = ""
    app_secret: str = ""
    consumer_key: str = ""
    timeout: float = 30.0
    _client: httpx.Client | None = field(default=None, repr=False, compare=False)
    _time_delta_cache: int | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        self.endpoint = (self.endpoint or "").strip().rstrip("/")
        if self.endpoint and not self.endpoint.endswith("/1.0"):
            # Accept the bare host or a URL that already contains /1.0;
            # normalise so signing sees the exact URL that is sent.
            if not self.endpoint.startswith(("http://", "https://")):
                self.endpoint = "https://" + self.endpoint
            if "/1.0" not in self.endpoint:
                self.endpoint = self.endpoint.rstrip("/") + "/1.0"
        if self._client is None and self.endpoint:
            self._client = httpx.Client(timeout=self.timeout)

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    @property
    def configured(self) -> bool:
        """True when platform app credentials are present."""
        return bool(self.endpoint) and bool(self.app_key) and bool(self.app_secret)

    @property
    def authenticated(self) -> bool:
        """True when a consumer key is available for signed calls."""
        return self.configured and bool(self.consumer_key)

    def _require_configured(self) -> None:
        if not self.configured:
            raise OvhError(
                "OVH account is not configured (set endpoint + app key + app secret)",
                status_code=503,
            )

    def _require_authenticated(self) -> None:
        if not self.authenticated:
            raise OvhError(
                "No OVH consumer key — run the consumer-key flow first",
                status_code=503,
            )

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    # ------------------------------------------------------------------
    # Low-level signed call
    # ------------------------------------------------------------------

    def _full_url(self, path: str, params: Mapping[str, str] | None) -> str:
        url = self.endpoint + (path if path.startswith("/") else "/" + path)
        if params:
            # Deterministic order: the signed URL and the sent URL must be
            # byte-identical, so sort and use the same string for both.
            query = urlencode(sorted(params.items()))
            url = f"{url}?{query}"
        return url

    def _sign(
        self,
        method: str,
        url: str,
        body: str,
        timestamp: str,
    ) -> str:
        message = "+".join(
            [self.app_secret, self.consumer_key, method.upper(), url, body, timestamp]
        )
        return "$1$" + hashlib.sha1(message.encode("utf-8")).hexdigest()

    def _sync_time_delta(self) -> int:
        if self._time_delta_cache is None:
            server_time = self._call("GET", "/auth/time", params=None, need_auth=False)
            self._time_delta_cache = int(server_time) - int(time.time())
        return self._time_delta_cache

    def _call(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        body: Mapping[str, Any] | None = None,
        need_auth: bool = True,
    ) -> Any:
        if self._client is None:
            raise OvhError("OVH client is not connected (unconfigured)")
        self._require_configured()

        body_str = ""
        if body is not None:
            # Compact separators: the signed body must match the sent body.
            body_str = json.dumps(body, separators=(",", ":"))

        url = self._full_url(path, params)
        headers = {"X-Ovh-Application": self.app_key}
        if need_auth:
            self._require_authenticated()
            now = str(int(time.time()) + self._sync_time_delta())
            headers["X-Ovh-Consumer"] = self.consumer_key
            headers["X-Ovh-Timestamp"] = now
            headers["X-Ovh-Signature"] = self._sign(method, url, body_str, now)

        request_kwargs: dict[str, Any] = {"headers": headers, "timeout": self.timeout}
        if body_str:
            request_kwargs["content"] = body_str
            request_kwargs["headers"] = {**headers, "Content-Type": "application/json"}

        try:
            response = self._client.request(method, url, **request_kwargs)
        except httpx.HTTPError as exc:
            raise OvhError(f"OVH request failed: {exc}") from exc

        if response.status_code >= 100 and response.status_code < 300:
            if response.status_code == 204 or not response.text:
                return None
            try:
                return response.json()
            except ValueError as exc:
                raise OvhError("OVH returned non-JSON response") from exc

        message = response.text[:500]
        try:
            payload = response.json()
            if isinstance(payload, dict):
                message = str(
                    payload.get("message") or payload.get("faults") or message
                )
                code = payload.get("errorCode")
        except ValueError:
            code = None

        if response.status_code == 403:
            if code in (
                "NOT_GRANTED_CALL",
                "NOT_CREDENTIAL",
                "INVALID_KEY",
                "INVALID_CREDENTIAL",
                "FORBIDDEN",
            ):
                raise OvhError(
                    f"OVH denied the call ({code}): {message}", status_code=403
                )
        if response.status_code == 404:
            raise OvhError(f"OVH resource not found: {path}", status_code=404)
        raise OvhError(
            f"OVH error {response.status_code}: {message}",
            status_code=response.status_code,
        )

    # ------------------------------------------------------------------
    # High-level operations
    # ------------------------------------------------------------------

    def ping(self) -> None:
        """Unauthenticated reachability probe (GET /auth/time).

        Raises :class:`OvhError` on network failure or an unreachable /
        mis-formed endpoint. Use after creating/updating an account.
        """
        self._sync_time_delta()

    def whoami(self) -> dict[str, Any]:
        """Authenticated identity check (GET /me)."""
        data = self._call("GET", "/me")
        return data if isinstance(data, dict) else {"ok": bool(data)}

    def request_consumer_key(
        self,
        access_rules: list[dict[str, str]] | None = None,
        *,
        redirection: str | None = None,
    ) -> dict[str, Any]:
        """Create a consumer key (app-auth only, NOT signed).

        Returns ``{consumerKey, validationUrl, state}``; the operator must
        visit ``validationUrl`` and approve before the key is usable.
        """
        payload: dict[str, Any] = {
            "accessRules": access_rules or CONSUMER_KEY_RULES,
        }
        if redirection:
            payload["redirection"] = redirection
        data = self._call("POST", "/auth/credential", body=payload, need_auth=False)
        if not isinstance(data, dict) or "consumerKey" not in data:
            raise OvhError("OVH consumer-key request returned no consumerKey")
        return data

    def credential_state(self, consumer_key: str) -> dict[str, Any]:
        """Validation status of a (possibly new) consumer key.

        Uses the app+consumer signature; OVH answers with a dict including
        ``validationStatus`` ("ok", "pendingValidation", "expired", ...).
        """
        self.consumer_key = consumer_key or self.consumer_key
        self._require_authenticated()
        data = self._call("GET", f"/auth/credential/{consumer_key}")
        return data if isinstance(data, dict) else {"validationStatus": "unknown"}

    def _server_specs(self, server_name: str) -> dict[str, Any] | None:
        """Fetch ``/specifications/hardware`` and map onto normalizer keys.

        Available on every region (EU/US/CA). Returns a partial dict with
        ``cores`` / ``memory`` (MB) / ``disk`` (list of ``{size}``) only when
        OVH supplies them; ``None`` on any failure so the caller keeps the
        base object (fields then degrade to "—" in the UI).
        """
        try:
            spec = self._call(
                "GET", f"/dedicated/server/{server_name}/specifications/hardware"
            )
        except OvhError as exc:
            log.debug("OVH: specs unavailable for %s (%s)", server_name, exc)
            return None
        if not isinstance(spec, dict):
            return None
        out: dict[str, Any] = {}
        mem = spec.get("memorySize")
        if isinstance(mem, dict):
            val = _num(mem.get("value"))
            if val is not None:
                out["memory"] = val  # MB — normalize_server converts to GB
        cpp = _num(spec.get("coresPerProcessor"))
        nproc = _num(spec.get("numberOfProcessors"))
        if cpp is not None:
            out["cores"] = cpp * (nproc or 1)
        total = 0
        for group in spec.get("diskGroups") or []:
            if not isinstance(group, dict):
                continue
            ds = group.get("diskSize")
            size = _num(ds.get("value") if isinstance(ds, dict) else ds)
            count = _num(group.get("numberOfDisks")) or 1
            if size:
                total += size * count
        if total:
            out["disk"] = [{"size": total}]
        return out or None

    def list_dedicated_servers(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """List the account's dedicated servers with details (defensive parse).

        ``GET /dedicated/server`` returns a list of server names; each name is
        fetched for its attributes and, when available, its hardware
        specifications (cores/RAM/disk). Capped at ``limit`` servers.
        """
        ids = self._call("GET", "/dedicated/server")
        if not isinstance(ids, list):
            return []
        servers: list[dict[str, Any]] = []
        for sid in ids[:limit]:
            if not isinstance(sid, str):
                continue
            try:
                raw = self._call("GET", f"/dedicated/server/{sid}")
            except OvhError as exc:
                log.warning("OVH: skipping %s (%s)", sid, exc)
                continue
            if not isinstance(raw, dict):
                continue
            specs = self._server_specs(sid)
            if specs:
                merged = dict(raw)
                for key, value in specs.items():
                    if merged.get(key) in (None, "", []):
                        merged[key] = value
                raw = merged
            extra_ips = self.list_server_ips(sid)
            nics = self.list_nics(sid)
            servers.append(normalize_server(raw, extra_ips=extra_ips, nics=nics))
        return servers

    def list_server_ips(self, server_name: str) -> list[str]:
        """All IPs routed to a dedicated server (public + vRack/private).

        ``GET /dedicated/server/{serviceName}/ips``. Missing/empty degrades
        to ``[]`` so callers still classify whatever the server object had.
        """
        try:
            data = self._call("GET", f"/dedicated/server/{server_name}/ips")
        except OvhError:
            return []
        out: list[str] = []
        if isinstance(data, list):
            for item in data:
                addr = None
                if isinstance(item, str):
                    addr = item.split("/", 1)[0].strip()
                elif isinstance(item, dict):
                    addr = _first_str(item, "ip", "ipAddress", "address", "ipBlock")
                    if addr:
                        addr = addr.split("/", 1)[0].strip()
                if addr and addr not in out:
                    out.append(addr)
        return out

    def list_nics(self, server_name: str) -> list[dict[str, Any]]:
        """Network interfaces (MAC, link type, vRack VNI) for a dedicated server.

        ``GET /dedicated/server/{serviceName}/networkInterfaceController``
        returns MAC strings or objects; each MAC is then fetched for
        ``linkType`` / ``virtualNetworkInterface``. Empty on any failure.
        """
        try:
            data = self._call(
                "GET", f"/dedicated/server/{server_name}/networkInterfaceController"
            )
        except OvhError:
            return []
        macs: list[str] = []
        if isinstance(data, list):
            for item in data:
                if isinstance(item, str) and item.strip():
                    macs.append(item.strip())
                elif isinstance(item, dict):
                    mac = _first_str(item, "mac", "macAddress", "address")
                    if mac:
                        macs.append(mac)
        nics: list[dict[str, Any]] = []
        for mac in macs:
            detail: dict[str, Any] = {"mac": mac}
            try:
                raw = self._call(
                    "GET",
                    f"/dedicated/server/{server_name}/networkInterfaceController/"
                    f"{quote(mac, safe='')}",
                )
            except OvhError:
                raw = {}
            if isinstance(raw, dict) and raw:
                detail["link_type"] = (
                    _first_str(raw, "linkType", "type") or ""
                ).lower()
                detail["vni"] = _first_str(
                    raw, "virtualNetworkInterface", "virtualNetworkInterfaceId", "uuid"
                )
            nics.append(detail)
        return nics

    def list_vracks(self) -> list[str]:
        """vRack service names (``pn-…``). ``GET /vrack``."""
        try:
            data = self._call("GET", "/vrack")
        except OvhError:
            return []
        if not isinstance(data, list):
            return []
        return [str(item).strip() for item in data if str(item).strip()]

    def get_vrack(self, vrack: str) -> dict[str, Any]:
        try:
            data = self._call("GET", f"/vrack/{vrack}")
        except OvhError as exc:
            if exc.status_code == 404:
                return {}
            raise
        return data if isinstance(data, dict) else {}

    def vrack_eligible_services(self, vrack: str) -> dict[str, Any]:
        try:
            data = self._call("GET", f"/vrack/{vrack}/eligibleServices")
        except OvhError:
            return {}
        return data if isinstance(data, dict) else {}

    def vrack_dedicated_servers(self, vrack: str) -> list[str]:
        try:
            data = self._call("GET", f"/vrack/{vrack}/dedicatedServer")
        except OvhError:
            return []
        if not isinstance(data, list):
            return []
        return [str(item).strip() for item in data if str(item).strip()]

    def vrack_dedicated_interfaces(self, vrack: str) -> list[str]:
        try:
            data = self._call("GET", f"/vrack/{vrack}/dedicatedServerInterface")
        except OvhError:
            return []
        if not isinstance(data, list):
            return []
        return [str(item).strip() for item in data if str(item).strip()]

    def vrack_interface_details(self, vrack: str) -> list[dict[str, Any]]:
        """Who is actually plugged into this vRack (Rise-style VNI attach)."""
        try:
            data = self._call("GET", f"/vrack/{vrack}/dedicatedServerInterfaceDetails")
        except OvhError:
            return []
        if not isinstance(data, list):
            return []
        out: list[dict[str, Any]] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            out.append(
                {
                    "server": _first_str(
                        item, "dedicatedServer", "server", "name", "domain"
                    ),
                    "interface": _first_str(
                        item, "dedicatedServerInterface", "interface", "id"
                    ),
                }
            )
        return out

    def attach_server_to_vrack(self, vrack: str, server_name: str) -> Any:
        """Legacy attach: whole server into the vRack (older SKUs)."""
        return self._call(
            "POST",
            f"/vrack/{vrack}/dedicatedServer",
            body={"dedicatedServer": server_name},
        )

    def attach_interface_to_vrack(self, vrack: str, interface_id: str) -> Any:
        """Rise/VNI attach: private NIC into the vRack."""
        return self._call(
            "POST",
            f"/vrack/{vrack}/dedicatedServerInterface",
            body={"dedicatedServerInterface": interface_id},
        )

    def list_vrack_ips(self, vrack: str) -> list[dict[str, Any]]:
        """IP blocks on a vRack, including any VLAN number OVH has recorded.

        Dedicated Rise boxes typically pick the 802.1q tag in the OS (Talos)
        rather than via this API; we still surface the blocks so the operator
        can look up an existing VLAN instead of inventing one.
        """
        try:
            data = self._call("GET", f"/vrack/{vrack}/ip")
        except OvhError:
            return []
        blocks: list[str] = []
        if isinstance(data, list):
            blocks = [str(item).strip() for item in data if str(item).strip()]
        out: list[dict[str, Any]] = []
        for block in blocks:
            detail: dict[str, Any] = {}
            try:
                raw = self._call("GET", f"/vrack/{vrack}/ip/{quote(block, safe='')}")
            except OvhError:
                raw = {}
            if isinstance(raw, dict):
                detail = raw
            vlan_raw = detail.get("vlan")
            if vlan_raw is None:
                vlan_raw = detail.get("vlanId") or detail.get("vlanNumber")
            vlan: int | None = None
            if vlan_raw is not None and vlan_raw != "":
                try:
                    vlan = int(vlan_raw)
                except (TypeError, ValueError):
                    vlan = None
            out.append(
                {
                    "ip": _first_str(detail, "ip", "ipBlock", "block") or block,
                    "vlan": vlan,
                    "gateway": _first_str(detail, "gateway"),
                    "zone": _first_str(detail, "zone"),
                }
            )
        return out

    def get_server(self, server_name: str) -> dict[str, Any]:
        """Fetch one dedicated server's raw attributes (``{}`` when 404).

        Used to resolve a server's hardware reference (for
        :meth:`list_os_templates`) without a full inventory sweep.
        """
        try:
            data = self._call("GET", f"/dedicated/server/{server_name}")
        except OvhError as exc:
            if exc.status_code == 404:
                return {}
            raise
        return data if isinstance(data, dict) else {}

    def reinstall_server(
        self,
        server_name: str,
        operating_system: str,
        *,
        customizations: Mapping[str, Any] | None = None,
        storage: list[Mapping[str, Any]] | None = None,
    ) -> Any:
        """Reinstall a server from an OS template (BYOI).

        ``POST /dedicated/server/{serviceName}/reinstall`` with body
        ``{"operatingSystem": ...}`` plus optional ``customizations`` /
        ``storage``. OVH returns the raw task id (a string in most
        regions; callers coerce defensively).
        """
        body: dict[str, Any] = {"operatingSystem": operating_system}
        if customizations:
            body["customizations"] = dict(customizations)
        if storage:
            body["storage"] = [dict(s) for s in storage]
        return self._call(
            "POST", f"/dedicated/server/{server_name}/reinstall", body=body
        )

    def list_os_templates(self, hardware: str) -> list[str]:
        """OS template names installable on a hardware reference.

        ``GET /dedicated/server/osAvailabilities?hardware=<ref>`` returns a
        list of template names; OVH answers ``[]`` for references it has no
        catalog entry for, and the shape is guarded anyway (non-lists
        degrade to ``[]``).
        """
        data = self._call(
            "GET",
            "/dedicated/server/osAvailabilities",
            params={"hardware": hardware},
        )
        if isinstance(data, list):
            return [str(t) for t in data if isinstance(t, (str, int))]
        return []

    def list_compatible_templates(self, server_name: str) -> dict[str, Any]:
        """Templates installable on a specific server, by category.

        ``GET /dedicated/server/{serviceName}/install/compatibleTemplates``
        — OVH documents an ``ovh`` key for catalog OSes and a ``byoi`` key
        for bring-your-own-image data; the shape varies by region, so the
        raw mapping is returned as-is (non-dicts degrade to ``{}``).
        """
        data = self._call(
            "GET", f"/dedicated/server/{server_name}/install/compatibleTemplates"
        )
        return data if isinstance(data, dict) else {}

    def install_status(self, server_name: str) -> dict[str, Any]:
        """Current install state of a server (defensive: raw mapping or {})."""
        data = self._call("GET", f"/dedicated/server/{server_name}/install/status")
        return data if isinstance(data, dict) else {}


def infer_ovh_image_type(image_url: str) -> str:
    """OVH BYOI ``imageType``: qcow2 if the URL says so, otherwise raw.

    Talos factory disk images are ``metal-amd64.raw.xz`` / ``.qcow2``. OVH
    documents ``raw`` and ``qcow2``; compressed raw is still typed ``raw``.
    """
    path = str(image_url or "").split("?", 1)[0].lower()
    if path.endswith(".qcow2") or ".qcow2." in path:
        return "qcow2"
    return "raw"


def byoi_customizations(
    *,
    image_url: str,
    hostname: str | None = None,
    ssh_key: str | None = None,
    efi_bootloader_path: str | None = None,
    image_type: str | None = None,
) -> dict[str, Any]:
    """OVH ``customizations`` hash for a Talos (or other) factory image."""
    url = str(image_url or "").strip()
    custom: dict[str, Any] = {
        "imageURL": url,
        "imageType": image_type or infer_ovh_image_type(url),
        "efiBootloaderPath": (efi_bootloader_path or DEFAULT_EFI_BOOTLOADER),
    }
    host = str(hostname or "").strip()
    if host:
        custom["hostname"] = host
    key = str(ssh_key or "").strip()
    if key:
        custom["sshKey"] = key
    return custom


def classify_install_status(payload: Any) -> str:
    """Map an OVH ``install/status`` body to ``doing`` / ``done`` / ``error``.

    OVH's shape varies by region: a top-level ``status``, a ``progress``
    list of steps, or an empty body after the task is reaped. 404 is handled
    by the waiter (cleared status → done).
    """
    if payload is None:
        return "done"
    if not isinstance(payload, dict) or not payload:
        return "doing"
    status = str(payload.get("status") or payload.get("state") or "").strip().lower()
    if status in ("error", "failed", "expired", "cancelled", "canceled"):
        return "error"
    if status in ("done", "finished", "ok", "completed", "success"):
        return "done"
    progress = payload.get("progress")
    if isinstance(progress, list) and progress:
        states = [
            str(step.get("status") or "").lower()
            for step in progress
            if isinstance(step, dict)
        ]
        if any(s in ("error", "failed") for s in states):
            return "error"
        if any(s in ("doing", "running", "pending", "todo") for s in states):
            return "doing"
        if states and all(s in ("done", "finished", "ok") for s in states):
            return "done"
    if status in ("doing", "running", "pending", "installing"):
        return "doing"
    return "doing"


# ----------------------------------------------------------------------
# Environment / inventory identity
# ----------------------------------------------------------------------
# An environment is OVH when it is bound to an OvhAccount. Dedicated servers
# imported from that account are source: ovh. Static rows in an OVH-bound
# env that match the live inventory (IP, then hostname) inherit the same
# identity so Talos BYOI does not depend on the operator re-tagging YAML.
# Bare-metal, terraform, and older saved sources do not inherit OVH identity.

NON_OVH_SOURCES = frozenset({"maas", "baremetal", "terraform"})


def env_is_ovh(env: Any) -> bool:
    """True when this environment is bound to an OVH account."""
    return bool(getattr(env, "ovh_account_id", None))


def server_source(entry: Mapping[str, Any] | None) -> str:
    return str((entry or {}).get("source") or "").strip().lower()


def server_is_explicit_ovh(entry: Mapping[str, Any] | None) -> bool:
    return server_source(entry) == "ovh"


def server_may_inherit_ovh(entry: Mapping[str, Any] | None) -> bool:
    """True unless the row is already claimed by another install path."""
    return server_source(entry) not in NON_OVH_SOURCES


def inventory_indexes(
    inventory: list[Mapping[str, Any]],
) -> tuple[dict[str, str], dict[str, str]]:
    """Build IP→service_name and hostname→service_name maps from a live list.

    First writer wins on collisions so a later duplicate IP cannot steal a
    server already matched.
    """
    by_ip: dict[str, str] = {}
    by_host: dict[str, str] = {}
    for srv in inventory:
        sid = str(srv.get("server_id") or "").strip()
        if not sid:
            continue
        for ip in srv.get("ips") or []:
            ip_s = str(ip or "").strip()
            if ip_s and ip_s not in by_ip:
                by_ip[ip_s] = sid
        primary = str(srv.get("ip") or "").strip()
        if primary and primary not in by_ip:
            by_ip[primary] = sid
        host = str(srv.get("hostname") or "").strip()
        if host and host not in by_host:
            by_host[host] = sid
        if sid not in by_host:
            by_host[sid] = sid
    return by_ip, by_host


def resolve_ovh_service_name(
    hostname: str,
    entry: Mapping[str, Any],
    by_ip: Mapping[str, str],
    by_host: Mapping[str, str],
) -> str | None:
    """Prefer an explicit service_name, then IP, then hostname / service id."""
    sn = str(entry.get("service_name") or "").strip() or None
    if sn:
        return sn
    ip = str(entry.get("ip") or "").strip()
    if ip and ip in by_ip:
        return by_ip[ip]
    return by_host.get(hostname)


def planned_ovh_tags(
    servers: Mapping[str, Any],
    inventory: list[Mapping[str, Any]],
) -> dict[str, str]:
    """hostname → OVH service name for doc servers that match live inventory.

    Skips rows that already name another source. Used to persist ``source: ovh`` on import/adopt
    so the config document agrees with the bound account.
    """
    by_ip, by_host = inventory_indexes(inventory)
    out: dict[str, str] = {}
    for hostname, entry in servers.items():
        if not isinstance(entry, dict):
            continue
        if not server_may_inherit_ovh(entry):
            continue
        sn = resolve_ovh_service_name(str(hostname), entry, by_ip, by_host)
        if sn:
            out[str(hostname)] = sn
    return out


# ----------------------------------------------------------------------
# Normalisation + role heuristics (pure, unit-testable, no network)
# ----------------------------------------------------------------------


def _num(value: Any, *, divisor: int = 1) -> int | None:
    """Best-effort int parse; ``divisor`` converts e.g. MB -> GB when set."""
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if not out.is_integer():
        return None
    return int(out) // divisor if divisor else int(out)


def _first_str(raw: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _first_id(raw: dict[str, Any], *keys: str) -> str | None:
    """First non-empty id among ``keys``; ints (e.g. US ``serverId``) coerce to str.

    Passes over ints on the first sweep so a genuine string id elsewhere in
    ``keys`` wins, then falls back to the first int if nothing else matched.
    """
    for key in keys:
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for key in keys:
        value = raw.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return str(value)
    return None


def _first_number(raw: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        value = _num(raw.get(key))
        if value is not None:
            return value
    return None


def classify_ip(addr: str) -> str:
    """``private`` for RFC1918/ULA/link-local, else ``public``.

    Rise 1 vRack addresses are RFC1918. The OVH public NIC is a globally
    routed address. Bad/empty values count as public so we never silently
    treat garbage as the cluster fabric.
    """
    text = str(addr or "").split("/", 1)[0].strip()
    if not text:
        return "public"
    try:
        parsed = ipaddress.ip_address(text)
    except ValueError:
        return "public"
    if parsed.is_private or parsed.is_link_local or parsed.is_reserved:
        return "private"
    return "public"


def split_server_ips(ips: list[str]) -> dict[str, str | None | list[str]]:
    """Split an address list into public / private and pick the cluster IP.

    Cluster IP is the first private address (vRack / RFC1918). If the box
    has no private NIC yet we fall back to the public address and the
    caller must not default-deny the public edge.
    """
    public: list[str] = []
    private: list[str] = []
    seen: set[str] = set()
    for raw in ips:
        addr = str(raw or "").split("/", 1)[0].strip()
        if not addr or addr in seen:
            continue
        seen.add(addr)
        if classify_ip(addr) == "private":
            private.append(addr)
        else:
            public.append(addr)
    cluster = private[0] if private else (public[0] if public else None)
    return {
        "ips": public + private,
        "public_ip": public[0] if public else None,
        "private_ip": private[0] if private else None,
        "ip": cluster,
    }


def _collect_ips(raw: dict[str, Any]) -> list[str]:
    """Collect IPs; OVH sends ``ip`` as a plain string on the US
    endpoint and as a list (of dicts or strings) elsewhere."""
    ips: list[str] = []
    ip_field = raw.get("ip")
    if isinstance(ip_field, str):
        if ip_field.strip():
            ips.append(ip_field.strip())
    elif isinstance(ip_field, list):
        for entry in ip_field:
            if isinstance(entry, dict):
                addr = _first_str(entry, "ip", "address", "net")
            elif isinstance(entry, str):
                addr = entry.strip() or None
            else:
                addr = None
            if addr:
                ips.append(addr)
    if not ips:
        addr = _first_str(raw, "publicIp", "ipAddress")
        if addr:
            ips.append(addr)
    return ips


def pick_private_nic(
    nics: list[Mapping[str, Any]],
    raw: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Choose the vRack/private NIC for VLAN tagging.

    Prefer an enabled vRack VNI from the server object, then a NIC whose
    link type is private/vrack, then the second NIC on a dual-port Rise box.
    """
    raw = raw or {}
    vnis: list[str] = []
    for key in ("enabledVrackVnis", "enabledVrackVNIs"):
        val = raw.get(key)
        if isinstance(val, list):
            vnis.extend(str(item).strip() for item in val if str(item).strip())
        elif isinstance(val, str) and val.strip():
            vnis.append(val.strip())
    for nic in nics:
        vni = str(nic.get("vni") or "").strip()
        if vni and vni in vnis:
            return dict(nic)
    for nic in nics:
        link = str(nic.get("link_type") or "").lower()
        if link in ("private", "vrack", "isolated"):
            return dict(nic)
    if len(nics) >= 2:
        return dict(nics[1])
    return dict(nics[0]) if nics else None


def normalize_server(
    raw: dict[str, Any],
    *,
    extra_ips: list[str] | None = None,
    nics: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Map a raw OVH dedicated-server object to the console's shape.

    Defensive on purpose: the object shape differs by region — the EU/CA
    endpoints expose ``cores``/``memory``/``disk`` and a string ``serverId``,
    while the US (``*.ovhcloud.com``) endpoint exposes neither and an integer
    ``serverId``. Unknown fields degrade to ``None`` (rendered "—" in the UI)
    rather than crashing; ``ram_gb`` remains the primary role-sizing signal.

    ``ip`` is the cluster address: private/vRack when present, else public.
    ``public_ip`` / ``private_ip`` are the dual-NIC split for Talos.
    """
    server_id = (
        _first_id(raw, "serverId", "serverName", "name", "domain", "id") or "unknown"
    )

    ips = _collect_ips(raw)
    for extra in extra_ips or []:
        addr = str(extra or "").split("/", 1)[0].strip()
        if addr and addr not in ips:
            ips.append(addr)
    split = split_server_ips(ips)

    # OVH reports memory in MB; tolerate values that are already GB.
    memory = _first_number(raw, "memory", "ram", "memoryInMb")
    ram_gb = None
    if memory is not None:
        ram_gb = memory // 1024 if memory > 1024 else memory

    disks: list[int] = []
    for disk in raw.get("disk") or []:
        size = _num(disk.get("size") if isinstance(disk, dict) else disk)
        if size is not None:
            disks.append(size)

    # ``commercialRange`` looks like "KS-6 | AMD Epyc 7351P": left of the pipe
    # is the SKU, right is the CPU. Older EU objects carry the SKU as a bare
    # string (no pipe) in ``commercialRange``/``dedicatedServerName``.
    model = None
    cpu = _first_str(raw, "cpu", "cpuModel", "processor")
    range_val = _first_str(
        raw, "commercialRange", "dedicatedServerName", "template", "range"
    )
    if range_val:
        if "|" in range_val:
            left, _, right = range_val.partition("|")
            model = left.strip() or None
            if not cpu:
                cpu = right.strip() or None
        if not model:
            model = range_val

    nic_list = list(nics or [])
    private_nic = pick_private_nic(nic_list, raw)
    return {
        "server_id": server_id,
        "hostname": _first_str(raw, "displayName", "name", "domain") or server_id,
        "ip": split["ip"],
        "public_ip": split["public_ip"],
        "private_ip": split["private_ip"],
        "ips": split["ips"],
        "cores": _first_number(raw, "cores", "cpuCount", "coresCount"),
        "cpu": cpu,
        "ram_gb": ram_gb,
        "disk_gb": max(disks) if disks else None,
        "model": model,
        "datacenter": _first_str(raw, "datacenter", "dc"),
        "region": _first_str(raw, "region", "availabilityZone"),
        "status": _first_str(raw, "status", "state", "powerStatus", "powerState")
        or "unknown",
        "os": _first_str(raw, "os", "operatingSystem"),
        "link_speed_mbps": _first_number(raw, "linkSpeed"),
        "nics": nic_list,
        "private_mac": (private_nic or {}).get("mac"),
        "vrack_vni": (private_nic or {}).get("vni"),
        "raw": raw,
    }


def assign_roles(servers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Auto-assign console roles from hardware, biggest-memory-first.

    Deterministic heuristic (no cluster is deployed yet, so we optimise for
    the roles the wizard requires):

    - top 3 by RAM (tie: cores, then hostname): ``k8s_control_plane`` +
      ``etcd`` + ``control`` (also ``compute`` so nothing sits idle);
    - 4th/5th largest (when >= 5 servers): extra ``etcd`` members (odd quorum
      of 5) + ``compute``;
    - every remaining server: ``compute``;
    - the server with the most disk additionally gets ``storage`` (longhorn),
      even if it is a control node;
    - fewer than 3 servers: every server gets the full required set
      (``k8s_control_plane`` + ``etcd`` + ``control`` + ``compute``) so the
      wizard's required-role check still passes (the user can adjust).

    Returns new dicts (input is not mutated) with a ``roles`` list added.
    """
    ordered = sorted(
        servers,
        key=lambda s: (
            -(s.get("ram_gb") or 0),
            -(s.get("cores") or 0),
            s.get("hostname") or "",
        ),
    )
    by_id: dict[str, dict[str, Any]] = {}
    for srv in ordered:
        out = {k: v for k, v in srv.items() if k != "raw"}
        out["roles"] = []
        by_id[srv.get("server_id")] = out

    def add(server_id: str | None, *roles: str) -> None:
        target = by_id.get(server_id or "")
        if target is None:
            return
        for role in roles:
            if role not in target["roles"]:
                target["roles"].append(role)

    n = len(ordered)
    if n == 0:
        return []
    if n >= 3:
        for srv in ordered[:3]:
            add(srv.get("server_id"), "k8s_control_plane", "etcd", "control", "compute")
        if n >= 5:
            for srv in ordered[3:5]:
                add(srv.get("server_id"), "etcd", "compute")
        for srv in ordered[3 if n < 5 else 5 :]:
            add(srv.get("server_id"), "compute")
    else:
        for srv in ordered:
            add(srv.get("server_id"), "k8s_control_plane", "etcd", "control", "compute")

    disk_top = max(
        (s for s in ordered if (s.get("disk_gb") or 0) > 0),
        key=lambda s: s.get("disk_gb") or 0,
        default=None,
    )
    if disk_top is not None:
        add(disk_top.get("server_id"), "storage")

    return [by_id[s.get("server_id")] for s in ordered]


__all__ = [
    "CONSUMER_KEY_RULES",
    "DEFAULT_BYOI_OS",
    "DEFAULT_EFI_BOOTLOADER",
    "DEFAULT_ENDPOINT",
    "OVH_ENDPOINTS",
    "OvhClient",
    "OvhError",
    "assign_roles",
    "byoi_customizations",
    "classify_install_status",
    "classify_ip",
    "infer_ovh_image_type",
    "normalize_server",
    "pick_private_nic",
    "split_server_ips",
]
