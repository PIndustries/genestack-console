"""What: List Bare-Metal Nodes. List the environment's registered bare-metal nodes, power
state (registered|booting|talos-ready|failed), and boot stage
(new|commissioning|commissioned|talos|fresh-maintenance|installed|failed).
Where: app/modules/baremetal/nodes_list.py. BaremetalModule lists this file.
Why: One file so this step does not grow the job runner.
"""

from __future__ import annotations

HANDLERS = ("baremetal_nodes_list",)

OPERATION = {
    "id": "baremetal.nodes.list",
    "name": "List Bare-Metal Nodes",
    "description": (
        "List the environment's registered bare-metal nodes, power state "
        "(registered|booting|talos-ready|failed), and boot stage "
        "(new|commissioning|commissioned|talos|fresh-maintenance|"
        "installed|failed). The UI also reads these via "
        "GET /api/v1/environments/{id}/baremetal."
    ),
    "required_role": "viewer",
    "backend": "baremetal",
    "params": [],
    "handler": "baremetal_nodes_list",
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
    if env is None:
        return {
            "ok": False,
            "error": "baremetal.nodes.list requires an environment",
            "returncode": 2,
        }
    from app.services import baremetal as baremetal_service

    nodes = baremetal_service.list_nodes(self.db, env)
    log(f"[baremetal] listed {len(nodes)} nodes")
    return {
        "ok": True,
        "dry_run": dry,
        "nodes": nodes,
        "count": len(nodes),
        "message": f"{len(nodes)} bare-metal nodes",
    }
