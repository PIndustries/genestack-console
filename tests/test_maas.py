"""MAAS integration / mock tests.

The shared test config (tests/conftest.py) sets ``maas.mock: true``
explicitly — the mock inventory is opt-in, never the default.
"""

from __future__ import annotations

import pytest

import yaml


def _extract_machines(payload):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("machines", "items", "results", "data", "nodes"):
            if key in payload and isinstance(payload[key], list):
                return payload[key]
    return []


def test_list_machines_mock_returns_at_least_three(client, admin_headers):
    """GET /api/v1/maas/machines with maas.mock: true returns >= 3 machines."""
    resp = client.get("/api/v1/maas/machines", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body.get("mock") is True
    assert body.get("maas_configured") is True
    machines = _extract_machines(body)
    assert (
        len(machines) >= 3
    ), f"expected >= 3 mock machines, got {len(machines)}: {body}"


def test_machine_power_mock(client, admin_headers):
    """GET /api/v1/maas/machines/{system_id}/power returns mock power state."""
    resp = client.get("/api/v1/maas/machines/abc123/power", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body.get("system_id") == "abc123"
    assert body.get("power_state") == "on"


def test_machine_power_unknown_system_id_404(client, admin_headers):
    resp = client.get(
        "/api/v1/maas/machines/no-such-machine/power", headers=admin_headers
    )
    assert resp.status_code == 404, resp.text


# ---------------------------------------------------------------------------
# Mock default OFF: unconfigured MAAS never serves fake machines
# ---------------------------------------------------------------------------


def test_settings_maas_mock_defaults_off():
    from app.config import Settings

    settings = Settings()
    assert settings.maas_mock is False
    assert settings.maas_url == ""


def test_load_settings_parses_maas_mock_opt_in(tmp_path):
    from app.config import load_settings

    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump({"maas": {"url": "", "api_key": "", "mock": True}}))
    assert load_settings(cfg).maas_mock is True

    cfg.write_text(yaml.safe_dump({"maas": {"url": "", "api_key": ""}}))
    assert load_settings(cfg).maas_mock is False


def test_unconfigured_client_returns_empty_list_not_mock():
    """No URL + mock off: empty reads, no fake inventory, clear write errors."""
    from app.services.maas import MaasClient, MaasError

    maas = MaasClient(url="", api_key="")
    assert maas.mock is False
    assert maas.configured is False
    assert maas.live is False
    assert maas.list_machines() == []
    assert maas.list_tags() == []
    assert maas.list_images() == []

    with pytest.raises(MaasError) as excinfo:
        maas.get_machine("abc123")
    assert excinfo.value.status_code == 503
    for call in (maas.commission, maas.deploy, maas.release):
        with pytest.raises(MaasError) as excinfo:
            call("abc123")
        assert excinfo.value.status_code == 503


def test_from_settings_mock_is_explicit_opt_in():
    from app.services.maas import MaasClient

    plain = MaasClient.from_settings({"maas_url": "", "maas_api_key": ""})
    assert plain.mock is False
    assert plain.list_machines() == []

    opted_in = MaasClient.from_settings(
        {"maas_url": "", "maas_api_key": "", "maas_mock": True}
    )
    assert opted_in.mock is True
    assert opted_in.configured is True
    assert len(opted_in.list_machines()) >= 3


def test_machines_api_unconfigured_empty_state(client, admin_headers, monkeypatch):
    """With mock off and no URL, the API says maas_configured: false, []."""
    from app.config import Settings

    monkeypatch.setattr(
        "app.routers.maas.get_settings",
        lambda: Settings(maas_url="", maas_api_key="", maas_mock=False),
    )
    resp = client.get("/api/v1/maas/machines", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["maas_configured"] is False
    assert body["mock"] is False
    assert body["machines"] == []
    assert body["count"] == 0


# ---------------------------------------------------------------------------
# MaasClient mock write-op transitions (no API / no MAAS needed)
# ---------------------------------------------------------------------------


def test_mock_client_commission_deploy_release_cycle():
    """Mock client cycles Ready -> Commissioning -> Deployed -> Released."""
    from app.services.maas import MaasClient

    maas = MaasClient(url="", api_key="", mock=True)
    assert maas.mock
    assert maas.get_machine("def456")["status_name"] == "Ready"

    machine = maas.commission("def456")
    assert machine["status_name"] == "Commissioning"
    assert machine["status"] == 1
    # Transition is observable through the read methods on the same instance
    assert maas.get_machine("def456")["status_name"] == "Commissioning"

    machine = maas.deploy("def456", hostname="gs-compute-02")
    assert machine["status_name"] == "Deployed"
    assert machine["status"] == 6
    assert machine["hostname"] == "gs-compute-02"
    assert maas.get_machine("def456")["hostname"] == "gs-compute-02"

    machine = maas.release("def456")
    assert machine["status_name"] == "Released"
    assert maas.get_machine("def456")["status_name"] == "Released"


def test_mock_client_transitions_are_instance_local():
    """A fresh mock client sees the pristine inventory, not prior transitions."""
    from app.services.maas import MaasClient

    maas = MaasClient(url="", api_key="", mock=True)
    maas.commission("def456")
    assert maas.get_machine("def456")["status_name"] == "Commissioning"

    fresh = MaasClient(url="", api_key="", mock=True)
    assert fresh.get_machine("def456")["status_name"] == "Ready"


def test_mock_client_write_ops_unknown_system_id_404():
    from app.services.maas import MaasClient, MaasError

    maas = MaasClient(url="", api_key="", mock=True)
    for call in (
        maas.commission,
        maas.deploy,
        maas.release,
    ):
        with pytest.raises(MaasError) as excinfo:
            call("no-such-machine")
        assert excinfo.value.status_code == 404


# ---------------------------------------------------------------------------
# MaasClient mock boot-resources (talos factory image zero-touch chain)
# ---------------------------------------------------------------------------


def test_mock_client_upload_and_list_images():
    """Mock upload_image records the boot-resource; list_images returns it."""
    from app.services.maas import MaasClient

    maas = MaasClient(url="", api_key="", mock=True)
    assert maas.list_images() == []

    record = maas.upload_image(
        "talos-genestack", b"fake-image-bytes", title="Talos factory image"
    )
    assert record["name"] == "talos-genestack"
    assert record["type"] == "uploaded"
    assert record["size"] == len(b"fake-image-bytes")

    images = maas.list_images()
    assert images == [
        {"name": "talos-genestack", "title": "Talos factory image", "type": "uploaded"}
    ]

    # Instance-local, like the machine write-op overrides
    fresh = MaasClient(url="", api_key="", mock=True)
    assert fresh.list_images() == []


def test_mock_client_upload_from_file(tmp_path):
    """upload_image accepts a path (M3 disk download); record matches bytes."""
    import hashlib
    from pathlib import Path

    from app.services.maas import MaasClient

    image_file = tmp_path / "talos.raw.xz"
    image_file.write_bytes(b"fake-image-bytes")

    maas = MaasClient(url="", api_key="", mock=True)
    record = maas.upload_image("talos-genestack", image_file, title="from disk")
    assert record["size"] == len(b"fake-image-bytes")
    assert record["sha256"] == hashlib.sha256(b"fake-image-bytes").hexdigest()
    assert record["title"] == "from disk"

    with pytest.raises(Exception, match="image file not found"):
        maas.upload_image("x", Path(tmp_path / "missing.img"))


def test_mock_client_deploy_with_custom_image():
    """deploy(image=...) records osystem=custom + distro_series=<image>."""
    from app.services.maas import MaasClient

    maas = MaasClient(url="", api_key="", mock=True)
    machine = maas.deploy("def456", image="talos-genestack")
    assert machine["status_name"] == "Deployed"
    assert machine["osystem"] == "custom"
    assert machine["distro_series"] == "talos-genestack"
    assert machine["deployed_image"] == "talos-genestack"

    # Observable through reads on the same instance
    assert maas.get_machine("def456")["distro_series"] == "talos-genestack"


def test_mock_client_deploy_without_image_keeps_ubuntu():
    """image=None keeps the default genestack Ubuntu deploy behavior."""
    from app.services.maas import MaasClient

    maas = MaasClient(url="", api_key="", mock=True)
    machine = maas.deploy("def456")
    assert "deployed_image" not in machine
    assert machine["osystem"] == "ubuntu"
