"""What: Talos Node Reboot. Queue ``talosctl reboot --wait=false`` for one inventory node.
Where: app/modules/platform/talos_reboot.py. PlatformModule lists this file.
Why: One file so this step does not grow the job runner.
"""

from __future__ import annotations

from app.modules.params import p as _p

HANDLERS = ("platform_talos_reboot",)

OPERATION = {
    "id": "platform.talos.reboot",
    "name": "Talos Node Reboot",
    "description": (
        "Queue ``talosctl reboot --wait=false`` for one inventory node. "
        "Honors env/global dry_run (rehearsal only). Mutating; per-env lock."
    ),
    "required_role": "operator",
    "backend": "genestack",
    "params": [
        _p("name", True, "Inventory / node hostname"),
        _p(
            "dry_run",
            False,
            "Force a dry-run rehearsal regardless of env setting",
            "boolean",
        ),
    ],
    "handler": "platform_talos_reboot",
    "mutating": True,
    "timeout_seconds": 120,
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
    from app.services import platform as platform_svc

    name = str(params.get("name") or "").strip()
    if not name:
        return {"ok": False, "dry_run": dry, "error": "name is required"}
    effective_dry = dry or bool(params.get("dry_run"))
    log(f"platform.talos.reboot node={name} dry_run={effective_dry}")
    if env is None:
        return {"ok": False, "dry_run": effective_dry, "error": "environment required"}
    return platform_svc.talos_reboot(
        env, name, self.settings, self.db, dry_run=effective_dry
    )
