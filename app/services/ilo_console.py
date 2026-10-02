"""iLO HTML5 remote console (IRC) sessions for the console UI.

The operator browser cannot reach BMC addresses on the hardware network.
The Console host can. Sessions are in-memory (like noVNC): an unguessable
``ilo_`` id bound to an environment, holding the iLO JSON ``session_key`` so
the BMC password never leaves the server.

iLO 4 HTML5 IRC (``/irc.html``) builds its KVM socket as::

    wss:// + options.host + /wss/ircport

``options.host`` is ``window.location.host``. When we serve ``irc.html`` from
the Console we rewrite ``js/socket.js`` so the socket stays on this origin
and under the session path, and so HTTP (not only HTTPS) pages use ``ws://``.
"""

from __future__ import annotations

import re
import secrets
import threading
import time
from dataclasses import dataclass
from urllib.parse import urljoin

import httpx

from app.config import Settings
from app.models import BaremetalNode
from app.services.crypto import decrypt_secret

SESSION_TTL_SECONDS = 15 * 60
SESSION_ID_RE = re.compile(r"^ilo_[A-Za-z0-9_-]{8,80}$")
NODE_ID_RE = re.compile(r"^[A-Za-z0-9-]{8,64}$")

SOCKADDR_OLD = 'this.sockaddr = "wss://" + options.host + "/wss/ircport"'
# iLO importScripts() socket.js into a Worker. Workers have self.location
# (the worker script URL) and no window. Strip a trailing /js/ so the KVM
# socket stays on the session prefix, not .../js/wss/ircport.
SOCKADDR_NEW = (
    "this.sockaddr = (function(){var loc=self.location;"
    'var proto=loc.protocol==="https:"?"wss://":"ws://";'
    'var dir=loc.pathname.replace(/\\/[^/]*$/,"/").replace(/\\/js\\/$,"/");'
    'return proto+loc.host+dir+"wss/ircport"})()'
)
# renderer.js: in an iframe, iLO sets path="../" so Worker("js/worker_decoder.js")
# resolves outside the console session prefix and 404s — black KVM canvas.
IFRAME_REL_OLD = 'window.top === window.self ? "" : "../"'
IFRAME_REL_NEW = '""'
# icons.js (OEM extendedIcons): same iframe trap, different literal.
ICONS_IFRAME_OLD = (
    'window.top === window.self ? "js/extendedIcons.js" : "../js/extendedIcons.js"'
)
ICONS_IFRAME_NEW = '"js/extendedIcons.js"'
# irc.js startHtml5Irc: `me.renderer = new Renderer(...), renderer.onready = …`
# assumes an implicit global. When `this` is not window (or the lookup fails
# in the hosted iframe), the comma expression throws ReferenceError and the
# browser aborts before sending the KVM client hello — hub logs
# "client disconnected" and the canvas stays Loading/black. BMC handshake
# itself is fine (AUTH_OK + VGA) when the hello is sent.
RENDERER_GLOBAL_OLD = "me.renderer = new Renderer(settings), renderer."
RENDERER_GLOBAL_NEW = (
    "window.renderer = me.renderer = new Renderer(settings), renderer."
)
# iLO.js sendJsonRequest forces a leading slash on relative json/rest URLs,
# which would escape the Console session prefix. Keep match("/json/…$")
# literals intact — those are suffix checks, not request URLs.
JSON_PREFIXER_OLD = '"json/" == my_url.match("^json/") && (my_url = "/" + my_url)'
REST_PREFIXER_OLD = '"rest/" == my_url.match("^rest/") && (my_url = "/" + my_url)'
# getCache builds absolute "/json/" + name — never hits the relative prefixer.
ABS_JSON_REQURL_OLD = 'reqUrl: "/json/" + req.name'
ABS_REST_REQURL_OLD = 'reqUrl: "/rest/" + req.name'
_FORWARD_HEADERS = frozenset(
    {"content-type", "x-auth-token", "x-client-type", "accept"}
)

_ASSET_RE = re.compile(
    r"^(?:[A-Za-z0-9._-]+/)*[A-Za-z0-9._-]+(?:\.(?:html|js|css|map|png|svg|ico|gif|woff2?|ttf|json))?$"
)

