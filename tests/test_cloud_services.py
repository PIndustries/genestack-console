"""First-cloud defaults must fit measured nodes and stay on their private LAN."""

import copy

import pytest


def document(count=1):
    roles = ["k8s_control_plane", "control", "compute", "network", "storage"]
    machines = [
        {
            "name": f"node-{i}",
            "ip": f"10.77.28.{191 + i}",
            "cpu_threads": 16,
            "memory_gib": 64,
            "roles": roles,
            "disks": [{"purpose": "storage", "capacity_bytes": 120 * 1024**3}],
        }
        for i in range(count)
    ]
    return {
        "network": {"cidr": "10.77.28.0/24", "gateway": "10.77.28.1"},
        "pxe": {
            "next_server": "10.77.28.1",
            "range_start": "10.77.28.191",
            "range_end": "10.77.28.254",
        },
        "servers": {m["name"]: {"ip": m["ip"], "roles": roles} for m in machines},
        "hardware_installation": {"reviewed_plan": {"machines": machines}},
        "storage": {
            "replication_factor": min(3, count),
            "longhorn_cinder": {"enabled": True, "capacity_gib": 42},
        },
    }


def test_single_node_cloud_has_no_impossible_three_node_requirements():
    from app.services.cloud_services import configure, rendered_files

    doc = document()
    original = copy.deepcopy(doc)
    configured, plan = configure(doc, "owned-environment")
    files = rendered_files(configured)
    assert doc == original
    assert plan["service_replicas"] == 1
    assert plan["quorum_replicas"] == 1
    assert configured["network"]["interfaces"] == "automatic"
    assert "container_interface" not in configured["network"]
    assert "external_interface" not in configured["network"].get("ovn", {})
    assert "replicas: 1" in files["kustomize/mariadb-cluster/overlay/console-sizing.yaml"]
    assert "replicas: 1" in files["kustomize/rabbitmq-cluster/overlay/console-sizing.yaml"]
    assert configured["helm_overrides"]["memcached"]["pod"]["replicas"]["server"] == 1
    assert plan["storage_requested_gib"] <= plan["storage_usable_gib"]


def test_private_vips_exclude_dhcp_and_reserved_addresses():
    from app.services.cloud_services import configure

    doc, plan = configure(document(), "owned-environment")
    assert plan["endpoint_ip"] == "10.77.28.2"
    assert plan["pool_start"] == "10.77.28.3"
    assert plan["pool_end"] == "10.77.28.9"
    assert doc["network"]["gateway_domain"].endswith(".home.arpa")
    assert not doc["network"].get("acme_email")
    assert doc["cloud_services"]["private_network_only"] is True
    crowded = document()
    crowded["pxe"]["range_start"] = "10.77.28.2"
    with pytest.raises(ValueError, match="private endpoint"):
        configure(crowded, "owned-environment")


def test_dynamic_replicas_follow_real_control_nodes_and_preserve_overrides():
    from app.services.cloud_services import configure

    doc = document(3)
    doc["helm_overrides"] = {"memcached": {"conf": {"memcached": {"max_connections": 512}}}}
    configured, plan = configure(doc, "owned-environment")
    assert plan["quorum_replicas"] == 3
    assert configured["helm_overrides"]["memcached"]["conf"]["memcached"]["max_connections"] == 512
    assert configured["helm_overrides"]["memcached"]["pod"]["replicas"]["server"] == 3


def test_missing_capacity_or_insufficient_memory_stops_before_mutations():
    from app.services.cloud_services import configure

    for field, value in [("memory_gib", 4), ("cpu_threads", 1), ("cpu_threads", None)]:
        doc = document()
        doc["hardware_installation"]["reviewed_plan"]["machines"][0][field] = value
        with pytest.raises(ValueError, match="capacity"):
            configure(doc, "owned-environment")


def test_saved_private_addresses_and_volume_sizes_are_stable_on_retry():
    from app.services.cloud_services import configure

    configured, plan = configure(document(), "owned-environment")
    repeated, after = configure(configured, "owned-environment")
    assert after == plan
    assert repeated == configured
    configured["pxe"]["range_start"] = "10.77.28.2"
    with pytest.raises(ValueError, match="unchanged private endpoint"):
        configure(configured, "owned-environment")


def test_automatic_interfaces_remove_the_manual_deployment_blocker():
    from app.services import deploy_needs, envconfig
    from app.services.cloud_services import configure

    doc, _ = configure(document(), "owned-environment")
    assert not deploy_needs.blocking(doc)
    assert envconfig.doc_env(doc)["GENESTACK_NETWORK_INTERFACES"] == "automatic"


def test_compatibility_adapter_refuses_unknown_network_scripts():
    from app.services.cloud_services import compatible_infrastructure

    with pytest.raises(ValueError, match="Unrecognized"):
        compatible_infrastructure("echo unknown layout", "/tmp/runtime/site-network.sh")


def test_private_setup_never_attaches_a_saved_external_provider_port():
    from app.services.cloud_services import configure

    doc = document()
    doc["network"]["ovn"] = {"external_interface": "management0"}
    with pytest.raises(ValueError, match="external provider"):
        configure(doc, "owned-environment")


def test_recognized_new_core_uses_the_shipped_scoped_network_helper():
    from app.services.cloud_services import compatible_infrastructure

    source = 'source "${GENESTACK_BASE_DIR:-/opt/genestack}/bin/lib/site-network.sh"\ngenestack_site_network\ngenestack_annotate_provider_ports\n'
    result = compatible_infrastructure(source, "/scoped/runtime/site-network.sh")
    assert "/scoped/runtime/site-network.sh" in result
    assert "bin/lib/site-network.sh" not in result
