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


def test_database_operator_and_webhooks_fit_the_actual_control_nodes():
    from app.services.cloud_services import configure

    for count in [1, 2, 3]:
        configured, _plan = configure(document(count), "owned-environment")
        operator = configured["helm_overrides"]["mariadb-operator"]
        assert operator["ha"]["replicas"] == count
        assert operator["webhook"]["ha"]["replicas"] == count
        assert operator["certController"]["ha"]["replicas"] == count


def test_operator_rollout_can_replace_a_pod_when_all_eligible_nodes_are_occupied():
    import yaml

    from app.services.cloud_services import configure, rendered_files

    configured, _ = configure(document(), "owned-environment")
    body = yaml.safe_load(
        rendered_files(configured)["kustomize/mariadb-operator/overlay/kustomization.yaml"]
    )
    patch = yaml.safe_load(body["patches"][0]["patch"])
    strategy = patch[0]["value"]
    assert strategy["rollingUpdate"]["maxSurge"] == 0
    assert strategy["rollingUpdate"]["maxUnavailable"] == 1


def test_cache_and_glance_temporary_claims_fit_the_reserved_data_budget():
    import yaml

    from app.services.cloud_services import configure, finish_rendered_files, rendered_files

    configured, plan = configure(document(), "owned-environment")
    files = rendered_files(configured)
    finish_rendered_files(configured, files)
    overlay = yaml.safe_load(files["kustomize/memcached/overlay/kustomization.yaml"])
    patch = yaml.safe_load(overlay["patches"][0]["patch"])
    assert patch[0]["op"] == "test" and patch[0]["value"] == "memcached-data"
    assert patch[1]["value"] == "1Gi"
    assert plan["storage_requested_gib"] == 42 + 8 + 4 + 10 + 2 + 1 + 2 + 1 + 1
    prior = copy.deepcopy(configured)
    prior["cloud_services"].pop("cache_gib")
    prior["cloud_services"]["storage_requested_gib"] = 68
    corrected, after = configure(prior, "owned-environment")
    assert after["database_gib"] == plan["database_gib"]
    assert corrected["storage"]["longhorn_cinder"]["capacity_gib"] == 42


def test_cloud_autoscalers_preserve_the_managed_cinder_overlay():
    import yaml

    from app.services.cloud_services import configure, finish_rendered_files

    configured, _ = configure(document(), "owned-environment")
    key = "kustomize/cinder/overlay/kustomization.yaml"
    files = {key: "resources: [../base]\npatches: [{path: retained-cinder-storage.yaml}]\n"}
    finish_rendered_files(configured, files)
    overlay = yaml.safe_load(files[key])
    assert overlay["patches"][0]["path"] == "retained-cinder-storage.yaml"
    hpa = yaml.safe_load(overlay["patches"][1]["patch"])
    assert [p["value"] for p in hpa] == [1, 2]


def test_guided_libvirt_keeps_the_normal_post_renderer_after_talos_adaptation(tmp_path):
    from types import SimpleNamespace
    from app.services.cloud_services import configure, rendered_files
    from app.services.deploy import _pipeline_command

    doc, _ = configure(document(), 'owned-environment')
    files = rendered_files(doc)
    config = tmp_path / 'config'
    root = tmp_path / 'core'
    (config / 'runtime').mkdir(parents=True)
    (root / 'bin').mkdir(parents=True)
    (config / 'runtime/libvirt-talos-post-render.sh').write_text(files['runtime/libvirt-talos-post-render.sh'])
    installer = root / 'bin/install-libvirt.sh'
    original = '#!/bin/bash\nhelm upgrade --post-renderer "$GENESTACK_OVERRIDES_DIR/kustomize/kustomize.sh" --post-renderer-args "libvirt/overlay"\n'
    installer.write_text(original)
    ctx = SimpleNamespace(config_dir=config, genestack_root=root)
    argv = _pipeline_command('bin/install-libvirt.sh', ctx, dry_run=False, local=True)
    adapted = (config / 'runtime/install-libvirt.sh').read_text()
    assert argv == ['bash', str(config / 'runtime/install-libvirt.sh')]
    assert '--post-renderer /bin/bash' in adapted
    assert '--post-renderer-args "libvirt/overlay"' in adapted
    assert installer.read_text() == original
    assert _pipeline_command('bin/install-libvirt.sh', ctx, dry_run=True, local=True) == ['bash', 'bin/install-libvirt.sh']
    installer.write_text('unrecognized installer')
    with pytest.raises(ValueError, match='Unrecognized libvirt'):
        _pipeline_command('bin/install-libvirt.sh', ctx, dry_run=False, local=True)


