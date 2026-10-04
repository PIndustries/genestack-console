"""Ubuntu autoinstall, MicroK8s dry-run, and Kubespray adopt."""

from __future__ import annotations

from types import SimpleNamespace

from app.modules import clear_module_cache, handler_map
from app.modules.order import CATALOG_ORDER
from app.services.bootselect import (
    render_commission_ipxe,
    render_disk_ipxe,
    render_profile_ipxe,
    render_talos_ipxe,
)
from app.services.catalog import get_operation_catalog

_RUN_ARGS = dict(
    handler="",
    op=None,
    job=None,
    env=None,
    log=lambda *_a, **_k: None,
    ctx=None,
    params={},
    deadline=None,
    check_cancel=None,
    dry=True,
    timeout=1,
    gs_root="",
    ans_root="",
    extra_env={},
    ssh_target=None,
    remote_env={},
    executor=None,
    agent_env_id=None,
)

_BASE = "http://10.10.0.1:8080"


def test_load_modules_sees_host_operations():
    clear_module_cache()
    ids = [op.id for op in get_operation_catalog()]
    assert ids[-4:] == [
        "hosts.ubuntu.prepare",
        "hosts.ubuntu.bringup",
        "hosts.microk8s.install",
        "hosts.kubespray.adopt",
    ]
    assert ids == list(CATALOG_ORDER)
    hmap = handler_map()
    assert "hosts_ubuntu_prepare" in hmap
    assert "hosts_ubuntu_bringup" in hmap
    assert "hosts_microk8s_install" in hmap
    assert "hosts_kubespray_adopt" in hmap


def test_prepare_dry_writes_user_data_and_ubuntu_ipxe(tmp_path, monkeypatch):
    from app.modules.hosts import ubuntu as ubuntu_mod

    monkeypatch.setattr(ubuntu_mod, "pxe_data_dir", lambda _runner: tmp_path)
    key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITestKey operator@lab"
    result = ubuntu_mod.run(
        None,
        **{
            **_RUN_ARGS,
            "handler": "hosts_ubuntu_prepare",
            "dry": True,
            "params": {"hostname": "Lab1", "ssh_key": key},
        },
    )
    assert result["ok"] is True
    assert result["dry_run"] is True
    assert result["ssh_used"] is False
    assert "ssh was not used" in result["message"].lower()
    user_path = tmp_path / "pxe" / "ubuntu" / "lab1" / "user-data"
    meta_path = tmp_path / "pxe" / "ubuntu" / "lab1" / "meta-data"
    assert result["user_data"] == str(user_path)
    text = user_path.read_text(encoding="utf-8")
    assert "autoinstall" in text
    assert "microk8s" not in text
    assert key in text
    assert "openstack" not in text.lower() or "does not install OpenStack" in text
    assert meta_path.is_file()
    assert "lab1" in meta_path.read_text(encoding="utf-8")
    assert (tmp_path / "pxe" / "ubuntu" / "user-data").read_text(encoding="utf-8") == text

    assert render_profile_ipxe("disk", _BASE, "tok") == render_disk_ipxe()
    assert render_profile_ipxe("commission", _BASE, "tok") == render_commission_ipxe(
        _BASE, "tok"
    )
    assert render_profile_ipxe("talos", _BASE, "tok") == render_talos_ipxe(_BASE)
    ubuntu = render_profile_ipxe("ubuntu", _BASE, "tok", hostname="lab1")
    assert "ubuntu" in ubuntu.lower()
    assert "autoinstall" in ubuntu
    assert f"{_BASE}/ubuntu/lab1/" in ubuntu
    other = render_profile_ipxe("ubuntu", _BASE, "tok", hostname="lab2")
    assert f"{_BASE}/ubuntu/lab2/" in other
    assert f"{_BASE}/ubuntu/lab1/" not in other


