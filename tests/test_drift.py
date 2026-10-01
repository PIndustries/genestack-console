"""Doc-based components writes (PUT /components) and config drift detection."""

from __future__ import annotations

import hashlib
import uuid

import pytest
import yaml
from sqlalchemy import select

from app.db import SessionLocal, init_db
from app.models import ConfigDrift, Environment
from app.services import collector, envconfig, events
from app.services import genestack_bridge as bridge


@pytest.fixture(autouse=True, scope="module")
def _ensure_tables():
    """Create tables so the file also passes when run standalone."""
    init_db()


def _settings():
    from app.config import get_settings

    return get_settings()


def _suffix() -> str:
    return uuid.uuid4().hex[:8]


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


DOC = """\
provider: kubespray
components:
  keystone: true
  cinder: false
servers:
  ctrl-1:
    source: static
    ip: 10.0.0.11
    roles: [control]
"""


def _make_env(db, config_dir=None, **fields) -> Environment:
    env = Environment(name=f"drift-{_suffix()}", **fields)
    if config_dir is not None:
        env.genestack_config_dir = str(config_dir)
    db.add(env)
    db.commit()
    return env


def _env_with_doc(db, tmp_path, doc=DOC, **fields):
    """Env with a config dir and DOC stored as config version 1."""
    config_dir = tmp_path / f"etc-genestack-{_suffix()}"
    config_dir.mkdir()
    env = _make_env(db, config_dir=config_dir, **fields)
    envconfig.put_version(db, env, doc, "drift-test")
    db.commit()
    return env, config_dir


def _rendered_files(db, env) -> dict[str, str]:
    current = envconfig.get_current(db, env)
    assert current is not None
    doc, _row = current
    return envconfig.render_to_files(doc, env, _settings())


def _write_host_files(config_dir, files: dict[str, str]) -> None:
    for relpath, text in files.items():
        target = config_dir / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")


def _drift_rows(db, env_id: str) -> dict[str, ConfigDrift]:
    return {
        row.artifact: row
        for row in db.scalars(
            select(ConfigDrift).where(ConfigDrift.environment_id == env_id)
        ).all()
    }


# ---------------------------------------------------------------------------
# PUT /components -> config document
# ---------------------------------------------------------------------------


