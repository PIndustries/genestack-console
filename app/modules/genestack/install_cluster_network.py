"""Install the first cluster network through the same ordinary scoped job path."""
from __future__ import annotations

import json
import time

HANDLERS = ('genestack_install_cluster_network',)
OPERATION = {
    'id': 'genestack.install_cluster_network', 'name': 'Install cluster networking',
    'description': 'Install the configured kube-ovn network on the reviewed cluster and verify that its nodes become Ready.',
    'required_role': 'admin', 'backend': 'genestack', 'mutating': True,
    'params': [], 'handler': 'genestack_install_cluster_network', 'timeout_seconds': 3600,
}


def run(self, handler, op, job, env, log, ctx, params, deadline, check_cancel,
        dry, timeout, gs_root, ans_root, extra_env, ssh_target, remote_env, executor, agent_env_id):
    from app.services import cluster_network, envconfig, reviewed_cluster, talos
    from app.services import genestack_bridge as bridge

    if env is None or ssh_target or agent_env_id or ctx.config_dir is None:
        return {'ok': False, 'error': 'Guided cluster networking requires the local deployer appliance', 'returncode': 2}
    try:
        doc, version = cluster_network.configuration(self.db, env)
        if dry:
            return {'ok': True, 'dry_run': True, 'message': 'Would install cluster networking; nothing changed'}
        path = ctx.ensure_kubeconfig()
        if not path:
            raise ValueError('This cloud has no stored cluster connection')
        scoped_env = dict(extra_env or {}, KUBECONFIG=path)
        def command(argv, limit=timeout):
            check_cancel()
            remaining = int((deadline or (time.monotonic() + limit)) - time.monotonic())
            if remaining <= 0:
                raise ValueError('Cluster network installation deadline exceeded')
            return bridge.run_command(argv, cwd=gs_root, timeout=min(limit, remaining),
                                      dry_run=False, extra_env=scoped_env, log=log)
        result = command(['kubectl', 'get', 'nodes', '-o', 'json', '--request-timeout=20s'], 30)
        if result.get('returncode') != 0:
            raise ValueError('Cannot read this cloud’s Kubernetes nodes before networking')
        control_planes = cluster_network.verified_nodes(doc, json.loads(result.get('stdout') or '{}'))
        if envconfig.get_current(self.db, env)[1].version != version:
            raise ValueError('Configuration changed; review the cloud before continuing')
        check_cancel()
        reviewed_cluster.validate_talos_configuration(doc, env,
            temporary_root=self.settings.data_dir / 'installation-preflight', log=log)
        for name in sorted(doc['servers']):
            result = command([talos.talosctl_command(), 'patch', 'machineconfig',
                '--nodes', doc['servers'][name]['ip'], '--endpoints', doc['servers'][name]['ip'],
                '--talosconfig', str(ctx.config_dir / 'talos' / 'talosconfig'),
                '--patch', talos.firewall_yaml(doc), '--mode=no-reboot'], 60)
            if result.get('returncode') != 0:
                raise ValueError('Could not reconcile the reviewed cluster network policy without rebooting')
        files = envconfig.render_to_files(doc, env, self.settings)
        envconfig.push_rendered(files, ctx, log, False)
        envconfig.ensure_kustomize_layout(ctx.config_dir, gs_root, log)
        envconfig.ensure_runtime_tree(ctx.config_dir, gs_root, log)
        for argv in [
            ['kubectl', 'label', 'nodes', *control_planes, 'kube-ovn/role=master', '--overwrite'],
            ['kubectl', 'label', 'nodes', *sorted(doc['servers']), 'ovn.kubernetes.io/ovs_dp_type=kernel', '--overwrite'],
        ]:
            result = command(argv, 60)
            if result.get('returncode') != 0:
                raise ValueError('Could not label the reviewed Kubernetes nodes for networking')
        check_cancel()
        remaining = int((deadline or (time.monotonic() + timeout)) - time.monotonic())
        result = bridge.enable_service('kube-ovn', gs_root, dry_run=False, timeout=max(1, remaining),
                                       extra_env=scoped_env, log=log)
        if not result.get('ok'):
            return result
        result = command(['kubectl', '-n', 'kube-system', 'rollout', 'status', 'deployment/kube-ovn-controller', '--timeout=600s'], 610)
        if result.get('returncode') != 0:
            raise ValueError('Cluster network controller did not become available; inspect this job’s logs')
        result = command(['kubectl', 'wait', '--for=condition=Ready', *['node/' + name for name in sorted(doc['servers'])], '--timeout=600s'], 610)
        if result.get('returncode') != 0:
            raise ValueError('The reviewed Kubernetes nodes did not become Ready after network installation')
        result = command(['kubectl', '-n', 'kube-system', 'rollout', 'status', 'deployment/coredns', '--timeout=300s'], 310)
        if result.get('returncode') != 0:
            raise ValueError('Cluster DNS did not become available after networking; inspect the job logs')
        result = command(['kubectl', '-n', 'kube-system', 'rollout', 'status', 'daemonset/kube-ovn-pinger', '--timeout=300s'], 310)
        if result.get('returncode') != 0:
            raise ValueError('The pod network diagnostic did not become available')
        service = command(['kubectl', '-n', 'default', 'get', 'service', 'kubernetes', '-o', 'json'], 30)
        if service.get('returncode') != 0:
            raise ValueError('Cannot verify the cluster API service for DNS checks')
        service_ip = json.loads(service.get('stdout') or '{}').get('spec', {}).get('clusterIP')
        resolved = command(['kubectl', '-n', 'kube-system', 'exec', 'daemonset/kube-ovn-pinger', '-c', 'pinger',
                            '--', 'getent', 'hosts', 'kubernetes.default.svc.cluster.local'], 60)
        answers = {line.split()[0] for line in (resolved.get('stdout') or '').splitlines() if line.split()}
        if resolved.get('returncode') != 0 or not service_ip or service_ip not in answers:
            raise ValueError('Pod DNS did not resolve the actual cluster API service')
        log('[networking] Pod DNS resolved the actual Kubernetes API service')
        self.write_audit(actor=job.created_by or 'system', action='env.install_cluster_network',
                        resource_type='environment', resource_id=env.id, environment_id=env.id,
                        details={'config_version': version, 'nodes': sorted(doc['servers'])}, success=True)
        return {'ok': True, 'dry_run': False, 'cluster_network_ready': True, 'cloud_services_ready': False,
                'message': 'Cluster networking is ready. Continue storage and OpenStack setup'}
    except (ValueError, OSError) as error:
        return {'ok': False, 'error': str(error), 'returncode': 2, 'dry_run': False}
