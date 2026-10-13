"""Inventory reach checks stay read-only and never echo a secret."""

from pathlib import Path
from types import SimpleNamespace

from app.services import host_reach


class _Sock:
    def close(self):
        return None


def test_closed_port_is_down(monkeypatch):
    def refused(*_a, **_k):
        raise ConnectionRefusedError("refused")

    monkeypatch.setattr(host_reach.socket, "create_connection", refused)
    row = host_reach.probe_address("192.0.2.10", "root", key_text=None, password=None)
    assert row["up"] is False
    assert row["connect"] is False
    assert row["authenticated"] is False
    assert row["running"] is False
    assert row["detail"] == "connection refused"


def test_open_port_and_key_login_is_running(monkeypatch):
    seen = []

    def run(argv, **_kwargs):
        seen.append(list(argv))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(host_reach.socket, "create_connection", lambda *_a, **_k: _Sock())
    monkeypatch.setattr(host_reach.subprocess, "run", run)
    monkeypatch.setattr(host_reach.shutil, "which", lambda name: "/usr/bin/ssh" if name == "ssh" else None)
    secret = "-----BEGIN OPENSSH PRIVATE KEY-----\nnot-a-real-key\n"
    row = host_reach.probe_address("192.0.2.11", "ubuntu", key_text=secret, password=None)
    assert row == {
        "hostname": "",
        "ip": "192.0.2.11",
        "up": True,
        "running": True,
        "connect": True,
        "authenticated": True,
        "detail": "",
    }
    assert seen and seen[0][0] == "/usr/bin/ssh"
    assert "true" in seen[0]
    assert secret not in " ".join(seen[0])
    assert "not-a-real-key" not in str(row)


def test_permission_denied_stays_unauthenticated(monkeypatch):
    def run(_argv, **_kwargs):
        return SimpleNamespace(returncode=255, stdout="", stderr="Permission denied (publickey).")

    monkeypatch.setattr(host_reach.socket, "create_connection", lambda *_a, **_k: _Sock())
    monkeypatch.setattr(host_reach.subprocess, "run", run)
    monkeypatch.setattr(host_reach.shutil, "which", lambda name: "/usr/bin/ssh" if name == "ssh" else None)
    row = host_reach.probe_address("192.0.2.12", "root", key_text="line\n", password=None)
    assert row["up"] is True
    assert row["connect"] is True
    assert row["authenticated"] is False
    assert row["running"] is False
    assert row["detail"] == "authentication failed"


def test_password_is_not_an_argument(monkeypatch):
    seen = {}

    def run(argv, **kwargs):
        seen["argv"] = list(argv)
        seen["env"] = kwargs.get("env") or {}
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(host_reach.socket, "create_connection", lambda *_a, **_k: _Sock())
    monkeypatch.setattr(host_reach.subprocess, "run", run)
    monkeypatch.setattr(
        host_reach.shutil,
        "which",
        lambda name: "/usr/bin/ssh" if name == "ssh" else "/usr/bin/sshpass" if name == "sshpass" else None,
    )
    row = host_reach.probe_address("192.0.2.13", "ubuntu", key_text=None, password="s3cret-value")
    assert row["authenticated"] is True
    assert "s3cret-value" not in seen["argv"]
    assert seen["env"].get("SSHPASS") == "s3cret-value"
    assert "s3cret-value" not in str(row)


