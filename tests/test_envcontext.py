"""Unit tests for app.services.envcontext (per-env execution context)."""

from __future__ import annotations

import os
import stat
import time
from pathlib import Path

from app.config import get_settings
from app.models import Environment
from app.services.crypto import encrypt_secret
from app.services.envcontext import build_context, sweep_staged_kubeconfigs


def _env(**kwargs) -> Environment:
    kwargs.setdefault("id", "env-test-1")
    kwargs.setdefault("name", "env-test-1")
    return Environment(**kwargs)


def test_null_context_uses_global_dry_run_and_no_overrides():
    settings = get_settings()
    ctx = build_context(None, settings)
    assert ctx.dry_run is settings.dry_run
    assert ctx.config_dir is None
    assert ctx.kubeconfig is None

    env = ctx.subprocess_env()
    assert env["GENESTACK_BASE_DIR"] == str(ctx.genestack_root)
    assert "GENESTACK_CONFIG" not in env or "GENESTACK_CONFIG" in os.environ
    ctx.cleanup()  # no-op


def test_per_env_dry_run_overrides_global_both_ways():
    settings = get_settings()
    assert build_context(_env(dry_run=False), settings).dry_run is False
    assert build_context(_env(dry_run=True), settings).dry_run is True
    assert build_context(_env(dry_run=None), settings).dry_run is settings.dry_run


def test_config_dir_sets_genestack_env_vars(tmp_path):
    settings = get_settings()
    config_dir = tmp_path / "etc-genestack"
    (config_dir / "inventory").mkdir(parents=True)

    ctx = build_context(_env(genestack_config_dir=str(config_dir)), settings)
    env = ctx.subprocess_env()
    assert env["GENESTACK_CONFIG"] == str(config_dir)
    assert env["GENESTACK_OVERRIDES_DIR"] == str(config_dir)
    assert env["ANSIBLE_INVENTORY"] == str(config_dir / "inventory")
    assert env["GENESTACK_BASE_DIR"] == str(ctx.genestack_root)


def test_no_inventory_dir_means_no_ansible_inventory(tmp_path):
    settings = get_settings()
    config_dir = tmp_path / "etc-genestack"
    config_dir.mkdir()

    env = build_context(
        _env(genestack_config_dir=str(config_dir)), settings
    ).subprocess_env()
    assert env.get("ANSIBLE_INVENTORY") == os.environ.get("ANSIBLE_INVENTORY")


def test_remote_context_keeps_deployer_paths_and_inventory(tmp_path):
    target = tmp_path / "console-local"
    target.mkdir()
    alias = tmp_path / "deployer-path"
    alias.symlink_to(target)
    ctx = build_context(
        _env(
            deployer_ssh_host="deployer.example.com",
            deployer_ssh_user="deploy",
            genestack_path=str(alias),
            genestack_config_dir="/etc/genestack",
        )
    )
    assert ctx.ssh_target == "deploy@deployer.example.com"
    assert ctx.genestack_root == alias
    assert ctx.config_dir == Path("/etc/genestack")
    assert ctx.remote_env()["GENESTACK_BASE_DIR"] == str(alias)
    assert ctx.remote_env()["GENESTACK_CONFIG"] == "/etc/genestack"
    assert ctx.remote_env()["ANSIBLE_INVENTORY"] == "/etc/genestack/inventory"


def test_kubeconfig_blob_staged_0600_and_cleaned_up(tmp_path, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "data_dir", tmp_path)

    payload = "apiVersion: v1\nclusters: []\n"
    ctx = build_context(
        _env(kubeconfig_data=encrypt_secret(payload, settings)), settings
    )

    staged = Path(ctx.kubeconfig)
    assert staged.is_file()
    assert staged.parent == tmp_path / "kubeconfigs"
    assert staged.read_text(encoding="utf-8") == payload
    assert stat.S_IMODE(staged.stat().st_mode) == 0o600
    # The staging dir holds decrypted cluster credentials for every env — it
    # must be owner-only (0700) even while a job has the file on disk.
    assert stat.S_IMODE(staged.parent.stat().st_mode) == 0o700
    assert ctx.subprocess_env()["KUBECONFIG"] == str(staged)

    ctx.cleanup()
    assert not staged.exists()
    ctx.cleanup()  # idempotent


