"""Install persistent storage on mounted reviewed data disks, then prepare Cinder."""
from __future__ import annotations

import json
import time

import yaml
from sqlalchemy import select

from app.models import Job, JobStatus

HANDLERS = ('genestack_install_cluster_storage',)
OPERATION = {'id': 'genestack.install_cluster_storage', 'name': 'Install reviewed cloud storage',
    'description': 'Use only the reviewed persistent data volumes, size replicas for this cluster, and prepare retained block storage for OpenStack.',
    'required_role': 'admin', 'backend': 'genestack', 'mutating': True, 'params': [],
    'handler': 'genestack_install_cluster_storage', 'timeout_seconds': 7200}


def run(self, handler, op, job, env, log, ctx, params, deadline, check_cancel,
        dry, timeout, gs_root, ans_root, extra_env, ssh_target, remote_env, executor, agent_env_id):
    from app.services import (
        cluster_network,
        cluster_storage,
        envconfig,
        managed_cinder,
        talos,
        talos_prepare,
    )
    from app.services import genestack_bridge as bridge

    if env is None or ssh_target or agent_env_id or ctx.config_dir is None:
        return {'ok': False, 'error': 'Guided storage requires the local deployer appliance', 'returncode': 2}
    try:
        original_doc, version = cluster_network.configuration(self.db, env)
        network = self.db.scalar(select(Job).where(Job.environment_id == env.id,
            Job.operation == 'genestack.install_cluster_network').order_by(Job.created_at.desc()).limit(1))
        if network is None or network.status != JobStatus.success or network.dry_run is not False:
            raise ValueError('Complete the real cluster networking check before persistent storage')
        doc, plan = cluster_storage.configure(original_doc)
        if dry:
            return {'ok': True, 'dry_run': True, 'proposal': plan, 'hardware_modified': False}
        path = ctx.ensure_kubeconfig()
        if not path:
            raise ValueError('This cloud has no stored Kubernetes connection')
        scoped_env = dict(extra_env or {}, KUBECONFIG=path)
        def command(argv, limit=60, body=None):
            check_cancel()
            remaining = int((deadline or (time.monotonic() + limit)) - time.monotonic())
            if remaining <= 0:
                raise ValueError('Cloud storage installation deadline exceeded')
            return bridge.run_command(argv, cwd=gs_root, timeout=min(limit, remaining),
                dry_run=False, extra_env=scoped_env, input_text=body, log=log)
        def checked(argv, limit=60, body=None):
            result = command(argv, limit, body)
            if result.get('returncode') != 0:
                raise ValueError('Cloud storage step failed: ' + ' '.join(argv[:2] if argv[0] == 'python3' else argv[:5]))
            return result.get('stdout') or ''
        response = json.loads(checked(['kubectl', 'get', 'nodes', '-o', 'json']))
        cluster_network.verified_nodes(doc, response)
        if any(not any(c.get('type') == 'Ready' and c.get('status') == 'True'
                       for c in n.get('status', {}).get('conditions', [])) for n in response['items']):
            raise ValueError('Every reviewed Kubernetes node must be Ready before storage installation')
        ctl = talos.talosctl_command()
        config = str(ctx.config_dir / 'talos' / 'talosconfig')
        for name in sorted(doc['servers']):
            base = [ctl, '--nodes', doc['servers'][name]['ip'], '--endpoints', doc['servers'][name]['ip'], '--talosconfig', config]
            services = checked([*base, 'services'])
            if not talos_prepare.iscsi_running(talos_prepare.parse_services(services)):
                raise ValueError('The reviewed Talos node needs its iSCSI extension running before storage')
            if name not in plan['storage_nodes']:
                continue
            reports = []
            for kind in ['disks', 'volumestatus', 'mountstatus']:
                reports.append([v for v in yaml.safe_load_all(checked([*base, 'get', kind, '-o', 'yaml'])) if isinstance(v, dict)])
            cluster_storage.verify_mounts(doc, name, *reports)
            node = next(n for n in response['items'] if n['metadata']['name'] == name)
            actual = json.loads(node['metadata'].get('annotations', {}).get('node.longhorn.io/default-disks-config', '[]'))
            expected_paths = {d['path'] for d in cluster_storage.expected_disks(doc, name)}
            if ({d.get('path') for d in actual} != expected_paths
                or node['metadata'].get('labels', {}).get('node.longhorn.io/create-default-disk') != 'config'):
                raise ValueError('The reviewed persistent disk paths are missing from the node configuration')
        classes = cluster_storage.storage_classes(env.id, plan['replicas'])
        for desired in classes:
            existing = checked(['kubectl', 'get', 'storageclass', desired['metadata']['name'], '--ignore-not-found', '-o', 'json'])
            if existing:
                existing = json.loads(existing)
                if (existing.get('metadata', {}).get('labels', {}).get(cluster_storage.OWNER) != env.id
                    or existing.get('provisioner') != desired['provisioner'] or existing.get('parameters') != desired['parameters']):
                    raise ValueError('An existing storage class requires review; automatic replacement is forbidden')
        if envconfig.get_current(self.db, env)[1].version != version:
            raise ValueError('Configuration changed; review storage before continuing')
        if doc != original_doc:
            envconfig.put_version(self.db, env, yaml.safe_dump(doc, sort_keys=False), job.created_by,
                                  expected_version=version)
            self.db.commit()
        log(f"[storage] Reviewed data disks verified; {plan['replicas']} replica(s), {plan['cinder_capacity_gib']} GiB initial block storage")
        envconfig.push_rendered(envconfig.render_to_files(doc, env, self.settings), ctx, log, False)
        envconfig.ensure_kustomize_layout(ctx.config_dir, gs_root, log)
        envconfig.ensure_runtime_tree(ctx.config_dir, gs_root, log)
        for name, privileged in [('longhorn-system', True), ('openstack', True)]:
            metadata = {'name': name}
            if privileged:
                metadata['labels'] = {'pod-security.kubernetes.io/enforce': 'privileged'}
            checked(['kubectl', 'apply', '-f', '-'], body=json.dumps({'apiVersion': 'v1', 'kind': 'Namespace', 'metadata': metadata}))
        checked(['kubectl', 'label', 'nodes', *sorted(doc['servers']), 'longhorn.io/storage-node=enabled', '--overwrite'])
        check_cancel()
        remaining = int((deadline or (time.monotonic() + timeout)) - time.monotonic())
        if remaining <= 0:
            raise ValueError('Storage installation deadline exceeded')
        result = bridge.enable_service('longhorn', gs_root, dry_run=False, timeout=remaining,
                                       extra_env=scoped_env, log=log)
        if not result.get('ok'):
            return result
        checked(['kubectl', '-n', 'longhorn-system', 'rollout', 'status', 'daemonset/longhorn-manager', '--timeout=600s'], 610)
        checked(['kubectl', '-n', 'longhorn-system', 'rollout', 'status', 'deployment/longhorn-driver-deployer', '--timeout=600s'], 610)
        checked(['kubectl', 'apply', '-f', '-'], body=json.dumps({'apiVersion': 'v1', 'kind': 'List', 'items': classes}))
        for name in plan['storage_nodes']:
            checked(['kubectl', '-n', 'longhorn-system', 'wait', '--for=condition=Ready', 'nodes.longhorn.io/' + name, '--timeout=300s'], 310)
            node = json.loads(checked(['kubectl', '-n', 'longhorn-system', 'get', 'nodes.longhorn.io', name, '-o', 'json']))
            for expected in cluster_storage.expected_disks(doc, name):
                disk = node.get('spec', {}).get('disks', {}).get(expected['name'], {})
                if disk.get('path') != expected['path'] or disk.get('allowScheduling') is not True:
                    raise ValueError('Longhorn is not using the reviewed persistent data path')
        prepared = managed_cinder.prepare_command(doc, env.id, str(ctx.config_dir))
        if prepared is None:
            raise ValueError('The reviewed block storage export is missing')
        checked(prepared, min(900, timeout))
        self.write_audit(actor=job.created_by or 'system', action='env.install_cluster_storage',
            resource_type='environment', resource_id=env.id, environment_id=env.id,
            details={'replicas': plan['replicas'], 'cinder_capacity_gib': plan['cinder_capacity_gib'],
                     'storage_nodes': plan['storage_nodes']}, success=True)
        return {'ok': True, 'dry_run': False, 'cluster_storage_ready': True, 'cloud_services_ready': False,
                'message': 'Persistent storage and the retained block storage export are ready. Continue OpenStack setup'}
    except (ValueError, OSError) as error:
        return {'ok': False, 'dry_run': False, 'error': str(error), 'returncode': 2}
