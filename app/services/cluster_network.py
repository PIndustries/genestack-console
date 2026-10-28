"""First cluster network installation on exactly the reviewed Kubernetes nodes."""
from __future__ import annotations

from app.models import Job, JobStatus


def configuration(db, env):
    from app.services import envconfig

    current = envconfig.get_current(db, env)
    if current is None or not env.kubeconfig_data:
        raise ValueError('Complete this cloud’s cluster installation before networking')
    doc, row = current
    record = doc.get('hardware_installation') or {}
    job = db.get(Job, record.get('installation_job_id')) if record.get('installation_job_id') else None
    if (record.get('execution_authorized') is not True or job is None
        or job.environment_id != env.id or job.status != JobStatus.success
        or job.operation not in ('genestack.install_reviewed_cluster', 'genestack.resume_reviewed_cluster')
        or (doc.get('talos') or {}).get('cni') != 'kube-ovn'):
        raise ValueError('The reviewed cluster installation must finish before networking')
    machines = (record.get('reviewed_plan') or {}).get('machines') or []
    if not machines or {m['name'] for m in machines} != set(doc.get('servers') or {}):
        raise ValueError('The reviewed cluster machines changed')
    for m in machines:
        server = doc['servers'][m['name']]
        if server.get('ip') != m['ip'] or server.get('roles') != m['roles']:
            raise ValueError('The reviewed cluster assignments changed')
    return doc, row.version


def verified_nodes(doc, response) -> list[str]:
    """Reject an inherited, partial or unrelated Kubernetes connection."""
    expected = doc['servers']
    nodes = response.get('items') or []
    if len(nodes) != len(expected) or {n.get('metadata', {}).get('name') for n in nodes} != set(expected):
        raise ValueError('Kubernetes nodes do not match the reviewed cloud')
    for node in nodes:
        name = node['metadata']['name']
        addresses = {a.get('address') for a in node.get('status', {}).get('addresses', []) if a.get('type') == 'InternalIP'}
        if not node['metadata'].get('uid') or expected[name]['ip'] not in addresses:
            raise ValueError('Kubernetes addresses do not match the reviewed cloud')
    control_planes = sorted(name for name, server in expected.items() if 'k8s_control_plane' in server['roles'])
    if not control_planes:
        raise ValueError('The reviewed cloud has no control plane')
    return control_planes
