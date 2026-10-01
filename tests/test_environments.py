"""Environment CRUD tests."""

from __future__ import annotations

import uuid
from pathlib import Path

from app.config import get_settings


def test_create_list_get_environment(client, admin_headers):
    """Create an environment, list it, then fetch by id."""
    name = f"env-{uuid.uuid4().hex[:10]}"
    create = client.post(
        "/api/v1/environments",
        headers=admin_headers,
        json={
            "name": name,
            "description": "integration test environment",
            "region": "lab",
            "tier": "dev",
        },
    )
    assert create.status_code in (200, 201), create.text
    created = create.json()
    eid = created.get("id")
    assert eid is not None, f"no id in create response: {created}"
    assert created.get("name") == name
    assert created["genestack_config_dir"].endswith(
        f"/environments/{name}/etc-genestack"
    )

    listed = client.get("/api/v1/environments", headers=admin_headers)
    assert listed.status_code == 200
    envs = listed.json()
    assert isinstance(envs, list)
    ids = {e.get("id") for e in envs}
    assert eid in ids, f"created env {eid} not in list {ids}"

    got = client.get(f"/api/v1/environments/{eid}", headers=admin_headers)
    assert got.status_code == 200, got.text
    body = got.json()
    assert body.get("id") == eid
    assert body.get("name") == name
    assert body["genestack_config_dir"].endswith(f"/environments/{name}/etc-genestack")


def test_create_environment_defaults_local_config_dir(client, admin_headers):
    """POST without genestack_config_dir stores a local-hub path under data_dir."""
    name = f"env-hub-{uuid.uuid4().hex[:10]}"
    create = client.post(
        "/api/v1/environments",
        headers=admin_headers,
        json={"name": name},
    )
    assert create.status_code in (200, 201), create.text
    created = create.json()
    expected = str(
        Path(get_settings().data_dir) / "environments" / name / "etc-genestack"
    )
    assert created["genestack_config_dir"] == expected
    assert created["genestack_config_dir"].endswith(
        f"/environments/{name}/etc-genestack"
    )
    assert Path(expected).is_dir()

    blank = client.post(
        "/api/v1/environments",
        headers=admin_headers,
        json={
            "name": f"env-blank-{uuid.uuid4().hex[:10]}",
            "genestack_config_dir": "  ",
        },
    )
    assert blank.status_code in (200, 201), blank.text
    blank_name = blank.json()["name"]
    assert blank.json()["genestack_config_dir"].endswith(
        f"/environments/{blank_name}/etc-genestack"
    )


def test_environment_inventory(client, admin_headers, viewer_headers):
    """GET /api/v1/environments/{id}/inventory returns an inventory dict."""
    name = f"env-inv-{uuid.uuid4().hex[:10]}"
    create = client.post(
        "/api/v1/environments",
        headers=admin_headers,
        json={
            "name": name,
            "deployer_ssh_host": "deployer-01.example.com",
            "deployer_ssh_user": "ubuntu",
        },
    )
    assert create.status_code in (200, 201), create.text
    eid = create.json()["id"]

    resp = client.get(f"/api/v1/environments/{eid}/inventory", headers=viewer_headers)
    assert resp.status_code == 200, resp.text
    inv = resp.json()
    assert isinstance(inv, dict)
    assert "all" in inv
    assert inv["all"]["vars"]["environment_id"] == eid
    assert "deployer-01.example.com" in inv["all"]["hosts"]

    missing = client.get(
        "/api/v1/environments/does-not-exist/inventory", headers=viewer_headers
    )
    assert missing.status_code == 404


def test_delete_environment_admin_only(
    client, admin_headers, operator_headers, viewer_headers
):
    """DELETE requires admin; removes the environment; 404 when missing."""
    name = f"env-del-{uuid.uuid4().hex[:10]}"
    create = client.post(
        "/api/v1/environments",
        headers=admin_headers,
        json={"name": name},
    )
    assert create.status_code in (200, 201), create.text
    eid = create.json()["id"]

    resp = client.delete(f"/api/v1/environments/{eid}", headers=viewer_headers)
    assert resp.status_code == 403, resp.text
    resp = client.delete(f"/api/v1/environments/{eid}", headers=operator_headers)
    assert resp.status_code == 403, resp.text

    resp = client.delete(f"/api/v1/environments/{eid}", headers=admin_headers)
    assert resp.status_code in (200, 204), resp.text

    got = client.get(f"/api/v1/environments/{eid}", headers=admin_headers)
    assert got.status_code == 404

    again = client.delete(f"/api/v1/environments/{eid}", headers=admin_headers)
    assert again.status_code == 404


