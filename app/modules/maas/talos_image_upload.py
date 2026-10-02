"""What: Upload Talos Factory Image to MAAS. Zero-touch talos provisioning, step 1:
download the Talos Image Factory image over HTTPS (bounded size, sha256 logged) and
upload it to the environment's MAAS as a custom boot-resource.
Where: app/modules/maas/talos_image_upload.py. MaasModule lists this file.
Why: One file so this step does not grow the job runner.
"""

from __future__ import annotations

from app.modules.params import p as _p

HANDLERS = ("maas_talos_image_upload",)

OPERATION = {
    "id": "maas.talos.image_upload",
    "name": "Upload Talos Factory Image to MAAS",
    "description": (
        "Zero-touch talos provisioning, step 1: download the Talos Image "
        "Factory image over HTTPS (bounded size, sha256 logged) and upload "
        "it to the environment's MAAS as a custom boot-resource. Then "
        "deploy machines with maas.machine.deploy image=<name>. The image "
        "URL defaults to the env config doc talos.image_url when the param "
        "is omitted."
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
