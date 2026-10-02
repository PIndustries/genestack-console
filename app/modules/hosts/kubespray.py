"""What: Record an existing Kubespray cluster on this environment.
Where: app/modules/hosts/kubespray.py. HostsModule lists this file.
Why: One file so this step does not grow the job runner.
"""

from __future__ import annotations

from app.modules.params import p as _p

HANDLERS = ("hosts_kubespray_adopt",)

OPERATION = {
    "id": "hosts.kubespray.adopt",
    "name": "Adopt Kubespray Cluster",
    "description": (
        "Record a Kubernetes cluster that Kubespray already built. "
        "When kubeconfig text is passed and this is not a dry run, it is "
        "Fernet-encrypted onto the environment. The console then uses the "
        "existing SSH and kubectl path. This does not clone Kubespray and "
        "does not run Ansible."
    ),
    "required_role": "admin",
    "backend": "internal",
    "params": [
        _p(
            "kubeconfig",
            False,
            "Kubeconfig text to store on the environment (encrypted at rest)",
        ),
    ],
    "secret_params": ("kubeconfig",),
    "handler": "hosts_kubespray_adopt",
    "mutating": True,
    "timeout_seconds": 120,
}

_MANAGED = "The cluster is managed with the existing SSH and kubectl path."


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
    text = str(params.get("kubeconfig") or "").strip()
    if dry:
        log("[kubespray] dry run: kubeconfig was not written")
        return {
            "ok": True,
            "dry_run": True,
            "wrote_kubeconfig": False,
            "would_store_kubeconfig": bool(text),
            "message": f"Dry run: kubeconfig was not written. {_MANAGED}",
        }
    if env is None:
        return {
            "ok": False,
            "dry_run": False,
            "wrote_kubeconfig": False,
            "error": "hosts.kubespray.adopt requires an environment",
        }
    wrote = False
    if text:
        from app.services.crypto import encrypt_secret
        from app.services.env_field_guards import refuse_kubeconfig_exec_plugins

        try:
            cleaned = refuse_kubeconfig_exec_plugins(text)
        except ValueError as exc:
            msg = str(exc)
            if "exec" in msg:
                error = msg
            elif "YAML" in msg:
                error = "kubeconfig must be valid YAML"
            else:
                error = "kubeconfig was refused"
            log("[kubespray] kubeconfig refused")
            return {
                "ok": False,
                "dry_run": False,
                "wrote_kubeconfig": False,
                "error": error,
                "returncode": 2,
            }
        except TypeError:
            log("[kubespray] kubeconfig refused")
            return {
                "ok": False,
                "dry_run": False,
                "wrote_kubeconfig": False,
                "error": "kubeconfig must be a mapping",
                "returncode": 2,
            }
        settings = getattr(self, "settings", None) if self is not None else None
        env.kubeconfig_data = encrypt_secret(cleaned, settings)
        wrote = True
        db = getattr(self, "db", None) if self is not None else None
        if db is not None:
            db.add(env)
            db.flush()
        log("[kubespray] stored kubeconfig on the environment")
    else:
        log("[kubespray] no kubeconfig passed")
    log(f"[kubespray] {_MANAGED}")
    audit = getattr(self, "write_audit", None) if self is not None else None
    if callable(audit) and job is not None:
        audit(
            actor=getattr(job, "created_by", None) or "system",
            action="env.kubespray.adopt",
            resource_type="environment",
            resource_id=getattr(env, "id", None),
            environment_id=getattr(env, "id", None),
            details={"wrote_kubeconfig": wrote, "dry_run": False},
            success=True,
        )
    return {
        "ok": True,
        "dry_run": False,
        "wrote_kubeconfig": wrote,
        "message": f"Kubespray cluster recorded. {_MANAGED}",
    }
