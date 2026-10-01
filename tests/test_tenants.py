"""Tenant CRUD, membership management, and cross-tenant route scoping."""

from __future__ import annotations

import uuid

import pytest


def _suffix() -> str:
    return uuid.uuid4().hex[:8]


def _create_tenant(client, admin_headers, name=None):
    name = name or f"tenant-{_suffix()}"
    resp = client.post("/api/v1/tenants", headers=admin_headers, json={"name": name})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _create_user(client, admin_headers, username=None, password="pw", memberships=None):
    username = username or f"user-{_suffix()}"
    resp = client.post(
        "/api/v1/users",
        headers=admin_headers,
        json={
            "username": username,
            "password": password,
            "memberships": memberships or [],
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _login_headers(client, username, password="pw"):
    resp = client.post(
        "/api/v1/auth/login", json={"username": username, "password": password}
    )
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


def _create_env(client, headers, tenant_id=None, name=None):
    name = name or f"env-{_suffix()}"
    body = {"name": name}
    if tenant_id:
        body["tenant_id"] = tenant_id
    resp = client.post("/api/v1/environments", headers=headers, json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.fixture
def two_tenants(client, admin_headers):
    """Two tenants, each with an env, plus an operator and a viewer in tenant A."""
    tenant_a = _create_tenant(client, admin_headers)
    tenant_b = _create_tenant(client, admin_headers)
    env_a = _create_env(client, admin_headers, tenant_id=tenant_a["id"])
    env_b = _create_env(client, admin_headers, tenant_id=tenant_b["id"])

    operator = _create_user(
        client,
        admin_headers,
        memberships=[{"tenant_id": tenant_a["id"], "role": "operator"}],
    )
    viewer = _create_user(
        client,
        admin_headers,
        memberships=[{"tenant_id": tenant_a["id"], "role": "viewer"}],
    )
    return {
        "tenant_a": tenant_a,
        "tenant_b": tenant_b,
        "env_a": env_a,
        "env_b": env_b,
        "operator": operator,
        "viewer": viewer,
        "operator_headers": _login_headers(client, operator["username"]),
        "viewer_headers": _login_headers(client, viewer["username"]),
    }


# ---------------------------------------------------------------------------
# Tenant CRUD permissions
# ---------------------------------------------------------------------------


def test_tenant_create_requires_platform_admin(client, two_tenants):
    resp = client.post(
        "/api/v1/tenants",
        headers=two_tenants["operator_headers"],
        json={"name": f"nope-{_suffix()}"},
    )
    assert resp.status_code == 403


def test_tenant_list_only_own_for_members(client, admin_headers, two_tenants):
    all_tenants = client.get("/api/v1/tenants", headers=admin_headers)
    assert all_tenants.status_code == 200
    all_ids = {t["id"] for t in all_tenants.json()}
    assert two_tenants["tenant_a"]["id"] in all_ids
    assert two_tenants["tenant_b"]["id"] in all_ids

    mine = client.get("/api/v1/tenants", headers=two_tenants["operator_headers"])
    assert mine.status_code == 200
    mine_ids = {t["id"] for t in mine.json()}
    assert mine_ids == {two_tenants["tenant_a"]["id"]}


def test_tenant_get_patch_permissions(client, admin_headers, two_tenants):
    tenant_a = two_tenants["tenant_a"]
    tenant_b = two_tenants["tenant_b"]

    # Member can read own tenant; cross-tenant read is forbidden
    assert (
        client.get(
            f"/api/v1/tenants/{tenant_a['id']}", headers=two_tenants["viewer_headers"]
        ).status_code
        == 200
    )
    assert (
        client.get(
            f"/api/v1/tenants/{tenant_b['id']}", headers=two_tenants["viewer_headers"]
        ).status_code
        == 403
    )

    # Viewer/operator members cannot patch; tenant admin can
    for headers in (two_tenants["viewer_headers"], two_tenants["operator_headers"]):
        resp = client.patch(
            f"/api/v1/tenants/{tenant_a['id']}",
            headers=headers,
            json={"description": "updated"},
        )
        assert resp.status_code == 403

    admin_user = _create_user(
        client,
        admin_headers,
        memberships=[{"tenant_id": tenant_a["id"], "role": "admin"}],
    )
    admin_tenant_headers = _login_headers(client, admin_user["username"])
    resp = client.patch(
        f"/api/v1/tenants/{tenant_a['id']}",
        headers=admin_tenant_headers,
        json={"description": "updated by tenant admin"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["description"] == "updated by tenant admin"


def test_tenant_delete_requires_platform_admin(client, admin_headers):
    tenant = _create_tenant(client, admin_headers)
    admin_user = _create_user(
        client,
        admin_headers,
        memberships=[{"tenant_id": tenant["id"], "role": "admin"}],
    )
    headers = _login_headers(client, admin_user["username"])
    assert (
        client.delete(f"/api/v1/tenants/{tenant['id']}", headers=headers).status_code
        == 403
    )

    resp = client.delete(f"/api/v1/tenants/{tenant['id']}", headers=admin_headers)
    assert resp.status_code == 204
    assert (
        client.get(f"/api/v1/tenants/{tenant['id']}", headers=admin_headers).status_code
        == 404
    )


# ---------------------------------------------------------------------------
# Membership management
# ---------------------------------------------------------------------------


def test_membership_management(client, admin_headers):
    tenant = _create_tenant(client, admin_headers)
    tenant_admin = _create_user(
        client,
        admin_headers,
        memberships=[{"tenant_id": tenant["id"], "role": "admin"}],
    )
    admin_tenant_headers = _login_headers(client, tenant_admin["username"])
    target = _create_user(client, admin_headers)

    # Non-member cannot list members
    outsider = _create_user(client, admin_headers)
    outsider_headers = _login_headers(client, outsider["username"])
    assert (
        client.get(
            f"/api/v1/tenants/{tenant['id']}/members", headers=outsider_headers
        ).status_code
        == 403
    )

    # Tenant admin adds a member
    resp = client.post(
        f"/api/v1/tenants/{tenant['id']}/members",
        headers=admin_tenant_headers,
        json={"username": target["username"], "role": "viewer"},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["role"] == "viewer"

    # Duplicate add conflicts
    resp = client.post(
        f"/api/v1/tenants/{tenant['id']}/members",
        headers=admin_tenant_headers,
        json={"username": target["username"], "role": "viewer"},
    )
    assert resp.status_code == 409

    members = client.get(
        f"/api/v1/tenants/{tenant['id']}/members", headers=admin_tenant_headers
    )
    assert members.status_code == 200
    usernames = {m["username"] for m in members.json()}
    assert {tenant_admin["username"], target["username"]} <= usernames

    # Tenant admin removes the member
    resp = client.delete(
        f"/api/v1/tenants/{tenant['id']}/members/{target['id']}",
        headers=admin_tenant_headers,
    )
    assert resp.status_code == 204


def test_add_member_unknown_user_404(client, admin_headers):
    tenant = _create_tenant(client, admin_headers)
    resp = client.post(
        f"/api/v1/tenants/{tenant['id']}/members",
        headers=admin_headers,
        json={"username": f"missing-{_suffix()}", "role": "viewer"},
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Environment scoping
# ---------------------------------------------------------------------------


def test_cross_tenant_env_access_forbidden(client, two_tenants):
    env_b = two_tenants["env_b"]
    headers = two_tenants["operator_headers"]

    assert (
        client.get(f"/api/v1/environments/{env_b['id']}", headers=headers).status_code
        == 403
    )
    resp = client.patch(
        f"/api/v1/environments/{env_b['id']}", headers=headers, json={"tier": "prod"}
    )
    assert resp.status_code == 403
    resp = client.post(
        f"/api/v1/environments/{env_b['id']}/jobs",
        headers=headers,
        json={"operation": "host.preflight", "params": {}, "run_sync": True},
    )
    assert resp.status_code == 403


def test_member_viewer_reads_but_cannot_write(client, two_tenants):
    env_a = two_tenants["env_a"]
    headers = two_tenants["viewer_headers"]

    resp = client.get(f"/api/v1/environments/{env_a['id']}", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["tenant_id"] == two_tenants["tenant_a"]["id"]

    resp = client.patch(
        f"/api/v1/environments/{env_a['id']}", headers=headers, json={"tier": "prod"}
    )
    assert resp.status_code == 403

    resp = client.post(
        f"/api/v1/environments/{env_a['id']}/jobs",
        headers=headers,
        json={"operation": "host.preflight", "params": {}, "run_sync": True},
    )
    assert resp.status_code == 403


def test_member_operator_crud_in_own_tenant(client, two_tenants):
    env_a = two_tenants["env_a"]
    headers = two_tenants["operator_headers"]

    resp = client.patch(
        f"/api/v1/environments/{env_a['id']}", headers=headers, json={"tier": "lab"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["tier"] == "lab"

    resp = client.post(
        "/api/v1/environments",
        headers=headers,
        json={
            "name": f"op-env-{_suffix()}",
            "tenant_id": two_tenants["tenant_a"]["id"],
        },
    )
    assert resp.status_code == 201, resp.text

    # ... but cannot create an env in the other tenant
    resp = client.post(
        "/api/v1/environments",
        headers=headers,
        json={
            "name": f"op-env-x-{_suffix()}",
            "tenant_id": two_tenants["tenant_b"]["id"],
        },
    )
    assert resp.status_code == 403


def test_env_list_filtered_by_membership(client, admin_headers, two_tenants):
    all_envs = client.get("/api/v1/environments", headers=admin_headers)
    assert all_envs.status_code == 200
    all_ids = {e["id"] for e in all_envs.json()}
    assert two_tenants["env_a"]["id"] in all_ids
    assert two_tenants["env_b"]["id"] in all_ids

    mine = client.get("/api/v1/environments", headers=two_tenants["operator_headers"])
    assert mine.status_code == 200
    mine_ids = {e["id"] for e in mine.json()}
    assert two_tenants["env_a"]["id"] in mine_ids
    assert two_tenants["env_b"]["id"] not in mine_ids


def test_platform_admin_sees_all_envs(client, admin_headers, two_tenants):
    username = f"superuser-{_suffix()}"
    resp = client.post(
        "/api/v1/users",
        headers=admin_headers,
        json={"username": username, "password": "pw", "platform_admin": True},
    )
    assert resp.status_code == 201, resp.text
    headers = _login_headers(client, username)

    envs = client.get("/api/v1/environments", headers=headers)
    assert envs.status_code == 200
    ids = {e["id"] for e in envs.json()}
    assert two_tenants["env_a"]["id"] in ids
    assert two_tenants["env_b"]["id"] in ids

    resp = client.get(
        f"/api/v1/environments/{two_tenants['env_b']['id']}", headers=headers
    )
    assert resp.status_code == 200


def test_users_endpoints_require_platform_admin(client, admin_headers, two_tenants):
    headers = two_tenants["operator_headers"]
    assert client.get("/api/v1/users", headers=headers).status_code == 403
    resp = client.post(
        "/api/v1/users",
        headers=headers,
        json={"username": f"denied-{_suffix()}", "password": "pw"},
    )
    assert resp.status_code == 403

    users = client.get("/api/v1/users", headers=admin_headers)
    assert users.status_code == 200
    assert any(
        u["username"] == two_tenants["operator"]["username"] for u in users.json()
    )


# ---------------------------------------------------------------------------
# Password reset / user deletion (account lifecycle)
# ---------------------------------------------------------------------------


def test_password_reset_by_admin_and_self(client, admin_headers):
    user = _create_user(client, admin_headers)
    username = user["username"]

    # Platform admin resets the password; old one stops working
    resp = client.post(
        f"/api/v1/users/{username}/password",
        headers=admin_headers,
        json={"password": "new-pw"},
    )
    assert resp.status_code == 204
    assert (
        client.post(
            "/api/v1/auth/login", json={"username": username, "password": "pw"}
        ).status_code
        == 401
    )
    headers = _login_headers(client, username, password="new-pw")

    # The user can change their own password
    resp = client.post(
        f"/api/v1/users/{username}/password",
        headers=headers,
        json={"password": "self-pw"},
    )
    assert resp.status_code == 204
    _login_headers(client, username, password="self-pw")


def test_password_reset_forbidden_for_other_users(client, admin_headers, two_tenants):
    other = _create_user(client, admin_headers)
    resp = client.post(
        f"/api/v1/users/{other['username']}/password",
        headers=two_tenants["operator_headers"],
        json={"password": "nope"},
    )
    assert resp.status_code == 403

    resp = client.post(
        f"/api/v1/users/missing-{_suffix()}/password",
        headers=admin_headers,
        json={"password": "x"},
    )
    assert resp.status_code == 404


def test_delete_user(client, admin_headers, two_tenants):
    tenant_a = two_tenants["tenant_a"]
    user = _create_user(
        client,
        admin_headers,
        memberships=[{"tenant_id": tenant_a["id"], "role": "viewer"}],
    )

    # Non-platform-admin session users cannot delete accounts
    resp = client.delete(
        f"/api/v1/users/{user['username']}", headers=two_tenants["operator_headers"]
    )
    assert resp.status_code == 403

    # Platform admin deletes; memberships cascade and login stops working
    resp = client.delete(f"/api/v1/users/{user['username']}", headers=admin_headers)
    assert resp.status_code == 204
    usernames = {
        u["username"] for u in client.get("/api/v1/users", headers=admin_headers).json()
    }
    assert user["username"] not in usernames
    assert (
        client.post(
            "/api/v1/auth/login", json={"username": user["username"], "password": "pw"}
        ).status_code
        == 401
    )
    members = client.get(
        f"/api/v1/tenants/{tenant_a['id']}/members", headers=admin_headers
    )
    assert user["username"] not in {m["username"] for m in members.json()}

    assert (
        client.delete(
            f"/api/v1/users/missing-{_suffix()}", headers=admin_headers
        ).status_code
        == 404
    )


def test_delete_own_account_rejected(client, admin_headers):
    username = f"self-del-{_suffix()}"
    resp = client.post(
        "/api/v1/users",
        headers=admin_headers,
        json={"username": username, "password": "pw", "platform_admin": True},
    )
    assert resp.status_code == 201, resp.text
    headers = _login_headers(client, username)
    resp = client.delete(f"/api/v1/users/{username}", headers=headers)
    assert resp.status_code == 409


# ---------------------------------------------------------------------------
# Default-tenant backfill
# ---------------------------------------------------------------------------


def test_default_tenant_backfill(client, admin_headers):
    """Environments with NULL tenant_id are moved into a `default` tenant by init_db."""
    from app.db import SessionLocal, init_db
    from app.models import Environment

    name = f"legacy-env-{_suffix()}"
    db = SessionLocal()
    try:
        env = Environment(name=name, metadata_json={})
        db.add(env)
        db.commit()
        env_id = env.id
        assert env.tenant_id is None
    finally:
        db.close()

    init_db()

    tenants = client.get("/api/v1/tenants", headers=admin_headers)
    default = [t for t in tenants.json() if t["name"] == "default"]
    assert default, "init_db should create the default tenant"

    resp = client.get(f"/api/v1/environments/{env_id}", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["tenant_id"] == default[0]["id"]
