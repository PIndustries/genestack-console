"""One-click deploy orchestration (genestack.deploy).

Takes an environment from config document to deployed cloud in one job:

  Phase 1 (unless skip_push): render the current config document and push the
      rendered files to the env's config dir via envconfig.push_rendered.
  Phase 2: run the pipeline stages in PIPELINE_STAGES order (starting at
      ``from_stage`` when given), each item as ``bash <script>`` through the
      genestack bridge — via the env's connected agent when one is available,
      else over ssh on the deploy host, else locally (pick_executor). Stops at the first non-zero
      returncode. When the config doc has a ``components:`` section, stage
      items whose service is explicitly set to ``false`` are skipped (see
      service_registry.filter_stage_items). When ``parallelism`` > 1 and the
      run is not a dry run, the OpenStack services stage (the stage that
      installs keystone) runs keystone first and fans its remaining items out
      across that many workers; all other stages stay sequential. After the
      ``hosts`` stage
      succeeds, the kubeconfig is fetched from the first control-plane node
      when it is not already present (non-fatal).
  Provider branching: when the config doc's ``provider`` is ``talos``, the
      ``hosts`` stage runs the talos bootstrap flow (app/services/talos.py —
      talosctl gen config / apply-config / endpoints / bootstrap / kubeconfig)
      INSTEAD of ``bash bin/setup-hosts.sh``; the kubespray host-setup +
      cluster.yml portion that script runs is skipped entirely. The talos
      flow fetches the kubeconfig itself, so the post-hosts auto-fetch is a
      no-op via its existing file-exists guard.
  Phase 3 (non-dry-run only): run bin/setup-openstack-rc.sh so the deploy
      finishes with genestack's standard credentials in place. Non-fatal —
      a failure logs a warning and sets ``credentials_warning`` on the result.

Kept out of job_runner so the dispatcher stays thin; the handler only wires
ctx/params and writes the audit entry.
"""

from __future__ import annotations

import concurrent.futures
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable

from sqlalchemy.orm import Session

from app.config import Settings
from app.models import Environment, OvhAccount
from app.services import envconfig as envconfig_service
from app.services import genestack_bridge as bridge
from app.services import secret_lease
from app.services import service_registry
from app.services import talos as talos_service
from app.services.crypto import decrypt_secret
from app.services.envcontext import EnvContext
from app.services.executors import pick_executor
from app.services.job_runner import JobDeadlineExceededError, clamp_deadline_timeout
from app.services import baremetal as baremetal_service
from app.services.ovh import (
    DEFAULT_BYOI_OS,
    OvhClient,
    OvhError,
    byoi_customizations,
    classify_install_status,
    env_is_ovh,
    inventory_indexes,
    resolve_ovh_service_name,
    server_is_explicit_ovh,
    server_may_inherit_ovh,
)
from app.services.service_registry import PIPELINE_STAGES, stage_required
from app.services import deploy_timing

LogFn = Callable[[str], None]

# OVH BYOI install can take a long time on dedicated metal; the job catalog
# timeout is the hard cap. These are the inner poll cadences.
_OVH_INSTALL_POLL_S = 15.0
_OVH_TALOS_POLL_S = 10.0

# Kubeconfig auto-fetch: genestack's kubespray writes admin.conf only on the
# first control-plane node; the deploy host copy lands under the config dir.
REMOTE_ADMIN_CONF = "/etc/kubernetes/admin.conf"
KUBECONFIG_FALLBACK_RELPATH = "inventory/artifacts/admin.conf"
_SERVER_LINE_RE = re.compile(r"^(\s*server:\s*)\S+.*$", re.MULTILINE)

# Parallel OpenStack services: the stage that installs keystone fans its
# remaining items out across a thread pool after keystone completes.
# ``parallelism`` clamps to 1..16; 1 (the default) keeps the historical
# sequential loop so logs/behavior stay byte-identical.
_PARALLEL_MIN = 1
_PARALLEL_MAX = 16
_PARALLEL_DEFAULT = 1


def _effective_parallelism(parallelism: int | None) -> int:
    if parallelism is None:
        return _PARALLEL_DEFAULT
    try:
        value = int(parallelism)
    except (TypeError, ValueError):
        return _PARALLEL_DEFAULT
    return max(_PARALLEL_MIN, min(_PARALLEL_MAX, value))


def _is_services_stage(stage: dict[str, Any]) -> bool:
    """The OpenStack services stage is the one that installs keystone."""
    return any(item.get("name") == "keystone" for item in stage["items"])


