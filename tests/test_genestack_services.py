"""Tests for the genestack service registry, cluster probes, pipeline, and components writes."""

from __future__ import annotations

import shutil

import pytest
import yaml

from app.services import cluster as cluster_probe
from app.services.service_registry import (
    build_service_registry,
    discover_deployable_services,
)
from tests.fake_genestack import write_fake_genestack_root


@pytest.fixture
def fresh_root(tmp_path):
    return write_fake_genestack_root(tmp_path / "genestack")


def _by_name(registry: dict) -> dict:
    return {s["name"]: s for s in registry["services"]}


# ---------------------------------------------------------------- registry


def test_registry_header_parsing_and_joins(fresh_root):
    registry = build_service_registry(fresh_root)
    assert registry["genestack_root"] == str(fresh_root.resolve())
    services = _by_name(registry)

    keystone = services["keystone"]
    assert keystone["script"] == "bin/install-keystone.sh"
    assert keystone["namespace"] == "openstack"
    assert keystone["helm_repo"] == "openstack-helm"
    assert keystone["helm_repo_url"] == "https://charts.example.com/openstack-helm"
    assert keystone["chart_version"] == "2026.1.8+db238e7c3"
    assert keystone["desired"] is True
    assert keystone["has_helm_configs"] is True
    assert keystone["has_kustomize"] is True
    assert keystone["category"] == "core"


def test_registry_strips_inline_comment(fresh_root):
    services = _by_name(build_service_registry(fresh_root))
    metallb = services["metallb"]
    assert metallb["namespace"] == "kube-system"
    assert metallb["category"] == "infrastructure"


def test_registry_missing_fields_become_null(fresh_root):
    services = _by_name(build_service_registry(fresh_root))
    placement = services["placement"]
    # absent from openstack-components.yaml and helm-chart-versions.yaml
    assert placement["desired"] is None
    assert placement["chart_version"] is None
    assert placement["has_kustomize"] is False

    cinder = services["cinder"]
    assert cinder["desired"] is False
    assert cinder["has_helm_configs"] is True
    assert cinder["has_kustomize"] is False


def test_registry_categories(fresh_root):
    services = _by_name(build_service_registry(fresh_root))
    assert services["cinder"]["category"] == "core"
    assert services["grafana"]["category"] == "monitoring"
    assert services["mariadb-operator"]["category"] == "infrastructure"
    assert services["tempest"]["category"] == "testing"
    assert services["service-template"]["category"] == "other"


def test_discover_deployable_services(fresh_root, tmp_path):
    deployable = discover_deployable_services(fresh_root)
    assert "cinder" in deployable
    assert "keystone" in deployable
    assert "service-template" not in deployable
    # Missing root -> empty set, no exception
    assert discover_deployable_services(tmp_path / "nope") == frozenset()


def test_registry_missing_root_never_raises(tmp_path):
    registry = build_service_registry(tmp_path / "nope")
    assert registry["services"] == []


# ------------------------------------------------------------- GET /services


