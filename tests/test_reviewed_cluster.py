"""A first install must materialize its reviewed targets without guessing or early writes."""
import copy

import pytest
import yaml

from app.db import SessionLocal
from app.models import BaremetalNode, Environment
from app.services import envconfig, reviewed_cluster, talos_storage
from tests.test_hardware_installation_save import stored_machine


@pytest.fixture
def reviewed(client, admin_headers):
    env, base, identity, _ = stored_machine(client, admin_headers)
    doc = yaml.safe_load(client.get(base + '/config', headers=admin_headers).json()['yaml'])
    doc['pxe']['dns'] = '1.1.1.1'
    assert client.put(base + '/config', headers=admin_headers,
                      json={'yaml_text': yaml.safe_dump(doc), 'expected_version': 1}).status_code == 201
    plan = client.get(base + '/baremetal/installation-plan', headers=admin_headers).json()
    assert client.put(base + '/config/installation-plan', headers=admin_headers,
                      json={'fingerprint': plan['fingerprint'], 'expected_version': plan['config_version']}).status_code == 200
    plan = client.get(base + '/baremetal/installation-plan', headers=admin_headers).json()
    return env['id'], identity, plan, {'fingerprint': plan['fingerprint'], 'expected_version': plan['config_version'],
        'confirmed_machine_ids': [identity], 'erase_reviewed_disks_confirmed': True}


def test_materialization_uses_exact_review_and_does_not_write_config_or_disks(reviewed):
    eid, nid, plan, params = reviewed
    with SessionLocal() as db:
        env = db.get(Environment, eid)
        doc, _ = reviewed_cluster.configuration(db, env, params)
        machine, = plan['machines']
        assert doc['servers'][machine['name']]['roles'] == machine['roles']
        assert doc['servers'][machine['name']]['private_mac'] == machine['mac']
        assert doc['talos']['install_disk_selectors'] == {machine['name']: 'naa.6000000000000001'}
        assert doc['talos']['data_disk_selectors'] == {machine['name']: ['naa.6000000000000002']}
        assert doc['talos']['nameservers'] == ['1.1.1.1'] and doc['talos']['cni'] == 'kube-ovn'
        assert doc['network']['gateway'] == '10.77.27.1'
        assert doc['helm_overrides']['longhorn']['defaultSettings']['defaultReplicaCount'] == 1
        assert envconfig.get_current(db, env)[1].version == params['expected_version']
        node = db.get(BaremetalNode, nid)
        assert node.wiped_at is None and node.next_boot == 'disk' and node.commission_token is None


@pytest.mark.parametrize('change', [{'erase_reviewed_disks_confirmed': False}, {'expected_version': True},
    {'fingerprint': 'stale'}, {'confirmed_machine_ids': []}, {'confirmed_machine_ids': ['foreign']},
    {'confirmed_machine_ids': [{}]}])
def test_stale_or_incomplete_consent_is_rejected_before_any_mutation(reviewed, change):
    eid, _, _, params = reviewed
    with SessionLocal() as db:
        env = db.get(Environment, eid)
        with pytest.raises(ValueError):
            reviewed_cluster.configuration(db, env, dict(params, **change))
        assert envconfig.get_current(db, env)[1].version == params['expected_version']


def test_existing_cluster_is_not_treated_as_first_install(reviewed):
    eid, _, _, params = reviewed
    with SessionLocal() as db:
        env = db.get(Environment, eid)
        env.kubeconfig_path = '/fixture/already-existing-kubeconfig'
        with pytest.raises(ValueError, match='Existing clusters'):
            reviewed_cluster.configuration(db, env, params)


def test_changed_bmc_identity_cannot_retarget_a_saved_review(reviewed, monkeypatch):
    from app.services import redfish

    eid, nid, plan, _ = reviewed
    with SessionLocal() as db:
        env = db.get(Environment, eid)
        node = db.get(BaremetalNode, nid)
        changed = copy.deepcopy(node.hardware_report)
        changed['serial'] = 'DIFFERENT-MACHINE'
        monkeypatch.setattr(redfish, 'hardware_inventory', lambda *a, **k: changed)
        with pytest.raises(ValueError, match='management target changed'):
            reviewed_cluster.verify_management_identity(db, env, plan, settings=None, check_cancel=lambda: None)
        assert node.wiped_at is None


def test_management_identity_accepts_case_insensitive_bmc_mac(reviewed, monkeypatch):
    from app.services import redfish

    eid, nid, plan, _ = reviewed
    with SessionLocal() as db:
        env = db.get(Environment, eid)
        node = db.get(BaremetalNode, nid)
        fresh = copy.deepcopy(node.hardware_report)
        fresh['nics'] = [
            {**nic, 'mac': str(nic.get('mac') or '').upper()}
            for nic in fresh.get('nics', [])
        ]
        monkeypatch.setattr(redfish, 'hardware_inventory', lambda *a, **k: fresh)
        reviewed_cluster.verify_management_identity(
            db, env, plan, settings=None, check_cancel=lambda: None
        )
        assert node.wiped_at is None


