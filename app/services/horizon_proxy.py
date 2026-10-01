"""In-portal Horizon proxy: session mint, auto-login, HTML/cookie rewrite.

The operator browser cannot reach horizon.<domain> on the cluster fabric.
Console logs into Horizon with the Keystone admin secret (never sent to the
browser), then proxies HTTP under::

    /api/v1/environments/{id}/cloud/horizon/{session_id}/…

Sessions are in-memory (like noVNC): an unguessable ``hz_`` id. Iframe
requests cannot send X-API-Key, so the session id is the capability.
"""

from __future__ import annotations

import base64
import hashlib
import re
import secrets
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from app.services.osclient import OPENSTACK_NS, load_kube_http

SESSION_TTL_SECONDS = 30 * 60
SESSION_ID_RE = re.compile(r"^hz_[A-Za-z0-9_-]{8,80}$")
HORIZON_CANDIDATES = (("horizon-int", 80), ("horizon", 80))
# Alias kept so open_session cannot NameError if one name is used.
HORIZON_SERVICES = HORIZON_CANDIDATES
LOGIN_PATH = "/auth/login/"
HTTP_TIMEOUT = 25.0
# kubectl port-forward local bind. kube-apiserver's service proxy hits Django
# ALLOWED_HOSTS (Host is the apiserver), which 500s the login page. Loopback
# plus Host: horizon-int matches in-cluster probes.
HORIZON_LOCAL_PORT = 16081
PORTFORWARD_WAIT = 8.0

CSRF_RE = re.compile(
    r'name=["\']csrfmiddlewaretoken["\'][^>]*value=["\']([^"\']+)|'
    r'value=["\']([^"\']+)["\'][^>]*name=["\']csrfmiddlewaretoken["\']',
    re.I,
)
REGION_RE = re.compile(
    r'name=["\']region["\'][^>]*value=["\']([^"\']+)|'
    r'<option[^>]+selected[^>]*value=["\']([^"\']+)["\']',
    re.I,
)
DOMAIN_SELECT_RE = re.compile(
    r'<select[^>]*name=["\']domain["\'][^>]*>(.*?)</select>',
    re.I | re.S,
)
DOMAIN_SELECTED_RE = re.compile(
    r'<option[^>]*selected[^>]*value=["\']([^"\']*)["\']|'
    r'<option[^>]*value=["\']([^"\']*)["\'][^>]*selected',
    re.I,
)
DOMAIN_OPTION_RE = re.compile(r'<option[^>]*value=["\']([^"\']*)["\']', re.I)

_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "host",
        "content-length",
        "cookie",
        "authorization",
    }
)
_STRIP_RESP = frozenset(
    {
        "content-security-policy",
        "content-security-policy-report-only",
        "x-frame-options",
        "x-xss-protection",
        "strict-transport-security",
        "content-encoding",
        "transfer-encoding",
        "content-length",
    }
)
# Horizon USE_SSL + CSRF_COOKIE_SECURE: do not tell Django this is HTTPS or
# CSRF Referer checks fail against the Console origin.
_STRIP_REQ = frozenset({"x-forwarded-proto", "x-forwarded-host", "x-forwarded-for", "origin"})


class HorizonProxyError(RuntimeError):
    """Could not reach Horizon or complete auto-login."""


@dataclass
class HorizonSession:
    env_id: str
    actor: str
    created: float
    cookies: dict[str, str] = field(default_factory=dict)
    service: str = "horizon-int"
    port: int = 80


_lock = threading.Lock()
_store: dict[str, HorizonSession] = {}


def create_session(*, env_id: str, actor: str) -> str:
    session_id = "hz_" + secrets.token_urlsafe(24)
    entry = HorizonSession(env_id=env_id, actor=actor, created=time.time())
    with _lock:
        _purge_expired()
        _store[session_id] = entry
    return session_id


def get_session(session_id: str, env_id: str | None = None) -> HorizonSession | None:
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
        entry.created = time.time()
        return entry


def drop_session(session_id: str) -> None:
    with _lock:
        _store.pop(session_id, None)


def _purge_expired() -> None:
    now = time.time()
    expired = [sid for sid, entry in _store.items() if now >= entry.created + SESSION_TTL_SECONDS]
    for sid in expired:
        _store.pop(sid, None)


