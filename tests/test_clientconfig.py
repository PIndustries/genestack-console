"""Client talosconfig and kubeconfig: grab the vault copy, or regenerate it."""

from __future__ import annotations

import uuid
from pathlib import Path

from sqlalchemy import select

from app.db import SessionLocal
from app.models import VaultItem
from app.services.clientconfig import (
    CLIENT_CERT_TTL,
    _https_endpoint,
    issue_client_config,
)
from app.services.crypto import decrypt_secret
from app.services.envcontext import EnvContext
from tests.test_tenants import _create_tenant

_SAVED = """context: lab
contexts:
  lab:
    endpoints:
      - 172.16.12.101
    ca: QUFB
    crt: QUFB
    key: QUFB
"""

_ISSUED_TALOS = "context: issued\ncontexts:\n  issued:\n    ca: QQ\n"
_ISSUED_KUBE = "apiVersion: v1\nkind: Config\nclusters:\n- name: c\n"
_SECRETS_TALOS = "context: secrets\ncontexts:\n  secrets:\n    ca: QQ\n"


def _ctx(tmp_path: Path, *, dry_run: bool = False, kubeconfig: str | None = None) -> EnvContext:
    return EnvContext(
        environment=None,
        genestack_root=tmp_path,
        config_dir=tmp_path,
        dry_run=dry_run,
        kubeconfig=kubeconfig,
    )


def _write_talos(tmp_path: Path, text: str = _SAVED) -> Path:
    talos = tmp_path / "talos"
    talos.mkdir()
    path = talos / "talosconfig"
    path.write_text(text, encoding="utf-8")
    return path


def _patch_ctl(monkeypatch, runner):
    monkeypatch.setattr("app.services.clientconfig.talosctl_bin", lambda: "/usr/bin/talosctl")
    monkeypatch.setattr("app.services.clientconfig._run", runner)


def test_https_endpoint_brackets_ipv6():
    assert _https_endpoint("2001:db8::1") == "https://[2001:db8::1]:6443"
    assert _https_endpoint("172.16.12.101") == "https://172.16.12.101:6443"


def test_talosconfig_download_renews_for_one_year(tmp_path, monkeypatch):
    saved = _write_talos(tmp_path)
    calls: list[list[str]] = []

    def runner(argv, *, timeout=30):
        calls.append(list(argv))
        dest = Path(argv[argv.index("new") + 1])
        dest.write_text(_ISSUED_TALOS, encoding="utf-8")
        return 0

    _patch_ctl(monkeypatch, runner)
    path, source, cleanup = issue_client_config(_ctx(tmp_path), "talosconfig")
    try:
        assert source == "issued"
        assert path is not None
        assert path.read_text(encoding="utf-8") == _ISSUED_TALOS
        assert (path.stat().st_mode & 0o777) == 0o600
        assert saved.read_text(encoding="utf-8") == _SAVED
        argv = calls[0]
        assert argv[1:3] == ["--nodes", "172.16.12.101"]
        assert "config" in argv and "new" in argv
        assert argv[argv.index("--roles") + 1] == "os:admin"
        assert argv[argv.index("--crt-ttl") + 1] == CLIENT_CERT_TTL
        assert CLIENT_CERT_TTL == "8760h"
    finally:
        cleanup()
        assert path is not None and not path.exists()


def test_kubeconfig_download_regenerates_the_client_cert(tmp_path, monkeypatch):
    _write_talos(tmp_path)
    kube = tmp_path / "kubeconfig"
    kube.write_text("apiVersion: v1\nkind: Config\nclusters:\n- name: old\n", encoding="utf-8")
    calls: list[list[str]] = []

    def runner(argv, *, timeout=30):
        calls.append(list(argv))
        Path(argv[2]).write_text(_ISSUED_KUBE, encoding="utf-8")
        return 0

    _patch_ctl(monkeypatch, runner)
    path, source, cleanup = issue_client_config(
        _ctx(tmp_path, kubeconfig=str(kube)), "kubeconfig"
    )
    try:
        assert source == "issued"
        assert path is not None and path != kube
        assert path.read_text(encoding="utf-8") == _ISSUED_KUBE
        assert "name: old" in kube.read_text(encoding="utf-8")
        argv = calls[0]
        assert argv[1] == "kubeconfig"
        assert argv[argv.index("--nodes") + 1] == "172.16.12.101"
        assert "--force" in argv
    finally:
        cleanup()


