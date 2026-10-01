"""Overlay YAML read/write (Expert-tab helm/kustomize editor)."""

from __future__ import annotations

import subprocess
import uuid
from pathlib import Path

import yaml

from tests.test_descriptor import _write_fake_config_dir


def _env(
    client,
    headers,
    config_dir: Path,
    genestack_root: Path | None = None,
    **extra,
) -> dict:
    body = {
        "name": f"ovl-{uuid.uuid4().hex[:8]}",
        "genestack_config_dir": str(config_dir),
    }
    if genestack_root is not None:
        body["genestack_path"] = str(genestack_root)
    resp = client.post("/api/v1/environments", headers=headers, json=body)
    assert resp.status_code in (200, 201), resp.text
    env = resp.json()
    if extra:
        patch = client.patch(
            f"/api/v1/environments/{env['id']}",
            headers=headers,
            json=extra,
        )
        assert patch.status_code == 200, patch.text
        return patch.json()
    return env


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=30
    )
    assert proc.returncode == 0, f"git {' '.join(args)} failed: {proc.stderr}"
    return proc.stdout.strip()


def _init_git_repo(path: Path) -> Path:
    """A git checkout with user.email set and an initial commit so HEAD exists."""
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.email", "console-test@example.com")
    _git(path, "config", "user.name", "console-test")
    (path / "README.md").write_text("state repo\n", encoding="utf-8")
    _git(path, "add", "README.md")
    _git(path, "commit", "-m", "initial")
    return path


