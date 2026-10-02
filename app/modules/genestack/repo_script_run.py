"""What: Run Genestack Repo Script (curated). Run a curated genestack utility script as
'bash scripts/<name> [args]' from GENESTACK_ROOT.
Where: app/modules/genestack/repo_script_run.py. GenestackModule lists this file.
Why: One file so this step does not grow the job runner.
"""

from __future__ import annotations

import shlex
from pathlib import Path

from app.modules.params import p as _p
from app.services import genestack_bridge as bridge
from app.services.repo_scripts import SAFE_REPO_SCRIPTS

HANDLERS = ("genestack_repo_script_run",)

OPERATION = {
    "id": "genestack.repo_script.run",
    "name": "Run Genestack Repo Script (curated)",
    "description": (
        "Run a curated genestack utility script as 'bash scripts/<name> "
        "[args]' from GENESTACK_ROOT. The script must exist under scripts/ "
        "and be in SAFE_REPO_SCRIPTS (app/services/repo_scripts.py — the "
        f"extension point). Allowlist: {', '.join(sorted(SAFE_REPO_SCRIPTS))}."
    ),
    "required_role": "operator",
    "backend": "genestack",
    "params": [
        _p("script", True, "Script basename under scripts/ (must be allowlisted)"),
        _p("args", False, "Extra arguments appended verbatim (shlex-split)"),
    ],
    "handler": "genestack_repo_script_run",
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
    from app.services import repo_scripts

    script = str(params.get("script", "")).strip()
    name = Path(script).name
    if not name or name != script:
        msg = f"Invalid script name '{script}' — basename only (no path separators)"
        log(f"[denied] {msg}")
        return {"ok": False, "error": msg, "returncode": 2}
    discovered = {
        s["name"] for s in repo_scripts.list_repo_scripts(gs_root)["scripts"]
    }
    if name not in discovered or name not in repo_scripts.SAFE_REPO_SCRIPTS:
        allowed = ", ".join(sorted(repo_scripts.SAFE_REPO_SCRIPTS))
        msg = (
            f"Script '{name}' is not runnable: it must exist under "
            f"{gs_root / 'scripts'} and be in the allowlist: {allowed}"
        )
        log(f"[denied] {msg}")
        return {"ok": False, "error": msg, "returncode": 2}

    args_str = str(params.get("args") or "").strip()
    argv = ["bash", f"scripts/{name}"]
    if args_str:
        argv.extend(shlex.split(args_str))
    log(
        f"[repo-script] running scripts/{name} args={args_str or '-'} dry_run={dry}"
    )
    result = bridge.run_command(
        argv,
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
    self.write_audit(
        actor=job.created_by or "system",
        action="env.repo_script_run",
        resource_type="environment" if env else "genestack_root",
        resource_id=env.id if env else str(gs_root),
        environment_id=env.id if env else None,
        details={
            "script": f"scripts/{name}",
            "args": args_str or None,
            "returncode": rc,
            "dry_run": dry,
        },
        success=ok,
    )
    return {
        "ok": ok,
        "script": name,
        "args": args_str or None,
        "returncode": rc,
        "dry_run": bool(result.get("dry_run", dry)),
        "message": (
            f"repo script {name} completed"
            if ok
            else f"repo script {name} failed (rc={rc}) — see log"
        ),
    }
