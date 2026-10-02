"""What: Shared steps: Commission MAAS Machine (maas.machine.commission); Deploy MAAS
Machine (maas.machine.deploy); Release MAAS Machine (maas.machine.release).
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
        "name": "Commission MAAS Machine",
        "description": "Commission a MAAS machine (op=commission) for the environment.",
        "required_role": "operator",
        "backend": "maas",
        "params": [
            _p("system_id", True, "MAAS machine system_id"),
        ],
        "handler": "maas_machine_commission",
        "mutating": True,
        "timeout_seconds": 1800,
    },
    {
        "id": "maas.machine.deploy",
        "name": "Deploy MAAS Machine",
        "description": (
            "Deploy a MAAS machine (op=deploy) with optional hostname and Genestack "
            "roles; renders cloud-init user-data and upserts the env config doc "
            "servers section so deploy -> inventory is one action. With image set "
            "(an uploaded custom boot-resource such as a Talos factory image), "
            "deploys osystem=custom and skips cloud-init user-data."
        ),
        "required_role": "operator",
        "backend": "maas",
        "params": [
            _p("system_id", True, "MAAS machine system_id"),
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
        "name": "Release MAAS Machine",
        "description": "Release a MAAS machine back to the pool (op=release).",
        "required_role": "operator",
        "backend": "maas",
        "params": [
            _p("system_id", True, "MAAS machine system_id"),
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
