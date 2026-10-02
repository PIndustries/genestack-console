"""What: List Genestack Repo Scripts. Inventory genestack's utility/maintenance tooling
under GENESTACK_ROOT: scripts/*.sh utilities (with first-comment description),
maintenances/*.txt runbooks (with title), and ops-tools/** helpers.
Where: app/modules/genestack/repo_scripts_list.py. GenestackModule lists this file.
Why: One file so this step does not grow the job runner.
"""

from __future__ import annotations

HANDLERS = ("genestack_repo_scripts_list",)

OPERATION = {
    "id": "genestack.repo_scripts.list",
    "name": "List Genestack Repo Scripts",
    "description": (
        "Inventory genestack's utility/maintenance tooling under "
        "GENESTACK_ROOT: scripts/*.sh utilities (with first-comment "
        "description), maintenances/*.txt runbooks (with title), and "
        "ops-tools/** helpers. Read-only."
    ),
    "required_role": "viewer",
    "backend": "genestack",
    "params": [],
    "handler": "genestack_repo_scripts_list",
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
    from app.services import repo_scripts

    inventory = repo_scripts.list_repo_scripts(gs_root)
    counts = inventory["counts"]
    log(
        f"repo scripts under {gs_root}: scripts={counts['scripts']} "
        f"maintenances={counts['maintenances']} ops_tools={counts['ops_tools']}"
    )
    return {
        "ok": True,
        **inventory,
        "count": sum(counts.values()),
        "message": (
            f"{counts['scripts']} scripts, {counts['maintenances']} "
            f"maintenances, {counts['ops_tools']} ops tools"
        ),
    }
