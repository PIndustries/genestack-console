"""NetApp cinder credential rotation through the console config push.

The open recommendation: operators can UPDATE the NetApp username/password
via PUT /api/v1/environments/{id}/config (the ``secrets:`` section) and the
next ``genestack.config.push`` re-renders the ``netapp-cinder-backend``
k8s Secret (and the pushed files) with the new values — no environment
re-creation, no hand-editing the deploy host's kubesecrets.yaml.

Rotation path:
    PUT /config (secrets.netapp-cinder-backend.data.{username,password})
        -> envconfig.put_version (fernet-encrypted at rest, sentinel "***"
           keeps previously stored values on masked re-PUT)
        -> genestack.config.push job (operator+)
        -> render_to_files (decrypts stored values, base64 into the
           netapp-cinder-backend Secret manifest)
        -> push_rendered (merges kubesecrets.yaml by Secret name; the
           console entry wins on conflict so rotated values replace stale
           generated ones)
"""

from __future__ import annotations

import base64
import uuid

import yaml
from sqlalchemy import select

from app.db import SessionLocal
from app.models import EnvConfigVersion, Environment
from app.services import envconfig as envconfig_service
from app.services.crypto import decrypt_secret
from tests.test_config_push import _capture_local_writes, _kubesecret_writes

OLD_USER = "netapp-admin"
OLD_PASS = "old-netapp-pw"
NEW_USER = "cinder-svc-2"
NEW_PASS = "rotated-netapp-pw-777"

OLD_DOC = f"""\
secrets:
  netapp-cinder-backend:
    namespace: openstack
    data:
      username: {OLD_USER}
      password: {OLD_PASS}
"""

# Shape mirrors bin/create-secrets.sh output: a generated
# netapp-cinder-backend entry (stale creds) plus an unrelated secret that
# push must never rotate ("no mass rotation" rule).
GENERATED_WITH_NETAPP = f"""\
---
apiVersion: v1
kind: Secret
metadata:
  name: mariadb
  namespace: openstack
type: Opaque
data:
  root-password: {base64.b64encode(b"generated-root-pw").decode()}
  password: {base64.b64encode(b"generated-mariadb-pw").decode()}
---
apiVersion: v1
kind: Secret
metadata:
  name: netapp-cinder-backend
  namespace: openstack
type: Opaque
data:
  username: {base64.b64encode(OLD_USER.encode()).decode()}
  password: {base64.b64encode(OLD_PASS.encode()).decode()}
"""


def _b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


def _suffix() -> str:
    return uuid.uuid4().hex[:8]


