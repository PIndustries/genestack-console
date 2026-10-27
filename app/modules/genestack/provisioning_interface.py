"""Provision an unused local appliance port without touching the management uplink."""
from __future__ import annotations

from app.modules.params import p

HANDLERS = ('genestack_provisioning_interface',)
OPERATION = {
    'id': 'genestack.provisioning_interface', 'name': 'Set up dedicated machine network',
    'description': 'Assign a private subnet and outbound masquerading to an unused dedicated Ethernet port. Does not start DHCP or reboot machines.',
    'required_role': 'admin', 'backend': 'genestack', 'mutating': True,
    'params': [p('interface', True, 'Unused Ethernet interface'),
               p('fingerprint', True, 'Current inventory fingerprint'),
               p('dedicated_network_confirmed', True, 'Dedicated machine network confirmed', 'boolean')],
    'handler': 'genestack_provisioning_interface', 'timeout_seconds': 90,
}


def run(self, handler, op, job, env, log, ctx, params, deadline, check_cancel,
        dry, timeout, gs_root, ans_root, extra_env, ssh_target, remote_env, executor, agent_env_id):
    from app.services.provisioning_interface import setup

    if env is None or ssh_target or agent_env_id:
        return {'ok': False, 'error': 'Port setup requires the local deployer appliance', 'returncode': 2}
    check_cancel()
    try:
        result = setup(params, dry_run=dry)
    except (ValueError, OSError) as error:
        return {'ok': False, 'error': str(error), 'returncode': 2}
    self.write_audit(actor=job.created_by or 'system', action='env.provisioning_interface',
                    resource_type='environment', resource_id=env.id, environment_id=env.id,
                    details=result, success=True)
    return result
