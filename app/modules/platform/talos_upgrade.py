"""What: Talos Node Upgrade. Queue ``talosctl upgrade`` for one node (installer image from
param or env talos.install_image).
Where: app/modules/platform/talos_upgrade.py. PlatformModule lists this file.
Why: One file so this step does not grow the job runner.
"""

from __future__ import annotations

from app.modules.params import p as _p

HANDLERS = ("platform_talos_upgrade",)

OPERATION = {
    "id": "platform.talos.upgrade",
    "name": "Talos Node Upgrade",
    "description": (
        "Queue ``talosctl upgrade`` for one node (installer image from "
        "param or env talos.install_image). Honors dry_run. Mutating; per-env lock."
    ),
    "required_role": "operator",
    "backend": "genestack",
    "params": [
        _p("name", True, "Inventory / node hostname"),
        _p("image", False, "Installer image override (registry-style)"),
        _p(
            "dry_run",
            False,
            "Force a dry-run rehearsal regardless of env setting",
            "boolean",
        ),
    ],
    "handler": "platform_talos_upgrade",
    "mutating": True,
    "timeout_seconds": 600,
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
    image = params.get("image")
    image_s = str(image).strip() if image is not None else None
    if image_s == "":
        image_s = None
    effective_dry = dry or bool(params.get("dry_run"))
    log(f"platform.talos.upgrade node={name} image={image_s or '-'} dry_run={effective_dry}")
    if env is None:
        return {"ok": False, "dry_run": effective_dry, "error": "environment required"}
    return platform_svc.talos_upgrade(
        env, name, self.settings, self.db, image=image_s, dry_run=effective_dry
    )