def _reset() -> None:
    with _lock:
        _store.clear()
    _stop_portforward()


def is_safe_path(path: str) -> bool:
    text = str(path or "").replace("\\", "/")
    if text.startswith("//") or "://" in text:
        return False
    if text.startswith("/"):
        text = text[1:]
    if ".." in text.split("/"):
        return False
    return True


def proxy_prefix(env_id: str, session_id: str) -> str:
    return f"/api/v1/environments/{env_id}/cloud/horizon/{session_id}"


def horizon_upstream_url(apiserver: str, service: str, port: int, path: str) -> str:
    rel = path if path.startswith("/") else f"/{path}"
    return (
        f"{apiserver.rstrip('/')}/api/v1/namespaces/{OPENSTACK_NS}"
        f"/services/http:{service}:{port}/proxy{rel}"
    )


def _csrf_from_html(html: str) -> str:
    match = CSRF_RE.search(html or "")
    if not match:
        return ""
    return match.group(1) or match.group(2) or ""


def _region_from_html(html: str) -> str:
    match = REGION_RE.search(html or "")
    if not match:
        return ""
    return match.group(1) or match.group(2) or ""


def _domain_from_html(html: str) -> str:
    """Horizon's domain <select> uses Keystone names (often ``Default``)."""
    block = DOMAIN_SELECT_RE.search(html or "")
    if not block:
        return "Default"
    body = block.group(1)
    selected = DOMAIN_SELECTED_RE.search(body)
    if selected:
        return selected.group(1) or selected.group(2) or "Default"
    options = [opt for opt in DOMAIN_OPTION_RE.findall(body) if opt]
    for opt in options:
        if opt.lower() == "default":
            return opt
    return options[0] if options else "Default"


def _merge_cookies(store: dict[str, str], response: httpx.Response) -> None:
    for name, value in response.cookies.items():
        store[str(name)] = str(value)
    getter = getattr(response.headers, "get_list", None)
    raw_list: list[str] = list(getter("set-cookie")) if callable(getter) else []
    if not raw_list:
        one = response.headers.get("set-cookie")
        if one:
            raw_list = [one]
    for raw in raw_list:
        pair = raw.split(";", 1)[0]
        if "=" not in pair:
            continue
        name, _, val = pair.partition("=")
        name = name.strip()
        if name:
            store[name] = val.strip()


def _cookie_header(store: dict[str, str]) -> str:
    return "; ".join(f"{k}={v}" for k, v in store.items() if k and v is not None)


def rewrite_set_cookie(header: str, prefix: str, *, secure: bool) -> str | None:
    """Rebind Horizon cookies onto the Console proxy path. Drop Domain."""
    raw = (header or "").strip()
    if not raw or "=" not in raw:
        return None
    parts = [p.strip() for p in raw.split(";")]
    out = [parts[0]]
    path = prefix.rstrip("/") or "/"
    seen_path = False
    for item in parts[1:]:
        key, _, val = item.partition("=")
        name = key.strip().lower()
        if name == "domain":
            continue
        if name == "path":
            out.append(f"Path={path}")
            seen_path = True
            continue
        if name == "secure":
            if secure:
                out.append("Secure")
            continue
        out.append(item)
    if not seen_path:
        out.append(f"Path={path}")
    if secure and not any(p.lower() == "secure" for p in out[1:]):
        out.append("Secure")
    return "; ".join(out)


def rewrite_location(location: str, prefix: str, public_hosts: tuple[str, ...] = ()) -> str:
    loc = (location or "").strip()
    if not loc:
        return loc
    prefix = prefix.rstrip("/")
    parsed = urlparse(loc)
    if parsed.scheme and parsed.netloc:
        host = parsed.netloc.split("@")[-1].split(":")[0].lower()
        if public_hosts and host not in {h.lower() for h in public_hosts} and "horizon" not in host:
            return loc
        path = parsed.path or "/"
        return prefix + path + (("?" + parsed.query) if parsed.query else "")
    if loc.startswith("//"):
        return rewrite_location("https:" + loc, prefix, public_hosts)
    if loc.startswith("/"):
        if loc.startswith(prefix + "/") or loc == prefix:
            return loc
        return prefix + loc
    return loc


