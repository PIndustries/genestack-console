import pytest


def machine(name, capacities):
    return {'name': name, 'disks': [{'purpose': 'storage', 'wwid': 'naa.' + str(index + 1).zfill(16),
                                   'capacity_bytes': size * 1024**3} for index, size in enumerate(capacities)]}


def test_storage_sizes_replicas_and_export_for_one_real_data_host():
    from app.services.cluster_storage import proposal
    result = proposal({'machines': [machine('owned', [120])]})
    assert result['replicas'] == 1
    assert result['cinder_capacity_gib'] == 42
    assert result['storage_nodes'] == ['owned']


def test_asymmetric_disks_cannot_overcommit_one_longhorn_replica():
    from app.services.cluster_storage import proposal
    result = proposal({'machines': [machine('large', [2000]), machine('small', [40])]})
    assert result['replicas'] == 2
    assert result['cinder_capacity_gib'] <= 14


def test_missing_reviewed_data_storage_is_not_silently_placed_on_system_disk():
    from app.services.cluster_storage import proposal
    with pytest.raises(ValueError, match='reviewed data'):
        proposal({'machines': [machine('system-only', [])]})



def test_storage_mount_must_match_the_reviewed_wwid_and_real_volume():
    from app.services.cluster_storage import expected_disks, verify_mounts
    doc = {'servers': {'owned': {}}, 'talos': {'install_disk_selectors': {'owned': 'naa.0000000000000001'},
           'data_disk_selectors': {'owned': ['naa.0000000000000002']}},
           'hardware_installation': {'reviewed_plan': {'machines': [machine('owned', [120])]}}}
    doc['hardware_installation']['reviewed_plan']['machines'][0]['disks'][0]['wwid'] = 'naa.0000000000000002'
    expected, = expected_disks(doc, 'owned')
    disk = {'metadata': {'id': 'sdb'}, 'spec': {'dev_path': '/dev/sdb', 'wwid': expected['wwid'], 'size': 120 * 1024**3, 'readonly': False}}
    volume = {'metadata': {'id': expected['id']}, 'spec': {'phase': 'ready', 'parentLocation': '/dev/sdb', 'location': '/dev/sdb1'}}
    mount = {'metadata': {'id': expected['id']}, 'spec': {'source': '/dev/sdb1', 'target': expected['path'], 'filesystem': 'xfs', 'readOnly': False, 'detached': False}}
    verify_mounts(doc, 'owned', [disk], [volume], [mount])
    with pytest.raises(ValueError, match='not mounted'):
        verify_mounts(doc, 'owned', [disk], [volume], [])
    disk['spec']['wwid'] = 'naa.0000000000000001'
    with pytest.raises(ValueError, match='not mounted'):
        verify_mounts(doc, 'owned', [disk], [volume], [mount])


def test_replica_override_is_allowed_only_when_real_nodes_can_supply_it():
    from app.services.cluster_storage import proposal
    plan = {'machines': [machine('one', [120]), machine('two', [120])]}
    assert proposal(plan, replicas=1)['replicas'] == 1
    with pytest.raises(ValueError, match='replicas'):
        proposal(plan, replicas=3)



def test_render_and_later_pipeline_keep_measured_replica_count_and_ownership():
    import yaml

    from app.models import Environment
    from app.services import envconfig
    doc = {'storage': {'replication_factor': 1}}
    env = Environment(id='owned-cloud', name='owned-cloud')
    files = envconfig.render_to_files(doc, env)
    for name in ['longhorn-general-storageclass.yaml', 'longhorn-general-multi-attach-storageclass.yaml']:
        definition = yaml.safe_load(files['manifests/longhorn/' + name])
        assert definition['parameters']['numberOfReplicas'] == '1'
        assert definition['metadata']['labels']['genestack.dev/storage-owner'] == 'owned-cloud'
    assert envconfig.doc_env(doc)['LONGHORN_STORAGE_REPLICAS'] == '1'


def test_existing_owned_longhorn_parameter_maps_are_preserved_for_immutable_classes():
    import yaml

    from app.services import cluster_storage, envconfig
    from app.models import Environment

    owner = 'owned-cloud'
    existing_parameters = {
        'general': {'numberOfReplicas': '2', 'dataLocality': 'best-effort', 'custom': 'kept'},
        'general-multi-attach': {'numberOfReplicas': '2', 'dataLocality': 'best-effort'},
    }
    existing = [
        {'metadata': {'name': name, 'labels': {cluster_storage.OWNER: owner}},
         'provisioner': 'driver.longhorn.io', 'parameters': parameters}
        for name, parameters in existing_parameters.items()
    ]
    configured = cluster_storage.reconcile_existing_storage_classes(
        {'storage': {'replication_factor': 3},
         'hardware_installation': {'reviewed_plan': {'machines': [
             machine('node-a', [120]), machine('node-b', [120]),
         ]}}},
        owner,
        existing,
    )
    assert configured['storage']['replication_factor'] == 2
    assert configured['helm_overrides']['longhorn']['defaultSettings']['defaultReplicaCount'] == 2
    env = Environment(id=owner, name=owner)
    files = envconfig.render_to_files(configured, env)
    for name, expected in existing_parameters.items():
        rendered_name = f'manifests/longhorn/longhorn-{name}-storageclass.yaml'
        assert yaml.safe_load(files[rendered_name])['parameters'] == expected


def test_existing_longhorn_classes_require_matching_owner_and_replicas():
    from app.services import cluster_storage

    owner = 'owned-cloud'
    existing = [
        {'metadata': {'name': name, 'labels': {cluster_storage.OWNER: owner}},
         'provisioner': 'driver.longhorn.io',
         'parameters': {'numberOfReplicas': replicas}}
        for name, replicas in [('general', '2'), ('general-multi-attach', '3')]
    ]
    with pytest.raises(ValueError, match='different replica counts'):
        cluster_storage.reconcile_existing_storage_classes({}, owner, existing)
    existing[0]['metadata']['labels'][cluster_storage.OWNER] = 'other-cloud'
    with pytest.raises(ValueError, match='ownership review'):
        cluster_storage.reconcile_existing_storage_classes({}, owner, existing)


def test_longhorn_participant_selector_is_a_real_label_not_null():
    from app.services.cluster_storage import configure

    doc = {'servers': {'owned': {'roles': ['compute', 'storage']}},
           'talos': {'install_disk_selectors': {'owned': 'naa.0000000000000001'},
                     'data_disk_selectors': {'owned': ['naa.0000000000000002']}},
           'hardware_installation': {'reviewed_plan': {'machines': [machine('owned', [120])]}}}
    doc['hardware_installation']['reviewed_plan']['machines'][0]['disks'][0]['wwid'] = 'naa.0000000000000002'
    configured, plan = configure(doc)
    values = configured['helm_overrides']['longhorn']
    assert values['global']['nodeSelector']['longhorn.io/storage-node'] == 'enabled'
    assert values['longhornUI']['replicas'] == 1
    assert values['csi']['attacherReplicaCount'] == 1
    assert configured['storage']['longhorn_cinder']['capacity_gib'] == 42
    assert plan['replicas'] == 1
