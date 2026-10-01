"""In-portal noVNC sessions and kube-apiserver proxy helpers.

Nova's noVNC URL (novnc.cluster.local / VLAN 100) is not reachable from the
operator browser or the console host. HTTP assets and the websockify tunnel
are fetched through the same kube-apiserver service proxy used for Keystone
and Nova:

    {apiserver}/api/v1/namespaces/openstack/services/http:nova-novncproxy:6080/proxy/{path}

Sessions are in-memory (like tickets.py): an unguessable ``nvc_`` id bound to
an environment, holding the Nova console token so the browser never sees it.
"""

from __future__ import annotations

import base64
import hashlib
import re
import secrets
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import yaml

from app.services.osclient import OPENSTACK_NS, load_kube_http

SESSION_TTL_SECONDS = 15 * 60
SESSION_ID_RE = re.compile(r"^nvc_[A-Za-z0-9_-]{8,80}$")
SERVER_ID_RE = re.compile(r"^[A-Za-z0-9-]{8,64}$")
HTML_NAMES = frozenset({"", "vnc_lite.html", "vnc_auto.html"})

NOVNC_SERVICE = "nova-novncproxy"
NOVNC_PORT = 6080
# kubectl port-forward local bind. kube-apiserver's service-proxy WebSocket
# to nova-novncproxy drops immediately; HTTP assets still use the proxy.
NOVNC_LOCAL_PORT = 16080
PORTFORWARD_WAIT = 8.0

UNAVAILABLE_HTML = (
    '<!DOCTYPE html><html><head><meta charset="utf-8">'
    "<title>Console unavailable</title></head><body>"
    "<p>Console proxy could not reach nova-novncproxy.</p>"
    "</body></html>"
)

_TOKEN_IN_URL_RE = re.compile(r"[?&]token=([^&]+)")
_TOKEN_QUERY_RE = re.compile(r"([?&])token=[^&\"'\s#]*")


class NovncProxyError(RuntimeError):
    """Kube / nova-novncproxy HTTP or TLS setup failed."""


@dataclass(frozen=True)
class NovncSession:
    env_id: str
    server_id: str
    nova_token: str
    created: float
    username: str


_lock = threading.Lock()
_store: dict[str, NovncSession] = {}


def create_session(
    *,
    env_id: str,
    server_id: str,
    nova_token: str,
    username: str,
) -> str:
    """Mint a console session; returns the unguessable session id."""
    session_id = "nvc_" + secrets.token_urlsafe(24)
    entry = NovncSession(
        env_id=env_id,
        server_id=server_id,
        nova_token=nova_token,
        created=time.time(),
        username=username,
    )
    with _lock:
        _purge_expired()
        _store[session_id] = entry
    return session_id


def get_session(session_id: str, env_id: str | None = None) -> NovncSession | None:
    """Return a live session bound to ``env_id``, or None if missing/expired."""
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
    """Tests: drop all outstanding console sessions."""
    with _lock:
        _store.clear()
    _stop_portforward()


def parse_nova_token(url: str) -> str | None:
    """Extract the Nova console token from a noVNC URL.

    Nova typically returns either ``?token=`` on the page URL or a nested
    ``path=/websockify?token=`` (often URL-encoded as ``path=%2Fwebsockify%3Ftoken%3D…``).
    """
    raw = (url or "").strip()
    if not raw:
        return None
    parsed = urlparse(raw)
    qs = parse_qs(parsed.query, keep_blank_values=False)
    direct = (qs.get("token") or [None])[0]
    if direct:
        return unquote(direct)
    for path_val in qs.get("path") or []:
        inner = unquote(path_val)
        inner_parsed = urlparse(
            inner if "://" in inner else f"stub://x/{inner.lstrip('/')}"
        )
        nested = (parse_qs(inner_parsed.query).get("token") or [None])[0]
        if nested:
            return unquote(nested)
        if "token=" in inner:
            nested_qs = parse_qs(inner.split("?", 1)[-1] if "?" in inner else inner)
            nested = (nested_qs.get("token") or [None])[0]
            if nested:
                return unquote(nested)
    decoded = unquote(unquote(raw))
    match = _TOKEN_IN_URL_RE.search(decoded)
    if match:
        return unquote(match.group(1))
    return None


_ASSET_RE = re.compile(
    r"^(?:[A-Za-z0-9._-]+/)*[A-Za-z0-9._-]+\.(?:html|js|css|map|png|svg|ico|woff2?|ttf|json)$"
)