def test_libvirt_startup_update_replaces_pods_with_subpath_config_mounts():
    import hashlib
    import yaml
    from app.paths import package_root
    from app.services.cloud_services import configure, finish_rendered_files, rendered_files

    doc, _ = configure(document(), 'owned-environment')
    files = rendered_files(doc)
    finish_rendered_files(doc, files)
    overlay = yaml.safe_load(files['kustomize/libvirt/overlay/kustomization.yaml'])
    patch = yaml.safe_load(overlay['patches'][0]['patch'])
    annotations = patch['spec']['template']['metadata']['annotations']
    assert annotations['genestack.dev/libvirt-talos-runtime'] == hashlib.sha256(
        (package_root() / 'app/assets/libvirt-talos-post-render.sh').read_bytes()
    ).hexdigest()


def test_libvirt_shares_only_its_own_runtime_sockets_with_talos():
    import yaml
    from app.services.cloud_services import configure, finish_rendered_files, rendered_files

    doc, _ = configure(document(), 'owned-environment')
    files = rendered_files(doc)
    finish_rendered_files(doc, files)
    overlay = yaml.safe_load(files['kustomize/libvirt/overlay/kustomization.yaml'])
    patch = yaml.safe_load(overlay['patches'][0]['patch'])['spec']['template']['spec']
    volume = next(v for v in patch['volumes'] if v['name'] == 'run')
    assert volume['hostPath'] == {'path': '/run/libvirt', 'type': 'DirectoryOrCreate'}
    for container in patch['containers']:
        assert {'mountPath': '/run', '$patch': 'delete'} in container['volumeMounts']
        assert {'name': 'run', 'mountPath': '/run/libvirt'} in container['volumeMounts']


def test_first_flavor_is_confirmed_and_created_only_for_an_empty_cloud():
    from app.services.cloud_services import ensure_initial_flavor

    class Cloud:
        def __init__(self, existing=()): self.rows = list(existing); self.calls = []
        def list_flavors(self): return self.rows
        def flavor_create(self, **values):
            self.calls.append(values)
            self.rows.append({'id': 'created-id', **values})
            return self.rows[-1]

    doc = document()
    machine = doc['hardware_installation']['reviewed_plan']['machines'][0]
    machine['disks'].append({'purpose': 'boot', 'capacity_bytes': 80 * 1024**3})
    cloud = Cloud()
    result = ensure_initial_flavor(cloud, doc)
    assert result == {'created': True, 'id': 'created-id', 'name': 'gs.small', 'vcpus': 2, 'ram': 2048, 'disk': 16}
    assert ensure_initial_flavor(cloud, doc) == {'created': False}
    assert len(cloud.calls) == 1
    custom = Cloud([{'id': 'custom', 'name': 'customer-size'}])
    assert ensure_initial_flavor(custom, doc) == {'created': False}
    assert not custom.calls
    machine['memory_gib'] = 8
    machine['cpu_threads'] = 4
    machine['disks'][-1]['capacity_bytes'] = 40 * 1024**3
    smaller = ensure_initial_flavor(Cloud(), doc)
    assert (smaller['vcpus'], smaller['ram'], smaller['disk']) == (1, 1024, 10)


def test_first_flavor_requires_confirmation_and_real_boot_capacity():
    from app.services.cloud_services import ensure_initial_flavor

    class Unconfirmed:
        def list_flavors(self): return []
        def flavor_create(self, **values): return {'id': 'not-confirmed'}

    doc = document()
    with pytest.raises(ValueError, match='capacity'):
        ensure_initial_flavor(Unconfirmed(), doc)
    doc['hardware_installation']['reviewed_plan']['machines'][0]['disks'].append({'purpose': 'boot', 'capacity_bytes': 80 * 1024**3})
    with pytest.raises(ValueError, match='confirmed'):
        ensure_initial_flavor(Unconfirmed(), doc)
