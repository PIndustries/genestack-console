"""What: Say hello. Return a greeting. This sample does not change the deploy host.
Where: examples/modules/hello/say.py. HelloModule lists this file.
Why: one file so a custom operation does not grow the job runner.
"""

from __future__ import annotations

from app.modules.params import p as _p

HANDLERS = ("hello_say",)

OPERATION = {
    "id": "hello.say",
    "name": "Say hello",
    "description": "Return a greeting. This sample does not change the deploy host.",
    "required_role": "viewer",
    "backend": "internal",
    "params": [
        _p("name", False, "Who to greet", default="world"),
    ],
    "handler": "hello_say",
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
    who = str((params or {}).get("name") or "world").strip() or "world"
    log(f"hello {who}")
    return {"ok": True, "message": f"hello {who}", "dry_run": bool(dry)}
