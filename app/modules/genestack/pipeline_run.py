"""What: Run Provisioning Pipeline Stage. Run one curated provisioning stage sequentially
(install scripts).
Where: app/modules/genestack/pipeline_run.py. GenestackModule lists this file.
Why: One file so this step does not grow the job runner.
"""

from __future__ import annotations

from typing import Any

from app.modules.params import p as _p
from app.services import genestack_bridge as bridge
from app.services.service_registry import (
    PIPELINE_STAGES,
    filter_stage_items,
    get_pipeline_stage,
)

HANDLERS = ("genestack_pipeline_run",)

OPERATION = {
    "id": "genestack.pipeline.run",
    "name": "Run Provisioning Pipeline Stage",
    "description": (
        "Run one curated provisioning stage sequentially (install scripts). "
        "Stage ids: see GET /api/v1/genestack/pipeline."
    ),
    "required_role": "admin",
    "backend": "genestack",
    "params": [
        _p("stage", True, "Pipeline stage id (e.g. hosts, core, observability)"),
    ],
    "handler": "genestack_pipeline_run",
    "mutating": True,
    "timeout_seconds": 14400,
}


def run(
    self,
    handler,
    op,
    job,
    env,
    log,
    ctx,
    params,
    deadline,
    check_cancel,
    dry,
    timeout,
    gs_root,
    ans_root,
    extra_env,
    ssh_target,
    remote_env,
    executor,
    agent_env_id,
):
    from app.services.job_runner import clamp_deadline_timeout

    stage_id = str(params.get("stage", "")).strip()
    stage = get_pipeline_stage(stage_id)
    if stage is None:
        valid = ", ".join(s["id"] for s in PIPELINE_STAGES)
        msg = f"Unknown pipeline stage '{stage_id}'. Valid stages: {valid}"
        log(f"[denied] {msg}")
        return {"ok": False, "error": msg, "returncode": 2}

    # Component filter: drop services the env's config doc disables.
    components: dict[str, Any] | None = None
    if env is not None:
        from app.services import envconfig as envconfig_service

        current = envconfig_service.get_current(self.db, env)
        if current is not None and isinstance(
            current[0].get("components"), dict
        ):
            components = current[0]["components"]
    items = filter_stage_items(stage, components, log)
    if not items:
        log(
            f"[pipeline] stage {stage['id']}: all items disabled in config doc — "
            "marked complete"
        )
        return {
            "ok": True,
            "stage": stage["id"],
            "results": [],
            "count": 0,
            "returncode": 0,
            "message": f"stage {stage['id']}: 0 enabled item(s), ok=True",
        }

    log(f"pipeline stage={stage['id']} items={len(items)} dry_run={dry}")
    results: list[dict[str, Any]] = []
    ok = True
    for item in items:
        if check_cancel is not None:
            check_cancel()
        r = bridge.run_command(
            ["bash", item["script"]],
            cwd=gs_root,
            timeout=clamp_deadline_timeout(timeout, deadline),
            dry_run=dry,
            extra_env=extra_env,
            ssh_target=ssh_target,
            remote_env=remote_env,
            agent_env_id=agent_env_id,
            log=log,
        )
        results.append(
            {
                "item": item["name"],
                "type": item["type"],
                "script": item["script"],
                "dry_run": r.get("dry_run"),
                "returncode": r.get("returncode"),
                "message": r.get("message"),
            }
        )
        if r.get("returncode") not in (0, None) and not r.get("dry_run"):
            ok = False
            log(
                f"[pipeline] stopping at {item['name']} rc={r.get('returncode')}"
            )
            break
    return {
        "ok": ok,
        "stage": stage["id"],
        "results": results,
        "count": len(results),
        "returncode": 0 if ok else 1,
        "message": f"stage {stage['id']}: {len(results)} item(s), ok={ok}",
    }
