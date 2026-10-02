"""What: Deploy Hyperconverged Lab. Run genestack's DESTRUCTIVE lab deployer
scripts/hyperconverged-lab.sh with a chosen platform: kubespray (Ubuntu VMs +
kubespray/ansible) or talos (Talos Linux).
Where: app/modules/genestack/hyperconverged_lab.py. GenestackModule lists this file.
Why: One file so this step does not grow the job runner.
"""

from __future__ import annotations

import shlex

from app.modules.params import p as _p
from app.services import genestack_bridge as bridge

HANDLERS = ("genestack_hyperconverged_lab",)

OPERATION = {
    "id": "genestack.hyperconverged_lab",
    "name": "Deploy Hyperconverged Lab",
    "description": (
        "Run genestack's DESTRUCTIVE lab deployer scripts/hyperconverged-lab.sh "
        "with a chosen platform: kubespray (Ubuntu VMs + kubespray/ansible) "
        "or talos (Talos Linux). Deploys a full hyperconverged OpenStack-on-"
        "Kubernetes lab on OpenStack infrastructure — it provisions VMs, "
        "k8s, and OpenStack, so only point it at a disposable lab cloud. "
        "include restricts which OpenStack services get installed (-i flag); "
        "extra_args is an advanced pass-through of additional script flags "
        "(they are wordsplit, quoted where needed). dry_run (or the env/"
        "global dry-run pin) logs the exact command without executing it."
    ),
    "required_role": "admin",
    "backend": "genestack",
    "params": [
        _p("platform", True, "kubespray or talos", enum=["kubespray", "talos"]),
        _p(
            "include",
            False,
            "Comma-separated OpenStack services to include (script -i flag)",
        ),
        _p(
            "extra_args",
            False,
            "Advanced: extra script flags, e.g. '-x --envoy-gateway-config' "
            "(space-separated, wordsplit by the console)",
        ),
        _p(
            "dry_run",
            False,
            "Force a dry-run rehearsal regardless of env setting",
            "boolean",
        ),
    ],
    "handler": "genestack_hyperconverged_lab",
    "mutating": True,
    "timeout_seconds": 14400,
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
    # DESTRUCTIVE lab deployer (scripts/hyperconverged-lab.sh) — like
    # genestack_deploy, the env/global dry-run pin (ctx.dry_run) always
    # wins; params.dry_run may only force a rehearsal on top of it,
    # never LIVE execution on a dry-run-pinned environment.
    effective_dry = dry or bool(params.get("dry_run"))
    platform = str(params.get("platform") or "").strip().lower()
    if platform not in ("kubespray", "talos"):
        msg = (
            f"Invalid hyperconverged-lab platform '{platform}' — "
            "must be one of: kubespray, talos"
        )
        log(f"[denied] {msg}")
        return {
            "ok": False,
            "error": msg,
            "returncode": 2,
            "platform": platform,
        }
    include = str(params.get("include") or "").strip()
    extra_args = str(params.get("extra_args") or "").strip()
    argv = ["bash", "scripts/hyperconverged-lab.sh", platform]
    if include:
        argv += ["-i", include]
    if extra_args:
        argv += shlex.split(extra_args)
    log(
        f"[hyperconverged] platform={platform} include={include or '-'} extra_args={extra_args or '-'} dry_run={effective_dry}"
    )
    result = bridge.run_command(
        argv,
        cwd=gs_root,
        timeout=timeout,
        dry_run=effective_dry,
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
        action="env.hyperconverged_lab",
        resource_type="environment" if env else "genestack_root",
        resource_id=env.id if env else str(gs_root),
        environment_id=env.id if env else None,
        details={
            "platform": platform,
            "include": include or None,
            "extra_args": extra_args or None,
            "script": "scripts/hyperconverged-lab.sh",
            "returncode": rc,
            "dry_run": effective_dry,
        },
        success=ok,
    )
    return {
        "ok": ok,
        "platform": platform,
        "include": include or None,
        "returncode": rc,
        "dry_run": bool(result.get("dry_run", effective_dry)),
        "message": (
            "hyperconverged-lab completed"
            if ok
            else f"hyperconverged-lab failed (rc={rc}) — see log"
        ),
    }
