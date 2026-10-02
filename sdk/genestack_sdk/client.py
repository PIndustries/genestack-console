"""HTTP client for the console the customer runs.

Tokens stay on the instance. This module does not log them, and it does not
send a client secret. The implicit grant is not implemented.
"""

from __future__ import annotations

from typing import Any

import httpx

_TOKEN_PATH = "/api/v1/oauth/token"
_CLIENT_ID = "genestack-console"
_SCOPE = "console"


class Client:
    """Call ``/api/v1`` on one console.

    ``api_key`` is sent as ``X-API-Key``. ``access_token`` is sent as
    ``Authorization: Bearer``. ``transport`` is optional and is how tests
    avoid a live network.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        access_token: str | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.access_token = access_token
        self.refresh_token: str | None = None
        kwargs: dict[str, Any] = {"timeout": 30.0, "trust_env": False}
        if transport is not None:
            kwargs["transport"] = transport
        self._http = httpx.Client(base_url=self.base_url, **kwargs)

    def __repr__(self) -> str:
        return f"Client(base_url={self.base_url!r})"

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> Client:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        if self.access_token:
            headers["Authorization"] = "Bearer " + self.access_token
        return headers

    def _path(self, path: str) -> str:
        if path.startswith("/"):
            return path
        return "/api/v1/" + path

    def request(
        self,
        method: str,
        path: str,
        json: Any = None,
        data: Any = None,
    ) -> httpx.Response:
        """Send one call. A path without a leading ``/`` is under ``/api/v1``."""
        kwargs: dict[str, Any] = {"headers": self._headers()}
        if json is not None:
            kwargs["json"] = json
        if data is not None:
            kwargs["data"] = data
        return self._http.request(method, self._path(path), **kwargs)

    def _store_tokens(self, payload: dict[str, Any]) -> None:
        access = payload.get("access_token")
        refresh = payload.get("refresh_token")
        if not isinstance(access, str) or not access:
            raise RuntimeError("token response missing access_token")
        if not isinstance(refresh, str) or not refresh:
            raise RuntimeError("token response missing refresh_token")
        self.access_token = access
        self.refresh_token = refresh

    def _form_token(self, form: dict[str, str]) -> dict[str, Any]:
        response = self.request("POST", _TOKEN_PATH, data=form)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("token response was not a JSON object")
        self._store_tokens(payload)
        return payload

    def login(self, username: str, password: str) -> dict[str, Any]:
        """Password grant. Stores the access token and the refresh token."""
        return self._form_token(
            {
                "grant_type": "password",
                "username": username,
                "password": password,
                "client_id": _CLIENT_ID,
                "scope": _SCOPE,
            }
        )

    def refresh(self) -> dict[str, Any]:
        """Refresh-token grant. Replaces both stored tokens."""
        if not self.refresh_token:
            raise RuntimeError("no refresh token")
        return self._form_token(
            {
                "grant_type": "refresh_token",
                "refresh_token": self.refresh_token,
                "client_id": _CLIENT_ID,
                "scope": _SCOPE,
            }
        )
