"""Generic Redfish client for BMC power/boot control.

Power and boot use standard Redfish resource paths. Read-only hardware
discovery also follows advertised legacy HPE inventory links. TLS
verification is disabled because BMCs ship self-signed certificates (a note
is logged once per process). All functions raise :class:`RedfishError` with
clean messages; the service layer (app.services.baremetal) converts those
into never-raise result dicts.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC
from typing import Any
from urllib.parse import unquote, urlsplit

import httpx

log = logging.getLogger(__name__)

TIMEOUT_SECONDS = 10.0

# power action -> Redfish ResetType
RESET_TYPES = {"on": "On", "off": "ForceOff", "restart": "ForceRestart"}

SYSTEM_PATH = "/redfish/v1/Systems/1"
RESET_PATH = f"{SYSTEM_PATH}/Actions/ComputerSystem.Reset"
ETHERNET_INTERFACES_PATH = f"{SYSTEM_PATH}/EthernetInterfaces"

_tls_note_logged = False


class RedfishError(RuntimeError):
    """A Redfish call failed (transport error or non-2xx response)."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def _base_url(host: str) -> str:
    host = host.strip().rstrip("/")
    if not host.startswith(("http://", "https://")):
        host = f"https://{host}"
    return host


SESSION_PATH = "/redfish/v1/SessionService/Sessions/"


def _client(host: str, username: str, password: str) -> httpx.Client:
    global _tls_note_logged
    if not _tls_note_logged:
        # BMCs present self-signed certs; verify=False is deliberate.
        log.info(
            "redfish: TLS verification disabled — BMCs use self-signed certificates"
        )
        _tls_note_logged = True
    return httpx.Client(
        base_url=_base_url(host),
        auth=(username, password),
        verify=False,
        timeout=TIMEOUT_SECONDS,
        follow_redirects=True,
    )


def _decode(response: httpx.Response) -> dict[str, Any] | None:
    if not response.content:
        return None
    try:
        data = response.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _open_session(client: httpx.Client, username: str, password: str) -> bool:
    """iLO 4 (and similar) reject Basic on Systems/*; they want X-Auth-Token.

    POST SessionService with the same credentials. Returns True when a token
    was installed on ``client``. Trailing slash is required — iLO 4 308s
    ``Sessions`` without it and drops the POST body.
    """
    try:
        response = client.request(
            "POST",
            SESSION_PATH,
            json={"UserName": username, "Password": password},
        )
    except httpx.HTTPError:
        return False
    if response.status_code >= 400:
        return False
    token = response.headers.get("X-Auth-Token") or response.headers.get("x-auth-token")
    if not token:
        return False
    client.headers["X-Auth-Token"] = token
    client.auth = None
    return True


