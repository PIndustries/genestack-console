"""Talos bootstrap tests — plan building and the genestack.talos.bootstrap op."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
import yaml

from app.db import SessionLocal
from app.models import Environment
from app.services import genestack_bridge as bridge
from app.services.crypto import decrypt_secret
from app.services.catalog import get_operation
from app.services.envconfig import ConfigValidationError, parse_document
from app.services.talos import (
    DEFAULT_TALOS_INSTALL_IMAGE,
    DEFAULT_VLAN_ID,
    build_talos_plan,
    etcd_patch_yaml,
    firewall_yaml,
    fleet_has_private_nic,
    network_patch_yaml,
    node_apply_ip,
    node_cluster_ip,
    node_vlan_patch_yaml,
    rewrite_kubeconfig_server,
    talos_pki_markers,
    talos_workdir_bootstrapped,
    vlan_id_from_doc,
)

DOC_TALOS = """\
provider: talos
talos:
  cluster_name: stagefurious
  install_disk: /dev/sdb
servers:
  cp1:
    ip: 10.0.0.11
    roles: [k8s_control_plane]
  worker1:
    ip: 10.0.0.21
    roles: [compute]
  worker2:
    ip: 10.0.0.22
    roles: [storage]
"""

DOC_TALOS_DEFAULTS = """\
provider: talos
servers:
  cp1:
    ip: 10.0.0.11
    roles: [k8s_control_plane]
  worker1:
    ip: 10.0.0.21
    roles: [compute]
"""

DOC_TALOS_NO_CP = """\
provider: talos
servers:
  worker1:
    ip: 10.0.0.21
    roles: [compute]
"""

DOC_TALOS_EMPTY_DISK = """\
provider: talos
talos:
  install_disk: ""
servers:
  cp1:
    ip: 10.0.0.11
    roles: [k8s_control_plane]