def test_read_helm_override(
    client, admin_headers, viewer_headers, tmp_path, genestack_root
):
    config_dir = _write_fake_config_dir(tmp_path / "etc-genestack")
    base = (
        genestack_root
        / "base-helm-configs"
        / "keystone"
        / "keystone-helm-overrides.yaml"
    )
    base.parent.mkdir(parents=True, exist_ok=True)
    base.write_text("replicas: 1\nreplicaCount: 1\n", encoding="utf-8")
    env = _env(client, admin_headers, config_dir, genestack_root)
    denied = client.get(
        f"/api/v1/environments/{env['id']}/overlays",
        params={"path": "helm-configs/keystone/keystone-helm-overrides.yaml"},
        headers=viewer_headers,
    )
    assert denied.status_code in (401, 403)
    resp = client.get(
        f"/api/v1/environments/{env['id']}/overlays",
        params={"path": "helm-configs/keystone/keystone-helm-overrides.yaml"},
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["mode"] == "ye"
    # Editor shows the merge (base replicaCount + local replicas: 2).
    merged = yaml.safe_load(body["content"])
    assert merged["replicas"] == 2
    assert merged["replicaCount"] == 1
    assert "replicas: 2" in body["local_content"]
    assert body["base_path"] is not None
    assert body["base_content"] is not None


def test_write_helm_override_roundtrip(client, admin_headers, tmp_path):
    config_dir = _write_fake_config_dir(tmp_path / "etc-genestack")
    env = _env(client, admin_headers, config_dir)
    path = "helm-configs/glance/glance.yaml"
    new = "replicas: 5\nimage: glance:test\n"
    resp = client.put(
        f"/api/v1/environments/{env['id']}/overlays",
        headers=admin_headers,
        json={"path": path, "content": new},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    git = body.get("git") or {}
    assert git.get("attempted") is False
    assert git.get("committed") is False
    assert git.get("error") is None
    on_disk = (config_dir / "helm-configs" / "glance" / "glance.yaml").read_text()
    assert yaml.safe_load(on_disk) == yaml.safe_load(new)
    got = client.get(
        f"/api/v1/environments/{env['id']}/overlays",
        params={"path": path},
        headers=admin_headers,
    )
    assert yaml.safe_load(got.json()["content"]) == yaml.safe_load(new)


def test_write_rejects_invalid_yaml(client, admin_headers, tmp_path):
    config_dir = _write_fake_config_dir(tmp_path / "etc-genestack")
    env = _env(client, admin_headers, config_dir)
    resp = client.put(
        f"/api/v1/environments/{env['id']}/overlays",
        headers=admin_headers,
        json={
            "path": "helm-configs/glance/glance.yaml",
            "content": "foo: [unterminated\n",
        },
    )
    assert resp.status_code == 400
    assert "YAML" in resp.json()["detail"]


def test_path_traversal_rejected(client, admin_headers, tmp_path):
    config_dir = _write_fake_config_dir(tmp_path / "etc-genestack")
    env = _env(client, admin_headers, config_dir)
    resp = client.get(
        f"/api/v1/environments/{env['id']}/overlays",
        params={"path": "helm-configs/../provider"},
        headers=admin_headers,
    )
    assert resp.status_code == 400


def test_unknown_root_rejected(client, admin_headers, tmp_path):
    config_dir = _write_fake_config_dir(tmp_path / "etc-genestack")
    env = _env(client, admin_headers, config_dir)
    resp = client.get(
        f"/api/v1/environments/{env['id']}/overlays",
        params={"path": "inventory/inventory.yaml"},
        headers=admin_headers,
    )
    assert resp.status_code == 400


def test_missing_helm_file_is_empty_local(client, admin_headers, tmp_path):
    """YE: a service with no local overlay still opens (chart+base merge)."""
    config_dir = _write_fake_config_dir(tmp_path / "etc-genestack")
    env = _env(client, admin_headers, config_dir)
    resp = client.get(
        f"/api/v1/environments/{env['id']}/overlays",
        params={"path": "helm-configs/keystone/missing.yaml"},
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["exists"] is False
    assert resp.json()["mode"] == "ye"


def test_missing_kustomize_404(client, admin_headers, tmp_path):
    config_dir = _write_fake_config_dir(tmp_path / "etc-genestack")
    env = _env(client, admin_headers, config_dir)
    resp = client.get(
        f"/api/v1/environments/{env['id']}/overlays",
        params={"path": "kustomize/keystone/overlay/nope.yaml"},
        headers=admin_headers,
    )
    assert resp.status_code == 404


def test_viewer_cannot_write(client, admin_headers, viewer_headers, tmp_path):
    config_dir = _write_fake_config_dir(tmp_path / "etc-genestack")
    env = _env(client, admin_headers, config_dir)
    resp = client.put(
        f"/api/v1/environments/{env['id']}/overlays",
        headers=viewer_headers,
        json={"path": "helm-configs/glance/glance.yaml", "content": "replicas: 1\n"},
    )
    assert resp.status_code in (401, 403)


def test_kustomize_and_gateway_readable(client, admin_headers, tmp_path):
    config_dir = _write_fake_config_dir(tmp_path / "etc-genestack")
    env = _env(client, admin_headers, config_dir)
    kz = client.get(
        f"/api/v1/environments/{env['id']}/overlays",
        params={"path": "kustomize/keystone/overlay/kustomization.yaml"},
        headers=admin_headers,
    )
    assert kz.status_code == 200, kz.text
    assert "resources:" in kz.json()["content"]
    gw = client.get(
        f"/api/v1/environments/{env['id']}/overlays",
        params={"path": "gateway-api/listeners/gateway.yaml"},
        headers=admin_headers,
    )
    assert gw.status_code == 200, gw.text
    assert yaml.safe_load(gw.json()["content"])["kind"] == "Gateway"


def test_ye_save_strips_keys_already_in_base(
    client, admin_headers, tmp_path, genestack_root
):
    """Saving the merged view writes only the delta to helm-configs (yaml-editor/ye)."""
    config_dir = _write_fake_config_dir(tmp_path / "etc-genestack")
    base = (
        genestack_root
        / "base-helm-configs"
        / "keystone"
        / "keystone-helm-overrides.yaml"
    )
    base.parent.mkdir(parents=True, exist_ok=True)
    base.write_text("replicas: 1\nreplicaCount: 1\n", encoding="utf-8")
    env = _env(client, admin_headers, config_dir, genestack_root)
    path = "helm-configs/keystone/keystone-helm-overrides.yaml"
    # Merged document: keep base replicaCount, change replicas, add a local-only key.
    merged = "replicas: 9\nreplicaCount: 1\nextra: local-only\n"
    resp = client.put(
        f"/api/v1/environments/{env['id']}/overlays",
        headers=admin_headers,
        json={"path": path, "content": merged},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["mode"] == "ye"
    delta = yaml.safe_load(body["delta_content"])
    assert delta == {"replicas": 9, "extra": "local-only"}
    on_disk = yaml.safe_load((config_dir / path).read_text())
    assert on_disk == delta
    assert "replicaCount" not in on_disk


def test_merge_and_patch_helpers():
    from app.services.overlays import compute_patch, merge_dicts

    floor = merge_dicts({"a": 1, "n": {"x": 1, "y": 2}}, {"n": {"y": 9}})
    assert floor == {"a": 1, "n": {"x": 1, "y": 9}}
    edited = {"a": 1, "n": {"x": 1, "y": 9, "z": 3}, "b": 2}
    assert compute_patch(floor, edited) == {"n": {"z": 3}, "b": 2}


def test_write_overlay_commits_to_state_repo(client, admin_headers, tmp_path):
    config_dir = _write_fake_config_dir(tmp_path / "etc-genestack")
    repo = _init_git_repo(tmp_path / "state-repo")
    env = _env(client, admin_headers, config_dir, state_repo_path=str(repo))
    path = "helm-configs/glance/glance.yaml"
    new = "replicas: 7\nimage: glance:git\n"
    resp = client.put(
        f"/api/v1/environments/{env['id']}/overlays",
        headers=admin_headers,
        json={"path": path, "content": new},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    git = body["git"]
    assert git["attempted"] is True
    assert git["committed"] is True
    assert git["pushed"] is False
    assert git["error"] is None
    assert git["sha"]
    assert git["path"] == f"state/{env['name']}/{path}"
    on_disk = (config_dir / path).read_text(encoding="utf-8")
    dumped = (repo / "state" / env["name"] / path).read_text(encoding="utf-8")
    assert dumped == on_disk
    assert yaml.safe_load(dumped) == yaml.safe_load(new)
    log = _git(repo, "log", "-1", "--format=%s%n%an")
    assert log.splitlines()[0] == f"overlay({env['name']}): {path}"
    assert "genestack-console" in log
    assert _git(repo, "rev-parse", "HEAD") == git["sha"]


def test_write_overlay_without_state_repo_path(client, admin_headers, tmp_path):
    config_dir = _write_fake_config_dir(tmp_path / "etc-genestack")
    env = _env(client, admin_headers, config_dir)
    resp = client.put(
        f"/api/v1/environments/{env['id']}/overlays",
        headers=admin_headers,
        json={"path": "helm-configs/glance/glance.yaml", "content": "replicas: 1\n"},
    )
    assert resp.status_code == 200, resp.text
    git = resp.json()["git"]
    assert git["attempted"] is False
    assert git["committed"] is False
    assert git["pushed"] is False
    assert git["sha"] is None
    assert git["path"] is None
    assert git["error"] is None


def test_write_overlay_git_not_a_repo_still_200(client, admin_headers, tmp_path):
    """Git dump failure must not fail the local helm-configs write."""
    config_dir = _write_fake_config_dir(tmp_path / "etc-genestack")
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()
    env = _env(client, admin_headers, config_dir, state_repo_path=str(not_a_repo))
    path = "helm-configs/glance/glance.yaml"
    new = "replicas: 3\n"
    resp = client.put(
        f"/api/v1/environments/{env['id']}/overlays",
        headers=admin_headers,
        json={"path": path, "content": new},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    git = body["git"]
    assert git["attempted"] is True
    assert git["committed"] is False
    assert git["pushed"] is False
    assert git["error"]
    on_disk = yaml.safe_load((config_dir / path).read_text(encoding="utf-8"))
    assert on_disk == {"replicas": 3}
    assert not (not_a_repo / "state").exists()


def test_load_helm_chart_values_adds_repo_and_shows(monkeypatch):
    from app.services import overlays as ovl

    ovl._HELM_CACHE.clear()
    ovl._added_repos.clear()
    calls: list[list[str]] = []

    def fake_which(_name: str) -> str:
        return "/usr/bin/helm"

    def fake_run(argv, timeout=25):
        calls.append(argv)
        if argv[1:3] == ["repo", "add"]:
            return 0, "", ""
        if argv[1:3] == ["show", "values"]:
            return 0, "pod:\n  replicas: 3\n", ""
        return 1, "", "unexpected"

    monkeypatch.setattr(ovl.shutil, "which", fake_which)
    monkeypatch.setattr(ovl, "_helm_run", fake_run)
    data, src = ovl.load_helm_chart_values("nova", "2026.1.8")
    assert data == {"pod": {"replicas": 3}}
    assert src and src.startswith("openstack-helm/nova:")
    assert any(c[1:3] == ["repo", "add"] for c in calls)
    # cache hit — no extra helm
    n = len(calls)
    data2, src2 = ovl.load_helm_chart_values("nova", "2026.1.8")
    assert data2 == data
    assert src2.startswith("cache:")
    assert len(calls) == n


def test_load_helm_chart_values_ignores_flag_version(monkeypatch):
    from app.services import overlays as ovl

    ovl._HELM_CACHE.clear()
    ovl._added_repos.clear()
    calls: list[list[str]] = []

    monkeypatch.setattr(ovl.shutil, "which", lambda _n: "/usr/bin/helm")
    monkeypatch.setattr(
        ovl, "_helm_run", lambda argv, timeout=25: calls.append(argv) or (0, "", "")
    )
    data, src = ovl.load_helm_chart_values("nova", "-f/etc/passwd")
    assert data == {}
    assert src is None
    assert calls == []
    data2, src2 = ovl.load_helm_chart_values("--add", "2026.1.8")
    assert data2 == {}
    assert src2 is None
    assert calls == []


def test_truncate_git_error_redacts_credentials():
    from app.services.overlays import _truncate_git_error

    msg = _truncate_git_error(
        "fatal: could not read from https://user:hunter2@github.com/org/repo.git"
    )
    assert "hunter2" not in msg
    assert "<redacted>" in msg
    tok = _truncate_git_error(
        "Authentication failed for ghp_abcdefghijklmnopqrstuvwxyz"
    )
    assert "ghp_abcdefghijklmnopqrstuvwxyz" not in tok
    assert "<redacted-token>" in tok
    pat = _truncate_git_error("fatal: github_pat_abcdefghijklmnopqrstuvwxyz")
    assert "github_pat_abcdefghijklmnopqrstuvwxyz" not in pat
    gl = _truncate_git_error("fatal: glpat-abcdefghijklmnopqrstuvwxyz")
    assert "glpat-abcdefghijklmnopqrstuvwxyz" not in gl
    token_user = _truncate_git_error("https://ghp_secretvalue@github.com/org/repo.git")
    assert "ghp_secretvalue" not in token_user


def test_git_remote_ok_rejects_flag_and_file():
    from app.services.overlays import _git_remote_ok, _git_url_ok

    assert _git_remote_ok("origin")
    assert _git_remote_ok("https://github.com/org/repo.git")
    assert _git_remote_ok("git@github.com:org/repo.git")
    assert _git_url_ok("ssh://git@github.com/org/repo.git")
    assert not _git_remote_ok("-u")
    assert not _git_remote_ok("--upload-pack=evil")
    assert not _git_remote_ok("file:///etc/passwd")
    assert not _git_remote_ok("/tmp/repo.git")
    assert not _git_remote_ok("")
    assert not _git_url_ok("ext::sh -c evil")
    assert not _git_url_ok("ssh://-oProxyCommand=evil/repo")
    assert not _git_url_ok("ssh://-oProxyCommand=bash@evil/repo")
    assert not _git_url_ok("file:///etc/passwd")


def test_write_overlay_rejects_flag_git_remote(client, admin_headers, tmp_path):
    config_dir = _write_fake_config_dir(tmp_path / "etc-genestack")
    repo = _init_git_repo(tmp_path / "state-repo")
    env = _env(
        client,
        admin_headers,
        config_dir,
        state_repo_path=str(repo),
        state_repo_remote="-u",
    )
    resp = client.put(
        f"/api/v1/environments/{env['id']}/overlays",
        headers=admin_headers,
        json={"path": "helm-configs/glance/glance.yaml", "content": "replicas: 2\n"},
    )
    assert resp.status_code == 200, resp.text
    git = resp.json()["git"]
    assert git["attempted"] is True
    assert git["committed"] is True
    assert git["pushed"] is False
    assert git["error"] == "invalid git remote"


def test_write_rejects_yaml_alias_bomb(client, admin_headers, tmp_path):
    config_dir = _write_fake_config_dir(tmp_path / "etc-genestack")
    env = _env(client, admin_headers, config_dir)
    bomb = "".join(f"a{i}: &a{i} [x]\n" for i in range(40))
    resp = client.put(
        f"/api/v1/environments/{env['id']}/overlays",
        headers=admin_headers,
        json={"path": "helm-configs/glance/glance.yaml", "content": bomb},
    )
    assert resp.status_code == 400
    assert "anchor" in resp.json()["detail"].lower()


def test_write_rejects_yaml_cycle(client, admin_headers, tmp_path):
    config_dir = _write_fake_config_dir(tmp_path / "etc-genestack")
    env = _env(client, admin_headers, config_dir)
    resp = client.put(
        f"/api/v1/environments/{env['id']}/overlays",
        headers=admin_headers,
        json={"path": "helm-configs/glance/glance.yaml", "content": "a: &a\n  b: *a\n"},
    )
    assert resp.status_code == 400


def test_write_rejects_yaml_alias_expansion(client, admin_headers, tmp_path):
    config_dir = _write_fake_config_dir(tmp_path / "etc-genestack")
    env = _env(client, admin_headers, config_dir)
    lines = ["a0: &a0 [x, x]\n"]
    lines.extend(f"a{i}: &a{i} [*a{i-1}, *a{i-1}]\n" for i in range(1, 14))
    resp = client.put(
        f"/api/v1/environments/{env['id']}/overlays",
        headers=admin_headers,
        json={"path": "helm-configs/glance/glance.yaml", "content": "".join(lines)},
    )
    assert resp.status_code == 400


def test_run_git_disables_hooks(tmp_path, monkeypatch):
    from app.services import overlays as ovl

    captured: dict = {}

    class Proc:
        returncode = 0
        stdout = "true\n"
        stderr = ""

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["env"] = kwargs.get("env") or {}
        return Proc()

    monkeypatch.setattr(ovl.subprocess, "run", fake_run)
    ovl._run_git(["git", "status"], cwd=tmp_path)
    argv = captured["argv"]
    assert argv[0] == "git"
    assert "core.hooksPath=/dev/null" in argv
    assert "protocol.file.allow=never" in argv
    assert "protocol.ext.allow=never" in argv
    assert "core.fsmonitor=false" in argv
    assert captured["env"].get("GIT_TERMINAL_PROMPT") == "0"
    assert captured["env"].get("GIT_ASKPASS") == "true"
    assert captured["env"].get("GIT_ALLOW_PROTOCOL") == "https:http:ssh:git"


def test_write_overlay_rejects_ext_origin(client, admin_headers, tmp_path):
    config_dir = _write_fake_config_dir(tmp_path / "etc-genestack")
    repo = _init_git_repo(tmp_path / "state-repo")
    _git(repo, "remote", "add", "origin", "ext::sh -c evil")
    env = _env(
        client,
        admin_headers,
        config_dir,
        state_repo_path=str(repo),
        state_repo_remote="origin",
    )
    resp = client.put(
        f"/api/v1/environments/{env['id']}/overlays",
        headers=admin_headers,
        json={"path": "helm-configs/glance/glance.yaml", "content": "replicas: 4\n"},
    )
    assert resp.status_code == 200, resp.text
    git = resp.json()["git"]
    assert git["attempted"] is True
    assert git["committed"] is True
    assert git["pushed"] is False
    assert git["error"] == "invalid git remote"
