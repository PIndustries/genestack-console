"""What: Backup MariaDB. Run genestack's scripts/backup-mariadb.sh: dumps every database in
the openstack namespace mariadb cluster (except performance_schema and
information_schema) to $HOME/backup/mariadb/<timestamp> on the host it runs on.
Where: app/modules/genestack/backup_mariadb.py. GenestackModule lists this file.
Why: One file so this step does not grow the job runner.
"""

from __future__ import annotations

from app.services import genestack_bridge as bridge

HANDLERS = ("genestack_backup_mariadb",)

OPERATION = {
    "id": "genestack.backup_mariadb",
    "name": "Backup MariaDB",
    "description": (
        "Run genestack's scripts/backup-mariadb.sh: dumps every database in "
        "the openstack namespace mariadb cluster (except performance_schema "
        "and information_schema) to $HOME/backup/mariadb/<timestamp> on the "
        "host it runs on."
    ),
    "required_role": "operator",
    "backend": "genestack",
    "params": [],
    "handler": "genestack_backup_mariadb",
    "mutating": True,
    "timeout_seconds": 3600,
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
            "error": "genestack.backup_mariadb requires an environment",
            "returncode": 2,
        }
    log(f"[backup] running scripts/backup-mariadb.sh dry_run={dry}")
    result = bridge.run_command(
        ["bash", "scripts/backup-mariadb.sh"],
        cwd=gs_root,
        timeout=timeout,
        dry_run=dry,
        extra_env=extra_env,
        ssh_target=ssh_target,
        remote_env=remote_env,
        agent_env_id=agent_env_id,
        log=log,
    )
    rc = result.get("returncode")
    ok = bool(result.get("dry_run")) or rc == 0
    # The script dumps each database (minus performance/information_schema)
    # into $HOME/backup/mariadb/<epoch>/ on the host it runs on.
    message = (
        "mariadb backup complete — dumps at $HOME/backup/mariadb/<timestamp> "
        "on the deploy host"
        if ok
        else f"mariadb backup failed (rc={rc}) — see log"
    )
    self.write_audit(
        actor=job.created_by or "system",
        action="env.backup_mariadb",
        resource_type="environment",
        resource_id=env.id,
        environment_id=env.id,
        details={
            "script": "scripts/backup-mariadb.sh",
            "returncode": rc,
            "dry_run": dry,
        },
        success=ok,
    )
    return {
        "ok": ok,
        "returncode": rc,
        "dry_run": bool(result.get("dry_run", dry)),
        "message": message,
    }
