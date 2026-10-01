"""genestack.state.export job tests (git-backed state export)."""

from __future__ import annotations

import fcntl
import os
import stat
import subprocess
import uuid
from pathlib import Path

from sqlalchemy import select

from app.db import SessionLocal
from app.models import AuditLog, Environment, JobStatus
from app.schemas import EnvironmentRead
from app.services import envconfig as envconfig_service
from app.services.catalog import get_operation, mutating_operation_ids
from app.services.job_runner import JobRunner
from app.services.ssh_keys import store_key_pair

DOC = """\
provider: kubespray
"""

SECRETS_DOC = """\
provider: kubespray
secrets:
  db-pw:
    data:
      password: s3cret
"""


def _suffix() -> str:
    return uuid.uuid4().hex[:8]


def _create_env(client, headers, **fields) -> dict:
    resp = client.post(
        "/api/v1/environments",
        headers=headers,
        json={"name": f"env-state-{_suffix()}", **fields},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _export_job(client, headers, env_id, run_sync=True):
    resp = client.post(
        f"/api/v1/environments/{env_id}/jobs",
        headers=headers,
        json={
            "operation": "genestack.state.export",
            "params": {},
            "run_sync": run_sync,
        },
    )
    return resp


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=30
    )
    assert proc.returncode == 0, f"git {' '.join(args)} failed: {proc.stderr}"
    return proc.stdout.strip()


def _init_repo(path: Path) -> Path:
    """A git checkout with an initial commit so HEAD exists."""
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-b", "main")
    (path / "README.md").write_text("state repo\n", encoding="utf-8")
    _git(path, "add", "README.md")
    _git(
        path,
        "-c",
        "user.name=state-test",
        "-c",
        "user.email=state-test@example.com",
        "commit",
        "-m",
        "initial",
    )
    return path


def _init_bare_remote(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "--bare", "-b", "main")
    return path


def _run_export(env_id: str, created_by: str = "state-test") -> tuple:
    """Create + run a state.export job synchronously. Returns (job, audit rows)."""
    db = SessionLocal()
    try:
        runner = JobRunner(db)
        job = runner.create_job(
            operation="genestack.state.export",
            params={},
            environment_id=env_id,
            created_by=created_by,
        )
        db.commit()
        job = runner.run_job(job)
        audits = list(
            db.scalars(
                select(AuditLog)
                .where(AuditLog.action == "env.state.export")
                .where(AuditLog.environment_id == env_id)
                .order_by(AuditLog.id)
            ).all()
        )
        for a in audits:
            db.expunge(a)
        return job, audits
    finally:
        db.close()


def _db_env(**fields) -> Environment:
    fields.setdefault("name", f"env-state-{_suffix()}")
    db = SessionLocal()
    try:
        env = Environment(**fields)
        db.add(env)
        db.commit()
        db.refresh(env)
        return env
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Catalog + schema
# ---------------------------------------------------------------------------


def test_catalog_operation_registered():
    op = get_operation("genestack.state.export")
    assert op is not None, "genestack.state.export missing from catalog"
    assert op.handler == "genestack_state_export"
    assert op.required_role == "operator"
    assert op.backend == "genestack"
    assert op.params == []
    assert op.mutating is True
    assert "genestack.state.export" in mutating_operation_ids()


