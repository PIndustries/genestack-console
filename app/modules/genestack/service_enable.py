"""What: Enable Genestack Service. Enable/install an OpenStack service via
bin/install-<service>.sh.
Where: app/modules/genestack/service_enable.py. GenestackModule lists this file.
Why: One file so this step does not grow the job runner.
"""

from __future__ import annotations

from app.modules.params import p as _p
from app.services import genestack_bridge as bridge

HANDLERS = ("genestack_service_enable",)

OPERATION = {
    "id": "genestack.service.enable",
    "name": "Enable Genestack Service",
    "description": (
        "Enable/install an OpenStack service via bin/install-<service>.sh. "
        "The allowlist is now all discovered install scripts under "
        "GENESTACK_ROOT/bin (excluding service-template)."
    ),
    "required_role": "admin",
    "backend": "genestack",
    "params": [
        _p(
            "service",
            True,
            "Service name (must have a discovered bin/install-<service>.sh)",
        ),
    ],
    "handler": "genestack_service_enable",
    "mutating": True,
    "timeout_seconds": 3600,
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
    service = str(params.get("service", ""))
    return bridge.enable_service(
        service,
        gs_root,
        dry_run=dry,
        timeout=timeout,
        extra_env=extra_env,
        ssh_target=ssh_target,
        remote_env=remote_env,
        agent_env_id=agent_env_id,
        log=log,
    )
