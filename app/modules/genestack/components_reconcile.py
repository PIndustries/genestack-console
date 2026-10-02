"""What: Reconcile Components. Diff the env config doc's components: block against deployed
helm releases and converge the cloud to the desired state.
Where: app/modules/genestack/components_reconcile.py. GenestackModule lists this file.
Why: One file so this step does not grow the job runner.
"""

from __future__ import annotations

from app.modules.params import p as _p

HANDLERS = ("genestack_components_reconcile",)

OPERATION = {
    "id": "genestack.components.reconcile",
    "name": "Reconcile Components",
    "description": (
        "Diff the env config doc's components: block against deployed helm "
        "releases and converge the cloud to the desired state. Default "
        "(apply=false) is plan-only: the plan is logged and nothing is "
        "executed. apply=true enables missing components via the "
        "genestack.service.enable path and helm-uninstalls undesired "
        "releases (protected core components are never uninstalled). "
        "Env/global dry_run forces plan-only even with apply=true."
    ),
    "required_role": "operator",
    "backend": "genestack",
    "params": [
        _p(
            "apply", False, "Execute the plan (default false: plan-only)", "boolean"
        ),
    ],
    "handler": "genestack_components_reconcile",
    "mutating": True,
    "timeout_seconds": 1800,
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
    if env is None:
        return {
            "ok": False,
            "error": "genestack.components.reconcile requires an environment",
            "returncode": 2,
        }
    from app.services import reconcile as reconcile_service

    return reconcile_service.run_reconcile(
        self.db,
        env,
        self.settings,
        apply=bool(params.get("apply", False)),
        log=log,
        timeout=timeout,
    )
