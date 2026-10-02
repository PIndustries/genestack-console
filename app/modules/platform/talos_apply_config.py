"""What: Talos Apply Machine Config. Queue ``talosctl apply-config --file … --mode <mode>``
for one node.
Where: app/modules/platform/talos_apply_config.py. PlatformModule lists this file.
Why: One file so this step does not grow the job runner.
"""

from __future__ import annotations

from app.modules.params import p as _p

HANDLERS = ("platform_talos_apply_config",)

OPERATION = {
    "id": "platform.talos.apply_config",
    "name": "Talos Apply Machine Config",
    "description": (
        "Queue ``talosctl apply-config --file … --mode <mode>`` for one node. "
        "Honors dry_run. Mutating; per-env lock."
    ),
    "required_role": "operator",
    "backend": "genestack",
    "params": [
        _p("name", True, "Inventory / node hostname"),
        _p("yaml", True, "Machineconfig YAML"),
        _p(
            "mode",
            False,
            "auto | staged | no-reboot | reboot (default auto)",
            default="auto",
            enum=["auto", "staged", "no-reboot", "reboot"],
        ),
        _p(
            "dry_run",
            False,
            "Force a dry-run rehearsal regardless of env setting",
            "boolean",
        ),
    ],
    "handler": "platform_talos_apply_config",
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
    yaml_text = params.get("yaml")
    if not name:
        return {"ok": False, "dry_run": dry, "error": "name is required"}
    if not isinstance(yaml_text, str) or not yaml_text.strip():
        return {"ok": False, "dry_run": dry, "error": "yaml is required"}
    mode = str(params.get("mode") or "auto").strip().lower() or "auto"
    effective_dry = dry or bool(params.get("dry_run"))
    log(
        f"platform.talos.apply_config node={name} mode={mode} "
        f"bytes={len(yaml_text)} dry_run={effective_dry}"
    )
    if env is None:
        return {"ok": False, "dry_run": effective_dry, "error": "environment required"}
    return platform_svc.talos_apply_config(
        env,
        name,
        self.settings,
        self.db,
        yaml_text=yaml_text,
        mode=mode,
        dry_run=effective_dry,
    )
