"""What: Verify environment (genestack test suite). Run genestack's own
scripts/tests/run-all-tests.sh as the official 'did it work' check.
Where: app/modules/genestack/verify.py. GenestackModule lists this file.
Why: One file so this step does not grow the job runner.
"""

from __future__ import annotations

from app.modules.params import p as _p
from app.services import genestack_bridge as bridge

HANDLERS = ("genestack_verify",)

OPERATION = {
    "id": "genestack.verify",
    "name": "Verify environment (genestack test suite)",
    "description": (
        "Run genestack's own scripts/tests/run-all-tests.sh as the official "
        "'did it work' check. Levels: quick, standard (default), full "
        "(full provisions real test resources, then cleans up)."
    ),
    "required_role": "operator",
    "backend": "genestack",
    "params": [
        _p("level", False, "Test level: quick, standard (default), or full"),
    ],
    "handler": "genestack_verify",
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
    level = str(params.get("level") or "standard").strip().lower()
    if level not in ("quick", "standard", "full"):
        msg = (
            f"Invalid verify level '{level}' — "
            "must be one of: quick, standard, full"
        )
        log(f"[denied] {msg}")
        return {"ok": False, "error": msg, "returncode": 2, "level": level}
    log(f"[verify] running genestack test suite level={level} dry_run={dry}")
    result = bridge.run_command(
        ["bash", "scripts/tests/run-all-tests.sh", level],
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
    message = (
        f"verify {level} passed"
        if ok
        else f"verify {level} failed (rc={rc}) — see log"
    )
    return {
        "ok": ok,
        "level": level,
        "returncode": rc,
        "dry_run": bool(result.get("dry_run", dry)),
        "message": message,
    }
