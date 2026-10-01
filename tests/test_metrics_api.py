"""Metrics chart endpoint tests: names, series, tenant scoping, validation."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.db import SessionLocal
from app.models import MetricSample
from app.routers import metrics as metrics_router


@pytest.fixture(scope="module", autouse=True)
def _wire_router(app):
    # main.py wiring lands with integration; mount here if absent so these
    # tests run standalone.
    paths = {getattr(r, "path", None) for r in app.routes}
    if "/api/v1/environments/{environment_id}/metrics/series" not in paths:
        app.include_router(metrics_router.router)


def _suffix() -> str:
    return uuid.uuid4().hex[:8]


def _create_env(client, admin_headers, tenant_id=None):
    body = {"name": f"env-{_suffix()}"}
    if tenant_id:
        body["tenant_id"] = tenant_id
    resp = client.post("/api/v1/environments", headers=admin_headers, json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _add_sample(env_id: str, name: str, value: float, ts: datetime) -> None:
    with SessionLocal() as db:
        db.add(
            MetricSample(
                environment_id=env_id, ts=ts, name=name, labels={}, value=value
            )
        )
        db.commit()


def test_metrics_names_and_series(client, admin_headers):
    env = _create_env(client, admin_headers)
    # Same 30-minute wall-clock bucket for both cpu samples.
    base = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    _add_sample(env["id"], "node.cpu.cores", 1.0, base + timedelta(minutes=5))
    _add_sample(env["id"], "node.cpu.cores", 3.0, base + timedelta(minutes=20))
    _add_sample(env["id"], "pod.memory.bytes", 99.0, base + timedelta(minutes=5))

    names = client.get(
        f"/api/v1/environments/{env['id']}/metrics/names", headers=admin_headers
    )
    assert names.status_code == 200, names.text
    rows = {r["name"]: r for r in names.json()}
    assert set(rows) == {"node.cpu.cores", "pod.memory.bytes"}
    assert rows["node.cpu.cores"]["samples"] == 2
    assert rows["pod.memory.bytes"]["samples"] == 1
    assert rows["node.cpu.cores"]["latest_ts"]

    series = client.get(
        f"/api/v1/environments/{env['id']}/metrics/series"
        "?name=node.cpu.cores&hours=24&bucket_minutes=30",
        headers=admin_headers,
    )
    assert series.status_code == 200, series.text
    body = series.json()
    assert body["name"] == "node.cpu.cores"
    assert body["hours"] == 24
    assert body["bucket_minutes"] == 30
    assert len(body["series"]) == 1
    bucket = body["series"][0]
    assert bucket["count"] == 2
    assert bucket["avg"] == 2.0
    assert bucket["min"] == 1.0
    assert bucket["max"] == 3.0
    assert bucket["bucket_start_iso"]


def test_metrics_empty_data_returns_200(client, admin_headers):
    env = _create_env(client, admin_headers)
    names = client.get(
        f"/api/v1/environments/{env['id']}/metrics/names", headers=admin_headers
    )
    assert names.status_code == 200
    assert names.json() == []

    series = client.get(
        f"/api/v1/environments/{env['id']}/metrics/series?name=node.cpu.cores",
        headers=admin_headers,
    )
    assert series.status_code == 200
    assert series.json() == {
        "name": "node.cpu.cores",
        "hours": 24,
        "bucket_minutes": 30,
        "series": [],
    }


def test_metrics_series_param_validation(client, admin_headers):
    env = _create_env(client, admin_headers)
    base_url = f"/api/v1/environments/{env['id']}/metrics/series"
    # name is required and length-bounded.
    assert client.get(base_url, headers=admin_headers).status_code == 422
    assert client.get(f"{base_url}?name=", headers=admin_headers).status_code == 422
    assert (
        client.get(f"{base_url}?name={'x' * 129}", headers=admin_headers).status_code
        == 422
    )
    # hours 1..168, bucket_minutes 5..720.
    for bad in (
        "name=m&hours=0",
        "name=m&hours=169",
        "name=m&bucket_minutes=4",
        "name=m&bucket_minutes=721",
    ):
        assert client.get(f"{base_url}?{bad}", headers=admin_headers).status_code == 422
    # Bounds are inclusive at the edges.
    ok = client.get(
        f"{base_url}?name=m&hours=168&bucket_minutes=720", headers=admin_headers
    )
    assert ok.status_code == 200


def test_metrics_tenant_isolation(client, admin_headers):
    suffix = _suffix()
    tenant_a = client.post(
        "/api/v1/tenants", headers=admin_headers, json={"name": f"ta-{suffix}"}
    ).json()
    tenant_b = client.post(
        "/api/v1/tenants", headers=admin_headers, json={"name": f"tb-{suffix}"}
    ).json()
    env_a = _create_env(client, admin_headers, tenant_id=tenant_a["id"])
    env_b = _create_env(client, admin_headers, tenant_id=tenant_b["id"])
    _add_sample(env_a["id"], "node.cpu.cores", 1.0, datetime.now(timezone.utc))

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

    # Own tenant's env: readable, samples visible.
    names = client.get(
        f"/api/v1/environments/{env_a['id']}/metrics/names", headers=headers
    )
    assert names.status_code == 200, names.text
    assert [r["name"] for r in names.json()] == ["node.cpu.cores"]
    series = client.get(
        f"/api/v1/environments/{env_a['id']}/metrics/series?name=node.cpu.cores",
        headers=headers,
    )
    assert series.status_code == 200

    # Other tenant's env: 403 on both endpoints (same as state.py).
    assert (
        client.get(
            f"/api/v1/environments/{env_b['id']}/metrics/names", headers=headers
        ).status_code
        == 403
    )
    assert (
        client.get(
            f"/api/v1/environments/{env_b['id']}/metrics/series?name=node.cpu.cores",
            headers=headers,
        ).status_code
        == 403
    )

    # Nonexistent env: 404.
    assert (
        client.get(
            "/api/v1/environments/does-not-exist/metrics/names", headers=headers
        ).status_code
        == 404
    )

    # Platform admin sees both tenants.
    assert (
        client.get(
            f"/api/v1/environments/{env_b['id']}/metrics/names",
            headers=admin_headers,
        ).status_code
        == 200
    )