def _request(
    method: str,
    host: str,
    username: str,
    password: str,
    path: str,
    json: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """One Redfish request; returns the decoded JSON body (None when empty)."""
    try:
        with _client(host, username, password) as client:
            response = client.request(method, path, json=json)
            # iLO 4: unauthenticated root is 200, Systems/* is 401 until a session exists.
            if response.status_code in (401, 403):
                if _open_session(client, username, password):
                    response = client.request(method, path, json=json)
    except httpx.HTTPError as exc:
        raise RedfishError(f"redfish {method} {path} failed: {exc}") from exc
    if response.status_code >= 400:
        raise RedfishError(
            f"redfish {method} {path}: HTTP {response.status_code}",
            status_code=response.status_code,
        )
    return _decode(response)


def power_state(host: str, username: str, password: str) -> str:
    """Current power state ("On", "Off", …) of the system."""
    data = _request("GET", host, username, password, SYSTEM_PATH) or {}
    state = data.get("PowerState")
    if not state:
        raise RedfishError("redfish Systems/1 response carries no PowerState")
    return str(state)


def hardware_inventory(
    host: str, username: str, password: str, *, check: Callable[[], None] | None = None
) -> dict[str, Any]:
    """Read hardware before provisioning, using one bounded BMC session.

    No power, boot, storage or configuration actions are sent. Follow only
    same-origin Redfish resource paths so a compromised link cannot forward
    BMC credentials elsewhere. Unknown measurements stay unknown.
    """
    from datetime import datetime

    origin = urlsplit(_base_url(host))

    def origin_tuple(url):
        return url.scheme.lower(), url.hostname, url.port or (443 if url.scheme == "https" else 80)

    def safe_path(value: Any) -> str:
        path = value.get("@odata.id") if isinstance(value, dict) else value
        parsed = urlsplit(str(path or ""))
        decoded = unquote(parsed.path)
        if (
            ((parsed.scheme or parsed.netloc) and origin_tuple(parsed) != origin_tuple(origin))
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or not decoded.startswith("/redfish/v1/")
            or ".." in decoded.split("/")
            or "\\" in decoded
        ):
            raise RedfishError("BMC returned an unsafe hardware resource link")
        return parsed.path

    def positive(value: Any) -> float | None:
        if isinstance(value, bool):
            return None
        try:
            result = float(value)
        except (TypeError, ValueError):
            return None
        return result if 0 < result < float("inf") else None

    issues: list[str] = []

    def resource(system: dict[str, Any], name: str):
        """iLO 4 exposes some resources in legacy links beside summaries."""
        value = system.get(name)
        if isinstance(value, dict) and value.get("@odata.id"):
            return value
        legacy = (system.get("links") or {}).get(name)
        if isinstance(legacy, dict) and legacy.get("href"):
            return {"@odata.id": legacy["href"]}
        for vendor in ("Hp", "Hpe"):
            legacy = (((system.get("Oem") or {}).get(vendor) or {}).get("links") or {}).get(name)
            if isinstance(legacy, dict) and legacy.get("href"):
                return {"@odata.id": legacy["href"]}
        return None

    session_path = None
    reads = 0
    with _client(host, username, password) as client:
        # Older iLO firmware closes connections without correctly announcing it.
        # Keep the authentication session, but don't reuse TCP connections.
        client.headers["Connection"] = "close"
        client.follow_redirects = False

        def read(path: str) -> httpx.Response:
            nonlocal reads
            for _ in range(5):
                if check:
                    check()
                reads += 1
                if reads > 256:
                    raise RedfishError("Hardware inventory exceeded its resource limit")
                response = client.get(path)
                if not response.has_redirect_location:
                    return response
                path = safe_path(response.headers["Location"])
            raise RedfishError("Hardware inventory exceeded its redirect limit")

        def get(link: Any, *, optional: bool = False) -> dict[str, Any]:
            path = safe_path(link)
            response = read(path)
            if optional and response.status_code == 404:
                return {}
            if not response.is_success:
                raise RedfishError(
                    "Hardware inventory read failed", status_code=response.status_code
                )
            return _decode(response) or {}

        def members(link: Any) -> list[dict[str, Any]]:
            collection = get(link)
            if collection.get("Members@odata.nextLink"):
                raise RedfishError("Paginated hardware inventory is not yet supported")
            refs = collection.get("Members") or []
            if len(refs) > 128:
                raise RedfishError("Hardware collection exceeded its member limit")
            return [get(ref) for ref in refs]

        def logical_volumes(controller: dict[str, Any], *, legacy: bool) -> list[dict[str, Any]]:
            link = resource(controller, "LogicalDrives" if legacy else "Volumes")
            if not link:
                return []
            volumes = []
            for volume in members(link):
                physical = resource(volume, "DataDrives") if legacy else None
                if physical:
                    attached = members(physical)
                else:
                    attached = [get(ref) for ref in (volume.get("Links") or {}).get("Drives", [])]
                identifiers = volume.get("Identifiers") or []
                identity = volume.get("VolumeUniqueIdentifier")
                if not identity:
                    # Only standardized persistent OS-visible identifiers qualify.
                    identity = next((item.get("DurableName") for item in identifiers
                                     if item.get("DurableNameFormat") in ("NAA", "EUI", "UUID")), None)
                capacity = positive(volume.get("CapacityBytes"))
                if capacity is None and legacy:
                    mib = positive(volume.get("CapacityMiB"))
                    capacity = mib * 1024**2 if mib else None
                volumes.append({
                    "id": safe_path(volume),
                    "unique_identifier": identity,
                    "capacity_bytes": capacity,
                    "raid_type": volume.get("RAIDType") or volume.get("Raid"),
                    "health": (volume.get("Status") or {}).get("Health"),
                    "state": (volume.get("Status") or {}).get("State"),
                    "physical_drive_serials": sorted({str(d["SerialNumber"]) for d in attached
                                                       if d.get("SerialNumber")}),
                    "os_mapping_verified": False,
                })
            return volumes

        try:
            if check:
                check()
            # iLO 4 can return HTTP 200 with anonymous, incomplete inventory.
            # Establish a session first rather than waiting for a 401 challenge.
            login = client.post(SESSION_PATH, json={"UserName": username, "Password": password})
            if login.status_code not in (404, 405):
                token = login.headers.get("X-Auth-Token")
                if not login.is_success or not token:
                    raise RedfishError(
                        "BMC did not authorize hardware inventory", status_code=login.status_code
                    )
                if login.headers.get("Location"):
                    session_path = safe_path(login.headers["Location"])
                client.headers["X-Auth-Token"] = token
                client.auth = None
            systems = members("/redfish/v1/Systems")
            if len(systems) != 1:
                raise RedfishError("Select a single physical system before hardware discovery")
            system = systems[0]
            processors = resource(system, "Processors")
            cpus = members(processors) if processors else []
            active = [
                p
                for p in cpus
                if (p.get("Status") or {}).get("State")
                not in ("Absent", "Disabled", "UnavailableOffline")
            ]
            threads = [positive(p.get("TotalThreads")) for p in active]
            cores = [positive(p.get("TotalEnabledCores") or p.get("TotalCores")) for p in active]
            cpu_threads = (
                int(sum(threads)) if threads and all(t is not None for t in threads) else None
            )
            cpu_cores = int(sum(cores)) if cores and all(c is not None for c in cores) else None
            memory = positive((system.get("MemorySummary") or {}).get("TotalSystemMemoryGiB"))
            if cpu_threads is None:
                issues.append("CPU thread count is unavailable")
            if memory is None:
                issues.append("Installed memory is unavailable")
            drives: dict[str, dict[str, Any]] = {}
            volumes: list[dict[str, Any]] = []
            storage = resource(system, "Storage")
            if storage:
                for controller in members(storage):
                    volumes.extend(logical_volumes(controller, legacy=False))
                    for ref in controller.get("Drives") or []:
                        path = safe_path(ref)
                        drive = get(path)
                        drives[path] = {
                            "id": path,
                            "serial": drive.get("SerialNumber"),
                            "capacity_bytes": positive(drive.get("CapacityBytes")),
                            "media_type": drive.get("MediaType"),
                            "protocol": drive.get("Protocol"),
                            "health": (drive.get("Status") or {}).get("Health"),
                            "state": (drive.get("Status") or {}).get("State"),
                        }
            else:
                # HPE iLO 4's physical-drive schema predates standard Storage.
                # Follow its advertised links; CapacityGB is decimal GB.
                # https://hewlettpackard.github.io/ilo-rest-api-docs/ilo4/
                smart = resource(system, "SmartStorage")
                if smart:
                    root = get(smart)
                    for kind in ("ArrayControllers", "HostBusAdapters"):
                        controllers = resource(root, kind)
                        if not controllers:
                            continue
                        for controller in members(controllers):
                            volumes.extend(logical_volumes(controller, legacy=True))
                            physical = resource(controller, "PhysicalDrives") or resource(
                                controller, "DiskDrives"
                            )
                            if not physical:
                                continue
                            for drive in members(physical):
                                capacity = positive(drive.get("CapacityMiB"))
                                size = capacity * 1024**2 if capacity else None
                                if size is None:
                                    capacity = positive(drive.get("CapacityGB"))
                                    size = capacity * 1000**3 if capacity else None
                                path = safe_path(drive)
                                drives[path] = {
                                    "id": path,
                                    "serial": drive.get("SerialNumber"),
                                    "capacity_bytes": size,
                                    "media_type": drive.get("MediaType"),
                                    "protocol": drive.get("InterfaceType"),
                                    "health": (drive.get("Status") or {}).get("Health"),
                                    "state": (drive.get("Status") or {}).get("State"),
                                }
                if not drives:
                    issues.append("Physical drive inventory is unavailable")
            nics = []
            interfaces = resource(system, "EthernetInterfaces")
            if interfaces:
                for nic in members(interfaces):
                    nics.append(
                        {
                            "id": nic.get("Id"),
                            "mac": nic.get("MACAddress") or nic.get("MacAddress"),
                            "link_status": nic.get("LinkStatus"),
                            "speed_mbps": positive(nic.get("SpeedMbps")),
                        }
                    )
            else:
                issues.append("Network interface inventory is unavailable")
            adapters = resource(system, "NetworkAdapters")
            if adapters:
                for adapter in members(adapters):
                    for port in adapter.get("PhysicalPorts") or []:
                        mac = port.get("MACAddress") or port.get("MacAddress")
                        if mac:
                            nics.append(
                                {
                                    "id": port.get("Name") or mac,
                                    "mac": mac,
                                    "link_status": port.get("LinkStatus"),
                                    "speed_mbps": positive(port.get("SpeedMbps")),
                                }
                            )
            return {
                "source": "redfish",
                "observed_at": datetime.now(UTC).isoformat(),
                "system_uuid": system.get("UUID"),
                "serial": system.get("SerialNumber"),
                "model": system.get("Model"),
                "power_state": system.get("PowerState"),
                "cpu_threads": cpu_threads,
                "cpu_cores": cpu_cores,
                "memory_gib": memory,
                "drives": list(drives.values()),
                "logical_volumes": volumes,
                "nics": nics,
                "issues": issues,
                "hardware_modified": False,
            }
        except httpx.HTTPError as exc:
            raise RedfishError("Hardware inventory transport failed") from exc
        finally:
            if session_path:
                try:
                    client.delete(session_path)
                except httpx.HTTPError:
                    log.warning("Hardware discovery could not close its BMC session")


def set_pxe_boot(host: str, username: str, password: str) -> None:
    """Set a one-shot PXE boot (BootSourceOverrideTarget=Pxe, Once)."""
    _request(
        "PATCH",
        host,
        username,
        password,
        SYSTEM_PATH,
        json={
            "Boot": {
                "BootSourceOverrideEnabled": "Once",
                "BootSourceOverrideTarget": "Pxe",
            }
        },
    )


def boot_override(host: str, username: str, password: str) -> str:
    """Read BootSourceOverrideTarget back from the system. Empty when absent."""
    data = _request("GET", host, username, password, SYSTEM_PATH) or {}
    boot = data.get("Boot") if isinstance(data.get("Boot"), dict) else {}
    return str(boot.get("BootSourceOverrideTarget") or "")


def power(host: str, username: str, password: str, action: str) -> str:
    """Power action via ComputerSystem.Reset; returns the ResetType sent."""
    reset_type = RESET_TYPES.get(str(action).strip().lower())
    if reset_type is None:
        valid = ", ".join(sorted(RESET_TYPES))
        raise RedfishError(f"unknown power action '{action}' (valid: {valid})")
    _request(
        "POST", host, username, password, RESET_PATH, json={"ResetType": reset_type}
    )
    return reset_type


def system_macs(host: str, username: str, password: str) -> list[str]:
    """MAC addresses of the system's ethernet interfaces (helps fill pxe_mac)."""
    data = _request("GET", host, username, password, ETHERNET_INTERFACES_PATH) or {}
    members = data.get("Members") or []
    macs: list[str] = []
    for member in members:
        odata = member.get("@odata.id") if isinstance(member, dict) else None
        if not odata:
            continue
        iface = _request("GET", host, username, password, str(odata)) or {}
        mac = iface.get("MACAddress") or iface.get("PermanentMACAddress")
        if mac:
            macs.append(str(mac))
    return macs


def insert_virtual_media(
    host: str, username: str, password: str, image_url: str
) -> str:
    """Insert a virtual CD via the generic Redfish InsertMedia action.

    iLO and iDRAC share this path. There is no vendor-specific manager URL.
    """
    image = (image_url or "").strip()
    if not image:
        raise RedfishError("image_url is empty")
    path = "/redfish/v1/Managers/1/VirtualMedia/CD/Actions/VirtualMedia.InsertMedia"
    _request(
        "POST",
        host,
        username,
        password,
        path,
        json={"Image": image, "Inserted": True},
    )
    return path


def force_restart(host: str, username: str, password: str) -> None:
    """Force system restart (cold boot)."""
    power(host, username, password, "ForceRestart")
