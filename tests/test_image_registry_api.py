"""Registry cache read API. The warm job stays registry.mirror."""

from __future__ import annotations

import uuid

from app.services import image_registry
from app.services.catalog import get_operation


def test_helm_charts_marks_oci_ahead_of_http_repos():
    rows = image_registry.helm_charts()
    names = [row["name"] for row in rows]
    assert names.index("cert-manager") < names.index("keystone")
    by_name = {row["name"]: row for row in rows}
    assert by_name["cert-manager"]["oci"] is True
    assert by_name["cert-manager"]["registry"] == "quay.io"
    assert by_name["keystone"]["oci"] is False
    assert by_name["keystone"]["url"].startswith("https://")


def test_registry_mirror_is_an_admin_job():
    op = get_operation("registry.mirror")
    assert op is not None
    assert op.required_role == "admin"
    assert op.id == "registry.mirror"


def test_registry_status_is_readable(client, admin_headers, viewer_headers, monkeypatch):
    """GET returns caches and charts. It does not ask Docker."""

    def fake(db, env, settings=None):
        return {
            "environment_id": env.id,
            "environment_name": env.name,
            "bind": "10.200.0.50",
            "running": False,
            "ready": False,
            "caches": [
                {
                    "registry": "docker.io",
                    "running": False,
                    "port": 5001,
                    "endpoint": "http://10.200.0.50:5001",
                    "images": 0,
                    "repositories": [],
                }
            ],
            "ready_count": 0,
            "cache_count": 1,
            "image_count": 0,
            "last_mirror": None,
            "charts": [
                {
                    "name": "cert-manager",
                    "oci": True,
                    "registry": "quay.io",
                    "url": "oci://quay.io/jetstack/cert-manager",
                    "repo": "charts",
                }
            ],
        }

    monkeypatch.setattr(image_registry, "for_environment", fake)
    name = f"env-reg-{uuid.uuid4().hex[:10]}"
    create = client.post(
        "/api/v1/environments",
        headers=admin_headers,
        json={"name": name},
    )
    assert create.status_code in (200, 201), create.text
    eid = create.json()["id"]

    got = client.get(f"/api/v1/environments/{eid}/registry", headers=viewer_headers)
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["environment_id"] == eid
    assert body["bind"] == "10.200.0.50"
    assert body["caches"][0]["registry"] == "docker.io"
    assert body["charts"][0]["name"] == "cert-manager"

    missing = client.get(
        "/api/v1/environments/does-not-exist/registry",
        headers=viewer_headers,
    )
    assert missing.status_code == 404
