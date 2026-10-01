"""maas.machine.commission/deploy/release operation tests (via the jobs API)."""

from __future__ import annotations

import base64
import uuid

import yaml


def _suffix() -> str:
    return uuid.uuid4().hex[:8]


def _create_env(client, headers, **fields):
    body = {"name": f"maas-env-{_suffix()}", **fields}
    resp = client.post("/api/v1/environments", headers=headers, json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _submit(client, headers, env_id, operation, params=None):
    resp = client.post(
        f"/api/v1/environments/{env_id}/jobs",
        headers=headers,
        json={"operation": operation, "params": params or {}, "run_sync": True},
    )
    assert resp.status_code in (200, 201), resp.text
    return resp.json()


def _create_tenant(client, admin_headers):
    resp = client.post(
        "/api/v1/tenants", headers=admin_headers, json={"name": f"tenant-{_suffix()}"}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _create_user(client, admin_headers, memberships=None):
    username = f"user-{_suffix()}"
    resp = client.post(
        "/api/v1/users",
        headers=admin_headers,
        json={"username": username, "password": "pw", "memberships": memberships or []},
    )
    assert resp.status_code == 201, resp.text
    return username


def _login_headers(client, username):
    resp = client.post(
        "/api/v1/auth/login", json={"username": username, "password": "pw"}
    )
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


# ---------------------------------------------------------------------------
# Dry-run
# ---------------------------------------------------------------------------


def test_commission_dry_run_logs_without_executing(client, admin_headers, monkeypatch):
    """Global test config is dry_run=True: the MAAS call is logged, not made."""
    from app.services.maas import MaasClient

    calls = []

    def _recorder(self, system_id, user_data_b64=None):  # pragma: no cover
        calls.append(system_id)

    monkeypatch.setattr(MaasClient, "commission", _recorder)

    env = _create_env(client, admin_headers)
    job = _submit(
        client,
        admin_headers,
        env["id"],
        "maas.machine.commission",
        {"system_id": "def456"},
    )
    assert job["status"] == "success", job
    assert "[dry-run]" in job["log_text"]
    assert "op=commission" in job["log_text"]
    assert calls == [], "dry-run must not call MaasClient.commission"


# ---------------------------------------------------------------------------
# Mock execution (env dry_run=False, empty MAAS url => mock client)
# ---------------------------------------------------------------------------


def test_commission_mock_success(client, admin_headers):
    env = _create_env(client, admin_headers, dry_run=False)
    job = _submit(
        client,
        admin_headers,
        env["id"],
        "maas.machine.commission",
        {"system_id": "def456"},
    )
    assert job["status"] == "success", job
    assert "Commissioning" in job["log_text"]


def test_deploy_renders_userdata_and_upserts_doc(client, admin_headers, monkeypatch):
    """deploy with roles renders GENESTACK_ENV/ROLE markers and upserts the doc."""
    from app.services.maas import MaasClient

    captured = {}

    def _fake_deploy(self, system_id, user_data_b64=None, hostname=None, image=None):
        captured["system_id"] = system_id
        captured["user_data_b64"] = user_data_b64
        captured["hostname"] = hostname
        captured["image"] = image
        return {
            "system_id": system_id,
            "hostname": hostname,
            "status_name": "Deployed",
            "status": 6,
        }

    monkeypatch.setattr(MaasClient, "deploy", _fake_deploy)

    env = _create_env(client, admin_headers, dry_run=False)
    job = _submit(
        client,
        admin_headers,
        env["id"],
        "maas.machine.deploy",
        {
            "system_id": "def456",
            "hostname": "gs-node-01",
            "roles": ["compute", "storage"],
        },
    )
    assert job["status"] == "success", job

    # Cloud-init user-data carries the markers provision_bridge.yml reads
    user_data = base64.b64decode(captured["user_data_b64"]).decode()
    assert f'GENESTACK_ENV="{env["name"]}"' in user_data
    assert 'GENESTACK_ROLE="compute,storage"' in user_data
    assert "hostname: gs-node-01" in user_data
    assert captured["hostname"] == "gs-node-01"

    # Deploy upserted the env config doc servers section (new version)
    config = client.get(
        f"/api/v1/environments/{env['id']}/config", headers=admin_headers
    )
    assert config.status_code == 200, config.text
    doc = yaml.safe_load(config.json()["yaml"])
    entry = doc["servers"]["gs-node-01"]
    assert entry["system_id"] == "def456"
    assert entry["source"] == "maas"
    assert entry["roles"] == ["compute", "storage"]

    versions = client.get(
        f"/api/v1/environments/{env['id']}/config/versions", headers=admin_headers
    )
    assert versions.status_code == 200
    assert len(versions.json()) == 1


def test_deploy_unknown_role_fails(client, admin_headers):
    env = _create_env(client, admin_headers, dry_run=False)
    job = _submit(
        client,
        admin_headers,
        env["id"],
        "maas.machine.deploy",
        {"system_id": "def456", "roles": ["compute", "bogus"]},
    )
    assert job["status"] == "failed", job
    assert "unknown role" in job["error"]


def test_deploy_invalid_system_id_surfaces_maas_error(client, admin_headers):
    env = _create_env(client, admin_headers, dry_run=False)
    job = _submit(
        client,
        admin_headers,
        env["id"],
        "maas.machine.deploy",
        {"system_id": "no-such-machine"},
    )
    assert job["status"] == "failed", job
    assert "not found" in job["error"].lower()


def test_release_mock_success(client, admin_headers):
    env = _create_env(client, admin_headers, dry_run=False)
    job = _submit(
        client,
        admin_headers,
        env["id"],
        "maas.machine.release",
        {"system_id": "abc123"},
    )
    assert job["status"] == "success", job
    assert "Released" in job["log_text"]


def test_release_requires_environment(client, admin_headers):
    resp = client.post(
        "/api/v1/jobs",
        headers=admin_headers,
        json={
            "operation": "maas.machine.release",
            "params": {"system_id": "abc123"},
            "run_sync": True,
        },
    )
    assert resp.status_code in (200, 201), resp.text
    job = resp.json()
    assert job["status"] == "failed", job
    assert "requires an environment" in job["error"]


# ---------------------------------------------------------------------------
# Credentials / scoping
# ---------------------------------------------------------------------------


def test_per_env_maas_url_overrides_global(client, admin_headers, monkeypatch):
    from app.services.maas import MaasClient

    captured = {}

    def _fake_from_settings(settings):
        captured.update(settings)
        return MaasClient(url="", api_key="", mock=True)

    monkeypatch.setattr(MaasClient, "from_settings", _fake_from_settings)

    env = _create_env(
        client,
        admin_headers,
        dry_run=False,
        maas_url="http://maas-env.example:5240/MAAS",
    )
    job = _submit(
        client,
        admin_headers,
        env["id"],
        "maas.machine.commission",
        {"system_id": "def456"},
    )
    assert job["status"] == "success", job
    assert captured["maas_url"] == "http://maas-env.example:5240/MAAS"


def test_maas_write_ops_tenant_scoping(client, admin_headers):
    """Cross-tenant operator gets 403; a viewer cannot submit the ops at all."""
    tenant_a = _create_tenant(client, admin_headers)
    tenant_b = _create_tenant(client, admin_headers)
    env_a = _create_env(client, admin_headers, tenant_id=tenant_a["id"])
    env_b = _create_env(client, admin_headers, tenant_id=tenant_b["id"])

    operator = _create_user(
        client, admin_headers, [{"tenant_id": tenant_a["id"], "role": "operator"}]
    )
    viewer = _create_user(
        client, admin_headers, [{"tenant_id": tenant_a["id"], "role": "viewer"}]
    )
    operator_headers = _login_headers(client, operator)
    viewer_headers = _login_headers(client, viewer)

    for op in (
        "maas.machine.commission",
        "maas.machine.deploy",
        "maas.machine.release",
    ):
        # Operator of tenant A cannot run write ops against tenant B's env
        resp = client.post(
            f"/api/v1/environments/{env_b['id']}/jobs",
            headers=operator_headers,
            json={"operation": op, "params": {"system_id": "abc123"}, "run_sync": True},
        )
        assert resp.status_code == 403, (op, resp.text)
        # Viewer of tenant A cannot run write ops even in their own tenant
        resp = client.post(
            f"/api/v1/environments/{env_a['id']}/jobs",
            headers=viewer_headers,
            json={"operation": op, "params": {"system_id": "abc123"}, "run_sync": True},
        )
        assert resp.status_code == 403, (op, resp.text)


def test_maas_write_ops_in_catalog(client, admin_headers):
    resp = client.get("/api/v1/operations", headers=admin_headers)
    assert resp.status_code == 200
    ops = {op["id"]: op for op in resp.json()}
    for op_id, timeout in (
        ("maas.machine.commission", 1800),
        ("maas.machine.deploy", 1800),
        ("maas.machine.release", 600),
        ("maas.talos.image_upload", 1800),
    ):
        op = ops[op_id]
        assert op["required_role"] == "operator"
        assert op["mutating"] is True
        assert op["timeout_seconds"] == timeout


# ---------------------------------------------------------------------------
# maas.machine.deploy with a custom image (talos zero-touch)
# ---------------------------------------------------------------------------


def test_deploy_with_custom_image_skips_userdata(client, admin_headers, monkeypatch):
    """image=talos-genestack deploys osystem=custom and skips cloud-init."""
    from app.services.maas import MaasClient

    captured = {}

    def _fake_deploy(self, system_id, user_data_b64=None, hostname=None, image=None):
        captured["system_id"] = system_id
        captured["user_data_b64"] = user_data_b64
        captured["hostname"] = hostname
        captured["image"] = image
        return {
            "system_id": system_id,
            "hostname": hostname,
            "status_name": "Deployed",
            "status": 6,
            "osystem": "custom" if image else "ubuntu",
            "distro_series": image or "noble",
        }

    monkeypatch.setattr(MaasClient, "deploy", _fake_deploy)

    env = _create_env(client, admin_headers, dry_run=False)
    job = _submit(
        client,
        admin_headers,
        env["id"],
        "maas.machine.deploy",
        {
            "system_id": "def456",
            "hostname": "gs-talos-01",
            "roles": ["k8s_control_plane"],
            "image": "talos-genestack",
        },
    )
    assert job["status"] == "success", job
    # Talos doesn't consume cloud-init: no user-data rendered despite roles
    assert captured["image"] == "talos-genestack"
    assert captured["user_data_b64"] is None
    assert "skipping cloud-init user-data" in job["log_text"]

    # The servers-doc upsert still happens for custom-image deploys
    config = client.get(
        f"/api/v1/environments/{env['id']}/config", headers=admin_headers
    )
    assert config.status_code == 200, config.text
    doc = yaml.safe_load(config.json()["yaml"])
    entry = doc["servers"]["gs-talos-01"]
    assert entry["system_id"] == "def456"
    assert entry["source"] == "maas"
    assert entry["roles"] == ["k8s_control_plane"]


def test_deploy_dry_run_logs_image(client, admin_headers):
    env = _create_env(client, admin_headers)
    job = _submit(
        client,
        admin_headers,
        env["id"],
        "maas.machine.deploy",
        {"system_id": "def456", "image": "talos-genestack"},
    )
    assert job["status"] == "success", job
    assert "[dry-run]" in job["log_text"]
    assert "image=talos-genestack" in job["log_text"]


# ---------------------------------------------------------------------------
# maas.talos.image_upload
# ---------------------------------------------------------------------------

FACTORY_URL = "https://factory.talos.dev/image/abc123/v1.9.0/nocloud-amd64.raw.xz"


def test_talos_image_upload_dry_run_logs_url_and_name(client, admin_headers):
    """Global test config is dry_run=True: URL + would-be name are logged."""
    env = _create_env(client, admin_headers)
    job = _submit(
        client,
        admin_headers,
        env["id"],
        "maas.talos.image_upload",
        {"image_url": FACTORY_URL},
    )
    assert job["status"] == "success", job
    assert "[dry-run]" in job["log_text"]
    assert FACTORY_URL in job["log_text"]
    # Upload name derived from the URL basename, talos- prefixed
    assert "talos-nocloud-amd64" in job["log_text"]


def test_talos_image_upload_missing_url_fails(client, admin_headers):
    """No param and no env doc talos.image_url -> rc=2 failure."""
    env = _create_env(client, admin_headers, dry_run=False)
    job = _submit(client, admin_headers, env["id"], "maas.talos.image_upload")
    assert job["status"] == "failed", job
    assert "image_url is required" in job["error"]


def test_talos_image_upload_defaults_to_env_doc(client, admin_headers):
    """image_url falls back to the env config doc talos.image_url."""
    env = _create_env(client, admin_headers)
    resp = client.put(
        f"/api/v1/environments/{env['id']}/config",
        headers=admin_headers,
        json={
            "yaml_text": (
                "talos:\n" "  cluster_name: gs-talos\n" f"  image_url: {FACTORY_URL}\n"
            )
        },
    )
    assert resp.status_code == 201, resp.text
    job = _submit(client, admin_headers, env["id"], "maas.talos.image_upload")
    assert job["status"] == "success", job
    assert FACTORY_URL in job["log_text"]


def test_talos_image_upload_uses_per_env_creds(client, admin_headers, monkeypatch):
    """Full run: download mocked; upload goes to the env's MAAS (mock client)."""
    from app.services.job_runner import JobRunner
    from app.services.maas import MaasClient

    captured = {}
    fake_content = b"fake-talos-image"

    def _fake_download(self, url, log, dest_dir, filename=None):  # pragma: no cover
        from pathlib import Path

        captured["download_url"] = url
        dest = Path(dest_dir)
        dest.mkdir(parents=True, exist_ok=True)
        path = dest / (filename or "talos.img")
        path.write_bytes(fake_content)
        return path, "deadbeef"

    def _fake_from_settings(settings):
        captured.update(settings)
        return MaasClient(url="", api_key="", mock=True)

    monkeypatch.setattr(JobRunner, "_download_factory_image", _fake_download)
    monkeypatch.setattr(MaasClient, "from_settings", _fake_from_settings)

    env = _create_env(
        client,
        admin_headers,
        dry_run=False,
        maas_url="http://maas-env.example:5240/MAAS",
    )
    job = _submit(
        client,
        admin_headers,
        env["id"],
        "maas.talos.image_upload",
        {"image_url": FACTORY_URL},
    )
    assert job["status"] == "success", job
    assert captured["download_url"] == FACTORY_URL
    assert captured["maas_url"] == "http://maas-env.example:5240/MAAS"
    assert "sha256=deadbeef" in job["log_text"]
    assert "talos-nocloud-amd64" in job["log_text"]


def test_talos_image_upload_tenant_scoping(client, admin_headers):
    """Cross-tenant operator gets 403; a viewer cannot submit the op at all."""
    tenant_a = _create_tenant(client, admin_headers)
    tenant_b = _create_tenant(client, admin_headers)
    env_a = _create_env(client, admin_headers, tenant_id=tenant_a["id"])
    env_b = _create_env(client, admin_headers, tenant_id=tenant_b["id"])

    operator = _create_user(
        client, admin_headers, [{"tenant_id": tenant_a["id"], "role": "operator"}]
    )
    viewer = _create_user(
        client, admin_headers, [{"tenant_id": tenant_a["id"], "role": "viewer"}]
    )
    operator_headers = _login_headers(client, operator)
    viewer_headers = _login_headers(client, viewer)

    resp = client.post(
        f"/api/v1/environments/{env_b['id']}/jobs",
        headers=operator_headers,
        json={
            "operation": "maas.talos.image_upload",
            "params": {"image_url": FACTORY_URL},
            "run_sync": True,
        },
    )
    assert resp.status_code == 403, resp.text
    resp = client.post(
        f"/api/v1/environments/{env_a['id']}/jobs",
        headers=viewer_headers,
        json={
            "operation": "maas.talos.image_upload",
            "params": {"image_url": FACTORY_URL},
            "run_sync": True,
        },
    )
    assert resp.status_code == 403, resp.text


# ---------------------------------------------------------------------------
# release -> env config doc cleanup (B6)
# ---------------------------------------------------------------------------


def test_release_removes_server_from_config_doc(client, admin_headers):
    """A successful MAAS release best-effort drops the server from the env doc."""
    env = _create_env(client, admin_headers, dry_run=False)
    # Deploy first: upserts servers.gs-compute-01 (source=maas) into the doc.
    dep = _submit(
        client,
        admin_headers,
        env["id"],
        "maas.machine.deploy",
        {"system_id": "def456", "hostname": "gs-compute-01", "roles": ["compute"]},
    )
    assert dep["status"] == "success", dep
    cfg = yaml.safe_load(
        client.get(
            f"/api/v1/environments/{env['id']}/config", headers=admin_headers
        ).json()["yaml"]
    )
    assert "gs-compute-01" in cfg["servers"]

    job = _submit(
        client,
        admin_headers,
        env["id"],
        "maas.machine.release",
        {"system_id": "def456"},
    )
    assert job["status"] == "success", job
    assert "Released" in job["log_text"]
    # The release cleaned up the doc (new version), not the release itself failing.
    cfg = yaml.safe_load(
        client.get(
            f"/api/v1/environments/{env['id']}/config", headers=admin_headers
        ).json()["yaml"]
    )
    assert "gs-compute-01" not in (cfg.get("servers") or {})


def test_release_dry_run_does_not_touch_config_doc(client, admin_headers):
    """Global dry-run: release is logged only, the config doc is untouched."""
    env = _create_env(client, admin_headers)  # dry_run stays True (test config)
    # Seed the doc directly: a dry-run deploy does not write the doc.
    put = client.put(
        f"/api/v1/environments/{env['id']}/config",
        headers=admin_headers,
        json={
            "yaml_text": "provider: kubespray\nservers:\n  gs-compute-01:\n    roles: [compute]\n"
        },
    )
    assert put.status_code == 201, put.text
    cfg = yaml.safe_load(
        client.get(
            f"/api/v1/environments/{env['id']}/config", headers=admin_headers
        ).json()["yaml"]
    )
    assert "gs-compute-01" in cfg["servers"]

    job = _submit(
        client,
        admin_headers,
        env["id"],
        "maas.machine.release",
        {"system_id": "def456"},
    )
    assert job["status"] == "success", job
    assert "[dry-run]" in job["log_text"]
    cfg = yaml.safe_load(
        client.get(
            f"/api/v1/environments/{env['id']}/config", headers=admin_headers
        ).json()["yaml"]
    )
    assert "gs-compute-01" in cfg["servers"]


# ---------------------------------------------------------------------------
# genestack.host_setup check flag (B7)
# ---------------------------------------------------------------------------


def test_host_setup_check_flag_forwarded(client, admin_headers, monkeypatch):
    """check=true on genestack.host_setup reaches bridge.run_playbook(check=True)."""
    from app.services import genestack_bridge as bridge

    captured: dict = {}

    def fake_run_playbook(playbook_name, **kwargs):
        captured["playbook"] = playbook_name
        captured["check"] = kwargs.get("check")
        return {"ok": True, "dry_run": True, "returncode": 0, "message": "host-setup"}

    monkeypatch.setattr(bridge, "run_playbook", fake_run_playbook)

    env = _create_env(client, admin_headers)
    job = _submit(
        client,
        admin_headers,
        env["id"],
        "genestack.host_setup",
        {"check": True},
    )
    assert job["status"] == "success", job
    assert captured.get("playbook") == "host-setup.yml"
    assert captured.get("check") is True

    job = _submit(client, admin_headers, env["id"], "genestack.host_setup", {})
    assert job["status"] == "success", job
    assert captured.get("check") is False
