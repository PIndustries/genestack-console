"""What: Shared steps: Terraform Plan (bare metal) (hardware.terraform.plan); Terraform
Apply (bare metal) (hardware.terraform.apply).
Where: app/modules/hardware/terraform_actions.py. HardwareModule lists this file.
Why: These handlers share one body, so they stay in one file instead of growing the job
runner.
"""

from __future__ import annotations

from app.modules.params import p as _p

HANDLERS = (
    "hardware_terraform_plan",
    "hardware_terraform_apply",
)

OPERATIONS = (
    {
        "id": "hardware.terraform.plan",
        "name": "Terraform Plan (bare metal)",
        "description": (
            "Plan Terraform for a saved Hardware account (AWS, Azure, GCP, "
            "or Rackspace). Decrypts credentials only in the job worker. "
            "Does not change inventory. Dry-run, or a missing terraform "
            "binary, returns a rehearsal payload without shelling out."
        ),
        "required_role": "operator",
        "backend": "internal",
        "params": [
            _p("account_id", True, "HardwareAccount id"),
            _p("environment_id", False, "Environment receiving the plan (job scope)"),
            _p("count", False, "Node count (default 1)", "integer"),
            _p("flavor", False, "Instance/flavor override"),
            _p("region", False, "Region override (default: the account region)"),
            _p("roles", False, "Genestack roles for planned nodes", "array"),
            _p(
                "dry_run",
                False,
                "Force a dry-run rehearsal regardless of env setting",
                "boolean",
            ),
        ],
        "handler": "hardware_terraform_plan",
        "mutating": True,
        "timeout_seconds": 1800,
    },
    {
        "id": "hardware.terraform.apply",
        "name": "Terraform Apply (bare metal)",
        "description": (
            "Apply Terraform for a saved Hardware account (AWS, Azure, GCP, "
            "or Rackspace) and upsert resulting hosts into the environment "
            "config servers section (source=terraform), the same inventory "
            "PXE/OVH/static hosts already use. Dry-run without a terraform "
            "binary imports placeholder hosts; live apply without the binary "
            "fails. Credentials are never returned or logged."
        ),
        "required_role": "operator",
        "backend": "internal",
        "params": [
            _p("account_id", True, "HardwareAccount id"),
            _p(
                "environment_id",
                False,
                "Environment receiving imported hosts (job scope)",
            ),
            _p("count", False, "Node count (default 1)", "integer"),
            _p("flavor", False, "Instance/flavor override"),
            _p("region", False, "Region override (default: the account region)"),
            _p("roles", False, "Genestack roles for imported hosts", "array"),
            _p(
                "dry_run",
                False,
                "Force a dry-run rehearsal regardless of env setting",
                "boolean",
            ),
        ],
        "handler": "hardware_terraform_apply",
        "mutating": True,
        "timeout_seconds": 1800,
    },
)


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
    from app.services import terraform as terraform_service

    action = "apply" if handler == "hardware_terraform_apply" else "plan"
    effective_dry = dry or bool(params.get("dry_run"))
    result = terraform_service.run_terraform_job(
        self.db,
        env,
        action=action,
        params=params,
        dry_run=effective_dry,
        log=log,
        timeout=timeout,
        actor=job.created_by or "system",
        settings=self.settings,
    )
    self.write_audit(
        actor=job.created_by or "system",
        action=f"env.hardware.terraform.{action}",
        resource_type="environment",
        resource_id=env.id if env is not None else None,
        environment_id=env.id if env is not None else None,
        details={
            "account_id": str(params.get("account_id") or ""),
            "count": result.get("count"),
            "kind": result.get("kind"),
            "imported": result.get("imported") or [],
            "dry_run": effective_dry,
        },
        success=bool(result.get("ok")),
    )
    return result
