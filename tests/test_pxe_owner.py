"""Real DFW regression: API startup and a worker must not bind competing PXE runtimes."""

from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from app.services import pxe, pxe_owner, pxe_runtime
from tests.test_hardware_installation_save import stored_machine
from tests.test_pxe import PXE_CFG, _fake_download, _settings, _tar_asset

READY = {"http_running": True, "dhcp_running": True, "tftp_running": True}
BUSY = {
    "http_running": False,
    "dhcp_running": False,
    "tftp_running": False,
    "http_error": "http bind 10.240.0.50:8088: [Errno 98] Address already in use",
}


def transport(monkeypatch, handler):
    original = httpx.Client
    options = []

    def client(**kwargs):
        options.append(kwargs)
        return original(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(pxe_owner.httpx, "Client", client)
    return options


def test_worker_delegates_only_to_authenticated_loopback_owner(monkeypatch):
    identity = str(uuid4())
    calls = []

    def handler(request):
        calls.append(request)
        assert request.url.host == "127.0.0.1" and request.url.port == 8090
        assert request.headers["X-API-Key"] == "fixture-operator-key"
        assert request.url.path == f"/api/v1/environments/{identity}/pxe/runtime/reload"
        return httpx.Response(200, json={"environment_id": identity, "ok": True, "runtime": READY})

    options = transport(monkeypatch, handler)
    settings = SimpleNamespace(
        port=8090, parsed_api_keys=lambda: {"fixture-operator-key": "operator"}
    )
    result = pxe_owner.reload_api_owner(identity, settings)
    assert pxe_owner.ready(result) and result["owner"] == "api" and len(calls) == 1
    assert options[0]["trust_env"] is False and options[0]["follow_redirects"] is False


@pytest.mark.parametrize(
    "kind", ["redirect", "wrong-environment", "partial-runtime", "unauthorized"]
)
def test_foreign_or_unready_owner_is_not_accepted_and_never_leaks_auth(monkeypatch, kind):
    identity = str(uuid4())
    calls = []

    def handler(request):
        calls.append(request)
        if kind == "redirect":
            return httpx.Response(307, headers={"Location": "https://foreign.invalid/"})
        if kind == "unauthorized":
            return httpx.Response(401, text="fixture-private-key")
        return httpx.Response(
            200,
            json={
                "environment_id": identity if kind != "wrong-environment" else str(uuid4()),
                "ok": True,
                "runtime": READY if kind != "partial-runtime" else BUSY,
            },
        )

    transport(monkeypatch, handler)
    settings = SimpleNamespace(port=8090, parsed_api_keys=lambda: {"fixture-private-key": "admin"})
    result = pxe_owner.reload_api_owner(identity, settings)
    assert not pxe_owner.ready(result) and len(calls) == 1
    assert "fixture-private-key" not in repr(result) and "foreign.invalid" not in repr(result)


def test_preparation_recovers_from_the_actual_dfw_port_conflict(tmp_path, monkeypatch):
    identity = str(uuid4())
    monkeypatch.setattr(pxe, "download_factory_image", _fake_download(_tar_asset()))
    monkeypatch.setattr(pxe_runtime, "apply_pxe_cfg", lambda *args: dict(BUSY))
    calls = []

    def reload(env, settings):
        calls.append(env)
        return {**READY, "owner": "api"}

    monkeypatch.setattr(pxe_owner, "reload_api_owner", reload)
    result = pxe.ensure_assets_and_config(
        SimpleNamespace(id=identity, name="physical-dfw"),
        {"pxe": dict(PXE_CFG), "talos": {"image_url": "https://factory.example/talos.tar"}},
        _settings(tmp_path),
    )
    assert result["ok"] and result["runtime"]["owner"] == "api" and calls == [identity]


def test_missing_helper_is_not_misdiagnosed_as_an_owner_conflict(tmp_path, monkeypatch):
    monkeypatch.setattr(pxe, "download_factory_image", _fake_download(_tar_asset()))
    monkeypatch.setattr(
        pxe_runtime,
        "apply_pxe_cfg",
        lambda *args: {**BUSY, "http_error": None, "tftp_error": "helper unavailable"},
    )
    monkeypatch.setattr(
        pxe_owner, "reload_api_owner", lambda *args: pytest.fail("Not a socket conflict")
    )
    result = pxe.ensure_assets_and_config(
        SimpleNamespace(id=str(uuid4()), name="physical-dfw"),
        {"pxe": dict(PXE_CFG), "talos": {"image_url": "https://factory.example/talos.tar"}},
        _settings(tmp_path),
    )
    assert not result["ok"] and result["error"] == "helper unavailable"


def test_api_owner_reload_never_rewrites_pending_boot_files_or_submits_jobs(
    client, admin_headers, monkeypatch
):
    _, base, _, _ = stored_machine(client, admin_headers)
    calls = []
    monkeypatch.setattr(
        pxe, "ensure_assets_and_config", lambda *a, **k: pytest.fail("Do not rewrite boot files")
    )
    monkeypatch.setattr(
        pxe_runtime, "apply_pxe_cfg", lambda *args: calls.append(args) or dict(READY)
    )
    response = client.post(base + "/pxe/runtime/reload", headers=admin_headers)
    assert response.status_code == 200 and response.json()["ok"] is True
    assert len(calls) == 1 and len(calls[0][1]) == 1
    assert client.get("/api/v1/jobs", headers=admin_headers).json() == []


def test_api_does_not_delegate_again_when_a_foreign_process_owns_the_port(
    client, admin_headers, monkeypatch
):
    _, base, _, _ = stored_machine(client, admin_headers)
    monkeypatch.setattr(pxe_runtime, "apply_pxe_cfg", lambda *a: dict(BUSY))
    monkeypatch.setattr(
        pxe_owner, "reload_api_owner", lambda *a: pytest.fail("Recursive delegation")
    )
    response = client.post(base + "/pxe/runtime/reload", headers=admin_headers)
    assert response.status_code == 200 and response.json()["ok"] is False


def test_viewer_cannot_reload_provisioning_runtime(
    client, admin_headers, viewer_headers, monkeypatch
):
    _, base, _, _ = stored_machine(client, admin_headers)
    monkeypatch.setattr(
        pxe_runtime, "apply_pxe_cfg", lambda *a: pytest.fail("Viewer changed runtime")
    )
    assert client.post(base + "/pxe/runtime/reload", headers=viewer_headers).status_code == 403
