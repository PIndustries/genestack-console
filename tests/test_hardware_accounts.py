"""Terraform provider accounts on Hardware."""

from __future__ import annotations


def test_providers_catalog(client, viewer_headers):
    resp = client.get("/api/v1/hardware/providers", headers=viewer_headers)
    assert resp.status_code == 200
    ids = [p["id"] for p in resp.json()["providers"]]
    assert ids[:4] == ["rackspace", "aws", "azure", "gcp"]
    assert "ovh" in ids
    assert "pxe" in ids
    assert "bmc" in ids


def test_account_crud_hides_secrets(client, admin_headers):
    created = client.post(
        "/api/v1/hardware/accounts",
        headers=admin_headers,
        json={
            "kind": "aws",
            "name": "core",
            "region": "us-east-1",
            "credentials": {"access_key": "AKIATEST", "secret_key": "super-secret"},
        },
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["kind"] == "aws"
    assert body["name"] == "core"
    assert body["has_credentials"] is True
    assert "credentials" not in body
    assert "secret_key" not in str(body)

    listed = client.get("/api/v1/hardware/accounts", headers=admin_headers)
    assert listed.status_code == 200
    names = [a["name"] for a in listed.json()["accounts"] if a["kind"] == "aws"]
    assert "core" in names

    deleted = client.delete(
        f"/api/v1/hardware/accounts/{body['id']}", headers=admin_headers
    )
    assert deleted.status_code == 204


def test_account_create_requires_admin(client, operator_headers):
    resp = client.post(
        "/api/v1/hardware/accounts",
        headers=operator_headers,
        json={"kind": "gcp", "name": "x", "credentials": {"project_id": "p"}},
    )
    assert resp.status_code == 403


def test_hardware_account_tenant_scoping(client):
    """Hardware accounts are scoped by tenant; users only see their tenant's accounts."""
    from datetime import datetime, timedelta, timezone

    from app.db import SessionLocal
    from app.models import (
        HardwareAccount,
        Membership,
        SessionToken,
        Tenant,
        User,
        UserRole,
    )
    from app.services.crypto import encrypt_secret

    db = SessionLocal()
    try:
        tenant_a = Tenant(name="tenant-hw-a", description="Tenant A")
        tenant_b = Tenant(name="tenant-hw-b", description="Tenant B")
        db.add_all([tenant_a, tenant_b])
        db.flush()

        user_a = User(username="user-hw-a", role=UserRole.admin, active=True)
        user_b = User(username="user-hw-b", role=UserRole.admin, active=True)
        db.add_all([user_a, user_b])
        db.flush()

        db.add_all(
            [
                Membership(
                    user_id=user_a.id, tenant_id=tenant_a.id, role=UserRole.admin
                ),
                Membership(
                    user_id=user_b.id, tenant_id=tenant_b.id, role=UserRole.admin
                ),
            ]
        )
        db.flush()

        token_a = SessionToken(
            token="hw_token_a",
            user_id=user_a.id,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        token_b = SessionToken(
            token="hw_token_b",
            user_id=user_b.id,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        db.add_all([token_a, token_b])
        db.flush()

        creds = encrypt_secret('{"access_key": "test", "secret_key": "test"}')
        account_a = HardwareAccount(
            kind="aws",
            name="tenant-a-account",
            region="us-east-1",
            credentials_encrypted=creds,
            tenant_id=tenant_a.id,
        )
        account_b = HardwareAccount(
            kind="aws",
            name="tenant-b-account",
            region="us-west-2",
            credentials_encrypted=creds,
            tenant_id=tenant_b.id,
        )
        db.add_all([account_a, account_b])
        db.commit()
        account_a_id = account_a.id
        account_b_id = account_b.id
    finally:
        db.close()

    resp = client.get(
        "/api/v1/hardware/accounts", headers={"Authorization": "Bearer hw_token_a"}
    )
    assert resp.status_code == 200
    accounts = resp.json()["accounts"]
    assert len(accounts) == 1
    assert accounts[0]["id"] == account_a_id
    assert accounts[0]["name"] == "tenant-a-account"

    resp = client.get(
        "/api/v1/hardware/accounts", headers={"Authorization": "Bearer hw_token_b"}
    )
    assert resp.status_code == 200
    accounts = resp.json()["accounts"]
    assert len(accounts) == 1
    assert accounts[0]["id"] == account_b_id
    assert accounts[0]["name"] == "tenant-b-account"

    resp = client.patch(
        f"/api/v1/hardware/accounts/{account_b_id}",
        json={"region": "eu-west-1"},
        headers={"Authorization": "Bearer hw_token_a"},
    )
    assert resp.status_code == 403

    resp = client.delete(
        f"/api/v1/hardware/accounts/{account_b_id}",
        headers={"Authorization": "Bearer hw_token_a"},
    )
    assert resp.status_code == 403


def test_hardware_account_requires_tenant_for_non_platform_admin(client):
    """Non-platform-admin users must specify tenant_id when creating hardware accounts."""
    from datetime import datetime, timedelta, timezone

    from app.db import SessionLocal
    from app.models import Membership, SessionToken, Tenant, User, UserRole

    db = SessionLocal()
    try:
        tenant = Tenant(name="tenant-hw-req", description="Test Tenant")
        db.add(tenant)
        db.flush()

        user = User(username="user-hw-req", role=UserRole.admin, active=True)
        db.add(user)
        db.flush()

        db.add(Membership(user_id=user.id, tenant_id=tenant.id, role=UserRole.admin))
        db.flush()

        token = SessionToken(
            token="hw_req_token",
            user_id=user.id,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        db.add(token)
        db.commit()
        tenant_id = tenant.id
    finally:
        db.close()

    data = {
        "kind": "aws",
        "name": "test-no-tenant",
        "region": "us-east-1",
        "credentials": {"access_key": "test", "secret_key": "test"},
    }
    resp = client.post(
        "/api/v1/hardware/accounts",
        json=data,
        headers={"Authorization": "Bearer hw_req_token"},
    )
    assert resp.status_code == 400
    assert "tenant_id is required" in resp.json()["detail"]

    data["tenant_id"] = tenant_id
    resp = client.post(
        "/api/v1/hardware/accounts",
        json=data,
        headers={"Authorization": "Bearer hw_req_token"},
    )
    assert resp.status_code == 201
    assert resp.json()["tenant_id"] == tenant_id


def test_hardware_account_unique_per_tenant(client):
    """Hardware account (kind, name) must be unique per tenant."""
    from datetime import datetime, timedelta, timezone

    from app.db import SessionLocal
    from app.models import Membership, SessionToken, Tenant, User, UserRole

    db = SessionLocal()
    try:
        tenant_a = Tenant(name="tenant-unique-a", description="Tenant A")
        tenant_b = Tenant(name="tenant-unique-b", description="Tenant B")
        db.add_all([tenant_a, tenant_b])
        db.flush()

        user = User(username="user-unique", role=UserRole.admin, active=True)
        db.add(user)
        db.flush()

        db.add_all(
            [
                Membership(user_id=user.id, tenant_id=tenant_a.id, role=UserRole.admin),
                Membership(user_id=user.id, tenant_id=tenant_b.id, role=UserRole.admin),
            ]
        )
        db.flush()

        token = SessionToken(
            token="hw_unique_token",
            user_id=user.id,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        db.add(token)
        db.commit()
        tenant_a_id = tenant_a.id
        tenant_b_id = tenant_b.id
    finally:
        db.close()

    data = {
        "kind": "aws",
        "name": "duplicate-name",
        "region": "us-east-1",
        "credentials": {"access_key": "test", "secret_key": "test"},
        "tenant_id": tenant_a_id,
    }
    resp = client.post(
        "/api/v1/hardware/accounts",
        json=data,
        headers={"Authorization": "Bearer hw_unique_token"},
    )
    assert resp.status_code == 201

    resp = client.post(
        "/api/v1/hardware/accounts",
        json=data,
        headers={"Authorization": "Bearer hw_unique_token"},
    )
    assert resp.status_code == 409

    data["tenant_id"] = tenant_b_id
    resp = client.post(
        "/api/v1/hardware/accounts",
        json=data,
        headers={"Authorization": "Bearer hw_unique_token"},
    )
    assert resp.status_code == 201
