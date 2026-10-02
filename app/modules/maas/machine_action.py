"""What: Shared steps for commission, deploy, and release.
Where: app/modules/maas/machine_action.py. MaasModule lists this file.
Why: These handlers share one body, so they stay in one file instead of growing the job
runner.
"""

from __future__ import annotations

from app.modules.params import p as _p

HANDLERS = (
    "maas_machine_commission",
    "maas_machine_deploy",
    "maas_machine_release",
)

OPERATIONS = (
    {
        "id": "maas.machine.commission",
        "name": "Commission machine",
        "description": "Commission one machine for the environment.",
        "required_role": "operator",
        "backend": "maas",
        "params": [
            _p("system_id", True, "Machine system_id"),
        ],
        "handler": "maas_machine_commission",
        "mutating": True,
        "timeout_seconds": 1800,
    },
    {
        "id": "maas.machine.deploy",
        "name": "Deploy machine",
        "description": (
            "Deploy one machine with an optional hostname and Genestack roles. "
            "Talos is installed by the console, from the network, on the bare-metal path."
        ),
        "required_role": "operator",
        "backend": "maas",
        "params": [
            _p("system_id", True, "Machine system_id"),
            _p("hostname", False, "Hostname to set on deploy"),
            _p(
                "roles",
                False,
                "Genestack roles: k8s_control_plane/etcd/control/compute/network/storage",
                "array",
            ),
            _p(
                "image",
                False,
                "Uploaded custom image name (e.g. talos-genestack); skips cloud-init",
            ),
        ],
        "handler": "maas_machine_deploy",
        "mutating": True,
        "timeout_seconds": 1800,
    },
    {
        "id": "maas.machine.release",
        "name": "Release machine",
        "description": "Release one machine.",
        "required_role": "operator",
        "backend": "maas",
        "params": [
            _p("system_id", True, "Machine system_id"),
        ],
        "handler": "maas_machine_release",
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
    return self._maas_machine_action(handler, job, env, params, log, dry)
