"""What: Tempest conformance suite. Run genestack's official OpenStack conformance suite
(tempest).
Where: app/modules/genestack/tempest.py. GenestackModule lists this file.
Why: One file so this step does not grow the job runner.
"""

from __future__ import annotations

from typing import Any

from app.modules.params import p as _p
from app.services import genestack_bridge as bridge

HANDLERS = ("genestack_tempest",)

OPERATION = {
    "id": "genestack.tempest",
    "name": "Tempest conformance suite",
    "description": (
        "Run genestack's official OpenStack conformance suite (tempest). "
        "action=install deploys the openstack-helm tempest chart (release "
        "tempest, namespace openstack) via bin/install-tempest.sh without "
        "running tests; action=run re-installs with "
        "manifests.job_run_tests=true so Helm waits on the real "
        "tempest-run-tests job; install-run (default) does both. suite "
        "narrows the test scope by passing two chart values "
        "to the install phase (install-tempest.sh forwards extra args to "
        "helm): conf.whitelist[0] (the chart renders it into the "
        "tempest-etc secret, mounted at /etc/tempest/test-whitelist) and "
        "a rewritten conf.script ('tempest run --include-list "
        "/etc/tempest/test-whitelist --exclude-list ... -w 4') — the "
        "deployed script only consumes the blacklist plus --smoke, so "
        "without the rewrite the whitelist file would be mounted but "
        "ignored. suite=full (or omitting suite) keeps the chart default: "
        "the test-blacklist plus --smoke. suite requires action=install "
        "or install-run (a bare helm test re-runs the deployed values)."
    ),
    "required_role": "operator",
    "backend": "genestack",
    "params": [
        _p(
            "action",
            False,
            "install, run, or install-run (default)",
            default="install-run",
            enum=["install", "run", "install-run"],
        ),
        _p(
            "suite",
            False,
            "Test suite scope: a tempest test regex (e.g. "
            "tempest\\.scenario\\.test_server_basic_ops). Applied via "
            "conf.whitelist[0] + a conf.script rewrite to "
            "--include-list during the install phase; 'full' (or "
            "omitted) keeps the chart default (test-blacklist + "
            "--smoke). Not usable with action=run.",
        ),
    ],
    "handler": "genestack_tempest",
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
    action = str(params.get("action") or "install-run").strip().lower()
    if action not in ("install", "run", "install-run"):
        msg = (
            f"Invalid tempest action '{action}' — "
            "must be one of: install, run, install-run"
        )
        log(f"[denied] {msg}")
        return {"ok": False, "error": msg, "returncode": 2, "action": action}
    suite = str(params.get("suite") or "").strip()
    suite_helm_args: list[str] = []
    if suite and suite != "full":
        # The run_tests job executes /tmp/run-tests.sh, whose body is
        # literally {{ .Values.conf.script }}. The deployed
        # conf.script (base-helm-configs/tempest) is:
        #   tempest run --exclude-list /etc/tempest/test-blacklist \
        #       --config-file /etc/tempest/tempest.conf -w 4 --smoke
        # i.e. it only consumes the *blacklist* file plus --smoke.
        # The chart separately renders conf.whitelist into the
        # tempest-etc secret and mounts it at
        # /etc/tempest/test-whitelist (job-run-tests.yaml), but the
        # script never points --include-list at it — so setting
        # conf.whitelist ALONE would mount the file but run the full
        # --smoke set (a silent no-op). Scoping a suite therefore
        # needs BOTH chart values, passed to the install phase
        # (install-tempest.sh forwards extra args to helm):
        #   conf.whitelist[0]  -> creates + mounts the include file
        #   conf.script        -> tempest run --include-list
        #                          /etc/tempest/test-whitelist
        # --include-list is the modern tempest flag for the include
        # file (tempest/cmd/run.py: --whitelist-file is deprecated
        # and ignored when --include-list is present). The regex is
        # applied by stestr on top of the still-mounted blacklist.
        # `full` (or an empty suite) keeps the chart default:
        # blacklist + --smoke.
        suite_helm_args = [
            "--set",
            f"conf.whitelist[0]={suite}",
            "--set",
            "conf.script=tempest run --include-list /etc/tempest/test-whitelist --exclude-list /etc/tempest/test-blacklist --config-file /etc/tempest/tempest.conf -w 4",
        ]
        log(
            f"[tempest] suite={suite}: install phase passes "
            f"conf.whitelist[0]={suite} and rewrites conf.script to "
            "tempest run --include-list /etc/tempest/test-whitelist "
            "(the mounted include file the chart would otherwise ignore)"
        )
    elif suite == "full":
        log(
            "[tempest] suite=full: chart default runs (test-blacklist + --smoke)"
        )
    log(f"[tempest] action={action} dry_run={dry}")

    phases: list[tuple[str, list[str]]] = []
    if action in ("install", "install-run"):
        # Deploys the openstack-helm tempest chart without running the
        # suite (install-tempest.sh sets manifests.job_run_tests=false).
        phases.append(
            ("install", ["bash", "bin/install-tempest.sh", *suite_helm_args])
        )
    if action in ("run", "install-run"):
        # job_run_tests is a post-install helm hook, not a `helm test`
        # suite — `helm test tempest` reports TEST SUITE: None. Re-run
        # the install script with the hook enabled so helm waits on
        # the real tempest-run-tests job. Trailing --set wins.
        phases.append(
            (
                "test",
                [
                    "bash",
                    "bin/install-tempest.sh",
                    *suite_helm_args,
                    "--set",
                    "manifests.job_run_tests=true",
                ],
            )
        )

    rc: int | None = 0
    ok = True
    message = f"tempest {action} completed"
    for phase, cmd in phases:
        result = bridge.run_command(
            cmd,
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
        if not ok:
            message = f"tempest {phase} failed rc={rc}"
            log(f"[tempest] {message}")
            break
    self.write_audit(
        actor=job.created_by or "system",
        action="env.tempest",
        resource_type="environment",
        resource_id=env.id if env else None,
        environment_id=env.id if env else None,
        details={
            "action": action,
            "suite": suite or None,
            "returncode": rc,
            "dry_run": dry,
        },
        success=ok,
    )
    out: dict[str, Any] = {
        "ok": ok,
        "action": action,
        "returncode": rc,
        "dry_run": dry,
        "message": message,
    }
    if suite:
        out["suite"] = suite
    return out