def _run_services_parallel(
    stage: dict[str, Any],
    items: list[dict[str, str]],
    *,
    parallelism: int,
    ctx: EnvContext,
    log: LogFn,
    dry_run: bool,
    timeout: int,
    deadline: float | None,
    extra_env: dict[str, str],
    ssh_target: str | None,
    remote_env: dict[str, str],
    agent_env_id: str | None,
    check_cancel: Callable[[], None] | None,
    stages_completed: int,
    stages_total: int,
    version: int | None,
    from_stage: str | None,
) -> dict[str, Any] | None:
    """Run the OpenStack services stage: keystone first, then the rest in parallel.

    Returns the same failure dict the sequential loop returns for the first
    failing item (in item order), or None when every item succeeded. On the
    first failure the queued items are cancelled; already-running items
    finish (their log lines may interleave, serialized under a lock).
    """
    log_lock = threading.Lock()

    def tlog(msg: str) -> None:
        with log_lock:
            log(msg)

    def run_item(item: dict[str, str]) -> Any:
        item_t0 = deploy_timing.now()
        if agent_env_id is not None:
            # The DB-backed agent relay drives the shared session, which is
            # not safe to use concurrently from pool workers: serialize the
            # whole command (log goes through the raw fn, lock already held).
            with log_lock:
                result = bridge.run_command(
                    ["bash", item["script"]],
                    cwd=ctx.genestack_root,
                    timeout=clamp_deadline_timeout(timeout, deadline),
                    dry_run=dry_run,
                    extra_env=extra_env,
                    ssh_target=ssh_target,
                    remote_env=remote_env,
                    agent_env_id=agent_env_id,
                    log=log,
                )
        else:
            result = bridge.run_command(
                ["bash", item["script"]],
                cwd=ctx.genestack_root,
                timeout=clamp_deadline_timeout(timeout, deadline),
                dry_run=dry_run,
                extra_env=extra_env,
                ssh_target=ssh_target,
                remote_env=remote_env,
                agent_env_id=agent_env_id,
                log=tlog,
            )
        deploy_timing.log_timing(
            tlog,
            stage=stage["id"],
            item=item["name"],
            seconds=deploy_timing.elapsed(item_t0),
        )
        return result.get("returncode")

    def failure_dict(failed_item: dict[str, str], rc: Any) -> dict[str, Any]:
        return {
            "ok": False,
            "error": (
                f"deploy failed at stage '{stage['id']}' "
                f"item '{failed_item['name']}' (rc={rc})"
            ),
            "returncode": rc,
            "stages_completed": stages_completed,
            "stages_total": stages_total,
            "failed_at": f"{stage['id']}/{failed_item['name']}",
            "dry_run": dry_run,
            "version": version,
            "from_stage": from_stage,
        }

    rest = list(items)
    keystone = next((it for it in rest if it["name"] == "keystone"), None)
    if keystone is not None:
        rest.remove(keystone)
        log(
            f"[deploy] stage {stage['id']}: keystone first, then "
            f"{len(rest)} item(s) across {parallelism} worker(s)"
        )
        if check_cancel is not None:
            check_cancel()
        rc = run_item(keystone)
        if rc not in (0, None) and not dry_run:
            log(
                f"[deploy] FAILED at {stage['id']}/keystone rc={rc} — stopping pipeline"
            )
            return failure_dict(keystone, rc)
    elif rest:
        log(
            f"[deploy] stage {stage['id']}: running {len(rest)} item(s) "
            f"across {parallelism} worker(s)"
        )

    if not rest:
        return None

    order = {id(item): i for i, item in enumerate(rest)}
    failures: list[tuple[int, dict[str, str], Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=parallelism) as pool:
        futures = {pool.submit(run_item, item): item for item in rest}
        for future in concurrent.futures.as_completed(futures):
            item = futures[future]
            try:
                rc = future.result()
            except concurrent.futures.CancelledError:
                continue
            if check_cancel is not None:
                check_cancel()
            if rc not in (0, None) and not dry_run:
                failures.append((order[id(item)], item, rc))
                tlog(
                    f"[deploy] FAILED at {stage['id']}/{item['name']} rc={rc} "
                    "— stopping pipeline"
                )
                pool.shutdown(wait=False, cancel_futures=True)
    if failures:
        _, first_item, first_rc = min(failures, key=lambda f: f[0])
        return failure_dict(first_item, first_rc)
    return None


def _control_plane_target(
    doc: dict[str, Any] | None, env: Environment
) -> tuple[str, str] | None:
    """(ip-or-hostname, ssh_target) of the first k8s_control_plane server in the doc."""
    servers = (doc or {}).get("servers")
    if not isinstance(servers, dict):
        return None
    for hostname, entry in servers.items():
        if not isinstance(entry, dict):
            continue
        roles = [str(role).lower() for role in entry.get("roles") or []]
        if "k8s_control_plane" not in roles:
            continue
        host = str(entry.get("ip") or hostname)
        user = str(entry.get("ssh_user") or env.deployer_ssh_user or "").strip()
        return host, f"{user}@{host}" if user else host
    return None


def _fetch_kubeconfig(
    doc: dict[str, Any] | None,
    env: Environment,
    ctx: EnvContext,
    log: LogFn,
    *,
    dry_run: bool,
    timeout: int,
    db: Session | None = None,
) -> None:
    """Fetch admin.conf from the first control-plane node after the hosts stage.

    The ``server:`` line is rewritten to the node's own address (admin.conf
    ships pointing at localhost) and the file is written 0600. Non-fatal by
    design: any failure logs a warning and the deploy continues.

    The write is local (this process). When it creates the file, the text is
    encrypted onto the environment when ``db`` is set, and the path is
    recorded so the job lease removes that file. ``log=None`` on the cat so
    admin.conf is not echoed. A file that already existed is not recorded.
    """
    if env.kubeconfig_data:
        log("[kubeconfig] env has a stored kubeconfig — skipping fetch")
        return
    if env.kubeconfig_path:
        target = Path(env.kubeconfig_path).expanduser()
    elif ctx.config_dir is not None:
        target = ctx.config_dir / KUBECONFIG_FALLBACK_RELPATH
    else:
        log(
            "[kubeconfig] no kubeconfig path (env.kubeconfig_path or config dir) — skipping fetch"
        )
        return
    cp = _control_plane_target(doc, env)
    if cp is None:
        log("[kubeconfig] no k8s_control_plane server in config doc — skipping fetch")
        return
    host, cp_target = cp
    if dry_run:
        log(f"[dry-run] would fetch {REMOTE_ADMIN_CONF} from {cp_target} -> {target}")
        return
    if target.exists():
        log(f"[kubeconfig] {target} already exists — skipping fetch")
        return
    try:
        # log=None: admin.conf holds cluster credentials — never echo it
        result = bridge.run_command(
            ["cat", REMOTE_ADMIN_CONF],
            timeout=min(timeout, 60),
            dry_run=False,
            ssh_target=cp_target,
            log=None,
        )
        content = result.get("stdout") or ""
        if result.get("returncode") != 0 or not content.strip():
            log(
                f"[deploy] WARNING kubeconfig fetch from {cp_target} failed "
                f"(rc={result.get('returncode')})"
            )
            return
        content = _SERVER_LINE_RE.sub(
            lambda m: f"{m.group(1)}https://{host}:6443", content
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        os.chmod(target, 0o600)
        try:
            secret_lease.store_kubeconfig(env, content, db)
        except Exception as exc:  # noqa: BLE001 — fetch stays non-fatal
            log(
                "[kubeconfig] WARNING could not store kubeconfig on the environment: "
                f"{type(exc).__name__}"
            )
        # Local write: the lease unlinks this path (not an ssh rm). Inside a
        # job that waits until the job ends; with no job lease it is removed
        # before this function returns.
        secret_lease.remember(
            target,
            config_dir=ctx.config_dir,
            log_fn=log,
        )
        log(
            f"[kubeconfig] wrote {target} from {cp_target} (server=https://{host}:6443)"
        )
    except Exception as exc:  # noqa: BLE001
        log(f"[deploy] WARNING kubeconfig fetch failed: {exc}")


def run_deploy(
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
    from_stage: str | None = None,
    until_stage: str | None = None,
    include_testing: bool = False,
    deadline: float | None = None,
    check_cancel: Callable[[], None] | None = None,
    parallelism: int | None = None,
) -> dict[str, Any]:
    """Push the config document, then run the genestack pipeline.

    ``deadline`` (monotonic) caps the whole job: each pipeline item gets the
    min of the op timeout and the remaining budget, and the loop stops with a
    deadline error once it passes. ``check_cancel`` (when given) is polled
    between items so an operator cancel stops the deploy at the next item.
    """
    version: int | None = None
    current = envconfig_service.get_current(db, env)
    doc: dict[str, Any] | None = None
    if current is not None:
        doc, row = current
        version = row.version

    # Executor preference: agent (connected) -> ssh deploy host -> local.
    executor = pick_executor(env, ctx, settings)
    agent_env_id = executor.agent_env_id
    if executor.kind == "agent":
        log("[deploy] executor: via agent — pipeline stages run on the agent host")
    elif executor.kind == "ssh":
        log(f"[deploy] executor: ssh deploy host {executor.ssh_target}")

    stage_ids = [stage["id"] for stage in PIPELINE_STAGES]
    start_index = 0
    if from_stage:
        if from_stage not in stage_ids:
            valid = ", ".join(stage_ids)
            msg = f"Unknown from_stage '{from_stage}'. Valid stages: {valid}"
            log(f"[deploy] {msg}")
            return {
                "ok": False,
                "error": msg,
                "returncode": 2,
                "dry_run": dry_run,
                "version": version,
                "from_stage": from_stage,
            }
        start_index = stage_ids.index(from_stage)
    until_id = str(until_stage or "").strip() or None
    if until_id:
        if until_id not in stage_ids:
            valid = ", ".join(stage_ids)
            msg = f"Unknown until_stage '{until_id}'. Valid stages: {valid}"
            log(f"[deploy] {msg}")
            return {
                "ok": False,
                "error": msg,
                "returncode": 2,
                "dry_run": dry_run,
                "version": version,
                "from_stage": from_stage,
                "until_stage": until_id,
            }
        until_index = stage_ids.index(until_id)
        if until_index < start_index:
            msg = (
                f"until_stage '{until_id}' is before from_stage "
                f"'{from_stage or stage_ids[0]}'"
            )
            log(f"[deploy] {msg}")
            return {
                "ok": False,
                "error": msg,
                "returncode": 2,
                "dry_run": dry_run,
                "version": version,
                "from_stage": from_stage,
                "until_stage": until_id,
            }
    run_tests = (
        bool(include_testing) or from_stage == "testing" or until_id == "testing"
    )

    if not skip_push:
        if current is None:
            msg = (
                "no config document yet — PUT "
                f"/api/v1/environments/{env.id}/config first (or pass skip_push)"
            )
            log(f"[deploy] {msg}")
            return {"ok": False, "error": msg, "returncode": 2, "dry_run": dry_run}
        if ctx.config_dir is None:
            msg = "Environment has no genestack_config_dir — nowhere to push rendered files"
            log(f"[deploy] {msg}")
            return {
                "ok": False,
                "error": msg,
                "returncode": 2,
                "dry_run": dry_run,
                "version": version,
            }
        log(f"[deploy] phase 1: push config version={version} dry_run={dry_run}")
        files = envconfig_service.render_to_files(doc, env, settings)
        pushed = envconfig_service.push_rendered(files, ctx, log, dry_run)
        log(
            f"[deploy] pushed config version={version} "
            f"files={pushed['count']} bytes={pushed['bytes']}"
        )
    else:
        log("[deploy] skip_push=true — skipping config push phase")
        envconfig_service.sync_chart_versions_from_repo(ctx, log, dry_run=dry_run)

    # bootstrap.sh creates kustomize/<svc>/overlay for every base-kustomize
    # service. host_prepare skips bootstrap on an existing /etc/genestack, so
    # newly split plays (redis-operator) have no overlay. Always reconcile
    # on a live run (never on dry-run — tests assert the config dir is empty).
    if not dry_run:
        created = envconfig_service.ensure_kustomize_layout(
            ctx.config_dir, ctx.genestack_root, log
        )
        if created:
            log(f"[deploy] kustomize layout: {created} missing overlay(s) created")

    components = (doc or {}).get("components")
    if not isinstance(components, dict):
        components = None

    # provider=talos: the hosts stage brings up Kubernetes with talosctl
    # (services/talos.py) instead of kubespray's bin/setup-hosts.sh.
    provider = (doc or {}).get("provider")
    talos_mode = isinstance(provider, str) and provider.strip().lower() == "talos"
    if talos_mode:
        log("[deploy] provider=talos — hosts stage will use the talosctl flow")

    # OVH + Talos: the metal is not Talos until BYOI finishes. If the hosts
    # stage will run, provision (or confirm :50000) before talosctl.
    if talos_mode and env_is_ovh(env) and start_index == 0:
        provisioned = _ensure_ovh_talos_ready(
            db,
            env,
            doc or {},
            log,
            dry_run=dry_run,
            deadline=deadline,
            check_cancel=check_cancel,
        )
        if not provisioned.get("ok"):
            return {
                "ok": False,
                "error": provisioned.get("error") or "OVH Talos provision failed",
                "returncode": provisioned.get("returncode") or 2,
                "stages_completed": 0,
                "stages_total": len(PIPELINE_STAGES),
                "failed_at": "hosts/byoi",
                "dry_run": dry_run,
                "version": version,
                "from_stage": from_stage,
            }

    stages_total = len(PIPELINE_STAGES)
    stages_completed = 0
    t0 = deploy_timing.now()
    timings: list[dict[str, Any]] = []
    optional_failed: list[str] = []

    def finish(payload: dict[str, Any]) -> dict[str, Any]:
        total = round(deploy_timing.elapsed(t0), 1)
        out = dict(payload)
        out["timings"] = {"total_s": total, "stages": list(timings)}
        if optional_failed:
            out["optional_failed"] = list(optional_failed)
        deploy_timing.log_timing(log, phase="total", seconds=total)
        if timings:
            score = " ".join(
                f"{row.get('id')}={float(row.get('seconds') or 0):.0f}s"
                for row in timings
            )
            log(f"[timing] scoreboard {score} total={total:.0f}s")
        return out

    _parallelism = _effective_parallelism(parallelism)
    if _parallelism > _PARALLEL_MIN:
        log(f"[deploy] parallelism={_parallelism} for the OpenStack services stage")
    if from_stage:
        log(
            f"[deploy] from_stage={from_stage} — "
            f"pipeline starts at stage {start_index + 1}/{stages_total}"
        )
    if until_id:
        log(f"[deploy] until_stage={until_id} — stopping at this control point")
    if not run_tests:
        log("[deploy] testing skipped — Tempest is its own control point")
    log(f"[deploy] phase 2: pipeline stages={stages_total} dry_run={dry_run}")
    for index, stage in enumerate(PIPELINE_STAGES, start=1):
        if index <= start_index:
            continue
        if stage["id"] == "testing" and not run_tests:
            log(
                f"[deploy] === stage {index}/{stages_total}: "
                f"{stage['id']} ({stage['name']}) — skipped "
                "(control point: run Tempest separately)"
            )
            continue
        items = service_registry.filter_stage_items(stage, components, log)
        if not items:
            stages_completed += 1
            log(
                f"[deploy] === stage {index}/{stages_total}: "
                f"{stage['id']} ({stage['name']}) ==="
            )
            log(
                f"[deploy] stage {stage['id']}: all items disabled in config doc — "
                f"marked complete ({stages_completed}/{stages_total})"
            )
            deploy_timing.log_timing(log, stage=stage["id"], seconds=0)
            timings.append({"id": stage["id"], "seconds": 0, "items": []})
            continue
        log(
            f"[deploy] === stage {index}/{stages_total}: "
            f"{stage['id']} ({stage['name']}) — {len(items)} item(s) ==="
        )
        stage_t0 = deploy_timing.now()
        stage_items: list[dict[str, Any]] = []
        if stage["id"] == "hosts" and talos_mode:
            log(
                "[deploy] stage hosts: provider=talos — using talosctl flow "
                "(bash bin/setup-hosts.sh skipped: kubespray host-setup + "
                "cluster.yml do not run)"
            )
            try:
                talos_result = talos_service.run_talos_bootstrap(
                    doc,
                    env,
                    log,
                    dry_run=dry_run,
                    timeout=timeout,
                    extra_env=extra_env,
                    ssh_target=ssh_target,
                    remote_env=remote_env,
                    agent_env_id=agent_env_id,
                    db=db,
                )
            except envconfig_service.ConfigValidationError as exc:
                log(f"[deploy] FAILED at hosts/talos — {exc}")
                deploy_timing.log_timing(
                    log,
                    stage="hosts",
                    item="talos",
                    seconds=deploy_timing.elapsed(stage_t0),
                )
                return finish(
                    {
                        "ok": False,
                        "error": f"deploy failed at stage 'hosts' (talos): {exc}",
                        "returncode": 2,
                        "stages_completed": stages_completed,
                        "stages_total": stages_total,
                        "failed_at": "hosts/talos",
                        "dry_run": dry_run,
                        "version": version,
                        "from_stage": from_stage,
                    }
                )
            if not talos_result.get("ok"):
                failed_phase = talos_result.get("failed_phase")
                rc = talos_result.get("returncode")
                failed_at = f"hosts/talos-{failed_phase}"
                plain = str(talos_result.get("error") or "").strip()
                if failed_phase == "reach" and plain:
                    log(f"[deploy] FAILED at {failed_at} — {plain}")
                else:
                    log(f"[deploy] FAILED at {failed_at} rc={rc} — stopping pipeline")
                deploy_timing.log_timing(
                    log,
                    stage="hosts",
                    item="talos",
                    seconds=deploy_timing.elapsed(stage_t0),
                )
                reach = failed_phase == "reach" and plain
                payload: dict[str, Any] = {
                    "ok": False,
                    "error": (
                        f"deploy failed at stage 'hosts' (talos): {plain}"
                        if reach
                        else (
                            f"deploy failed at stage 'hosts' "
                            f"talos phase '{failed_phase}' (rc={rc})"
                        )
                    ),
                    "returncode": rc,
                    "stages_completed": stages_completed,
                    "stages_total": stages_total,
                    "failed_at": failed_at,
                    "dry_run": dry_run,
                    "version": version,
                    "from_stage": from_stage,
                }
                step = talos_result.get("user_step")
                if reach and isinstance(step, dict):
                    payload["user_step"] = step
                return finish(payload)
            hosts_s = round(deploy_timing.elapsed(stage_t0), 1)
            deploy_timing.log_timing(log, stage="hosts", item="talos", seconds=hosts_s)
            deploy_timing.log_timing(log, stage="hosts", seconds=hosts_s)
            timings.append(
                {
                    "id": "hosts",
                    "seconds": hosts_s,
                    "items": [{"name": "talos", "seconds": hosts_s}],
                }
            )
            stages_completed += 1
            log(f"[deploy] stage hosts complete ({stages_completed}/{stages_total})")
            # The talos flow fetched the kubeconfig itself, so this is a no-op
            # via the existing file-exists guard.
            _fetch_kubeconfig(
                doc, env, ctx, log, dry_run=dry_run, timeout=timeout, db=db
            )
            if until_id and stage["id"] == until_id:
                log(f"[deploy] until_stage={until_id} — stopped at this control point")
                break
            continue
        if not dry_run and _is_services_stage(stage) and _parallelism > _PARALLEL_MIN:
            failure = _run_services_parallel(
                stage,
                items,
                parallelism=_parallelism,
                ctx=ctx,
                log=log,
                dry_run=dry_run,
                timeout=timeout,
                deadline=deadline,
                extra_env=extra_env,
                ssh_target=ssh_target,
                remote_env=remote_env,
                agent_env_id=agent_env_id,
                check_cancel=check_cancel,
                stages_completed=stages_completed,
                stages_total=stages_total,
                version=version,
                from_stage=from_stage,
            )
            if failure is not None:
                deploy_timing.log_timing(
                    log, stage=stage["id"], seconds=deploy_timing.elapsed(stage_t0)
                )
                return finish(failure)
        else:
            for item in items:
                if check_cancel is not None:
                    check_cancel()
                item_t0 = deploy_timing.now()
                result = bridge.run_command(
                    ["bash", item["script"]],
                    cwd=ctx.genestack_root,
                    timeout=clamp_deadline_timeout(timeout, deadline),
                    dry_run=dry_run,
                    extra_env=extra_env,
                    ssh_target=ssh_target,
                    remote_env=remote_env,
                    agent_env_id=agent_env_id,
                    log=log,
                )
                item_s = round(deploy_timing.elapsed(item_t0), 1)
                deploy_timing.log_timing(
                    log, stage=stage["id"], item=item["name"], seconds=item_s
                )
                stage_items.append({"name": item["name"], "seconds": item_s})
                rc = result.get("returncode")
                if rc not in (0, None) and not result.get("dry_run"):
                    failed_at = f"{stage['id']}/{item['name']}"
                    if not stage_required(stage):
                        log(
                            f"[deploy] WARNING optional {failed_at} rc={rc} — "
                            "continuing (optional control point)"
                        )
                        optional_failed.append(failed_at)
                        continue
                    log(f"[deploy] FAILED at {failed_at} rc={rc} — stopping pipeline")
                    deploy_timing.log_timing(
                        log, stage=stage["id"], seconds=deploy_timing.elapsed(stage_t0)
                    )
                    return finish(
                        {
                            "ok": False,
                            "error": (
                                f"deploy failed at stage '{stage['id']}' "
                                f"item '{item['name']}' (rc={rc})"
                            ),
                            "returncode": rc,
                            "stages_completed": stages_completed,
                            "stages_total": stages_total,
                            "failed_at": failed_at,
                            "dry_run": dry_run,
                            "version": version,
                            "from_stage": from_stage,
                            "until_stage": until_id,
                        }
                    )
        stage_s = round(deploy_timing.elapsed(stage_t0), 1)
        deploy_timing.log_timing(log, stage=stage["id"], seconds=stage_s)
        timings.append({"id": stage["id"], "seconds": stage_s, "items": stage_items})
        stages_completed += 1
        log(
            f"[deploy] stage {stage['id']} complete ({stages_completed}/{stages_total})"
        )
        if stage["id"] == "hosts":
            _fetch_kubeconfig(
                doc, env, ctx, log, dry_run=dry_run, timeout=timeout, db=db
            )
        if until_id and stage["id"] == until_id:
            log(f"[deploy] until_stage={until_id} — stopped at this control point")
            break

    # Final phase: genestack's standard credentials setup (writes
    # ~/.config/openstack/clouds.yaml from the in-cluster keystone secret).
    # Non-fatal: a failure here never fails an otherwise successful deploy.
    credentials_warning: str | None = None
    log("[deploy] phase 3: credentials — setup openstack rc")
    if dry_run:
        log("$ bash bin/setup-openstack-rc.sh")
        log("[dry-run] credentials setup skipped")
    else:
        try:
            cred_timeout = clamp_deadline_timeout(timeout, deadline)
        except JobDeadlineExceededError:
            # Non-fatal by design: an expired deadline skips credentials
            # setup rather than failing an otherwise successful deploy.
            credentials_warning = (
                "credentials setup skipped — job deadline exceeded; "
                "run bin/setup-openstack-rc.sh manually"
            )
            log(f"[deploy] WARNING {credentials_warning}")
            cred_timeout = None
        if cred_timeout is not None:
            cred = bridge.run_command(
                ["bash", "bin/setup-openstack-rc.sh"],
                cwd=ctx.genestack_root,
                timeout=cred_timeout,
                dry_run=dry_run,
                extra_env=extra_env,
                ssh_target=ssh_target,
                remote_env=remote_env,
                agent_env_id=agent_env_id,
                log=log,
            )
            rc = cred.get("returncode")
            if rc in (0, None):
                log("credentials: wrote ~/.config/openstack/clouds.yaml on deploy host")
            else:
                credentials_warning = f"credentials setup failed (rc={rc}) — run bin/setup-openstack-rc.sh manually"
                log(f"[deploy] WARNING {credentials_warning}")

    return finish(
        {
            "ok": True,
            "returncode": 0,
            "stages_completed": stages_completed,
            "stages_total": stages_total,
            "failed_at": None,
            "dry_run": dry_run,
            "version": version,
            "from_stage": from_stage,
            "until_stage": until_id,
            "credentials_warning": credentials_warning,
            "message": f"deploy complete: {stages_completed}/{stages_total} stages",
        }
    )


# ---------------------------------------------------------------------------
# OVH BYOI reinstall (ovh.byoi.reinstall)
# ---------------------------------------------------------------------------


def _talos_image_url(doc: dict[str, Any] | None) -> str:
    talos = (doc or {}).get("talos") if isinstance(doc, dict) else None
    if isinstance(talos, dict):
        url = str(talos.get("image_url") or "").strip()
        if url:
            return url
    from app.services.talos import DEFAULT_TALOS_IMAGE_URL

    return DEFAULT_TALOS_IMAGE_URL


def _server_ip(doc: dict[str, Any], hostname: str) -> str:
    ips = _server_probe_ips(doc, hostname)
    return ips[0] if ips else ""


def _server_probe_ips(doc: dict[str, Any], hostname: str) -> list[str]:
    """Private NIC first, then cluster ip, then public (maintenance fallback)."""
    servers = doc.get("servers") if isinstance(doc, dict) else None
    if not isinstance(servers, dict):
        return []
    entry = servers.get(hostname) or {}
    if not isinstance(entry, dict):
        return []
    ordered: list[str] = []
    for key in ("private_ip", "ip", "public_ip"):
        addr = str(entry.get(key) or "").split("/", 1)[0].strip()
        if addr and addr not in ordered:
            ordered.append(addr)
    return ordered


def _wait_ovh_install(
    client: OvhClient,
    service_name: str,
    log: LogFn,
    *,
    poll_interval: float,
    deadline: float | None,
    check_cancel: Callable[[], None] | None,
    sleep_fn: Callable[[float], None],
) -> str | None:
    """Poll ``install/status`` until done/error/timeout. None = success."""
    while True:
        if check_cancel is not None:
            check_cancel()
        if deadline is not None and time.monotonic() >= deadline:
            return f"timed out waiting for OVH install on {service_name}"
        try:
            payload = client.install_status(service_name)
        except OvhError as exc:
            if exc.status_code == 404:
                log(f"[ovh] {service_name} install/status 404 — treating as complete")
                return None
            return f"install status failed for {service_name}: {exc}"
        state = classify_install_status(payload)
        log(f"[ovh] {service_name} install {state}")
        if state == "done":
            return None
        if state == "error":
            return f"OVH install failed on {service_name}: {payload}"
        sleep_fn(poll_interval)


def _wait_talos_api(
    ip: str | list[str],
    log: LogFn,
    *,
    poll_interval: float,
    deadline: float | None,
    check_cancel: Callable[[], None] | None,
    sleep_fn: Callable[[float], None],
) -> str | None:
    """Poll the Talos maintenance API until :50000 accepts TLS. None = ready.

    Tries private then public so BYOI wait works before the host firewall
    is applied, and still works if only one NIC answers.
    """
    ips = [ip] if isinstance(ip, str) else list(ip or [])
    ips = [str(item).strip() for item in ips if str(item).strip()]
    if not ips:
        return "no IP to probe for Talos API"
    shown = ",".join(ips)
    while True:
        if check_cancel is not None:
            check_cancel()
        if deadline is not None and time.monotonic() >= deadline:
            return f"timed out waiting for Talos API at {shown}:50000"
        for candidate in ips:
            if baremetal_service.talos_api_ready(candidate, log=log):
                log(f"[ovh] talos API ready at {candidate}:50000")
                return None
        log(f"[ovh] waiting for Talos API at {shown}:50000")
        sleep_fn(poll_interval)


def _ensure_ovh_talos_ready(
    db: Session,
    env: Environment,
    doc: dict[str, Any],
    log: LogFn,
    *,
    dry_run: bool,
    deadline: float | None,
    check_cancel: Callable[[], None] | None,
) -> dict[str, Any]:
    """Before talosctl: BYOI any OVH node that is not yet answering on :50000."""
    image_url = _talos_image_url(doc)
    if not image_url:
        msg = (
            "talos.image_url is required to provision OVH dedicated servers — "
            "set a Talos factory image URL (qcow2 or raw) on the Deployment card"
        )
        log(f"[deploy] {msg}")
        return {"ok": False, "error": msg, "returncode": 2}

    account = db.get(OvhAccount, env.ovh_account_id) if env.ovh_account_id else None
    consumer_key = decrypt_secret(account.consumer_key_encrypted) if account else ""
    if account is None or not consumer_key:
        msg = "OVH account is bound but has no approved consumer key — re-run Connect"
        log(f"[deploy] {msg}")
        return {"ok": False, "error": msg, "returncode": 2}

    client = OvhClient(
        endpoint=account.endpoint,
        app_key=account.app_key,
        app_secret=decrypt_secret(account.app_secret_encrypted) or "",
        consumer_key=consumer_key,
    )
    try:
        targets = _ovh_resolve_service_names(
            client, doc, hostname_filter=None, log=log, ovh_env=True
        )
    finally:
        client.close()

    not_ready: list[str] = []
    for target in targets:
        if target.get("error"):
            continue
        hostname = target["hostname"]
        ready_ip = None
        for candidate in _server_probe_ips(doc, hostname):
            if baremetal_service.talos_api_ready(candidate, log=log):
                ready_ip = candidate
                break
        if ready_ip:
            log(f"[deploy] {hostname} ({ready_ip}) already Talos-ready")
            continue
        not_ready.append(hostname)

    if not not_ready:
        log("[deploy] all OVH nodes answer on :50000 — skipping BYOI")
        return {"ok": True}

    log(
        f"[deploy] OVH Talos provision needed on {len(not_ready)} node(s): "
        + ", ".join(not_ready)
    )
    if dry_run:
        log(
            f"[dry-run] would BYOI reinstall {len(not_ready)} server(s) "
            f"operatingSystem={DEFAULT_BYOI_OS} imageURL={image_url}"
        )
        return {"ok": True, "dry_run": True}

    result = ovh_byoi_reinstall_for_env(
        db,
        env,
        operating_system=DEFAULT_BYOI_OS,
        server_hostnames=not_ready,
        image_url=image_url,
        dry_run=False,
        wait=True,
        log=log,
        deadline=deadline,
        check_cancel=check_cancel,
    )
    if not result.get("ok"):
        return {
            "ok": False,
            "error": result.get("error") or "OVH BYOI reinstall failed",
            "returncode": 2,
        }
    return {"ok": True, "byoi": result}


def _ovh_resolve_service_names(
    client: OvhClient,
    doc: dict[str, Any],
    *,
    hostname_filter: frozenset[str] | None,
    log: LogFn,
    ovh_env: bool = False,
) -> list[dict[str, Any]]:
    """Map the env's OVH-owned servers to OVH service names.

    Explicit ``source: ovh`` rows are always selected (unresolvable → error).
    In an OVH-bound environment, ``static`` rows that are not already
    bare-metal or terraform match live inventory by IP or hostname, so
    a fleet imported before source tagging still BYOI-reinstalls. Unmatched
    static rows are skipped, not failed — they are not OVH boxes.

    Returns one entry per selected server (doc order):
    ``{"hostname", "service_name", "error"}``.
    """
    servers = doc.get("servers") or {}
    if not isinstance(servers, dict):
        return []
    explicit: list[tuple[str, str | None]] = []
    inherited: list[tuple[str, str | None]] = []
    for hostname, entry in servers.items():
        if not isinstance(entry, dict):
            continue
        if hostname_filter is not None and str(hostname) not in hostname_filter:
            continue
        sn = str(entry.get("service_name") or "").strip() or None
        if server_is_explicit_ovh(entry):
            explicit.append((str(hostname), sn))
        elif ovh_env and server_may_inherit_ovh(entry):
            inherited.append((str(hostname), sn))
    if not explicit and not inherited:
        return []

    need_sweep = any(sn is None for _, sn in explicit) or bool(inherited)
    by_ip: dict[str, str] = {}
    by_host: dict[str, str] = {}
    sweep_failed = False
    if need_sweep:
        try:
            inventory = client.list_dedicated_servers()
        except OvhError as exc:
            log(
                f"[ovh] inventory sweep failed, cannot resolve "
                f"{sum(1 for _, sn in explicit + inherited if sn is None)} server(s): {exc}"
            )
            sweep_failed = True
        else:
            by_ip, by_host = inventory_indexes(inventory)

    out: list[dict[str, Any]] = []

    def _resolve_one(hostname: str, sn: str | None, *, required: bool) -> None:
        if sn is not None:
            out.append({"hostname": hostname, "service_name": sn, "error": None})
            return
        if sweep_failed:
            if required:
                out.append(
                    {
                        "hostname": hostname,
                        "service_name": None,
                        "error": "inventory sweep failed — cannot resolve service name",
                    }
                )
            return
        entry = servers.get(hostname) or {}
        resolved = resolve_ovh_service_name(hostname, entry, by_ip, by_host)
        if resolved:
            out.append({"hostname": hostname, "service_name": resolved, "error": None})
            log(f"[ovh] resolved {hostname} -> {resolved}")
            return
        if required:
            ip = str(entry.get("ip") or "").strip()
            out.append(
                {
                    "hostname": hostname,
                    "service_name": None,
                    "error": f"no OVH server matches ip={ip or '-'} hostname='{hostname}'",
                }
            )
            log(
                f"[ovh] could not resolve OVH service name for '{hostname}' (ip={ip or '-'})"
            )

    for hostname, sn in explicit:
        _resolve_one(hostname, sn, required=True)
    for hostname, sn in inherited:
        _resolve_one(hostname, sn, required=False)
    return out


def ovh_byoi_reinstall_for_env(
    db: Session,
    env: Environment,
    *,
    operating_system: str | None = None,
    server_hostnames: list[str] | None = None,
    image_url: str | None = None,
    dry_run: bool = False,
    wait: bool = True,
    log: LogFn | None = None,
    deadline: float | None = None,
    check_cancel: Callable[[], None] | None = None,
    sleep_fn: Callable[[float], None] | None = None,
) -> dict[str, Any]:
    """Reinstall the env's OVH servers (BYOI) and optionally wait for Talos.

    Selects OVH-owned config servers, POSTs
    ``/dedicated/server/{service}/reinstall`` (with ``customizations.imageURL``
    when a Talos factory image is set), then — unless ``wait`` is false —
    polls install/status and the Talos maintenance API on :50000.

    One server failing never aborts the others. ``ok`` is true only when
    every selected server was reinstalled (and, if waiting, reached
    talos-ready). Dry run logs the would-be reinstalls and does not wait.
    """
    _log = log or (lambda _msg: None)
    _sleep = sleep_fn or time.sleep
    hostname_filter: frozenset[str] | None = None
    if server_hostnames:
        hostname_filter = frozenset(
            str(h).strip() for h in server_hostnames if str(h).strip()
        )

    if not env.ovh_account_id:
        _log("[ovh] environment is not bound to an OVH account (Admin -> OVH)")
        return {
            "ok": False,
            "error": "environment is not bound to an OVH account",
            "count": 0,
            "servers": [],
        }
    account = db.get(OvhAccount, env.ovh_account_id)
    if account is None:
        _log(f"[ovh] bound OVH account {env.ovh_account_id} not found")
        return {
            "ok": False,
            "error": f"bound OVH account '{env.ovh_account_id}' not found",
            "count": 0,
            "servers": [],
        }
    consumer_key = decrypt_secret(account.consumer_key_encrypted) or ""
    if not consumer_key:
        _log(f"[ovh] OVH account '{account.name}' has no approved consumer key")
        return {
            "ok": False,
            "error": (
                f"OVH account '{account.name}' has no approved consumer key — "
                "create one under Admin -> OVH accounts -> Connect."
            ),
            "count": 0,
            "servers": [],
        }

    current = envconfig_service.get_current(db, env)
    if current is None:
        _log("[ovh] no config document — nothing to reinstall")
        return {
            "ok": False,
            "error": "environment has no config document",
            "count": 0,
            "servers": [],
        }
    doc = current[0]
    provider = str(doc.get("provider") or "").strip().lower()
    image = str(image_url or "").strip() or _talos_image_url(doc)
    os_name = str(operating_system or "").strip()
    if provider == "talos":
        if not image:
            msg = (
                "talos.image_url is required for OVH BYOI — set a Talos factory "
                "image URL (prefer metal-amd64.qcow2) on the Deployment card"
            )
            _log(f"[ovh] {msg}")
            return {"ok": False, "error": msg, "count": 0, "servers": []}
        if not os_name or "byoi" not in os_name.lower():
            if os_name and os_name != DEFAULT_BYOI_OS:
                _log(
                    f"[ovh] Talos image URL set — using {DEFAULT_BYOI_OS} "
                    f"instead of catalog OS '{os_name}'"
                )
            os_name = DEFAULT_BYOI_OS
    if not os_name:
        _log("[ovh] operating_system is required when no Talos image URL is set")
        return {
            "ok": False,
            "error": "operating_system is required — the OVH OS template name to install",
            "count": 0,
            "servers": [],
        }

    ssh_key = (env.ssh_public_key or "").strip() or None
    efi_path = None
    talos_cfg = doc.get("talos") if isinstance(doc.get("talos"), dict) else {}
    if talos_cfg.get("efi_bootloader_path"):
        efi_path = str(talos_cfg.get("efi_bootloader_path")).strip() or None

    client = OvhClient(
        endpoint=account.endpoint,
        app_key=account.app_key,
        app_secret=decrypt_secret(account.app_secret_encrypted) or "",
        consumer_key=consumer_key,
    )
    try:
        if not client.authenticated:
            _log("[ovh] OVH client is not authenticated")
            return {
                "ok": False,
                "error": "OVH account credentials are incomplete",
                "count": 0,
                "servers": [],
            }

        servers = _ovh_resolve_service_names(
            client,
            doc,
            hostname_filter=hostname_filter,
            log=_log,
            ovh_env=env_is_ovh(env),
        )
        if not servers:
            _log(
                "[ovh] no OVH servers selected in the config document "
                "(need source: ovh, or static hosts whose IP/hostname match the bound account)"
            )
            return {
                "ok": False,
                "error": (
                    "no OVH servers found in the config document — "
                    "bind this environment to an OVH account and import the dedicated servers"
                ),
                "count": 0,
                "servers": [],
            }

        results: list[dict[str, Any]] = []
        pending: list[dict[str, Any]] = []
        for target in servers:
            hostname = target["hostname"]
            service_name = target["service_name"]
            if target.get("error"):
                _log(f"[ovh] skipping '{hostname}': {target['error']}")
                results.append(
                    {
                        "hostname": hostname,
                        "service_name": None,
                        "task_id": None,
                        "error": target["error"],
                    }
                )
                continue
            custom = (
                byoi_customizations(
                    image_url=image,
                    hostname=hostname,
                    ssh_key=ssh_key,
                    efi_bootloader_path=efi_path,
                )
                if image
                else None
            )
            if dry_run:
                extra = f" imageURL={image}" if image else ""
                _log(
                    f"[dry-run] would POST /dedicated/server/{service_name}/reinstall "
                    f"operatingSystem={os_name}{extra} (config server '{hostname}')"
                )
                results.append(
                    {
                        "hostname": hostname,
                        "service_name": service_name,
                        "task_id": None,
                        "error": None,
                        "dry_run": True,
                    }
                )
                continue
            try:
                task_id = client.reinstall_server(
                    service_name, os_name, customizations=custom
                )
            except OvhError as exc:
                _log(f"[ovh] reinstall {service_name} failed: {exc}")
                results.append(
                    {
                        "hostname": hostname,
                        "service_name": service_name,
                        "task_id": None,
                        "error": str(exc),
                    }
                )
                continue
            task_str = str(task_id) if task_id is not None else None
            _log(f"[ovh] reinstall {service_name} accepted — task {task_str or '-'}")
            row = {
                "hostname": hostname,
                "service_name": service_name,
                "task_id": task_str,
                "error": None,
                "ip": _server_ip(doc, hostname),
                "probe_ips": _server_probe_ips(doc, hostname),
            }
            results.append(row)
            pending.append(row)

        if wait and not dry_run:
            for row in pending:
                if check_cancel is not None:
                    check_cancel()
                err = _wait_ovh_install(
                    client,
                    row["service_name"],
                    _log,
                    poll_interval=_OVH_INSTALL_POLL_S,
                    deadline=deadline,
                    check_cancel=check_cancel,
                    sleep_fn=_sleep,
                )
                if err:
                    row["error"] = err
                    _log(f"[ovh] {err}")
                    continue
                row["install"] = "done"
                err = _wait_talos_api(
                    row.get("probe_ips") or row.get("ip") or "",
                    _log,
                    poll_interval=_OVH_TALOS_POLL_S,
                    deadline=deadline,
                    check_cancel=check_cancel,
                    sleep_fn=_sleep,
                )
                if err:
                    row["error"] = err
                    _log(f"[ovh] {err}")
                    continue
                row["talos"] = "ready"

        failed = [r for r in results if r.get("error")]
        ok = not failed
        if dry_run:
            message = f"[dry-run] would reinstall {len(results)} server(s)"
        elif wait:
            ready = sum(1 for r in results if r.get("talos") == "ready")
            message = (
                f"reinstalled {len(results) - len(failed)}/{len(results)} server(s), "
                f"{ready} talos-ready"
            )
        else:
            message = (
                f"reinstalled {len(results) - len(failed)}/{len(results)} server(s)"
            )
        _log(f"[ovh] {message}")
        return {
            "ok": ok,
            "count": len(results),
            "dry_run": dry_run,
            "servers": results,
            "message": message,
            "image_url": image or None,
            "operating_system": os_name,
            **(
                {}
                if ok
                else {
                    "error": f"{len(failed)}/{len(results)} server(s) failed: "
                    + "; ".join(f"{r['hostname']}: {r['error']}" for r in failed)
                }
            ),
        }
    except OvhError as exc:
        _log(f"[ovh] {exc}")
        return {"ok": False, "error": str(exc), "count": 0, "servers": []}
    finally:
        client.close()