def test_authorization_is_versioned_and_exposes_existing_job_instead_of_duplicate_start(reviewed):
    eid, _, plan, params = reviewed
    with SessionLocal() as db:
        env = db.get(Environment, eid)
        doc, _ = reviewed_cluster.configuration(db, env, params)
        reviewed_cluster.authorize(db, env, params, doc, plan, actor='fixture', job_id='fixture-install-job')
        latest = envconfig.get_current(db, env)
        assert latest[1].version == params['expected_version'] + 1
        assert latest[0]['hardware_installation']['installation_job_id'] == 'fixture-install-job'
        with pytest.raises(ValueError):
            reviewed_cluster.configuration(db, env, params)


def test_data_volumes_are_stable_and_never_select_boot_disk():
    servers = {'node': {}}
    config = {'install_disk_selectors': {'node': 'naa.6000000000000001'},
              'data_disk_selectors': {'node': ['naa.6000000000000002']}}
    documents = talos_storage.node_documents('node', config, servers)
    volume = documents[0]
    assert volume['kind'] == 'UserVolumeConfig'
    assert volume['provisioning']['diskSelector']['match'] == 'disk.wwid == "naa.6000000000000002"'
    assert volume['provisioning']['grow'] is True
    assert documents[-1]['labels']['node.longhorn.io/create-default-disk'] == 'config'
    assert talos_storage.node_documents('node', config, servers) == documents
    config['data_disk_selectors']['node'] = ['0x6000000000000001']
    with pytest.raises(ValueError, match='reuse a boot disk'):
        talos_storage.node_documents('node', config, servers)


@pytest.fixture
def stopped_installation(reviewed):
    from datetime import UTC, datetime

    from app.models import Job, JobStatus

    eid, nid, plan, params = reviewed
    with SessionLocal() as db:
        env = db.get(Environment, eid)
        doc, _ = reviewed_cluster.configuration(db, env, params)
        previous = Job(environment_id=eid, operation='genestack.install_reviewed_cluster',
                       status=JobStatus.failed, created_by='fixture')
        db.add(previous); db.flush()
        reviewed_cluster.authorize(db, env, params, doc, plan, actor='fixture', job_id=previous.id)
        node = db.get(BaremetalNode, nid)
        node.wiped_at = datetime.now(UTC)
        db.commit()
        return eid, nid, previous.id


def test_resume_preserves_existing_commissioning_and_configuration(stopped_installation):
    eid, nid, previous = stopped_installation
    with SessionLocal() as db:
        env = db.get(Environment, eid)
        node = db.get(BaremetalNode, nid)
        wiped = node.wiped_at
        doc, plan, version = reviewed_cluster.resumable_plan(db, env, previous)
        assert version == envconfig.get_current(db, env)[1].version
        assert doc['hardware_installation']['installation_job_id'] == previous
        assert [m['id'] for m in plan['machines']] == [nid]
        assert node.wiped_at == wiped and node.commission_token is None
        assert node.next_boot == 'disk'


@pytest.mark.parametrize('change', ['running', 'other-environment', 'other-operation', 'unwiped', 'changed-selector', 'changed-mac'])
def test_resume_rejects_unowned_active_or_changed_targets(stopped_installation, change):
    from app.models import Job, JobStatus

    eid, nid, previous = stopped_installation
    with SessionLocal() as db:
        env = db.get(Environment, eid)
        job = db.get(Job, previous)
        node = db.get(BaremetalNode, nid)
        if change == 'running': job.status = JobStatus.running
        if change == 'other-environment': job.environment_id = None
        if change == 'other-operation': job.operation = 'baremetal.inspect'
        if change == 'unwiped': node.wiped_at = None
        if change == 'changed-mac': node.pxe_mac = '52:54:00:00:00:ff'
        if change == 'changed-selector':
            doc, current = envconfig.get_current(db, env)
            doc = copy.deepcopy(doc)
            name = node.name
            doc['talos']['install_disk_selectors'][name] = 'naa.60000000000000ff'
            envconfig.put_version(db, env, yaml.safe_dump(doc), 'fixture', expected_version=current.version)
        db.commit()
        with pytest.raises(ValueError): reviewed_cluster.resumable_plan(db, env, previous)