def test_kubeconfig_renews_from_secrets_when_the_saved_client_is_refused(
    tmp_path, monkeypatch
):
    talos_path = _write_talos(tmp_path)
    (talos_path.parent / "secrets.yaml").write_text("cluster: {}\n", encoding="utf-8")

    def runner(argv, *, timeout=30):
        if argv[1] == "kubeconfig" and argv[argv.index("--talosconfig") + 1] == str(talos_path):
            return 1
        if argv[1:3] == ["gen", "config"]:
            work = Path(argv[argv.index("--output-dir") + 1])
            (work / "talosconfig").write_text(_SECRETS_TALOS, encoding="utf-8")
            return 0
        if argv[1] == "kubeconfig":
            Path(argv[2]).write_text(_ISSUED_KUBE, encoding="utf-8")
            return 0
        return 1

    _patch_ctl(monkeypatch, runner)
    path, source, cleanup = issue_client_config(_ctx(tmp_path), "kubeconfig")
    try:
        assert source == "issued"
        assert path is not None and path.read_text(encoding="utf-8") == _ISSUED_KUBE
        assert talos_path.read_text(encoding="utf-8") == _SAVED
    finally:
        cleanup()
        assert path is not None and not path.exists()
        assert talos_path.is_file()


def test_failed_issue_returns_the_saved_file(tmp_path, monkeypatch):
    saved = _write_talos(tmp_path)

    def runner(argv, *, timeout=30):
        return 1

    _patch_ctl(monkeypatch, runner)
    path, source, cleanup = issue_client_config(_ctx(tmp_path), "talosconfig")
    assert source == "stored"
    assert path == saved
    assert path.read_text(encoding="utf-8") == _SAVED
    cleanup()
    assert saved.is_file()


def test_secrets_bundle_renews_when_config_new_fails(tmp_path, monkeypatch):
    talos_path = _write_talos(tmp_path)
    (talos_path.parent / "secrets.yaml").write_text("cluster: {}\n", encoding="utf-8")
    calls: list[list[str]] = []

    def runner(argv, *, timeout=30):
        calls.append(list(argv))
        if "new" in argv:
            return 1
        work = Path(argv[argv.index("--output-dir") + 1])
        (work / "talosconfig").write_text(_SECRETS_TALOS, encoding="utf-8")
        return 0

    _patch_ctl(monkeypatch, runner)
    path, source, cleanup = issue_client_config(_ctx(tmp_path), "talosconfig")
    try:
        assert source == "issued"
        assert path is not None
        assert path.read_text(encoding="utf-8") == _SECRETS_TALOS
        gen = calls[1]
        assert gen[1:4] == ["gen", "config", "cluster"]
        assert "https://172.16.12.101:6443" in gen
        assert "--with-secrets" in gen
        assert "--force" not in gen
        assert talos_path.read_text(encoding="utf-8") == _SAVED
    finally:
        cleanup()


def test_unsafe_endpoint_is_not_passed_to_talosctl(tmp_path, monkeypatch):
    _write_talos(
        tmp_path,
        "context: lab\ncontexts:\n  lab:\n    endpoints:\n      - 172.16.12.101;touch\n",
    )
    calls: list[list[str]] = []

    def runner(argv, *, timeout=30):
        calls.append(list(argv))
        return 1

    _patch_ctl(monkeypatch, runner)
    path, source, cleanup = issue_client_config(_ctx(tmp_path), "talosconfig")
    assert calls == []
    assert source == "saved"
    assert path is not None and path.is_file()
    cleanup()


def test_dry_run_does_not_ask_the_cluster(tmp_path, monkeypatch):
    saved = _write_talos(tmp_path)

    def runner(argv, *, timeout=30):
        raise AssertionError("dry run must not call talosctl")

    _patch_ctl(monkeypatch, runner)
    path, source, _cleanup = issue_client_config(
        _ctx(tmp_path, dry_run=True), "talosconfig"
    )
    assert source == "saved"
    assert path == saved


