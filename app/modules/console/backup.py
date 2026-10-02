"""What: Backup console database + config. Run scripts/backup-console.sh on this hub:
online SQLite backup (or pg_dump) plus config.yaml into <backup_dir>/<UTC-stamp>/ with
--keep retention (default 7).
Where: app/modules/console/backup.py. ConsoleModule lists this file.
Why: One file so this step does not grow the job runner.
"""

from __future__ import annotations

from app.modules.params import p as _p
from app.services import genestack_bridge as bridge

HANDLERS = ("console_backup",)

OPERATION = {
    "id": "console.backup",
    "name": "Backup console database + config",
    "description": (
        "Run scripts/backup-console.sh on this hub: online "
        "SQLite backup (or pg_dump) plus config.yaml into "
        "<backup_dir>/<UTC-stamp>/ with --keep retention (default 7). "
        "Env-less (console host only). Dry-run prints destination and "
        "retention only. This operation does not schedule itself."
    ),
    "required_role": "operator",
    "backend": "internal",
    "params": [
        _p(
            "backup_dir",
            False,
            "Destination root for stamp dirs (default: <host_prefix>/backups/console)",
        ),
        _p(
            "keep",
            False,
            "Retention: newest N stamp directories to keep (default 7)",
            "integer",
            default=7,
        ),
        _p(
            "dry_run",
            False,
            "Force a dry-run rehearsal regardless of global dry-run pin",
            "boolean",
        ),
    ],
    "handler": "console_backup",
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
    from app.paths import host_prefix, package_root

    script = package_root() / "scripts" / "backup-console.sh"
    backup_dir = str(
        params.get("backup_dir") or (host_prefix() / "backups" / "console")
    ).strip()
    keep_raw = params.get("keep", 7)
    try:
        keep = int(keep_raw)
    except (TypeError, ValueError):
        return {
            "ok": False,
            "error": f"keep must be a positive integer (got: {keep_raw!r})",
            "returncode": 2,
        }
    if keep < 1:
        return {
            "ok": False,
            "error": f"keep must be >= 1 (got: {keep})",
            "returncode": 2,
        }
    config = str(self.settings.config_path)
    effective_dry = dry or bool(params.get("dry_run"))
    argv = [
        "bash",
        str(script),
        backup_dir,
        "--config",
        config,
        "--keep",
        str(keep),
    ]
    log(
        f"[console.backup] dir={backup_dir} keep={keep} "
        f"config={config} dry_run={effective_dry}"
    )
    if effective_dry:
        self.write_audit(
            actor=job.created_by or "system",
            action="console.backup",
            resource_type="console",
            resource_id="database",
            environment_id=None,
            details={
                "script": "scripts/backup-console.sh",
                "backup_dir": backup_dir,
                "keep": keep,
                "config": config,
                "dry_run": True,
            },
            success=True,
        )
        return {
            "ok": True,
            "dry_run": True,
            "backup_dir": backup_dir,
            "keep": keep,
            "config": config,
            "message": (
                f"would run backup-console.sh → {backup_dir} "
                f"(keep={keep}); no files written"
            ),
        }
    if not script.is_file():
        return {
            "ok": False,
            "error": f"backup script not found: {script}",
            "returncode": 2,
        }
    result = bridge.run_command(
        argv,
        cwd=package_root(),
        timeout=timeout,
        dry_run=False,
        extra_env=extra_env,
        ssh_target=None,
        agent_env_id=None,
        log=log,
    )
    rc = result.get("returncode")
    ok = rc == 0
    self.write_audit(
        actor=job.created_by or "system",
        action="console.backup",
        resource_type="console",
        resource_id="database",
        environment_id=None,
        details={
            "script": "scripts/backup-console.sh",
            "backup_dir": backup_dir,
            "keep": keep,
            "returncode": rc,
            "dry_run": False,
        },
        success=ok,
    )
    return {
        "ok": ok,
        "returncode": rc,
        "backup_dir": backup_dir,
        "keep": keep,
        "dry_run": False,
        "message": (
            f"console backup complete → {backup_dir} (keep={keep})"
            if ok
            else f"console backup failed (rc={rc}) — see log"
        ),
    }