def _create_env(client, headers, config_dir, **fields) -> dict:
    resp = client.post(
        "/api/v1/environments",
        headers=headers,
        json={
            "name": f"netapp-rot-{_suffix()}",
            "genestack_config_dir": str(config_dir),
            **fields,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _put_doc(client, headers, env_id, yaml_text) -> int:
    resp = client.put(
        f"/api/v1/environments/{env_id}/config",
        headers=headers,
        json={"yaml_text": yaml_text},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["version"]


def _push_job(client, headers, env_id, run_sync=True):
    resp = client.post(
        f"/api/v1/environments/{env_id}/jobs",
        headers=headers,
        json={"operation": "genestack.config.push", "params": {}, "run_sync": run_sync},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _manifests_by_name(text: str) -> dict:
    return {d["metadata"]["name"]: d for d in yaml.safe_load_all(text)}


def _rotate_via_api(client, headers, env_id) -> int:
    """Operator flow: read the masked doc, set the new creds, PUT it back."""
    current = client.get(f"/api/v1/environments/{env_id}/config", headers=headers)
    assert current.status_code == 200, current.text
    doc = yaml.safe_load(current.json()["yaml"])
    doc["secrets"]["netapp-cinder-backend"]["data"]["username"] = NEW_USER
    doc["secrets"]["netapp-cinder-backend"]["data"]["password"] = NEW_PASS
    return _put_doc(client, headers, env_id, yaml.safe_dump(doc))


def test_rotation_initial_push_renders_stored_creds(
    client, admin_headers, tmp_path, monkeypatch
):
    """(a) create env with netapp user/pass, push -> Secret carries b64(old).

    The job removes kubesecrets.yaml when it finishes. The bytes the push
    wrote are what this checks.
    """
    config_dir = tmp_path / "etc-genestack"
    config_dir.mkdir()
    captured = _capture_local_writes(monkeypatch)
    env = _create_env(client, admin_headers, config_dir, dry_run=False)
    _put_doc(client, admin_headers, env["id"], OLD_DOC)

    job = _push_job(client, admin_headers, env["id"])
    assert job["status"] == "success", job["error"]

    writes = _kubesecret_writes(captured, config_dir)
    assert len(writes) == 1
    merged = _manifests_by_name(writes[0].decode())
    assert merged["netapp-cinder-backend"]["data"] == {
        "username": _b64(OLD_USER),
        "password": _b64(OLD_PASS),
    }
    assert merged["netapp-cinder-backend"]["metadata"]["namespace"] == "openstack"
    assert not (config_dir / "kubesecrets.yaml").exists()
    log_text = client.get(f"/api/v1/jobs/{job['id']}", headers=admin_headers).json()[
        "log_text"
    ]
    assert OLD_PASS not in log_text


def test_rotation_update_via_config_api_re_renders_secret(
    client, admin_headers, tmp_path, monkeypatch
):
    """(b) update creds via the config API and push again.

    The first push merges the generated mariadb entry with the stored creds.
    That job then removes kubesecrets.yaml, so the second push writes only
    the rotated Secret. The backup of the original generated file still has
    the old password.
    """
    config_dir = tmp_path / "etc-genestack"
    config_dir.mkdir()
    (config_dir / "kubesecrets.yaml").write_text(
        GENERATED_WITH_NETAPP, encoding="utf-8"
    )
    captured = _capture_local_writes(monkeypatch)

    env = _create_env(client, admin_headers, config_dir, dry_run=False)
    _put_doc(client, admin_headers, env["id"], OLD_DOC)

    first = _push_job(client, admin_headers, env["id"])
    assert first["status"] == "success", first["error"]

    version = _rotate_via_api(client, admin_headers, env["id"])
    assert version == 2

    second = _push_job(client, admin_headers, env["id"])
    assert second["status"] == "success", second["error"]

    writes = _kubesecret_writes(captured, config_dir)
    assert len(writes) == 2
    first_merged = _manifests_by_name(writes[0].decode())
    assert first_merged["mariadb"]["data"] == {
        "root-password": _b64("generated-root-pw"),
        "password": _b64("generated-mariadb-pw"),
    }
    assert first_merged["netapp-cinder-backend"]["data"]["password"] == _b64(OLD_PASS)

    text = writes[1].decode()
    merged = _manifests_by_name(text)
    assert set(merged) == {"netapp-cinder-backend"}
    assert merged["netapp-cinder-backend"]["data"] == {
        "username": _b64(NEW_USER),
        "password": _b64(NEW_PASS),
    }
    assert _b64(OLD_PASS) not in text
    assert _b64(OLD_USER) not in text
    assert OLD_PASS not in text
    assert OLD_USER not in text
    assert not (config_dir / "kubesecrets.yaml").exists()
    # The job removes the live file and its backup, so the old password is
    # not left under the config directory.
    assert list((config_dir / ".console-backup").glob("*/kubesecrets.yaml")) == []
    leftover = [
        path
        for path in config_dir.rglob("*")
        if path.is_file()
        and OLD_PASS in path.read_text(encoding="utf-8", errors="replace")
    ]
    assert leftover == []
    for job in (first, second):
        log_text = client.get(
            f"/api/v1/jobs/{job['id']}", headers=admin_headers
        ).json()["log_text"]
        assert OLD_PASS not in log_text
        assert NEW_PASS not in log_text


def test_rotation_dry_run_renders_new_values(client, admin_headers, tmp_path):
    """Dry-run push after rotation: nothing written, but the rendered
    kubesecrets.yaml the push plans to write carries the new values."""
    config_dir = tmp_path / "etc-genestack"
    config_dir.mkdir()
    env = _create_env(client, admin_headers, config_dir)  # dry_run=True
    _put_doc(client, admin_headers, env["id"], OLD_DOC)

    job = _push_job(client, admin_headers, env["id"])
    assert job["status"] == "success", job["error"]
    _rotate_via_api(client, admin_headers, env["id"])

    job = _push_job(client, admin_headers, env["id"])
    assert job["status"] == "success", job["error"]
    log_text = client.get(f"/api/v1/jobs/{job['id']}", headers=admin_headers).json()[
        "log_text"
    ]
    assert "[dry-run] would write" in log_text
    assert "kubesecrets.yaml" in log_text

    # Still nothing on disk
    assert list(config_dir.rglob("*")) == []

    # The same render the job used (get_current + render_to_files) reflects
    # the rotated values
    db = SessionLocal()
    try:
        env_row = db.get(Environment, env["id"])
        doc, _row = envconfig_service.get_current(db, env_row)
        files = envconfig_service.render_to_files(doc, env_row)
    finally:
        db.close()
    rendered = _manifests_by_name(files["kubesecrets.yaml"])
    assert rendered["netapp-cinder-backend"]["data"] == {
        "username": _b64(NEW_USER),
        "password": _b64(NEW_PASS),
    }


def test_rotation_viewer_cannot_update(
    client, admin_headers, operator_headers, viewer_headers, tmp_path, monkeypatch
):
    """(c) Role gating per the existing config rules: PUT /config requires
    operator+ (viewer -> 403), and operator may rotate (the rule is not
    admin-only — same as every other config field)."""
    config_dir = tmp_path / "etc-genestack"
    config_dir.mkdir()
    captured = _capture_local_writes(monkeypatch)
    env = _create_env(client, admin_headers, config_dir, dry_run=False)
    _put_doc(client, admin_headers, env["id"], OLD_DOC)

    resp = client.put(
        f"/api/v1/environments/{env['id']}/config",
        headers=viewer_headers,
        json={"yaml_text": OLD_DOC},
    )
    assert resp.status_code == 403
    # Viewers can't trigger the push either
    resp = client.post(
        f"/api/v1/environments/{env['id']}/jobs",
        headers=viewer_headers,
        json={"operation": "genestack.config.push", "params": {}, "run_sync": True},
    )
    assert resp.status_code == 403

    # operator CAN rotate, and the rotation lands in the pushed Secret
    version = _rotate_via_api(client, operator_headers, env["id"])
    assert version == 2
    job = _push_job(client, operator_headers, env["id"])
    assert job["status"] == "success", job["error"]
    writes = _kubesecret_writes(captured, config_dir)
    assert len(writes) == 1
    merged = _manifests_by_name(writes[0].decode())
    assert merged["netapp-cinder-backend"]["data"] == {
        "username": _b64(NEW_USER),
        "password": _b64(NEW_PASS),
    }
    assert not (config_dir / "kubesecrets.yaml").exists()


def test_rotation_stored_at_rest_is_not_plaintext(client, admin_headers):
    """(d) Both config versions store the creds fernet-encrypted; the DB row
    never holds the plaintext old or new password."""
    resp = client.post(
        "/api/v1/environments",
        headers=admin_headers,
        json={"name": f"netapp-rot-{_suffix()}", "dry_run": True},
    )
    assert resp.status_code == 201, resp.text
    env_id = resp.json()["id"]
    _put_doc(client, admin_headers, env_id, OLD_DOC)
    _rotate_via_api(client, admin_headers, env_id)

    db = SessionLocal()
    try:
        rows = db.scalars(
            select(EnvConfigVersion)
            .where(EnvConfigVersion.environment_id == env_id)
            .order_by(EnvConfigVersion.version.asc())
        ).all()
    finally:
        db.close()
    assert [row.version for row in rows] == [1, 2]
    for row in rows:
        assert OLD_PASS not in row.yaml_text
        assert NEW_PASS not in row.yaml_text
        assert "fernet:" in row.yaml_text

    v2 = rows[1]
    doc, _warnings = envconfig_service.parse_document(v2.yaml_text)
    data = doc["secrets"]["netapp-cinder-backend"]["data"]
    assert data["username"].startswith("fernet:")
    assert data["password"].startswith("fernet:")
    assert decrypt_secret(data["username"]) == NEW_USER
    assert decrypt_secret(data["password"]) == NEW_PASS
    doc1, _ = envconfig_service.parse_document(rows[0].yaml_text)
    assert (
        decrypt_secret(doc1["secrets"]["netapp-cinder-backend"]["data"]["password"])
        == OLD_PASS
    )
