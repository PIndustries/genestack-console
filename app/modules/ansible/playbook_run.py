"""What: Run Allowlisted Playbook. Run an allowlisted ansible playbook.
Where: app/modules/ansible/playbook_run.py. AnsibleModule lists this file.
Why: One file so this step does not grow the job runner.
"""

from __future__ import annotations

from app.modules.params import p as _p
from app.services import genestack_bridge as bridge
from app.services.catalog import PLAYBOOK_ALLOWLIST

HANDLERS = ("ansible_playbook_run",)

OPERATION = {
    "id": "ansible.playbook.run",
    "name": "Run Allowlisted Playbook",
    "description": f"Run an allowlisted ansible playbook. Allowlist: {', '.join(sorted(PLAYBOOK_ALLOWLIST))}.",
    "required_role": "operator",
    "backend": "ansible",
    "params": [
        _p(
            "playbook",
            True,
            "Playbook filename (must be allowlisted)",
            enum=sorted(PLAYBOOK_ALLOWLIST),
        ),
        _p("limit", False, "Ansible --limit pattern"),
        _p("extra_vars", False, "Extra vars as JSON object", "object"),
        _p("tags", False, "Comma-separated ansible tags"),
    ],
    "handler": "ansible_playbook_run",
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
    playbook = str(params.get("playbook", ""))
    return bridge.run_playbook(
        playbook,
        ansible_root=ans_root,
        genestack_root=gs_root,
        limit=params.get("limit"),
        extra_vars=(
            params.get("extra_vars")
            if isinstance(params.get("extra_vars"), dict)
            else None
        ),
        tags=params.get("tags"),
        dry_run=dry,
        timeout=timeout,
        extra_env=extra_env,
        ssh_target=ssh_target,
        remote_env=remote_env,
        log=log,
    )