def test_schema_roundtrip(client, admin_headers, tmp_path):
    repo = _init_repo(tmp_path / "repo")
    remote = _init_bare_remote(tmp_path / "remote")
    env = _create_env(client, admin_headers)

    resp = client.patch(
        f"/api/v1/environments/{env['id']}",
        headers=admin_headers,
        json={"state_repo_path": str(repo), "state_repo_remote": str(remote)},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["state_repo_path"] == str(repo)
    assert resp.json()["state_repo_remote"] == str(remote)

    got = client.get(f"/api/v1/environments/{env['id']}", headers=admin_headers)
    assert got.status_code == 200, got.text
    assert got.json()["state_repo_path"] == str(repo)
    assert got.json()["state_repo_remote"] == str(remote)

    # Direct from_orm_env check
    db = SessionLocal()
    try:
        row = db.get(Environment, env["id"])
        read = EnvironmentRead.from_orm_env(row)
        assert read.state_repo_path == str(repo)
        assert read.state_repo_remote == str(remote)
    finally:
        db.close()


def test_schema_defaults_null(client, admin_headers):
    env = _create_env(client, admin_headers)
    body = client.get(f"/api/v1/environments/{env['id']}", headers=admin_headers).json()
    assert body["state_repo_path"] is None
    assert body["state_repo_remote"] is None


# ---------------------------------------------------------------------------
# Error paths (direct runner invocation)
# ---------------------------------------------------------------------------


def test_export_without_environment_fails(client, admin_headers):
    resp = client.post(
        "/api/v1/jobs",
        headers=admin_headers,
        json={"operation": "genestack.state.export", "params": {}, "run_sync": True},
    )
    assert resp.status_code == 201, resp.text
    job = resp.json()
    assert job["status"] == "failed"
    assert "requires an environment" in job["error"]


def test_export_without_state_repo_path_fails(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    env = _db_env(state_repo_path=None)
    db = SessionLocal()
    try:
        envconfig_service.put_version(db, env, DOC, "state-test")
        db.commit()
        runner = JobRunner(db)
        job = runner.create_job(
            operation="genestack.state.export",
            params={},
            environment_id=env.id,
            created_by="state-test",
        )
        db.commit()
        job = runner.run_job(job)
        assert job.status == JobStatus.failed
        assert "state_repo_path" in (job.error or "")
    finally:
        db.close()
    # Nothing written into the repo
    assert not (repo / "state").exists()


def test_export_not_a_git_checkout_fails(tmp_path):
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()
    env = _db_env(state_repo_path=str(not_a_repo))
    job, _ = _run_export(env.id)
    assert job.status == JobStatus.failed
    assert "not a git checkout" in (job.error or "")
    assert not (not_a_repo / "state").exists()

    missing = _db_env(state_repo_path=str(tmp_path / "does-not-exist"))
    job, _ = _run_export(missing.id)
    assert job.status == JobStatus.failed
    assert "not a git checkout" in (job.error or "")


def test_export_unsafe_env_name_rejected(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    for bad_name in ("env-state/with-slash", "-leading-dash", ".", ".."):
        env = _db_env(name=bad_name, state_repo_path=str(repo))
        job, _ = _run_export(env.id)
        assert job.status == JobStatus.failed, bad_name
        assert "not safe" in (job.error or ""), bad_name
    assert not (repo / "state").exists()


def test_export_without_config_document_fails(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    env = _db_env(state_repo_path=str(repo))
    job, _ = _run_export(env.id)
    assert job.status == JobStatus.failed
    assert "no config document" in (job.error or "")
    assert not (repo / "state").exists()


# ---------------------------------------------------------------------------
# Dry run (global test config is dry_run=True; env.dry_run None inherits it)
# ---------------------------------------------------------------------------


def test_export_dry_run_writes_nothing_commits_nothing(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    head_before = _git(repo, "rev-parse", "HEAD")

    env = _db_env(state_repo_path=str(repo))
    assert env.dry_run is None  # inherits global dry_run=True
    db = SessionLocal()
    try:
        row, _ = envconfig_service.put_version(db, env, DOC, "state-test")
        db.commit()
        version = row.version
    finally:
        db.close()

    job, audits = _run_export(env.id)
    assert job.status == JobStatus.success, job.error
    assert job.dry_run is True

    log_text = job.log_text or ""
    assert f"version {version}" in log_text
    assert f"dry-run: would write {repo / 'state' / env.name / 'provider'}" in log_text
    assert "would commit" in log_text
    assert "no remote configured, commit only" in log_text

    # No file writes, no git calls that mutate the repo
    assert not (repo / "state").exists()
    assert _git(repo, "rev-parse", "HEAD") == head_before
    assert _git(repo, "status", "--porcelain") == ""

    # Audit recorded as a successful rehearsal
    assert len(audits) == 1
    assert audits[0].success is True
    details = audits[0].details
    assert details["version"] == version
    assert details["files"] == 1
    assert details["bytes"] == len("kubespray\n")
    assert details["commit"] is None
    assert details["pushed"] is False
    assert details["dry_run"] is True


def test_export_dry_run_with_remote_logs_push(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    remote = _init_bare_remote(tmp_path / "remote")
    env = _db_env(state_repo_path=str(repo), state_repo_remote=str(remote))
    db = SessionLocal()
    try:
        envconfig_service.put_version(db, env, DOC, "state-test")
        db.commit()
    finally:
        db.close()

    job, audits = _run_export(env.id)
    assert job.status == JobStatus.success, job.error
    assert f"would push to remote '{remote}'" in (job.log_text or "")
    assert audits[0].details["dry_run"] is True
    # The bare remote received nothing
    assert _git(remote, "for-each-ref") == ""


# ---------------------------------------------------------------------------
# Real run: commit
# ---------------------------------------------------------------------------


def test_export_writes_files_and_commits(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    head_before = _git(repo, "rev-parse", "HEAD")

    env = _db_env(state_repo_path=str(repo), dry_run=False)
    db = SessionLocal()
    try:
        row, _ = envconfig_service.put_version(db, env, DOC, "state-test")
        db.commit()
        version = row.version
    finally:
        db.close()

    job, audits = _run_export(env.id)
    assert job.status == JobStatus.success, job.error
    assert job.dry_run is False

    # Rendered file landed under state/<env.name>/ with 0644
    target = repo / "state" / env.name / "provider"
    assert target.read_text(encoding="utf-8") == "kubespray\n"
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o644

    # Exactly one new commit, with the expected message
    head_after = _git(repo, "rev-parse", "HEAD")
    assert head_after != head_before
    assert _git(repo, "rev-list", "--count", "HEAD") == "2"
    assert _git(repo, "log", "-1", "--format=%s") == (
        f"state({env.name}): export config version {version}"
    )
    # Committed author comes from the handler's -c fallbacks
    assert _git(repo, "log", "-1", "--format=%an") == "genestack-console"

    # Working tree clean: only the state subtree was added
    assert _git(repo, "status", "--porcelain") == ""

    # Audit carries the commit sha and no push
    assert len(audits) == 1
    assert audits[0].success is True
    details = audits[0].details
    assert details["version"] == version
    assert details["files"] == 1
    assert details["commit"] == head_after
    assert details["pushed"] is False
    assert details["dry_run"] is False


def test_export_second_run_is_noop(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    env = _db_env(state_repo_path=str(repo), dry_run=False)
    db = SessionLocal()
    try:
        envconfig_service.put_version(db, env, DOC, "state-test")
        db.commit()
    finally:
        db.close()

    first, _ = _run_export(env.id)
    assert first.status == JobStatus.success, first.error
    head = _git(repo, "rev-parse", "HEAD")

    second, audits = _run_export(env.id)
    assert second.status == JobStatus.success, second.error
    assert "no changes staged, skipping commit" in (second.log_text or "")
    assert _git(repo, "rev-parse", "HEAD") == head
    assert _git(repo, "rev-list", "--count", "HEAD") == "2"
    # Second audit recorded the no-op (no commit, no push)
    assert len(audits) == 2
    assert audits[1].success is True
    assert audits[1].details["commit"] is None
    assert audits[1].details["pushed"] is False


# ---------------------------------------------------------------------------
# Push
# ---------------------------------------------------------------------------


def test_export_pushes_to_bare_remote(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    remote = _init_bare_remote(tmp_path / "remote")
    env = _db_env(
        state_repo_path=str(repo), state_repo_remote=str(remote), dry_run=False
    )
    db = SessionLocal()
    try:
        row, _ = envconfig_service.put_version(db, env, DOC, "state-test")
        db.commit()
        version = row.version
    finally:
        db.close()

    job, audits = _run_export(env.id)
    assert job.status == JobStatus.success, job.error
    assert f"pushed main to remote '{remote}'" in (job.log_text or "")

    # The remote's HEAD now contains the state subtree
    assert _git(remote, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    files = _git(remote, "ls-tree", "-r", "--name-only", "main").splitlines()
    assert f"state/{env.name}/provider" in files
    assert _git(remote, "log", "-1", "--format=%s") == (
        f"state({env.name}): export config version {version}"
    )

    assert audits[0].success is True
    assert audits[0].details["pushed"] is True
    assert audits[0].details["commit"] == _git(remote, "rev-parse", "HEAD")


def test_export_push_failure_commits_but_fails_job(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    bad_remote = "/nonexistent-dir-for-state-export/xyz.git"
    env = _db_env(
        state_repo_path=str(repo), state_repo_remote=bad_remote, dry_run=False
    )
    db = SessionLocal()
    try:
        envconfig_service.put_version(db, env, DOC, "state-test")
        db.commit()
    finally:
        db.close()

    job, audits = _run_export(env.id)
    assert job.status == JobStatus.failed
    assert "git push failed" in (job.error or "")

    # The commit still landed locally
    head = _git(repo, "rev-parse", "HEAD")
    assert _git(repo, "rev-list", "--count", "HEAD") == "2"
    assert (repo / "state" / env.name / "provider").exists()

    # Audit records the failure with the commit sha, pushed False
    assert len(audits) == 1
    assert audits[0].success is False
    assert audits[0].details["commit"] == head
    assert audits[0].details["pushed"] is False


# ---------------------------------------------------------------------------
# API plumbing: job creation + roles
# ---------------------------------------------------------------------------


def test_export_job_via_api_dry_run(client, admin_headers, tmp_path):
    repo = _init_repo(tmp_path / "repo")
    env = _create_env(client, admin_headers)
    patch = client.patch(
        f"/api/v1/environments/{env['id']}",
        headers=admin_headers,
        json={"state_repo_path": str(repo)},
    )
    assert patch.status_code == 200, patch.text

    put = client.put(
        f"/api/v1/environments/{env['id']}/config",
        headers=admin_headers,
        json={"yaml_text": DOC},
    )
    assert put.status_code == 201, put.text

    resp = _export_job(client, admin_headers, env["id"])
    assert resp.status_code == 201, resp.text
    job = resp.json()
    assert job["status"] == "success", job["error"]
    assert job["dry_run"] is True
    assert not (repo / "state").exists()


def test_export_job_viewer_forbidden(client, admin_headers, viewer_headers, tmp_path):
    repo = _init_repo(tmp_path / "repo")
    env = _create_env(client, admin_headers)
    client.patch(
        f"/api/v1/environments/{env['id']}",
        headers=admin_headers,
        json={"state_repo_path": str(repo)},
    )
    resp = _export_job(client, viewer_headers, env["id"])
    assert resp.status_code == 403


def _init_detached_repo(path: Path) -> Path:
    """Repo with an initial commit, then detached (git checkout --detach HEAD)."""
    repo = _init_repo(path)
    _git(repo, "checkout", "--detach", "HEAD")
    return repo


def _stage_unrelated_file(repo: Path, content: str = "unrelated\n") -> Path:
    p = repo / "somefile.txt"
    p.write_text(content, encoding="utf-8")
    _git(repo, "add", "somefile.txt")
    return p


def _export_result(env_id: str, created_by: str = "state-test") -> dict:
    """Run the state.export handler directly; return the raw result dict.

    The job row is marked finished afterward (a bare ``_dispatch`` leaves it
    ``queued``), so a follow-up run for the same env is not blocked by the
    mutating-job conflict guard.
    """
    from app.services.envcontext import build_context

    db = SessionLocal()
    try:
        runner = JobRunner(db)
        env = db.get(Environment, env_id)
        job = runner.create_job(
            operation="genestack.state.export",
            params={},
            environment_id=env_id,
            created_by=created_by,
        )
        db.commit()
        result = runner._dispatch(
            get_operation("genestack.state.export"),
            job,
            env,
            log=lambda _msg: None,
            ctx=build_context(env, runner.settings),
            params={},
            deadline=None,
            check_cancel=lambda: None,
        )
        job.status = JobStatus.success if result.get("ok") else JobStatus.failed
        job.error = result.get("error")
        db.add(job)
        db.commit()
        return result
    finally:
        db.close()


# ---------------------------------------------------------------------------
# A. Secrets are excluded from the git-backed state export
# ---------------------------------------------------------------------------


def test_render_to_files_include_secrets_toggle():
    """A: the state-export render (include_secrets=False) omits kubesecrets.yaml
    and .ssh/*, while the config-push render (default include_secrets=True)
    keeps them. Non-secret sections render either way."""
    from app.config import get_settings

    settings = get_settings()
    env = Environment(id=str(uuid.uuid4()), name=f"env-render-{_suffix()}")
    store_key_pair(env, comment="state-test")
    assert env.ssh_private_key_encrypted and env.ssh_public_key

    doc = {
        "provider": "kubespray",
        "secrets": {"db-pw": {"data": {"password": "plain"}}},
    }

    # config-push path: render_to_files(doc, env, settings) — include_secrets default True
    files = envconfig_service.render_to_files(doc, env, settings)
    assert files["provider"] == "kubespray\n"
    assert "kubesecrets.yaml" in files
    assert ".ssh/id_ed25519" in files
    assert files[".ssh/id_ed25519"].startswith("-----BEGIN OPENSSH PRIVATE KEY-----")
    assert files[".ssh/id_ed25519.pub"].strip() == env.ssh_public_key.strip()

    # state-export path: include_secrets=False — no secret files at all
    files = envconfig_service.render_to_files(doc, env, settings, include_secrets=False)
    assert "kubesecrets.yaml" not in files
    assert not any(p.startswith(".ssh/") for p in files)
    # non-secret sections still render
    assert files == {"provider": "kubespray\n"}


def test_export_excludes_secrets_files(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    env = _db_env(state_repo_path=str(repo), dry_run=False)
    # env key pair => .ssh files excluded; persist to the DB row
    db = SessionLocal()
    try:
        row = db.get(Environment, env.id)
        store_key_pair(row, comment="state-test")
        envconfig_service.put_version(db, row, SECRETS_DOC, "state-test")
        db.commit()
    finally:
        db.close()

    job, audits = _run_export(env.id)
    assert job.status == JobStatus.success, job.error

    log_text = job.log_text or ""
    # doc secrets (1) + key pair (1) = 2 excluded files
    assert "[state.export] excluded 2 secret file(s)" in log_text

    state_dir = repo / "state" / env.name
    assert (state_dir / "provider").read_text(encoding="utf-8") == "kubespray\n"
    # Secret files never land in the repo
    assert not (state_dir / "kubesecrets.yaml").exists()
    assert not (state_dir / ".ssh").exists()
    # No secret material anywhere in the committed tree
    committed = _git(repo, "ls-tree", "-r", "--name-only", "HEAD").splitlines()
    state_files = [p for p in committed if p.startswith(f"state/{env.name}/")]
    assert state_files == [f"state/{env.name}/provider"]
    assert "s3cret" not in (state_dir / "provider").read_text(encoding="utf-8")

    # Audit only counted the non-secret file
    details = audits[-1].details
    assert details["files"] == 1
    assert details["bytes"] == len("kubespray\n")
    assert details["commit"] is not None


def test_export_excludes_secrets_log_only(tmp_path):
    """No log line when the doc has neither secrets nor a key pair."""
    repo = _init_repo(tmp_path / "repo")
    env = _db_env(state_repo_path=str(repo), dry_run=False)
    db = SessionLocal()
    try:
        envconfig_service.put_version(db, env, DOC, "state-test")
        db.commit()
    finally:
        db.close()

    job, _ = _run_export(env.id)
    assert job.status == JobStatus.success, job.error
    assert "excluded" not in (job.log_text or "")


# ---------------------------------------------------------------------------
# B. Detached HEAD is rejected
# ---------------------------------------------------------------------------


def test_export_detached_head_rejected(tmp_path):
    repo = _init_detached_repo(tmp_path / "repo")
    # sanity: HEAD is detached (symbolic-ref fails / prints nothing)
    proc = subprocess.run(
        ["git", "symbolic-ref", "-q", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0
    assert proc.stdout.strip() == ""

    env = _db_env(state_repo_path=str(repo), dry_run=False)
    db = SessionLocal()
    try:
        envconfig_service.put_version(db, env, DOC, "state-test")
        db.commit()
    finally:
        db.close()

    result = _export_result(env.id)
    assert result["ok"] is False
    assert result["returncode"] == 2
    assert "detached HEAD" in result["error"]
    # Nothing written, nothing committed
    assert not (repo / "state").exists()
    assert _git(repo, "rev-list", "--count", "HEAD") == "1"
    assert _git(repo, "status", "--porcelain") == ""


# ---------------------------------------------------------------------------
# C. Scoped pathspec commit: unrelated staged files are untouched
# ---------------------------------------------------------------------------


def test_export_commit_only_state_files_with_unrelated_staged(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    _stage_unrelated_file(repo)
    head_before = _git(repo, "rev-parse", "HEAD")

    env = _db_env(state_repo_path=str(repo), dry_run=False)
    db = SessionLocal()
    try:
        envconfig_service.put_version(db, env, DOC, "state-test")
        db.commit()
    finally:
        db.close()

    job, _ = _run_export(env.id)
    assert job.status == JobStatus.success, job.error

    head_after = _git(repo, "rev-parse", "HEAD")
    assert head_after != head_before
    # The export commit contains ONLY state/<env>/* files
    committed = _git(repo, "show", "--name-only", "--format=", head_after).splitlines()
    assert [line for line in committed if line.strip()] == [
        f"state/{env.name}/provider"
    ]
    # The unrelated file is still staged (not committed, not un-staged)
    assert _git(repo, "diff", "--cached", "--name-only") == "somefile.txt"
    # ...and never committed
    assert "somefile.txt" not in _git(repo, "log", "--all", "--name-only", "--format=")


def test_export_noop_not_triggered_by_unrelated_staged_file(tmp_path):
    """Identical re-render + unrelated staged file => scoped diff still a no-op."""
    repo = _init_repo(tmp_path / "repo")
    env = _db_env(state_repo_path=str(repo), dry_run=False)
    db = SessionLocal()
    try:
        envconfig_service.put_version(db, env, DOC, "state-test")
        db.commit()
    finally:
        db.close()

    first, _ = _run_export(env.id)
    assert first.status == JobStatus.success, first.error
    head = _git(repo, "rev-parse", "HEAD")

    _stage_unrelated_file(repo)

    second, _ = _run_export(env.id)
    assert second.status == JobStatus.success, second.error
    assert "no changes staged, skipping commit" in (second.log_text or "")
    # No new commit: the unrelated staged file did not trigger one
    assert _git(repo, "rev-parse", "HEAD") == head
    assert _git(repo, "rev-list", "--count", "HEAD") == "2"
    # Unrelated file still staged
    assert _git(repo, "diff", "--cached", "--name-only") == "somefile.txt"


# ---------------------------------------------------------------------------
# D. Per-repo flock serialization
# ---------------------------------------------------------------------------


def test_export_lock_contention_rejected(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    env = _db_env(state_repo_path=str(repo), dry_run=False)
    db = SessionLocal()
    try:
        envconfig_service.put_version(db, env, DOC, "state-test")
        db.commit()
    finally:
        db.close()

    head_before = _git(repo, "rev-parse", "HEAD")
    lock_path = repo / ".git" / "console-state-export.lock"
    lock_fh = open(lock_path, "a+")
    fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        result = _export_result(env.id)
    finally:
        fcntl.flock(lock_fh, fcntl.LOCK_UN)
        lock_fh.close()

    assert result["ok"] is False
    assert result["returncode"] == 1
    assert result["error"] == "another state export is already running on this repo"
    # No files written, no commit made
    assert not (repo / "state").exists()
    assert _git(repo, "rev-parse", "HEAD") == head_before
    assert _git(repo, "status", "--porcelain") == ""

    # After release the same export succeeds
    job, _ = _run_export(env.id)
    assert job.status == JobStatus.success, job.error
    assert _git(repo, "rev-parse", "HEAD") != head_before


# ---------------------------------------------------------------------------
# E. Schema: state repo fields are length-capped
# ---------------------------------------------------------------------------


def test_schema_state_repo_fields_max_length(client, admin_headers):
    env = _create_env(client, admin_headers)
    body = {"state_repo_path": "x" * 513, "state_repo_remote": "y" * 513}
    resp = client.patch(
        f"/api/v1/environments/{env['id']}", headers=admin_headers, json=body
    )
    assert resp.status_code == 422, resp.text
    locs = {tuple(e.get("loc", ())) for e in resp.json()["detail"]}
    assert ("body", "state_repo_path") in locs
    assert ("body", "state_repo_remote") in locs

    # 512 chars is allowed
    ok = client.patch(
        f"/api/v1/environments/{env['id']}",
        headers=admin_headers,
        json={"state_repo_path": "/x" + "x" * 510, "state_repo_remote": "y" * 512},
    )
    assert ok.status_code == 200, ok.text

    got = client.get(f"/api/v1/environments/{env['id']}", headers=admin_headers)
    assert got.json()["state_repo_path"] == "/x" + "x" * 510
    assert got.json()["state_repo_remote"] == "y" * 512


# ---------------------------------------------------------------------------
# F. Remote URL credential redaction
# ---------------------------------------------------------------------------


def test_state_repo_remote_redaction(client, admin_headers):
    secret_url = "https://user:secretpw@example.com/repo.git"
    redacted = "https://user:***@example.com/repo.git"
    env = _create_env(client, admin_headers)

    resp = client.patch(
        f"/api/v1/environments/{env['id']}",
        headers=admin_headers,
        json={"state_repo_remote": secret_url},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["state_repo_remote"] == redacted

    got = client.get(f"/api/v1/environments/{env['id']}", headers=admin_headers)
    assert got.status_code == 200, got.text
    assert got.json()["state_repo_remote"] == redacted

    # Echoing the redacted value back must not clobber the stored URL
    patch = client.patch(
        f"/api/v1/environments/{env['id']}",
        headers=admin_headers,
        json={"state_repo_remote": redacted},
    )
    assert patch.status_code == 200, patch.text
    assert patch.json()["state_repo_remote"] == redacted

    # The raw DB row still holds the real credentials
    db = SessionLocal()
    try:
        row = db.get(Environment, env["id"])
        assert row.state_repo_remote == secret_url
        assert "secretpw" in row.state_repo_remote
    finally:
        db.close()

    # A genuinely new URL still updates the stored value
    new_url = "https://user:newpass@example.com/other.git"
    update = client.patch(
        f"/api/v1/environments/{env['id']}",
        headers=admin_headers,
        json={"state_repo_remote": new_url},
    )
    assert update.status_code == 200, update.text
    assert (
        update.json()["state_repo_remote"] == "https://user:***@example.com/other.git"
    )
    db = SessionLocal()
    try:
        assert db.get(Environment, env["id"]).state_repo_remote == new_url
    finally:
        db.close()


# ---------------------------------------------------------------------------
# B4. State export routes through the deploy host (ssh/agent) when the env is
#      remote, running the git work there instead of on the console host.
# ---------------------------------------------------------------------------


def _remote_env_with_doc(
    repo: Path, remote: Path | None, dry_run: bool = False
) -> Environment:
    """A remote (deployer_ssh_host set) env with a stored config document."""
    env = _db_env(
        state_repo_path=str(repo),
        state_repo_remote=str(remote) if remote else None,
        deployer_ssh_host="deploy@example.com",
        dry_run=dry_run,
    )
    db = SessionLocal()
    try:
        envconfig_service.put_version(db, env, DOC, "state-test")
        db.commit()
    finally:
        db.close()
    return env


def test_export_remote_dry_run_is_preview_only(tmp_path, monkeypatch):
    """Remote dry-run prefights the repo, then previews; ships nothing."""
    from app.services import genestack_bridge as bridge

    bare = _init_bare_remote(tmp_path / "remote")
    repo = _init_repo(tmp_path / "repo")  # exists, but the REMOTE host owns it
    env = _remote_env_with_doc(repo, bare, dry_run=True)

    calls: list[list[str]] = []
    monkeypatch.setattr(
        bridge,
        "run_command",
        lambda cmd, **kw: calls.append(list(cmd)) or {"returncode": 0},
    )

    result = _export_result(env.id)
    assert result["ok"] is True
    assert result["dry_run"] is True
    # Exactly one remote command: the read-only preflight probe (repo reachable
    # + on a branch). No file ships, no git add/commit/push.
    assert len(calls) == 1, calls
    assert calls[0][:1] == ["bash"]
    assert "symbolic-ref" in calls[0][-1]
    assert _git(bare, "for-each-ref") == "", "dry-run must not push"

    db = SessionLocal()
    try:
        audit = db.scalars(
            select(AuditLog)
            .where(
                AuditLog.action == "env.state.export",
                AuditLog.environment_id == env.id,
            )
            .order_by(AuditLog.id)
        ).all()[-1]
        assert audit.details["dry_run"] is True
        assert audit.details["remote_host"] == "ssh"
        assert audit.details["commit"] is None
        assert audit.details["pushed"] is False
    finally:
        db.close()


def test_export_remote_dry_run_preflight_failure_is_honest(tmp_path, monkeypatch):
    """Remote dry-run reports failure (not green) when the repo is broken."""
    from app.services import genestack_bridge as bridge

    bare = _init_bare_remote(tmp_path / "remote")
    repo = _init_repo(tmp_path / "repo")
    env = _remote_env_with_doc(repo, bare, dry_run=True)

    monkeypatch.setattr(
        bridge,
        "run_command",
        lambda cmd, **kw: {
            "returncode": 128,
            "stdout": "",
            "stderr": "fatal: not a git repository",
            "message": "fatal: not a git repository",
        },
    )

    result = _export_result(env.id)
    assert result["ok"] is False
    assert result["dry_run"] is True
    assert result["returncode"] == 2
    assert "not a usable git checkout" in result["error"]


def test_export_remote_runs_git_on_deploy_host(tmp_path, monkeypatch):
    """Remote live export ships files + runs git through bridge.run_command
    (the ssh/agent channel), never on the console host."""
    import base64

    from app.services import genestack_bridge as bridge

    bare = _init_bare_remote(tmp_path / "remote")
    # A console-local checkout stands in for the deploy host's state repo: the
    # fake file-ship writes the rendered files there and every git command
    # executes against it, so the commit is real and pushable to the bare remote.
    clone = _init_repo(tmp_path / "host-repo")
    subprocess.run(
        ["git", "remote", "add", "origin", str(bare)],
        cwd=clone,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "fetch", "origin"], cwd=clone, capture_output=True, check=True
    )

    env = _remote_env_with_doc(clone, bare)

    run_calls: list[tuple[str | None, str | None, list[str]]] = []

    def fake_run_command(cmd, **kwargs):
        cmd = list(cmd)
        # Record routing: ssh_target must be set (deploy host), never agent.
        run_calls.append((kwargs.get("ssh_target"), kwargs.get("agent_env_id"), cmd))
        if cmd[:1] == ["bash"]:
            # File ship: decode the embedded base64 into the host-side checkout.
            script = cmd[-1]
            import re as _re

            b64m = _re.search(r"echo (\S+) \| base64 -d", script)
            destm = _re.search(r"base64 -d > (\S+)", script)
            if b64m and destm:
                dest = clone / destm.group(1).strip("'")
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(base64.b64decode(b64m.group(1).strip("'")))
            return {"returncode": 0, "stdout": "", "stderr": "", "message": "ok"}
        # git * : run against the checkout (stands in for the host checkout).
        proc = subprocess.run(cmd, cwd=clone, capture_output=True, text=True)
        return {
            "returncode": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "message": "ok" if proc.returncode == 0 else proc.stderr.strip(),
        }

    monkeypatch.setattr(bridge, "run_command", fake_run_command)

    result = _export_result(env.id)
    assert result["ok"] is True, result
    assert result.get("committed") is True
    assert result.get("pushed") is True
    assert result.get("commit")

    # Every remote command was routed to the deploy host (ssh), none to an agent.
    assert run_calls, "expected at least one remote command"
    assert all(
        ssh == "deploy@example.com" and agent is None for ssh, agent, _c in run_calls
    )
    git_cmds = [c for _ssh, _agent, c in run_calls if c[:1] == ["git"]]
    assert any(c[1] == "add" for c in git_cmds)
    assert any("commit" in c for c in git_cmds)
    assert any(c[1] == "push" for c in git_cmds)

    # The rendered file was shipped to the deploy host and committed there.
    assert (clone / "state" / env.name / "provider").read_text() == "kubespray\n"
    # The commit landed in the checkout and was pushed to the bare remote.
    assert result["commit"] == _git(bare, "rev-parse", "HEAD")
    tree = _git(bare, "ls-tree", "-r", "--name-only", "main").splitlines()
    assert f"state/{env.name}/provider" in tree