def rewrite_html(html: str, prefix: str, public_hosts: tuple[str, ...] = ()) -> str:
    """Point Horizon's absolute URLs at the Console proxy prefix."""
    prefix = prefix.rstrip("/")
    hosts = [re.escape(h) for h in public_hosts if h]
    host_re = r"(?:horizon[^/\"'\s]*|" + "|".join(hosts) + r")" if hosts else r"horizon[^/\"'\s]*"

    def abs_to_prefix(match: re.Match[str]) -> str:
        path = match.group(1) or "/"
        return prefix + path

    text = html
    text = re.sub(
        rf"https?://{host_re}(/[^\"'\s]*)?",
        lambda m: prefix + (m.group(1) or "/"),
        text,
        flags=re.I,
    )
    text = re.sub(
        r"""(\b(?:href|src|action|data-url)=["'])/(?!/)""",
        rf"\1{prefix}/",
        text,
        flags=re.I,
    )
    text = re.sub(
        r"""(\b(?:url)\(["']?)/(?!/)""",
        rf"\1{prefix}/",
        text,
        flags=re.I,
    )
    # Horizon frame-bust when DISALLOW_IFRAME_EMBED is true.
    text = re.sub(
        r"if\s*\(\s*(?:self|window)\s*!==?\s*(?:top|parent)\s*\)\s*\{[^}]*\}",
        "/* iframe ok via console proxy */",
        text,
    )
    text = re.sub(
        r"if\s*\(\s*(?:window\.)?top\s*!==?\s*(?:window\.)?self\s*\)\s*\{[^}]*\}",
        "/* iframe ok via console proxy */",
        text,
    )
    text = re.sub(
        r"(?:window\.)?top\.location\s*=\s*(?:window\.)?self\.location\s*;?",
        "/* iframe ok */",
        text,
    )
    return text


def _admin_password(kube: httpx.Client, apiserver: str) -> str:
    resp = kube.get(f"{apiserver}/api/v1/namespaces/{OPENSTACK_NS}/secrets/keystone-admin")
    if resp.status_code != 200:
        raise HorizonProxyError("keystone-admin secret unavailable")
    data = (resp.json() or {}).get("data") or {}
    raw = data.get("password")
    if not raw:
        raise HorizonProxyError("keystone-admin secret has no password")
    return base64.b64decode(raw).decode("utf-8")


def _follow_same_host(
    client: httpx.Client,
    response: httpx.Response,
    *,
    origin: str,
    headers: dict[str, str] | None = None,
    hops: int = 5,
) -> httpx.Response:
    """Follow in-app redirects; do not chase the public Horizon hostname."""
    origin = origin.rstrip("/")
    posted = response
    loc = posted.headers.get("location")
    seen = 0
    while loc and seen < hops and posted.status_code in (301, 302, 303, 307, 308):
        seen += 1
        parsed = urlparse(loc)
        if parsed.scheme and parsed.netloc:
            host = parsed.netloc.split("@")[-1].split(":")[0].lower()
            origin_host = urlparse(origin).hostname or ""
            if host not in {origin_host.lower(), "horizon-int", "horizon"} and "horizon" not in host:
                break
            nxt = origin + (parsed.path or "/") + (("?" + parsed.query) if parsed.query else "")
        elif loc.startswith("/"):
            nxt = origin + loc
        else:
            nxt = origin + "/" + loc
        posted = client.get(nxt, headers=headers or {}, follow_redirects=False)
        loc = posted.headers.get("location")
    return posted


