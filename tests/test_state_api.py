"""State API tests: latest snapshot, history, /fleet/live, tenant scoping."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.db import SessionLocal
from app.models import ClusterSnapshot
from app.routers import state


@pytest.fixture(scope="module", autouse=True)
def _wire_router(app):
    # main.py wiring lands with integration; mount here if absent so these
    # tests run standalone.
    paths = {getattr(r, "path", None) for r in app.routes}
    if "/api/v1/fleet/live" not in paths:
        app.include_router(state.router)


def _suffix() -> str:
    return uuid.uuid4().hex[:8]


def _create_env(client, admin_headers, name=None, tenant_id=None):
    body = {"name": name or f"env-{_suffix()}"}
    if tenant_id:
        body["tenant_id"] = tenant_id
    resp = client.post("/api/v1/environments", headers=admin_headers, json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _add_snapshot(
    env_id: str,
    *,
    health: str = "healthy",
    taken_at: datetime | None = None,
    probe_ok: bool = True,
    error: str | None = None,
    summary: dict | None = None,
) -> int:
    with SessionLocal() as db:
        snap = ClusterSnapshot(
            environment_id=env_id,
            taken_at=taken_at or datetime.now(timezone.utc),
            probe_ok=probe_ok,
            error=error,
            nodes=[],
            pods=[],
            helm=[],
            summary=summary or {"nodes_ready": 1, "nodes_total": 1},
            health=health,
        )
        db.add(snap)
        db.commit()
        return snap.id


def test_state_404_before_any_snapshot(client, admin_headers):
    env = _create_env(client, admin_headers)
    resp = client.get(f"/api/v1/environments/{env['id']}/state", headers=admin_headers)
    assert resp.status_code == 404


def test_state_returns_latest_snapshot(client, admin_headers):
    env = _create_env(client, admin_headers)
    now = datetime.now(timezone.utc)
    _add_snapshot(env["id"], health="degraded", taken_at=now - timedelta(minutes=5))
    _add_snapshot(env["id"], health="healthy", taken_at=now)

    resp = client.get(f"/api/v1/environments/{env['id']}/state", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["environment_id"] == env["id"]
    assert body["health"] == "healthy"
    assert body["probe_ok"] is True
    assert body["error"] is None
    assert body["summary"]["nodes_ready"] == 1


def test_state_history_ordering_limit_and_hours(client, admin_headers):
    env = _create_env(client, admin_headers)
    now = datetime.now(timezone.utc)
    for minutes_ago in (30, 20, 10):
        _add_snapshot(env["id"], taken_at=now - timedelta(minutes=minutes_ago))
    # Outside the default 24h window — must not appear.
    _add_snapshot(env["id"], taken_at=now - timedelta(hours=48))

    resp = client.get(
        f"/api/v1/environments/{env['id']}/state/history", headers=admin_headers
    )
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    assert len(rows) == 3
    taken = [r["taken_at"] for r in rows]
    assert taken == sorted(taken, reverse=True)

    limited = client.get(
        f"/api/v1/environments/{env['id']}/state/history?limit=2",
        headers=admin_headers,
    )
    assert limited.status_code == 200
    assert [r["id"] for r in limited.json()] == [r["id"] for r in rows[:2]]

    wide = client.get(
        f"/api/v1/environments/{env['id']}/state/history?hours=72",
        headers=admin_headers,
    )
    assert wide.status_code == 200
    assert len(wide.json()) == 4

    # Limit is capped at 500, not silently accepted above that.
    over = client.get(
        f"/api/v1/environments/{env['id']}/state/history?limit=501",
        headers=admin_headers,
    )
    assert over.status_code == 422


def test_fleet_live_mixed_envs(client, admin_headers):
    env_with = _create_env(client, admin_headers, name=f"live-with-{_suffix()}")
    env_without = _create_env(client, admin_headers, name=f"live-without-{_suffix()}")
    _add_snapshot(
        env_with["id"],
        health="degraded",
        probe_ok=False,
        error="api server unreachable",
    )

    resp = client.get("/api/v1/fleet/live", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    rows = {r["environment_id"]: r for r in resp.json()}
    assert env_with["id"] in rows
    assert env_without["id"] in rows

    with_snap = rows[env_with["id"]]
    assert with_snap["name"] == env_with["name"]
    assert with_snap["health"] == "degraded"
    assert with_snap["probe_ok"] is False
    assert with_snap["error"] == "api server unreachable"
    assert with_snap["taken_at"] is not None
    assert with_snap["summary"]["nodes_ready"] == 1

    no_snap = rows[env_without["id"]]
    assert no_snap["health"] == "unknown"
    assert no_snap["probe_ok"] is None
    assert no_snap["error"] is None
    assert no_snap["taken_at"] is None
    assert no_snap["summary"] is None


def test_state_tenant_isolation(client, admin_headers):
    suffix = _suffix()
    tenant_a = client.post(
        "/api/v1/tenants", headers=admin_headers, json={"name": f"ta-{suffix}"}
    ).json()
    tenant_b = client.post(
        "/api/v1/tenants", headers=admin_headers, json={"name": f"tb-{suffix}"}
    ).json()
    env_a = _create_env(client, admin_headers, tenant_id=tenant_a["id"])
    env_b = _create_env(client, admin_headers, tenant_id=tenant_b["id"])
    _add_snapshot(env_a["id"])
    _add_snapshot(env_b["id"])

    user = client.post(
        "/api/v1/users",
        headers=admin_headers,
        json={
            "username": f"viewer-{suffix}",
            "password": "pw",
            "memberships": [{"tenant_id": tenant_a["id"], "role": "viewer"}],
        },
    ).json()
    login = client.post(
        "/api/v1/auth/login", json={"username": user["username"], "password": "pw"}
    )
    assert login.status_code == 200, login.text
    headers = {"Authorization": f"Bearer {login.json()['token']}"}

    # Own tenant's env: readable.
    resp = client.get(f"/api/v1/environments/{env_a['id']}/state", headers=headers)
    assert resp.status_code == 200, resp.text

    # Other tenant's env: same behavior as the environments router (403).
    assert (
        client.get(
            f"/api/v1/environments/{env_b['id']}/state", headers=headers
        ).status_code
        == 403
    )
    assert (
        client.get(
            f"/api/v1/environments/{env_b['id']}/state/history", headers=headers
        ).status_code
        == 403
    )

    # Nonexistent env: 404.
    assert (
        client.get(
            "/api/v1/environments/does-not-exist/state", headers=headers
        ).status_code
        == 404
    )

    # /fleet/live only shows tenant A's environments.
    live = client.get("/api/v1/fleet/live", headers=headers)
    assert live.status_code == 200, live.text
    visible = {r["environment_id"] for r in live.json()}
    assert env_a["id"] in visible
    assert env_b["id"] not in visible
