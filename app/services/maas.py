"""MAAS API client for Genestack Console.

Talks to Canonical MAAS over HTTP (httpx). Supports OAuth1 when
``requests-oauthlib`` / ``oauthlib`` are available; otherwise falls back to
mock mode only when explicitly requested (``mock=True``, development only).

MAAS API keys are typically ``consumer_key:token_key:token_secret``.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol

import httpx

logger = logging.getLogger(__name__)


class MaasError(Exception):
    """Raised when a MAAS API call fails or configuration is invalid."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class SupportsMaasSettings(Protocol):
    """Minimal settings surface used by :meth:`MaasClient.from_settings`."""

    maas_url: str
    maas_api_key: str
    maas_mock: bool


# Realistic mock inventory for dry-run / local console development.
# Tags mirror Genestack node roles: control-plane, compute, storage (and network).
_MOCK_MACHINES: list[dict[str, Any]] = [
    {
        "system_id": "abc123",
        "hostname": "gs-control-01",
        "fqdn": "gs-control-01.genestack.local",
        "status_name": "Deployed",
        "status": 6,
        "power_state": "on",
        "architecture": "amd64/generic",
        "distro_series": "noble",
        "osystem": "ubuntu",
        "ip_addresses": ["10.20.0.11"],
        "tag_names": ["control-plane", "openstack-control-plane"],
        "pool": {"name": "default"},
        "zone": {"name": "default"},
        "cpu_count": 16,
        "memory": 65536,
        "interface_set": [
            {
                "name": "ens3",
                "links": [
                    {"ip_address": "10.20.0.11", "subnet": {"cidr": "10.20.0.0/24"}}
                ],
            }
        ],
    },
    {
        "system_id": "def456",
        "hostname": "gs-compute-01",
        "fqdn": "gs-compute-01.genestack.local",
        "status_name": "Ready",
        "status": 4,
        "power_state": "off",
        "architecture": "amd64/generic",
        "distro_series": "noble",
        "osystem": "ubuntu",
        "ip_addresses": ["10.20.0.21"],
        "tag_names": ["compute"],
        "pool": {"name": "default"},
        "zone": {"name": "default"},
        "cpu_count": 32,
        "memory": 131072,
        "interface_set": [
            {
                "name": "ens3",
                "links": [
                    {"ip_address": "10.20.0.21", "subnet": {"cidr": "10.20.0.0/24"}}
                ],
            }
        ],
    },
    {
        "system_id": "ghi789",
        "hostname": "gs-storage-01",
        "fqdn": "gs-storage-01.genestack.local",
        "status_name": "Commissioning",
        "status": 1,
        "power_state": "on",
        "architecture": "amd64/generic",
        "distro_series": "noble",
        "osystem": "ubuntu",
        "ip_addresses": ["10.20.0.31"],
        "tag_names": ["storage"],
        "pool": {"name": "default"},
        "zone": {"name": "default"},
        "cpu_count": 24,
        "memory": 262144,
        "interface_set": [
            {
                "name": "ens3",
                "links": [
                    {"ip_address": "10.20.0.31", "subnet": {"cidr": "10.20.0.0/24"}}
                ],
            }
        ],
    },
]

# Mock status transitions for the write ops, keyed by MAAS op name. Codes are
# the real MAAS node-status codes; "Released" is a mock-only name (a real MAAS
# returns a released machine to Ready/4) kept so tests can observe the full
# Ready -> Commissioning -> Deployed -> Released cycle.
_MOCK_OP_STATUS: dict[str, tuple[str, int]] = {
    "commission": ("Commissioning", 1),
    "deploy": ("Deployed", 6),
    "release": ("Released", 4),
}

_MOCK_TAGS: list[dict[str, Any]] = [
    {"name": "control-plane", "comment": "Genestack OpenStack control plane"},
    {"name": "openstack-control-plane", "comment": "Alias for control-plane label"},
    {"name": "compute", "comment": "Genestack compute nodes"},
    {"name": "network", "comment": "Genestack network nodes"},
    {"name": "storage", "comment": "Genestack storage nodes"},
]


def _parse_api_key(api_key: str) -> tuple[str, str, str]:
    """Parse ``consumer_key:token_key:token_secret``."""
    parts = api_key.split(":")
    if len(parts) != 3 or not all(parts):
        raise MaasError(
            "Invalid MAAS API key format; expected consumer_key:token_key:token_secret"
        )
    return parts[0], parts[1], parts[2]