def test_get_services_shape(client, viewer_headers):
    resp = client.get("/api/v1/genestack/services", headers=viewer_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "genestack_root" in body
    assert isinstance(body["services"], list) and body["services"]
    entry = {s["name"]: s for s in body["services"]}["keystone"]
    for key in (
        "name",
        "script",
        "namespace",
        "helm_repo",
        "helm_repo_url",
        "chart_version",
        "desired",
        "has_helm_configs",
        "has_kustomize",
        "category",
    ):
        assert key in entry, entry
    assert entry["desired"] is True


def test_get_services_requires_auth(client):
    resp = client.get("/api/v1/genestack/services")
    assert resp.status_code == 401


def test_get_services_unknown_environment_404(client, viewer_headers):
    resp = client.get(
        "/api/v1/genestack/services",
        params={"environment_id": "does-not-exist"},
        headers=viewer_headers,
    )
    assert resp.status_code == 404


def test_get_pipeline_shape(client, viewer_headers):
    resp = client.get("/api/v1/genestack/pipeline", headers=viewer_headers)
    assert resp.status_code == 200, resp.text
    stages = resp.json()["stages"]
    ids = [s["id"] for s in stages]
    # Product pipeline now includes 'cni' stage
    assert ids == [
        "hosts",
        "infrastructure",
        "operators",
        "cni",
        "core",
        "compute-network",
        "platform-extras",
        "observability",
        "testing",
    ]
    core = next(s for s in stages if s["id"] == "core")
    assert [i["name"] for i in core["items"]] == ["keystone", "placement", "glance"]
    assert core["items"][0]["script"] == "bin/install-keystone.sh"
    hosts = next(s for s in stages if s["id"] == "hosts")
    assert hosts["items"] == [
        {"type": "script", "name": "setup-hosts.sh", "script": "bin/setup-hosts.sh"}
    ]


# ------------------------------------------------------------- PUT /components
#
# PUT /components now writes the env's config DOCUMENT (new version), never
# the on-host file. The full doc-merge/version/no-file-write behavior is
# covered in tests/test_drift.py; these cover auth/validation specifics.


def _create_env(client, headers, **fields):
    import uuid

    fields.setdefault("name", f"env-cmp-{uuid.uuid4().hex[:10]}")
    resp = client.post("/api/v1/environments", headers=headers, json=fields)
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["id"]


def test_put_components_requires_environment_id(client, operator_headers):
    """The config doc is per-environment — a global write no longer exists."""
    resp = client.put(
        "/api/v1/genestack/components",
        headers=operator_headers,
        json={"components": {"cinder": True}},
    )
    assert resp.status_code == 400, resp.text


def test_put_components_unknown_environment_404(client, operator_headers):
    resp = client.put(
        "/api/v1/genestack/components",
        params={"environment_id": "does-not-exist"},
        headers=operator_headers,
        json={"components": {"cinder": True}},
    )
    assert resp.status_code == 404


def test_put_components_rejects_unknown(client, operator_headers):
    eid = _create_env(client, operator_headers)
    resp = client.put(
        "/api/v1/genestack/components",
        params={"environment_id": eid},
        headers=operator_headers,
        json={"components": {"not-a-service": True}},
    )
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert "not-a-service" in detail
    assert "keystone" in detail  # lists valid component names


def test_put_components_viewer_forbidden(client, operator_headers, viewer_headers):
    eid = _create_env(client, operator_headers)
    resp = client.put(
        "/api/v1/genestack/components",
        params={"environment_id": eid},
        headers=viewer_headers,
        json={"components": {"cinder": True}},
    )
    assert resp.status_code == 403


def test_put_components_writes_audit(client, operator_headers, admin_headers):
    eid = _create_env(client, operator_headers)
    resp = client.put(
        "/api/v1/genestack/components",
        params={"environment_id": eid},
        headers=operator_headers,
        json={"components": {"cinder": False}},
    )
    assert resp.status_code == 200, resp.text

    audit = client.get(
        "/api/v1/audit",
        params={"action": "genestack.components.update"},
        headers=admin_headers,
    )
    assert audit.status_code == 200, audit.text
    rows = audit.json()
    assert rows, "expected an audit row for genestack.components.update"
    latest = rows[0]
    assert latest["action"] == "genestack.components.update"
    details = latest.get("details") or {}
    assert "cinder" in details.get("updated", {})
    assert details.get("version") == resp.json()["version"]


# -------------------------------------------------- genestack.service.enable


def _run_job(client, headers, operation, params=None):
    resp = client.post(
        "/api/v1/jobs",
        headers=headers,
        json={"operation": operation, "params": params or {}, "run_sync": True},
    )
    assert resp.status_code in (200, 201), resp.text
    return resp.json()


def test_enable_job_accepts_discovered_service(client, admin_headers):
    job = _run_job(
        client, admin_headers, "genestack.service.enable", {"service": "cinder"}
    )
    assert job["status"] == "success", job
    assert "install-cinder.sh" in (job.get("log_text") or "")


def test_enable_job_rejects_bad_service(client, admin_headers):
    for bad in ("evil;rm", "nonexistent-svc"):
        job = _run_job(
            client, admin_headers, "genestack.service.enable", {"service": bad}
        )
        assert job["status"] == "failed", job
        assert job.get("error")


def test_services_list_job(client, admin_headers):
    job = _run_job(client, admin_headers, "genestack.services.list")
    assert job["status"] == "success", job
    log = job.get("log_text") or ""
    assert "categories=" in log
    assert "core" in log


# --------------------------------------------------- genestack.pipeline.run


def test_pipeline_run_dry_run_logs_items_in_order(client, admin_headers):
    job = _run_job(client, admin_headers, "genestack.pipeline.run", {"stage": "core"})
    assert job["status"] == "success", job
    log = job.get("log_text") or ""
    i_keystone = log.find("$ bash bin/install-keystone.sh")
    i_placement = log.find("$ bash bin/install-placement.sh")
    i_glance = log.find("$ bash bin/install-glance.sh")
    assert -1 < i_keystone < i_placement < i_glance, log


def test_pipeline_run_script_stage(client, admin_headers):
    job = _run_job(client, admin_headers, "genestack.pipeline.run", {"stage": "hosts"})
    assert job["status"] == "success", job
    assert "$ bash bin/setup-hosts.sh" in (job.get("log_text") or "")


def test_pipeline_run_invalid_stage_fails(client, admin_headers):
    job = _run_job(client, admin_headers, "genestack.pipeline.run", {"stage": "bogus"})
    assert job["status"] == "failed", job
    assert "core" in (job.get("error") or "")


# ------------------------------------------------------------- cluster probes


def test_cluster_status_no_kubeconfig(monkeypatch):
    # No kubeconfig: the probe is skipped (kubectl would default to
    # http://127.0.0.1:8080 — the console itself) and reports it.
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/kubectl")
    result = cluster_probe.cluster_status(None)
    assert result["reachable"] is False
    assert result["nodes"] == []
    assert result["kubeconfig"] == "default"
    assert "kubeconfig" in (result["error"] or "")

    result = cluster_probe.services_status(None)
    assert result["reachable"] is False
    assert result["releases"] == []
    assert "kubeconfig" in (result["error"] or "")


def test_cluster_status_unreachable(tmp_path):
    if shutil.which("kubectl") is None:
        pytest.skip("kubectl not installed")
    result = cluster_probe.cluster_status(str(tmp_path / "missing-kubeconfig"))
    assert result["reachable"] is False
    assert result["error"]


def test_cluster_status_endpoint(client, viewer_headers):
    resp = client.get("/api/v1/genestack/cluster/status", headers=viewer_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "reachable" in body
    assert "nodes" in body and "namespaces" in body
    assert "error" in body

    resp = client.get("/api/v1/genestack/services/status", headers=viewer_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "reachable" in body and "releases" in body


def test_cluster_status_job(client, admin_headers):
    job = _run_job(client, admin_headers, "genestack.cluster.status")
    # Never raises; unreachable cluster is still a successful probe result
    assert job["status"] == "success", job
    assert "reachable=" in (job.get("log_text") or "")


# ------------------------------------------------- components env scoping


def test_components_desired_job_scoped_to_env_config_dir(
    client, operator_headers, tmp_path
):
    """The components.desired read job resolves the env's config-dir copy."""
    config_dir = tmp_path / "etc-genestack"
    config_dir.mkdir()
    env_components = config_dir / "openstack-components.yaml"
    env_components.write_text(
        yaml.safe_dump({"components": {"keystone": True, "cinder": False}}),
        encoding="utf-8",
    )
    eid = _create_env(client, operator_headers, genestack_config_dir=str(config_dir))

    job = client.post(
        f"/api/v1/environments/{eid}/jobs",
        headers=operator_headers,
        json={
            "operation": "genestack.components.desired",
            "params": {},
            "run_sync": True,
        },
    )
    assert job.status_code in (200, 201), job.text
    assert str(env_components) in (job.json().get("log_text") or "")
