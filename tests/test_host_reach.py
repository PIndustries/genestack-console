"""Inventory reach checks stay read-only and never echo a secret."""

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