def _normalize_base_url(url: str) -> str:
    """Ensure URL ends with a trailing slash and points at MAAS API root if needed."""
    url = url.strip().rstrip("/") + "/"
    # Accept either site root or .../MAAS/ or .../MAAS/api/2.0/
    lower = url.lower()
    if "/api/2.0/" in lower:
        # Keep as-is (already API root)
        return url if url.endswith("/") else url + "/"
    if lower.rstrip("/").endswith("/maas"):
        return url + "api/2.0/"
    if "/maas/" in lower:
        # e.g. http://host/MAAS/something -> rebuild carefully
        return url.rstrip("/") + "/api/2.0/" if not lower.endswith("/api/2.0/") else url
    # Bare host:port -> append MAAS api path
    return url + "MAAS/api/2.0/"


def _try_oauth1_auth(
    consumer_key: str, token_key: str, token_secret: str
) -> Any | None:
    """Build an httpx-compatible auth object if oauth libs are installed.

    MAAS uses OAuth1 (PLAINTEXT). ``httpx`` does not ship OAuth; we optionally
    use ``requests-oauthlib`` signing helpers, or a lightweight PLAINTEXT header.
    """
    try:
        from oauthlib.oauth1 import Client as OAuth1Client  # type: ignore[import-untyped]
    except ImportError:
        logger.debug("oauthlib not installed; using PLAINTEXT OAuth1 header builder")
        return _PlaintextOAuth1(consumer_key, token_key, token_secret)

    class _OAuth1HttpxAuth(httpx.Auth):
        def __init__(self, ck: str, tk: str, ts: str) -> None:
            self._client = OAuth1Client(
                ck,
                client_secret="",
                resource_owner_key=tk,
                resource_owner_secret=ts,
                signature_method="PLAINTEXT",
            )

        def auth_flow(self, request: httpx.Request):  # type: ignore[no-untyped-def]
            # Multipart/binary bodies (boot-resource uploads) are not
            # utf-8; PLAINTEXT signing does not need the body anyway.
            try:
                body = request.content.decode("utf-8") if request.content else None
            except UnicodeDecodeError:
                body = None
            uri, headers, _ = self._client.sign(
                str(request.url),
                http_method=request.method,
                body=body,
                headers=dict(request.headers),
            )
            request.headers["Authorization"] = headers["Authorization"]
            # uri may be re-encoded; keep original request URL
            yield request

    return _OAuth1HttpxAuth(consumer_key, token_key, token_secret)


class _PlaintextOAuth1(httpx.Auth):
    """Minimal OAuth1 PLAINTEXT auth for MAAS without external oauth deps."""

    def __init__(self, consumer_key: str, token_key: str, token_secret: str) -> None:
        self.consumer_key = consumer_key
        self.token_key = token_key
        self.token_secret = token_secret

    def auth_flow(self, request: httpx.Request):  # type: ignore[no-untyped-def]
        # oauth_signature for PLAINTEXT is consumer_secret&token_secret;
        # MAAS consumer_secret is empty string.
        signature = f"&{self.token_secret}"
        header = (
            'OAuth oauth_version="1.0", '
            'oauth_signature_method="PLAINTEXT", '
            f'oauth_consumer_key="{self.consumer_key}", '
            f'oauth_token="{self.token_key}", '
            f'oauth_signature="{signature}"'
        )
        request.headers["Authorization"] = header
        yield request


