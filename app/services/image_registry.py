"""Console-owned OCI pull-through cache. Cluster nodes pull from here.

Each upstream (docker.io, quay.io, …) is a registry:2 proxy on the
provisioning NIC. Talos machine config mirrors those hosts here so a
greenfield does not re-download from the internet. Warming copies every
image the live cluster already runs through the cache.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

import yaml

from app.config import Settings, get_settings
from app.models import Environment
from app.services import envconfig as envconfig_service

log = logging.getLogger(__name__)

LogFn = Callable[[str], None]

DEFAULT_BIND = "10.200.0.50"
REGISTRY_IMAGE = "registry:2"

# Pull-through caches. Port is unique per upstream so containerd mirrors
# keep the original repository path (no host prefix).
UPSTREAMS: tuple[tuple[str, str, int], ...] = (
    ("docker.io", "https://registry-1.docker.io", 5001),
    ("quay.io", "https://quay.io", 5002),
    ("ghcr.io", "https://ghcr.io", 5003),
    ("gcr.io", "https://gcr.io", 5004),
    ("registry.k8s.io", "https://registry.k8s.io", 5005),
    ("k8s.gcr.io", "https://registry.k8s.io", 5005),
    ("registry.opensource.zalan.do", "https://registry.opensource.zalan.do", 5006),
    ("public.ecr.aws", "https://public.ecr.aws", 5007),
    ("factory.talos.dev", "https://factory.talos.dev", 5008),
    ("docker-registry1.mariadb.com", "https://docker-registry1.mariadb.com", 5009),
)

_HOST_RE = re.compile(r"^[A-Za-z0-9.-]+(?::\d+)?$")


class RegistryError(RuntimeError):
    """Registry could not start or mirror an image."""


def registry_host_from_doc(doc: dict[str, Any] | None) -> str:
    pxe = (doc or {}).get("pxe") if isinstance(doc, dict) else None
    if isinstance(pxe, dict):
        host = str(pxe.get("next_server") or pxe.get("gateway") or "").strip()
        if host:
            return host
    return DEFAULT_BIND


def split_image(ref: str) -> tuple[str, str, str]:
    """Split ``registry/repo:tag`` into ``(registry, repository, tag)``.

    Tag may be ``latest``, a version, or ``@sha256:…``.
    """
    text = str(ref or "").strip()
    if not text or " " in text:
        raise ValueError("invalid image reference")
    digest = ""
    if "@sha256:" in text:
        text, digest = text.split("@", 1)
        digest = "@" + digest
    registry = "docker.io"
    rest = text
    slash = text.find("/")
    if slash > 0:
        head = text[:slash]
        if "." in head or ":" in head or head == "localhost":
            registry = head
            rest = text[slash + 1 :]
    tag = "latest"
    if ":" in rest and rest.rsplit(":", 1)[-1] and "/" not in rest.rsplit(":", 1)[-1]:
        rest, tag = rest.rsplit(":", 1)
    if digest:
        tag = digest
    if registry == "docker.io" and "/" not in rest:
        rest = f"library/{rest}"
    return registry, rest, tag


def _upstream_for(registry: str) -> tuple[str, str, int] | None:
    key = str(registry or "").lower()
    for name, remote, port in UPSTREAMS:
        if name == key:
            return name, remote, port
    return None


def _container_name(registry: str, port: int) -> str:
    safe = re.sub(r"[^a-z0-9.-]+", "-", registry.lower()).strip("-")
    return f"gsc-registry-{safe}-{port}"


def _data_root(settings: Settings | None = None) -> Path:
    settings = settings or get_settings()
    root = Path(settings.data_dir)
    if not root.is_absolute():
        root = Path("/opt/genestack-console") / root
    return root / "registry"


def _write_proxy_config(path: Path, remoteurl: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    cfg = {
        "version": "0.1",
        "log": {"fields": {"service": "registry"}},
        "storage": {"filesystem": {"rootdirectory": "/var/lib/registry"}},
        "http": {"addr": ":5000"},
        "proxy": {"remoteurl": remoteurl},
    }
    path.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")


def _docker(*args: str, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    docker = shutil.which("docker")
    if not docker:
        raise RegistryError("docker not found on PATH")
    return subprocess.run(
        [docker, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _container_running(name: str) -> bool:
    try:
        inspect = _docker("inspect", "-f", "{{.State.Running}}", name, timeout=10)
    except Exception:  # noqa: BLE001 — status must never raise
        return False
    return inspect.returncode == 0 and "true" in (inspect.stdout or "").lower()


def _repos_on_disk(data: Path) -> list[str]:
    """registry:2 proxy often returns an empty _catalog; count cached repos on disk."""
    root = data / "docker" / "registry" / "v2" / "repositories"
    if not root.is_dir():
        return []
    found: list[str] = []
    for manifests in root.rglob("_manifests"):
        if manifests.is_dir():
            found.append(str(manifests.parent.relative_to(root)))
    return sorted(found)[:80]


def _catalog(bind: str, port: int, data: Path | None = None) -> list[str]:
    """Repositories currently in this pull-through cache. Empty on miss/timeout."""
    import httpx

    url = f"http://{bind}:{int(port)}/v2/_catalog"
    try:
        with httpx.Client(timeout=0.8) as client:
            resp = client.get(url)
        if resp.status_code == 200:
            repos = (resp.json() or {}).get("repositories") or []
            listed = [str(x) for x in repos if x][:80]
            if listed:
                return listed
    except Exception:  # noqa: BLE001
        pass
    if data is not None:
        return _repos_on_disk(data)
    return []


def _ensure_container(bind: str, registry: str, remote: str, port: int, root: Path) -> dict[str, Any]:
    name = _container_name(registry, port)
    cfg = root / f"{name}.yml"
    data = root / name
    data.mkdir(parents=True, exist_ok=True)
    _write_proxy_config(cfg, remote)
    inspect = _docker("inspect", "-f", "{{.State.Running}}", name, timeout=15)
    running = inspect.returncode == 0 and "true" in (inspect.stdout or "").lower()
    if running:
        return {"name": name, "registry": registry, "port": port, "running": True}
    _docker("rm", "-f", name, timeout=20)
    proc = _docker(
        "run",
        "-d",
        "--restart",
        "unless-stopped",
        "--name",
        name,
        "-p",
        f"{bind}:{port}:5000",
        "-v",
        f"{data}:/var/lib/registry",
        "-v",
        f"{cfg}:/etc/docker/registry/config.yml:ro",
        REGISTRY_IMAGE,
        timeout=120,
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()[:240]
        raise RegistryError(f"failed to start {name}: {err or proc.returncode}")
    return {"name": name, "registry": registry, "port": port, "running": True}


def ensure_running(
    doc: dict[str, Any] | None = None,
    settings: Settings | None = None,
    log_fn: LogFn | None = None,
) -> dict[str, Any]:
    """Start pull-through caches for every known upstream (idempotent)."""
    bind = registry_host_from_doc(doc)
    root = _data_root(settings)
    root.mkdir(parents=True, exist_ok=True)
    started: list[dict[str, Any]] = []
    seen_ports: set[int] = set()
    errors: list[str] = []
    for registry, remote, port in UPSTREAMS:
        if port in seen_ports:
            continue
        seen_ports.add(port)
        try:
            started.append(_ensure_container(bind, registry, remote, port, root))
            if log_fn:
                log_fn(f"[registry] {registry} pull-through on {bind}:{port}")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{registry}:{port} {exc}")
            log.warning("registry start failed for %s: %s", registry, exc)
    return {
        "ok": not errors,
        "bind": bind,
        "caches": started,
        "error": "; ".join(errors) if errors else None,
    }


def status(doc: dict[str, Any] | None = None, settings: Settings | None = None) -> dict[str, Any]:
    """Pull-through caches for this env's provisioning NIC. Never raises."""
    bind = registry_host_from_doc(doc)
    root = _data_root(settings)
    caches: list[dict[str, Any]] = []
    seen_ports: set[int] = set()
    for registry, remote, port in UPSTREAMS:
        if port in seen_ports:
            continue
        seen_ports.add(port)
        name = _container_name(registry, port)
        running = _container_running(name)
        caches.append(
            {
                "registry": registry,
                "remote": remote,
                "port": port,
                "endpoint": f"http://{bind}:{port}",
                "running": running,
                "images": 0,
                "repositories": [],
                "data_dir": str(root / name),
            }
        )
    running_caches = [c for c in caches if c.get("running")]
    if running_caches:
        from concurrent.futures import ThreadPoolExecutor, as_completed

        with ThreadPoolExecutor(max_workers=min(8, len(running_caches))) as pool:
            futs = {
                pool.submit(
                    _catalog,
                    bind,
                    int(c["port"]),
                    Path(str(c.get("data_dir") or "")),
                ): c
                for c in running_caches
            }
            for fut in as_completed(futs):
                item = futs[fut]
                try:
                    repos = fut.result()
                except Exception:  # noqa: BLE001
                    repos = []
                item["repositories"] = repos[:12]
                item["images"] = len(repos)
                item.pop("data_dir", None)
    for item in caches:
        item.pop("data_dir", None)
    image_count = sum(int(c.get("images") or 0) for c in caches)
    ready = sum(1 for c in caches if c.get("running"))
    return {
        "bind": bind,
        "running": ready > 0,
        "ready": ready == len(caches) and bool(caches),
        "caches": caches,
        "ready_count": ready,
        "cache_count": len(caches),
        "image_count": image_count,
    }


