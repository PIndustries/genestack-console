"""What: Compile and publish Console binary. On this hub: compile the Console to a Linux
binary under dist/.
Where: app/modules/console/release.py. ConsoleModule lists this file.
Why: One file so this step does not grow the job runner.
"""

from __future__ import annotations

from app.services import genestack_bridge as bridge

HANDLERS = ("console_release",)

OPERATION = {
    "id": "console.release",
    "name": "Compile and publish Console binary",
    "description": (
        "On this hub: compile the Console to a Linux binary under dist/. "
        "Published installs download the GitHub Release asset. "
        "Runs locally on the hub (not on an environment)."
    ),
    "required_role": "admin",
    "backend": "internal",
    "params": [],
    "handler": "console_release",
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
    from app.paths import package_root

    script = package_root() / "scripts" / "compile-console.sh"
    log(f"[release] compile + publish via {script}")
    if dry:
        return {
            "ok": True,
            "dry_run": True,
            "message": "would compile the Console binary into dist/",
        }
    if not script.is_file():
        return {
            "ok": False,
            "error": f"compile script not found: {script}",
            "returncode": 2,
        }
    result = bridge.run_command(
        ["bash", str(script)],
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
        action="console.release",
        resource_type="console",
        resource_id="binary",
        environment_id=None,
        details={"returncode": rc, "dry_run": False},
        success=ok,
    )
    return {
        "ok": ok,
        "returncode": rc,
        "message": (
            "binary written under dist/"
            if ok
            else f"compile/publish failed (rc={rc}) — see log"
        ),
    }
