"""What: List Genestack Install Scripts. List bin/install-*.sh scripts under
GENESTACK_ROOT.
Where: app/modules/genestack/scripts_list.py. GenestackModule lists this file.
Why: One file so this step does not grow the job runner.
"""

from __future__ import annotations

from app.services import genestack_bridge as bridge

HANDLERS = ("genestack_scripts_list",)

OPERATION = {
    "id": "genestack.scripts.list",
    "name": "List Genestack Install Scripts",
    "description": "List bin/install-*.sh scripts under GENESTACK_ROOT.",
    "required_role": "viewer",
    "backend": "genestack",
    "params": [],
    "handler": "genestack_scripts_list",
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
    scripts = bridge.list_install_scripts(gs_root)
    log(f"found {len(scripts)} install scripts under {gs_root / 'bin'}")
    return {
        "ok": True,
        "scripts": scripts,
        "count": len(scripts),
        "genestack_root": str(gs_root),
        "message": f"{len(scripts)} scripts",
    }
