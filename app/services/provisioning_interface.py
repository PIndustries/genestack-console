"""Explicit, worker-only setup of an unused dedicated machine-network port."""
from __future__ import annotations

import fcntl
import ipaddress
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from app.services import provisioning_network as network

DIRECTORY = Path('/etc/systemd/network')
LOCK = Path('/run/genestack-provisioning-interface.lock')


def _run(argv: list[str]) -> None:
    try:
        subprocess.run(argv, check=True, capture_output=True, text=True, timeout=20)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise ValueError("Host networking could not complete " + argv[0] + ". Check the deployer’s network service") from error


def setup(params: dict[str, Any], *, dry_run: bool) -> dict[str, Any]:
    if params.get('dedicated_network_confirmed') is not True:
        raise ValueError('Confirm that this port connects only to the dedicated machine network')
    name, fingerprint = params.get('interface'), params.get('fingerprint')
    if not isinstance(name, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,14}', name):
        raise ValueError('Select an unused Ethernet port')
    if not isinstance(fingerprint, str) or not re.fullmatch('[a-f0-9]{64}', fingerprint):
        raise ValueError('Reload the port inventory before setup')
    if os.geteuid() != 0:
        raise ValueError('The deployer worker must have host setup privileges')
    _run(['systemctl', 'is-active', '--quiet', 'systemd-networkd'])
    with LOCK.open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        choices = network.unused_interfaces()
        chosen = next((c for c in choices if c['name'] == name and c['fingerprint'] == fingerprint), None)
        if chosen is None:
            raise ValueError('The port changed or is already in use. Reload network settings')
        config = chosen['suggested_config']
        address = ipaddress.IPv4Interface(config['next_server'] + '/' + config['netmask'])
        mac = chosen['mac']
        if not re.fullmatch(r'(?:[0-9a-f]{2}:){5}[0-9a-f]{2}', mac):
            raise ValueError('The selected port has no stable hardware address')
        content = ('# Managed by Genestack: dedicated provisioning port\n'
                   '[Match]\nMACAddress=' + mac + '\n\n[Network]\nDHCP=no\n'
                   'LinkLocalAddressing=ipv6\nIPv6AcceptRA=no\nAddress=' + str(address) + '\n'
                   'IPForward=ipv4\nIPMasquerade=ipv4\n\n[Link]\nRequiredForOnline=no\n')
        path = DIRECTORY / ('05-genestack-provision-' + mac.replace(':', '') + '.network')
        if path.exists() or path.is_symlink():
            raise ValueError('A saved configuration already owns this port. Reload network settings')
        before = network._json_command(['ip', '-j', 'route', 'show', 'default'])
        other = {c['id'] for c in network.interfaces() if c['name'] != name}
        result = {'ok': True, 'dry_run': dry_run, 'interface': name, 'mac': mac,
                  'address': str(address), 'pxe': config, 'starts_dhcp': False}
        if dry_run:
            return result
        DIRECTORY.mkdir(mode=0o755, parents=True, exist_ok=True)
        if DIRECTORY.is_symlink():
            raise ValueError('The host network directory must not be a symbolic link')
        created = False
        try:
            with path.open('x') as output:
                created = True
                os.fchmod(output.fileno(), 0o644)
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            _run(['networkctl', 'reload'])
            _run(['networkctl', 'reconfigure', name])
            stop = time.monotonic() + 20
            while time.monotonic() < stop:
                current = network.interfaces()
                if any(c['name'] == name and c['address'] == str(address) and c['mac'] == mac for c in current):
                    break
                time.sleep(0.5)
            else:
                raise ValueError('The dedicated port did not acquire its configured address')
            if before != network._json_command(['ip', '-j', 'route', 'show', 'default']):
                raise ValueError('The management route changed during port setup')
            if other != {c['id'] for c in network.interfaces() if c['name'] != name}:
                raise ValueError('Another interface changed during port setup')
            return result
        except Exception:
            if created:
                path.unlink()
                _run(['networkctl', 'reload'])
                _run(['networkctl', 'reconfigure', name])
            raise