@pytest.mark.parametrize('maintenance_ready', [False, True])
def test_resume_handler_never_recommissions_and_keeps_original_wipe(stopped_installation, monkeypatch, tmp_path, maintenance_ready):
    from types import SimpleNamespace

    from app.models import Job, JobStatus
    from app.modules.genestack import resume_reviewed_cluster
    from app.services import baremetal, deploy, greenfield

    eid, nid, previous = stopped_installation
    def forbidden(*args, **kwargs): raise AssertionError('Resume must never commission or wipe')
    monkeypatch.setattr(baremetal, 'begin_commission', forbidden)
    monkeypatch.setattr(greenfield, 'run_greenfield', forbidden)
    monkeypatch.setattr(reviewed_cluster, 'verify_management_identity', lambda *a, **k: None)
    monkeypatch.setattr(reviewed_cluster, 'validate_talos_configuration', lambda *a, **k: None)
    calls = []
    def boot(db, env, node, target, **kwargs):
        calls.append(target)
        return {'ok': True}
    monkeypatch.setattr(baremetal, 'set_next_boot', boot)
    probes = iter([maintenance_ready, True])
    monkeypatch.setattr(baremetal, 'talos_api_ready', lambda *a: next(probes))
    monkeypatch.setattr(deploy, 'run_deploy', lambda *a, **k: {'ok': True})
    with SessionLocal() as db:
        env = db.get(Environment, eid)
        node = db.get(BaremetalNode, nid)
        wiped = node.wiped_at
        job = Job(environment_id=eid, operation='genestack.resume_reviewed_cluster', status=JobStatus.running, created_by='fixture')
        db.add(job); db.flush()
        runner = SimpleNamespace(db=db, settings=SimpleNamespace(data_dir=tmp_path), write_audit=lambda **k: None)
        result = resume_reviewed_cluster.run(runner, None, None, job, env, lambda s: None, None,
            {'installation_job_id': previous}, None, lambda: None, False, 7200, None, None, {}, None, None, None, None)
        assert result['ok'] is True and result['cluster_bootstrap_complete'] is True
        assert result['cloud_services_ready'] is False
        assert calls == ([] if maintenance_ready else ['talos'])
        assert node.wiped_at == wiped and node.commission_token is None
        record = envconfig.get_current(db, env)[0]['hardware_installation']
        assert record['original_installation_job_id'] == previous and record['installation_job_id'] == job.id



def test_concurrent_config_change_during_resume_preflight_prevents_boot(stopped_installation, monkeypatch, tmp_path):
    from types import SimpleNamespace

    from app.models import Job, JobStatus
    from app.modules.genestack import resume_reviewed_cluster
    from app.services import baremetal

    eid, nid, previous = stopped_installation
    with SessionLocal() as db:
        env = db.get(Environment, eid)
        node = db.get(BaremetalNode, nid)
        wiped = node.wiped_at
        job = Job(environment_id=eid, operation='genestack.resume_reviewed_cluster', status=JobStatus.running, created_by='fixture')
        db.add(job); db.flush()
        monkeypatch.setattr(reviewed_cluster, 'verify_management_identity', lambda *a, **k: None)
        def concurrent_edit(*args, **kwargs):
            doc, current = envconfig.get_current(db, env)
            doc = copy.deepcopy(doc); doc['network']['gateway'] = '10.77.27.2'
            envconfig.put_version(db, env, yaml.safe_dump(doc), 'another-admin', expected_version=current.version)
            db.commit()
        monkeypatch.setattr(reviewed_cluster, 'validate_talos_configuration', concurrent_edit)
        monkeypatch.setattr(baremetal, 'set_next_boot', lambda *a, **k: pytest.fail('Stale snapshot must never boot'))
        runner = SimpleNamespace(db=db, settings=SimpleNamespace(data_dir=tmp_path), write_audit=lambda **k: pytest.fail('Must not authorize'))
        result = resume_reviewed_cluster.run(runner, None, None, job, env, lambda s: None, None,
            {'installation_job_id': previous}, None, lambda: None, False, 7200, None, None, {}, None, None, None, None)
        assert result['ok'] is False and 'changed' in result['error']
        doc, _ = envconfig.get_current(db, env)
        assert doc['network']['gateway'] == '10.77.27.2'
        assert doc['hardware_installation']['installation_job_id'] == previous
        assert node.wiped_at == wiped


def test_network_install_requires_successful_reviewed_bootstrap_and_own_connection(stopped_installation):
    from app.models import Job, JobStatus
    from app.services import cluster_network

    eid, _, previous = stopped_installation
    with SessionLocal() as db:
        env = db.get(Environment, eid)
        with pytest.raises(ValueError, match='installation before networking'):
            cluster_network.configuration(db, env)
        env.kubeconfig_data = 'opaque-fixture-not-a-real-credential'
        with pytest.raises(ValueError, match='must finish'):
            cluster_network.configuration(db, env)
        db.get(Job, previous).status = JobStatus.success
        db.commit()
        doc, version = cluster_network.configuration(db, env)
        assert version == envconfig.get_current(db, env)[1].version
        assert doc['hardware_installation']['installation_job_id'] == previous
        doc = copy.deepcopy(doc)
        doc['servers'][next(iter(doc['servers']))]['ip'] = '192.0.2.99'
        envconfig.put_version(db, env, yaml.safe_dump(doc), 'fixture', expected_version=version)
        db.commit()
        with pytest.raises(ValueError, match='assignments changed'):
            cluster_network.configuration(db, env)
