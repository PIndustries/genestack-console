"""What: MAAS Machine Power Status. Query power status for a MAAS machine system_id.
Where: app/modules/maas/machine_power_status.py. MaasModule lists this file.
Why: One file so this step does not grow the job runner.
"""

from __future__ import annotations

from app.modules.params import p as _p
from app.services import genestack_bridge as bridge

HANDLERS = ("maas_machine_power_status",)

OPERATION = {
    "id": "maas.machine.power_status",
    "name": "MAAS Machine Power Status",
    "description": "Query power status for a MAAS machine system_id.",
    "required_role": "viewer",
    "backend": "maas",
    "params": [
        _p("system_id", True, "MAAS machine system_id"),
        _p("environment_id", False, "Environment providing MAAS credentials"),
    ],
    "handler": "maas_machine_power_status",
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
    url, key = self._maas_creds(env)
    system_id = str(params.get("system_id", ""))
    try:
        client = self._maas_client(env)
        status = client.power_status(system_id)
        client.close()
        log(f"[maas] power_status system_id={system_id} -> {status}")
        return {"ok": True, "dry_run": dry, **status}
    except Exception as exc:  # noqa: BLE001
        log(f"[maas] power status via client failed ({exc}); bridge fallback")
        return bridge.maas_power_status(
            url, key, system_id, dry_run=dry, log=log
        )
