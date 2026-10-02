"""What: Vacuum console SQLite database. Run scripts/vacuum-console.sh on this hub: PRAGMA
wal_checkpoint(TRUNCATE) then VACUUM (SQLite).
Where: app/modules/console/vacuum.py. ConsoleModule lists this file.
Why: One file so this step does not grow the job runner.
"""

from __future__ import annotations

from app.modules.params import p as _p
from app.services import genestack_bridge as bridge

HANDLERS = ("console_vacuum",)

OPERATION = {
    "id": "console.vacuum",
    "name": "Vacuum console SQLite database",
    "description": (
        "Run scripts/vacuum-console.sh on this hub: "
        "PRAGMA wal_checkpoint(TRUNCATE) then VACUUM (SQLite). Dry-run "
        "reports page_count/freelist stats only — no mutate. Admin-only. "
        "Prefer running retention sweep before vacuum; take console.backup "
        "after. This operation does not schedule itself."
    ),
    "required_role": "admin",
    "backend": "internal",
    "params": [
        _p(
            "dry_run",
            False,
            "Force stats-only rehearsal regardless of global dry-run pin",
            "boolean",
        ),
    ],
    "handler": "console_vacuum",
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
    from app.paths import package_root

    script = package_root() / "scripts" / "vacuum-console.sh"
    config = str(self.settings.config_path)
    effective_dry = dry or bool(params.get("dry_run"))
    argv = ["bash", str(script), "--config", config]
    if effective_dry:
        argv.append("--dry-run")
    log(
        f"[console.vacuum] config={config} dry_run={effective_dry} "
        f"script={script}"
    )
    if not script.is_file():
        return {
            "ok": False,
            "error": f"vacuum script not found: {script}",
            "returncode": 2,
            "dry_run": effective_dry,
        }
    # Script owns dry-run vs live: --dry-run is stats-only (no VACUUM).
    # Always invoke locally on the console host (never SSH/agent).
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
        action="console.vacuum",
        resource_type="console",
        resource_id="database",
        environment_id=None,
        details={
            "script": "scripts/vacuum-console.sh",
            "config": config,
            "returncode": rc,
            "dry_run": effective_dry,
        },
        success=ok,
    )
    message = (
        "console vacuum dry-run (freelist/page stats only) — see log"
        if effective_dry and ok
        else (
            "console vacuum complete"
            if ok
            else f"console vacuum failed (rc={rc}) — see log"
        )
    )
    return {
        "ok": ok,
        "returncode": rc,
        "dry_run": effective_dry,
        "message": message,
    }