def login_with_client(
    client: httpx.Client,
    origin: str,
    *,
    username: str,
    password: str,
    domain: str = "default",
    extra_headers: dict[str, str] | None = None,
    referer_origin: str | None = None,
) -> dict[str, str]:
    """POST Horizon's login form. ``origin`` is scheme+host with no path."""
    origin = origin.rstrip("/")
    login_url = origin + LOGIN_PATH
    extra = dict(extra_headers or {})
    page = client.get(login_url, headers=extra, follow_redirects=False)
    page = _follow_same_host(client, page, origin=origin, headers=extra)
    if page.status_code >= 400:
        raise HorizonProxyError(f"horizon login page HTTP {page.status_code}")
    html = page.text or ""
    csrf = _csrf_from_html(html)
    if not csrf:
        # Already at the dashboard (session exists) or unusual form.
        if "csrfmiddlewaretoken" not in html.lower() and page.status_code == 200:
            jar = {str(k): str(v) for k, v in client.cookies.items()}
            _merge_cookies(jar, page)
            return jar
        raise HorizonProxyError("horizon login form missing CSRF token")
    region = _region_from_html(html)
    domain_name = _domain_from_html(html) if domain == "default" else domain
    form: dict[str, str] = {
        "csrfmiddlewaretoken": csrf,
        "username": username,
        "password": password,
        "domain": domain_name,
        "next": "/",
    }
    if region:
        form["region"] = region
    referer = (referer_origin or origin).rstrip("/") + LOGIN_PATH
    jar = {str(k): str(v) for k, v in client.cookies.items()}
    _merge_cookies(jar, page)
    headers = {
        "Referer": referer,
        "Content-Type": "application/x-www-form-urlencoded",
        "X-CSRFToken": csrf,
    }
    headers.update(extra)
    cookie = _cookie_header(jar)
    if cookie:
        headers["Cookie"] = cookie
    posted = client.post(login_url, data=form, headers=headers, follow_redirects=False)
    _merge_cookies(jar, posted)
    follow_headers = dict(extra)
    cookie = _cookie_header(jar)
    if cookie:
        follow_headers["Cookie"] = cookie
    posted = _follow_same_host(client, posted, origin=origin, headers=follow_headers)
    _merge_cookies(jar, posted)
    if posted.status_code >= 400:
        raise HorizonProxyError(f"horizon login HTTP {posted.status_code}")
    body = (posted.text or "").lower()
    if "csrfmiddlewaretoken" in body and 'name="password"' in body:
        raise HorizonProxyError("horizon login rejected")
    for name, value in client.cookies.items():
        jar[str(name)] = str(value)
    return jar


_pf_lock = threading.Lock()
_pf_proc: subprocess.Popen[bytes] | None = None
_pf_kubeconfig: str | None = None
_pf_digest: str | None = None
_pf_port: int | None = None
_pf_target: str | None = None


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
    dest = Path(tempfile.gettempdir()) / f"gsc-horizon-kubeconfig-{digest[:16]}"
    dest.write_bytes(src.read_bytes())
    dest.chmod(0o600)
    _pf_kubeconfig = str(dest)
    return str(dest)


def _stop_portforward() -> None:
    global _pf_proc, _pf_digest, _pf_port, _pf_target
    with _pf_lock:
        if _pf_proc is not None and _pf_proc.poll() is None:
            _pf_proc.terminate()
            try:
                _pf_proc.wait(timeout=2)
            except Exception:  # noqa: BLE001
                _pf_proc.kill()
        _pf_proc = None
        _pf_digest = None
        _pf_port = None
        _pf_target = None


def ensure_horizon_portforward(
    kubeconfig: str,
    *,
    service: str,
    port: int,
    local_port: int = HORIZON_LOCAL_PORT,
) -> int:
    """Keep a kubectl port-forward to Horizon on loopback."""
    global _pf_proc, _pf_digest, _pf_port, _pf_target
    digest = _kubeconfig_digest(kubeconfig)
    target = f"{service}:{port}"
    kubectl = shutil.which("kubectl")
    if not kubectl:
        raise HorizonProxyError("kubectl not found on PATH")
    stable = _copy_kubeconfig(kubeconfig, digest)
    with _pf_lock:
        if (
            _pf_proc is not None
            and _pf_proc.poll() is None
            and _pf_digest == digest
            and _pf_port == local_port
            and _pf_target == target
            and _port_open(local_port)
        ):
            return local_port
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
            f"svc/{service}",
            f"{local_port}:{port}",
        ]
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            raise HorizonProxyError("failed to start Horizon port-forward") from exc
        _pf_proc = proc
        deadline = time.monotonic() + PORTFORWARD_WAIT
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                _pf_proc = None
                _pf_digest = None
                _pf_port = None
                _pf_target = None
                raise HorizonProxyError("horizon port-forward exited")
            if _port_open(local_port):
                _pf_digest = digest
                _pf_port = local_port
                _pf_target = target
                return local_port
            time.sleep(0.2)
        raise HorizonProxyError("horizon port-forward did not become ready")


