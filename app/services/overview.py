"""One Overview read of the slow cluster and cloud sections.

Inventory, jobs, and bare metal stay on their own routes. Each section here
uses the same cache as the route the page used to call, so a miss dials once
and a later open of that route reuses the answer. One section that fails
leaves the others in the response.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any

from app.config import get_settings
from app.db import SessionLocal
from app.models import Environment
from app.services import k8s_ops, openstack_ops, platform, remembered
from app.services.demo import (
    canned_cloud_inventory,
    canned_gateways,
    canned_httproutes,
    canned_ingresses,
    canned_metallb_pools,
    canned_platform_overview,
    canned_services,
    canned_vms,
    canned_workloads,
    is_demo_env,
)
from app.services.read_cache import cached_read

_SECTIONS = (
    "platform",
    "cloud",
    "vms",
    "ingresses",
    "services",
    "gateways",
    "httproutes",
    "pools",
    "workloads",
)


def _read_key(
    env: Environment,
    kind: str,
    *,
    namespace: str | None = None,
    pods_only: bool | None = None,
) -> str:
    """Same key shape as the Kubernetes routes, so the caches are one cache."""
    env_id = str(getattr(env, "id", "") or "").strip()
    if not env_id:
        return ""
    key = f"k8s.{kind}:{env_id}"
    if namespace is not None:
        key += f":{namespace}"
    if pods_only is not None:
        key += f":pods_only={int(bool(pods_only))}"
    return key


def _section(env_id: str, name: str, *, refresh: bool) -> dict[str, Any]:
    with SessionLocal() as db:
        env = db.get(Environment, env_id)
        if env is None:
            return {"ok": False, "error": "missing"}
        settings = get_settings()
        demo = is_demo_env(env)
        if name == "platform":
            if demo:
                return {"environment_id": env.id, **canned_platform_overview(env)}
            result = platform.platform_overview(env, settings, db, refresh=refresh)
            return {"environment_id": env.id, **result}
        if name == "cloud":
            if demo:
                return {"environment_id": env.id, **canned_cloud_inventory()}
            result = openstack_ops.cloud_inventory(env, settings, refresh=refresh)
            return {"environment_id": env.id, **result}
        if name == "vms":
            if demo:
                return {"environment_id": env.id, **canned_vms()}
            result = cached_read(
                f"vms.servers:{env_id}",
                lambda: openstack_ops.list_servers(env, settings),
                refresh=refresh,
            )
            return {"environment_id": env.id, **result}
        if name == "ingresses":
            if demo:
                return canned_ingresses(namespace="")
            return cached_read(
                _read_key(env, "ingresses", namespace=""),
                lambda: k8s_ops.list_ingresses(env, settings, namespace=""),
                refresh=refresh,
            )
        if name == "services":
            if demo:
                return canned_services(namespace="")
            return cached_read(
                _read_key(env, "services", namespace=""),
                lambda: k8s_ops.list_services(env, settings, namespace=""),
                refresh=refresh,
            )
        if name == "gateways":
            if demo:
                return canned_gateways(namespace="")
            return cached_read(
                _read_key(env, "gateways", namespace=""),
                lambda: k8s_ops.list_gateways(env, settings, namespace=""),
                refresh=refresh,
            )
        if name == "httproutes":
            if demo:
                return canned_httproutes(namespace="")
            return cached_read(
                _read_key(env, "httproutes", namespace=""),
                lambda: k8s_ops.list_httproutes(env, settings, namespace=""),
                refresh=refresh,
            )
        if name == "pools":
            if demo:
                return canned_metallb_pools()
            return cached_read(
                _read_key(env, "metallb_pools"),
                lambda: k8s_ops.list_metallb_pools(env, settings),
                refresh=refresh,
            )
        if name == "workloads":
            if demo:
                return canned_workloads(namespace="", pods_only=True)
            return cached_read(
                _read_key(env, "workloads", namespace="", pods_only=True),
                lambda: k8s_ops.list_workloads(
                    env, settings, namespace="", pods_only=True
                ),
                refresh=refresh,
            )
        return {"ok": False, "error": "unknown"}


def _gather(env_id: str, *, refresh: bool) -> dict[str, Any]:
    """Dial the slow sections. Wall time follows the slowest one."""
    out: dict[str, Any] = {"environment_id": env_id}
    with ThreadPoolExecutor(max_workers=len(_SECTIONS)) as pool:
        futures = {
            name: pool.submit(_section, env_id, name, refresh=refresh)
            for name in _SECTIONS
        }
        for name, future in futures.items():
            try:
                out[name] = future.result()
            except Exception:  # noqa: BLE001
                out[name] = {"ok": False, "error": "unavailable"}
    return out


def _from_disk(env_id: str, stored: dict[str, Any]) -> dict[str, Any]:
    body = dict(stored["body"])
    body["environment_id"] = env_id
    body["remembered"] = True
    body["age_s"] = int(stored["age_s"])
    return body


def overview_read(env_id: str, *, refresh: bool = False) -> dict[str, Any]:
    """Return the last good Overview, then gather when asked or when none exists.

    A gather that does not answer leaves the previous copy in place.
    """
    if not refresh:
        stored = remembered.load("overview", env_id)
        if stored is not None:
            return _from_disk(env_id, stored)
    out = _gather(env_id, refresh=refresh)
    if remembered.worth_saving("overview", out):
        remembered.save("overview", env_id, out)
        out["remembered"] = False
        out["age_s"] = 0
        return out
    stored = remembered.load("overview", env_id)
    if stored is not None:
        return _from_disk(env_id, stored)
    out["remembered"] = False
    out["age_s"] = 0
    return out
