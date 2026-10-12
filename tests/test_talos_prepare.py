"""Talos nodes get Longhorn's iSCSI tools without a per-node annotation."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from app.services import deploy as deploy_service
from app.services import talos_prepare
from app.services.envcontext import EnvContext
from app.services.service_registry import PIPELINE_STAGES
from app.services.talos import DEFAULT_TALOS_INSTALL_IMAGE
from app.services.talos_prepare import TUNNEL_ANNOTATION

STAGE_IDS = [stage["id"] for stage in PIPELINE_STAGES]


def _completed(code: int, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(
        args=[], returncode=code, stdout=stdout, stderr=stderr
    )


def _daemonset(iface: str | None) -> str:
    args = [] if iface is None else [f"--iface={iface}"]
    body = {
        "spec": {
            "template": {
                "spec": {"containers": [{"name": "cni", "args": args}]}
            }
        }
    }
    return json.dumps(body)


def _nodes(*names: str) -> str:
    return json.dumps(
        {
            "items": [
                {
                    "metadata": {
                        "name": name,
                        "annotations": {TUNNEL_ANNOTATION: "eth0,ens160"},
                    }
                }
                for name in names
            ]
        }
    )


def _doc() -> dict:
    return {
        "provider": "talos",
        "talos": {"install_image": DEFAULT_TALOS_INSTALL_IMAGE},
        "servers": {
            "storage-1": {"ip": "10.0.0.41", "roles": ["storage-ceph"]},
            "cp-2": {"ip": "10.0.0.12", "roles": ["k8s_control_plane", "etcd"]},
            "worker-1": {"ip": "10.0.0.51", "roles": ["worker"]},
            "cp-1": {"ip": "10.0.0.11", "roles": ["k8s_control_plane"]},
        },
    }


def test_longhorn_targets_are_control_planes_then_storage():
    rows = talos_prepare.longhorn_targets(_doc())
    assert [row["name"] for row in rows] == ["cp-1", "cp-2", "storage-1"]
    assert rows[0]["kind"] == "control"
    assert rows[-1]["kind"] == "storage"


def test_prepare_script_keeps_that_order_and_skips_workers(tmp_path):
    text = talos_prepare.render_prepare_script(tmp_path, _doc())
    assert text is not None
    assert text.index("--node cp-1=10.0.0.11") < text.index("--node cp-2=10.0.0.12")
    assert text.index("--node cp-2=10.0.0.12") < text.index(
        "--node storage-1=10.0.0.41"
    )
    assert "worker-1" not in text
    path = talos_prepare.install_prepare_script(tmp_path, _doc())
    assert path is not None
    assert path.stat().st_mode & 0o777 == 0o700


def test_prepare_script_uses_the_installed_binary_when_frozen(tmp_path, monkeypatch):
    prefix = tmp_path / "opt" / "genestack-console"
    monkeypatch.setattr("app.paths.frozen", lambda: True)
    monkeypatch.setattr("app.paths.host_prefix", lambda: prefix)
    text = talos_prepare.render_prepare_script(tmp_path, _doc())
    assert text is not None
    assert f"{prefix}/bin/genestack-console talos-prepare" in text
    assert "-m app.services.talos_prepare" not in text
    assert "/tmp/onefile_" not in text


def test_prepare_script_is_absent_without_the_longhorn_schematic(tmp_path):
    doc = _doc()
    doc["talos"]["install_image"] = "factory.talos.dev/installer/deadbeef:v1.14.2"
    assert talos_prepare.render_prepare_script(tmp_path, doc) is None


def test_pinned_iface_wins_over_an_empty_one():
    manifest = {
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {"name": "cni", "args": ["--iface="]},
                        {"name": "other", "args": ["--iface=eth0"]},
                    ]
                }
            }
        }
    }
    assert talos_prepare.daemonset_iface(manifest) == "eth0"
    assert talos_prepare.should_clear_tunnel_annotation("eth0") is False
    assert talos_prepare.should_clear_tunnel_annotation("") is True
    assert talos_prepare.should_clear_tunnel_annotation(None) is False


def test_empty_iface_drops_the_annotation_and_skips_a_node_that_has_iscsi(tmp_path):
    talosconfig = tmp_path / "talosconfig"
    talosconfig.write_text("context: test\n", encoding="utf-8")
    calls: list[list[str]] = []

    def run(argv, timeout):
        calls.append(list(argv))
        if argv[0] == "kubectl" and "daemonset" in argv:
            return _completed(0, _daemonset(""))
        if argv[0] == "kubectl" and argv[1:3] == ["get", "nodes"]:
            return _completed(0, _nodes("cp-1"))
        if argv[0] == "kubectl":
            return _completed(0, "")
        if "services" in argv:
            return _completed(0, "10.0.0.11 ext-iscsid Running Running\n")
        raise AssertionError(argv)

    result = talos_prepare.prepare_before_longhorn(
        targets=[{"name": "cp-1", "ip": "10.0.0.11", "kind": "control"}],
        image=DEFAULT_TALOS_INSTALL_IMAGE,
        talosconfig=str(talosconfig),
        log=lambda _line: None,
        run=run,
    )
    assert result["ok"] is True
    assert result["cleared"] == ["cp-1"]
    assert result["skipped"] == ["cp-1"]
    assert result["upgraded"] == []
    flat = [" ".join(argv) for argv in calls]
    assert any("annotate node cp-1" in text and TUNNEL_ANNOTATION in text for text in flat)
    assert any("rollout restart" in text for text in flat)
    assert not any("upgrade" in text for text in flat)


def test_named_iface_keeps_the_annotation(tmp_path):
    calls: list[list[str]] = []

    def run(argv, timeout):
        calls.append(list(argv))
        if argv[0] == "kubectl" and "daemonset" in argv:
            return _completed(0, _daemonset("eth0"))
        if "services" in argv:
            return _completed(0, "ext-iscsid Running Running\n")
        raise AssertionError(argv)

    talosconfig = tmp_path / "talosconfig"
    talosconfig.write_text("context: test\n", encoding="utf-8")
    result = talos_prepare.prepare_before_longhorn(
        targets=[{"name": "cp-1", "ip": "10.0.0.11", "kind": "control"}],
        image=DEFAULT_TALOS_INSTALL_IMAGE,
        talosconfig=str(talosconfig),
        log=lambda _line: None,
        run=run,
    )
    assert result["ok"] is True
    assert result["cleared"] == []
    assert not any("annotate" in " ".join(argv) for argv in calls)


def test_control_planes_upgrade_one_at_a_time(tmp_path):
    talosconfig = tmp_path / "talosconfig"
    talosconfig.write_text("context: test\n", encoding="utf-8")
    upgraded: set[str] = set()
    ready: set[str] = set()
    calls: list[list[str]] = []

    def run(argv, timeout):
        calls.append(list(argv))
        if argv[0] == "kubectl" and "daemonset" in argv:
            return _completed(0, _daemonset(""))
        if argv[0] == "kubectl" and argv[1:3] == ["get", "nodes"]:
            return _completed(0, _nodes())
        if argv[0] == "kubectl":
            return _completed(0, "")
        ip = argv[argv.index("--nodes") + 1]
        if "services" in argv:
            if ip == "10.0.0.41" or ip in upgraded:
                ready.add(ip)
                return _completed(0, f"{ip} ext-iscsid Running Running\n")
            return _completed(0, f"{ip} cri Running Running\n")
        if "upgrade" in argv:
            if ip == "10.0.0.12":
                assert "10.0.0.11" in ready
            upgraded.add(ip)
            return _completed(0, "requested")
        raise AssertionError(argv)

    result = talos_prepare.prepare_before_longhorn(
        targets=talos_prepare.longhorn_targets(_doc()),
        image=DEFAULT_TALOS_INSTALL_IMAGE,
        talosconfig=str(talosconfig),
        log=lambda _line: None,
        run=run,
    )
    assert result["ok"] is True
    assert result["upgraded"] == ["cp-1", "cp-2"]
    assert result["skipped"] == ["storage-1"]
    upgrades = [argv for argv in calls if "upgrade" in argv]
    assert [argv[argv.index("--nodes") + 1] for argv in upgrades] == [
        "10.0.0.11",
        "10.0.0.12",
    ]


def test_upgrade_timeout_still_waits_for_iscsi(tmp_path):
    talosconfig = tmp_path / "talosconfig"
    talosconfig.write_text("context: test\n", encoding="utf-8")
    seen = {"services": 0}

    def run(argv, timeout):
        if argv[0] == "kubectl" and "daemonset" in argv:
            return _completed(0, _daemonset(None))
        if "services" in argv:
            seen["services"] += 1
            if seen["services"] == 1:
                return _completed(0, "cri Running Running\n")
            return _completed(0, "ext-iscsid Running Running\n")
        if "upgrade" in argv:
            raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout)
        raise AssertionError(argv)

    result = talos_prepare.prepare_before_longhorn(
        targets=[{"name": "cp-1", "ip": "10.0.0.11", "kind": "control"}],
        image=DEFAULT_TALOS_INSTALL_IMAGE,
        talosconfig=str(talosconfig),
        log=lambda _line: None,
        run=run,
    )
    assert result["ok"] is True
    assert result["upgraded"] == ["cp-1"]
    assert result["cleared"] == []


def test_missing_iscsi_fails_before_the_next_control_plane(tmp_path):
    talosconfig = tmp_path / "talosconfig"
    talosconfig.write_text("context: test\n", encoding="utf-8")
    clock = {"t": 0.0}
    calls: list[str] = []

    def run(argv, timeout):
        if argv[0] == "kubectl" and "daemonset" in argv:
            return _completed(0, _daemonset(None))
        if "services" in argv:
            calls.append(argv[argv.index("--nodes") + 1])
            return _completed(0, "cri Running Running\n")
        if "upgrade" in argv:
            return _completed(0, "requested")
        raise AssertionError(argv)

    result = talos_prepare.prepare_before_longhorn(
        targets=[
            {"name": "cp-1", "ip": "10.0.0.11", "kind": "control"},
            {"name": "cp-2", "ip": "10.0.0.12", "kind": "control"},
        ],
        image=DEFAULT_TALOS_INSTALL_IMAGE,
        talosconfig=str(talosconfig),
        log=lambda _line: None,
        run=run,
        now=lambda: clock["t"],
        sleep=lambda seconds: clock.__setitem__("t", clock["t"] + seconds),
        iscsi_wait_s=15,
        poll_s=10,
    )
    assert result["ok"] is False
    assert "cp-1" in result["error"]
    assert "10.0.0.12" not in calls


def test_local_talos_deploy_arms_the_prepare_script(tmp_path):
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    root = tmp_path / "genestack"
    ctx = EnvContext(
        environment=None,
        genestack_root=root,
        config_dir=cfg,
        dry_run=False,
        kubeconfig=None,
    )
    logs: list[str] = []
    env = deploy_service._arm_talos_longhorn_prepare(
        _doc(),
        ctx,
        {},
        logs.append,
        dry_run=False,
        local=True,
        start_index=0,
        until_id=None,
        stage_ids=STAGE_IDS,
    )
    script = Path(env["GENESTACK_TALOS_PREPARE"])
    assert script.is_file()
    assert "worker-1" not in script.read_text(encoding="utf-8")
    assert any("ext-iscsid" in line for line in logs)

    dry = deploy_service._arm_talos_longhorn_prepare(
        _doc(),
        ctx,
        {},
        logs.append,
        dry_run=True,
        local=True,
        start_index=0,
        until_id=None,
        stage_ids=STAGE_IDS,
    )
    assert dry == {}
    later = STAGE_IDS.index("infrastructure") + 1
    skipped = deploy_service._arm_talos_longhorn_prepare(
        _doc(),
        ctx,
        {},
        logs.append,
        dry_run=False,
        local=True,
        start_index=later,
        until_id=None,
        stage_ids=STAGE_IDS,
    )
    assert skipped == {}