"""


def _env(tmp_path: Path, **kwargs) -> Environment:
    return Environment(
        name=f"env-talos-{uuid.uuid4().hex[:8]}",
        genestack_config_dir=str(tmp_path / "etc-genestack"),
        **kwargs,
    )


def _doc(yaml_text: str) -> dict:
    doc, _warnings = parse_document(yaml_text)
    return doc


# ---------------------------------------------------------------------------
# build_talos_plan
# ---------------------------------------------------------------------------


def test_plan_cp_worker_split_and_overrides(tmp_path):
    plan = build_talos_plan(_doc(DOC_TALOS), _env(tmp_path))
    assert plan["cluster_name"] == "stagefurious"
    assert plan["install_disk"] == "/dev/sdb"
    assert [n["hostname"] for n in plan["control_planes"]] == ["cp1"]
    assert [n["ip"] for n in plan["control_planes"]] == ["10.0.0.11"]
    assert [n["hostname"] for n in plan["workers"]] == ["worker1", "worker2"]
    assert [n["ip"] for n in plan["workers"]] == ["10.0.0.21", "10.0.0.22"]
    assert plan["workdir"] == tmp_path / "etc-genestack" / "talos"


def test_plan_command_sequence(tmp_path):
    plan = build_talos_plan(_doc(DOC_TALOS), _env(tmp_path))
    kubeconfig = tmp_path / "etc-genestack" / "inventory" / "artifacts" / "admin.conf"
    phases = [c["phase"] for c in plan["commands"]]
    assert phases[0] == "write-network-patch"
    assert phases[1] == "write-etcd-patch"
    assert "gen-config" in phases
    assert phases[phases.index("gen-config") + 1] == "endpoints"
    assert phases[-3:] == ["wait-controlplane", "bootstrap", "kubeconfig"]
    gen = next(c["argv"] for c in plan["commands"] if c["phase"] == "gen-config")
    assert gen[0] == "talosctl" or gen[0].endswith("/talosctl")
    assert gen[1:6] == [
        "gen",
        "config",
        "stagefurious",
        "https://10.0.0.11:6443",
        "--install-disk",
    ]
    assert "--config-patch" in gen
    assert "--config-patch-control-plane" in gen
    assert "--with-docs=false" in gen
    assert "--with-examples=false" in gen
    # Greenfield must not pass --force (not required; bootstrapped hard-fails).
    assert "--force" not in gen
    assert "--install-image" in gen
    assert DEFAULT_TALOS_INSTALL_IMAGE in gen
    apply_nodes = [
        c["argv"][c["argv"].index("--nodes") + 1]
        for c in plan["commands"]
        if c["phase"].startswith("apply-")
    ]
    assert apply_nodes == ["10.0.0.11", "10.0.0.21", "10.0.0.22"]
    kube = next(c["argv"] for c in plan["commands"] if c["phase"] == "kubeconfig")
    assert str(kubeconfig) in kube
    assert "10.0.0.11" in kube


def test_gen_config_greenfield_omits_force(tmp_path):
    """Empty talos workdir may generate config; --force is not required or passed."""
    env = _env(tmp_path)
    workdir = tmp_path / "etc-genestack" / "talos"
    assert not workdir.exists()
    assert talos_workdir_bootstrapped(workdir) is False
    assert talos_pki_markers(workdir) == []
    plan = build_talos_plan(_doc(DOC_TALOS), env)
    assert plan["workdir"] == workdir
    gen = next(c["argv"] for c in plan["commands"] if c["phase"] == "gen-config")
    assert gen[0] == "talosctl" or gen[0].endswith("/talosctl")
    assert gen[1:3] == ["gen", "config"]
    assert "--force" not in gen


def test_gen_config_refuses_existing_secrets_yaml(tmp_path):
    """Existing secrets.yaml means live PKI — hosts stage must not --force gen."""
    workdir = tmp_path / "etc-genestack" / "talos"
    workdir.mkdir(parents=True)
    (workdir / "secrets.yaml").write_text("cluster:\n  id: fake\n", encoding="utf-8")
    assert talos_workdir_bootstrapped(workdir) is True
    assert talos_pki_markers(workdir) == ["secrets.yaml"]
    with pytest.raises(ConfigValidationError, match="already looks bootstrapped"):
        build_talos_plan(_doc(DOC_TALOS), _env(tmp_path))
    with pytest.raises(ConfigValidationError, match="secrets.yaml"):
        build_talos_plan(_doc(DOC_TALOS), _env(tmp_path))
    with pytest.raises(ConfigValidationError, match="cannot overwrite live Talos PKI"):
        build_talos_plan(_doc(DOC_TALOS), _env(tmp_path))


def test_gen_config_refuses_existing_talosconfig(tmp_path):
    """Existing talosconfig alone is enough to refuse gen-config --force."""
    workdir = tmp_path / "etc-genestack" / "talos"
    workdir.mkdir(parents=True)
    (workdir / "talosconfig").write_text("context: fake\n", encoding="utf-8")
    assert talos_pki_markers(workdir) == ["talosconfig"]
    with pytest.raises(ConfigValidationError, match="talosconfig"):
        build_talos_plan(_doc(DOC_TALOS), _env(tmp_path))


def test_gen_config_refuses_both_pki_markers(tmp_path):
    workdir = tmp_path / "etc-genestack" / "talos"
    workdir.mkdir(parents=True)
    (workdir / "secrets.yaml").write_text("cluster:\n  id: fake\n", encoding="utf-8")
    (workdir / "talosconfig").write_text("context: fake\n", encoding="utf-8")
    assert talos_pki_markers(workdir) == ["secrets.yaml", "talosconfig"]
    with pytest.raises(ConfigValidationError, match="secrets.yaml, talosconfig"):
        build_talos_plan(_doc(DOC_TALOS), _env(tmp_path))


def test_resume_skips_gen_and_never_passes_force(tmp_path):
    """A saved identity is reused. gen config --force is still refused."""
    env = _env(tmp_path)
    workdir = tmp_path / "etc-genestack" / "talos"
    workdir.mkdir(parents=True)
    (workdir / "secrets.yaml").write_text("cluster:\n  id: fake\n", encoding="utf-8")
    (workdir / "talosconfig").write_text("context: fake\n", encoding="utf-8")
    (workdir / "controlplane.yaml").write_text("version: v1alpha1\n", encoding="utf-8")
    plan = build_talos_plan(_doc(DOC_TALOS), env, allow_existing_pki=True)
    assert not any(command["phase"] == "gen-config" for command in plan["commands"])
    apply = next(
        command["argv"]
        for command in plan["commands"]
        if command["phase"] == "apply-controlplane"
    )
    assert "--insecure" in apply
    assert "--force" not in apply


def test_resume_regenerates_machine_yaml_with_existing_secrets(tmp_path):
    env = _env(tmp_path)
    workdir = tmp_path / "etc-genestack" / "talos"
    workdir.mkdir(parents=True)
    (workdir / "secrets.yaml").write_text("cluster:\n  id: fake\n", encoding="utf-8")
    (workdir / "talosconfig").write_text("context: fake\n", encoding="utf-8")
    plan = build_talos_plan(_doc(DOC_TALOS), env, allow_existing_pki=True)
    gen = next(
        command["argv"] for command in plan["commands"] if command["phase"] == "gen-config"
    )
    assert "--force" not in gen
    assert gen[gen.index("--with-secrets") + 1] == "secrets.yaml"


def test_controlplane_yaml_alone_not_pki_marker(tmp_path):
    """Machine config without secrets/talosconfig is not treated as bootstrapped.

    Gen-config still omits --force, so talosctl itself refuses if the file exists
    on the deploy host; we only hard-fail on the PKI-bearing markers.
    """
    workdir = tmp_path / "etc-genestack" / "talos"
    workdir.mkdir(parents=True)
    (workdir / "controlplane.yaml").write_text("version: v1alpha1\n", encoding="utf-8")
    assert talos_workdir_bootstrapped(workdir) is False
    plan = build_talos_plan(_doc(DOC_TALOS), _env(tmp_path))
    gen = next(c["argv"] for c in plan["commands"] if c["phase"] == "gen-config")
    assert "--force" not in gen


DOC_TALOS_DUAL_NIC = """\
provider: talos
talos:
  cluster_name: example
  install_disk: /dev/sda
servers:
  ns1:
    ip: 10.10.0.11
    private_ip: 10.10.0.11
    public_ip: 203.0.113.10
    roles: [k8s_control_plane]
  ns2:
    ip: 10.10.0.12
    private_ip: 10.10.0.12
    public_ip: 192.0.2.130
    roles: [compute]
