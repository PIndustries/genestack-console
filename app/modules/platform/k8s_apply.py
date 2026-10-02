"""What: Apply Kubernetes YAML. Queue server-side apply of YAML manifests.
Where: app/modules/platform/k8s_apply.py. PlatformModule lists this file.
Why: One file so this step does not grow the job runner.
"""

from __future__ import annotations

from app.modules.params import p as _p

HANDLERS = ("k8s_apply",)

OPERATION = {
    "id": "k8s.apply",
    "name": "Apply Kubernetes YAML",
    "description": (
        "Queue server-side apply of YAML manifests. Honors env/global dry_run. "
        "Mutating; per-env lock."
    ),
    "required_role": "operator",
    "backend": "genestack",
    "params": [
        _p("yaml", True, "Kubernetes manifest YAML"),
        _p(
            "dry_run",
            False,
            "Force a dry-run rehearsal regardless of env setting",
            "boolean",
        ),
    ],
    "handler": "k8s_apply",
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
    from app.services import k8s_ops

    yaml_text = params.get("yaml")
    if not isinstance(yaml_text, str) or not yaml_text.strip():
        return {"ok": False, "dry_run": dry, "error": "yaml is required"}
    effective_dry = dry or bool(params.get("dry_run"))
    log(f"k8s.apply bytes={len(yaml_text)} dry_run={effective_dry}")
    if env is None:
        return {"ok": False, "dry_run": effective_dry, "error": "environment required"}
    prev = getattr(env, "dry_run", None)
    try:
        if effective_dry:
            env.dry_run = True
        return k8s_ops.apply_yaml(env, self.settings, text=yaml_text)
    finally:
        env.dry_run = prev
