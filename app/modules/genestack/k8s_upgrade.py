"""What: Kubernetes Upgrade (kubespray). Run kubespray's upgrade-cluster.yml from the
genestack kubespray submodule against the environment inventory, per
docs/k8s-kubespray-upgrade.md.
Where: app/modules/genestack/k8s_upgrade.py. GenestackModule lists this file.
Why: One file so this step does not grow the job runner.
"""

from __future__ import annotations

from pathlib import Path

from app.modules.params import p as _p
from app.services import genestack_bridge as bridge

HANDLERS = ("genestack_k8s_upgrade",)

OPERATION = {
    "id": "genestack.k8s_upgrade",
    "name": "Kubernetes Upgrade (kubespray)",
    "description": (
        "Run kubespray's upgrade-cluster.yml from the genestack kubespray "
        "submodule against the environment inventory, per "
        "docs/k8s-kubespray-upgrade.md. One major version jump per run; "
        "2+ hours is normal. The target version comes from the inventory "
        "group_vars k8s_cluster.kube_version file — when kube_version is "
        "given, the console rewrites that line in "
        "<config_dir>/inventory/group_vars/k8s_cluster/k8s-cluster.yml "
        "(backing up the original as k8s-cluster.yml.bak-<timestamp> "
        "next to it) before running the playbook; on dry-run it only "
        "logs the intended change."
    ),
    "required_role": "admin",
    "backend": "genestack",
    "params": [
        _p(
            "kube_version",
            False,
            "Target Kubernetes version (e.g. 1.31.4) — written to "
            "k8s_cluster.kube_version in the env inventory group_vars "
            "k8s-cluster.yml before the upgrade (original backed up as "
            "k8s-cluster.yml.bak-<timestamp>); when omitted the "
            "inventory's current value is used as-is",
        ),
    ],
    "handler": "genestack_k8s_upgrade",
    "mutating": True,
    "timeout_seconds": 21600,
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
    from app.services.job_runner import (
        K8sUpgradeFileError,
        _kube_version_rewritten,
        _write_group_vars_with_backup,
    )

    if env is None or ctx.config_dir is None:
        return {
            "ok": False,
            "error": (
                "genestack.k8s_upgrade requires an environment with a "
                "genestack_config_dir (inventory source)"
            ),
            "returncode": 2,
        }
    kubespray_dir = gs_root / "submodules" / "kubespray"
    if ssh_target is None and not kubespray_dir.is_dir():
        # Local execution only — remote runs fail on the deploy host
        # and surface the ansible rc instead.
        msg = (
            f"kubespray submodule not found at {kubespray_dir} — "
            "init it first (git submodule update --init submodules/kubespray)"
        )
        log(f"[missing] {msg}")
        return {"ok": False, "error": msg, "returncode": 2}
    kube_version = str(params.get("kube_version") or "").strip()
    # Source of truth for the target version: the inventory
    # group_vars k8s_cluster.kube_version file. When kube_version is
    # given the console rewrites that line before running the
    # playbook, with a .bak-<ts> copy of the original next to it
    # (envconfig push backup style).
    group_vars_file = (
        ctx.config_dir
        / "inventory"
        / "group_vars"
        / "k8s_cluster"
        / "k8s-cluster.yml"
    )
    kube_version_old: str | None = None
    kube_version_backup: Path | None = None
    if kube_version:
        if dry:
            log(
                f"[k8s-upgrade] would set kube_version={kube_version} in {group_vars_file}"
            )
        else:
            try:
                kube_version_old, new_text = _kube_version_rewritten(
                    group_vars_file, kube_version, ssh_target, agent_env_id, log
                )
                kube_version_backup = _write_group_vars_with_backup(
                    group_vars_file, new_text, ssh_target, agent_env_id, log
                )
            except K8sUpgradeFileError as exc:
                log(f"[denied] {exc}")
                return {"ok": False, "error": str(exc), "returncode": 2}
    inventory = ctx.config_dir / "inventory"
    log(
        f"[k8s-upgrade] kubespray upgrade-cluster.yml inventory={inventory} dry_run={dry}"
    )
    result = bridge.run_command(
        [
            "ansible-playbook",
            "upgrade-cluster.yml",
            "--become",
            "-i",
            str(inventory),
        ],
        cwd=kubespray_dir,
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
        action="env.k8s_upgrade",
        resource_type="environment",
        resource_id=env.id,
        environment_id=env.id,
        details={
            "kube_version": kube_version or None,
            "kube_version_previous": kube_version_old,
            "group_vars_file": str(group_vars_file),
            "group_vars_backup": (
                str(kube_version_backup) if kube_version_backup else None
            ),
            "inventory": str(inventory),
            "kubespray_dir": str(kubespray_dir),
            "returncode": rc,
            "dry_run": dry,
        },
        success=ok,
    )
    message = (
        "k8s upgrade completed"
        if ok
        else f"k8s upgrade failed (rc={rc}) — see log"
    )
    if kube_version and not dry and ok:
        message += f" (kube_version set {kube_version_old} -> {kube_version})"
    return {
        "ok": ok,
        "returncode": rc,
        "dry_run": bool(result.get("dry_run", dry)),
        "message": message,
    }