"""


def test_dual_nic_uses_private_cluster_ip_and_public_apply(tmp_path):
    entry = {
        "ip": "10.10.0.11",
        "private_ip": "10.10.0.11",
        "public_ip": "203.0.113.10",
    }
    assert node_cluster_ip(entry, "ns1") == "10.10.0.11"
    assert node_apply_ip(entry, "ns1") == "203.0.113.10"
    doc = _doc(DOC_TALOS_DUAL_NIC)
    assert fleet_has_private_nic(doc) is True
    plan = build_talos_plan(doc, _env(tmp_path))
    assert plan["lock_public"] is True
    assert plan["control_planes"][0]["ip"] == "10.10.0.11"
    assert plan["control_planes"][0]["apply_ip"] == "203.0.113.10"
    gen = next(c["argv"] for c in plan["commands"] if c["phase"] == "gen-config")
    assert "https://10.10.0.11:6443" in gen
    apply_cp = next(
        c["argv"] for c in plan["commands"] if c["phase"] == "apply-controlplane"
    )
    assert "203.0.113.10" in apply_cp
    boot = next(c["argv"] for c in plan["commands"] if c["phase"] == "bootstrap")
    assert "203.0.113.10" in boot
    endpoints = next(c["argv"] for c in plan["commands"] if c["phase"] == "endpoints")
    assert "203.0.113.10" in endpoints
    assert "--talosconfig=./talosconfig" in endpoints
    assert endpoints[1] == "config"
    wait = next(
        c["argv"] for c in plan["commands"] if c["phase"] == "wait-controlplane"
    )
    assert "203.0.113.10" in " ".join(wait)
    assert "--insecure" not in wait
    kube = next(c["argv"] for c in plan["commands"] if c["phase"] == "kubeconfig")
    assert "203.0.113.10" in kube
    assert any(c["phase"] == "write-firewall" for c in plan["commands"])
    patch = network_patch_yaml(doc)
    assert "10.0.0.0/8" in patch
    assert "validSubnets" in patch
    assert "etcd" not in patch
    etcd = etcd_patch_yaml(doc)
    assert "advertisedSubnets" in etcd
    assert "10.0.0.0/8" in etcd
    fw = firewall_yaml(doc)
    assert fw.lstrip().startswith("---")
    assert "NetworkDefaultActionConfig" in fw
    assert "ingress: block" in fw
    # Dual-NIC does not punch 6443/50000 to the world. Management CIDRs required.
    assert "0.0.0.0/0" not in fw
    assert "public-ingress-tcp" not in fw


def test_firewall_public_management_cidrs_not_world():
    built = yaml.safe_load(DOC_TALOS_DUAL_NIC)
    built.setdefault("talos", {})["public_management_cidrs"] = ["203.0.113.20/32"]
    fw = firewall_yaml(built)
    assert "6443" in fw
    assert "50000" in fw
    assert "203.0.113.20/32" in fw
    assert "0.0.0.0/0" not in fw


def test_private_cidr_pins_fabric_and_nodeports():
    doc = _doc(DOC_TALOS_VLAN)
    patch = network_patch_yaml(doc)
    assert "10.10.0.0/24" in patch
    assert "10.0.0.0/8" not in patch
    assert "nodeport-addresses" in patch
    assert "healthz-bind-address" in patch
    fw = firewall_yaml(doc)
    assert "10.10.0.0/24" in fw
    assert "10.236.0.0/14" in fw
    assert "0.0.0.0/0" not in fw


DOC_TALOS_VLAN = """\
provider: talos
talos:
  cluster_name: example
  install_disk: /dev/sda
ovh:
  vlan_id: 10
  private_cidr: 10.10.0.0/24
servers:
  ns1:
    ip: 10.10.0.11
    private_ip: 10.10.0.11
    public_ip: 203.0.113.10
    private_mac: "00:11:22:33:44:55"
    roles: [k8s_control_plane]
  ns2:
    ip: 10.10.0.12
    private_ip: 10.10.0.12
    public_ip: 192.0.2.130
    private_mac: "00:11:22:33:44:66"
    roles: [compute]
"""


def test_vlan_patch_tags_private_nic_by_mac(tmp_path):
    doc = _doc(DOC_TALOS_VLAN)
    entry = doc["servers"]["ns1"]
    patch = node_vlan_patch_yaml("ns1", entry, doc)
    assert patch is not None
    assert "00:11:22:33:44:55" in patch
    assert "hostname: ns1" in patch
    assert "vlanId: 10" in patch
    assert "10.10.0.11/24" in patch
    assert "addresses:" in patch
    loaded = yaml.safe_load(patch)
    iface = loaded["machine"]["network"]["interfaces"][0]
    assert "addresses" not in iface
    assert iface["vlans"][0]["vlanId"] == 10
    assert iface["vlans"][0]["addresses"] == ["10.10.0.11/24"]
    plan = build_talos_plan(doc, _env(tmp_path))
    assert plan["vlan_id"] == 10
    phases = [c["phase"] for c in plan["commands"]]
    assert phases.count("write-node-net") == 2
    apply_cp = next(
        c["argv"] for c in plan["commands"] if c["phase"] == "apply-controlplane"
    )
    assert "--config-patch" in apply_cp
    assert "@node-ns1.yaml" in apply_cp
    apply_w = next(c["argv"] for c in plan["commands"] if c["phase"] == "apply-worker")
    assert "@node-ns2.yaml" in apply_w


def test_rewrite_kubeconfig_server(tmp_path):
    path = tmp_path / "admin.conf"
    path.write_text(
        "clusters:\n- cluster:\n    server: https://10.10.0.11:6443\n", encoding="utf-8"
    )
    assert rewrite_kubeconfig_server(path, "https://203.0.113.10:6443") is True
    assert "https://203.0.113.10:6443" in path.read_text(encoding="utf-8")
    assert rewrite_kubeconfig_server(path, "https://203.0.113.10:6443") is False


def test_untagged_vlan_pins_address_on_private_mac():
    doc = _doc(
        """\
provider: talos
ovh:
  vlan_id: 0
  private_cidr: 10.10.0.0/24
servers:
  ns1:
    ip: 10.10.0.11
    private_ip: 10.10.0.11
    private_mac: "aa:bb:cc:dd:ee:ff"
    roles: [k8s_control_plane]
"""
    )
    patch = node_vlan_patch_yaml("ns1", doc["servers"]["ns1"], doc)
    assert patch is not None
    assert "aa:bb:cc:dd:ee:ff" in patch
    assert "10.10.0.11/24" in patch
    assert "vlanId" not in patch
    assert "vlans:" not in patch
    assert vlan_id_from_doc(doc) == 0


def test_missing_vlan_id_defaults_to_100(tmp_path):
    """Fabric UI default is VLAN 100; a doc that never saved fabric must not land untagged."""
    doc = _doc(
        """\
