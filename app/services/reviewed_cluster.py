"""Materialize a confirmed first-install plan; validate it before any disk operation."""
from __future__ import annotations

import copy
import ipaddress
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import yaml

from app.services import envconfig, hardware_installation, talos


def configuration(db, env, params: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    if params.get('erase_reviewed_disks_confirmed') is not True:
        raise ValueError('Confirm erasing the exact reviewed disks before installation')
    current = envconfig.get_current(db, env)
    plan = hardware_installation.for_environment(db, env)
    expected = params.get('expected_version')
    if (isinstance(expected, bool) or not isinstance(expected, int) or current is None
        or expected != current[1].version or params.get('fingerprint') != plan['fingerprint']):
        raise ValueError('The configuration or hardware changed. Review and save the plan again')
    if not plan['saved_plan_matches'] or plan['blockers'] or not plan['machines']:
        raise ValueError('Save a verified installation plan before starting')
    identifiers = params.get('confirmed_machine_ids')
    if (not isinstance(identifiers, list) or any(not isinstance(value, str) for value in identifiers)
        or len(identifiers) != len(set(identifiers))
        or set(identifiers) != {m['id'] for m in plan['machines']}):
        raise ValueError('Confirm exactly every machine in the reviewed plan')
    doc = copy.deepcopy(current[0])
    if doc.get('servers') or env.kubeconfig_path or env.kubeconfig_data:
        raise ValueError('This action is for a new cloud. Existing clusters require the reviewed rebuild workflow')
    doc['provider'] = 'talos'
    doc['servers'] = {m['name']: {'source': 'baremetal', 'ip': m['ip'],
                                'private_ip': m['ip'], 'private_mac': m['mac'],
                                'roles': list(m['roles'])} for m in plan['machines']}
    selected = doc.setdefault('talos', {})
    selected.pop('install_disk', None)
    selected.setdefault('cni', 'kube-ovn')
    selected['install_disk_selectors'] = {m['name']: next(d['wwid'] for d in m['disks'] if d['purpose'] == 'boot') for m in plan['machines']}
    selected['data_disk_selectors'] = {m['name']: [d['wwid'] for d in m['disks'] if d['purpose'] == 'storage'] for m in plan['machines']}
    pxe = doc['pxe']
    cidr = str(ipaddress.IPv4Network(pxe['next_server'] + '/' + pxe['netmask'], strict=False))
    selected.setdefault('cluster_cidrs', [cidr])
    network = doc.setdefault('network', {})
    network.setdefault('gateway', pxe['gateway'])
    network.setdefault('cidr', cidr)
    # Explicit resolver configuration is needed when the node NIC is pinned static.
    selected['nameservers'] = [str(ipaddress.IPv4Address(value.strip())) for value in pxe['dns'].split(',')]
    storage_nodes = sum(bool(values) for values in selected['data_disk_selectors'].values())
    if storage_nodes:
        replicas = min(3, storage_nodes)
        overrides = doc.setdefault('helm_overrides', {}).setdefault('longhorn', {})
        settings = overrides.setdefault('defaultSettings', {})
        settings.setdefault('createDefaultDiskLabeledNodes', True)
        settings.setdefault('defaultReplicaCount', replicas)
        overrides.setdefault('persistence', {}).setdefault('defaultClassReplicaCount', replicas)
    # Normal validation catches any incompatible pre-existing advanced settings.
    envconfig.parse_document(yaml.safe_dump(doc, sort_keys=False))
    return doc, plan


def verify_management_identity(db, env, plan, *, settings, check_cancel) -> None:
    from sqlalchemy import select

    from app.models import BaremetalNode
    from app.services.crypto import decrypt_secret
    from app.services.redfish import hardware_inventory

    rows = {n.id: n for n in db.scalars(select(BaremetalNode).where(BaremetalNode.environment_id == env.id))}
    for machine in plan['machines']:
        check_cancel()
        node = rows[machine['id']]
        before = node.hardware_report or {}
        fresh = hardware_inventory(node.bmc_host, node.bmc_username,
                                   decrypt_secret(node.bmc_password, settings), check=check_cancel)
        if (fresh.get('serial') != before.get('serial')
            or (before.get('system_uuid') and fresh.get('system_uuid') != before['system_uuid'])
            or machine['mac'] not in {n.get('mac') for n in fresh.get('nics', [])}):
            raise ValueError('A management target changed. Discover and inspect it again before installation')
        disks = lambda report: sorted((str(d.get('serial')), d.get('capacity_bytes')) for d in report.get('drives', []))
        if disks(fresh) != disks(before):
            raise ValueError('A machine’s disks changed. Discover and inspect it again before installation')


def validate_talos_configuration(doc: dict[str, Any], env, *, temporary_root: Path, log) -> None:
    """Use the installed Talos validator, with temporary PKI and no node contact."""
    temporary_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='cluster-preflight-', dir=temporary_root) as directory:
        candidate = SimpleNamespace(name=env.name, genestack_config_dir=directory, kubeconfig_path=None)
        plan = talos.build_talos_plan(doc, candidate)
        root = Path(plan['workdir'])
        root.mkdir(mode=0o700)
        for command in plan['commands']:
            if command['phase'] not in ('write-network-patch', 'write-etcd-patch', 'gen-config', 'write-node-net'):
                continue
            result = subprocess.run(command['argv'], cwd=root, capture_output=True, text=True, timeout=60, check=False)
            if result.returncode:
                raise ValueError('Generated Talos configuration failed preflight before any machine was changed')
        ctl = talos.talosctl_command()
        for machine in plan['control_planes'] + plan['workers']:
            base = 'controlplane.yaml' if machine in plan['control_planes'] else 'worker.yaml'
            output = root / (machine['hostname'] + '-validated.yaml')
            patch = root / ('node-' + machine['hostname'] + '.yaml')
            commands = [[ctl, 'machineconfig', 'patch', str(root / base), '--patch', '@' + str(patch), '--output', str(output)],
                        [ctl, 'validate', '--config', str(output), '--mode', 'metal']]
            for argv in commands:
                result = subprocess.run(argv, cwd=root, capture_output=True, text=True, timeout=60, check=False)
                if result.returncode:
                    raise ValueError('Talos rejected a reviewed machine configuration before installation')
        log('[installation] Actual generated per-machine Talos configurations passed offline validation; no machine contacted')


