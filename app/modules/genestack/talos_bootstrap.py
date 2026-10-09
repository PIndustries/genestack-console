"""What: Talos Bootstrap (provider=talos). Bring up Kubernetes the talos way for a
provider=talos env, per docs/k8s-talos.md: talosctl gen config, apply-config to each
control-plane and worker node, set endpoints, bootstrap etcd (once), then fetch the
kubeconfig — all from <config_dir>/talos on the deploy host.
Where: app/modules/genestack/talos_bootstrap.py. GenestackModule lists this file.
Why: One file so this step does not grow the job runner.
"""

from __future__ import annotations

HANDLERS = ("genestack_talos_bootstrap",)

OPERATION = {
    "id": "genestack.talos.bootstrap",
    "name": "Talos Bootstrap (provider=talos)",
    "description": (
        "Bring up Kubernetes the talos way for a provider=talos env, per "
        "docs/k8s-talos.md: talosctl gen config, apply-config to each "
        "control-plane and worker node, set endpoints, bootstrap etcd "
        "(once), then fetch the kubeconfig — all from <config_dir>/talos "
        "on the deploy host. Control-plane nodes are doc servers with role "
        "k8s_control_plane; every other server is a worker. Cluster name "
        "and install disk come from the doc's talos: section (defaults: "
        "env name, /dev/sda). Note: pin kube-ovn to v1.14.10 for talos and "
        "boot nodes from a Talos Image Factory image with iscsi-tools + "
        "util-linux-tools extensions. Stops at the first failing phase. "
        "A machine whose certificate does not match the saved config is "
        "rebooted into the installer when this console has its management "
        "port. Otherwise the job names the one step left and waits for you "
        "to confirm that step."
    ),
    "required_role": "admin",
    "backend": "genestack",
    "params": [],
    "handler": "genestack_talos_bootstrap",
    "mutating": True,
    "timeout_seconds": 7200,
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
            "error": "genestack.talos.bootstrap requires an environment",
            "returncode": 2,
        }
    from app.services import envconfig as envconfig_service
    from app.services import talos as talos_service

    current = envconfig_service.get_current(self.db, env)
    if current is None:
        msg = (
            "no config document yet — PUT "
            f"/api/v1/environments/{env.id}/config first"
        )
        log(f"[talos] {msg}")
        return {"ok": False, "error": msg, "returncode": 2}
    try:
        result = talos_service.run_talos_bootstrap(
            current[0],
            env,
            log,
            dry_run=dry,
            timeout=timeout,
            extra_env=extra_env,
            ssh_target=ssh_target,
            remote_env=remote_env,
            agent_env_id=agent_env_id,
            db=self.db,
        )
    except envconfig_service.ConfigValidationError as exc:
        log(f"[talos] {exc}")
        return {"ok": False, "error": str(exc), "returncode": 2}
    self.write_audit(
        actor=job.created_by or "system",
        action="env.talos_bootstrap",
        resource_type="environment",
        resource_id=env.id,
        environment_id=env.id,
        details={
            "cluster_name": result.get("cluster_name"),
            "install_disk": result.get("install_disk"),
            "control_planes": result.get("control_planes"),
            "workers": result.get("workers"),
            "failed_phase": result.get("failed_phase"),
            "dry_run": dry,
        },
        success=bool(result.get("ok")),
    )
    return result
