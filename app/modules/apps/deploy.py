"""What: Deploy Git-backed App. Clone a linked GitHub repository and apply it to Kubernetes
(Helm/Kustomize/manifests) or OpenStack (Heat/Terraform/Ansible).
Where: app/modules/apps/deploy.py. AppModule lists this file.
Why: One file so this step does not grow the job runner.
"""

from __future__ import annotations

from app.modules.params import p as _p

HANDLERS = ("app_deploy",)

OPERATION = {
    "id": "app.deploy",
    "name": "Deploy Git-backed App",
    "description": (
        "Clone a linked GitHub repository and apply it to Kubernetes "
        "(Helm/Kustomize/manifests) or OpenStack (Heat/Terraform/Ansible)."
    ),
    "required_role": "operator",
    "backend": "internal",
    "params": [
        _p("app_id", True, "App id"),
        _p("force", False, "Redeploy even if SHA is unchanged", "boolean"),
    ],
    "handler": "app_deploy",
    "mutating": True,
    "timeout_seconds": 900,
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
    from app.models import App as AppRow
    from app.services import apps as apps_svc

    app_id = str((params or {}).get("app_id") or "").strip()
    force = bool((params or {}).get("force"))
    if env is None:
        return {
            "ok": False,
            "error": "app.deploy requires an environment",
            "returncode": 2,
        }
    row = self.db.get(AppRow, app_id)
    if row is None or row.environment_id != env.id:
        return {"ok": False, "error": "app not found", "returncode": 2}
    log(f"[app] deploy {row.name} target={row.target} repo={row.repo_url}")
    result = apps_svc.deploy_app(
        row,
        env,
        kubeconfig=ctx.kubeconfig,
        force=force,
        log_fn=log,
        dry_run=dry,
    )
    row.last_sha = result.get("sha") or row.last_sha
    row.last_job_id = job.id
    row.last_status = "success" if result.get("ok") else "failed"
    row.last_error = (
        None if result.get("ok") else str(result.get("error") or "")[:400]
    )
    self.db.add(row)
    self.write_audit(
        actor=job.created_by or "system",
        action="env.app.deploy",
        resource_type="app",
        resource_id=row.id,
        environment_id=env.id,
        details={
            "name": row.name,
            "sha": result.get("sha"),
            "ok": result.get("ok"),
            "skipped": result.get("skipped"),
        },
        success=bool(result.get("ok")),
    )
    if not result.get("ok"):
        result.setdefault("returncode", 1)
    return result
