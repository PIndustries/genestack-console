"""Git-backed Apps: CRUD, HMAC webhook, detect helpers."""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from pathlib import Path

from app.services import apps as apps_svc


def _env(client, headers, **extra) -> dict:
    body = {"name": f"appenv-{uuid.uuid4().hex[:8]}"}
    body.update(extra)
    resp = client.post("/api/v1/environments", headers=headers, json=body)
    assert resp.status_code in (200, 201), resp.text
    return resp.json()


def test_validate_repo_url():
    assert apps_svc.validate_repo_url("https://github.com/org/app").endswith("app.git")
    for bad in (
        "file:///etc/passwd",
        "https://evil.example/org/app",
        "https://github.com/../app",
        "-u",
        "git@github.com:org/app.git",
    ):
        try:
            apps_svc.validate_repo_url(bad)
            raise AssertionError(bad)
        except apps_svc.AppError:
            pass


def test_detect_kubernetes(tmp_path: Path):
    (tmp_path / "Chart.yaml").write_text("name: x\n")
    assert apps_svc.detect_kubernetes(tmp_path) == "helm"
    other = tmp_path / "k"
    other.mkdir()
    (other / "kustomization.yaml").write_text("resources: []\n")
    assert apps_svc.detect_kubernetes(other) == "kustomize"
    man = tmp_path / "m"
    man.mkdir()
    (man / "deploy").mkdir()
    (man / "deploy" / "deploy.yaml").write_text("kind: Deployment\n")
    assert apps_svc.detect_kubernetes(man) == "manifests"
    dock = tmp_path / "d"
    dock.mkdir()
    (dock / "Dockerfile").write_text("FROM scratch\n")
    assert apps_svc.detect_kubernetes(dock) == "dockerfile"


def test_detect_openstack(tmp_path: Path):
    tf = tmp_path / "tf"
    tf.mkdir()
    (tf / "main.tf").write_text('resource "null_resource" "x" {}\n')
    assert apps_svc.detect_openstack(tf) == "terraform"
    an = tmp_path / "an"
    an.mkdir()
    (an / "site.yml").write_text("- hosts: all\n")
    assert apps_svc.detect_openstack(an) == "ansible"


def test_hmac_roundtrip():
    secret = "s3cret"
    body = b'{"ref":"refs/heads/main"}'
    digest = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert apps_svc.verify_github_signature(secret, body, digest)
    assert not apps_svc.verify_github_signature(secret, body, "sha256=deadbeef")
    assert not apps_svc.verify_github_signature(secret, body, None)


def test_create_list_viewer_forbidden(client, admin_headers, viewer_headers):
    env = _env(client, admin_headers)
    created = client.post(
        f"/api/v1/environments/{env['id']}/apps",
        headers=admin_headers,
        json={
            "name": "demo",
            "repo_url": "https://github.com/example/demo",
            "target": "kubernetes",
        },
    )
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["webhook_secret"]
    assert body["webhook_url"].endswith(body["app"]["webhook_id"])
    assert body["app"]["has_deploy_token"] is False
    listed = client.get(f"/api/v1/environments/{env['id']}/apps", headers=admin_headers)
    assert listed.status_code == 200
    assert len(listed.json()["apps"]) == 1
    denied = client.post(
        f"/api/v1/environments/{env['id']}/apps",
        headers=viewer_headers,
        json={
            "name": "nope",
            "repo_url": "https://github.com/example/nope",
            "target": "kubernetes",
        },
    )
    assert denied.status_code in (401, 403)


def test_create_rejects_bad_repo(client, admin_headers):
    env = _env(client, admin_headers)
    resp = client.post(
        f"/api/v1/environments/{env['id']}/apps",
        headers=admin_headers,
        json={
            "name": "x",
            "repo_url": "https://evil.example/a/b",
            "target": "kubernetes",
        },
    )
    assert resp.status_code == 400


def test_webhook_hmac_and_ping(client, admin_headers):
    env = _env(client, admin_headers)
    created = client.post(
        f"/api/v1/environments/{env['id']}/apps",
        headers=admin_headers,
        json={
            "name": "hooked",
            "repo_url": "https://github.com/example/hooked",
            "target": "kubernetes",
            "branch": "main",
        },
    ).json()
    webhook_id = created["app"]["webhook_id"]
    secret = created["webhook_secret"]
    ping = json.dumps({"zen": "ok"}).encode()
    sig = "sha256=" + hmac.new(secret.encode(), ping, hashlib.sha256).hexdigest()
    resp = client.post(
        f"/api/v1/hooks/apps/{webhook_id}",
        content=ping,
        headers={"X-Hub-Signature-256": sig, "X-GitHub-Event": "ping"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["event"] == "ping"
    bad = client.post(
        f"/api/v1/hooks/apps/{webhook_id}",
        content=ping,
        headers={"X-Hub-Signature-256": "sha256=00", "X-GitHub-Event": "ping"},
    )
    assert bad.status_code == 401


def test_webhook_other_branch_ignored(client, admin_headers):
    env = _env(client, admin_headers)
    created = client.post(
        f"/api/v1/environments/{env['id']}/apps",
        headers=admin_headers,
        json={
            "name": "br",
            "repo_url": "https://github.com/example/br",
            "target": "openstack",
            "branch": "main",
        },
    ).json()
    secret = created["webhook_secret"]
    payload = json.dumps({"ref": "refs/heads/dev"}).encode()
    sig = "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    resp = client.post(
        f"/api/v1/hooks/apps/{created['app']['webhook_id']}",
        content=payload,
        headers={"X-Hub-Signature-256": sig, "X-GitHub-Event": "push"},
    )
    assert resp.status_code == 200
    assert resp.json()["ignored"] == "other-branch"


def test_deploy_endpoint_queues_job(client, admin_headers, monkeypatch):
    env = _env(client, admin_headers)
    created = client.post(
        f"/api/v1/environments/{env['id']}/apps",
        headers=admin_headers,
        json={
            "name": "ship",
            "repo_url": "https://github.com/example/ship",
            "target": "kubernetes",
        },
    ).json()
    resp = client.post(
        f"/api/v1/environments/{env['id']}/apps/{created['app']['id']}/deploy",
        headers=admin_headers,
        json={"force": True},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["job_id"]