def test_ssh_option_in_the_user_is_refused(monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("must not connect")

    monkeypatch.setattr(host_reach.socket, "create_connection", boom)
    row = host_reach.probe_address("192.0.2.14", "-oProxyCommand=x", key_text="k", password=None)
    assert row["authenticated"] is False
    assert row["detail"] == "address refused"


def test_talos_port_matches_the_api():
    from app.services.baremetal import TALOS_API_PORT

    assert host_reach._TALOS_API_PORT == TALOS_API_PORT == 50000


def test_inventory_os_matches_the_machines_page():
    assert host_reach.inventory_os({}, boot_stage="", next_boot="", provider="") == "talos"
    assert host_reach.inventory_os({}, boot_stage="", next_boot="", provider="talos") == "talos"
    assert (
        host_reach.inventory_os({}, boot_stage="installed", next_boot="disk", provider="talos")
        == "talos"
    )
    assert host_reach.inventory_os({}, boot_stage="", next_boot="", provider="kubespray") == "ubuntu"
    assert host_reach.inventory_os({}, boot_stage="ubuntu", next_boot="", provider="talos") == "ubuntu"
    assert host_reach.inventory_os({}, boot_stage="", next_boot="ubuntu", provider="talos") == "ubuntu"
    assert (
        host_reach.inventory_os(
            {"adopt": "kubespray"}, boot_stage="talos", next_boot="talos", provider="talos"
        )
        == "ubuntu"
    )
    assert host_reach.inventory_os({}, boot_stage="talos", next_boot="disk", provider="kubespray") == "talos"


def test_open_talos_api_is_up_without_ssh(monkeypatch):
    ports = []

    def connect(addr, *_a, **_k):
        ports.append(addr[1])
        return _Sock()

    def boom(*_a, **_k):
        raise AssertionError("ssh must not run")

    monkeypatch.setattr(host_reach.socket, "create_connection", connect)
    monkeypatch.setattr(host_reach.subprocess, "run", boom)
    row = host_reach.probe_talos("192.0.2.20")
    assert ports == [50000]
    assert row["up"] is True
    assert row["connect"] is True
    assert row["running"] is True
    assert row["authenticated"] is False
    assert row["detail"] == ""
    assert row["check"] == "talos"
    assert "connection refused" not in str(row)


def test_closed_talos_api_names_the_api(monkeypatch):
    def refused(addr, *_a, **_k):
        assert addr[1] == 50000
        raise ConnectionRefusedError("refused")

    monkeypatch.setattr(host_reach.socket, "create_connection", refused)
    row = host_reach.probe_talos("192.0.2.21")
    assert row["up"] is False
    assert row["connect"] is False
    assert row["detail"] == "Talos API refused"
    assert row["check"] == "talos"


def test_talos_environment_does_not_probe_ssh(monkeypatch):
    ports = []

    def closed(host, port, _timeout):
        ports.append((host, port))
        if port == 50000:
            return ""
        return "connection refused"

    monkeypatch.setattr(host_reach, "_tcp_closed", closed)
    monkeypatch.setattr(host_reach, "get_decrypted_private_key", lambda _env: None)
    monkeypatch.setattr(
        "app.services.envconfig.get_current",
        lambda _db, _env: (
            {
                "provider": "talos",
                "servers": {
                    "cp": {"private_ip": "192.0.2.31"},
                    "adopted": {"private_ip": "192.0.2.32", "adopt": "kubespray"},
                },
            },
            1,
        ),
    )

    class _Scalars:
        def all(self):
            return [SimpleNamespace(name="cp", boot_stage="talos", next_boot="disk")]

    class _Db:
        def scalars(self, _stmt):
            return _Scalars()

    env = SimpleNamespace(id="env-1", deployer_ssh_user="root")
    result = host_reach.probe_environment(_Db(), env)
    by = {row["hostname"]: row for row in result["hosts"]}
    assert ("192.0.2.31", 50000) in ports
    assert ("192.0.2.31", 22) not in ports
    assert by["cp"]["up"] is True
    assert by["cp"]["check"] == "talos"
    assert by["cp"]["detail"] == ""
    assert ("192.0.2.32", 22) in ports
    assert by["adopted"].get("check") != "talos"
    assert by["adopted"]["detail"] == "connection refused"


def test_machines_page_does_not_print_an_ssh_refusal_on_talos():
    text = Path("app/static/js/pages/environment_servers.js").read_text(encoding="utf-8")
    assert 'row.detail || "no SSH"' not in text
    assert 'statusPill("ok", "Talos")' in text