def test_maas_api_key_masked_in_responses(client, admin_headers, viewer_headers):
    """Stored MAAS API key is masked in create/detail/list responses."""
    from app.db import SessionLocal
    from app.models import Environment

    name = f"env-mask-{uuid.uuid4().hex[:10]}"
    real_key = "consumer-key:token-key:token-secret"
    create = client.post(
        "/api/v1/environments",
        headers=admin_headers,
        json={"name": name, "maas_api_key_encrypted": real_key},
    )
    assert create.status_code in (200, 201), create.text
    eid = create.json()["id"]
    assert create.json()["maas_api_key_encrypted"] == "********"

    got = client.get(f"/api/v1/environments/{eid}", headers=viewer_headers)
    assert got.status_code == 200, got.text
    assert got.json()["maas_api_key_encrypted"] == "********"

    listed = client.get("/api/v1/environments", headers=viewer_headers)
    assert listed.status_code == 200
    match = [e for e in listed.json() if e.get("id") == eid]
    assert match, f"env {eid} not in list"
    assert match[0]["maas_api_key_encrypted"] == "********"

    # PATCH echoing the masked sentinel leaves the stored key unchanged
    patch = client.patch(
        f"/api/v1/environments/{eid}",
        headers=admin_headers,
        json={
            "maas_api_key_encrypted": "********",
            "description": "sentinel round-trip",
        },
    )
    assert patch.status_code == 200, patch.text
    assert patch.json()["maas_api_key_encrypted"] == "********"

    db = SessionLocal()
    try:
        env = db.get(Environment, eid)
        assert env is not None
        # Stored encrypted at rest (fernet:); decrypts back to the real key
        from app.services.crypto import decrypt_secret

        assert env.maas_api_key_encrypted.startswith("fernet:")
        assert decrypt_secret(env.maas_api_key_encrypted) == real_key
    finally:
        db.close()

    # Env without a key returns null, not the mask
    bare = client.post(
        "/api/v1/environments",
        headers=admin_headers,
        json={"name": f"env-nomask-{uuid.uuid4().hex[:10]}"},
    )
    assert bare.status_code in (200, 201), bare.text
    assert bare.json()["maas_api_key_encrypted"] is None


def test_env_handle_fields_masked_and_encrypted(client, admin_headers, viewer_headers):
    """genestack_config_dir / kubeconfig_data / dry_run round-trip; secrets never leak."""
    from app.db import SessionLocal
    from app.models import Environment
    from app.services.crypto import FERNET_PREFIX, decrypt_secret

    kubeconfig = "apiVersion: v1\nclusters: []\n"
    name = f"env-handle-{uuid.uuid4().hex[:10]}"
    create = client.post(
        "/api/v1/environments",
        headers=admin_headers,
        json={
            "name": name,
            "genestack_config_dir": "/etc/genestack-lab",
            "kubeconfig_data": kubeconfig,
            "dry_run": False,
        },
    )
    assert create.status_code in (200, 201), create.text
    created = create.json()
    eid = created["id"]
    # kubeconfig payload is masked, never returned
    assert created["kubeconfig_data"] == "***"
    assert created["genestack_config_dir"] == "/etc/genestack-lab"
    assert created["dry_run"] is False

    got = client.get(f"/api/v1/environments/{eid}", headers=viewer_headers)
    assert got.status_code == 200, got.text
    assert got.json()["kubeconfig_data"] == "***"

    listed = client.get("/api/v1/environments", headers=viewer_headers)
    match = [e for e in listed.json() if e.get("id") == eid]
    assert match and match[0]["kubeconfig_data"] == "***"

    # Stored value is fernet-encrypted and decrypts back to the payload
    db = SessionLocal()
    try:
        env = db.get(Environment, eid)
        assert env is not None
        assert env.kubeconfig_data.startswith(FERNET_PREFIX)
        assert decrypt_secret(env.kubeconfig_data) == kubeconfig
        assert env.dry_run is False
    finally:
        db.close()

    # PATCH updates the fields; echoing the mask leaves the stored blob unchanged
    patch = client.patch(
        f"/api/v1/environments/{eid}",
        headers=admin_headers,
        json={"kubeconfig_data": "***", "dry_run": True},
    )
    assert patch.status_code == 200, patch.text
    assert patch.json()["dry_run"] is True
    assert patch.json()["kubeconfig_data"] == "***"

    db = SessionLocal()
    try:
        env = db.get(Environment, eid)
        assert decrypt_secret(env.kubeconfig_data) == kubeconfig
        assert env.dry_run is True
    finally:
        db.close()

    # Env without kubeconfig returns null, not the mask
    bare = client.post(
        "/api/v1/environments",
        headers=admin_headers,
        json={"name": f"env-nokube-{uuid.uuid4().hex[:10]}"},
    )
    assert bare.status_code in (200, 201), bare.text
    assert bare.json()["kubeconfig_data"] is None
    assert bare.json()["dry_run"] is None
