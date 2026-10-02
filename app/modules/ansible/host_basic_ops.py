"""What: Host Basic Ops. Run ansible basic_ops.yml with an action parameter.
Where: app/modules/ansible/host_basic_ops.py. AnsibleModule lists this file.
Why: One file so this step does not grow the job runner.
"""

from __future__ import annotations

from app.modules.params import p as _p
from app.services import genestack_bridge as bridge

HANDLERS = ("host_basic_ops",)

OPERATION = {
    "id": "host.basic_ops",
    "name": "Host Basic Ops",
    "description": "Run ansible basic_ops.yml with an action parameter.",
    "required_role": "operator",
    "backend": "ansible",
    "params": [
        _p(
            "action",
            True,
            "Action passed to basic_ops (ping, facts, disk_check, all)",
            enum=["ping", "facts", "disk_check", "all"],
        ),
        _p("limit", False, "Ansible --limit pattern"),
        _p("extra_vars", False, "Extra vars as JSON object", "object"),
    ],
    "handler": "host_basic_ops",
    "mutating": True,
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
    action = params.get("action")
    extra = (
        params.get("extra_vars")
        if isinstance(params.get("extra_vars"), dict)
        else {}
    )
    extra = {**extra, "action": action}
    return bridge.run_playbook(
        "basic_ops.yml",
        ansible_root=ans_root,
        genestack_root=gs_root,
        limit=params.get("limit"),
        extra_vars=extra,
        dry_run=dry,
        timeout=timeout,
        extra_env=extra_env,
        ssh_target=ssh_target,
        remote_env=remote_env,
        log=log,
    )
