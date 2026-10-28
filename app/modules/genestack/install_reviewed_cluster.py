"""Explicit first-cluster installation from a saved, verified native review."""
from __future__ import annotations

import subprocess
from pathlib import Path

from app.modules.params import p

HANDLERS = ('genestack_install_reviewed_cluster',)
OPERATION = {
    'id': 'genestack.install_reviewed_cluster', 'name': 'Install reviewed cluster',
    'description': 'Erase only the reviewed disks, install Talos and bootstrap Kubernetes. Continue cloud networking and OpenStack services afterward.',
    'required_role': 'admin', 'backend': 'genestack', 'mutating': True,
    'params': [p('expected_version', True, 'Reviewed configuration version', 'integer'),
               p('fingerprint', True, 'Current saved plan fingerprint'),
               p('confirmed_machine_ids', True, 'Exactly the reviewed machine UUIDs', 'array'),
               p('erase_reviewed_disks_confirmed', True, 'Explicit consent to erase the reviewed disks', 'boolean')],
    'handler': 'genestack_install_reviewed_cluster', 'timeout_seconds': 7200,
}


def run(self, handler, op, job, env, log, ctx, params, deadline, check_cancel,
        dry, timeout, gs_root, ans_root, extra_env, ssh_target, remote_env, executor, agent_env_id):
    from app.services import deploy, greenfield, reviewed_cluster

    if env is None or ssh_target or agent_env_id:
        return {'ok': False, 'error': 'Guided cluster installation requires the local deployer appliance', 'returncode': 2}
    try:
        doc, plan = reviewed_cluster.configuration(self.db, env, params)
        if dry:
            return {'ok': True, 'dry_run': True, 'hardware_modified': False,
                    'message': 'Would install the reviewed cluster; no configuration saved or disks changed'}
        check_cancel()
        reviewed_cluster.verify_management_identity(self.db, env, plan, settings=self.settings, check_cancel=check_cancel)
        reviewed_cluster.validate_talos_configuration(doc, env,
            temporary_root=Path(self.settings.data_dir) / 'installation-preflight', log=log)
        check_cancel()
        reviewed_cluster.authorize(self.db, env, params, doc, plan, actor=job.created_by or 'system', job_id=job.id)
    except (ValueError, OSError, subprocess.TimeoutExpired) as error:
        return {'ok': False, 'error': str(error), 'returncode': 2, 'hardware_modified': False}
    self.write_audit(actor=job.created_by or 'system', action='env.install_reviewed_cluster',
                    resource_type='environment', resource_id=env.id, environment_id=env.id,
                    details={'config_version': params['expected_version'], 'fingerprint': params['fingerprint'],
                             'confirmed_machine_ids': params['confirmed_machine_ids']}, success=True)
    allowed = {m['name']: [d['wwid'] for d in m['disks']] for m in plan['machines']}
    result = greenfield.run_greenfield(self.db, env, ctx, log, dry_run=False, skip_push=True,
        timeout=timeout, extra_env=extra_env, ssh_target=None, remote_env=remote_env,
        settings=self.settings, deadline=deadline, check_cancel=check_cancel,
        boot='pxe', stop_after='talos', wipe_disk_wwids=allowed)
    if not result.get('ok'):
        return result
    check_cancel()
    result = deploy.run_deploy(self.db, env, ctx, log, dry_run=False, skip_push=False,
        timeout=timeout, extra_env=extra_env, ssh_target=None, remote_env=remote_env,
        settings=self.settings, from_stage='hosts', until_stage='hosts',
        deadline=deadline, check_cancel=check_cancel)
    result.update(cloud_services_ready=False)
    if result.get('ok'):
        result.update(cluster_bootstrap_complete=True,
                      message='Cluster control plane bootstrapped. Continue cloud networking and OpenStack setup')
    return result
