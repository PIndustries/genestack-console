"""What: Talos Node Reset. Queue ``talosctl reset --wait=false`` (graceful/wipe/reboot
flags).
Where: app/modules/platform/talos_reset.py. PlatformModule lists this file.
Why: One file so this step does not grow the job runner.
"""

from __future__ import annotations

from app.modules.params import p as _p

HANDLERS = ("platform_talos_reset",)

OPERATION = {
    "id": "platform.talos.reset",
    "name": "Talos Node Reset",
    "description": (
        "Queue ``talosctl reset --wait=false`` (graceful/wipe/reboot flags). "
        "Wipes system disk by default. Honors dry_run. Mutating; per-env lock."
    ),
    "required_role": "operator",
    "backend": "genestack",
    "params": [
        _p("name", True, "Inventory / node hostname"),
        _p("graceful", False, "Leave etcd if possible (default true)", "boolean", default=True),
        _p("reboot", False, "Reboot after reset instead of halt (default false)", "boolean", default=False),
        _p("wipe", False, "Wipe system disk (default true)", "boolean", default=True),
        _p(
            "dry_run",
            False,
            "Force a dry-run rehearsal regardless of env setting",
            "boolean",
        ),
    ],
    "handler": "platform_talos_reset",
    "mutating": True,
    "timeout_seconds": 300,
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
    graceful = params.get("graceful", True)
    reboot = params.get("reboot", False)
    wipe = params.get("wipe", True)
    effective_dry = dry or bool(params.get("dry_run"))
    log(
        f"platform.talos.reset node={name} graceful={graceful} "
        f"reboot={reboot} wipe={wipe} dry_run={effective_dry}"
    )
    if env is None:
        return {"ok": False, "dry_run": effective_dry, "error": "environment required"}
    return platform_svc.talos_reset(
        env,
        name,
        self.settings,
        self.db,
        graceful=bool(graceful),
        reboot=bool(reboot),
        wipe=bool(wipe),
        dry_run=effective_dry,
    )