UNAVAILABLE_HTML = (
    '<!DOCTYPE html><html><head><meta charset="utf-8">'
    "<title>iLO console unavailable</title></head><body>"
    "<p>Console proxy could not reach the BMC iLO.</p>"
    "</body></html>"
)


class IloConsoleError(RuntimeError):
    """iLO JSON login or HTTP fetch failed."""


@dataclass(frozen=True)
class IloSession:
    env_id: str
    node_id: str
    node_name: str
    bmc_host: str
    session_key: str
    created: float
    username: str


_lock = threading.Lock()
_store: dict[str, IloSession] = {}


def create_session(
    *,
    env_id: str,
    node_id: str,
    node_name: str,
    bmc_host: str,
    session_key: str,
    username: str,
) -> str:
    session_id = "ilo_" + secrets.token_urlsafe(24)
    entry = IloSession(
        env_id=env_id,
        node_id=node_id,
        node_name=node_name,
        bmc_host=bmc_host,
        session_key=session_key,
        created=time.time(),
        username=username,
    )
    with _lock:
        _purge_expired()
        _store[session_id] = entry
    return session_id


def get_session(session_id: str, env_id: str | None = None) -> IloSession | None:
    session_id = (session_id or "").strip()
    if not SESSION_ID_RE.match(session_id):
        return None
    with _lock:
        _purge_expired()
        entry = _store.get(session_id)
        if entry is None:
            return None
        if time.time() >= entry.created + SESSION_TTL_SECONDS:
            _store.pop(session_id, None)
            return None
        if env_id is not None and entry.env_id != env_id:
            return None
        return entry


def _purge_expired() -> None:
    now = time.time()
    expired = [
        sid
        for sid, entry in _store.items()
        if now >= entry.created + SESSION_TTL_SECONDS
    ]
    for sid in expired:
        _store.pop(sid, None)


def _reset() -> None:
    """Tests: drop all outstanding iLO console sessions."""
    with _lock:
        _store.clear()


def bmc_origin(host: str) -> str:
    host = (host or "").strip().rstrip("/")
    if host.startswith(("http://", "https://")):
        return host
    return f"https://{host}"


