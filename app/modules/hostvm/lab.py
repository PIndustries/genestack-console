"""What: Create Lab (hostvm.lab).
Where: app/modules/hostvm/lab.py. HostVmModule lists this file.
Why: A console can start a 3 to 5 node lab on the computer it runs on.
The job is the way those guests get created.
"""

from __future__ import annotations

from app.services import lab_launch

HANDLERS = ("hostvm_lab",)

OPERATION = {
    "id": "hostvm.lab",
    "name": "Create Lab",
    "description": (
        "Start a 3 to 5 node lab on this console host. The default is 3: "
        "control, compute and network, and storage. Four adds a worker. "
        "Five adds a second compute. Each guest is Ubuntu, 4 vCPU, 16 GiB, "
        "and a thin 80 GB disk. This is not a production workload. The "
        "console host stays outside the cluster. The environment stays "
        "rehearsed until you Apply. If qemu is not installed, the job "
        "installs qemu-system-x86 and qemu-utils. Pass confirm true to "
        "start the guests. Without confirm, the job only writes the plan."
    ),
    "required_role": "operator",
    "backend": "internal",
    "params": [
        {
            "name": "count",
            "required": False,
            "description": "3, 4, or 5. The default is 3.",
            "type": "integer",
            "default": 3,
            "enum": [3, 4, 5],
        },
        {
            "name": "confirm",
            "required": False,
            "description": "Start the guests. Without this, the job only writes the plan.",
            "type": "boolean",
            "default": False,
            "enum": None,
        },
        {
            "name": "name",
            "required": False,
            "description": "Environment name. The default is genestack-lab.",
            "type": "string",
            "default": "genestack-lab",
            "enum": None,
        },
    ],
    "handler": "hostvm_lab",
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
    # The lab is host-level. Confirm starts the guests. The environment it
    # writes stays rehearsed so OpenStack does not install until Apply.
    del handler, op, env, ctx, deadline, dry, timeout
    del gs_root, ans_root, extra_env, ssh_target, remote_env, executor, agent_env_id
    return lab_launch.create_lab(
        self.db,
        self.settings,
        job,
        log,
        params or {},
        check_cancel=check_cancel,
    )
