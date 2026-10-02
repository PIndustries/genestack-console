"""What: Drain Kubernetes Node. Queue a Kubernetes node drain (cordon + evict).
Where: app/modules/platform/k8s_node_drain.py. PlatformModule lists this file.
Why: One file so this step does not grow the job runner.
"""

from __future__ import annotations

from app.modules.params import p as _p

HANDLERS = ("k8s_node_drain",)

OPERATION = {
    "id": "k8s.node.drain",
    "name": "Drain Kubernetes Node",
    "description": (
        "Queue a Kubernetes node drain (cordon + evict). Honors env/global "
        "dry_run. Mutating; per-env lock."
    ),
    "required_role": "operator",
    "backend": "genestack",
    "params": [
        _p("name", True, "Kubernetes node name"),
        _p("ignore_daemonsets", False, "Skip DaemonSet pods (default true)", "boolean", default=True),
        _p("delete_emptydir", False, "Delete emptyDir pods (default false)", "boolean", default=False),
        _p("grace_period", False, "Pod termination grace period seconds", "integer", default=30),
        _p("timeout", False, "Drain timeout seconds", "integer", default=90),
        _p(
            "dry_run",
            False,
            "Force a dry-run rehearsal regardless of env setting",
            "boolean",
        ),
    ],
    "handler": "k8s_node_drain",
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
    from app.services import k8s_ops

    name = str(params.get("name") or "").strip()
    if not name:
        return {"ok": False, "dry_run": dry, "error": "name is required"}
    effective_dry = dry or bool(params.get("dry_run"))
    # Temporarily force env dry_run for this call when params/env pin it.
    # k8s_ops.drain_node reads EnvContext.dry_run from the environment row /
    # global settings; override via a shallow pin on the env object.
    ignore_daemonsets = params.get("ignore_daemonsets", True)
    delete_emptydir = params.get("delete_emptydir", False)
    grace_period = int(params.get("grace_period") or 30)
    timeout = int(params.get("timeout") or 90)
    log(f"k8s.node.drain node={name} dry_run={effective_dry}")
    if env is None:
        return {"ok": False, "dry_run": effective_dry, "error": "environment required"}
    prev = getattr(env, "dry_run", None)
    try:
        if effective_dry:
            env.dry_run = True
        return k8s_ops.drain_node(
            env,
            self.settings,
            name=name,
            ignore_daemonsets=bool(ignore_daemonsets),
            delete_emptydir=bool(delete_emptydir),
            grace_period=grace_period,
            timeout=timeout,
        )
    finally:
        env.dry_run = prev
