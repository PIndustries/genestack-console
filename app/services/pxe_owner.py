"""Reload the API's existing PXE runtime instead of binding a second owner."""

from __future__ import annotations

import os
from typing import Any
from uuid import UUID

import httpx


def ready(runtime: dict[str, Any]) -> bool:
    return all(runtime.get(key) is True for key in ("http_running", "dhcp_running", "tftp_running"))


def address_in_use(runtime: dict[str, Any]) -> bool:
    return any(
        "address already in use" in str(runtime.get(key) or "").lower()
        for key in ("http_error", "dhcp_error", "tftp_error")
    )


def reload_api_owner(environment_id: str, settings) -> dict[str, Any]:
    """Use existing machine credentials only on loopback, with no redirects.

    The endpoint only reapplies runtime state from its own database. It never
    renders over the worker's new boot files or recursively delegates again.
    """
    try:
        identity = str(UUID(environment_id))
        port = int(os.environ.get("GSC_PORT") or settings.port)
        if not 1 <= port <= 65535:
            raise ValueError("Invalid listener")
        key = next(
            key
            for key, role in settings.parsed_api_keys().items()
            if key and role in {"admin", "operator", "platform-admin"}
        )
        with httpx.Client(timeout=30, trust_env=False, follow_redirects=False) as client:
            response = client.post(
                f"http://127.0.0.1:{port}/api/v1/environments/{identity}/pxe/runtime/reload",
                headers={"X-API-Key": key},
            )
            response.raise_for_status()
            payload = response.json()
        runtime = payload.get("runtime")
        if (
            payload.get("environment_id") == identity
            and payload.get("ok") is True
            and isinstance(runtime, dict)
            and ready(runtime)
        ):
            return {**runtime, "owner": "api"}
    except (ValueError, TypeError, AttributeError, StopIteration, httpx.HTTPError):
        pass
    # Never return response bodies, credential-bearing URLs or auth headers.
    return {
        "http_running": False,
        "dhcp_running": False,
        "tftp_running": False,
        "http_error": "Could not reload the existing Console PXE owner; no second runtime was started",
    }
