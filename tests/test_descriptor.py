"""Environment descriptor endpoint tests (Phase 3)."""

from __future__ import annotations

import uuid
from pathlib import Path

import yaml


def _write_fake_config_dir(config_dir: Path) -> Path:
    """Build a fake /etc/genestack-equivalent config dir at ``config_dir``."""
    (config_dir / "provider").parent.mkdir(parents=True, exist_ok=True)
    (config_dir / "provider").write_text("kubespray\n", encoding="utf-8")

    inventory = config_dir / "inventory"
    (inventory / "group_vars" / "all").mkdir(parents=True)
    (inventory / "group_vars" / "kube_control_plane").mkdir(parents=True)
    (inventory / "inventory.yaml").write_text(
        yaml.safe_dump(
            {
                "all": {
                    "children": {
                        "kube_control_plane": {"hosts": {"cp-1": {}, "cp-2": {}}},
                        "kube_node": {"hosts": {"worker-1": {}}},
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    (config_dir / "openstack-components.yaml").write_text(
        yaml.safe_dump({"components": {"keystone": True, "glance": False}}),
        encoding="utf-8",
    )
    (config_dir / "helm-chart-versions.yaml").write_text(
        yaml.safe_dump({"charts": {"keystone": "2026.1.8+db238e7c3"}}),
        encoding="utf-8",
    )

    for service, filename in (
        ("keystone", "keystone-helm-overrides.yaml"),
        ("glance", "glance.yaml"),
        ("global_overrides", "global.yaml"),
    ):
        service_dir = config_dir / "helm-configs" / service
        service_dir.mkdir(parents=True)
        (service_dir / filename).write_text("replicas: 2\n", encoding="utf-8")

    overlay = config_dir / "kustomize" / "keystone" / "overlay"
    overlay.mkdir(parents=True)
    (overlay / "kustomization.yaml").write_text("resources: []\n", encoding="utf-8")
    # A kustomize dir without overlay/kustomization.yaml is not an overlay
    (config_dir / "kustomize" / "glance").mkdir(parents=True)

    listeners = config_dir / "gateway-api" / "listeners"
    listeners.mkdir(parents=True)
    (listeners / "gateway.yaml").write_text("kind: Gateway\n", encoding="utf-8")
    routes = config_dir / "gateway-api" / "routes"
    routes.mkdir(parents=True)
    (routes / "keystone.yaml").write_text("kind: HTTPRoute\n", encoding="utf-8")
    metallb = config_dir / "manifests" / "metallb"
    metallb.mkdir(parents=True)
    (metallb / "pools.yaml").write_text("kind: IPAddressPool\n", encoding="utf-8")

    return config_dir


def _create_env(client, headers, **fields) -> dict:
    name = f"env-desc-{uuid.uuid4().hex[:10]}"
    resp = client.post(
        "/api/v1/environments", headers=headers, json={"name": name, **fields}
    )
    assert resp.status_code in (200, 201), resp.text
    return resp.json()


def test_descriptor_full_config_dir(
    client, admin_headers, viewer_headers, genestack_root, tmp_path
):
    """Descriptor assembles every section from a populated config dir."""
    config_dir = _write_fake_config_dir(tmp_path / "etc-genestack")
    env = _create_env(
        client,
        admin_headers,
        region="lab",
        tier="dev",
        description="descriptor test env",
        genestack_path=str(genestack_root),
        genestack_config_dir=str(config_dir),
    )

    resp = client.get(
        f"/api/v1/environments/{env['id']}/descriptor", headers=viewer_headers
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["descriptor_version"] == 1
    assert body["generated_at"]

    assert body["environment"] == {
        "id": env["id"],
        "name": env["name"],
        "region": "lab",
        "tier": "dev",
        "description": "descriptor test env",
    }

    assert body["provider"] == {"provider": "kubespray", "error": None}

    topology = body["topology"]
    assert topology["error"] is None
    groups = {g["name"]: g for g in topology["groups"]}
    assert groups["kube_control_plane"] == {
        "name": "kube_control_plane",
        "hosts": ["cp-1", "cp-2"],
        "count": 2,
    }
    assert groups["kube_node"]["hosts"] == ["worker-1"]
    assert topology["group_vars"] == ["all", "kube_control_plane"]

    components = body["components"]
    assert components["scope"] == "environment"
    assert components["components"] == {"keystone": True, "glance": False}
    assert components["chart_versions"] == {
        "charts": {"keystone": "2026.1.8+db238e7c3"}
    }
    assert components["chart_versions_source"] == "environment"
    assert components["error"] is None

    helm = body["helm_overrides"]
    assert helm["error"] is None
    # Local helm-configs plus services that only exist under base-helm-configs.
    assert helm["services"]["keystone"] == ["keystone-helm-overrides.yaml"]
    assert helm["services"]["glance"] == ["glance-helm-overrides.yaml", "glance.yaml"]
    assert helm["services"]["cinder"] == ["cinder-helm-overrides.yaml"]
    assert helm["services"]["global_overrides"] == ["global.yaml"]
    assert helm["base_defaults"]["keystone"] is True
    assert helm["base_defaults"]["cinder"] is True
    assert helm["base_defaults"]["glance"] is False
    assert helm["base_defaults"]["global_overrides"] is False

    assert body["kustomize_overlays"] == ["keystone"]

    gateway = body["gateway"]
    assert gateway["gateway_api"] == ["listeners/gateway.yaml", "routes/keystone.yaml"]
    assert gateway["metallb"] == ["pools.yaml"]

    live = body["live_state"]
    assert isinstance(live["cluster"]["reachable"], bool)
    assert isinstance(live["cluster"]["nodes"], int)
    assert isinstance(live["services"]["releases"], int)


def test_descriptor_yaml_format(client, admin_headers, viewer_headers, tmp_path):
    """?format=yaml returns a parseable YAML PlainTextResponse."""
    config_dir = _write_fake_config_dir(tmp_path / "etc-genestack")
    env = _create_env(client, admin_headers, genestack_config_dir=str(config_dir))

    resp = client.get(
        f"/api/v1/environments/{env['id']}/descriptor",
        params={"format": "yaml"},
        headers=viewer_headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/plain")

    parsed = yaml.safe_load(resp.text)
    assert parsed["descriptor_version"] == 1
    assert parsed["environment"]["id"] == env["id"]
    assert parsed["provider"]["provider"] == "kubespray"


def test_descriptor_unknown_environment_404(client, viewer_headers):
    resp = client.get(
        "/api/v1/environments/does-not-exist/descriptor", headers=viewer_headers
    )
    assert resp.status_code == 404


def test_descriptor_requires_auth(client):
    resp = client.get("/api/v1/environments/does-not-exist/descriptor")
    assert resp.status_code in (401, 403)


def test_descriptor_degrades_on_empty_config_dir(
    client, admin_headers, viewer_headers, genestack_root, tmp_path
):
    """An empty config dir yields nulls/empty lists with error notes, no 500."""
    config_dir = tmp_path / "etc-genestack-empty"
    config_dir.mkdir()
    env = _create_env(
        client,
        admin_headers,
        genestack_path=str(genestack_root),
        genestack_config_dir=str(config_dir),
    )

    resp = client.get(
        f"/api/v1/environments/{env['id']}/descriptor", headers=viewer_headers
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["provider"]["provider"] is None
    assert body["provider"]["error"]

    assert body["topology"]["groups"] == []
    assert body["topology"]["group_vars"] == []
    assert body["topology"]["error"]

    components = body["components"]
    assert components["scope"] == "environment"
    assert components["components"] == {}
    assert components["error"]  # openstack-components.yaml missing
    # chart versions fall back to the genestack root copy
    assert components["chart_versions_source"] == "global"
    assert components["chart_versions"]["charts"]["keystone"] == "2026.1.8+db238e7c3"

    # No local helm-configs, but base-helm-configs still lists services for ye.
    assert body["helm_overrides"]["error"] is None
    assert "keystone" in body["helm_overrides"]["services"]
    assert body["helm_overrides"]["base_defaults"]["keystone"] is True
    assert body["kustomize_overlays"] == []
    assert body["gateway"] == {"gateway_api": [], "metallb": [], "error": None}
    assert isinstance(body["live_state"]["cluster"]["reachable"], bool)


def test_descriptor_without_config_dir(client, admin_headers, viewer_headers):
    """Create without a dir still gets a local-hub path; empty dir sections note missing files."""
    env = _create_env(client, admin_headers)
    assert env["genestack_config_dir"].endswith(
        f"/environments/{env['name']}/etc-genestack"
    )

    resp = client.get(
        f"/api/v1/environments/{env['id']}/descriptor", headers=viewer_headers
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["provider"]["error"] and "provider" in body["provider"]["error"]
    assert body["topology"]["error"] and "inventory" in body["topology"]["error"]
    assert body["components"]["scope"] == "environment"
    assert body["kustomize_overlays"] == []
    assert body["gateway"]["error"] is None
    assert "reachable" in body["live_state"]["cluster"]
