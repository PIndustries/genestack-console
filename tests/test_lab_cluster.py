"""The console lab is a job. These tests do not start qemu."""

from __future__ import annotations

import os
import subprocess
import uuid
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from sqlalchemy import select

from app.db import SessionLocal
from app.models import EnvConfigVersion, Environment
from app.services import lab_launch
from app.services.catalog import get_operation
from app.services.envconfig import get_current
from app.services.inventory import build_inventory_from_environment
from app.services.lab_cluster import (
    LAB_DISK,
    LAB_MEM_MB,
    QCOW_MAGIC,
    cidata_iso,
    guest_paths,
    linux_argv,
    nodes_for,
    parse_count,
    qemu_img_argv,
    refusal,
    room_error,
    user_data,
)


def _submit(client, headers, params):
    response = client.post(
        "/api/v1/jobs",
        headers=headers,
        json={"operation": "hostvm.lab", "params": params, "run_sync": True},
    )
    assert response.status_code in (200, 201, 202), response.text
    return response.json()


def _env(name="genestack-lab"):
    env = MagicMock()
    env.id = "lab-env"
    env.name = name
    env.region = "local"
    env.tier = "dev"
    env.deployer_ssh_host = None
    env.deployer_ssh_user = None
    env.genestack_path = "/opt/genestack"
    env.kubeconfig_path = None
    env.ssh_public_key = "ssh-ed25519 AAAA lab"
    env.metadata_json = {"lab": True}
    return env


def _patch_host(monkeypatch, root: Path):
    started: list[list[str]] = []
    monkeypatch.setattr(lab_launch, "hypervisor_root", lambda settings: root)
    monkeypatch.setattr(lab_launch, "kvm_present", lambda: True)
    monkeypatch.setattr(lab_launch, "qemu_bin", lambda: "qemu-system-x86_64")
    monkeypatch.setattr(lab_launch, "qemu_img_bin", lambda: "qemu-img")
    monkeypatch.setattr(lab_launch, "memory_available_mb", lambda: 200_000)
    monkeypatch.setattr(lab_launch, "disk_free_bytes", lambda path: 80 * 1024**3)
    monkeypatch.setattr(lab_launch, "port_busy", lambda port: False)

    def fake_image(dest, url, log):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(QCOW_MAGIC + b"\0" * 16)
        log(f"[lab] cloud image cached: {dest}")

    def fake_overlay(base, disk):
        disk.parent.mkdir(parents=True, exist_ok=True)
        disk.write_bytes(b"overlay")

    def fake_start(argv, cwd):
        started.append(list(argv))
        pidfile = Path(argv[argv.index("-pidfile") + 1])
        pidfile.write_text(str(os.getpid()), encoding="utf-8")

    monkeypatch.setattr(lab_launch, "ensure_image", fake_image)
    monkeypatch.setattr(lab_launch, "create_overlay", fake_overlay)
    monkeypatch.setattr(lab_launch, "start_qemu", fake_start)
    return started


def _delete_env(name: str) -> None:
    with SessionLocal() as session:
        env = session.scalar(select(Environment).where(Environment.name == name))
        if env is None:
            return
        session.query(EnvConfigVersion).filter(
            EnvConfigVersion.environment_id == env.id
        ).delete()
        session.delete(env)
        session.commit()


def test_nodes_cover_three_four_and_five():
    three = nodes_for(3)
    assert [node.name for node in three] == ["lab-1", "lab-2", "lab-3"]
    assert three[0].roles == ("k8s_control_plane", "etcd", "control")
    assert three[1].roles == ("compute", "network")
    assert three[2].roles == ("storage",)
    assert [node.ssh_port for node in three] == [2222, 2223, 2224]
    assert three[0].cpus == 4
    assert three[0].mem_mb == LAB_MEM_MB
    assert three[0].disk == LAB_DISK
    assert nodes_for(4)[3].roles == ("worker",)
    assert nodes_for(5)[4].roles == ("compute",)
    assert parse_count(None) == 3
    with pytest.raises(ValueError):
        parse_count(2)
    with pytest.raises(ValueError):
        parse_count("six")


def test_room_and_kvm_refusal():
    assert room_error(3, 200_000, 80 * 1024**3) is None
    low = room_error(3, 1000, 80 * 1024**3)
    assert low is not None and "The guests were not started." in low
    small = room_error(3, 200_000, 1024)
    assert small is not None and "GiB free" in small
    assert "no /dev/kvm" in (refusal(
        3,
        mem_available_mb=200_000,
        disk_free_bytes=80 * 1024**3,
        kvm=False,
        qemu=True,
        qemu_img=True,
    ) or "")