provider: talos
ovh:
  private_cidr: 10.10.0.0/24
servers:
  ns1:
    ip: 10.10.0.11
    private_ip: 10.10.0.11
    private_mac: "00:11:22:33:44:55"
    roles: [k8s_control_plane]
"""
    )
    assert vlan_id_from_doc(doc) == DEFAULT_VLAN_ID == 100
    patch = node_vlan_patch_yaml("ns1", doc["servers"]["ns1"], doc)
    assert patch is not None
    assert "vlanId: 100" in patch
    loaded = yaml.safe_load(patch)
    assert loaded["machine"]["network"]["interfaces"][0]["vlans"][0]["vlanId"] == 100
    plan = build_talos_plan(doc, _env(tmp_path))
    assert plan["vlan_id"] == 100


def test_invalid_vlan_id_defaults_to_100():
    assert vlan_id_from_doc({"ovh": {}}) == 100
    assert vlan_id_from_doc({"ovh": {"vlan_id": None}}) == 100
    assert vlan_id_from_doc({"ovh": {"vlan_id": ""}}) == 100
    assert vlan_id_from_doc({"ovh": {"vlan_id": "not-a-vlan"}}) == 100
    assert vlan_id_from_doc({"ovh": {"vlan_id": 4001}}) == 100
    assert vlan_id_from_doc({"ovh": {"vlan_id": -1}}) == 100
    assert vlan_id_from_doc({"ovh": {"vlan_id": 0}}) == 0
    assert vlan_id_from_doc({"ovh": {"vlan_id": 10}}) == 10


def test_plan_defaults_cluster_name_and_disk(tmp_path):
    env = _env(tmp_path)
    plan = build_talos_plan(_doc(DOC_TALOS_DEFAULTS), env)
    assert plan["cluster_name"] == env.name
    assert plan["install_disk"] == "/dev/sda"


def test_plan_hostname_fallback_when_no_ip(tmp_path):
    doc = _doc(
        """\
provider: talos
servers:
  cp1:
    roles: [k8s_control_plane]
"""
    )
    plan = build_talos_plan(doc, _env(tmp_path))
    assert plan["control_planes"][0]["hostname"] == "cp1"
    assert plan["control_planes"][0]["ip"] == "cp1"


def test_plan_kubeconfig_path_override(tmp_path):
    env = _env(tmp_path, kubeconfig_path=str(tmp_path / "kc.yaml"))
    plan = build_talos_plan(_doc(DOC_TALOS_DEFAULTS), env)
    assert plan["kubeconfig"] == tmp_path / "kc.yaml"
    kubeconfig_cmd = plan["commands"][-1]
    assert kubeconfig_cmd["argv"][2] == str(tmp_path / "kc.yaml")


def test_plan_no_control_plane_fails(tmp_path):
    with pytest.raises(ConfigValidationError, match="k8s_control_plane"):
        build_talos_plan(_doc(DOC_TALOS_NO_CP), _env(tmp_path))


def test_plan_rejects_shell_metachar_ip(tmp_path):
    doc = _doc(
        """\
provider: talos
servers:
  cp1:
    ip: "10.0.0.11; curl evil"
    roles: [k8s_control_plane]