def is_safe_console_path(path: str) -> bool:
    """Allow only relative noVNC static assets. Reject traversal, URLs, queries."""
    text = str(path or "").strip()
    if not text:
        return True
    if text.startswith(("/", "\\")) or "\\" in text or ".." in text.split("/"):
        return False
    if any(c in text for c in "?:#%@"):
        return False
    return bool(_ASSET_RE.match(text))


def is_html_path(path: str) -> bool:
    return path in HTML_NAMES


def rewrite_novnc_html(html: str) -> str:
    """Force a relative websockify path and strip any Nova token from the page.

    ``/websockify`` would escape our ``/cloud/console/{session}/`` prefix;
    the iframe must open ``websockify`` as a relative URL. The Nova token
    stays server-side — never in the browser URL or HTML.
    """
    text = html
    text = re.sub(r"path=%2[Ff]websockify", "path=websockify", text)
    text = text.replace("path=/websockify", "path=websockify")
    text = text.replace("/websockify", "websockify")
    text = re.sub(r"%3[Ff]token%3[Dd][^&\"'\s#]*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\?token=[^&\"'\s#]*&?", "?", text)
    text = re.sub(r"&token=[^&\"'\s#]*", "", text)
    text = _TOKEN_QUERY_RE.sub(lambda m: "?" if m.group(1) == "?" else "", text)
    text = text.replace("?&", "?")
    text = re.sub(r"\?(?=[\"'\s<]|$)", "", text)
    return text


def novnc_proxy_url(apiserver: str, path: str) -> str:
    """Kube service-proxy URL for a nova-novncproxy path."""
    rel = path.lstrip("/")
    return (
        f"{apiserver.rstrip('/')}/api/v1/namespaces/{OPENSTACK_NS}"
        f"/services/http:{NOVNC_SERVICE}:{NOVNC_PORT}/proxy/{rel}"
    )


def fetch_novnc_asset(kubeconfig_path: str, path: str) -> tuple[int, bytes, str]:
    """GET an asset from nova-novncproxy via the kube API proxy.

    Returns ``(status_code, body, content_type)``. Raises ``NovncProxyError``
    when the kube client cannot be built or the request fails to complete.
    """
    try:
        client, apiserver, cleanup = load_kube_http(kubeconfig_path)
    except Exception as exc:  # noqa: BLE001
        raise NovncProxyError("failed to open kube client") from exc
    try:
        url = novnc_proxy_url(apiserver, path)
        response = client.get(url)
        ctype = response.headers.get("content-type") or "application/octet-stream"
        return response.status_code, bytes(response.content), ctype
    except NovncProxyError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise NovncProxyError("nova-novncproxy request failed") from exc
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass
        _cleanup_files(cleanup)


def _b64file(data: str, suffix: str) -> str:
    raw = base64.b64decode(data)
    fd, name = tempfile.mkstemp(prefix="gsc-nvc-", suffix=suffix)
    with open(fd, "wb") as fh:
        fh.write(raw)
    Path(name).chmod(0o600)
    return name


def _cleanup_files(paths: list[str]) -> None:
    for item in paths:
        try:
            Path(item).unlink(missing_ok=True)
        except OSError:
            pass


def _kube_identity(
    kubeconfig_path: str,
) -> tuple[str, ssl.SSLContext, dict[str, str], list[str]]:
    """Parse kubeconfig into (apiserver, ssl_context, headers, cleanup_paths).

    Client certs go on the SSLContext via ``load_cert_chain`` — never as an
    httpx ``verify=(ca, cert)`` tuple.
    """
    path = Path(kubeconfig_path)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise NovncProxyError("invalid kubeconfig")
    clusters = data.get("clusters") or []
    users = data.get("users") or []
    if not clusters or not users:
        raise NovncProxyError("kubeconfig missing cluster/user")
    cluster = (clusters[0] or {}).get("cluster") or {}
    user = (users[0] or {}).get("user") or {}
    server = str(cluster.get("server") or "").rstrip("/")
    if not server:
        raise NovncProxyError("kubeconfig has no server")
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
    return server, ctx, headers, cleanup


def _kube_ws_target(
    kubeconfig: str,
) -> tuple[str, ssl.SSLContext, dict[str, str], list[str]]:
    """Return ``(wss_websockify_url, ssl_context, headers, cleanup)``.

    The URL does **not** include the Nova token; the caller appends it.
    """
    apiserver, ssl_ctx, headers, cleanup = _kube_identity(kubeconfig)
    parsed = urlparse(apiserver)
    scheme = "wss" if parsed.scheme != "http" else "ws"
    prefix = (parsed.path or "").rstrip("/")
    wss_url = (
        f"{scheme}://{parsed.netloc}{prefix}/api/v1/namespaces/{OPENSTACK_NS}"
        f"/services/http:{NOVNC_SERVICE}:{NOVNC_PORT}/proxy/websockify"
    )
    return wss_url, ssl_ctx, headers, cleanup


