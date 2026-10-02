"""What: Genestack Host Setup. Run core Genestack ansible/playbooks/host-setup.yml
(host_setup role — same path operators use via setup-hosts.sh).
Where: app/modules/genestack/host_setup.py. GenestackModule lists this file.
Why: One file so this step does not grow the job runner.
"""

from __future__ import annotations

from app.modules.params import p as _p
from app.services import genestack_bridge as bridge

HANDLERS = ("genestack_host_setup",)

OPERATION = {
    "id": "genestack.host_setup",
    "name": "Genestack Host Setup",
    "description": (
        "Run core Genestack ansible/playbooks/host-setup.yml "
        "(host_setup role — same path operators use via setup-hosts.sh)."
    ),
    "required_role": "admin",
    "backend": "genestack",
    "params": [
        _p("limit", False, "Ansible --limit pattern"),
        _p("check", False, "If true, ansible --check mode", "boolean"),
    ],
    "handler": "genestack_host_setup",
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
    # Reuse core Genestack ansible/playbooks/host-setup.yml (same as setup-hosts.sh)
    check = params.get("check")
    check_mode = check is True or str(check).lower() in ("1", "true", "yes")
    inv = None
    if env and env.metadata and isinstance(env.metadata, dict):
        inv = env.metadata.get("ansible_inventory")
    result = bridge.run_playbook(
        "host-setup.yml",
        ansible_root=ans_root,
        genestack_root=gs_root,
        limit=params.get("limit"),
        inventory_path=inv,
        check=check_mode,
        dry_run=dry,
        timeout=timeout,
        extra_env=extra_env,
        ssh_target=ssh_target,
        remote_env=remote_env,
        log=log,
    )
    result["message"] = result.get("message") or "genestack host-setup"
    return result