def json_login(host: str, username: str, password: str) -> str:
    """iLO 4 JSON login; returns session_key. Never logs the password."""
    origin = bmc_origin(host)
    try:
        with httpx.Client(verify=False, timeout=15.0, follow_redirects=True) as client:
            response = client.post(
                f"{origin}/json/login_session",
                json={
                    "method": "login",
                    "user_login": username,
                    "password": password,
                },
                headers={"Content-Type": "application/json"},
            )
    except httpx.HTTPError as exc:
        raise IloConsoleError(f"iLO login failed: {exc}") from exc
    if response.status_code >= 400:
        raise IloConsoleError(f"iLO login HTTP {response.status_code}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise IloConsoleError("iLO login returned non-JSON") from exc
    if not isinstance(payload, dict):
        raise IloConsoleError("iLO login returned an unexpected body")
    key = payload.get("session_key")
    if not key:
        raise IloConsoleError("iLO login did not return a session_key")
    return str(key)


def login_node(node: BaremetalNode, settings: Settings) -> str:
    password = decrypt_secret(node.bmc_password, settings) or ""
    return json_login(node.bmc_host, node.bmc_username or "", password)


def is_safe_console_path(path: str) -> bool:
    text = str(path or "").strip()
    if not text:
        return True
    if text.startswith(("/", "\\")) or "\\" in text or ".." in text.split("/"):
        return False
    if any(c in text for c in "?:#%@"):
        return False
    if text in ("irc.html", "favicon.ico"):
        return True
    if re.match(r"^(?:json|rest|blob)/[A-Za-z0-9._$-/]+$", text):
        return True
    return bool(_ASSET_RE.match(text))


def rewrite_socket_js(text: str) -> str:
    """Keep the KVM WebSocket on the Console origin and session path."""
    if SOCKADDR_OLD in text:
        return text.replace(SOCKADDR_OLD, SOCKADDR_NEW)
    return re.sub(
        r'this\.sockaddr\s*=\s*"wss://"\s*\+\s*options\.host\s*\+\s*"/wss/ircport"',
        SOCKADDR_NEW,
        text,
    )


def rewrite_iframe_relative_roots(text: str) -> str:
    """Keep Worker/asset roots on the session path when IRC is iframed."""
    out = text
    if IFRAME_REL_OLD in out:
        out = out.replace(IFRAME_REL_OLD, IFRAME_REL_NEW)
    if ICONS_IFRAME_OLD in out:
        out = out.replace(ICONS_IFRAME_OLD, ICONS_IFRAME_NEW)
    return out


def rewrite_irc_renderer_global(text: str) -> str:
    """Bind window.renderer so startHtml5Irc event hooks do not ReferenceError."""
    if RENDERER_GLOBAL_OLD in text:
        return text.replace(RENDERER_GLOBAL_OLD, RENDERER_GLOBAL_NEW)
    return text


def rewrite_json_roots(text: str, prefix: str) -> str:
    """Keep iLO json/rest fetches under the Console session prefix.

    Do not rewrite ``.match("/json/…$")`` suffix checks — those must stay
    origin-absolute so they still match the prefixed URL (path ends with
    ``/json/…`` after the session prefix is applied).
    """
    prefix = prefix.rstrip("/")
    out = text.replace(
        JSON_PREFIXER_OLD,
        f'"json/" == my_url.match("^json/") && (my_url = "{prefix}/" + my_url)',
    )
    out = out.replace(
        REST_PREFIXER_OLD,
        f'"rest/" == my_url.match("^rest/") && (my_url = "{prefix}/" + my_url)',
    )
    # getCache uses absolute "/json/" + name (never matches ^json/).
    out = out.replace(
        ABS_JSON_REQURL_OLD,
        f'reqUrl: "{prefix}/json/" + req.name',
    )
    out = out.replace(
        ABS_REST_REQURL_OLD,
        f'reqUrl: "{prefix}/rest/" + req.name',
    )
    out = out.replace('href="/favicon.ico', f'href="{prefix}/favicon.ico')
    out = out.replace("href='/favicon.ico", f"href='{prefix}/favicon.ico")
    return out


def fetch_ilo_asset(
    origin: str,
    path: str,
    session_key: str,
    *,
    method: str = "GET",
    body: bytes | None = None,
    extra_headers: dict[str, str] | None = None,
) -> tuple[int, bytes, str]:
    """Proxy an iLO static/JSON/REST call; returns status, body, content-type."""
    rel = path.lstrip("/") or "irc.html"
    url = urljoin(origin.rstrip("/") + "/", rel)
    headers = {"Cookie": f"sessionKey={session_key}"}
    if extra_headers:
        for key, value in extra_headers.items():
            if key.lower() in _FORWARD_HEADERS and value:
                headers[key] = value
    method = (method or "GET").upper()
    try:
        with httpx.Client(verify=False, timeout=20.0, follow_redirects=True) as client:
            response = client.request(
                method,
                url,
                headers=headers,
                content=body if method not in ("GET", "HEAD") else None,
            )
    except httpx.HTTPError as exc:
        raise IloConsoleError(f"iLO fetch failed: {exc}") from exc
    ctype = response.headers.get("content-type") or "application/octet-stream"
    return response.status_code, response.content, ctype.split(";")[0].strip()


def session_prefix(env_id: str, session_id: str) -> str:
    return f"/api/v1/environments/{env_id}/baremetal/console/{session_id}"


def apply_rewrites(path: str, body: bytes, prefix: str) -> bytes:
    name = path.rsplit("/", 1)[-1] if path else "irc.html"
    if name.endswith(".js") or name in ("irc.html", ""):
        text = body.decode("utf-8", errors="replace")
        if name == "socket.js" or "sockaddr" in text:
            text = rewrite_socket_js(text)
        if (
            IFRAME_REL_OLD in text
            or ICONS_IFRAME_OLD in text
            or "worker_decoder" in text
            or "extendedIcons" in text
        ):
            text = rewrite_iframe_relative_roots(text)
        if RENDERER_GLOBAL_OLD in text or "startHtml5Irc" in text:
            text = rewrite_irc_renderer_global(text)
        if (
            "my_url" in text
            or "/json/" in text
            or "favicon.ico" in text
            or ABS_JSON_REQURL_OLD in text
            or ABS_REST_REQURL_OLD in text
        ):
            text = rewrite_json_roots(text, prefix)
        return text.encode("utf-8")
    return body
