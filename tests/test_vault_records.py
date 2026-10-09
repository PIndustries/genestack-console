"""View, replace, and delete one environment vault record.

The list stays names only. A value is returned to an admin and is absent
from the list body and the audit row.
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select

from app.db import SessionLocal
from app.models import BaremetalNode, Environment, VaultItem
from app.services.crypto import encrypt_secret
from app.services.ssh_keys import generate_ed25519_key
from tests.test_tenants import _create_tenant

_KUBE = "apiVersion: v1\nkind: Config\nclusters:\n- name: c\n"
_TALOS = "context: lab\ncontexts:\n  lab:\n    ca: QQ\n"
_TALOS_DISK = "context: disk\ncontexts:\n  disk:\n    ca: DISK\n"


def _env(client, admin_headers):
    tenant = _create_tenant(client, admin_headers)
    created = client.post(
        "/api/v1/environments",
        headers=admin_headers,
        json={
            "name": f"vault-rec-{uuid.uuid4().hex[:8]}",
            "tenant_id": tenant["id"],
        },
    )
    assert created.status_code in (200, 201), created.text
    return tenant, created.json()


def _params(tenant, env, name):
    return {
        "tenant_id": tenant["id"],
        "environment_id": env["id"],
        "name": name,
    }


def _put(client, headers, tenant, env, name, value):
    return client.put(
        "/api/v1/vault/records",
        headers=headers,
        json={
            "tenant_id": tenant["id"],
            "environment_id": env["id"],
            "name": name,
            "value": value,
        },
    )


def _names(client, headers, tenant, env):
    listed = client.get(
        "/api/v1/vault/items",
        headers=headers,
        params={"tenant_id": tenant["id"], "environment_id": env["id"]},
    )
    assert listed.status_code == 200, listed.text
    return listed


def _audits(client, headers, env_id, action):
    listed = client.get(
        "/api/v1/audit",
        headers=headers,
        params={"environment_id": env_id, "action": action, "limit": 20},
    )
    assert listed.status_code == 200, listed.text
    return listed


def _node(env_id, name, password):
    with SessionLocal() as db:
        node = BaremetalNode(
            environment_id=env_id,
            name=name,
            bmc_host="bmc.example.com",
            bmc_username="root",
            bmc_password=encrypt_secret(password),
        )
        db.add(node)
        db.commit()
        return node.id


def test_records_require_admin(client, admin_headers, operator_headers, viewer_headers):
    tenant, env = _env(client, admin_headers)
    secret = f"note-{uuid.uuid4().hex}"
    saved = _put(client, admin_headers, tenant, env, "lab", secret)
    assert saved.status_code == 200, saved.text
    assert secret not in saved.text
    assert "value" not in saved.json()
    params = _params(tenant, env, "secret/lab")
    for headers in (operator_headers, viewer_headers):
        denied = client.get("/api/v1/vault/records", headers=headers, params=params)
        assert denied.status_code == 403
        assert secret not in denied.text
        refused = _put(client, headers, tenant, env, "lab", f"other-{secret}")
        assert refused.status_code == 403
        assert secret not in refused.text
        removed = client.delete("/api/v1/vault/records", headers=headers, params=params)
        assert removed.status_code == 403
    opened = client.get("/api/v1/vault/records", headers=admin_headers, params=params)
    assert opened.status_code == 200, opened.text
    assert opened.json()["value"] == secret
    assert opened.headers.get("cache-control") == "no-store"
    listed = _names(client, viewer_headers, tenant, env)
    assert secret not in listed.text
    assert "secret/lab" in {row["name"] for row in listed.json()["items"]}


def test_other_tenant_record_is_hidden(client, admin_headers):
    tenant, env = _env(client, admin_headers)
    other = _create_tenant(client, admin_headers)
    secret = f"hidden-{uuid.uuid4().hex}"
    assert _put(client, admin_headers, tenant, env, "lab", secret).status_code == 200
    denied = client.get(
        "/api/v1/vault/records",
        headers=admin_headers,
        params={
            "tenant_id": other["id"],
            "environment_id": env["id"],
            "name": "secret/lab",
        },
    )
    assert denied.status_code == 404
    assert denied.json()["detail"] == "Environment not found"
    assert secret not in denied.text


def test_ssh_record_replaces_the_public_key(
    client, admin_headers, operator_headers, viewer_headers
):
    tenant, env = _env(client, admin_headers)
    first = generate_ed25519_key("vault-one")
    second = generate_ed25519_key("vault-two")
    marker = f"not-a-key-{uuid.uuid4().hex}"
    saved = _put(client, admin_headers, tenant, env, "ssh", first["private"])
    assert saved.status_code == 200, saved.text
    assert "BEGIN OPENSSH PRIVATE KEY" not in saved.text
    public = client.get(
        f"/api/v1/environments/{env['id']}/ssh-key/public",
        headers=admin_headers,
    )
    assert public.status_code == 200, public.text
    assert public.json()["public_key"].split()[1] == first["public"].split()[1]
    opened = client.get(
        "/api/v1/vault/records",
        headers=admin_headers,
        params=_params(tenant, env, "ssh"),
    )
    assert opened.json()["value"] == first["private"].strip()
    assert opened.json()["kind"] == "ssh"
    rejected = _put(client, admin_headers, tenant, env, "ssh", marker)
    assert rejected.status_code == 400
    assert marker not in rejected.text
    assert "unencrypted SSH private key" in rejected.json()["detail"]
    still = client.get(
        f"/api/v1/environments/{env['id']}/ssh-key/public",
        headers=admin_headers,
    )
    assert still.json()["public_key"].split()[1] == first["public"].split()[1]
    replaced = _put(client, admin_headers, tenant, env, "ssh", second["private"])
    assert replaced.status_code == 200, replaced.text
    changed = client.get(
        f"/api/v1/environments/{env['id']}/ssh-key/public",
        headers=admin_headers,
    )
    assert changed.json()["public_key"].split()[1] == second["public"].split()[1]
    operator = _put(client, operator_headers, tenant, env, "ssh", first["private"])
    assert operator.status_code == 403
    kept = client.get(
        f"/api/v1/environments/{env['id']}/ssh-key/public",
        headers=admin_headers,
    )
    assert kept.json()["public_key"].split()[1] == second["public"].split()[1]
    removed = client.delete(
        "/api/v1/vault/records",
        headers=admin_headers,
        params=_params(tenant, env, "ssh"),
    )
    assert removed.status_code == 200, removed.text
    assert "value" not in removed.json()
    gone = client.get(
        f"/api/v1/environments/{env['id']}/ssh-key/public",
        headers=viewer_headers,
    )
    assert gone.json()["has_key"] is False
    listed = _names(client, admin_headers, tenant, env)
    assert "ssh" not in {row["name"] for row in listed.json()["items"]}
    assert "BEGIN OPENSSH PRIVATE KEY" not in listed.text
    regenerated = client.post(
        f"/api/v1/environments/{env['id']}/ssh-key/regenerate",
        headers=operator_headers,
    )
    assert regenerated.status_code == 200, regenerated.text
    back = _names(client, admin_headers, tenant, env)
    assert "ssh" in {row["name"] for row in back.json()["items"]}
    audit = _audits(client, admin_headers, env["id"], "vault.record.read")
    assert "BEGIN OPENSSH PRIVATE KEY" not in audit.text
    assert audit.json()[0]["details"] == {"name": "ssh", "kind": "ssh"}


def test_bmc_password_updates_and_delete_keeps_the_machine(client, admin_headers):
    tenant, env = _env(client, admin_headers)
    machine = f"node-{uuid.uuid4().hex[:8]}"
    original = f"bmc-{uuid.uuid4().hex}"
    updated = f"bmc-next-{uuid.uuid4().hex}"
    node_id = _node(env["id"], machine, original)
    name = f"bmc/{machine}"
    opened = client.get(
        "/api/v1/vault/records",
        headers=admin_headers,
        params=_params(tenant, env, name),
    )
    assert opened.status_code == 200, opened.text
    assert opened.json()["value"] == original
    assert opened.json()["kind"] == "bmc"
    listed = _names(client, admin_headers, tenant, env)
    assert original not in listed.text
    assert name in {row["name"] for row in listed.json()["items"]}
    saved = _put(client, admin_headers, tenant, env, name, updated)
    assert saved.status_code == 200, saved.text
    assert updated not in saved.text
    again = client.get(
        "/api/v1/vault/records",
        headers=admin_headers,
        params=_params(tenant, env, name),
    )
    assert again.json()["value"] == updated
    missing = _put(client, admin_headers, tenant, env, "bmc/missing-machine", updated)
    assert missing.status_code == 404
    assert missing.json()["detail"] == "That machine is not in this environment."
    removed = client.delete(
        "/api/v1/vault/records",
        headers=admin_headers,
        params=_params(tenant, env, name),
    )
    assert removed.status_code == 200, removed.text
    after = _names(client, admin_headers, tenant, env)
    assert name not in {row["name"] for row in after.json()["items"]}
    assert updated not in after.text
    with SessionLocal() as db:
        node = db.get(BaremetalNode, node_id)
        assert node is not None
        assert node.bmc_password == ""
        count = db.scalar(
            select(func.count(BaremetalNode.id)).where(
                BaremetalNode.environment_id == env["id"]
            )
        )
        assert count == 1
    audit = _audits(client, admin_headers, env["id"], "vault.record.write")
    assert updated not in audit.text
    assert original not in audit.text
    assert {"name": name, "kind": "bmc"} in [row["details"] for row in audit.json()]


def test_client_config_edit_rejects_bad_text_and_leaves_the_disk_file(
    client, admin_headers, operator_headers, tmp_path, monkeypatch
):
    tenant, env = _env(client, admin_headers)
    disk = tmp_path / "talos" / "talosconfig"
    disk.parent.mkdir()
    disk.write_text(_TALOS_DISK, encoding="utf-8")
    patched = client.patch(
        f"/api/v1/environments/{env['id']}",
        headers=admin_headers,
        json={"genestack_config_dir": str(tmp_path), "dry_run": False},
    )
    assert patched.status_code in (200, 204), patched.text

    def fail_talosctl():
        raise AssertionError("editing a vault record must not call talosctl")

    monkeypatch.setattr("app.services.clientconfig.talosctl_bin", fail_talosctl)
    bad = _put(client, admin_headers, tenant, env, "talosconfig", "not a config")
    assert bad.status_code == 400
    assert bad.json()["detail"] == "That value is not a talosconfig."
    assert "not a config" not in bad.text
    saved = _put(client, admin_headers, tenant, env, "talosconfig", _TALOS)
    assert saved.status_code == 200, saved.text
    assert _TALOS not in saved.text
    worse = _put(client, admin_headers, tenant, env, "talosconfig", "still not")
    assert worse.status_code == 400
    opened = client.get(
        "/api/v1/vault/records",
        headers=admin_headers,
        params=_params(tenant, env, "talosconfig"),
    )
    assert opened.json()["value"] == _TALOS
    grabbed = client.get(
        f"/api/v1/environments/{env['id']}/access/talosconfig",
        headers=operator_headers,
    )
    assert grabbed.status_code == 200, grabbed.text
    assert grabbed.text == _TALOS
    assert grabbed.headers.get("x-genestack-credential") == "vault"
    assert disk.read_text(encoding="utf-8") == _TALOS_DISK
    kube = _put(client, admin_headers, tenant, env, "kubeconfig", _KUBE)
    assert kube.status_code == 200, kube.text
    refused = _put(client, admin_headers, tenant, env, "kubeconfig", "nope")
    assert refused.status_code == 400
    removed = client.delete(
        "/api/v1/vault/records",
        headers=admin_headers,
        params=_params(tenant, env, "talosconfig"),
    )
    assert removed.status_code == 200, removed.text
    assert disk.read_text(encoding="utf-8") == _TALOS_DISK
    filed = client.get(
        f"/api/v1/environments/{env['id']}/access/talosconfig",
        headers=operator_headers,
    )
    assert filed.status_code == 200, filed.text
    assert filed.text == _TALOS_DISK
    assert filed.headers.get("x-genestack-credential") == "filed"
    assert disk.read_text(encoding="utf-8") == _TALOS_DISK
    with SessionLocal() as db:
        row = db.scalar(
            select(VaultItem).where(
                VaultItem.environment_id == env["id"],
                VaultItem.name == "talosconfig",
            )
        )
        assert row is not None
        kube_row = db.get(Environment, env["id"])
        assert kube_row is not None


def test_note_round_trip_and_unreadable_secret(client, admin_headers):
    tenant, env = _env(client, admin_headers)
    secret = f"note-{uuid.uuid4().hex}"
    changed = f"note-next-{uuid.uuid4().hex}"
    saved = _put(client, admin_headers, tenant, env, "alpha", secret)
    assert saved.status_code == 200, saved.text
    assert saved.json()["name"] == "secret/alpha"
    listed = _names(client, admin_headers, tenant, env)
    assert secret not in listed.text
    updated = _put(client, admin_headers, tenant, env, "secret/alpha", changed)
    assert updated.status_code == 200, updated.text
    opened = client.get(
        "/api/v1/vault/records",
        headers=admin_headers,
        params=_params(tenant, env, "secret/alpha"),
    )
    assert opened.json()["value"] == changed
    assert opened.json()["kind"] == "note"
    removed = client.delete(
        "/api/v1/vault/records",
        headers=admin_headers,
        params=_params(tenant, env, "alpha"),
    )
    assert removed.status_code == 200, removed.text
    missing = client.get(
        "/api/v1/vault/records",
        headers=admin_headers,
        params=_params(tenant, env, "secret/alpha"),
    )
    assert missing.status_code == 404
    with SessionLocal() as db:
        row = db.get(Environment, env["id"])
        row.ssh_private_key_encrypted = "fernet:not-a-token"
        db.commit()
    broken = client.get(
        "/api/v1/vault/records",
        headers=admin_headers,
        params=_params(tenant, env, "ssh"),
    )
    assert broken.status_code == 400
    assert broken.json()["detail"] == "That record could not be read."
    assert "not-a-token" not in broken.text
    audit = _audits(client, admin_headers, env["id"], "vault.record.delete")
    assert changed not in audit.text
    assert secret not in audit.text


def test_vault_page_offers_record_actions(client):
    page = client.get("/ui")
    assert page.status_code == 200
    assert "/static/js/app.js?v=" in page.text
    assert "v=ls63" in page.text
    detail = client.get("/static/js/pages/environment_detail.js")
    assert 'environment_vault.js?v=ls63' in detail.text
    card = client.get("/static/js/pages/environment_vault.js")
    assert card.status_code == 200
    text = card.text
    assert "data-env-vault-view" in text
    assert "data-env-vault-edit" in text
    assert "data-env-vault-del" in text
    assert "Nothing is stored for this environment yet." in text
    assert 'els.area.value = value || ""' in text
    assert "innerHTML = payload" not in text
    shell = client.get("/static/js/app.js")
    assert 'environment_detail.js?v=ls63' in shell.text