def test_kubeconfig_staging_dir_forced_0700_despite_umask(tmp_path, monkeypatch):
    """os.makedirs is umask-limited; the explicit chmod must still yield 0700."""
    settings = get_settings()
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    # Pre-create the dir with a permissive mode to simulate a loose umask /
    # pre-existing state — _stage_kubeconfig must tighten it, not inherit it.
    kube_dir = tmp_path / "kubeconfigs"
    kube_dir.mkdir(mode=0o755)

    payload = "apiVersion: v1\nclusters: []\n"
    ctx = build_context(
        _env(kubeconfig_data=encrypt_secret(payload, settings)), settings
    )
    assert stat.S_IMODE(Path(ctx.kubeconfig).parent.stat().st_mode) == 0o700
    ctx.cleanup()


def test_sweep_staged_kubeconfigs_removes_leftovers(tmp_path):
    kube_dir = tmp_path / "kubeconfigs"
    kube_dir.mkdir()
    (kube_dir / "env-a.yaml").write_text("secret-a")
    (kube_dir / "env-b.yaml").write_text("secret-b")
    (kube_dir / "keep-me").mkdir()  # subdirs are not touched

    removed = sweep_staged_kubeconfigs(tmp_path)
    assert removed == 2
    assert not (kube_dir / "env-a.yaml").exists()
    assert not (kube_dir / "env-b.yaml").exists()
    assert (kube_dir / "keep-me").is_dir()


def test_sweep_staged_kubeconfigs_missing_dir_is_noop(tmp_path):
    assert sweep_staged_kubeconfigs(tmp_path) == 0


def test_sweep_keeps_a_kubeconfig_a_running_job_still_needs(tmp_path):
    kube_dir = tmp_path / "kubeconfigs"
    kube_dir.mkdir()
    live = kube_dir / "live.yaml"
    leaked = kube_dir / "leaked.yaml"
    live.write_text("live")
    leaked.write_text("old")
    old = time.time() - (6 * 3600 + 60)
    os.utime(leaked, (old, old))

    removed = sweep_staged_kubeconfigs(tmp_path, older_than_seconds=6 * 3600)
    assert removed == 1
    assert live.read_text() == "live"
    assert not leaked.exists()


def test_ensure_kubeconfig_restages_when_the_file_was_swept(tmp_path, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    payload = "apiVersion: v1\nclusters: []\ncurrent-context: live\n"
    ctx = build_context(
        _env(kubeconfig_data=encrypt_secret(payload, settings)), settings
    )
    gone = Path(ctx.kubeconfig)
    gone.unlink()

    fresh = ctx.ensure_kubeconfig()
    assert fresh is not None
    assert fresh != str(gone)
    assert Path(fresh).read_text(encoding="utf-8") == payload
    assert ctx.kubeconfig == fresh
    ctx.cleanup()
    assert not Path(fresh).exists()


def test_bind_kubeconfig_replaces_a_missing_path(tmp_path, monkeypatch):
    from app.services.deploy import _bind_kubeconfig

    settings = get_settings()
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    payload = "apiVersion: v1\nclusters: []\ncurrent-context: live\n"
    ctx = build_context(
        _env(kubeconfig_data=encrypt_secret(payload, settings)), settings
    )
    gone = ctx.kubeconfig
    Path(gone).unlink()
    notes: list[str] = []
    extra = {"KUBECONFIG": gone}
    _bind_kubeconfig(ctx, extra, notes.append)
    assert extra["KUBECONFIG"] != gone
    assert Path(extra["KUBECONFIG"]).read_text(encoding="utf-8") == payload
    assert notes == ["[deploy] kubeconfig file was gone; staged a new one"]
    ctx.cleanup()


def test_kubeconfig_contexts_do_not_share_a_file(tmp_path, monkeypatch):
    """A second reader must not unlink the file a running job still uses."""
    settings = get_settings()
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    payload = "apiVersion: v1\nclusters: []\ncurrent-context: live\n"
    env = _env(kubeconfig_data=encrypt_secret(payload, settings))

    first = build_context(env, settings)
    second = build_context(env, settings)
    assert first.kubeconfig != second.kubeconfig
    assert Path(first.kubeconfig).read_text(encoding="utf-8") == payload
    assert Path(second.kubeconfig).read_text(encoding="utf-8") == payload

    first.cleanup()
    assert not Path(first.kubeconfig).exists()
    assert Path(second.kubeconfig).is_file()
    assert second.subprocess_env()["KUBECONFIG"] == second.kubeconfig
    second.cleanup()
    assert not Path(second.kubeconfig).exists()


def test_kubeconfig_path_used_when_no_blob(tmp_path):
    settings = get_settings()
    kube = tmp_path / "kubeconfig"
    kube.write_text("apiVersion: v1\n", encoding="utf-8")

    ctx = build_context(_env(kubeconfig_path=str(kube)), settings)
    assert ctx.kubeconfig == str(kube)
    ctx.cleanup()
    assert kube.is_file()  # host-path kubeconfig is never removed
