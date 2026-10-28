"""Exact reviewed data disks become persistent, automatically exposed user volumes."""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from app.services.install_disks import install_disk_selectors


def data_disk_selectors(talos: dict[str, Any], servers: dict[str, Any]) -> dict[str, list[str]] | None:
    raw = talos.get('data_disk_selectors')
    if raw is None:
        return None
    boot = install_disk_selectors(talos, servers)
    if boot is None or not isinstance(raw, dict) or set(raw) != set(servers):
        raise ValueError('talos.data_disk_selectors requires exact boot selectors and every configured machine')
    canonical = lambda value: re.sub(r'^(?:naa\.|eui\.|0x)', '', value.lower())
    used = {canonical(identity) for identity in boot.values()}
    for identities in raw.values():
        if not isinstance(identities, list) or len(identities) > 64:
            raise ValueError('Each machine must have a bounded list of reviewed data disk WWIDs')
        for identity in identities:
            if not isinstance(identity, str) or not re.fullmatch(r'(?:(?:naa|eui)\.|0x)?[0-9a-fA-F]{16,64}', identity):
                raise ValueError('Data disks require exact WWIDs without wildcards')
            key = canonical(identity)
            if key in used:
                raise ValueError('A data disk cannot reuse a boot disk or another data disk identity')
            used.add(key)
    return {name: list(values) for name, values in raw.items()}


def node_documents(hostname: str, talos: dict[str, Any], servers: dict[str, Any]) -> list[dict[str, Any]]:
    selectors = data_disk_selectors(talos, servers)
    if selectors is None:
        return []
    documents, disks = [], []
    for identity in selectors[hostname]:
        canonical = re.sub(r'^(?:naa\.|eui\.|0x)', '', identity.lower())
        name = 'gs-data-' + hashlib.sha256(canonical.encode()).hexdigest()[:12]
        documents.append({'apiVersion': 'v1alpha1', 'kind': 'UserVolumeConfig', 'name': name,
                          'volumeType': 'partition', 'provisioning': {
                              'diskSelector': {'match': 'disk.wwid == ' + json.dumps(identity)},
                              'minSize': '1GiB', 'maxSize': '100%', 'grow': True},
                          'filesystem': {'type': 'xfs'}})
        disks.append({'name': name, 'path': '/var/mnt/' + name, 'allowScheduling': True})
    if disks:
        documents.append({'apiVersion': 'v1alpha1', 'kind': 'KubeNodeConfig',
                          'labels': {'node.longhorn.io/create-default-disk': 'config'},
                          'annotations': {'node.longhorn.io/default-disks-config': json.dumps(disks, separators=(',', ':'))}})
    return documents
