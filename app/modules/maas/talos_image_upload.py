"""What: Download a Talos image. The console installs Talos from the network.
Where: app/modules/maas/talos_image_upload.py. MaasModule lists this file.
Why: One file so this step does not grow the job runner.
"""

from __future__ import annotations

from app.modules.params import p as _p

HANDLERS = ("maas_talos_image_upload",)

OPERATION = {
    "id": "maas.talos.image_upload",
    "name": "Fetch Talos image",
    "description": (
        "Download a Talos image. Talos is installed by the console, from "
        "the network. The image URL defaults to the environment's "
        "talos.image_url when the param is omitted."
    ),
    "required_role": "operator",
    "backend": "maas",
    "params": [
        _p(
            "image_url",
            False,
            "Talos factory image URL (default: env config doc talos.image_url)",
        ),
    ],
    "handler": "maas_talos_image_upload",
    "mutating": True,
    "timeout_seconds": 1800,
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
    return self._maas_talos_image_upload(job, env, params, log, dry)
