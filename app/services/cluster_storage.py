"""Size first-cloud storage from reviewed data disks and verify their actual mounts."""
from __future__ import annotations

import copy
from typing import Any

from app.services import managed_cinder, talos_storage

OWNER = 'genestack.dev/storage-owner'


def proposal(plan: dict[str, Any], replicas: int | None = None) -> dict[str, Any]:
    hosts = {}
    for machine in plan.get('machines') or []:
        disks = [d for d in machine['disks'] if d['purpose'] == 'storage']
        if not disks:
            continue
        sizes = [d['capacity_bytes'] for d in disks]
        if any(isinstance(v, bool) or not isinstance(v, int) or v <= 0 for v in sizes):
            raise ValueError('Storage requires measured reviewed data disk capacities')
        hosts[machine['name']] = sizes
    if not hosts:
        raise ValueError('This cloud has no reviewed data disks for persistent storage')
    if replicas is None:
        replicas = min(3, len(hosts))
    if isinstance(replicas, bool) or not isinstance(replicas, int) or not 1 <= replicas <= min(20, len(hosts)):
        raise ValueError('Storage replicas must fit the measured storage nodes')
    total = sum(sum(sizes) for sizes in hosts.values()) // 1024**3
    # One Longhorn replica must fit one physical disk. An asymmetric pool must
    # not size a single export from aggregate capacity alone.
    per_replica = sorted((max(sizes) * 7 // 10 // 1024**3 for sizes in hosts.values()), reverse=True)[replicas - 1]
    capacity = min(total * 7 // 10 // replicas // 2, per_replica // 2)
    if capacity < 8:
        raise ValueError('The reviewed data disks need more usable capacity for cloud storage')
    return {'storage_nodes': sorted(hosts), 'replicas': replicas, 'capacity_gib': total,
            'cinder_capacity_gib': capacity, 'max_export_gib': per_replica}


def configure(doc: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    doc = copy.deepcopy(doc)
    machines = doc['hardware_installation']['reviewed_plan']['machines']
    selected = talos_storage.data_disk_selectors(doc['talos'], doc['servers'])
    for machine in machines:
        reviewed = [d['wwid'] for d in machine['disks'] if d['purpose'] == 'storage']
        if selected is None or selected[machine['name']] != reviewed:
            raise ValueError('The reviewed storage disk selectors changed')
    storage = doc.setdefault('storage', {})
    plan = proposal({'machines': machines}, storage.get('replication_factor'))
    if (storage.get('ceph') or {}).get('enabled'):
        raise ValueError('Review the selected Ceph backend before configuring Longhorn storage')
    storage.setdefault('ceph', {})['enabled'] = False
    storage.setdefault('replication_factor', plan['replicas'])
    if storage['replication_factor'] != plan['replicas']:
        raise ValueError('The storage replication setting differs from the measured cluster')
    storage.setdefault('longhorn_cinder', {'enabled': True, 'capacity_gib': plan['cinder_capacity_gib']})
    managed = managed_cinder.settings(doc)
    if managed is None or managed['capacity_gib'] > plan['max_export_gib']:
        raise ValueError('Review the requested block storage capacity before installation')
    plan['cinder_capacity_gib'] = managed['capacity_gib']
    values = doc.setdefault('helm_overrides', {}).setdefault('longhorn', {})
    values.setdefault('global', {}).setdefault('nodeSelector', {})['longhorn.io/storage-node'] = 'enabled'
    defaults = values.setdefault('defaultSettings', {})
    defaults['createDefaultDiskLabeledNodes'] = True
    defaults['defaultReplicaCount'] = plan['replicas']
    defaults.setdefault('systemManagedComponentsNodeSelector', '')
    values.setdefault('persistence', {})['defaultClass'] = False
    values['persistence']['defaultClassReplicaCount'] = plan['replicas']
    values.setdefault('longhornUI', {}).setdefault('replicas', min(2, len(doc['servers'])))
    csi = values.setdefault('csi', {})
    for name in ['attacherReplicaCount', 'provisionerReplicaCount', 'resizerReplicaCount', 'snapshotterReplicaCount']:
        csi.setdefault(name, min(3, len(doc['servers'])))
    return doc, plan


def expected_disks(doc, hostname):
    documents = talos_storage.node_documents(hostname, doc['talos'], doc['servers'])
    volumes = [d for d in documents if d.get('kind') == 'UserVolumeConfig']
    return [{'id': 'u-' + d['name'], 'name': d['name'], 'path': '/var/mnt/' + d['name'],
             'wwid': identity} for d, identity in zip(volumes, doc['talos']['data_disk_selectors'][hostname], strict=True)]


def verify_mounts(doc, hostname, disks, volumes, mounts):
    indexed = lambda values: {v['metadata']['id']: v['spec'] for v in values if isinstance(v, dict)}
    disk_specs, volume_specs, mount_specs = indexed(disks), indexed(volumes), indexed(mounts)
    machine = next(m for m in doc['hardware_installation']['reviewed_plan']['machines'] if m['name'] == hostname)
    sizes = {d['wwid']: d['capacity_bytes'] for d in machine['disks'] if d['purpose'] == 'storage'}
    for expected in expected_disks(doc, hostname):
        disk = next((d for d in disk_specs.values() if d.get('wwid') == expected['wwid']), None)
        volume, mount = volume_specs.get(expected['id'], {}), mount_specs.get(expected['id'], {})
        if (disk is None or disk.get('readonly') or disk.get('size') != sizes[expected['wwid']]
            or volume.get('phase') != 'ready' or volume.get('parentLocation') != disk.get('dev_path')
            or mount.get('source') != volume.get('location') or mount.get('target') != expected['path']
            or mount.get('filesystem') != 'xfs' or mount.get('readOnly', True) or mount.get('detached', True)):
            raise ValueError('A reviewed data disk is not mounted as its verified persistent volume')


def storage_classes(environment_id, replicas):
    return [{'apiVersion': 'storage.k8s.io/v1', 'kind': 'StorageClass',
             'metadata': {'name': name, 'labels': {OWNER: environment_id},
                          'annotations': {'storageclass.kubernetes.io/is-default-class': default}},
             'provisioner': 'driver.longhorn.io', 'allowVolumeExpansion': True,
             'reclaimPolicy': 'Delete', 'volumeBindingMode': 'Immediate',
             'parameters': {'numberOfReplicas': str(replicas), 'dataLocality': 'best-effort',
                            'staleReplicaTimeout': '2880', 'fromBackup': '', 'fsType': 'ext4'}}
            for name, default in [('general', 'true'), ('general-multi-attach', 'false')]]
