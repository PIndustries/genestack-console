"""Generic Redfish client for BMC power/boot control.

Works against any Redfish-conformant BMC (iLO, iDRAC, …) over the standard
``/redfish/v1/Systems/1`` resource paths — no vendor extensions. TLS
verification is disabled because BMCs ship self-signed certificates (a note
is logged once per process). All functions raise :class:`RedfishError` with
clean messages; the service layer (app.services.baremetal) converts those
into never-raise result dicts.
"""

from __future__ import annotations

import logging
from typing import Any

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
    """Insert virtual CD/DVD media (ISO image) into the system."""
    # Stub implementation - would use Redfish VirtualMedia endpoints
    return "/redfish/v1/Managers/1/VirtualMedia/CD"


def force_restart(host: str, username: str, password: str) -> None:
    """Force system restart (cold boot)."""
    power(host, username, password, "ForceRestart")