def _client_env(client, admin_headers, tmp_path: Path) -> tuple[dict, Path]:
    tenant = _create_tenant(client, admin_headers)
    env = client.post(
        "/api/v1/environments",
        headers=admin_headers,
        json={
            "name": f"client-cert-{uuid.uuid4().hex[:8]}",
            "tenant_id": tenant["id"],
        },
    ).json()
    saved = _write_talos(tmp_path)
    patched = client.patch(
        f"/api/v1/environments/{env['id']}",
        headers=admin_headers,
        json={"genestack_config_dir": str(tmp_path), "dry_run": False},
    )
    assert patched.status_code in (200, 204), patched.text
    return env, saved


def _vault_copy(env_id: str, kind: str) -> tuple[str | None, str | None]:
    with SessionLocal() as db:
        row = db.scalar(
            select(VaultItem).where(
                VaultItem.environment_id == env_id,
                VaultItem.name == kind,
            )
        )
        if row is None:
            return None, None
        return decrypt_secret(row.value_encrypted), row.kind


def test_api_grab_files_the_disk_copy_then_serves_the_vault(
    client, admin_headers, tmp_path, monkeypatch
):
    env, saved = _client_env(client, admin_headers, tmp_path)

    def runner(argv, *, timeout=30):
        raise AssertionError("grab must not ask the cluster")

    _patch_ctl(monkeypatch, runner)
    first = client.get(
        f"/api/v1/environments/{env['id']}/access/talosconfig",
        headers=admin_headers,
    )
    assert first.status_code == 200, first.text
    assert first.headers.get("x-genestack-credential") == "filed"
    assert "no-store" in first.headers.get("cache-control", "")
    assert first.content == _SAVED.encode()
    assert saved.read_text(encoding="utf-8") == _SAVED
    text, kind = _vault_copy(env["id"], "talosconfig")
    assert text == _SAVED
    assert kind == "talosconfig"

    saved.unlink()
    second = client.get(
        f"/api/v1/environments/{env['id']}/access/talosconfig",
        headers=admin_headers,
    )
    assert second.status_code == 200, second.text
    assert second.headers.get("x-genestack-credential") == "vault"
    assert second.content == _SAVED.encode()


