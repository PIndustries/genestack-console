"""What: Genestack Service Registry. Build the service registry from bin/install-*.sh
headers, chart versions, and desired state; returns counts per category.
Where: app/modules/genestack/services_list.py. GenestackModule lists this file.
Why: One file so this step does not grow the job runner.
"""

from __future__ import annotations

from app.services.service_registry import build_service_registry

HANDLERS = ("genestack_services_list",)

OPERATION = {
    "id": "genestack.services.list",
    "name": "Genestack Service Registry",
    "description": (
        "Build the service registry from bin/install-*.sh headers, chart "
        "versions, and desired state; returns counts per category."
    ),
    "required_role": "viewer",
    "backend": "genestack",
    "params": [],
    "handler": "genestack_services_list",
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
    registry = build_service_registry(gs_root)
    services = registry["services"]
    categories: dict[str, int] = {}
    for svc in services:
        categories[svc["category"]] = categories.get(svc["category"], 0) + 1
    log(f"registry: {len(services)} services, categories={categories}")
    return {
        "ok": True,
        "count": len(services),
        "categories": categories,
        "services": [svc["name"] for svc in services],
        "genestack_root": registry["genestack_root"],
        "message": f"{len(services)} services",
    }