def test_linux_argv_is_thin_kvm_and_pidfile_sits_on_the_root(tmp_path):
    node = nodes_for(3)[0]
    paths = guest_paths(tmp_path, node)
    argv = linux_argv(
        node,
        disk=paths["disk"],
        seed=paths["seed"],
        serial=paths["serial"],
        pidfile=paths["pidfile"],
    )
    assert argv[0] == "qemu-system-x86_64"
    assert "pc,accel=kvm" in argv
    assert any(
        f"hostfwd=tcp:127.0.0.1:{node.ssh_port}-:22" in arg for arg in argv
    )
    assert paths["pidfile"].parent == tmp_path
    assert str(paths["pidfile"]) in argv
    img = qemu_img_argv(tmp_path / "base.img", paths["disk"])
    assert "preallocation" not in img
    assert "-o" not in img
    assert img[-1] == "80G"


def test_user_data_has_the_public_key_only():
    node = nodes_for(3)[0]
    text = user_data(node, "ssh-ed25519 AAAA lab")
    assert "ssh-ed25519 AAAA lab" in text
    assert "PRIVATE" not in text
    assert "ubuntu" in text


def test_cidata_iso_carries_the_label_and_both_files():
    blob = cidata_iso(b"hello-user", b"hello-meta")
    assert blob[32768] == 1
    assert blob[32769:32774] == b"CD001"
    assert blob[32768 + 40 : 32768 + 46] == b"cidata"
    assert b"USER-DATA;1" in blob
    assert b"META-DATA;1" in blob
    assert b"hello-user" in blob
    assert b"hello-meta" in blob


def test_inventory_uses_ssh_port_as_ansible_port():
    servers = {
        "lab-1": {
            "ip": "127.0.0.1",
            "ssh_user": "ubuntu",
            "ssh_port": 2222,
            "source": "static",
            "roles": ["k8s_control_plane", "etcd", "control"],
        },
        "lab-2": {
            "ip": "127.0.0.1",
            "ssh_user": "ubuntu",
            "source": "static",
            "roles": ["compute", "network"],
        },
    }
    inventory = build_inventory_from_environment(
        _env(), servers=servers, include_deployer=False
    )
    first = inventory["all"]["hosts"]["lab-1"]
    assert first["ansible_host"] == "127.0.0.1"
    assert first["ansible_port"] == 2222
    assert first["ansible_user"] == "ubuntu"
    assert "ansible_port" not in inventory["all"]["hosts"]["lab-2"]
    children = inventory["all"]["children"]["k8s_cluster"]["children"]
    assert "lab-1" in children["kube_control_plane"]["hosts"]
    assert "lab-1" in children["openstack_control_plane"]["hosts"]


def test_catalog_registers_lab():
    operation = get_operation("hostvm.lab")
    assert operation is not None
    assert operation.mutating is True
    assert operation.required_role == "operator"
    assert operation.timeout_seconds == 3600
    assert {param.name for param in operation.params} == {"count", "confirm", "name"}
    assert all(not param.required for param in operation.params)


def test_hosts_page_offers_the_lab_job():
    page = Path("app/static/js/pages/hosts.js").read_text(encoding="utf-8")
    shell = Path("app/static/js/app.js").read_text(encoding="utf-8")
    assert 'operation: "hostvm.lab"' in page
    assert "confirm: true" in page
    assert "hosts.js?v=ls69" in shell


def test_lab_without_confirm_only_writes_the_plan(client, operator_headers):
    with SessionLocal() as session:
        before = session.scalar(
            select(Environment).where(Environment.name == "genestack-lab")
        )
    job = _submit(client, operator_headers, {"count": 3})
    assert job["status"] == "success", job
    assert job["dry_run"] is True
    assert "Plan only" in job["log_text"]
    assert "lab-1" in job["log_text"]
    assert "lab-3" in job["log_text"]
    with SessionLocal() as session:
        after = session.scalar(
            select(Environment).where(Environment.name == "genestack-lab")
        )
    assert (before.id if before else None) == (after.id if after else None)