def test_api_renew_replaces_the_vault_and_leaves_the_disk_file(
    client, admin_headers, tmp_path, monkeypatch
):
    env, saved = _client_env(client, admin_headers, tmp_path)
    calls: list[list[str]] = []

    def runner(argv, *, timeout=30):
        calls.append(list(argv))
        dest = Path(argv[argv.index("new") + 1])
        dest.write_text(_ISSUED_TALOS, encoding="utf-8")
        return 0

    _patch_ctl(monkeypatch, runner)
    resp = client.post(
        f"/api/v1/environments/{env['id']}/access/talosconfig",
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers.get("x-genestack-credential") == "issued"
    assert resp.content == _ISSUED_TALOS.encode()
    assert saved.read_text(encoding="utf-8") == _SAVED
    text, kind = _vault_copy(env["id"], "talosconfig")
    assert text == _ISSUED_TALOS
    assert kind == "talosconfig"
    assert calls and "new" in calls[0]
    assert calls[0][calls[0].index("--crt-ttl") + 1] == "8760h"

    saved.unlink()
    grabbed = client.get(
        f"/api/v1/environments/{env['id']}/access/talosconfig",
        headers=admin_headers,
    )
    assert grabbed.status_code == 200, grabbed.text
    assert grabbed.headers.get("x-genestack-credential") == "vault"
    assert grabbed.content == _ISSUED_TALOS.encode()
    assert len(calls) == 1


def test_api_renew_failure_leaves_the_vault_copy(
    client, admin_headers, tmp_path, monkeypatch
):
    env, saved = _client_env(client, admin_headers, tmp_path)
    filed = client.get(
        f"/api/v1/environments/{env['id']}/access/talosconfig",
        headers=admin_headers,
    )
    assert filed.status_code == 200, filed.text
    assert _vault_copy(env["id"], "talosconfig")[0] == _SAVED
    _patch_ctl(monkeypatch, lambda argv, *, timeout=30: 1)
    resp = client.post(
        f"/api/v1/environments/{env['id']}/access/talosconfig",
        headers=admin_headers,
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == (
        "The cluster did not issue a new certificate. "
        "The copy in the vault is unchanged."
    )
    assert _vault_copy(env["id"], "talosconfig")[0] == _SAVED
    assert saved.read_text(encoding="utf-8") == _SAVED


def test_api_missing_kubeconfig_stays_missing_when_the_cluster_does_not_issue(
    client, admin_headers, tmp_path, monkeypatch
):
    env, _saved = _client_env(client, admin_headers, tmp_path)
    _patch_ctl(monkeypatch, lambda argv, *, timeout=30: 1)
    missing = client.get(
        f"/api/v1/environments/{env['id']}/access/kubeconfig",
        headers=admin_headers,
    )
    assert missing.status_code == 404
    assert missing.json()["detail"] == "kubeconfig not found"
    refused = client.post(
        f"/api/v1/environments/{env['id']}/access/kubeconfig",
        headers=admin_headers,
    )
    assert refused.status_code == 409
    assert "unchanged" in refused.json()["detail"]
    assert _vault_copy(env["id"], "kubeconfig") == (None, None)


def test_environment_vault_lists_only_that_environments_names(client, admin_headers):
    tenant = _create_tenant(client, admin_headers)
    other = _create_tenant(client, admin_headers)
    home = client.post(
        "/api/v1/environments",
        headers=admin_headers,
        json={"name": f"vault-home-{uuid.uuid4().hex[:8]}", "tenant_id": tenant["id"]},
    ).json()
    away = client.post(
        "/api/v1/environments",
        headers=admin_headers,
        json={"name": f"vault-away-{uuid.uuid4().hex[:8]}", "tenant_id": tenant["id"]},
    ).json()
    stranger = client.post(
        "/api/v1/environments",
        headers=admin_headers,
        json={"name": f"vault-stranger-{uuid.uuid4().hex[:8]}", "tenant_id": other["id"]},
    ).json()
    for target, name in ((home, "alpha"), (away, "beta")):
        saved = client.put(
            "/api/v1/vault/items",
            headers=admin_headers,
            json={
                "tenant_id": tenant["id"],
                "environment_id": target["id"],
                "name": name,
                "value": f"secret-{name}",
            },
        )
        assert saved.status_code == 200, saved.text
        assert f"secret-{name}" not in saved.text
    listed = client.get(
        "/api/v1/vault/items",
        headers=admin_headers,
        params={"tenant_id": tenant["id"], "environment_id": home["id"]},
    )
    assert listed.status_code == 200, listed.text
    home_rows = listed.json()["items"]
    assert {row["name"] for row in home_rows} == {"secret/alpha", "ssh"}
    assert {row["environment_id"] for row in home_rows} == {home["id"]}
    assert next(row["kind"] for row in home_rows if row["name"] == "ssh") == "ssh"
    assert "secret-alpha" not in listed.text
    assert "beta" not in listed.text
    everything = client.get(
        "/api/v1/vault/items",
        headers=admin_headers,
        params={"tenant_id": tenant["id"]},
    )
    assert everything.status_code == 200, everything.text
    assert {"secret/alpha", "secret/beta"} <= {row["name"] for row in everything.json()["items"]}
    wrong = client.get(
        "/api/v1/vault/items",
        headers=admin_headers,
        params={"tenant_id": tenant["id"], "environment_id": stranger["id"]},
    )
    assert wrong.status_code == 404
    page = client.get("/ui")
    assert page.status_code == 200
    assert 'data-env-tab="vault"' in page.text
    script = client.get("/static/js/pages/environment_detail.js")
    assert script.status_code == 200
    assert 'data-tab="vault"' in script.text
    card = client.get("/static/js/pages/environment_vault.js")
    assert card.status_code == 200
    assert "Nothing is stored for this environment yet." in card.text