def redact_token(text: str, token: str) -> str:
    """Replace a Nova token in a string so it is never logged."""
    if not token or not text:
        return text
    return text.replace(token, "<redacted>")


_pf_lock = threading.Lock()
_pf_proc: subprocess.Popen[bytes] | None = None
_pf_kubeconfig: str | None = None
_pf_digest: str | None = None
_pf_port: int | None = None


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.4):
            return True
    except OSError:
        return False


def _kubeconfig_digest(kubeconfig: str) -> str:
    return hashlib.sha256(Path(kubeconfig).read_bytes()).hexdigest()


def _copy_kubeconfig(kubeconfig: str, digest: str) -> str:
    """Stable kubeconfig path so a staged temp file is not unlinked under kubectl."""
    global _pf_kubeconfig
    src = Path(kubeconfig)
    dest = Path(tempfile.gettempdir()) / f"gsc-novnc-kubeconfig-{digest[:16]}"
    dest.write_bytes(src.read_bytes())
    dest.chmod(0o600)
    _pf_kubeconfig = str(dest)
    return str(dest)


def ensure_novnc_portforward(kubeconfig: str, *, port: int = NOVNC_LOCAL_PORT) -> int:
    """Keep a kubectl port-forward to nova-novncproxy on loopback.

    Reuses a live forward only when it is still ours and matches this
    kubeconfig. Raises NovncProxyError if kubectl is missing or the forward
    does not come up.
    """
    global _pf_proc, _pf_digest, _pf_port
    digest = _kubeconfig_digest(kubeconfig)
    kubectl = shutil.which("kubectl")
    if not kubectl:
        raise NovncProxyError("kubectl not found on PATH")
    stable = _copy_kubeconfig(kubeconfig, digest)
    with _pf_lock:
        if (
            _pf_proc is not None
            and _pf_proc.poll() is None
            and _pf_digest == digest
            and _pf_port == port
            and _port_open(port)
        ):
            return port
        if _pf_proc is not None and _pf_proc.poll() is None:
            _pf_proc.terminate()
            _pf_proc = None
        cmd = [
            kubectl,
            "--kubeconfig",
            stable,
            "-n",
            OPENSTACK_NS,
            "port-forward",
            f"svc/{NOVNC_SERVICE}",
            f"{port}:{NOVNC_PORT}",
        ]
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            raise NovncProxyError(
                "failed to start nova-novncproxy port-forward"
            ) from exc
        _pf_proc = proc
        deadline = time.monotonic() + PORTFORWARD_WAIT
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                _pf_proc = None
                _pf_digest = None
                _pf_port = None
                raise NovncProxyError("nova-novncproxy port-forward exited")
            if _port_open(port):
                _pf_digest = digest
                _pf_port = port
                return port
            time.sleep(0.2)
        raise NovncProxyError("nova-novncproxy port-forward did not become ready")


def local_websockify_url(port: int, token: str) -> str:
    return f"ws://127.0.0.1:{int(port)}/websockify?token={token}"


def _stop_portforward() -> None:
    """Tests: drop the managed port-forward subprocess."""
    global _pf_proc, _pf_digest, _pf_port
    with _pf_lock:
        if _pf_proc is not None and _pf_proc.poll() is None:
            _pf_proc.terminate()
        _pf_proc = None
        _pf_digest = None
        _pf_port = None


async def connect_novnc_upstream(
    url: str,
    ssl_ctx: ssl.SSLContext | None,
    headers: dict[str, str],
    subprotocol: str | None = None,
) -> Any:
    """Open the upstream websockify WebSocket (local port-forward or kube proxy)."""
    import websockets

    kwargs: dict[str, Any] = {
        "ssl": ssl_ctx if url.startswith("wss://") else None,
        "max_size": None,
        "open_timeout": 15,
        "proxy": None,
    }
    if subprotocol:
        kwargs["subprotocols"] = [subprotocol]
    try:
        return await websockets.connect(
            url, additional_headers=headers or None, **kwargs
        )
    except TypeError:
        kwargs.pop("proxy", None)
        return await websockets.connect(url, extra_headers=headers or None, **kwargs)
