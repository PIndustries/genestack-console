"""Grafana / Prometheus / Alertmanager / Tempo in-console proxy.

Operator browser cannot reach monitoring ClusterIPs. Console port-forwards
to the service, logs into Grafana with the in-cluster admin secret (never
sent to the browser), and proxies HTTP under::

    /api/v1/environments/{id}/cloud/{kind}/{session_id}/…

Prometheus / Alertmanager / Tempo have no login. Loki has no UI — use the
observe/logs API (native Console viewer) or Grafana Explore.
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

from app.services.horizon_proxy import (
    _HOP,
    _STRIP_REQ,
    _STRIP_RESP,
    is_safe_path,
    rewrite_html,
    rewrite_location,
    rewrite_set_cookie,
)
from app.services.osclient import load_kube_http

SESSION_TTL_SECONDS = 30 * 60
SESSION_ID_RE = re.compile(r"^ob_[A-Za-z0-9_-]{8,80}$")
HTTP_TIMEOUT = 25.0
PORTFORWARD_WAIT = 8.0
MONITORING_NS = "monitoring"

KINDS: dict[str, dict[str, Any]] = {
    "grafana": {
        "label": "Grafana",
        "namespace": MONITORING_NS,
        "services": (("grafana", 80),),
        "local_port": 16083,
        "home": "/",
        "login": True,
        "secret": "grafana",
    },
    "prometheus": {
        "label": "Prometheus",
        "namespace": MONITORING_NS,
        "services": (
            ("kube-prometheus-stack-prometheus", 9090),
            ("prometheus-operated", 9090),
            ("prometheus", 9090),
        ),
        "local_port": 16084,
        "home": "/graph",
        "login": False,
    },
    "alertmanager": {
        "label": "Alertmanager",
        "namespace": MONITORING_NS,
        "services": (
            ("kube-prometheus-stack-alertmanager", 9093),
            ("alertmanager-operated", 9093),
        ),
        "local_port": 16085,
        "home": "/#/alerts",
        "login": False,
    },
    "tempo": {
        "label": "Tempo",
        "namespace": MONITORING_NS,
        "services": (("tempo", 3200),),
        "local_port": 16087,
        "home": "/",
        "login": False,
    },
}


class ObsProxyError(RuntimeError):
    """Could not reach a monitoring dashboard."""


@dataclass
class ObsSession:
    env_id: str
    actor: str
    kind: str
    created: float
    cookies: dict[str, str] = field(default_factory=dict)
    service: str = ""
    port: int = 80
    namespace: str = MONITORING_NS


_lock = threading.Lock()
_store: dict[str, ObsSession] = {}
_pf_lock = threading.Lock()
_pf: dict[str, dict[str, Any]] = {}


def create_session(*, env_id: str, actor: str, kind: str) -> str:
    session_id = "ob_" + secrets.token_urlsafe(24)
    entry = ObsSession(env_id=env_id, actor=actor, kind=kind, created=time.time())
    with _lock:
        _purge_expired()
        _store[session_id] = entry
    return session_id


def get_session(session_id: str, env_id: str | None = None) -> ObsSession | None:
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
    with _pf_lock:
        for item in list(_pf.values()):
            proc = item.get("proc")
            if proc is not None and proc.poll() is None:
                try:
                    proc.terminate()
                except Exception:  # noqa: BLE001
                    pass
        _pf.clear()


def proxy_prefix(env_id: str, kind: str, session_id: str) -> str:
    return f"/api/v1/environments/{env_id}/cloud/{kind}/{session_id}"


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            return True
    except OSError:
        return False


def _kubeconfig_digest(path: str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:16]


def _copy_kubeconfig(src: str, digest: str) -> str:
    dest = Path(tempfile.gettempdir()) / f"gsc-obs-{digest}.kubeconfig"
    if not dest.exists():
        shutil.copy2(src, dest)
        dest.chmod(0o600)
    return str(dest)


def ensure_portforward(
    kubeconfig: str,
    *,
    kind: str,
    namespace: str,
    service: str,
    port: int,
    local_port: int,
) -> int:
    kubectl = shutil.which("kubectl")
    if not kubectl:
        raise ObsProxyError("kubectl not found on PATH")
    digest = _kubeconfig_digest(kubeconfig)
    target = f"{namespace}/{service}:{port}"
    stable = _copy_kubeconfig(kubeconfig, digest)
    with _pf_lock:
        cur = _pf.get(kind)
        if (
            cur
            and cur.get("proc") is not None
            and cur["proc"].poll() is None
            and cur.get("digest") == digest
            and cur.get("target") == target
            and cur.get("port") == local_port
            and _port_open(local_port)
        ):
            return local_port
        if cur and cur.get("proc") is not None and cur["proc"].poll() is None:
            try:
                cur["proc"].terminate()
            except Exception:  # noqa: BLE001
                pass
        cmd = [
            kubectl,
            "--kubeconfig",
            stable,
            "-n",
            namespace,
            "port-forward",
            f"svc/{service}",
            f"{local_port}:{port}",
        ]
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as exc:
            raise ObsProxyError(f"failed to start {kind} port-forward") from exc
        deadline = time.monotonic() + PORTFORWARD_WAIT
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise ObsProxyError(f"{kind} port-forward exited")
            if _port_open(local_port):
                _pf[kind] = {
                    "proc": proc,
                    "digest": digest,
                    "target": target,
                    "port": local_port,
                }
                return local_port
            time.sleep(0.2)
        raise ObsProxyError(f"{kind} port-forward did not become ready")


def _grafana_secret(kube: httpx.Client, apiserver: str) -> tuple[str, str]:
    resp = kube.get(f"{apiserver}/api/v1/namespaces/{MONITORING_NS}/secrets/grafana")
    if resp.status_code != 200:
        raise ObsProxyError("grafana secret unavailable")
    data = (resp.json() or {}).get("data") or {}
    user_b = data.get("admin-user")
    pw_b = data.get("admin-password")
    if not pw_b:
        raise ObsProxyError("grafana secret has no admin-password")
    user = base64.b64decode(user_b).decode("utf-8") if user_b else "admin"
    password = base64.b64decode(pw_b).decode("utf-8")
    return user, password


def _merge_cookies(store: dict[str, str], response: httpx.Response) -> None:
    for name, value in response.cookies.items():
        store[str(name)] = str(value)


def open_session(
    kubeconfig_path: str,
    *,
    env_id: str,
    actor: str,
    kind: str,
) -> tuple[str, ObsSession]:
    spec = KINDS.get(kind)
    if spec is None:
        raise ObsProxyError(f"unknown dashboard '{kind}'")
    kube, apiserver, _cleanup = load_kube_http(kubeconfig_path)
    try:
        last_err = "no service"
        picked: tuple[str, int] | None = None
        for name, port in spec["services"]:
            url = (
                f"{apiserver}/api/v1/namespaces/{spec['namespace']}/services/{name}"
            )
            resp = kube.get(url)
            if resp.status_code == 200:
                picked = (name, int(port))
                break
            last_err = f"{name} HTTP {resp.status_code}"
        if picked is None:
            raise ObsProxyError(f"{spec['label']} service not found ({last_err})")
        service, port = picked
        local = ensure_portforward(
            kubeconfig_path,
            kind=kind,
            namespace=spec["namespace"],
            service=service,
            port=port,
            local_port=int(spec["local_port"]),
        )
        origin = f"http://127.0.0.1:{local}"
        sid = create_session(env_id=env_id, actor=actor, kind=kind)
        entry = get_session(sid, env_id)
        assert entry is not None
        entry.service = service
        entry.port = port
        entry.namespace = spec["namespace"]
        if spec.get("login"):
            user, password = _grafana_secret(kube, apiserver)
            with httpx.Client(timeout=HTTP_TIMEOUT, follow_redirects=False) as client:
                login = client.post(
                    f"{origin}/login",
                    json={"user": user, "password": password},
                    headers={"Content-Type": "application/json", "Accept": "application/json"},
                )
                _merge_cookies(entry.cookies, login)
                if login.status_code not in (200, 204, 302, 303) or not entry.cookies:
                    raise ObsProxyError(f"grafana login failed HTTP {login.status_code}")
        return sid, entry
    finally:
        kube.close()


def upstream_url(entry: ObsSession, path: str) -> str:
    spec = KINDS[entry.kind]
    local = int(spec["local_port"])
    rel = path if path.startswith("/") else f"/{path}"
    return f"http://127.0.0.1:{local}{rel}"


def cookie_header(entry: ObsSession) -> str:
    return "; ".join(f"{k}={v}" for k, v in entry.cookies.items() if k)


def rewrite_body(kind: str, content_type: str, body: bytes, prefix: str) -> bytes:
    if "html" not in (content_type or "").lower() and "javascript" not in (content_type or "").lower():
        return body
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return body
    hosts = (kind, f"{kind}.example.com")
    out = rewrite_html(text, prefix, public_hosts=hosts)
    return out.encode("utf-8")


def public_hosts_for(kind: str) -> tuple[str, ...]:
    return (kind, f"{kind}.example.com")
