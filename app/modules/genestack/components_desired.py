"""What: Shared steps: Desired Components (genestack.components.desired); List Components
(alias) (genestack.components.list).
Where: app/modules/genestack/components_desired.py. GenestackModule lists this file.
Why: These catalog entries run this one file, so the job runner stays a lookup.
"""

from __future__ import annotations

from app.services import genestack_bridge as bridge

HANDLERS = ("genestack_components_desired",)

OPERATIONS = (
    {
        "id": "genestack.components.desired",
        "name": "Desired Components",
        "description": "Read openstack-components.yaml from GENESTACK_ROOT or env genestack_path.",
        "required_role": "viewer",
        "backend": "genestack",
        "params": [],
        "handler": "genestack_components_desired",
    },
    {
        "id": "genestack.components.list",
        "name": "List Components (alias)",
        "description": "Alias for genestack.components.desired.",
        "required_role": "viewer",
        "backend": "genestack",
        "params": [],
        "handler": "genestack_components_desired",
    },
)


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
    path, scope = bridge.resolve_components_path(self.settings, env)
    result = bridge.read_components_desired(path)
    log(
        f"components path={result.get('path')} exists={result.get('exists')} scope={scope}"
    )
    result["ok"] = True
    result["scope"] = scope
    result["message"] = (
        "components loaded"
        if result.get("exists")
        else "components file missing"
    )
    return result
