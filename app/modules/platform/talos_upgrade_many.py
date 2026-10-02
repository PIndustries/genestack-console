"""What: Talos Bulk Upgrade. Queue Talos upgrades across nodes.
Where: app/modules/platform/talos_upgrade_many.py. PlatformModule lists this file.
Why: One file so this step does not grow the job runner.
"""

from __future__ import annotations

from app.modules.params import p as _p

HANDLERS = ("platform_talos_upgrade_many",)

OPERATION = {
    "id": "platform.talos.upgrade_many",
    "name": "Talos Bulk Upgrade",
    "description": (
        "Queue Talos upgrades across nodes. mode=parallel (lab), sequential, "
        "or rolling (one-at-a-time; same as sequential in this slice). "
        "Honors dry_run. Mutating; per-env lock."
    ),
    "required_role": "operator",
    "backend": "genestack",
    "params": [
        _p("image", False, "Installer image override"),
        _p(
            "mode",
            False,
            "parallel | sequential | rolling (default rolling)",
            default="rolling",
            enum=["parallel", "sequential", "rolling"],
        ),
        _p(
            "names",
            False,
            "Node hostnames (default: all inventory servers)",
            "array",
        ),
        _p(
            "dry_run",
            False,
            "Force a dry-run rehearsal regardless of env setting",
            "boolean",
        ),
    ],
    "handler": "platform_talos_upgrade_many",
    "mutating": True,
    "timeout_seconds": 7200,
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

    image = params.get("image")
    image_s = str(image).strip() if image is not None else None
    if image_s == "":
        image_s = None
    mode = str(params.get("mode") or "rolling").strip().lower() or "rolling"
    names = params.get("names")
    name_list = None
    if isinstance(names, list):
        name_list = [str(n).strip() for n in names if str(n).strip()]
    effective_dry = dry or bool(params.get("dry_run"))
    log(
        f"platform.talos.upgrade_many mode={mode} "
        f"names={len(name_list) if name_list else 'all'} dry_run={effective_dry}"
    )
    if env is None:
        return {"ok": False, "dry_run": effective_dry, "error": "environment required"}
    return platform_svc.talos_upgrade_many(
        env,
        self.settings,
        self.db,
        image=image_s,
        mode=mode,
        names=name_list,
        dry_run=effective_dry,
    )