def _local_origin(local_port: int) -> str:
    return f"http://127.0.0.1:{int(local_port)}"


def open_session(
    kubeconfig_path: str,
    *,
    env_id: str,
    actor: str,
) -> tuple[str, HorizonSession]:
    """Mint a session, find Horizon, log in as Keystone admin."""
    try:
        kube, apiserver, cleanup = load_kube_http(kubeconfig_path)
    except Exception as exc:  # noqa: BLE001
        raise HorizonProxyError("failed to open kube client") from exc
    try:
        password = _admin_password(kube, apiserver)
    finally:
        try:
            kube.close()
        except Exception:  # noqa: BLE001
            pass
        for item in cleanup:
            try:
                Path(item).unlink(missing_ok=True)
            except OSError:
                pass

    last_err = "horizon service not found"
    for service, port in HORIZON_CANDIDATES:
        try:
            local = ensure_horizon_portforward(
                kubeconfig_path, service=service, port=port
            )
        except HorizonProxyError as exc:
            last_err = str(exc)
            continue
        extra = {"Host": service}
        origin = _local_origin(local)
        try:
            with httpx.Client(
                timeout=HTTP_TIMEOUT, follow_redirects=False, trust_env=False
            ) as client:
                cookies = login_with_client(
                    client,
                    origin,
                    username="admin",
                    password=password,
                    extra_headers=extra,
                    referer_origin=f"http://{service}",
                )
        except HorizonProxyError as exc:
            last_err = str(exc)
            # Login page was reachable; do not mask CSRF/auth with a later
            # missing-service connection error.
            if "login" in str(exc).lower() or "csrf" in str(exc).lower():
                break
            continue
        except Exception as exc:  # noqa: BLE001
            last_err = str(exc)
            continue
        sid = create_session(env_id=env_id, actor=actor)
        entry = get_session(sid, env_id)
        if entry is None:
            raise HorizonProxyError("session store failed")
        entry.service = service
        entry.port = port
        entry.cookies = cookies
        return sid, entry
    raise HorizonProxyError(last_err)


def forward(
    kubeconfig_path: str,
    session: HorizonSession,
    *,
    method: str,
    path: str,
    query: str = "",
    body: bytes | None = None,
    content_type: str | None = None,
    extra_headers: dict[str, str] | None = None,
) -> tuple[int, bytes, dict[str, str], list[str]]:
    """Proxy one request. Returns status, body, headers, set-cookie list."""
    if not is_safe_path(path):
        raise HorizonProxyError("invalid path")
    rel = path if path.startswith("/") else f"/{path}"
    if query:
        rel = f"{rel}?{query}"
    try:
        local = ensure_horizon_portforward(
            kubeconfig_path, service=session.service, port=session.port
        )
    except HorizonProxyError:
        raise
    url = _local_origin(local) + rel
    headers: dict[str, str] = {"Host": session.service}
    cookie = _cookie_header(session.cookies)
    if cookie:
        headers["Cookie"] = cookie
    if content_type:
        headers["Content-Type"] = content_type
    if extra_headers:
        for key, val in extra_headers.items():
            lk = key.lower()
            if lk in _HOP or lk in _STRIP_REQ:
                continue
            headers[key] = val
    try:
        with httpx.Client(
            timeout=HTTP_TIMEOUT, follow_redirects=False, trust_env=False
        ) as client:
            response = client.request(
                method.upper(),
                url,
                content=body if body else None,
                headers=headers,
            )
        _merge_cookies(session.cookies, response)
        out_headers: dict[str, str] = {}
        set_cookies: list[str] = []
        for key, val in response.headers.items():
            lk = key.lower()
            if lk in _STRIP_RESP or lk in _HOP:
                continue
            if lk == "set-cookie":
                set_cookies.append(val)
                continue
            out_headers[key] = val
        getter = getattr(response.headers, "get_list", None)
        if callable(getter):
            set_cookies = list(getter("set-cookie")) or set_cookies
        return response.status_code, bytes(response.content or b""), out_headers, set_cookies
    except HorizonProxyError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HorizonProxyError("horizon request failed") from exc
