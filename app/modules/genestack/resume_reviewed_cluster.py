"""Continue a failed authorized first install without commissioning disks again."""
from __future__ import annotations

import subprocess
from pathlib import Path

import yaml

from app.modules.params import p

HANDLERS = ('genestack_resume_reviewed_cluster',)
OPERATION = {
    'id': 'genestack.resume_reviewed_cluster', 'name': 'Continue reviewed cluster installation',
    'description': 'Continue the same authorized installation, preserving completed disk wipes and commissioning only unfinished reviewed machines.',
    'required_role': 'admin', 'backend': 'genestack', 'mutating': True,
    'params': [p('installation_job_id', True, 'Stopped installation job in this environment')],
    'handler': 'genestack_resume_reviewed_cluster', 'timeout_seconds': 7200,
}


def run(self, handler, op, job, env, log, ctx, params, deadline, check_cancel,
        dry, timeout, gs_root, ans_root, extra_env, ssh_target, remote_env, executor, agent_env_id):
    from app.services import deploy, envconfig, greenfield, reviewed_cluster

    if env is None or ssh_target or agent_env_id:
        return {'ok': False, 'error': 'Guided installation requires the local deployer appliance', 'returncode': 2}
    try:
        doc, plan, expected_version = reviewed_cluster.resumable_plan(self.db, env, str(params.get('installation_job_id') or ''))
        if dry:
            return {'ok': True, 'dry_run': True, 'hardware_modified': False}
        reviewed_cluster.verify_management_identity(self.db, env, plan, settings=self.settings, check_cancel=check_cancel)
        reviewed_cluster.validate_talos_configuration(doc, env,
            temporary_root=Path(self.settings.data_dir) / 'installation-preflight', log=log)
        check_cancel()
        doc['hardware_installation']['original_installation_job_id'] = doc['hardware_installation'].get('original_installation_job_id') or params['installation_job_id']
        doc['hardware_installation']['installation_job_id'] = job.id
        envconfig.put_version(self.db, env, yaml.safe_dump(doc, sort_keys=False), job.created_by,
                              expected_version=expected_version)
        self.db.commit()
    except (ValueError, OSError, subprocess.TimeoutExpired) as error:
        return {'ok': False, 'error': str(error), 'returncode': 2}
    self.write_audit(actor=job.created_by or 'system', action='env.resume_reviewed_cluster',
                    resource_type='environment', resource_id=env.id, environment_id=env.id,
                    details={'previous_installation_job_id': params['installation_job_id'],
                             'confirmed_machine_ids': [m['id'] for m in plan['machines']]}, success=True)
    log('[installation] Continuing the reviewed plan; completed wipes are preserved and only unfinished machines are commissioned')
    allowed = {
        machine['name']: [disk['wwid'] for disk in machine['disks']]
        for machine in plan['machines']
    }
    result = greenfield.run_greenfield(
        self.db, env, ctx, log, dry_run=False, skip_push=True,
        timeout=timeout, extra_env=extra_env, ssh_target=None,
        remote_env=remote_env, settings=self.settings, deadline=deadline,
        check_cancel=check_cancel, boot='pxe', stop_after='talos',
        replace_hosts=params.get('replace_hosts'),
        wipe_disk_wwids=allowed, preserve_commissioned=True,
    )
    if not result.get('ok'):
        return result
    check_cancel()
    result = deploy.run_deploy(self.db, env, ctx, log, dry_run=False, skip_push=False,
        timeout=timeout, extra_env=extra_env, ssh_target=None, remote_env=remote_env,
        settings=self.settings, from_stage='hosts', until_stage='hosts',
        deadline=deadline, check_cancel=check_cancel)
    result.update(cloud_services_ready=False)
    if result.get('ok'):
        result.update(cluster_bootstrap_complete=True)
    return result