@dataclass
class MaasClient:
    """HTTP client for MAAS machines and tags.

    Three modes:
    - live: ``url`` set (and ``mock`` false) — talks to a real MAAS.
    - mock: ``mock=True`` — serves the built-in sample inventory; explicit
      development opt-in (``maas.mock: true`` in config.yaml).
    - unconfigured: no ``url`` and ``mock`` false — reads return empty lists,
      write ops raise :class:`MaasError` (503). Never fake data.
    """

    url: str
    api_key: str
    mock: bool = False
    timeout: float = 30.0
    _client: httpx.Client | None = field(default=None, repr=False, compare=False)
    # Instance-local mock write-op state: system_id -> field overrides applied
    # on reads so a mock commission/deploy/release cycle stays observable.
    _mock_overrides: dict[str, dict[str, Any]] = field(
        default_factory=dict, repr=False, compare=False
    )
    # Instance-local mock boot-resources: images recorded by upload_image so
    # a mock upload -> list_images round-trip stays observable.
    _mock_images: list[dict[str, Any]] = field(
        default_factory=list, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        # Mock is explicit opt-in only; an empty URL no longer implies it.
        if self.mock:
            return
        if not (self.url or "").strip():
            # Unconfigured: not connected; reads return empty, writes raise.
            self._base_url = ""
            return
        if not self.api_key:
            raise MaasError("maas_api_key is required when not in mock mode")
        base = _normalize_base_url(self.url)
        consumer_key, token_key, token_secret = _parse_api_key(self.api_key)
        auth = _try_oauth1_auth(consumer_key, token_key, token_secret)
        self._base_url = base
        self._client = httpx.Client(
            base_url=base,
            auth=auth,
            timeout=self.timeout,
            headers={"Accept": "application/json"},
        )

    @property
    def configured(self) -> bool:
        """True when the client can serve machine data (live URL or mock)."""
        return self.mock or self.live

    @property
    def live(self) -> bool:
        """True when talking to a real MAAS (URL set, not mock)."""
        return bool((self.url or "").strip()) and not self.mock

    def _require_configured(self) -> None:
        """Fail write/single-machine ops fast when MAAS is not configured."""
        if not self.configured:
            raise MaasError(
                "MAAS is not configured for this environment "
                "(set maas.url + maas.api_key in config.yaml; "
                "maas.mock: true enables the dev-only mock inventory)",
                status_code=503,
            )

    # ------------------------------------------------------------------
    # Construction helpers
    # ------------------------------------------------------------------

    @classmethod
    def from_settings(
        cls, settings: SupportsMaasSettings | Mapping[str, Any]
    ) -> MaasClient:
        """Build a client from Settings or a mapping with maas_url / maas_api_key.

        ``maas_mock`` (or ``mock``) opts into the mock inventory; without it
        an empty URL yields an unconfigured client, not fake machines.
        """
        url, api_key, mock = cls._extract_config(settings)
        if not url:
            return cls(url="", api_key=api_key or "", mock=mock)
        return cls(url=url, api_key=api_key, mock=mock)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> MaasClient:
        """Prefer console Settings; env mapping only for tests."""
        if env is None:
            try:
                from app.config import get_settings

                return cls.from_settings(get_settings())
            except Exception:  # noqa: BLE001
                return cls(url="", api_key="", mock=False)
        mock_raw = env.get("maas_mock", env.get("MAAS_MOCK", ""))
        return cls.from_settings(
            {
                "maas_url": env.get("maas_url", env.get("MAAS_URL", "")),
                "maas_api_key": env.get("maas_api_key", env.get("MAAS_API_KEY", "")),
                "maas_mock": str(mock_raw).strip().lower()
                in {"1", "true", "yes", "on"},
            }
        )

    @staticmethod
    def _extract_config(
        settings: SupportsMaasSettings | Mapping[str, Any],
    ) -> tuple[str, str, bool]:
        if isinstance(settings, Mapping):
            url = str(settings.get("maas_url") or settings.get("url") or "")
            api_key = str(settings.get("maas_api_key") or settings.get("api_key") or "")
            mock = bool(settings.get("maas_mock") or settings.get("mock") or False)
        else:
            url = str(getattr(settings, "maas_url", "") or "")
            api_key = str(getattr(settings, "maas_api_key", "") or "")
            mock = bool(getattr(settings, "maas_mock", False))
        return url.strip(), api_key.strip(), mock

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def list_machines(self) -> list[dict[str, Any]]:
        """Return all machines known to MAAS.

        Unconfigured (no URL, mock off) returns an empty list — never the
        mock inventory.
        """
        if self.mock:
            return [self._mock_machine(m) for m in _MOCK_MACHINES]
        if not self.live:
            return []
        return self._get_json("machines/")

    def get_machine(self, system_id: str) -> dict[str, Any]:
        """Return a single machine by system_id."""
        if self.mock:
            for m in _MOCK_MACHINES:
                if m["system_id"] == system_id:
                    return self._mock_machine(m)
            raise MaasError(f"Machine not found: {system_id}", status_code=404)
        self._require_configured()
        return self._get_json(f"machines/{system_id}/")

    def commission(
        self, system_id: str, user_data_b64: str | None = None
    ) -> dict[str, Any]:
        """Commission a machine (``op=commission``); returns the machine record."""
        if self.mock:
            return self._mock_transition(system_id, "commission")
        self._require_configured()
        data = {"op": "commission"}
        if user_data_b64:
            data["user_data"] = user_data_b64
        return self._post_json(f"machines/{system_id}/", data=data)

    def deploy(
        self,
        system_id: str,
        user_data_b64: str | None = None,
        hostname: str | None = None,
        image: str | None = None,
    ) -> dict[str, Any]:
        """Deploy a machine (``op=deploy``); returns the machine record.

        ``user_data_b64`` is base64-encoded cloud-init user-data; ``hostname``
        renames the machine on deploy. ``image`` is the name of an uploaded
        custom boot-resource (e.g. a Talos factory image): MAAS deploys those
        with ``osystem=custom`` + ``distro_series=<image name>``.
        """
        if self.mock:
            return self._mock_transition(
                system_id, "deploy", hostname=hostname, image=image
            )
        self._require_configured()
        data = {"op": "deploy"}
        if image:
            # rtype=uploaded custom images deploy as osystem=custom with the
            # image name carried in distro_series.
            data["osystem"] = "custom"
            data["distro_series"] = image
        if user_data_b64:
            data["user_data"] = user_data_b64
        if hostname:
            data["hostname"] = hostname
        return self._post_json(f"machines/{system_id}/", data=data)

    def release(self, system_id: str) -> dict[str, Any]:
        """Release a machine back to the pool (``op=release``)."""
        if self.mock:
            return self._mock_transition(system_id, "release")
        self._require_configured()
        return self._post_json(f"machines/{system_id}/", data={"op": "release"})

    def power_status(self, system_id: str) -> dict[str, Any]:
        """Return power state for a machine.

        Prefer the machine's ``power_state`` field; when talking to a live MAAS,
        also try ``op=query_power_state`` for a fresh reading.
        """
        if self.mock:
            machine = self.get_machine(system_id)
            return {
                "system_id": system_id,
                "state": machine.get("power_state", "unknown"),
                "power_state": machine.get("power_state", "unknown"),
            }

        self._require_configured()

        # Fresh query when supported
        try:
            data = self._get_json(
                f"machines/{system_id}/", params={"op": "query_power_state"}
            )
            if isinstance(data, dict) and ("state" in data or "power_state" in data):
                state = data.get("state") or data.get("power_state")
                return {"system_id": system_id, "state": state, "power_state": state}
        except MaasError as exc:
            logger.debug(
                "query_power_state failed (%s); falling back to machine record", exc
            )

        machine = self.get_machine(system_id)
        state = machine.get("power_state", "unknown")
        return {"system_id": system_id, "state": state, "power_state": state}

    def list_tags(self) -> list[dict[str, Any]]:
        """Return MAAS tags (Genestack roles map to tags such as control-plane)."""
        if self.mock:
            return [dict(t) for t in _MOCK_TAGS]
        if not self.live:
            return []
        return self._get_json("tags/")

    def upload_image(
        self,
        name: str,
        content: bytes | Path,
        title: str | None = None,
        architecture: str = "amd64/generic",
    ) -> dict[str, Any]:
        """Upload a custom boot-resource (e.g. a Talos factory image).

        Flow: a single multipart ``POST boot-resources/`` with the metadata
        fields (``name``, ``title``, ``architecture``, ``filetype=tgz``) and
        the image bytes in the ``content`` form part — the same call the
        ``maas boot-resources create ... content@=<file>`` CLI makes. (MAAS
        also documents a chunked upload via ``op=...`` on the resource; the
        single multipart request is sufficient here and keeps the client
        thin.) ``content`` may be raw bytes or a path to a downloaded image
        file — paths keep a multi-GB image on disk, only the 1 MiB httpx
        read buffer is in memory.
        """
        title = title or name
        is_file = isinstance(content, (str, Path))
        file_path = Path(content) if is_file else None
        content_bytes = None if is_file else content
        if is_file and not file_path.is_file():
            raise MaasError(f"image file not found: {file_path}")
        if self.mock:
            if is_file:
                content_bytes = file_path.read_bytes()
            record = {
                "id": len(self._mock_images) + 1,
                "name": name,
                "title": title,
                "architecture": architecture,
                "type": "uploaded",
                "size": (file_path.stat().st_size if is_file else len(content_bytes)),
                "sha256": hashlib.sha256(content_bytes).hexdigest(),
            }
            self._mock_images.append(record)
            return dict(record)
        self._require_configured()
        data = {
            "name": name,
            "title": title,
            "architecture": architecture,
            "filetype": "tgz",
        }
        if is_file:
            fh = file_path.open("rb")
            try:
                files = {"content": (name, fh, "application/octet-stream")}
                return self._post_json("boot-resources/", data=data, files=files)
            finally:
                fh.close()
        files = {"content": (name, content_bytes, "application/octet-stream")}
        return self._post_json("boot-resources/", data=data, files=files)

    def list_images(self) -> list[dict[str, Any]]:
        """List boot-resources, projected to (name, title, type)."""
        if self.mock:
            resources: list[dict[str, Any]] = list(self._mock_images)
        elif not self.live:
            resources = []
        else:
            resources = self._get_json("boot-resources/")
        return [
            {
                "name": r.get("name"),
                "title": r.get("title"),
                "type": r.get("type"),
            }
            for r in resources
        ]

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def __enter__(self) -> MaasClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _mock_machine(self, machine: dict[str, Any]) -> dict[str, Any]:
        """Copy of a mock machine with this instance's write-op overrides applied."""
        m = dict(machine)
        m.update(self._mock_overrides.get(m["system_id"], {}))
        return m

    def _mock_transition(
        self,
        system_id: str,
        op: str,
        hostname: str | None = None,
        image: str | None = None,
    ) -> dict[str, Any]:
        """Apply a deterministic mock status transition for a write op."""
        machine = self.get_machine(system_id)  # raises MaasError(404) when unknown
        status_name, status_code = _MOCK_OP_STATUS[op]
        override: dict[str, Any] = {"status_name": status_name, "status": status_code}
        if op in ("commission", "deploy"):
            override["power_state"] = "on"
        if hostname:
            override["hostname"] = hostname
            override["fqdn"] = (
                hostname if "." in hostname else f"{hostname}.genestack.local"
            )
        if image:
            # Mock the osystem=custom + distro_series=<image> deploy params.
            override["osystem"] = "custom"
            override["distro_series"] = image
            override["deployed_image"] = image
        self._mock_overrides.setdefault(system_id, {}).update(override)
        machine.update(override)
        return machine

    def _post_json(
        self,
        path: str,
        *,
        data: Mapping[str, str] | None = None,
        files: Mapping[str, Any] | None = None,
    ) -> Any:
        if self._client is None:
            raise MaasError("MAAS client is not connected (unconfigured or closed)")
        try:
            response = self._client.post(
                path,
                data=dict(data) if data else None,
                files=dict(files) if files else None,
            )
        except httpx.HTTPError as exc:
            raise MaasError(f"MAAS request failed: {exc}") from exc

        if response.status_code == 401:
            raise MaasError("MAAS authentication failed", status_code=401)
        if response.status_code == 404:
            raise MaasError(f"MAAS resource not found: {path}", status_code=404)
        if response.status_code >= 400:
            raise MaasError(
                f"MAAS error {response.status_code}: {response.text[:500]}",
                status_code=response.status_code,
            )
        try:
            return response.json()
        except ValueError as exc:
            raise MaasError("MAAS returned non-JSON response") from exc

    def _get_json(
        self,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
    ) -> Any:
        if self._client is None:
            raise MaasError("MAAS client is not connected (unconfigured or closed)")
        try:
            response = self._client.get(path, params=dict(params) if params else None)
        except httpx.HTTPError as exc:
            raise MaasError(f"MAAS request failed: {exc}") from exc

        if response.status_code == 401:
            raise MaasError("MAAS authentication failed", status_code=401)
        if response.status_code == 404:
            raise MaasError(f"MAAS resource not found: {path}", status_code=404)
        if response.status_code >= 400:
            raise MaasError(
                f"MAAS error {response.status_code}: {response.text[:500]}",
                status_code=response.status_code,
            )
        try:
            return response.json()
        except ValueError as exc:
            raise MaasError("MAAS returned non-JSON response") from exc


__all__ = ["MaasClient", "MaasError"]