"""
    )
    with pytest.raises(ConfigValidationError, match="node address"):
        build_talos_plan(doc, _env(tmp_path))


def test_plan_empty_install_disk_fails(tmp_path):
    with pytest.raises(ConfigValidationError, match="install_disk"):
        build_talos_plan(_doc(DOC_TALOS_EMPTY_DISK), _env(tmp_path))


def test_plan_without_config_dir_fails(tmp_path):
    env = Environment(name=f"env-talos-{uuid.uuid4().hex[:8]}")
    with pytest.raises(ConfigValidationError, match="genestack_config_dir"):
        build_talos_plan(_doc(DOC_TALOS_DEFAULTS), env)


def test_talos_doc_section_validation():
    with pytest.raises(ConfigValidationError, match="'talos' must be a mapping"):
        parse_document("talos: not-a-mapping\n")
    with pytest.raises(
        ConfigValidationError, match="talos.cluster_name must be a string"
    ):
        parse_document("talos:\n  cluster_name: 42\n")
    _doc, warnings = parse_document("talos:\n  bogus_key: x\n")
    assert any("talos: unknown key 'bogus_key'" in w for w in warnings)


# ---------------------------------------------------------------------------
# genestack.talos.bootstrap op
# ---------------------------------------------------------------------------


def _create_env(client, headers, **fields) -> dict:
    resp = client.post(
        "/api/v1/environments",
        headers=headers,
        json={"name": f"env-talos-{uuid.uuid4().hex[:8]}", **fields},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _put_doc(client, headers, env_id, doc=DOC_TALOS):
    resp = client.put(
        f"/api/v1/environments/{env_id}/config",
        headers=headers,
        json={"yaml_text": doc},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["version"]


def _talos_job(client, headers, env_id, params=None):
    return client.post(
        f"/api/v1/environments/{env_id}/jobs",
        headers=headers,
        json={
            "operation": "genestack.talos.bootstrap",
            "params": params or {},
            "run_sync": True,
        },
    )


def _job_log(client, headers, job_id) -> str:
    resp = client.get(f"/api/v1/jobs/{job_id}", headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()["log_text"]


def test_talos_op_in_catalog():
    op = get_operation("genestack.talos.bootstrap")
    assert op is not None
    assert op.required_role == "admin"
    assert op.mutating is True
    assert op.handler == "genestack_talos_bootstrap"
    assert op.timeout_seconds == 7200


def test_talos_bootstrap_dry_run_logs_command_sequence_in_order(
    client, admin_headers, tmp_path
):
    """Global test config is dry_run=True: full sequence logged, nothing run."""
    config_dir = tmp_path / "etc-genestack"
    config_dir.mkdir()
    env = _create_env(client, admin_headers, genestack_config_dir=str(config_dir))
    _put_doc(client, admin_headers, env["id"])

    resp = _talos_job(client, admin_headers, env["id"])
    assert resp.status_code == 201, resp.text
    job = resp.json()
    assert job["status"] == "success", job["error"]

    log_text = _job_log(client, admin_headers, job["id"])
    kubeconfig = config_dir / "inventory" / "artifacts" / "admin.conf"
    expected = [
        "talosctl gen config stagefurious https://10.0.0.11:6443 --install-disk /dev/sdb",
        "talosctl config endpoints 10.0.0.11 --talosconfig=./talosconfig",
        "talosctl apply-config --insecure --nodes 10.0.0.11 --file controlplane.yaml",
        "talosctl apply-config --insecure --nodes 10.0.0.21 --file worker.yaml",
        "talosctl apply-config --insecure --nodes 10.0.0.22 --file worker.yaml",
        "talosctl bootstrap --nodes 10.0.0.11 --talosconfig=./talosconfig",
        f"talosctl kubeconfig {kubeconfig} --nodes 10.0.0.11 --talosconfig=./talosconfig",
    ]
    positions = []
    for line in expected:
        pos = log_text.find(line)
        assert pos != -1, f"missing log line: {line}"
        positions.append(pos)
    assert positions == sorted(positions), "talos commands logged out of order"
    # Advisory notes from docs/k8s-talos.md are logged, never enforced
    assert "kube-ovn to v1.14.10" in log_text
    assert "iscsi-tools" in log_text
    # Nothing executed
    assert list(config_dir.rglob("*")) == []

    audit = client.get(
        "/api/v1/audit",
        headers=admin_headers,
        params={"environment_id": env["id"], "action": "env.talos_bootstrap"},
    )
    entries = [e for e in audit.json() if e["action"] == "env.talos_bootstrap"]
    assert entries, "expected an env.talos_bootstrap audit entry"
    details = entries[0]["details"]
    assert details["cluster_name"] == "stagefurious"
    assert details["control_planes"] == 1
    assert details["workers"] == 2
    assert entries[0]["success"] is True


def _capture_talos_commands(monkeypatch, fail_phase=None, write_kubeconfig=False):
    """Capture run_command calls; optionally fail one talos phase with rc=1."""
    captured: list[list[str]] = []

    def fake_run_command(cmd, **kwargs):  # noqa: ARG001
        argv = [str(c) for c in cmd]
        captured.append(argv)
        rc = 0
        if fail_phase == "gen-config" and argv[1:3] == ["gen", "config"]:
            rc = 1
        elif (
            fail_phase == "apply-controlplane"
            and "apply-config" in argv
            and "controlplane.yaml" in argv
        ):
            rc = 1
        elif (
            fail_phase == "apply-worker"
            and "apply-config" in argv
            and "worker.yaml" in argv
        ):
            rc = 1
        elif fail_phase == "endpoints" and argv[1:3] == ["config", "endpoints"]:
            rc = 1
        elif fail_phase == "bootstrap" and argv[1] == "bootstrap":
            rc = 1
        elif argv[1] == "kubeconfig" and write_kubeconfig:
            Path(argv[2]).parent.mkdir(parents=True, exist_ok=True)
            Path(argv[2]).write_text("talos-kubeconfig\n", encoding="utf-8")
        return {"returncode": rc, "dry_run": False, "message": f"rc={rc}"}

    monkeypatch.setattr(bridge, "run_command", fake_run_command)
    return captured


def test_talos_bootstrap_phase_failure_stops(
    client, admin_headers, tmp_path, monkeypatch
):
    config_dir = tmp_path / "etc-genestack"
    config_dir.mkdir()
    env = _create_env(
        client, admin_headers, genestack_config_dir=str(config_dir), dry_run=False
    )
    _put_doc(client, admin_headers, env["id"])
    captured = _capture_talos_commands(monkeypatch, fail_phase="bootstrap")

    resp = _talos_job(client, admin_headers, env["id"])
    assert resp.status_code == 201, resp.text
    job = resp.json()
    assert job["status"] == "failed"
    assert "phase 'bootstrap'" in job["error"]

    # The kubeconfig fetch after the failing phase never ran
    assert not any(argv[1] == "kubeconfig" for argv in captured)

    log_text = _job_log(client, admin_headers, job["id"])
    assert "[talos] FAILED at phase 'bootstrap' rc=1" in log_text

    audit = client.get(
        "/api/v1/audit",
        headers=admin_headers,
        params={"environment_id": env["id"], "action": "env.talos_bootstrap"},
    )
    entries = [e for e in audit.json() if e["action"] == "env.talos_bootstrap"]
    assert entries
    assert entries[0]["details"]["failed_phase"] == "bootstrap"
    assert entries[0]["success"] is False


def test_talos_bootstrap_kubeconfig_written_to_env_path(
    client, admin_headers, tmp_path, monkeypatch
):
    """talosctl kubeconfig writes to the env's kubeconfig target path."""
    config_dir = tmp_path / "etc-genestack"
    config_dir.mkdir()
    kubeconfig = config_dir / "inventory" / "artifacts" / "admin.conf"
    env = _create_env(
        client, admin_headers, genestack_config_dir=str(config_dir), dry_run=False
    )
    _put_doc(client, admin_headers, env["id"], doc=DOC_TALOS_DEFAULTS)
    captured = _capture_talos_commands(monkeypatch, write_kubeconfig=True)

    resp = _talos_job(client, admin_headers, env["id"])
    assert resp.status_code == 201, resp.text
    job = resp.json()
    assert job["status"] == "success", job["error"]

    kubeconfig_cmds = [argv for argv in captured if argv[1] == "kubeconfig"]
    assert len(kubeconfig_cmds) == 1
    assert kubeconfig_cmds[0][2] == str(kubeconfig)
    # The job created the file, stored it, and removed it when it finished.
    assert not kubeconfig.exists()
    assert (config_dir / "talos").is_dir()
    db = SessionLocal()
    try:
        row = db.get(Environment, env["id"])
        assert decrypt_secret(row.kubeconfig_data) == "talos-kubeconfig\n"
    finally:
        db.close()
    log_text = _job_log(client, admin_headers, job["id"])
    assert "talos-kubeconfig" not in log_text


