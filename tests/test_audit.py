"""Audit listing tenant scoping.

Non-platform-admin users only see entries for environments in tenants they
belong to (mirroring /api/v1/jobs); an explicit environment_id filter 403s on
a foreign env and 404s on a missing one. Platform admins see everything.
"""

from __future__ import annotations

import uuid

import pytest

from app.db import SessionLocal
from app.models import AuditLog


def _suffix() -> str:
    return uuid.uuid4().hex[:8]


def _create_env(client, admin_headers, tenant_id=None):
    body = {"name": f"audit-env-{_suffix()}"}
    if tenant_id:
        body["tenant_id"] = tenant_id
    resp = client.post("/api/v1/environments", headers=admin_headers, json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _write_entry(env_id: str, action: str) -> None:
    db = SessionLocal()
    try:
        db.add(
            AuditLog(actor="test", action=action, environment_id=env_id, success=True)
        )
        db.commit()
    finally:
        db.close()


@pytest.fixture
def two_tenants(client, admin_headers):
    """Two tenants with an env each, plus a viewer session in the first."""
    suffix = _suffix()
    tenant_a = client.post(
        "/api/v1/tenants", headers=admin_headers, json={"name": f"ta-{suffix}"}
    ).json()
    tenant_b = client.post(
        "/api/v1/tenants", headers=admin_headers, json={"name": f"tb-{suffix}"}
    ).json()
    env_a = _create_env(client, admin_headers, tenant_id=tenant_a["id"])
    env_b = _create_env(client, admin_headers, tenant_id=tenant_b["id"])
    user = client.post(
        "/api/v1/users",
        headers=admin_headers,
        json={
            "username": f"audit-viewer-{suffix}",
            "password": "pw",
            "memberships": [{"tenant_id": tenant_a["id"], "role": "viewer"}],
        },
    ).json()
    login = client.post(
        "/api/v1/auth/login", json={"username": user["username"], "password": "pw"}
    )
    assert login.status_code == 200, login.text
    member_headers = {"Authorization": f"Bearer {login.json()['token']}"}
    return {"env_a": env_a, "env_b": env_b, "member_headers": member_headers}


def test_member_sees_only_own_tenant_entries(client, two_tenants):
    marker_a = f"test.action.{_suffix()}"
    marker_b = f"test.action.{_suffix()}"
    _write_entry(two_tenants["env_a"]["id"], marker_a)
    _write_entry(two_tenants["env_b"]["id"], marker_b)

    resp = client.get("/api/v1/audit", headers=two_tenants["member_headers"])
    assert resp.status_code == 200, resp.text
    env_ids = {entry["environment_id"] for entry in resp.json()}
    assert two_tenants["env_a"]["id"] in env_ids
    assert two_tenants["env_b"]["id"] not in env_ids
    actions = {entry["action"] for entry in resp.json()}
    assert marker_a in actions
    assert marker_b not in actions


def test_member_environment_filter_allows_own_env(client, two_tenants):
    env_a = two_tenants["env_a"]
    _write_entry(env_a["id"], f"test.action.{_suffix()}")
    resp = client.get(
        f"/api/v1/audit?environment_id={env_a['id']}",
        headers=two_tenants["member_headers"],
    )
    assert resp.status_code == 200, resp.text
    assert all(entry["environment_id"] == env_a["id"] for entry in resp.json())


def test_member_environment_filter_forbids_foreign_env(client, two_tenants):
    resp = client.get(
        f"/api/v1/audit?environment_id={two_tenants['env_b']['id']}",
        headers=two_tenants["member_headers"],
    )
    assert resp.status_code == 403


def test_member_environment_filter_missing_env_404(client, two_tenants):
    resp = client.get(
        f"/api/v1/audit?environment_id={uuid.uuid4().hex}",
        headers=two_tenants["member_headers"],
    )
    assert resp.status_code == 404


def test_platform_admin_sees_all_tenants(client, admin_headers, two_tenants):
    marker_b = f"test.action.{_suffix()}"
    _write_entry(two_tenants["env_b"]["id"], marker_b)
    resp = client.get("/api/v1/audit", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    env_ids = {entry["environment_id"] for entry in resp.json()}
    assert two_tenants["env_a"]["id"] in env_ids
    assert two_tenants["env_b"]["id"] in env_ids
    # ... and may filter on any env directly.
    resp = client.get(
        f"/api/v1/audit?environment_id={two_tenants['env_b']['id']}",
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text
    assert marker_b in {entry["action"] for entry in resp.json()}