def authorize(db, env, params, doc, plan, *, actor: str, job_id: str):
    record = doc['hardware_installation']
    record.update(execution_authorized=True, installation_job_id=job_id,
                  confirmed_by=actor, confirmed_fingerprint=plan['fingerprint'],
                  confirmed_machine_ids=sorted(params['confirmed_machine_ids']))
    row, _ = envconfig.put_version(db, env, yaml.safe_dump(doc, sort_keys=False), actor,
                                 expected_version=params['expected_version'])
    db.commit()
    return row


def resumable_plan(db, env, previous_job_id: str) -> tuple[dict[str, Any], dict[str, Any], int]:
    from sqlalchemy import select

    from app.models import BaremetalNode, Job, JobStatus

    previous = db.get(Job, previous_job_id)
    if (previous is None or previous.environment_id != env.id or previous.status != JobStatus.failed
        or previous.operation not in ('genestack.install_reviewed_cluster', 'genestack.resume_reviewed_cluster')):
        raise ValueError('Select this cloud’s stopped installation before continuing')
    current = envconfig.get_current(db, env)
    if current is None:
        raise ValueError('The authorized installation configuration is missing')
    doc = copy.deepcopy(current[0])
    record = doc.get('hardware_installation') or {}
    plan = record.get('reviewed_plan') or {}
    machines = plan.get('machines') or []
    if (record.get('execution_authorized') is not True or record.get('installation_job_id') != previous_job_id
        or not machines or set(record.get('confirmed_machine_ids') or []) != {m['id'] for m in machines}):
        raise ValueError('The installation authorization changed. Review the current targets')
    rows = {n.id: n for n in db.scalars(select(BaremetalNode).where(BaremetalNode.environment_id == env.id))}
    if set(rows) != {m['id'] for m in machines} or set(doc.get('servers') or {}) != {m['name'] for m in machines}:
        raise ValueError('The installation machines changed. Do not resume an old disk plan')
    selected = doc.get('talos') or {}
    for machine in machines:
        node = rows[machine['id']]
        entry = doc['servers'][machine['name']]
        boot = next(d['wwid'] for d in machine['disks'] if d['purpose'] == 'boot')
        data = [d['wwid'] for d in machine['disks'] if d['purpose'] == 'storage']
        if (node.wiped_at is None or node.name != machine['name'] or node.expected_ip != machine['ip']
            or node.pxe_mac != machine['mac'] or entry.get('private_mac') != machine['mac']
            or entry.get('ip') != machine['ip'] or entry.get('roles') != machine['roles']
            or selected.get('install_disk_selectors', {}).get(machine['name']) != boot
            or selected.get('data_disk_selectors', {}).get(machine['name']) != data):
            raise ValueError('The commissioned machine or disk selectors changed. Review the targets')
    return doc, plan, current[1].version
