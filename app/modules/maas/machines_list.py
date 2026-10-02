"""What: List MAAS Machines. List machines from the environment MAAS (or default console
MAAS).
Where: app/modules/maas/machines_list.py. MaasModule lists this file.
Why: One file so this step does not grow the job runner.
"""

from __future__ import annotations

from app.modules.params import p as _p
from app.services import genestack_bridge as bridge

HANDLERS = ("maas_machines_list",)

OPERATION = {
    "id": "maas.machines.list",
    "name": "List MAAS Machines",
    "description": "List machines from the environment MAAS (or default console MAAS).",
    "required_role": "viewer",
    "backend": "maas",
    "params": [
        _p("environment_id", False, "Override environment for MAAS credentials"),
    ],
    "handler": "maas_machines_list",
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
    try:
        client = self._maas_client(env)
        machines = client.list_machines()
        is_mock = client.mock
        client.close()
        log(f"[maas] listed {len(machines)} machines mock={is_mock}")
        return {
            "ok": True,
            "mock": is_mock,
            "maas_configured": client.configured,
            "dry_run": dry,
            "machines": machines,
            "count": len(machines),
            "message": f"{len(machines)} machines",
        }
    except Exception as exc:  # noqa: BLE001
        log(f"[maas] MaasClient failed ({exc}); falling back to bridge")
        return bridge.maas_list_machines(url, key, dry_run=dry, log=log)