def _create_env(client, headers, **fields) -> dict:
    resp = client.post(
        "/api/v1/environments",
        headers=headers,
        json={"name": f"env-drift-{_suffix()}", **fields},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_put_components_merges_into_new_config_version(
    client, operator_headers, tmp_path
):
    config_dir = tmp_path / "etc-genestack"
    config_dir.mkdir()
    on_host = config_dir / "openstack-components.yaml"
    on_host_text = yaml.safe_dump({"components": {"keystone": True, "cinder": False}})
    on_host.write_text(on_host_text, encoding="utf-8")
    env = _create_env(client, operator_headers, genestack_config_dir=str(config_dir))

    # Seed config v1 with a components block.
    resp = client.put(
        f"/api/v1/environments/{env['id']}/config",
        headers=operator_headers,
        json={"yaml_text": "components:\n  keystone: true\n  cinder: false\n"},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["version"] == 1

    resp = client.put(
        "/api/v1/genestack/components",
        params={"environment_id": env["id"]},
        headers=operator_headers,
        json={"components": {"cinder": True}},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["version"] == 2
    assert body["note"] == "saved to config v2 — push to apply"
    assert body["updated"] == {"cinder": {"old": False, "new": True}}
    assert body["components"] == {"keystone": True, "cinder": True}

    # The new version holds the merged components block; untouched keys survive.
    current = client.get(
        f"/api/v1/environments/{env['id']}/config", headers=operator_headers
    )
    assert current.status_code == 200, current.text
    assert current.json()["version"] == 2
    doc = yaml.safe_load(current.json()["yaml"])
    assert doc["components"] == {"keystone": True, "cinder": True}

    # The on-host file is NOT written — push is an explicit operator action.
    assert on_host.read_text(encoding="utf-8") == on_host_text
    assert not (config_dir / "openstack-components.yaml.bak").exists()


def test_put_components_creates_v1_when_no_prior_doc(client, operator_headers):
    env = _create_env(client, operator_headers)
    resp = client.put(
        "/api/v1/genestack/components",
        params={"environment_id": env["id"]},
        headers=operator_headers,
        json={"components": {"keystone": True}},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["version"] == 1
    assert body["components"] == {"keystone": True}
    assert body["updated"] == {"keystone": {"old": None, "new": True}}


def test_put_components_rejects_unknown_keys(client, operator_headers):
    env = _create_env(client, operator_headers)
    resp = client.put(
        "/api/v1/genestack/components",
        params={"environment_id": env["id"]},
        headers=operator_headers,
        json={"components": {"not-a-service": True}},
    )
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert "not-a-service" in detail
    assert "keystone" in detail  # valid names from the service registry


def test_put_components_audit_records_version(client, operator_headers, admin_headers):
    env = _create_env(client, operator_headers)
    resp = client.put(
        "/api/v1/genestack/components",
        params={"environment_id": env["id"]},
        headers=operator_headers,
        json={"components": {"glance": True}},
    )
    assert resp.status_code == 200, resp.text

    audit = client.get(
        "/api/v1/audit",
        params={"action": "genestack.components.update"},
        headers=admin_headers,
    )
    assert audit.status_code == 200, audit.text
    latest = audit.json()[0]
    assert latest["action"] == "genestack.components.update"
    assert latest["environment_id"] == env["id"]
    details = latest.get("details") or {}
    assert details.get("version") == resp.json()["version"]
    assert "glance" in details.get("updated", {})


# ---------------------------------------------------------------------------
# Drift classification (service level)
# ---------------------------------------------------------------------------


def test_drift_match_then_drift_and_missing(tmp_path):
    db = SessionLocal()
    try:
        env, config_dir = _env_with_doc(db, tmp_path)
        files = _rendered_files(db, env)
        assert set(collector.DRIFT_ARTIFACTS) <= set(files)
        _write_host_files(config_dir, files)

        statuses = collector.check_config_drift(db, env, _settings())
        assert statuses == {name: "match" for name in collector.DRIFT_ARTIFACTS}

        rows = _drift_rows(db, env.id)
        assert set(rows) == set(collector.DRIFT_ARTIFACTS)
        for name, row in rows.items():
            assert row.status == "match"
            assert row.expected_sha256 == _sha256(files[name])
            assert row.actual_sha256 == row.expected_sha256
            assert row.detail is None

        # Hand-edit one artifact, remove another.
        (config_dir / "openstack-components.yaml").write_text(
            files["openstack-components.yaml"] + "# hand edit\n", encoding="utf-8"
        )
        (config_dir / "provider").unlink()

        statuses = collector.check_config_drift(db, env, _settings())
        assert statuses == {
            "openstack-components.yaml": "drift",
            "provider": "missing",
            "inventory/inventory.yaml": "match",
        }

        rows = _drift_rows(db, env.id)
        drifted = rows["openstack-components.yaml"]
        assert drifted.status == "drift"
        assert drifted.expected_sha256 == _sha256(files["openstack-components.yaml"])
        assert drifted.actual_sha256 != drifted.expected_sha256
        assert drifted.detail
        missing = rows["provider"]
        assert missing.status == "missing"
        assert missing.actual_sha256 is None
        assert missing.detail
    finally:
        db.close()


def test_drift_over_ssh_transport(tmp_path, monkeypatch):
    """Remote envs read artifacts through the ssh bridge; rc maps to status."""
    db = SessionLocal()
    try:
        env, _config_dir = _env_with_doc(
            db, tmp_path, deployer_ssh_host="deployer.example.com"
        )
        files = _rendered_files(db, env)

        def fake_run_command(cmd, **kwargs):  # noqa: ARG001
            script = cmd[-1]
            if "openstack-components.yaml" in script:
                return {
                    "returncode": 0,
                    "stdout": files["openstack-components.yaml"],
                    "stderr": "",
                }
            if "provider" in script:
                return {"returncode": 3, "stdout": "", "stderr": ""}  # absent
            return {"returncode": 1, "stdout": "", "stderr": "cat: Permission denied"}

        monkeypatch.setattr(bridge, "run_command", fake_run_command)

        statuses = collector.check_config_drift(db, env, _settings())
        assert statuses == {
            "openstack-components.yaml": "match",
            "provider": "missing",
            "inventory/inventory.yaml": "unknown",
        }

        rows = _drift_rows(db, env.id)
        unknown = rows["inventory/inventory.yaml"]
        assert unknown.status == "unknown"
        assert "Permission denied" in (unknown.detail or "")
        assert unknown.actual_sha256 is None
        assert unknown.expected_sha256 == _sha256(files["inventory/inventory.yaml"])
    finally:
        db.close()


def test_drift_upsert_in_place_per_artifact(tmp_path):
    db = SessionLocal()
    try:
        env, config_dir = _env_with_doc(db, tmp_path)
        _write_host_files(config_dir, _rendered_files(db, env))

        collector.check_config_drift(db, env, _settings())
        first = _drift_rows(db, env.id)
        collector.check_config_drift(db, env, _settings())
        second = _drift_rows(db, env.id)

        # Still exactly one row per artifact — updated, not appended.
        assert len(second) == len(collector.DRIFT_ARTIFACTS)
        assert {name: row.id for name, row in first.items()} == {
            name: row.id for name, row in second.items()
        }
        for name, row in second.items():
            assert row.checked_at >= first[name].checked_at
    finally:
        db.close()


def test_drift_event_published_only_on_aggregate_change(tmp_path, monkeypatch):
    published = []
    monkeypatch.setattr(
        events,
        "publish_sync",
        lambda topic, payload: published.append((topic, payload)),
    )

    db = SessionLocal()
    try:
        env, config_dir = _env_with_doc(db, tmp_path)
        files = _rendered_files(db, env)
        _write_host_files(config_dir, files)

        # All match from the start: never drifted -> still not drifted, no event.
        collector.check_config_drift(db, env, _settings())
        assert [p for _t, p in published if p.get("type") == "drift"] == []

        # Introduce drift -> event with drifted=True.
        (config_dir / "provider").write_text("hand-edited\n", encoding="utf-8")
        collector.check_config_drift(db, env, _settings())
        drift_events = [p for _t, p in published if p.get("type") == "drift"]
        assert len(drift_events) == 1
        event = drift_events[0]
        assert event["environment_id"] == env.id
        assert event["drifted"] is True
        assert event["artifacts"]["provider"] == "drift"
        assert event["artifacts"]["openstack-components.yaml"] == "match"

        # Still drifted on the next check -> no new event.
        collector.check_config_drift(db, env, _settings())
        assert len([p for _t, p in published if p.get("type") == "drift"]) == 1

        # Back to all-match -> event with drifted=False.
        _write_host_files(config_dir, files)
        collector.check_config_drift(db, env, _settings())
        drift_events = [p for _t, p in published if p.get("type") == "drift"]
        assert len(drift_events) == 2
        assert drift_events[-1]["drifted"] is False
        assert all(t == "fleet" for t, p in published if p.get("type") == "drift")
    finally:
        db.close()


def test_drift_skips_env_without_config_doc(tmp_path):
    db = SessionLocal()
    try:
        env = _make_env(db, config_dir=tmp_path)
        assert collector.check_config_drift(db, env, _settings()) is None
        assert _drift_rows(db, env.id) == {}
    finally:
        db.close()


def test_drift_check_never_fails_a_probe(monkeypatch):
    def _boom(*args, **kwargs):
        raise RuntimeError("drift boom")

    monkeypatch.setattr(collector, "_check_config_drift", _boom)
    monkeypatch.setattr(
        collector,
        "_run_probe",
        lambda cmd, *, env=None, timeout=0: (None, "kubectl missing"),  # noqa: ARG005
    )

    db = SessionLocal()
    try:
        env = _make_env(db)
        snap = collector.probe_environment(db, env, _settings())
        assert snap.probe_ok is False
        assert snap.health == "down"
    finally:
        db.close()


# ---------------------------------------------------------------------------
# GET /environments/{id}/drift + /fleet/live
# ---------------------------------------------------------------------------


def _seed_drift_row(env_id: str, artifact: str, status: str, detail: str | None = None):
    with SessionLocal() as db:
        db.add(
            ConfigDrift(
                environment_id=env_id,
                artifact=artifact,
                status=status,
                expected_sha256="a" * 64,
                actual_sha256=(
                    ("b" * 64)
                    if status == "drift"
                    else ("a" * 64 if status == "match" else None)
                ),
                detail=detail,
            )
        )
        db.commit()


def test_drift_endpoint_empty_when_never_checked(client, admin_headers):
    env = _create_env(client, admin_headers)
    resp = client.get(f"/api/v1/environments/{env['id']}/drift", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "environment_id": env["id"],
        "drifted": False,
        "checked_at": None,
        "artifacts": [],
    }


def test_drift_endpoint_shape(client, admin_headers):
    env = _create_env(client, admin_headers)
    _seed_drift_row(env["id"], "openstack-components.yaml", "match")
    _seed_drift_row(env["id"], "provider", "drift", detail="on-host content differs")

    resp = client.get(f"/api/v1/environments/{env['id']}/drift", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["environment_id"] == env["id"]
    assert body["drifted"] is True
    assert body["checked_at"] is not None
    artifacts = {a["artifact"]: a for a in body["artifacts"]}
    assert set(artifacts) == {"openstack-components.yaml", "provider"}
    match = artifacts["openstack-components.yaml"]
    assert match["status"] == "match"
    assert match["expected_sha256"] == "a" * 64
    assert match["actual_sha256"] == "a" * 64
    assert match["detail"] is None
    drifted = artifacts["provider"]
    assert drifted["status"] == "drift"
    assert drifted["actual_sha256"] == "b" * 64
    assert drifted["detail"] == "on-host content differs"


def test_drift_endpoint_unknown_only_is_not_drifted(client, admin_headers):
    env = _create_env(client, admin_headers)
    _seed_drift_row(env["id"], "provider", "unknown", detail="ssh unreachable")
    resp = client.get(f"/api/v1/environments/{env['id']}/drift", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["drifted"] is False


def test_drift_endpoint_tenant_scoping(client, admin_headers):
    suffix = _suffix()
    tenant_a = client.post(
        "/api/v1/tenants", headers=admin_headers, json={"name": f"drift-ta-{suffix}"}
    ).json()
    tenant_b = client.post(
        "/api/v1/tenants", headers=admin_headers, json={"name": f"drift-tb-{suffix}"}
    ).json()
    env_a = _create_env(client, admin_headers, tenant_id=tenant_a["id"])
    env_b = _create_env(client, admin_headers, tenant_id=tenant_b["id"])

    user = client.post(
        "/api/v1/users",
        headers=admin_headers,
        json={
            "username": f"drift-viewer-{suffix}",
            "password": "pw",
            "memberships": [{"tenant_id": tenant_a["id"], "role": "viewer"}],
        },
    ).json()
    login = client.post(
        "/api/v1/auth/login", json={"username": user["username"], "password": "pw"}
    )
    assert login.status_code == 200, login.text
    headers = {"Authorization": f"Bearer {login.json()['token']}"}

    # Own tenant: readable; other tenant: 403; nonexistent: 404.
    resp = client.get(f"/api/v1/environments/{env_a['id']}/drift", headers=headers)
    assert resp.status_code == 200, resp.text
    assert (
        client.get(
            f"/api/v1/environments/{env_b['id']}/drift", headers=headers
        ).status_code
        == 403
    )
    assert (
        client.get(
            "/api/v1/environments/does-not-exist/drift", headers=headers
        ).status_code
        == 404
    )


def test_fleet_live_includes_drifted_field(client, admin_headers):
    never_checked = _create_env(client, admin_headers)
    matching = _create_env(client, admin_headers)
    drifted = _create_env(client, admin_headers)
    _seed_drift_row(matching["id"], "provider", "match")
    _seed_drift_row(drifted["id"], "provider", "missing")

    resp = client.get("/api/v1/fleet/live", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    rows = {r["environment_id"]: r for r in resp.json()}
    assert rows[never_checked["id"]]["drifted"] is None
    assert rows[matching["id"]]["drifted"] is False
    assert rows[drifted["id"]]["drifted"] is True
