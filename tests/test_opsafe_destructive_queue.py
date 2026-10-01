"""Destructive routes enqueue audited jobs with locks and a dry-run."""

from __future__ import annotations

import uuid

from app.services.catalog import get_operation, mutating_operation_ids
from app.services import platform


OPS = (
    "platform.talos.reboot",
    "platform.talos.shutdown",
    "platform.talos.reset",
    "platform.talos.upgrade",
    "platform.talos.upgrade_many",
    "platform.talos.apply_config",
    "k8s.node.drain",
    "k8s.apply",
)


def _env(client, headers, prefix: str, **extra) -> dict:
    body = {"name": f"{prefix}-{uuid.uuid4().hex[:8]}", **extra}
    resp = client.post("/api/v1/environments", headers=headers, json=body)
    assert resp.status_code in (200, 201), resp.text
    return resp.json()


def test_catalog_registers_destructive_ops_as_mutating():
    mutating = mutating_operation_ids()
    for op_id in OPS:
        op = get_operation(op_id)
        assert op is not None, op_id
        assert op.mutating is True
        assert op_id in mutating
        assert op.handler


def test_reboot_enqueues_and_conflicts(client, admin_headers):
    env = _env(client, admin_headers, "opsafe-reboot")
    url = f"/api/v1/environments/{env['id']}/platform/nodes/ns1/reboot"
    first = client.post(url, headers=admin_headers)
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["ok"] is True
    assert body["operation"] == "platform.talos.reboot"
    assert body["status"] == "queued"
    job_id = body["job_id"]

    second = client.post(url, headers=admin_headers)
    assert second.status_code == 409, second.text
    detail = second.json()["detail"]
    assert detail["conflicting_job_id"] == job_id


def test_reset_run_sync_dry_run_no_talosctl(client, admin_headers, monkeypatch):
    env = _env(client, admin_headers, "opsafe-reset", dry_run=True)
    captured: dict = {"calls": []}
    monkeypatch.setattr(
        platform, "_node_public_ip", lambda *a, **k: ("/tmp/talosconfig", "10.10.0.11")
    )
    monkeypatch.setattr(platform.shutil, "which", lambda _n: "/usr/bin/talosctl")

    def boom(*a, **k):
        captured["calls"].append(a)
        raise AssertionError("talosctl must not run in dry-run")

    monkeypatch.setattr(platform.subprocess, "run", boom)
    resp = client.post(
        f"/api/v1/environments/{env['id']}/platform/nodes/ns1/reset",
        headers=admin_headers,
        json={"graceful": True, "reboot": False, "wipe": True, "run_sync": True},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["status"] == "success"
    assert body["dry_run"] is True
    assert body["operation"] == "platform.talos.reset"
    assert captured["calls"] == []


def test_drain_enqueues(client, admin_headers):
    env = _env(client, admin_headers, "opsafe-drain")
    resp = client.post(
        f"/api/v1/environments/{env['id']}/k8s/nodes/node-1/drain",
        headers=admin_headers,
        json={},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["operation"] == "k8s.node.drain"
    assert body["status"] == "queued"
    assert body["job_id"]


def test_k8s_apply_enqueues(client, admin_headers):
    env = _env(client, admin_headers, "opsafe-apply")
    resp = client.post(
        f"/api/v1/environments/{env['id']}/k8s/apply",
        headers=admin_headers,
        json={"yaml": "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: x\n"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["operation"] == "k8s.apply"
    assert body["status"] == "queued"


def test_upgrade_many_parallel_dry_run(monkeypatch):
    """Service-level bulk upgrade honors dry_run without subprocess."""
    calls = []

    def fake_upgrade(env, name, settings=None, db=None, image=None, *, dry_run=False):
        calls.append((name, dry_run, image))
        return {
            "ok": True,
            "dry_run": dry_run,
            "node": name,
            "message": f"[dry-run] would upgrade {name}",
        }

    monkeypatch.setattr(platform, "talos_upgrade", fake_upgrade)
    env = type("E", (), {"id": "e1"})()
    result = platform.talos_upgrade_many(
        env,
        settings=None,
        db=None,
        image="factory.talos.dev/installer/abc12345:v1.13.9",
        mode="parallel",
        names=["n1", "n2"],
        dry_run=True,
    )
    assert result["ok"] is True
    assert result["dry_run"] is True
    assert result["mode"] == "parallel"
    assert len(calls) == 2
    assert all(c[1] is True for c in calls)


def test_reboot_dry_run_unit(monkeypatch):
    monkeypatch.setattr(
        platform, "_node_public_ip", lambda *a, **k: ("/tmp/tc", "10.0.0.1")
    )
    monkeypatch.setattr(platform.shutil, "which", lambda _n: "/usr/bin/talosctl")

    def boom(*a, **k):
        raise AssertionError("no subprocess in dry-run")

    monkeypatch.setattr(platform.subprocess, "run", boom)
    result = platform.talos_reboot(object(), "ns1", settings=object(), db=None, dry_run=True)
    assert result["ok"] is True
    assert result["dry_run"] is True
    assert "dry-run" in result["message"]
