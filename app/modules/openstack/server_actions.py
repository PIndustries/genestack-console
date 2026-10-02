"""What: Shared steps: Start OpenStack Server (openstack.server.start); Stop OpenStack
Server (openstack.server.stop); Reboot OpenStack Server (openstack.server.reboot);
Delete OpenStack Server (openstack.server.delete).
Where: app/modules/openstack/server_actions.py. OpenStackModule lists this file.
Why: These handlers share one body, so they stay in one file instead of growing the job
runner.
"""

from __future__ import annotations

from app.modules.params import p as _p
from app.services import openstack_ops

HANDLERS = (
    "openstack_server_start",
    "openstack_server_stop",
    "openstack_server_reboot",
    "openstack_server_delete",
)

OPERATIONS = (
    {
        "id": "openstack.server.start",
        "name": "Start OpenStack Server",
        "description": "Start a stopped Nova server (openstack server start <server_id>).",
        "required_role": "operator",
        "backend": "genestack",
        "params": [
            _p("server_id", True, "Nova server UUID"),
        ],
        "handler": "openstack_server_start",
        "mutating": True,
        "timeout_seconds": 600,
    },
    {
        "id": "openstack.server.stop",
        "name": "Stop OpenStack Server",
        "description": "Stop a running Nova server (openstack server stop <server_id>).",
        "required_role": "operator",
        "backend": "genestack",
        "params": [
            _p("server_id", True, "Nova server UUID"),
        ],
        "handler": "openstack_server_stop",
        "mutating": True,
        "timeout_seconds": 600,
    },
    {
        "id": "openstack.server.reboot",
        "name": "Reboot OpenStack Server",
        "description": "Reboot a Nova server (openstack server reboot <server_id>).",
        "required_role": "operator",
        "backend": "genestack",
        "params": [
            _p("server_id", True, "Nova server UUID"),
        ],
        "handler": "openstack_server_reboot",
        "mutating": True,
        "timeout_seconds": 600,
    },
    {
        "id": "openstack.server.delete",
        "name": "Delete OpenStack Server",
        "description": (
            "Delete a Nova server irreversibly (openstack server delete <server_id>)."
        ),
        "required_role": "admin",
        "backend": "genestack",
        "params": [
            _p("server_id", True, "Nova server UUID"),
        ],
        "handler": "openstack_server_delete",
        "mutating": True,
        "timeout_seconds": 600,
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
    if env is None:
        return {
            "ok": False,
            "error": f"{op.id} requires an environment",
            "returncode": 2,
        }
    action = handler.removeprefix("openstack_server_")
    server_id = str(params.get("server_id", ""))
    return openstack_ops.server_action(
        env,
        self.settings,
        action,
        server_id,
        dry_run=dry,
        timeout=timeout,
        log=log,
    )
