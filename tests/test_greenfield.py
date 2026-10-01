"""Greenfield redeploy: PXE every inventory BMC, then deploy from hosts."""

from __future__ import annotations

import uuid

from app.services.catalog import get_operation


def _suffix() -> str:
    return uuid.uuid4().hex[:8]


def _create_env(client, headers, **fields):
    resp = client.post(
        "/api/v1/environments",
        headers=headers,
        json={"name": f"gf-{_suffix()}", **fields},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _put_doc(client, headers, env_id, doc):
    resp = client.put(
        f"/api/v1/environments/{env_id}/config",
        headers=headers,
        json={"yaml_text": doc},
    )
    assert resp.status_code == 201, resp.text


DOC = """\
provider: talos
talos:
  cluster_name: lab
  install_disk: /dev/sda
servers:
  ctrl1:
    roles: [k8s_control_plane, etcd]
    private_ip: 10.200.0.51
  compute1:
    roles: [compute]
    private_ip: 10.200.0.55
"""


def test_greenfield_in_catalog():
    op = get_operation("genestack.greenfield")
    assert op is not None
    assert op.required_role == "admin"
    assert op.handler == "genestack_greenfield"
    assert op.mutating is True


def test_greenfield_missing_bmc_fails(client, admin_headers):
    env = _create_env(client, admin_headers)
    _put_doc(client, admin_headers, env["id"], DOC)
    resp = client.post(
        f"/api/v1/environments/{env['id']}/jobs",
        headers=admin_headers,
        json={"operation": "genestack.greenfield", "params": {}, "run_sync": True},
    )
    assert resp.status_code == 201, resp.text
    job = resp.json()
    assert job["status"] == "failed"
    assert "BMC" in (job.get("error") or "") or "BMC" in (job.get("log_text") or "")


def test_greenfield_dry_run_pxe_then_hosts(client, admin_headers, monkeypatch):
    from tests.test_baremetal import _make_node

    env = _create_env(client, admin_headers)
    _put_doc(client, admin_headers, env["id"], DOC)
    _make_node(env["id"], name="ctrl1", expected_ip="10.200.0.51")
    _make_node(env["id"], name="compute1", expected_ip="10.200.0.55")

    captured = {}

    def fake_deploy(*args, **kwargs):
        captured["from_stage"] = kwargs.get("from_stage")
        captured["skip_push"] = kwargs.get("skip_push")
        captured["dry_run"] = kwargs.get("dry_run")
        return {"ok": True, "stages_completed": 0, "stages_total": 8, "dry_run": True}

    monkeypatch.setattr("app.services.deploy.run_deploy", fake_deploy)
    resp = client.post(
        f"/api/v1/environments/{env['id']}/jobs",
        headers=admin_headers,
        json={
            "operation": "genestack.greenfield",
            "params": {"dry_run": True},
            "run_sync": True,
        },
    )
    assert resp.status_code == 201, resp.text
    job = resp.json()
    assert job["status"] == "success", job.get("error")
    log = job.get("log_text") or ""
    assert "would PXE/ISO-boot ctrl1" in log
    assert "would PXE/ISO-boot compute1" in log
    assert captured.get("from_stage") == "hosts"
    assert captured.get("dry_run") is True


def test_boot_for_talos_auto_iso_when_pxe_silent(monkeypatch):
    """When PXE traffic is silent, boot_for_talos falls back to ISO boot."""
    from types import SimpleNamespace

    from app.services import baremetal

    calls = []
    monkeypatch.setattr(
        baremetal,
        "pxe_boot",
        lambda *a, **k: calls.append("pxe") or {"ok": True},
    )
    monkeypatch.setattr(baremetal, "pxe_traffic_seen", lambda *a, **k: False)
    monkeypatch.setattr(
        baremetal,
        "iso_boot",
        lambda *a, **k: calls.append("iso")
        or {"ok": True, "image_url": "http://x/iso"},
    )
    logs = []
    node = SimpleNamespace(
        name="n1", pxe_mac="aa:bb:cc:dd:ee:01", expected_ip="10.0.0.9"
    )
    result = baremetal.boot_for_talos(
        None,
        SimpleNamespace(id="env-1"),
        node,
        boot="auto",
        dry_run=False,
        log=logs.append,
    )
    assert calls == ["pxe", "iso"]
    assert result.get("ok") is True
    assert any("not coming through" in ln for ln in logs)
    # Event publishing is tested in test_iso_boot_publishes_kind_iso


def test_boot_for_talos_auto_stays_on_pxe_when_traffic(monkeypatch):
    from types import SimpleNamespace

    from app.services import baremetal

    calls = []
    monkeypatch.setattr(
        baremetal,
        "pxe_boot",
        lambda *a, **k: calls.append("pxe") or {"ok": True},
    )
    monkeypatch.setattr(baremetal, "pxe_traffic_seen", lambda *a, **k: True)
    monkeypatch.setattr(
        baremetal, "iso_boot", lambda *a, **k: calls.append("iso") or {"ok": True}
    )
    node = SimpleNamespace(
        name="n1", pxe_mac="aa:bb:cc:dd:ee:01", expected_ip="10.0.0.9"
    )
    result = baremetal.boot_for_talos(
        None, SimpleNamespace(), node, boot="auto", dry_run=False, log=lambda *_: None
    )
    assert calls == ["pxe"]
    assert result.get("ok") is True


def test_boot_for_talos_pxe_mode_skips_iso(monkeypatch):
    from types import SimpleNamespace

    from app.services import baremetal

    calls = []
    monkeypatch.setattr(
        baremetal,
        "pxe_boot",
        lambda *a, **k: calls.append("pxe") or {"ok": True},
    )
    monkeypatch.setattr(baremetal, "pxe_traffic_seen", lambda *a, **k: False)
    monkeypatch.setattr(
        baremetal, "iso_boot", lambda *a, **k: calls.append("iso") or {"ok": True}
    )
    node = SimpleNamespace(name="n1")
    baremetal.boot_for_talos(
        None, SimpleNamespace(), node, boot="pxe", dry_run=False, log=lambda *_: None
    )
    assert calls == ["pxe"]


def test_pxe_traffic_seen_ignores_stale_lease(monkeypatch):
    from types import SimpleNamespace

    from app.services import baremetal

    now = 1_800_000_000.0
    monkeypatch.setattr(
        "app.services.pxe_runtime.get_manager",
        lambda: SimpleNamespace(
            status=lambda: {
                "runtimes": {
                    "eno1": {
                        "leases": [
                            {
                                "mac": "aa:bb:cc:dd:ee:01",
                                "ip": "10.0.0.9",
                                "last_seen": now - 3600,
                            }
                        ],
                        "downloads": [],
                    }
                }
            }
        ),
    )
    node = SimpleNamespace(pxe_mac="aa:bb:cc:dd:ee:01", expected_ip="10.0.0.9")
    assert baremetal.pxe_traffic_seen(node, now) is False
    monkeypatch.setattr(
        "app.services.pxe_runtime.get_manager",
        lambda: SimpleNamespace(
            status=lambda: {
                "runtimes": {
                    "eno1": {
                        "leases": [
                            {
                                "mac": "aa:bb:cc:dd:ee:01",
                                "ip": "10.0.0.9",
                                "last_seen": now,
                            }
                        ],
                        "downloads": [],
                    }
                }
            }
        ),
    )
    assert baremetal.pxe_traffic_seen(node, now - 10) is True


def test_iso_boot_publishes_kind_iso(monkeypatch):
    from types import SimpleNamespace

    from app.services import baremetal

    seen = []
    monkeypatch.setattr(
        "app.services.events.publish_sync",
        lambda topic, payload: seen.append((topic, payload)),
    )
    monkeypatch.setattr(
        baremetal,
        "iso_image_url",
        lambda *a, **k: "http://10.0.0.1:8090/pxe-media/metal-amd64.iso",
    )
    monkeypatch.setattr(baremetal, "decrypt_secret", lambda *a, **k: "pw")
    monkeypatch.setattr(
        "app.services.redfish.insert_virtual_media", lambda *a, **k: "/vm/1"
    )
    monkeypatch.setattr(baremetal, "_cold_cycle", lambda *a, **k: None)
    # Mock envconfig.get_current to return empty config
    monkeypatch.setattr(
        "app.services.envconfig.get_current", lambda db, env: ({}, None)
    )
    env = SimpleNamespace(id="env-1")
    node = SimpleNamespace(
        id="n",
        name="ctrl1",
        bmc_host="10.0.0.2",
        bmc_username="admin",
        bmc_password_encrypted="enc",
        state="registered",
    )
    db = SimpleNamespace(add=lambda *_: None, flush=lambda: None)
    result = baremetal.iso_boot(db, env, node, dry_run=False, log=lambda *_: None)
    assert result.get("ok") is True
    metals = [p for _, p in seen if p.get("type") == "metal"]
    assert metals
    assert metals[0]["kind"] == "iso"
    assert metals[0]["host"] == "ctrl1"
    assert metals[0]["image_url"] == "http://10.0.0.1:8090/pxe-media/metal-amd64.iso"
    assert "inserting virtual CD" in metals[0]["message"]


def test_host_boot_state_old_os_vs_maintenance(monkeypatch):
    from app.services import baremetal

    monkeypatch.setattr(baremetal, "talos_api_ready", lambda ip, log=None: True)
    monkeypatch.setattr(baremetal, "k8s_ready_for_ip", lambda ip, kube: True)
    assert baremetal.host_boot_state("10.200.0.41", "/kube") == "old-os"
    monkeypatch.setattr(baremetal, "k8s_ready_for_ip", lambda ip, kube: False)
    assert baremetal.host_boot_state("10.200.0.41", "/kube") == "maintenance"
    monkeypatch.setattr(baremetal, "k8s_ready_for_ip", lambda ip, kube: None)
    assert baremetal.host_boot_state("10.200.0.41", None) == "maintenance"
    assert (
        baremetal.host_boot_state(
            "10.200.0.41", "/kube", was_ready={"10.200.0.41"}, saw_down=False
        )
        == "old-os"
    )
    assert (
        baremetal.host_boot_state(
            "10.200.0.41", "/kube", was_ready={"10.200.0.41"}, saw_down=True
        )
        == "maintenance"
    )
    monkeypatch.setattr(baremetal, "talos_api_ready", lambda ip, log=None: False)
    assert baremetal.host_boot_state("10.200.0.41", "/kube") == "down"


def test_k8s_ready_for_ip_matches_internal_ip(monkeypatch):
    from app.services import baremetal

    class _Fake:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

        def list_nodes(self):
            return [
                {"internal_ip": "10.200.0.41", "status": "Ready"},
                {"internal_ip": "10.200.0.42", "status": "NotReady"},
            ]

    monkeypatch.setattr("app.services.k8sclient.K8sClient", lambda path: _Fake())
    assert baremetal.k8s_ready_for_ip("10.200.0.41", "/kube") is True
    assert baremetal.k8s_ready_for_ip("10.200.0.42", "/kube") is False
    assert baremetal.k8s_ready_for_ip("10.200.0.99", "/kube") is None
    assert baremetal.k8s_ready_for_ip("10.200.0.41", "") is None


def test_ops_greenfield_queues(client, admin_headers, monkeypatch):
    env = _create_env(client, admin_headers)
    _put_doc(client, admin_headers, env["id"], DOC)
    resp = client.post(
        f"/api/v1/environments/{env['id']}/jobs",
        headers=admin_headers,
        json={
            "operation": "genestack.greenfield",
            "params": {"skip_push": True, "boot": "auto"},
        },
    )
    assert resp.status_code == 201, resp.text
    job = resp.json()
    assert job["operation"] == "genestack.greenfield"
    assert job["status"] in {"queued", "running", "success", "failed"}