def test_bringup_action_picks_one_step():
    from app.modules.hosts.ubuntu import bringup_action

    assert bringup_action(port_open=True, talos_api=False, leave=False) == "already"
    assert bringup_action(port_open=False, talos_api=False, leave=False) == "install"
    assert bringup_action(port_open=True, talos_api=True, leave=False) == "leave"
    assert bringup_action(port_open=True, talos_api=True, leave=True) == "install"


def test_bringup_dry_does_not_boot(monkeypatch):
    from app.modules.hosts import ubuntu as ubuntu_mod
    from app.services import envconfig as envconfig_service

    monkeypatch.setattr(
        envconfig_service,
        "get_current",
        lambda _db, _env: ({"servers": {"n1": {"private_ip": "10.1.1.8"}}}, None),
    )

    class _Db:
        def scalar(self, *_a, **_k):
            return SimpleNamespace(expected_ip="10.1.1.8", name="n1")

    def boom(*_a, **_k):
        raise AssertionError("set_next_boot was called")

    monkeypatch.setattr("app.services.baremetal.set_next_boot", boom)
    monkeypatch.setattr("app.services.baremetal.talos_api_ready", lambda *_a, **_k: False)
    monkeypatch.setattr(ubuntu_mod, "_port_open", lambda *_a, **_k: False)
    result = ubuntu_mod.run(
        SimpleNamespace(db=_Db(), settings=None),
        **{
            **_RUN_ARGS,
            "handler": "hosts_ubuntu_bringup",
            "dry": True,
            "env": SimpleNamespace(id="env"),
            "params": {"hostnames": ["n1"]},
        },
    )
    assert result["ok"] is True
    assert result["rebooted"] is False
    assert result["hosts"][0]["action"] == "install"
    assert result["hosts"][0]["dry_run"] is True


def test_microk8s_dry_does_not_ssh(monkeypatch):
    from app.modules.hosts import microk8s

    def boom(*_a, **_k):
        raise AssertionError("ssh was called")

    monkeypatch.setattr(microk8s, "_run_ssh", boom)
    monkeypatch.setattr("subprocess.run", boom)
    result = microk8s.run(
        None,
        **{
            **_RUN_ARGS,
            "handler": "hosts_microk8s_install",
            "dry": True,
            "params": {"host": "10.1.1.8", "ssh_user": "ubuntu"},
        },
    )
    assert result["ok"] is True
    assert result["dry_run"] is True
    assert result["ssh_used"] is False
    joined = "\n".join(result["commands"])
    assert "ssh ubuntu@10.1.1.8 sudo snap install microk8s --classic" in joined
    assert "ssh ubuntu@10.1.1.8 sudo microk8s status --wait-ready" in joined


def test_adopt_dry_does_not_write_kubeconfig(monkeypatch):
    from app.modules.hosts import kubespray

    def boom(*_a, **_k):
        raise AssertionError("encrypt was called")

    monkeypatch.setattr("app.services.crypto.encrypt_secret", boom)
    env = SimpleNamespace(kubeconfig_data="keep")
    secret = "apiVersion: v1\nkind: Config\nclusters: []\n"
    result = kubespray.run(
        None,
        **{
            **_RUN_ARGS,
            "handler": "hosts_kubespray_adopt",
            "dry": True,
            "env": env,
            "params": {"kubeconfig": secret},
        },
    )
    assert result["ok"] is True
    assert result["dry_run"] is True
    assert result["wrote_kubeconfig"] is False
    assert env.kubeconfig_data == "keep"
    assert secret not in result["message"]


