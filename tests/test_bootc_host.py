"""bootc status parsing and the settings routes that upgrade or roll back."""

from __future__ import annotations

import subprocess

from app.services import bootc, updatecheck


def test_version_from_image_keeps_calver_tags_only():
    assert bootc.version_from_image(
        "localhost/genestack-console-appliance:2026.10.07.12"
    ) == "2026.10.07.12"
    assert bootc.version_from_image("quay.io/example/os:latest") == ""
    assert bootc.version_from_image("name:2026.10.07.12@sha256:abc") == "2026.10.07.12"


def test_parse_status_reads_booted_and_rollback():
    view = bootc.parse_status(
        {
            "status": {
                "booted": {
                    "image": {
                        "image": {"image": "localhost/genestack-console-appliance:2026.10.07.13"},
                        "version": "2026.10.07.13",
                        "imageDigest": "sha256:abc",
                    }
                },
                "rollback": {
                    "image": {
                        "image": {"image": "localhost/genestack-console-appliance:2026.10.07.12"},
                    }
                },
                "rollbackQueued": False,
                "readOnly": False,
            }
        }
    )
    assert view["bootc"] is True
    assert view["booted"]["version"] == "2026.10.07.13"
    assert view["rollback"]["version"] == "2026.10.07.12"
    assert view["staged"] is None


def test_release_binary_url_rejects_anything_except_a_version():
    assert updatecheck.release_binary_url("2026.10.07.13").endswith(
        "/download/v2026.10.07.13/genestack-console-linux-amd64"
    )
    assert updatecheck.release_binary_url("../secret") is None
    assert updatecheck.release_binary_url("2026.10.07.13;rm") is None
    assert updatecheck.release_binary_url("") is None


def test_watch_skips_the_program_file_when_bootc_is_installed(monkeypatch):
    monkeypatch.setattr(updatecheck, "bootc_installed", lambda: True, raising=False)
    # watch_once imports installed from the module. Patch that function.
    monkeypatch.setattr("app.services.bootc.installed", lambda: True)
    result = updatecheck.watch_once(
        settings=type("S", (), {"update_watch": True})(),
        info={"update_available": True, "latest": "2026.10.07.13"},
        installed=True,
    )
    assert result["skipped"] == "bootc"
    assert result["applied"] is False


def test_apply_action_refuses_unknown_commands(monkeypatch):
    monkeypatch.setattr(bootc, "bootc_path", lambda: "/usr/bin/bootc")
    called = {}

    def run(cmd, timeout):
        called["cmd"] = cmd
        called["timeout"] = timeout
        return subprocess.CompletedProcess(cmd, 0, stdout="queued", stderr="")

    monkeypatch.setattr(bootc, "_maybe_sudo", lambda cmd: cmd)
    result = bootc.apply_action("reboot", run=run)
    assert result["ok"] is False
    assert "cmd" not in called
    ok = bootc.apply_action("rollback", run=run)
    assert ok["ok"] is True
    assert called["cmd"] == ["/usr/bin/bootc", "rollback", "--apply"]


def test_host_route_and_bootc_post(client, viewer_headers, admin_headers, monkeypatch):
    monkeypatch.setattr(
        "app.routers.update.bootc.host_view",
        lambda: {"bootc": True, "booted": {"version": "2026.10.07.13"}},
    )
    denied = client.get("/api/v1/update/host")
    assert denied.status_code == 401
    seen = client.get("/api/v1/update/host", headers=viewer_headers)
    assert seen.status_code == 200
    assert seen.json()["booted"]["version"] == "2026.10.07.13"

    monkeypatch.setattr(
        "app.routers.update.bootc.apply_action",
        lambda action: {"ok": True, "applied": True, "action": action, "message": "rebooting"},
    )
    blocked = client.post("/api/v1/update/bootc", headers=viewer_headers, json={"action": "upgrade"})
    assert blocked.status_code == 403
    accepted = client.post(
        "/api/v1/update/bootc",
        headers=admin_headers,
        json={"action": "upgrade"},
    )
    assert accepted.status_code == 200
    assert accepted.json()["action"] == "upgrade"


def test_apply_route_passes_a_chosen_version(client, admin_headers, monkeypatch):
    seen = {}

    def apply_version(settings, version):
        seen["version"] = version
        return {"ok": True, "applied": False, "message": "already current"}

    monkeypatch.setattr("app.routers.update.updatecheck.apply_version", apply_version)
    monkeypatch.setattr(
        "app.routers.update.updatecheck.apply_binary",
        lambda settings: {"ok": True, "applied": False, "message": "channel"},
    )
    chosen = client.post(
        "/api/v1/update/apply",
        headers=admin_headers,
        json={"version": "2026.10.07.11"},
    )
    assert chosen.status_code == 200
    assert seen["version"] == "2026.10.07.11"
    plain = client.post("/api/v1/update/apply", headers=admin_headers, json={})
    assert plain.status_code == 200
    assert plain.json()["message"] == "channel"
