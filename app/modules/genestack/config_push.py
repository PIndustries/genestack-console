"""What: Push Env Config Document. Render the environment config document and write the
files to its config dir (over the ssh executor when a deploy host is set), backing up
pre-existing files first.
Where: app/modules/genestack/config_push.py. GenestackModule lists this file.
Why: One file so this step does not grow the job runner.
"""

from __future__ import annotations

from app.services.crypto import encrypt_secret

HANDLERS = ("genestack_config_push",)

OPERATION = {
    "id": "genestack.config.push",
    "name": "Push Env Config Document",
    "description": (
        "Render the environment config document and write "
        "the files to its config dir (over the ssh executor when a deploy "
        "host is set), backing up pre-existing files first."
    ),
    "required_role": "operator",
    "backend": "genestack",
    "params": [],
    "handler": "genestack_config_push",
    "mutating": True,
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
            "error": "genestack.config.push requires an environment",
            "returncode": 2,
        }
    from app.services import envconfig as envconfig_service
    from app.services.crypto import encrypt_secret

    current = envconfig_service.get_current(self.db, env)
    if current is None:
        msg = (
            "Cannot push config: no config document exists for environment '{name}'. "
            "Store a config first with PUT /api/v1/environments/{env_id}/config, "
            "then re-run this job."
        ).format(name=env.name, env_id=env.id)
        log(f"[config.push] {msg}")
        return {"ok": False, "error": msg, "returncode": 2}
    doc, row = current
    log(
        f"[config.push] Rendering config version {row.version} for environment '{env.name}'..."
    )
    try:
        files = envconfig_service.render_to_files(doc, env, self.settings)
        log(
            f"[config.push] Rendered {len(files)} file(s) from config version {row.version}"
        )
        result = envconfig_service.push_rendered(files, ctx, log, dry)
    except envconfig_service.ConfigValidationError as exc:
        log(f"[config.push] Configuration error: {exc}")
        return {
            "ok": False,
            "error": f"Config validation error in version {row.version}: {exc}",
            "returncode": 2,
            "version": row.version,
        }

    if not dry:
        # Sync deploy/maas doc sections onto the Environment row so the
        # execution context and MAAS client keep working.
        deploy = doc.get("deploy")
        if isinstance(deploy, dict):
            if deploy.get("ssh_host") is not None:
                env.deployer_ssh_host = deploy["ssh_host"]
            if deploy.get("ssh_user") is not None:
                env.deployer_ssh_user = deploy["ssh_user"]
        maas_doc = doc.get("maas")
        if isinstance(maas_doc, dict):
            if maas_doc.get("url") is not None:
                env.maas_url = maas_doc["url"]
            if maas_doc.get("api_key"):
                env.maas_api_key_encrypted = encrypt_secret(
                    maas_doc["api_key"], self.settings
                )
        self.db.add(env)
        self.db.flush()

    self.write_audit(
        actor=job.created_by or "system",
        action="env.config.push",
        resource_type="environment",
        resource_id=env.id,
        environment_id=env.id,
        details={
            "version": row.version,
            "files": result["count"],
            "bytes": result["bytes"],
            "dry_run": dry,
        },
    )
    result["version"] = row.version
    return result
