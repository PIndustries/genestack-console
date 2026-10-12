"""Facts that stop a deploy before setup-infrastructure.sh."""

from __future__ import annotations

from types import SimpleNamespace

from app.services.deploy_needs import blocking, collect, node_label_args


def _ready(**overrides: object) -> dict:
    network = {
        "gateway_domain": "cloud.example.org",
        "acme_email": "ops@cloud.example.org",
        "container_interface": "bond0",
        "compute_interface": "bond1",
        "ovn": {"external_interface": "bond0.126"},
    }
    network.update(overrides)
    return {
        "provider": "kubespray",
        "network": network,
        "servers": {
            "n1": {
                "roles": ["control", "compute", "network", "storage", "worker"],
            }
        },
    }


def test_empty_gateway_domain_blocks():
    doc = _ready()
    doc["network"]["gateway_domain"] = ""
    hits = blocking(doc)
    assert [item["code"] for item in hits] == ["gateway-domain"]
    assert "stops on a prompt before it installs kube-ovn" in hits[0]["detail"]


def test_equal_interfaces_block():
    doc = _ready()
    doc["network"]["compute_interface"] = "bond0"
    hits = blocking(doc)
    assert [item["code"] for item in hits] == ["interfaces"]
    assert "both bond0" in hits[0]["detail"]


def test_empty_interface_blocks():
    doc = _ready()
    doc["network"]["container_interface"] = ""
    hits = blocking(doc)
    assert hits[0]["code"] == "interfaces"
    assert "guesses NICs" in hits[0]["detail"]


def test_missing_control_role_blocks():
    doc = _ready()
    doc["servers"]["n1"]["roles"] = ["compute", "network", "storage", "worker"]
    codes = [item["code"] for item in blocking(doc)]
    assert codes == ["role-control"]


def test_kubernetes_control_plane_role_is_not_the_openstack_label():
    doc = _ready()
    doc["servers"] = {
        "cp1": {
            "roles": [
                "k8s_control_plane",
                "compute",
                "network",
                "storage",
                "worker",
            ]
        }
    }
    codes = [item["code"] for item in blocking(doc)]
    assert "role-control" in codes


def test_hyperconverged_skips_roles():
    doc = _ready(hyperconverged=True)
    doc["servers"] = {}
    assert blocking(doc) == []


def test_partial_vlan_blocks():
    doc = _ready()
    doc["network"]["ovn"] = {
        "external_interface": "bond0.126",
        "external_vlan_interface": "bond0",
        "external_vlan_id": "100",
    }
    codes = [item["code"] for item in blocking(doc)]
    assert codes == ["provider-vlan"]


def test_talos_image_without_schematic_blocks():
    doc = _ready()
    doc["provider"] = "talos"
    doc["talos"] = {"install_image": "factory.talos.dev/installer/vanilla:v1.14.2"}
    codes = [item["code"] for item in blocking(doc)]
    assert codes == ["talos-iscsi"]


def test_talos_default_image_is_a_reboot_notice():
    doc = _ready()
    doc["provider"] = "talos"
    needs = collect(doc)
    reboot = [item for item in needs if item["code"] == "talos-reboot"]
    assert len(reboot) == 1
    assert reboot[0]["blocks"] is False
    assert "ext-iscsid" in reboot[0]["detail"]
    assert blocking(doc) == []


def test_cluster_local_with_email_is_a_notice():
    doc = _ready()
    doc["network"]["gateway_domain"] = "cluster.local"
    needs = collect(doc)
    notice = [item for item in needs if item["code"] == "acme-domain"]
    assert len(notice) == 1
    assert notice[0]["blocks"] is False
    assert "stays pending" in notice[0]["detail"]
    assert blocking(doc) == []


def test_empty_acme_email_is_a_notice():
    doc = _ready()
    doc["network"]["acme_email"] = ""
    needs = collect(doc)
    notice = [item for item in needs if item["code"] == "acme-email"]
    assert len(notice) == 1
    assert notice[0]["blocks"] is False
    assert blocking(doc) == []


def test_eth3_on_talos_is_a_notice():
    doc = _ready()
    doc["provider"] = "talos"
    doc["network"]["ovn"] = {"external_interface": "eth3"}
    needs = collect(doc)
    port = [item for item in needs if item["code"] == "provider-port"]
    assert len(port) == 1
    assert port[0]["blocks"] is False
    assert "eth3" in port[0]["detail"]
    assert "bond or PCI" in port[0]["detail"]
    assert not any(item["code"] == "provider-vlan" for item in needs)


def test_saved_bond_is_not_a_provider_notice():
    assert collect(_ready()) == []


def test_empty_provider_port_is_a_notice():
    doc = _ready()
    doc["network"]["ovn"] = {}
    needs = collect(doc)
    port = [item for item in needs if item["code"] == "provider-port"]
    assert len(port) == 1
    assert port[0]["blocks"] is False
    assert "no physical port" in port[0]["detail"]


def test_continue_from_core_ignores_infrastructure_facts():
    assert collect({"provider": "kubespray"}, stages={"core"}) == []
    assert blocking({"provider": "kubespray"}, stages={"core", "compute-network"}) == []


def test_worker_label_is_applied_to_a_compute_node():
    planned = node_label_args(
        {"servers": {"compute-01": {"roles": ["compute"]}}}
    )
    assert planned == [
        (
            "compute-01",
            [
                "openstack-compute-node=enabled",
                "node-role.kubernetes.io/worker=worker",
            ],
        )
    ]


def test_unsafe_hostname_is_skipped():
    assert node_label_args({"servers": {"bad host": {"roles": ["compute"]}}}) == []


def test_hyperconverged_does_not_label():
    assert (
        node_label_args(
            {
                "network": {"hyperconverged": "true"},
                "servers": {"n1": {"roles": ["compute"]}},
            }
        )
        == []
    )


def test_greenfield_live_stops_before_commission(monkeypatch):
    from app.services import greenfield

    doc = {"provider": "kubespray", "servers": {"n1": {"roles": ["compute"]}}}
    monkeypatch.setattr(
        greenfield.envconfig_service,
        "get_current",
        lambda db, env: (doc, None),
    )
    logs: list[str] = []
    result = greenfield.run_greenfield(
        None,
        SimpleNamespace(),
        None,
        logs.append,
        dry_run=False,
        skip_push=True,
        timeout=1,
        extra_env={},
        ssh_target=None,
        remote_env={},
        settings=None,
    )
    assert result["ok"] is False
    assert result["returncode"] == 2
    assert "Gateway domain is not saved" in result["error"]
    assert not any("DESTRUCTIVE" in line for line in logs)
