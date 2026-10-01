"""Greenfield redeploy: PXE (or OVH BYOI) every inventory box, then full pipeline.

Puts metal back into Talos maintenance (one-shot PXE, then iLO virtual CD
when DHCP/TFTP never show, or when the box comes back k8s Ready / old OS;
OVH BYOI on OVH). Real maintenance is :50000 up and not a Ready k8s node.
Then ``genestack.deploy`` from ``hosts``. Never logs BMC passwords.
"""

from __future__ import annotations

import time
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import BaremetalNode, Environment
from app.services import baremetal as baremetal_service
from app.services import deploy as deploy_service
from app.services import deploy_timing
from app.services import envconfig as envconfig_service
from app.services.envcontext import EnvContext
from app.services.ovh import env_is_ovh

LogFn = Callable[[str], None]

_BOOT_MODES = frozenset({"auto", "pxe", "iso"})
_WAIT_DEFAULT = 900
_POLL_S = 5.0
_ISO_RETRY_LIMIT = 2
_ISO_COOLDOWN_S = 120.0


def _servers(doc: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    raw = (doc or {}).get("servers") if isinstance(doc, dict) else None
    if not isinstance(raw, dict):
        return {}
    return {str(k): v for k, v in raw.items() if isinstance(v, dict)}


def _server_ip(entry: dict[str, Any]) -> str:
    for key in ("private_ip", "ip", "cluster_ip", "public_ip"):
        val = str(entry.get(key) or "").split("/", 1)[0].strip()
        if val:
            return val
    return ""


def _bm_by_name(db: Session, env: Environment) -> dict[str, BaremetalNode]:
    rows = list(
        db.scalars(select(BaremetalNode).where(BaremetalNode.environment_id == env.id))
    )
    out: dict[str, BaremetalNode] = {}
    for row in rows:
        out[str(row.name or "").strip().lower()] = row
        short = str(row.name or "").split("-")[-1].strip().lower()
        if short and short not in out:
            out[short] = row
    return out


def _boot_one(
    db: Session,
    env: Environment,
    node: BaremetalNode,
    *,
    boot: str,
    dry_run: bool,
    log: LogFn,
    settings: Settings | None,
) -> dict[str, Any]:
    return baremetal_service.boot_for_talos(
        db, env, node, boot=boot, dry_run=dry_run, log=log, settings=settings
    )


def run_greenfield(
    db: Session,
    env: Environment,
    ctx: EnvContext,
    log: LogFn,
    *,
    dry_run: bool,
    skip_push: bool,
    timeout: int,
    extra_env: dict[str, str],
    ssh_target: str | None,
    remote_env: dict[str, str],
    settings: Settings,
    deadline: float | None = None,
    check_cancel: Callable[[], None] | None = None,
    parallelism: int | None = None,
    boot: str = "auto",
    wait_seconds: int | None = None,
) -> dict[str, Any]:
    """PXE/BYOI every inventory server, then ``run_deploy`` from hosts."""
    boot_mode = str(boot or "auto").strip().lower()
    if boot_mode not in _BOOT_MODES:
        msg = f"Unknown boot '{boot}'. Valid: auto, pxe, iso"
        log(f"[greenfield] {msg}")
        return {"ok": False, "error": msg, "returncode": 2, "dry_run": dry_run}

    wait_s = int(wait_seconds or _WAIT_DEFAULT)
    if wait_s < 60:
        wait_s = 60
    workers = 2
    if parallelism is not None:
        try:
            workers = max(1, min(16, int(parallelism)))
        except (TypeError, ValueError):
            workers = 2

    current = envconfig_service.get_current(db, env)
    doc: dict[str, Any] = current[0] if current else {}
    servers = _servers(doc)
    if not servers:
        msg = "greenfield requires a config document with servers: (inventory)"
        log(f"[greenfield] {msg}")
        return {"ok": False, "error": msg, "returncode": 2, "dry_run": dry_run}

    t0 = deploy_timing.now()
    metal_hosts: list[dict[str, Any]] = []
    phases: list[dict[str, Any]] = []

    def _stamp(payload: dict[str, Any]) -> dict[str, Any]:
        total = round(deploy_timing.elapsed(t0), 1)
        deploy_timing.log_timing(log, phase="total", seconds=total)
        out = dict(payload)
        timings = dict(out.get("timings") or {})
        timings["phases"] = list(phases) + list(timings.get("phases") or [])
        timings["hosts"] = list(metal_hosts) + list(timings.get("hosts") or [])
        timings["total_s"] = total
        out["timings"] = timings
        return out

    log(
        f"[greenfield] DESTRUCTIVE: PXE/ISO every inventory box, format Talos "
        f"install disks, rebuild Kubernetes + OpenStack "
        f"(boot={boot_mode} dry_run={dry_run} hosts={len(servers)})"
    )

    if env_is_ovh(env):
        log("[greenfield] OVH environment — BYOI reinstall then pipeline from hosts")
        byoi = deploy_service.ovh_byoi_reinstall_for_env(
            db,
            env,
            dry_run=dry_run,
            wait=not dry_run,
            log=log,
            deadline=deadline,
            check_cancel=check_cancel,
        )
        if not byoi.get("ok"):
            return _stamp(
                {
                    "ok": False,
                    "error": byoi.get("error") or "OVH BYOI reinstall failed",
                    "returncode": byoi.get("returncode") or 1,
                    "dry_run": dry_run,
                    "failed_at": "greenfield/byoi",
                }
            )
    else:
        bm_index = _bm_by_name(db, env)
        plan: list[tuple[str, BaremetalNode, str]] = []
        missing: list[str] = []
        for hostname, entry in servers.items():
            node = bm_index.get(hostname.strip().lower()) or bm_index.get(
                hostname.split("-")[-1].strip().lower()
            )
            if node is None:
                missing.append(hostname)
                continue
            ip = str(node.expected_ip or "").strip() or _server_ip(entry)
            if not ip:
                missing.append(f"{hostname} (no expected_ip / private_ip)")
                continue
            if not node.expected_ip:
                node.expected_ip = ip
                db.add(node)
            plan.append((hostname, node, ip))
        db.flush()
        if missing:
            msg = (
                "greenfield needs a registered BMC for every inventory server: "
                + ", ".join(missing)
            )
            log(f"[greenfield] {msg}")
            return {
                "ok": False,
                "error": msg,
                "returncode": 2,
                "dry_run": dry_run,
                "failed_at": "greenfield/bmc",
            }

        if dry_run:
            for hostname, node, ip in plan:
                log(
                    f"[dry-run] would PXE/ISO-boot {hostname} bmc={node.bmc_host} "
                    f"then wait for talos maintenance at {ip}:{baremetal_service.TALOS_API_PORT}"
                )
        else:
            try:
                pxe_t0 = deploy_timing.now()
                baremetal_service._prepare_pxe(db, env, settings, log)
                pxe_s = round(deploy_timing.elapsed(pxe_t0), 1)
                deploy_timing.log_timing(log, phase="pxe-prep", seconds=pxe_s)
                phases.append({"id": "pxe-prep", "seconds": pxe_s})
            except Exception as exc:  # noqa: BLE001
                log(f"[greenfield] pxe prep failed: {exc}")
                return _stamp(
                    {
                        "ok": False,
                        "error": f"pxe preparation failed: {exc}",
                        "returncode": 2,
                        "dry_run": False,
                        "failed_at": "greenfield/pxe-prep",
                    }
                )
            boot_errors: list[str] = []
            iso_count: dict[str, int] = {}
            last_iso_at: dict[str, float] = {}
            node_by_host: dict[str, BaremetalNode] = {}
            boot_t0 = deploy_timing.now()
            host_t0 = {hostname: deploy_timing.now() for hostname, _node, _ip in plan}
            for hostname, node, _ip in plan:
                if check_cancel:
                    check_cancel()
                result = _boot_one(
                    db,
                    env,
                    node,
                    boot=boot_mode,
                    dry_run=False,
                    log=log,
                    settings=settings,
                )
                node_by_host[hostname] = node
                if str(result.get("via") or "") == "iso":
                    iso_count[hostname] = 1
                    last_iso_at[hostname] = time.monotonic()
                else:
                    iso_count[hostname] = 0
                if not result.get("ok"):
                    boot_errors.append(
                        f"{hostname}: {result.get('error') or 'boot failed'}"
                    )
            boot_s = round(deploy_timing.elapsed(boot_t0), 1)
            deploy_timing.log_timing(log, phase="pxe", seconds=boot_s)
            phases.append({"id": "pxe", "seconds": boot_s})
            if boot_errors:
                msg = "PXE/ISO boot failed: " + "; ".join(boot_errors)
                log(f"[greenfield] {msg}")
                return _stamp(
                    {
                        "ok": False,
                        "error": msg,
                        "returncode": 1,
                        "dry_run": False,
                        "failed_at": "greenfield/pxe",
                    }
                )

            kube = getattr(ctx, "kubeconfig", None)
            was_ready: set[str] = set()
            for _hostname, _node, ip in plan:
                if baremetal_service.k8s_ready_for_ip(ip, kube) is True:
                    was_ready.add(ip)
            if was_ready:
                log(
                    f"[greenfield] {len(was_ready)} inventory IPs are k8s Ready "
                    "now — those must go down; Ready again means the old OS"
                )
            wait_t0 = deploy_timing.now()
            deadline_wait = time.monotonic() + wait_s
            pending = {hostname: ip for hostname, _node, ip in plan}
            saw_down: dict[str, bool] = {hostname: False for hostname in pending}
            attempt = 0
            while pending:
                if check_cancel:
                    check_cancel()
                if deadline is not None and time.monotonic() >= deadline:
                    wait_s_done = round(deploy_timing.elapsed(wait_t0), 1)
                    deploy_timing.log_timing(log, phase="metal", seconds=wait_s_done)
                    phases.append({"id": "metal", "seconds": wait_s_done})
                    return _stamp(
                        {
                            "ok": False,
                            "error": "greenfield deadline exceeded waiting for Talos maintenance",
                            "returncode": 1,
                            "failed_at": "greenfield/maintenance",
                        }
                    )
                ready: list[str] = []
                remount: list[str] = []
                for hostname, ip in pending.items():
                    state = baremetal_service.host_boot_state(
                        ip,
                        kube,
                        was_ready=was_ready,
                        saw_down=saw_down.get(hostname, False),
                    )
                    if state == "down":
                        saw_down[hostname] = True
                    if state == "maintenance":
                        ready.append(hostname)
                        host_s = round(
                            deploy_timing.elapsed(host_t0.get(hostname, wait_t0)), 1
                        )
                        deploy_timing.log_timing(
                            log, phase="metal", host=hostname, seconds=host_s
                        )
                        metal_hosts.append(
                            {"host": hostname, "seconds": host_s, "phase": "metal"}
                        )
                        log(
                            f"[greenfield] {hostname} in Talos maintenance at "
                            f"{ip}:{baremetal_service.TALOS_API_PORT} "
                            "(not a Ready k8s node)"
                        )
                        try:
                            from app.services.live_events import publish_metal

                            publish_metal(
                                getattr(env, "id", None),
                                "boot",
                                f"{hostname} in Talos maintenance",
                                host=hostname,
                                ip=ip,
                            )
                        except Exception:
                            pass
                    elif (
                        state == "old-os"
                        and boot_mode != "pxe"
                        and iso_count.get(hostname, 0) < _ISO_RETRY_LIMIT
                        and (time.monotonic() - last_iso_at.get(hostname, 0.0))
                        >= _ISO_COOLDOWN_S
                    ):
                        remount.append(hostname)
                for hostname in ready:
                    pending.pop(hostname, None)
                    node = node_by_host.get(hostname) or bm_index.get(
                        hostname.strip().lower()
                    )
                    if node is not None and str(node.state or "") != "talos-ready":
                        node.state = "talos-ready"
                        db.add(node)
                if ready:
                    db.commit()
                for hostname in remount:
                    if hostname not in pending:
                        continue
                    if check_cancel:
                        check_cancel()
                    node = node_by_host.get(hostname)
                    if node is None:
                        continue
                    log(
                        f"[greenfield] {hostname} booted the old OS (k8s Ready) — "
                        "PXE/ISO did not stick; mounting iLO virtual CD"
                    )
                    try:
                        from app.services.live_events import publish_metal

                        publish_metal(
                            getattr(env, "id", None),
                            "iso",
                            f"{hostname} old OS (k8s Ready) — remounting iLO disc",
                            host=hostname,
                        )
                    except Exception:
                        pass
                    iso = baremetal_service.iso_boot(
                        db, env, node, dry_run=False, log=log, settings=settings
                    )
                    iso_count[hostname] = iso_count.get(hostname, 0) + 1
                    last_iso_at[hostname] = time.monotonic()
                    if not iso.get("ok"):
                        log(
                            f"[greenfield] {hostname} iLO disc remount failed: "
                            f"{iso.get('error')}"
                        )
                if not pending:
                    break
                if time.monotonic() >= deadline_wait:
                    left_bits = []
                    for h, ip in pending.items():
                        st = baremetal_service.host_boot_state(
                            ip,
                            kube,
                            was_ready=was_ready,
                            saw_down=saw_down.get(h, False),
                        )
                        left_bits.append(f"{h} ({ip} {st})")
                    msg = "timed out waiting for Talos maintenance: " + ", ".join(
                        left_bits
                    )
                    log(f"[greenfield] {msg}")
                    wait_s_done = round(deploy_timing.elapsed(wait_t0), 1)
                    deploy_timing.log_timing(log, phase="metal", seconds=wait_s_done)
                    phases.append({"id": "metal", "seconds": wait_s_done})
                    return _stamp(
                        {
                            "ok": False,
                            "error": msg,
                            "returncode": 1,
                            "failed_at": "greenfield/maintenance",
                        }
                    )
                attempt += 1
                if attempt % 6 == 0:
                    left = ", ".join(pending)
                    log(f"[greenfield] still waiting on {left}")
                time.sleep(_POLL_S)

    if not env_is_ovh(env) and not dry_run:
        wait_s_done = (
            round(deploy_timing.elapsed(wait_t0), 1) if "wait_t0" in locals() else 0.0
        )
        deploy_timing.log_timing(log, phase="metal", seconds=wait_s_done)
        phases.append({"id": "metal", "seconds": wait_s_done})
    log("[greenfield] metal in maintenance — deploying Talos + OpenStack from hosts")
    result = deploy_service.run_deploy(
        db,
        env,
        ctx,
        log,
        dry_run=dry_run,
        skip_push=skip_push,
        timeout=timeout,
        extra_env=extra_env,
        ssh_target=ssh_target,
        remote_env=remote_env,
        settings=settings,
        from_stage="hosts",
        deadline=deadline,
        check_cancel=check_cancel,
        parallelism=workers,
    )
    if (
        isinstance(result, dict)
        and not result.get("failed_at")
        and not result.get("ok")
    ):
        result = {**result, "failed_at": "hosts"}
    return _stamp(result if isinstance(result, dict) else {"ok": bool(result)})
