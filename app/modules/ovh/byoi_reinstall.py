"""What: OVH BYOI Reinstall. Reinstall the environment's OVH dedicated servers via BYOI.
Where: app/modules/ovh/byoi_reinstall.py. OvhModule lists this file.
Why: One file so this step does not grow the job runner.
"""

from __future__ import annotations

from app.modules.params import p as _p

HANDLERS = ("ovh_byoi_reinstall",)

OPERATION = {
    "id": "ovh.byoi.reinstall",
    "name": "OVH BYOI Reinstall",
    "description": (
        "Reinstall the environment's OVH dedicated servers via BYOI. "
        "For provider=talos this uses talos.image_url (or params.image_url) "
        "as customizations.imageURL with operatingSystem=byoi_64, then "
        "polls OVH install/status and the Talos maintenance API on :50000 "
        "until each node is ready. Catalog OS templates still work for "
        "non-Talos reinstalls. Requires an OVH-bound environment and a "
        "consumer key that includes POST /dedicated/server/*/reinstall "
        "(re-run Connect if the key is older than that rule)."
    ),
    "required_role": "admin",
    "backend": "internal",
    "params": [
        _p(
            "operating_system",
            False,
            "OVH OS template (default byoi_64 when a Talos image URL is set)",
        ),
        _p(
            "server_hostnames",
            False,
            "Only reinstall these config server hostnames (default: all OVH-owned servers)",
            "array",
        ),
        _p("image_url", False, "Override talos.image_url for this reinstall"),
        _p(
            "wait",
            False,
            "Wait for OVH install + Talos :50000 (default true)",
            "boolean",
        ),
        _p(
            "dry_run",
            False,
            "Force a dry-run rehearsal regardless of env setting",
            "boolean",
        ),
    ],
    "handler": "ovh_byoi_reinstall",
    "mutating": True,
    "timeout_seconds": 10800,
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
            "error": (
                "Cannot reinstall OVH servers: this job has no environment assigned. "
                "Create the job under an environment with POST /api/v1/environments/<id>/jobs."
            ),
            "returncode": 2,
        }
    from app.services import deploy as deploy_service

    operating_system = str(params.get("operating_system") or "").strip() or None
    image_url = str(params.get("image_url") or "").strip() or None
    raw_hostnames = params.get("server_hostnames") or []
    if isinstance(raw_hostnames, str):
        raw_hostnames = [h for h in raw_hostnames.split(",") if h.strip()]
    server_hostnames = [
        str(h).strip() for h in raw_hostnames if str(h).strip()
    ] or None
    # The env/global dry-run pin always wins; params.dry_run may only
    # force a rehearsal on top of it (same clamp as genestack.deploy).
    effective_dry = dry or bool(params.get("dry_run"))
    wait = params.get("wait") is not False
    log(
        f"[ovh] BYOI reinstall for environment '{env.name}' "
        f"operating_system={operating_system or '(from image)'} "
        f"server_hostnames={','.join(server_hostnames) if server_hostnames else 'all OVH-owned'} "
        f"(dry_run={effective_dry} wait={wait})"
    )
    result = deploy_service.ovh_byoi_reinstall_for_env(
        self.db,
        env,
        operating_system=operating_system,
        server_hostnames=server_hostnames,
        image_url=image_url,
        dry_run=effective_dry,
        wait=wait,
        log=log,
        deadline=deadline,
        check_cancel=check_cancel,
    )
    self.write_audit(
        actor=job.created_by or "system",
        action="env.ovh.byoi_reinstall",
        resource_type="environment",
        resource_id=env.id,
        environment_id=env.id,
        details={
            "operating_system": operating_system,
            "server_hostnames": server_hostnames,
            "count": result.get("count"),
            "dry_run": effective_dry,
            "failed": [
                s["hostname"]
                for s in result.get("servers") or []
                if s.get("error")
            ],
        },
        success=bool(result.get("ok")),
    )
    return result
