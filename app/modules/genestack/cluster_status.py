"""What: Cluster Status. Probe cluster reachability, nodes, and namespaces via kubectl.
Where: app/modules/genestack/cluster_status.py. GenestackModule lists this file.
Why: One file so this step does not grow the job runner.
"""

from __future__ import annotations

from app.services import cluster as cluster_probe

HANDLERS = ("genestack_cluster_status",)

OPERATION = {
    "id": "genestack.cluster.status",
    "name": "Cluster Status",
    "description": "Probe cluster reachability, nodes, and namespaces via kubectl.",
    "required_role": "viewer",
    "backend": "genestack",
    "params": [],
    "handler": "genestack_cluster_status",
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
    result = cluster_probe.cluster_status(ctx.kubeconfig)
    log(
        f"cluster reachable={result.get('reachable')} "
        f"nodes={len(result.get('nodes') or [])} error={result.get('error')}"
    )
    result["ok"] = True
    result["message"] = (
        "cluster reachable"
        if result.get("reachable")
        else f"cluster unreachable: {result.get('error')}"
    )
    return result
