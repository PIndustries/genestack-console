"""Deploy-host preparation orchestration (genestack.host_prepare).

Takes a fresh deploy host to "ready for push + deploy", mirroring exactly
what docs/genestack-getting-started.md tells operators to do by hand:

  1. preflight — git must be present (fatal); ansible-playbook may be absent
     (warning only — bootstrap.sh installs ansible into ~/.venvs/genestack).
  2. repo — ``git clone --recurse-submodules <repo_url> <genestack_path>``
     when absent; when a checkout already exists only ``git -C <path> fetch``
     runs (never a pull — an existing checkout's branch is left as-is).
  3. checkout — ``git -C <path> checkout <repo_ref>`` only on a fresh clone.
  4. bootstrap — ``sudo -n -E bash <genestack_path>/bootstrap.sh`` with
     GENESTACK_CONFIG set to the env's config dir (bootstrap.sh sources
     scripts/genestack.rc, which honors GENESTACK_CONFIG, defaulting to
     /etc/genestack). ``sudo -n`` because ssh runs BatchMode (no tty).
  5. verify — provider file + inventory dir + helm-configs dir exist under
     the config dir.

Steps stop at the first failure. dry_run logs every command and executes
nothing (the fresh-clone path is planned, since a dry run cannot probe).

Kept out of job_runner so the dispatcher stays thin; the handler only wires
ctx/params and writes the audit entry.
"""

from __future__ import annotations

import shlex
from typing import Any, Callable

from app.models import Environment
from app.services import genestack_bridge as bridge
from app.services.envcontext import EnvContext

LogFn = Callable[[str], None]

DEFAULT_REPO_URL = "https://github.com/rackerlabs/genestack"
DEFAULT_REPO_REF = "main"
DEFAULT_GENESTACK_PATH = "/opt/genestack"
DEFAULT_CONFIG_DIR = "/etc/genestack"


def _resolve_params(env: Environment | None, params: dict[str, Any]) -> dict[str, str]:
    meta = (env.metadata_json or {}) if env else {}
    return {
        "repo_url": str(
            params.get("repo_url") or meta.get("repo_url") or DEFAULT_REPO_URL
        ),
        "repo_ref": str(params.get("repo_ref") or DEFAULT_REPO_REF),
        # env.genestack_path is the console-side checkout root, not the
        # remote deploy-host clone path — only an explicit param overrides.
        "genestack_path": str(params.get("genestack_path") or DEFAULT_GENESTACK_PATH),
        "config_dir": str(
            params.get("config_dir")
            or (env.genestack_config_dir if env else None)
            or DEFAULT_CONFIG_DIR
        ),
    }