def test_talos_remote_kubeconfig_is_stored_and_removed(
    client, admin_headers, tmp_path, monkeypatch
):
    """talosctl on the deploy host does not leave the kubeconfig there."""
    config_dir = tmp_path / "etc-genestack"
    config_dir.mkdir()
    kubeconfig = config_dir / "inventory" / "artifacts" / "admin.conf"
    secret = "talos-remote-kubeconfig\n"
    env = _create_env(
        client,
        admin_headers,
        genestack_config_dir=str(config_dir),
        deployer_ssh_host="deployer.example",
        deployer_ssh_user="ubuntu",
        dry_run=False,
    )
    _put_doc(client, admin_headers, env["id"], doc=DOC_TALOS_DEFAULTS)
    captured: list[tuple[list[str], str | None, object]] = []

    def fake_run_command(cmd, **kwargs):
        argv = [str(c) for c in cmd]
        captured.append((argv, kwargs.get("ssh_target"), kwargs.get("log")))
        if argv[:2] == ["test", "-f"]:
            return {"returncode": 1, "stdout": "", "stderr": ""}
        if argv[:1] == ["cat"]:
            return {"returncode": 0, "stdout": secret, "stderr": ""}
        if argv[:2] == ["rm", "-f"]:
            return {"returncode": 0, "stdout": "", "stderr": ""}
        return {"returncode": 0, "stdout": "", "stderr": "", "dry_run": False}

    monkeypatch.setattr(bridge, "run_command", fake_run_command)

    resp = _talos_job(client, admin_headers, env["id"])
    assert resp.status_code == 201, resp.text
    job = resp.json()
    assert job["status"] == "success", job["error"]
    assert not kubeconfig.exists()
    db = SessionLocal()
    try:
        row = db.get(Environment, env["id"])
        assert decrypt_secret(row.kubeconfig_data) == secret
    finally:
        db.close()
    log_text = _job_log(client, admin_headers, job["id"])
    assert secret.strip() not in log_text
    reads = [item for item in captured if item[0][:1] == ["cat"]]
    assert reads and reads[0][2] is None
    assert reads[0][1] == "ubuntu@deployer.example"
    removals = [item for item in captured if item[0][:2] == ["rm", "-f"]]
    assert any(str(kubeconfig) in item[0] for item in removals)
    assert all(item[2] is None for item in removals)


def test_talos_bootstrap_without_config_document_fails(client, admin_headers, tmp_path):
    config_dir = tmp_path / "etc-genestack"
    config_dir.mkdir()
    env = _create_env(client, admin_headers, genestack_config_dir=str(config_dir))

    resp = _talos_job(client, admin_headers, env["id"])
    assert resp.status_code == 201, resp.text
    job = resp.json()
    assert job["status"] == "failed"
    assert "no config document" in job["error"]


def test_talos_bootstrap_no_control_plane_fails(client, admin_headers, tmp_path):
    config_dir = tmp_path / "etc-genestack"
    config_dir.mkdir()
    env = _create_env(client, admin_headers, genestack_config_dir=str(config_dir))
    _put_doc(client, admin_headers, env["id"], doc=DOC_TALOS_NO_CP)

    resp = _talos_job(client, admin_headers, env["id"])
    assert resp.status_code == 201, resp.text
    job = resp.json()
    assert job["status"] == "failed"
    assert "k8s_control_plane" in job["error"]


def test_talos_bootstrap_operator_forbidden(
    client, admin_headers, operator_headers, tmp_path
):
    config_dir = tmp_path / "etc-genestack"
    config_dir.mkdir()
    env = _create_env(client, admin_headers, genestack_config_dir=str(config_dir))

    resp = _talos_job(client, operator_headers, env["id"])
    assert resp.status_code == 403


DOC_ONE_CP = """\
provider: talos
servers:
  cp1:
    ip: 10.0.0.11
    roles: [k8s_control_plane]
"""


def _probe_reply(argv, *, insecure_state, auth_ok, rebooted=None):
    """Fake talosctl version. None means this argv is not a probe."""
    if len(argv) < 2 or argv[1] != "version":
        return None
    if "--insecure" in argv:
        if rebooted is not None and rebooted.get("ok"):
            return {
                "returncode": 0,
                "stdout": "Talos\n",
                "stderr": "",
                "dry_run": False,
            }
        if insecure_state == "maintenance":
            return {"returncode": 0, "stdout": "Talos\n", "stderr": "", "dry_run": False}
        if insecure_state == "down":
            return {
                "returncode": 1,
                "stdout": "",
                "stderr": "connection refused\n",
                "dry_run": False,
            }
        if insecure_state == "unknown-authority":
            return {
                "returncode": 1,
                "stdout": "",
                "stderr": (
                    "rpc error: code = Unavailable desc = connection error: "
                    "desc = transport: authentication handshake failed: tls: "
                    "failed to verify certificate: x509: certificate signed by "
                    "unknown authority\n"
                ),
                "dry_run": False,
            }
        return {
            "returncode": 1,
            "stdout": "",
            "stderr": (
                "rpc error: code = Unavailable desc = "
                "error reading server preface: remote error: tls: certificate required\n"
            ),
            "dry_run": False,
        }
    if any(str(part).startswith("--talosconfig") for part in argv):
        if auth_ok:
            return {"returncode": 0, "stdout": "Talos\n", "stderr": "", "dry_run": False}
        return {
            "returncode": 1,
            "stdout": "",
            "stderr": (
                "tls: failed to verify certificate: x509: certificate signed by "
                "unknown authority\n"
            ),
            "dry_run": False,
        }
    return None