def for_environment(
    db: Any,
    env: Environment,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Registry payload scoped to one environment (Overview / ops)."""
    doc: dict[str, Any] | None = None
    try:
        current = envconfig_service.get_current(db, env)
        raw = current[0] if current else {}
        if isinstance(raw, dict):
            doc = raw
    except Exception:  # noqa: BLE001
        doc = None
    payload = status(doc, settings)
    payload["environment_id"] = env.id
    payload["environment_name"] = env.name
    last = None
    try:
        from sqlalchemy import select

        from app.models import Job

        row = db.scalar(
            select(Job)
            .where(Job.environment_id == env.id, Job.operation == "registry.mirror")
            .order_by(Job.created_at.desc())
            .limit(1)
        )
        if row is not None:
            last = {
                "id": row.id,
                "status": row.status.value if hasattr(row.status, "value") else str(row.status),
                "error": row.error,
                "created_at": row.created_at.isoformat() if row.created_at else None,
                "finished_at": row.finished_at.isoformat() if row.finished_at else None,
            }
    except Exception:  # noqa: BLE001
        last = None
    payload["last_mirror"] = last
    return payload


def registry_pull_env(doc: dict[str, Any] | None = None) -> dict[str, str]:
    """Env vars so deploy-host helm/scripts talk to the Console pull-through.

    Nodes already pin containerd at these caches via Talos
    ``registries.mirrors`` (``skipFallback``). This injects the same bind
    into the job subprocess environment.
    """
    bind = registry_host_from_doc(doc)
    out: dict[str, str] = {"GSC_REGISTRY_HOST": bind}
    seen: set[int] = set()
    for registry, _remote, port in UPSTREAMS:
        if port in seen:
            continue
        seen.add(port)
        key = (
            registry.upper()
            .replace(".", "_")
            .replace("-", "_")
        )
        out[f"GSC_REGISTRY_{key}"] = f"http://{bind}:{port}"
    return out


def talos_registry_patch_yaml(doc: dict[str, Any] | None = None) -> str:
    """Talos machine.registries patch: every upstream mirrors to Console.

    Endpoints are plain HTTP pull-through caches. Talos 1.13+ rejects
    ``registries.config[].tls`` on a non-HTTPS host (``TLS config specified
    for non-HTTPS registry``), which blocks kubelet image pulls. HTTP
    mirrors need no TLS skip-verify block.
    """
    bind = registry_host_from_doc(doc)
    mirrors: dict[str, Any] = {}
    for registry, _remote, port in UPSTREAMS:
        endpoint = f"http://{bind}:{port}"
        # skipFallback: kubelet talks only to the Console cache. A miss
        # is fetched by the pull-through proxy (deployer uplink), not by
        # the node hitting the internet.
        mirrors[registry] = {"endpoints": [endpoint], "skipFallback": True}
    patch = {"machine": {"registries": {"mirrors": mirrors}}}
    return yaml.safe_dump(patch, sort_keys=False)


def _cluster_images(kubeconfig: str) -> list[str]:
    kubectl = shutil.which("kubectl")
    if not kubectl:
        return []
    env = os.environ.copy()
    env["KUBECONFIG"] = kubeconfig
    proc = subprocess.run(
        [
            kubectl,
            "get",
            "pods",
            "-A",
            "-o",
            "json",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
        check=False,
    )
    if proc.returncode != 0 or not proc.stdout:
        return []
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return []
    images: set[str] = set()
    for pod in payload.get("items") or []:
        spec = (pod or {}).get("spec") or {}
        for key in ("containers", "initContainers", "ephemeralContainers"):
            for ctr in spec.get(key) or []:
                img = str((ctr or {}).get("image") or "").strip()
                if img:
                    images.add(img)
    images.add("registry.k8s.io/pause:3.10")
    return sorted(images)


_MANIFEST_ACCEPT = (
    "application/vnd.oci.image.manifest.v1+json,"
    "application/vnd.docker.distribution.manifest.v2+json,"
    "application/vnd.docker.distribution.manifest.list.v2+json,"
    "application/vnd.oci.image.index.v1+json"
)


def _warm_ref(client: Any, base: str, ref: str) -> int:
    """Fetch a manifest and its blobs through the proxy. Returns parts pulled."""
    url = f"{base}/manifests/{quote(str(ref), safe=':@')}"
    resp = client.get(url, headers={"Accept": _MANIFEST_ACCEPT})
    if resp.status_code not in (200, 307, 302):
        raise RegistryError(f"HTTP {resp.status_code}")
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        return 1
    if not isinstance(body, dict):
        return 1
    media = str(body.get("mediaType") or "")
    n = 1
    if "manifest.list" in media or "image.index" in media:
        chosen = None
        for item in body.get("manifests") or []:
            plat = (item or {}).get("platform") or {}
            if plat.get("os", "linux") == "linux" and plat.get("architecture") in ("amd64", "x86_64"):
                chosen = item.get("digest")
                break
        if not chosen:
            first = (body.get("manifests") or [{}])[0] or {}
            chosen = first.get("digest")
        if chosen:
            n += _warm_ref(client, base, chosen)
        return n
    digests: list[str] = []
    cfg = ((body.get("config") or {}) or {}).get("digest")
    if cfg:
        digests.append(str(cfg))
    for layer in body.get("layers") or []:
        digest = (layer or {}).get("digest")
        if digest:
            digests.append(str(digest))
    for digest in digests:
        blob = client.get(f"{base}/blobs/{digest}")
        if blob.status_code not in (200, 307, 302):
            raise RegistryError(f"blob {digest[:21]} HTTP {blob.status_code}")
        n += 1
    return n


def _warm_one(bind: str, registry: str, repo: str, tag: str, port: int) -> tuple[bool, str]:
    """Pull manifest + layers through the pull-through cache so greenfield hits disk."""
    import httpx

    ref = tag[1:] if tag.startswith("@") else tag
    base = f"http://{bind}:{port}/v2/{quote(repo, safe='/_-.')}"
    try:
        timeout = httpx.Timeout(300.0, connect=15.0)
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            parts = _warm_ref(client, base, ref)
        return True, f"{registry}/{repo}:{tag} cached ({parts} parts)"
    except Exception as exc:  # noqa: BLE001
        return False, f"{registry}/{repo}:{tag} {type(exc).__name__}: {exc}"


def mirror_cluster(
    env: Environment,
    kubeconfig: str | None,
    log_fn: LogFn,
    *,
    dry_run: bool,
    settings: Settings | None = None,
    check_cancel: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Pull every live cluster image through the Console caches."""
    current = None
    try:
        from app.db import SessionLocal

        db = SessionLocal()
        try:
            current = envconfig_service.get_current(db, env)
        finally:
            db.close()
    except Exception:  # noqa: BLE001
        current = None
    doc = current[0] if current else {}
    started = ensure_running(doc if isinstance(doc, dict) else None, settings, log_fn)
    if not started.get("ok") and not dry_run:
        return {
            "ok": False,
            "error": started.get("error") or "registry failed to start",
            "returncode": 2,
        }
    bind = started.get("bind") or registry_host_from_doc(doc if isinstance(doc, dict) else None)
    images = _cluster_images(kubeconfig) if kubeconfig else []
    log_fn(
        f"[registry] warming {len(images)} unique image(s) through {bind} "
        f"(manifests + layers, so the next greenfield does not hit the internet)"
    )
    ok_n = 0
    fail_n = 0
    skipped = 0
    for ref in images:
        if check_cancel:
            check_cancel()
        try:
            registry, repo, tag = split_image(ref)
        except ValueError:
            skipped += 1
            continue
        up = _upstream_for(registry)
        if up is None:
            log_fn(f"[registry] skip unknown registry {registry} ({ref})")
            skipped += 1
            continue
        _name, _remote, port = up
        if dry_run:
            log_fn(f"[dry-run] would cache {registry}/{repo}:{tag} via :{port}")
            ok_n += 1
            continue
        good, msg = _warm_one(bind, registry, repo, tag, port)
        log_fn(f"[registry] {msg}")
        if good:
            ok_n += 1
        else:
            fail_n += 1
        time.sleep(0.05)
    return {
        "ok": fail_n == 0,
        "cached": ok_n,
        "failed": fail_n,
        "skipped": skipped,
        "total": len(images),
        "bind": bind,
        "dry_run": dry_run,
        "returncode": 0 if fail_n == 0 else 1,
    }


# Images every greenfield pulls before OpenStack helm exists. Console
# fetch-through so the first helm install is LAN. Keep docker.io entries
# so unknown-upstream skips are observable in tests.
BOOTSTRAP_WARM_IMAGES: tuple[str, ...] = (
    "registry.k8s.io/pause:3.10",
    "docker.io/library/registry:2",
    "quay.io/jetstack/cert-manager-controller:v1.19.5",
    "quay.io/jetstack/cert-manager-cainjector:v1.19.5",
    "quay.io/jetstack/cert-manager-webhook:v1.19.5",
    "docker.io/longhornio/longhorn-manager:v1.11.1",
    "docker.io/longhornio/longhorn-engine:v1.11.1",
    "ghcr.io/mariadb-operator/mariadb-operator:0.38.1",
    "quay.io/metallb/controller:v0.15.2",
    "docker.io/envoyproxy/gateway:v1.7.0",
)


def warm_for_deploy(
    doc: dict[str, Any] | None,
    kubeconfig: str | None,
    settings: Settings | None,
    log_fn: LogFn,
    *,
    dry_run: bool = False,
    check_cancel: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Prime pull-through caches with bootstrap + live cluster images.

    ``settings`` is accepted for the caller's signature; warming does not
    use it (bind comes from the env config doc). Dry-run never touches
    docker or the network and omits ``bind`` from the result.
    """
    del settings  # call-compatible with deploy/greenfield
    images: list[str] = list(BOOTSTRAP_WARM_IMAGES)
    seen = set(images)
    if kubeconfig:
        for img in _cluster_images(kubeconfig):
            if img not in seen:
                images.append(img)
                seen.add(img)
    bind = registry_host_from_doc(doc)
    log_fn(
        f"[registry] warming {len(images)} unique image(s) through {bind} "
        "(manifests + layers, so helm pulls over LAN)"
    )
    ok_n = 0
    fail_n = 0
    skipped = 0
    for ref in images:
        if check_cancel:
            check_cancel()
        try:
            registry, repo, tag = split_image(ref)
        except ValueError:
            skipped += 1
            continue
        up = _upstream_for(registry)
        if up is None:
            log_fn(f"[registry] skip unknown registry {registry} ({ref})")
            skipped += 1
            continue
        _name, _remote, port = up
        if dry_run:
            log_fn(f"[dry-run] would cache {registry}/{repo}:{tag} via :{port}")
            ok_n += 1
            continue
        good, msg = _warm_one(bind, registry, repo, tag, port)
        log_fn(f"[registry] {msg}")
        if good:
            ok_n += 1
        else:
            fail_n += 1
    result: dict[str, Any] = {
        "ok": fail_n == 0,
        "cached": ok_n,
        "failed": fail_n,
        "skipped": skipped,
        "total": len(images),
        "dry_run": dry_run,
    }
    if not dry_run:
        result["bind"] = bind
    return result
