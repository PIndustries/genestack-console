"""What: Export State to Repo. Render the environment's current config document and write
the files under state/<env>/ in the repo checkout configured by state_repo_path,
committing (and pushing to state_repo_remote when set) the result.
Where: app/modules/genestack/state_export.py. GenestackModule lists this file.
Why: One file so this step does not grow the job runner.
"""

from __future__ import annotations

from pathlib import Path

HANDLERS = ("genestack_state_export",)

OPERATION = {
    "id": "genestack.state.export",
    "name": "Export State to Repo",
    "description": (
        "Render the environment's current config document "
        "and write the files under state/<env>/ in the repo checkout "
        "configured by state_repo_path, committing (and pushing to "
        "state_repo_remote when set) the result. When the environment "
        "resolves to a deploy host (connected agent, else ssh), the git "
        "work happens there instead of on the console host. Secret files "
        "(kubesecrets.yaml, .ssh keys) are intentionally excluded — the "
        "repo holds non-secret config; secrets remain in the console "
        "database."
    ),
    "required_role": "operator",
    "backend": "genestack",
    "params": [],
    "handler": "genestack_state_export",
    "mutating": True,
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
    from app.services.job_runner import clamp_deadline_timeout

    if env is None:
        return {
            "ok": False,
            "error": "genestack.state.export requires an environment",
            "returncode": 2,
        }
    import fcntl
    import subprocess

    from app.services import envconfig as envconfig_service
    from app.services.executors import pick_executor

    state_repo_path = (env.state_repo_path or "").strip()
    if not state_repo_path:
        return {
            "ok": False,
            "error": "genestack.state.export requires state_repo_path to be set on the environment",
            "returncode": 2,
        }
    if "/" in env.name or env.name.startswith("-") or env.name in (".", ".."):
        log(
            f"[state.export] environment name '{env.name}' is not safe as a path segment"
        )
        return {
            "ok": False,
            "error": f"environment name '{env.name}' is not safe as a state repo path segment",
            "returncode": 2,
        }

    executor = pick_executor(env, ctx, self.settings)
    agent_env_id = executor.agent_env_id
    repo_root = Path(state_repo_path).expanduser()

    # The checkout is owned by the deploy host (connected agent, else ssh):
    # run the whole git flow there. The local .git check / fcntl lock below
    # apply only when the console host owns the checkout.
    if ctx.ssh_target or agent_env_id:
        remote = (env.state_repo_remote or "").strip()
        return self._state_export_remote(
            env,
            job,
            params,
            ctx,
            log,
            dry,
            timeout,
            deadline,
            executor,
            remote,
        )

    if not repo_root.is_dir() or not (repo_root / ".git").exists():
        log(
            f"[state.export] state_repo_path '{state_repo_path}' is not a git checkout"
        )
        return {
            "ok": False,
            "error": f"state_repo_path '{state_repo_path}' is not a git checkout (missing .git)",
            "returncode": 2,
        }

    # Serialize exports across environments sharing this checkout:
    # concurrent git add/commit would race on .git/index.
    lockfile = open(repo_root / ".git" / "console-state-export.lock", "a+")
    try:
        fcntl.flock(lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        lockfile.close()
        log(
            "[state.export] another state export is already running on this repo"
        )
        return {
            "ok": False,
            "error": "another state export is already running on this repo",
            "returncode": 1,
        }
    try:
        proc = subprocess.run(
            ["git", "symbolic-ref", "-q", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=clamp_deadline_timeout(timeout, deadline),
        )
        if proc.returncode != 0:
            log(
                f"[state.export] state repo '{repo_root}' is in detached HEAD state"
            )
            return {
                "ok": False,
                "error": f"state repo '{repo_root}' is in detached HEAD state — check out a branch (e.g. 'git checkout main') and re-run",
                "returncode": 2,
            }

        current = envconfig_service.get_current(self.db, env)
        if current is None:
            msg = (
                "Cannot export state: no config document exists for environment '{name}'. "
                "Store a config first with PUT /api/v1/environments/{env_id}/config, "
                "then re-run this job."
            ).format(name=env.name, env_id=env.id)
            log(f"[state.export] {msg}")
            return {"ok": False, "error": msg, "returncode": 2}
        doc, row = current
        log(
            f"[state.export] Rendering config version {row.version} for environment '{env.name}'..."
        )
        try:
            files = envconfig_service.render_to_files(
                doc, env, self.settings, include_secrets=False
            )
        except envconfig_service.ConfigValidationError as exc:
            log(f"[state.export] Configuration error: {exc}")
            return {
                "ok": False,
                "error": f"Config validation error in version {row.version}: {exc}",
                "returncode": 2,
                "version": row.version,
            }
        excluded_secrets = (
            1
            if isinstance(doc.get("secrets"), dict) and doc.get("secrets")
            else 0
        ) + (1 if env.ssh_private_key_encrypted else 0)
        if excluded_secrets:
            log(
                f"[state.export] excluded {excluded_secrets} secret file(s) from state export "
                "(kubesecrets.yaml / .ssh — secrets stay in the DB)"
            )
        log(
            f"[state.export] Rendered {len(files)} file(s) from config version {row.version}"
        )

        state_dir = repo_root / "state" / env.name
        file_sizes = {
            path: len(content.encode("utf-8"))
            for path, content in files.items()
        }
        total_bytes = sum(file_sizes.values())
        commit_msg = f"state({env.name}): export config version {row.version}"

        if dry:
            for path in sorted(files):
                log(
                    f"[state.export] dry-run: would write {state_dir / path} ({file_sizes[path]} bytes)"
                )
            log(f"[state.export] dry-run: would commit '{commit_msg}'")
            remote = (env.state_repo_remote or "").strip()
            if remote:
                log(f"[state.export] dry-run: would push to remote '{remote}'")
            else:
                log("[state.export] dry-run: no remote configured, commit only")
            self.write_audit(
                actor=job.created_by or "system",
                action="env.state.export",
                resource_type="environment",
                resource_id=env.id,
                environment_id=env.id,
                details={
                    "version": row.version,
                    "files": len(files),
                    "bytes": total_bytes,
                    "commit": None,
                    "pushed": False,
                    "dry_run": True,
                },
            )
            return {
                "ok": True,
                "dry_run": True,
                "version": row.version,
                "files": len(files),
                "bytes": total_bytes,
                "state_dir": str(state_dir),
            }

        for path, content in files.items():
            target = state_dir / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            target.chmod(0o644)
            log(f"[state.export] wrote {target} ({file_sizes[path]} bytes)")

        try:
            proc = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=repo_root,
                capture_output=True,
                text=True,
                timeout=clamp_deadline_timeout(timeout, deadline),
            )
            if proc.returncode != 0:
                raise OSError(f"git rev-parse failed: {proc.stderr.strip()}")
            branch = proc.stdout.strip()
            log(f"[state.export] git: on branch '{branch}'")

            proc = subprocess.run(
                ["git", "add", "--", f"state/{env.name}"],
                cwd=repo_root,
                capture_output=True,
                text=True,
                timeout=clamp_deadline_timeout(timeout, deadline),
            )
            if proc.returncode != 0:
                raise OSError(f"git add failed: {proc.stderr.strip()}")

            proc = subprocess.run(
                [
                    "git",
                    "diff",
                    "--cached",
                    "--quiet",
                    "--",
                    f"state/{env.name}",
                ],
                cwd=repo_root,
                capture_output=True,
                text=True,
                timeout=clamp_deadline_timeout(timeout, deadline),
            )
            if proc.returncode == 0:
                log("[state.export] no changes staged, skipping commit")
                self.write_audit(
                    actor=job.created_by or "system",
                    action="env.state.export",
                    resource_type="environment",
                    resource_id=env.id,
                    environment_id=env.id,
                    details={
                        "version": row.version,
                        "files": len(files),
                        "bytes": total_bytes,
                        "commit": None,
                        "pushed": False,
                        "dry_run": False,
                    },
                )
                return {
                    "ok": True,
                    "dry_run": False,
                    "version": row.version,
                    "files": len(files),
                    "bytes": total_bytes,
                    "state_dir": str(state_dir),
                    "committed": False,
                    "commit": None,
                    "pushed": False,
                    "remote": (env.state_repo_remote or "").strip() or None,
                }

            proc = subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=genestack-console",
                    "-c",
                    "user.email=console@localhost",
                    "commit",
                    "-m",
                    commit_msg,
                    "--",
                    f"state/{env.name}",
                ],
                cwd=repo_root,
                capture_output=True,
                text=True,
                timeout=clamp_deadline_timeout(timeout, deadline),
            )
            if proc.returncode != 0:
                raise OSError(f"git commit failed: {proc.stderr.strip()}")

            proc = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo_root,
                capture_output=True,
                text=True,
                timeout=clamp_deadline_timeout(timeout, deadline),
            )
            if proc.returncode != 0:
                raise OSError(
                    f"git rev-parse HEAD failed: {proc.stderr.strip()}"
                )
            sha = proc.stdout.strip()
            log(f"[state.export] committed {sha} ({commit_msg})")

            pushed = False
            remote = (env.state_repo_remote or "").strip()
            if remote:
                proc = subprocess.run(
                    ["git", "push", remote, branch],
                    cwd=repo_root,
                    capture_output=True,
                    text=True,
                    timeout=clamp_deadline_timeout(timeout, deadline),
                )
                if proc.returncode != 0:
                    log(
                        f"[state.export] git push failed: {proc.stderr.strip()}"
                    )
                    self.write_audit(
                        actor=job.created_by or "system",
                        action="env.state.export",
                        resource_type="environment",
                        resource_id=env.id,
                        environment_id=env.id,
                        details={
                            "version": row.version,
                            "files": len(files),
                            "bytes": total_bytes,
                            "commit": sha,
                            "pushed": False,
                            "dry_run": False,
                        },
                        success=False,
                    )
                    return {
                        "ok": False,
                        "error": f"git push failed: {proc.stderr.strip()}",
                        "returncode": proc.returncode,
                        "version": row.version,
                        "committed": True,
                        "commit": sha,
                        "pushed": False,
                        "remote": remote,
                    }
                pushed = True
                log(f"[state.export] pushed {branch} to remote '{remote}'")
        except (subprocess.TimeoutExpired, OSError) as exc:
            log(f"[state.export] git error: {exc}")
            self.write_audit(
                actor=job.created_by or "system",
                action="env.state.export",
                resource_type="environment",
                resource_id=env.id,
                environment_id=env.id,
                details={
                    "version": row.version,
                    "files": len(files),
                    "bytes": total_bytes,
                    "commit": None,
                    "pushed": False,
                    "dry_run": False,
                },
                success=False,
            )
            return {"ok": False, "error": str(exc), "returncode": 1}

        self.write_audit(
            actor=job.created_by or "system",
            action="env.state.export",
            resource_type="environment",
            resource_id=env.id,
            environment_id=env.id,
            details={
                "version": row.version,
                "files": len(files),
                "bytes": total_bytes,
                "commit": sha,
                "pushed": pushed,
                "dry_run": False,
            },
        )
        return {
            "ok": True,
            "dry_run": False,
            "version": row.version,
            "files": len(files),
            "bytes": total_bytes,
            "state_dir": str(state_dir),
            "committed": True,
            "commit": sha,
            "pushed": pushed,
            "remote": remote,
        }
    finally:
        fcntl.flock(lockfile, fcntl.LOCK_UN)
        lockfile.close()