def _install_probe(monkeypatch, *, insecure_state, auth_ok, rebooted=None, bootstrap_done=False):
    captured: list[list[str]] = []

    def fake_run_command(cmd, **kwargs):
        argv = [str(part) for part in cmd]
        captured.append(argv)
        probed = _probe_reply(
            argv,
            insecure_state=insecure_state,
            auth_ok=auth_ok,
            rebooted=rebooted,
        )
        if probed is not None:
            return probed
        if bootstrap_done and len(argv) > 1 and argv[1] == "bootstrap":
            return {
                "returncode": 1,
                "stdout": "",
                "stderr": "cluster is already bootstrapped\n",
                "dry_run": False,
            }
        if len(argv) > 1 and argv[1] == "kubeconfig":
            Path(argv[2]).parent.mkdir(parents=True, exist_ok=True)
            Path(argv[2]).write_text("talos-kubeconfig\n", encoding="utf-8")
        return {"returncode": 0, "stdout": "", "stderr": "", "dry_run": False}

    monkeypatch.setattr(bridge, "run_command", fake_run_command)
    return captured


def _save_identity(config_dir: Path) -> None:
    workdir = config_dir / "talos"
    workdir.mkdir(parents=True)
    (workdir / "secrets.yaml").write_text("cluster:\n  id: fake\n", encoding="utf-8")
    (workdir / "talosconfig").write_text("context: fake\n", encoding="utf-8")
    (workdir / "controlplane.yaml").write_text("version: v1alpha1\n", encoding="utf-8")


def test_talos_bootstrap_retries_saved_identity_in_maintenance(
    client, admin_headers, tmp_path, monkeypatch
):
    """A failed run's secrets must not make the next click stop before apply."""
    config_dir = tmp_path / "etc-genestack"
    config_dir.mkdir()
    _save_identity(config_dir)
    env = _create_env(
        client, admin_headers, genestack_config_dir=str(config_dir), dry_run=False
    )
    _put_doc(client, admin_headers, env["id"], doc=DOC_ONE_CP)
    captured = _install_probe(monkeypatch, insecure_state="maintenance", auth_ok=False)

    resp = _talos_job(client, admin_headers, env["id"])
    assert resp.status_code == 201, resp.text
    job = resp.json()
    assert job["status"] == "success", job["error"]
    assert not any(argv[1:3] == ["gen", "config"] for argv in captured)
    assert any(
        "apply-config" in argv and "--insecure" in argv and "controlplane.yaml" in argv
        for argv in captured
    )
    assert "already looks bootstrapped" not in (job.get("error") or "")


def test_talos_bootstrap_matching_client_leaves_the_node_installed(
    client, admin_headers, tmp_path, monkeypatch
):
    config_dir = tmp_path / "etc-genestack"
    config_dir.mkdir()
    _save_identity(config_dir)
    env = _create_env(
        client, admin_headers, genestack_config_dir=str(config_dir), dry_run=False
    )
    _put_doc(client, admin_headers, env["id"], doc=DOC_ONE_CP)
    captured = _install_probe(
        monkeypatch, insecure_state="cert", auth_ok=True, bootstrap_done=True
    )

    resp = _talos_job(client, admin_headers, env["id"])
    assert resp.status_code == 201, resp.text
    job = resp.json()
    assert job["status"] == "success", job["error"]
    assert not any("apply-config" in argv for argv in captured)
    assert any(argv[1] == "kubeconfig" for argv in captured if len(argv) > 1)
    log_text = _job_log(client, admin_headers, job["id"])
    assert "stays in place" in log_text
    assert "already bootstrapped" in log_text


def test_talos_bootstrap_certificate_required_names_the_machine(
    client, admin_headers, tmp_path, monkeypatch
):
    """No management port: stop before a new identity, and say why."""
    config_dir = tmp_path / "etc-genestack"
    config_dir.mkdir()
    env = _create_env(
        client, admin_headers, genestack_config_dir=str(config_dir), dry_run=False
    )
    _put_doc(client, admin_headers, env["id"], doc=DOC_ONE_CP)
    captured = _install_probe(monkeypatch, insecure_state="cert", auth_ok=False)

    resp = _talos_job(client, admin_headers, env["id"])
    assert resp.status_code == 201, resp.text
    job = resp.json()
    assert job["status"] == "failed"
    error = job["error"]
    assert "cp1" in error
    assert "10.0.0.11" in error
    assert "not in Talos maintenance mode" in error
    assert "no management port" in error
    assert "certificate required" in error
    assert "confirm on this job" in error.lower()
    assert "already looks bootstrapped" not in error
    step = job["user_step"]
    assert step["kind"] == "boot-installer"
    assert step["hostname"] == "cp1"
    assert step["address"] == "10.0.0.11"
    assert step["confirm"] == "The installer is up — continue"
    assert "replaces the install" in step["detail"]
    assert "Remove those files" not in error
    assert "rc=1" not in error
    assert not any(argv[1:3] == ["gen", "config"] for argv in captured)
    assert not (config_dir / "talos" / "secrets.yaml").exists()


