"""Rook/Ceph rendering: storage.ceph -> kustomize/rook-ceph/overlay.

The config doc renders a kustomize overlay (like kustomize_patches renders for
keystone) that references the repo's upstream rook trees; bootstrap.sh symlinks
base-kustomize/<svc>/base into <config dir>/kustomize/<svc>/base, so the
overlay resolves against ../<svc>/base at push/deploy time.
"""

from __future__ import annotations

import uuid

import pytest
import yaml

from app.models import Environment
from app.services import envconfig as envconfig_service

ROOK_OVERLAY_PATH = "kustomize/rook-ceph/overlay/kustomization.yaml"

CEPH_DOC = """\
storage:
  ceph:
    enabled: true
"""

EXTERNAL_PVC_DOC = """\
storage:
  ceph:
    enabled: true
    external_pvc: true
"""

CEPH_DISABLED_DOC = """\
storage:
  ceph:
    enabled: false
"""


def _suffix() -> str:
    return uuid.uuid4().hex[:8]


def _create_env(client, headers, **fields) -> dict:
    resp = client.post(
        "/api/v1/environments",
        headers=headers,
        json={"name": f"env-rook-{_suffix()}", **fields},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _put_doc(client, headers, env_id, yaml_text):
    resp = client.put(
        f"/api/v1/environments/{env_id}/config",
        headers=headers,
        json={"yaml_text": yaml_text},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# render_to_files (service level)
# ---------------------------------------------------------------------------


def test_render_ceph_enabled_rook_overlay():
    """storage.ceph: {enabled: true} renders the rook-ceph kustomize overlay."""
    env = Environment(id="env-x", name="env-x")
    files = envconfig_service.render_to_files(yaml.safe_load(CEPH_DOC), env)
    assert set(files) == {ROOK_OVERLAY_PATH}
    assert yaml.safe_load(files[ROOK_OVERLAY_PATH]) == {
        "apiVersion": "kustomize.config.k8s.io/v1beta1",
        "kind": "Kustomization",
        "resources": [
            "../rook-operator/base",
            "../rook-defaults/base",
            "../rook-cluster/base",
        ],
    }


def test_render_ceph_external_pvc_variant():
    """external_pvc selects the *-external-pvc upstream trees."""
    env = Environment(id="env-x", name="env-x")
    files = envconfig_service.render_to_files(yaml.safe_load(EXTERNAL_PVC_DOC), env)
    assert set(files) == {ROOK_OVERLAY_PATH}
    assert yaml.safe_load(files[ROOK_OVERLAY_PATH]) == {
        "apiVersion": "kustomize.config.k8s.io/v1beta1",
        "kind": "Kustomization",
        "resources": [
            "../rook-operator/base",
            "../rook-defaults-external-pvc/base",
            "../rook-cluster-external-pvc/base",
        ],
    }


def test_render_ceph_disabled_no_files():
    env = Environment(id="env-x", name="env-x")
    files = envconfig_service.render_to_files(yaml.safe_load(CEPH_DISABLED_DOC), env)
    assert files == {}


def test_render_no_storage_no_rook_files():
    env = Environment(id="env-x", name="env-x")
    files = envconfig_service.render_to_files({"components": {"keystone": True}}, env)
    assert set(files) == {"openstack-components.yaml"}


def test_render_ceph_alone_enables_overlay():
    """Presence of a non-empty ceph mapping enables rook (enabled defaults on)."""
    env = Environment(id="env-x", name="env-x")
    files = envconfig_service.render_to_files(
        {"storage": {"ceph": {"external_pvc": False}}}, env
    )
    assert set(files) == {ROOK_OVERLAY_PATH}


def test_render_ceph_merges_with_cinder_vars():
    """ceph and cinder_* render side by side."""
    env = Environment(id="env-x", name="env-x")
    doc = yaml.safe_load(CEPH_DOC)
    doc["storage"]["cinder_backend_name"] = "netapp-iscsi-1"
    files = envconfig_service.render_to_files(doc, env)
    assert set(files) == {
        ROOK_OVERLAY_PATH,
        "inventory/group_vars/cinder_storage_nodes/console-rendered.yml",
    }
    assert yaml.safe_load(
        files["inventory/group_vars/cinder_storage_nodes/console-rendered.yml"]
    ) == {"cinder_backend_name": "netapp-iscsi-1"}


# ---------------------------------------------------------------------------
# parse_document validation
# ---------------------------------------------------------------------------


def test_parse_ceph_known_keys_no_warnings():
    _doc, warnings = envconfig_service.parse_document(CEPH_DOC)
    assert warnings == []
    _doc, warnings = envconfig_service.parse_document(EXTERNAL_PVC_DOC)
    assert warnings == []


def test_parse_ceph_unknown_sub_key_warns_not_rejects():
    _doc, warnings = envconfig_service.parse_document(
        "storage:\n  ceph:\n    enabled: true\n    bogus_tune: 1\n"
    )
    assert any("bogus_tune" in w for w in warnings)


def test_parse_ceph_wrong_shapes_rejected():
    with pytest.raises(
        envconfig_service.ConfigValidationError, match="storage.ceph must be a mapping"
    ):
        envconfig_service.parse_document("storage:\n  ceph: true\n")
    with pytest.raises(
        envconfig_service.ConfigValidationError, match="must be a boolean"
    ):
        envconfig_service.parse_document("storage:\n  ceph:\n    enabled: maybe\n")
    with pytest.raises(
        envconfig_service.ConfigValidationError, match="must be a boolean"
    ):
        envconfig_service.parse_document("storage:\n  ceph:\n    external_pvc: 1\n")


# ---------------------------------------------------------------------------
# API: PUT config, GET render
# ---------------------------------------------------------------------------


def test_render_endpoint_ceph_enabled(client, admin_headers):
    env = _create_env(client, admin_headers)
    _put_doc(client, admin_headers, env["id"], CEPH_DOC)

    resp = client.get(
        f"/api/v1/environments/{env['id']}/config/render", headers=admin_headers
    )
    assert resp.status_code == 200, resp.text
    files = resp.json()["files"]
    assert ROOK_OVERLAY_PATH in files
    assert yaml.safe_load(files[ROOK_OVERLAY_PATH])["resources"] == [
        "../rook-operator/base",
        "../rook-defaults/base",
        "../rook-cluster/base",
    ]


def test_render_endpoint_ceph_disabled_no_rook_files(client, admin_headers):
    env = _create_env(client, admin_headers)
    _put_doc(client, admin_headers, env["id"], CEPH_DISABLED_DOC)

    resp = client.get(
        f"/api/v1/environments/{env['id']}/config/render", headers=admin_headers
    )
    assert resp.status_code == 200, resp.text
    assert ROOK_OVERLAY_PATH not in resp.json()["files"]


def test_render_endpoint_ceph_external_pvc(client, admin_headers):
    env = _create_env(client, admin_headers)
    _put_doc(client, admin_headers, env["id"], EXTERNAL_PVC_DOC)

    resp = client.get(
        f"/api/v1/environments/{env['id']}/config/render", headers=admin_headers
    )
    assert resp.status_code == 200, resp.text
    assert yaml.safe_load(resp.json()["files"][ROOK_OVERLAY_PATH])["resources"] == [
        "../rook-operator/base",
        "../rook-defaults-external-pvc/base",
        "../rook-cluster-external-pvc/base",
    ]


def test_put_ceph_unknown_sub_key_warns(client, admin_headers):
    env = _create_env(client, admin_headers)
    body = _put_doc(
        client,
        admin_headers,
        env["id"],
        "storage:\n  ceph:\n    enabled: true\n    future_dial: 42\n",
    )
    assert any("future_dial" in w for w in body["warnings"])


def test_push_ceph_enabled_writes_overlay_local(client, admin_headers, tmp_path):
    """config.push with ceph enabled writes the overlay file to the config dir."""
    config_dir = tmp_path / "etc-genestack"
    config_dir.mkdir()
    env = _create_env(
        client, admin_headers, genestack_config_dir=str(config_dir), dry_run=False
    )
    _put_doc(client, admin_headers, env["id"], CEPH_DOC)

    resp = client.post(
        f"/api/v1/environments/{env['id']}/jobs",
        headers=admin_headers,
        json={"operation": "genestack.config.push", "params": {}, "run_sync": True},
    )
    assert resp.status_code == 201, resp.text
    job = resp.json()
    assert job["status"] == "success", job["error"]

    overlay = config_dir / "kustomize" / "rook-ceph" / "overlay" / "kustomization.yaml"
    assert yaml.safe_load(overlay.read_text(encoding="utf-8"))["resources"] == [
        "../rook-operator/base",
        "../rook-defaults/base",
        "../rook-cluster/base",
    ]
