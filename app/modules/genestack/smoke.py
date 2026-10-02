"""What: Genestack Smoke Checks. Lightweight smoke validation of genestack paths and key
artifacts.
Where: app/modules/genestack/smoke.py. GenestackModule lists this file.
Why: One file so this step does not grow the job runner.
"""

from __future__ import annotations

from app.services import genestack_bridge as bridge

HANDLERS = ("genestack_smoke",)

OPERATION = {
    "id": "genestack.smoke",
    "name": "Genestack Smoke Checks",
    "description": "Lightweight smoke validation of genestack paths and key artifacts.",
    "required_role": "viewer",
    "backend": "genestack",
    "params": [],
    "handler": "genestack_smoke",
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
    result = bridge.smoke_check(gs_root, ans_root)
    for c in result.get("checks", []):
        log(f"check {c['name']}: ok={c['ok']} {c.get('detail', '')}")
    result["message"] = "smoke ok" if result.get("ok") else "smoke failed"
    return result