def test_talos_bootstrap_saved_identity_does_not_dead_end(
    client, admin_headers, tmp_path, monkeypatch
):
    """The reported retry: secrets exist, the node wants a certificate, no BMC."""
    config_dir = tmp_path / "etc-genestack"
    config_dir.mkdir()
    _save_identity(config_dir)
    env = _create_env(
        client, admin_headers, genestack_config_dir=str(config_dir), dry_run=False
    )
    _put_doc(client, admin_headers, env["id"], doc=DOC_ONE_CP)
    captured = _install_probe(monkeypatch, insecure_state="cert", auth_ok=False)

    resp = _talos_job(client, admin_headers, env["id"])
    assert resp.status_code == 201, resp.text
    job = resp.json()
    assert job["status"] == "failed"
    error = job["error"]
    assert "not in Talos maintenance mode" in error
    assert "no management port" in error
    assert "confirm on this job" in error.lower()
    assert job["user_step"]["kind"] == "boot-installer"
    assert "already looks bootstrapped" not in error
    assert "Remove those files" not in error
    assert (config_dir / "talos" / "secrets.yaml").is_file()
    assert not any(argv[1:3] == ["gen", "config"] for argv in captured)
    assert not any("--force" in argv for argv in captured)


def test_talos_bootstrap_reboots_into_maintenance_then_applies(
    client, admin_headers, tmp_path, monkeypatch
):
    from datetime import datetime, timezone

    from tests.test_baremetal import _make_node

    config_dir = tmp_path / "etc-genestack"
    config_dir.mkdir()
    env = _create_env(
        client, admin_headers, genestack_config_dir=str(config_dir), dry_run=False
    )
    _put_doc(client, admin_headers, env["id"], doc=DOC_ONE_CP)
    _make_node(
        env["id"],
        name="cp1",
        wiped_at=datetime.now(timezone.utc),
        expected_ip="10.0.0.11",
        pxe_mac="aa:bb:cc:dd:ee:41",
    )
    rebooted = {"ok": False}
    calls: list[tuple] = []

    def fake_boot(
        db,
        env_row,
        node,
        target,
        *,
        boot_now,
        dry_run,
        log,
        settings=None,
        replace_installed=False,
    ):
        calls.append((node.name, target, boot_now, dry_run, replace_installed))
        rebooted["ok"] = True
        return {"ok": True}

    monkeypatch.setattr("app.services.baremetal.set_next_boot", fake_boot)
    captured = _install_probe(
        monkeypatch, insecure_state="cert", auth_ok=False, rebooted=rebooted
    )

    resp = _talos_job(client, admin_headers, env["id"])
    assert resp.status_code == 201, resp.text
    job = resp.json()
    assert job["status"] == "success", job["error"]
    assert not job.get("user_step")
    assert calls == [("cp1", "talos", True, False, True)]
    assert any(
        "apply-config" in argv and "--insecure" in argv for argv in captured
    )
    log_text = _job_log(client, admin_headers, job["id"])
    assert "rebooting it into the Talos installer" in log_text
    assert "back in maintenance mode" in log_text


def test_talos_bootstrap_reconciles_a_mismatched_certificate(
    client, admin_headers, tmp_path, monkeypatch
):
    """Console-owned machine, no prior wipe: wrong CA is drift, so reboot and apply."""
    from tests.test_baremetal import _make_node

    config_dir = tmp_path / "etc-genestack"
    config_dir.mkdir()
    env = _create_env(
        client, admin_headers, genestack_config_dir=str(config_dir), dry_run=False
    )
    _put_doc(client, admin_headers, env["id"], doc=DOC_ONE_CP)
    _make_node(env["id"], name="cp1", expected_ip="10.0.0.11", pxe_mac="aa:bb:cc:dd:ee:42")
    rebooted = {"ok": False}
    calls: list[tuple] = []

    def fake_boot(
        db,
        env_row,
        node,
        target,
        *,
        boot_now,
        dry_run,
        log,
        settings=None,
        replace_installed=False,
    ):
        calls.append((node.name, target, boot_now, dry_run, replace_installed))
        rebooted["ok"] = True
        return {"ok": True}

    monkeypatch.setattr("app.services.baremetal.set_next_boot", fake_boot)
    captured = _install_probe(
        monkeypatch, insecure_state="unknown-authority", auth_ok=False, rebooted=rebooted
    )

    resp = _talos_job(client, admin_headers, env["id"])
    assert resp.status_code == 201, resp.text
    job = resp.json()
    assert job["status"] == "success", job["error"]
    assert not job.get("user_step")
    assert calls == [("cp1", "talos", True, False, True)]
    assert any(
        "apply-config" in argv and "--insecure" in argv for argv in captured
    )
    log_text = _job_log(client, admin_headers, job["id"])
    assert "does not match" in log_text


def test_talos_bootstrap_down_node_does_not_write_an_identity(
    client, admin_headers, tmp_path, monkeypatch
):
    config_dir = tmp_path / "etc-genestack"
    config_dir.mkdir()
    env = _create_env(
        client, admin_headers, genestack_config_dir=str(config_dir), dry_run=False
    )
    _put_doc(client, admin_headers, env["id"], doc=DOC_ONE_CP)
    captured = _install_probe(monkeypatch, insecure_state="down", auth_ok=False)

    resp = _talos_job(client, admin_headers, env["id"])
    assert resp.status_code == 201, resp.text
    job = resp.json()
    assert job["status"] == "failed"
    assert "not answering" in job["error"]
    assert "10.0.0.11" in job["error"]
    assert "did not write a new cluster identity" in job["error"]
    assert "confirm on this job" in job["error"].lower()
    assert job["user_step"]["kind"] == "power-on"
    assert job["user_step"]["confirm"] == "It is on — continue"
    assert not any(argv[1:3] == ["gen", "config"] for argv in captured)
    assert not (config_dir / "talos" / "secrets.yaml").exists()