def run_host_prepare(
    env: Environment | None,
    ctx: EnvContext,  # noqa: ARG001 — paths resolve from params/env, not ctx
    log: LogFn,
    *,
    params: dict[str, Any],
    dry_run: bool,
    timeout: int,
    extra_env: dict[str, str],
    ssh_target: str | None,
    remote_env: dict[str, str],
    agent_env_id: str | None = None,
) -> dict[str, Any]:
    """Run the prepare sequence on the deploy host (over ssh when configured)."""
    resolved = _resolve_params(env, params)
    repo_url = resolved["repo_url"]
    repo_ref = resolved["repo_ref"]
    genestack_path = resolved["genestack_path"]
    config_dir = resolved["config_dir"]
    log(
        f"[prepare] repo={repo_url} ref={repo_ref} path={genestack_path} "
        f"config_dir={config_dir} dry_run={dry_run}"
    )

    steps: list[dict[str, Any]] = []

    def _fail(message: str, returncode: int | None) -> dict[str, Any]:
        log(f"[prepare] FAILED {message}")
        return {
            "ok": False,
            "error": message,
            "returncode": returncode if returncode not in (0, None) else 1,
            "steps": steps,
            "dry_run": dry_run,
            **resolved,
        }

    def _step(
        name: str,
        script: str,
        *,
        step_extra_env: dict[str, str] | None = None,
        step_remote_env: dict[str, str] | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Run one bash step; returns (result, failed)."""
        log(f"[prepare] step {len(steps) + 1}: {name}")
        result = bridge.run_command(
            ["bash", "-c", script],
            timeout=timeout,
            dry_run=dry_run,
            extra_env=step_extra_env if step_extra_env is not None else extra_env,
            ssh_target=ssh_target,
            remote_env=step_remote_env if step_remote_env is not None else remote_env,
            agent_env_id=agent_env_id,
            log=log,
        )
        rc = result.get("returncode")
        steps.append(
            {
                "step": name,
                "command": script,
                "returncode": rc,
                "dry_run": bool(result.get("dry_run", dry_run)),
            }
        )
        failed = rc not in (0, None) and not result.get("dry_run")
        return result, failed

    # Step: preflight — git is required; ansible is installed by bootstrap.sh
    result, failed = _step("preflight-git", "command -v git")
    if failed:
        return _fail(
            "preflight failed: git not found on target host — install git first",
            result.get("returncode"),
        )
    result, failed = _step("preflight-ansible", "command -v ansible-playbook")
    if failed:
        log(
            "[prepare] WARNING ansible-playbook not found — "
            "bootstrap.sh installs ansible into ~/.venvs/genestack"
        )

    # Step: repo — clone when absent (checkout the ref only on a fresh
    # clone); an existing checkout gets a fetch only, branch untouched.
    quoted_path = shlex.quote(genestack_path)
    fresh = True
    if dry_run:
        log(
            "[dry-run] cannot probe for an existing checkout — planning fresh-clone path"
        )
    else:
        probe, _ = _step("repo-probe", f"test -d {quoted_path}")
        fresh = probe.get("returncode") not in (0, None)
    if fresh:
        result, failed = _step(
            "repo-clone",
            f"git clone --recurse-submodules {shlex.quote(repo_url)} {quoted_path}",
        )
        if failed:
            return _fail(f"git clone of {repo_url} failed", result.get("returncode"))
        result, failed = _step(
            "repo-checkout",
            f"git -C {quoted_path} checkout {shlex.quote(repo_ref)}",
        )
        if failed:
            return _fail(
                f"git checkout of ref '{repo_ref}' failed", result.get("returncode")
            )
    else:
        log(
            f"[prepare] existing checkout at {genestack_path} — fetch only, branch untouched"
        )
        result, failed = _step("repo-fetch", f"git -C {quoted_path} fetch")
        if failed:
            log(
                "[prepare] WARNING git fetch failed — continuing with the existing checkout"
            )

    # Step: bootstrap — GENESTACK_CONFIG points bootstrap.sh at the env's
    # config dir (honored via scripts/genestack.rc; defaults to /etc/genestack).
    log(f"[prepare] bootstrap env: GENESTACK_CONFIG={config_dir}")
    result, failed = _step(
        "bootstrap",
        f"sudo -n -E bash {quoted_path}/bootstrap.sh",
        step_extra_env={**extra_env, "GENESTACK_CONFIG": config_dir},
        step_remote_env={**remote_env, "GENESTACK_CONFIG": config_dir},
    )
    if failed:
        return _fail("bootstrap.sh failed — see log", result.get("returncode"))

    # Step: verify the config-dir skeleton bootstrap.sh should have built
    quoted_config = shlex.quote(config_dir)
    result, failed = _step(
        "verify",
        f"test -f {quoted_config}/provider && "
        f"test -d {quoted_config}/inventory && "
        f"test -d {quoted_config}/helm-configs",
    )
    if failed:
        return _fail(
            f"verify failed: config skeleton incomplete under {config_dir} "
            "(need provider file, inventory/ and helm-configs/)",
            result.get("returncode"),
        )

    message = (
        f"[dry-run] host prepare planned for {genestack_path} (nothing executed)"
        if dry_run
        else f"host prepared: {genestack_path} + {config_dir} ready for push + deploy"
    )
    log(f"[prepare] {message}")
    return {
        "ok": True,
        "steps": steps,
        "returncode": 0,
        "dry_run": dry_run,
        "message": message,
        **resolved,
    }