def test_adopt_live_encrypts_kubeconfig(monkeypatch):
    from app.modules.hosts import kubespray

    captured: dict[str, str] = {}

    def fake_encrypt(plain, settings=None):
        captured["plain"] = plain
        return "fernet:stored"

    monkeypatch.setattr("app.services.crypto.encrypt_secret", fake_encrypt)
    env = SimpleNamespace(kubeconfig_data=None, id="env-1")
    secret = "apiVersion: v1\nkind: Config\nusers: []\n"
    logs: list[str] = []
    result = kubespray.run(
        SimpleNamespace(db=None, settings=None),
        **{
            **_RUN_ARGS,
            "handler": "hosts_kubespray_adopt",
            "dry": False,
            "env": env,
            "params": {"kubeconfig": secret},
            "log": logs.append,
        },
    )
    assert result["ok"] is True
    assert result["dry_run"] is False
    assert result["wrote_kubeconfig"] is True
    assert env.kubeconfig_data == "fernet:stored"
    assert captured["plain"] == secret.strip()
    assert all(secret not in line for line in logs)
    assert "kubectl" in result["message"]


def test_ubuntu_next_boot_writes_that_machines_seed(tmp_path, monkeypatch):
    from unittest.mock import MagicMock

    from app.services import baremetal

    node = SimpleNamespace(
        id="node-1",
        name="lab1",
        pxe_mac="aa:bb:cc:dd:ee:01",
        next_boot="disk",
        boot_stage="new",
        wiped_at=None,
    )
    key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITestKey operator@lab"
    env = SimpleNamespace(id="env-1", ssh_public_key=key)
    settings = SimpleNamespace(data_dir=str(tmp_path))
    monkeypatch.setattr(baremetal, "_prepare_pxe", lambda *a, **k: {"ok": True})

    def powered(*_a, **_k):
        raise AssertionError("powered")

    monkeypatch.setattr(baremetal, "pxe_boot", powered)
    result = baremetal.set_next_boot(
        MagicMock(),
        env,
        node,
        "ubuntu",
        boot_now=False,
        dry_run=False,
        log=lambda *_a, **_k: None,
        settings=settings,
    )
    assert result["ok"] is True
    assert node.next_boot == "ubuntu"
    assert node.boot_stage == "ubuntu"
    text = (tmp_path / "pxe" / "ubuntu" / "lab1" / "user-data").read_text(encoding="utf-8")
    assert key in text
    assert "lab2" not in text

    missing_key = baremetal.set_next_boot(
        MagicMock(),
        SimpleNamespace(id="env-1", ssh_public_key=""),
        node,
        "ubuntu",
        boot_now=False,
        dry_run=False,
        log=lambda *_a, **_k: None,
        settings=settings,
    )
    assert missing_key["ok"] is False
    assert "SSH public key" in missing_key["error"]

    no_mac = SimpleNamespace(
        id="node-2",
        name="lab2",
        pxe_mac="",
        next_boot="disk",
        boot_stage="new",
    )
    missing_mac = baremetal.set_next_boot(
        MagicMock(),
        env,
        no_mac,
        "ubuntu",
        boot_now=False,
        dry_run=False,
        log=lambda *_a, **_k: None,
        settings=settings,
    )
    assert missing_mac["ok"] is False
    assert "PXE MAC" in missing_mac["error"]
    assert no_mac.next_boot == "disk"


def test_set_next_boot_accepts_ubuntu(monkeypatch):
    from app.services import baremetal

    def powered(*_a, **_k):
        raise AssertionError("powered")

    monkeypatch.setattr(baremetal, "pxe_boot", powered)
    monkeypatch.setattr(baremetal, "power_action", powered)
    node = SimpleNamespace(name="lab1")
    chosen = baremetal.set_next_boot(
        None,
        SimpleNamespace(),
        node,
        "ubuntu",
        boot_now=True,
        dry_run=True,
        log=lambda *_a, **_k: None,
    )
    assert chosen["ok"] is True
    assert chosen["dry_run"] is True
    assert chosen["next_boot"] == "ubuntu"
    refused = baremetal.set_next_boot(
        None,
        SimpleNamespace(),
        node,
        "nope",
        boot_now=False,
        dry_run=True,
        log=lambda *_a, **_k: None,
    )
    assert refused["ok"] is False
    assert "ubuntu" in refused["error"]
