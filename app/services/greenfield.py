"""Greenfield redeploy: commission, then Talos, then the pipeline.

Each inventory server PXE-boots a RAM disk that wipes fixed disks and
reports hardware. The next one-shot PXE serves Talos. Fresh maintenance is
the Talos API up, the node not Kubernetes Ready, and Talos served after
that wipe. Then ``genestack.deploy`` from ``hosts``. ``stop_after`` can
hold after the wipe report or after fresh maintenance. OVH environments
still use BYOI. Never logs BMC passwords.
"""

from __future__ import annotations

import time
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import BaremetalNode, Environment
from app.services import baremetal as baremetal_service
from app.services.bootselect import metal_ready, served_after_wipe
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
    stop_after: str = "",
) -> dict[str, Any]:
    """PXE/BYOI every inventory server, then ``run_deploy`` from hosts."""
    boot_mode = str(boot or "auto").strip().lower()
    if boot_mode not in _BOOT_MODES:
        msg = f"Unknown boot '{boot}'. Valid: auto, pxe, iso"
        log(f"[greenfield] {msg}")
        return {"ok": False, "error": msg, "returncode": 2, "dry_run": dry_run}
    stop = str(stop_after or "").strip().lower()
    if stop not in ("", "commission", "talos"):
        msg = "stop_after must be commission or talos"
        log(f"[greenfield] {msg}")
        return {"ok": False, "error": msg, "returncode": 2, "dry_run": dry_run}
    if boot_mode == "iso":
        msg = (
            "greenfield boot=iso cannot wipe disks. Use pxe or auto so the "
            "commission RAM disk runs before Talos."
        )
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
        f"[greenfield] DESTRUCTIVE: commission RAM disk wipes fixed disks, "
        f"then Talos, then OpenStack from hosts "
        f"(boot={boot_mode} dry_run={dry_run} hosts={len(servers)} "
        f"stop_after={stop or 'deploy'})"
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
                    f"[dry-run] would commission {hostname} (RAM disk wipe) then "
                    f"serve Talos and wait for fresh maintenance at "
                    f"{ip}:{baremetal_service.TALOS_API_PORT} stop_after={stop or 'deploy'}"
                )
        else:
            for hostname, node, _ip in plan:
                started = baremetal_service.begin_commission(
                    db, env, node, log=log, settings=settings, boot_now=False
                )
                if not started.get("ok"):
                    return _stamp(
                        {
                            "ok": False,
                            "error": started.get("error") or f"{hostname} commission setup failed",
                            "returncode": 2,
                            "dry_run": False,
                            "failed_at": "greenfield/commission",
                        }
                    )
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
            last_talos_at: dict[str, float] = {}
            node_by_host: dict[str, BaremetalNode] = {}
            boot_t0 = deploy_timing.now()
            host_t0 = {hostname: deploy_timing.now() for hostname, _node, _ip in plan}
            for hostname, node, _ip in plan:
                if check_cancel:
                    check_cancel()
                db.refresh(node)
                result = baremetal_service.pxe_boot(
                    db, node, dry_run=False, log=log, settings=settings
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
            waiting_for = (
                "a commission report"
                if stop == "commission"
                else "fresh Talos maintenance"
            )

            def _fail_pending() -> None:
                for hostname in pending:
                    failed = node_by_host.get(hostname)
                    if failed is None:
                        continue
                    failed.state = "failed"
                    failed.boot_stage = "failed"
                    db.add(failed)
                db.commit()

            while pending:
                if check_cancel:
                    check_cancel()
                if deadline is not None and time.monotonic() >= deadline:
                    wait_s_done = round(deploy_timing.elapsed(wait_t0), 1)
                    deploy_timing.log_timing(log, phase="metal", seconds=wait_s_done)
                    phases.append({"id": "metal", "seconds": wait_s_done})
                    _fail_pending()
                    return _stamp(
                        {
                            "ok": False,
                            "error": (
                                "greenfield deadline exceeded waiting for "
                                f"{waiting_for}"
                            ),
                            "returncode": 1,
                            "failed_at": "greenfield/maintenance",
                        }
                    )
                ready: list[str] = []
                remount: list[str] = []
                for hostname, ip in list(pending.items()):
                    node = node_by_host.get(hostname)
                    if node is not None:
                        db.refresh(node)
                        if (
                            stop != "commission"
                            and node.wiped_at is not None
                            and not served_after_wipe(
                                node.talos_served_at, node.wiped_at
                            )
                            and (
                                hostname not in last_talos_at
                                or (time.monotonic() - last_talos_at[hostname])
                                >= _ISO_COOLDOWN_S
                            )
                        ):
                            last_talos_at[hostname] = time.monotonic()
                            served = baremetal_service._serve_talos(
                                db, env, node, log=log, settings=settings
                            )
                            if not served.get("ok"):
                                log(
                                    f"[greenfield] {hostname} Talos boot failed: "
                                    f"{served.get('error')}"
                                )
                    probe_ip = ip
                    if node is not None:
                        addrs = baremetal_service.probe_addresses(node, ip)
                        probe_ip = addrs[0] if addrs else ip
                    state = baremetal_service.host_boot_state(
                        probe_ip,
                        kube,
                        was_ready=was_ready,
                        saw_down=saw_down.get(hostname, False),
                        require_fresh=stop != "commission",
                        wiped_at=getattr(node, "wiped_at", None),
                        talos_served_at=getattr(node, "talos_served_at", None),
                    )
                    if state == "installed":
                        from app.services.talos import installed_talos_guidance

                        guide = installed_talos_guidance(hostname, probe_ip)
                        msg = str(guide["error"])
                        log(f"[greenfield] {msg}")
                        return _stamp(
                            {
                                "ok": False,
                                "error": msg,
                                "user_step": guide["user_step"],
                                "returncode": 2,
                                "dry_run": False,
                                "failed_at": "greenfield/maintenance",
                            }
                        )
                    if state == "down":
                        saw_down[hostname] = True
                    stage = getattr(node, "boot_stage", "") if node is not None else ""
                    if metal_ready(stage, state, stop):
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
                        if stop == "commission":
                            ready_msg = (
                                f"{hostname} commissioned "
                                "(fixed disks wiped; Talos was not served)"
                            )
                        else:
                            ready_msg = (
                                f"{hostname} in fresh Talos maintenance at "
                                f"{ip}:{baremetal_service.TALOS_API_PORT}"
                            )
                        log(f"[greenfield] {ready_msg}")
                        try:
                            from app.services.live_events import publish_metal

                            publish_metal(
                                getattr(env, "id", None),
                                "boot",
                                ready_msg,
                                host=hostname,
                                ip=ip,
                            )
                        except Exception:
                            pass
                    elif (
                        state == "old-os"
                        and stage in ("talos", "commissioned", "fresh-maintenance")
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
                    if node is not None and stop != "commission":
                        node.state = "talos-ready"
                        node.boot_stage = "fresh-maintenance"
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
                        left_node = node_by_host.get(h)
                        st = baremetal_service.host_boot_state(
                            ip,
                            kube,
                            was_ready=was_ready,
                            saw_down=saw_down.get(h, False),
                            require_fresh=stop != "commission",
                            wiped_at=(
                                getattr(left_node, "wiped_at", None)
                                if left_node is not None
                                else None
                            ),
                            talos_served_at=(
                                getattr(left_node, "talos_served_at", None)
                                if left_node is not None
                                else None
                            ),
                        )
                        left_bits.append(f"{h} ({ip} {st})")
                    msg = f"timed out waiting for {waiting_for}: " + ", ".join(
                        left_bits
                    )
                    log(f"[greenfield] {msg}")
                    wait_s_done = round(deploy_timing.elapsed(wait_t0), 1)
                    deploy_timing.log_timing(log, phase="metal", seconds=wait_s_done)
                    phases.append({"id": "metal", "seconds": wait_s_done})
                    _fail_pending()
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
    if stop in ("commission", "talos"):
        log(
            f"[greenfield] stopped after {stop} — OpenStack deploy was not started"
        )
        return _stamp(
            {
                "ok": True,
                "dry_run": dry_run,
                "stopped_after": stop,
                "stages_completed": [stop],
            }
        )
    log("[greenfield] metal in fresh maintenance — deploying Talos + OpenStack from hosts")
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