def test_lab_confirm_records_the_environment(
    client, operator_headers, monkeypatch, tmp_path
):
    name = f"lab-{uuid.uuid4().hex[:8]}"
    root = tmp_path / "vms"
    root.mkdir()
    started = _patch_host(monkeypatch, root)
    try:
        job = _submit(
            client,
            operator_headers,
            {"count": 3, "confirm": True, "name": name},
        )
        assert job["status"] == "success", job.get("log_text")
        assert job["dry_run"] is False
        assert "Not a production workload" in job["log_text"]
        assert len(started) == 3
        assert any("hostfwd=tcp:127.0.0.1:2222-:22" in arg for arg in started[0])
        assert Path(started[0][started[0].index("-pidfile") + 1]).parent == root
        with SessionLocal() as session:
            env = session.scalar(select(Environment).where(Environment.name == name))
            assert env is not None
            assert env.dry_run is True
            assert env.tier == "dev"
            assert (env.metadata_json or {}).get("lab") is True
            assert env.ssh_public_key.startswith("ssh-")
            current = get_current(session, env)
            assert current is not None
            doc = current[0]
            assert doc["provider"] == "kubespray"
            server = doc["servers"]["lab-1"]
            assert server["ip"] == "127.0.0.1"
            assert server["ssh_port"] == 2222
            assert server["ssh_user"] == "ubuntu"
            assert "network" in doc["servers"]["lab-2"]["roles"]
            assert doc["servers"]["lab-3"]["roles"] == ["storage"]
            pubkey = env.ssh_public_key
        seed = (root / "lab-1" / "user-data").read_text(encoding="utf-8")
        assert pubkey in seed
        assert "PRIVATE" not in seed
        again = _submit(
            client,
            operator_headers,
            {"count": 3, "confirm": True, "name": name},
        )
        assert again["status"] == "success", again.get("log_text")
        assert len(started) == 3
        assert "already running" in again["log_text"]
    finally:
        _delete_env(name)


def test_lab_refuses_a_small_host(client, operator_headers, monkeypatch, tmp_path):
    name = f"lab-{uuid.uuid4().hex[:8]}"
    root = tmp_path / "vms"
    root.mkdir()
    started = _patch_host(monkeypatch, root)
    monkeypatch.setattr(lab_launch, "memory_available_mb", lambda: 1000)
    job = _submit(
        client, operator_headers, {"count": 3, "confirm": True, "name": name}
    )
    assert job["status"] == "failed", job
    assert "not started" in (job["error"] or "").lower()
    assert started == []
    with SessionLocal() as session:
        assert (
            session.scalar(select(Environment).where(Environment.name == name)) is None
        )


def test_lab_refuses_a_name_that_is_not_a_lab(
    client, operator_headers, monkeypatch, tmp_path
):
    name = f"taken-{uuid.uuid4().hex[:8]}"
    with SessionLocal() as session:
        session.add(
            Environment(name=name, tier="prod", dry_run=False, metadata_json={})
        )
        session.commit()
    root = tmp_path / "vms"
    root.mkdir()
    started = _patch_host(monkeypatch, root)
    try:
        job = _submit(
            client, operator_headers, {"count": 3, "confirm": True, "name": name}
        )
        assert job["status"] == "failed", job
        assert "not this lab" in (job["error"] or "")
        assert started == []
    finally:
        _delete_env(name)


def test_lab_requires_operator(client, viewer_headers):
    response = client.post(
        "/api/v1/jobs",
        headers=viewer_headers,
        json={"operation": "hostvm.lab", "params": {"count": 3}, "run_sync": True},
    )
    assert response.status_code == 403


def test_ensure_qemu_installs_the_same_packages_as_the_installer(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(lab_launch, "qemu_bin", lambda: None)
    monkeypatch.setattr(lab_launch, "qemu_img_bin", lambda: None)
    monkeypatch.setattr(
        lab_launch.shutil,
        "which",
        lambda name: "/usr/bin/apt-get" if name == "apt-get" else None,
    )

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(lab_launch.subprocess, "run", fake_run)
    lab_launch.ensure_qemu(lambda message: None)
    assert calls[0][:2] == ["/usr/bin/apt-get", "update"]
    assert calls[1] == [
        "/usr/bin/apt-get",
        "install",
        "-y",
        "--no-install-recommends",
        "qemu-system-x86",
        "qemu-utils",
    ]


def test_ensure_qemu_skips_when_the_binaries_exist(monkeypatch):
    monkeypatch.setattr(lab_launch, "qemu_bin", lambda: "/usr/bin/qemu-system-x86_64")
    monkeypatch.setattr(lab_launch, "qemu_img_bin", lambda: "/usr/bin/qemu-img")

    def boom(*args, **kwargs):
        raise AssertionError("apt should not run")

    monkeypatch.setattr(lab_launch.subprocess, "run", boom)
    lab_launch.ensure_qemu(lambda message: None)


def test_ensure_image_reuses_a_cached_qcow(tmp_path):
    dest = tmp_path / "noble.img"
    with dest.open("wb") as handle:
        handle.write(QCOW_MAGIC)
        handle.truncate(60_000_000)
    notes: list[str] = []
    lab_launch.ensure_image(dest, "http://127.0.0.1:1/missing", notes.append)
    assert any("cached" in note for note in notes)
