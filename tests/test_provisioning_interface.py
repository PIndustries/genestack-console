"""Unused-port setup must never repurpose an uplink or partially keep a failed change."""
from copy import deepcopy

import pytest

from app.services import provisioning_interface as setup
from app.services import provisioning_network as network

PORT = {'ifname': 'eth1', 'link_type': 'ether', 'address': '52:54:00:27:10:90',
        'flags': ['BROADCAST', 'MULTICAST'], 'addr_info': []}


def inventory(monkeypatch, records=None, routes=None, routes6=None):
    def command(argv):
        if 'address' in argv:
            return deepcopy(records if records is not None else [PORT])
        return deepcopy((routes6 if '-6' in argv else routes) or [])
    monkeypatch.setattr(network, '_json_command', command)
    monkeypatch.setattr(network, 'dns_servers', lambda: ['1.1.1.1'])


def test_unused_port_has_complete_private_suggestion_without_shell_setup(monkeypatch):
    inventory(monkeypatch)
    port, = network.unused_interfaces()
    assert port['name'] == 'eth1' and port['address'] == ''
    assert len(port['fingerprint']) == 64
    config = port['suggested_config']
    choice = dict(port, address=config['next_server'] + '/24')
    assert network.validate(config, [choice], 8080) == config


@pytest.mark.parametrize('change,routes,routes6', [
    ({'addr_info': [{'family': 'inet', 'local': '10.0.2.15', 'prefixlen': 24}]}, [], []),
    ({}, [{'dev': 'eth1', 'dst': 'default'}], []),
    ({}, [], [{'dev': 'eth1', 'dst': 'default'}]),
    ({'addr_info': [{'family': 'inet6', 'scope': 'global'}]}, [], []),
    ({'master': 'bond0'}, [], []),
    ({'linkinfo': {'info_kind': 'bridge'}}, [], []),
    ({'ifname': 'eth1;reboot'}, [], []),
    ({'address': 'bad'}, [], []),
])
def test_in_use_and_virtual_ports_are_never_offered(monkeypatch, change, routes, routes6):
    inventory(monkeypatch, [dict(PORT, **change)], routes, routes6)
    assert network.unused_interfaces() == []


def test_suggestion_avoids_existing_routed_network(monkeypatch):
    inventory(monkeypatch, routes=[{'dev': 'eth0', 'dst': '10.77.28.0/24'}])
    assert network.unused_interfaces()[0]['suggested_config']['next_server'] == '10.77.29.1'


@pytest.fixture
def host(monkeypatch, tmp_path):
    inventory(monkeypatch)
    choice = network.unused_interfaces()[0]
    monkeypatch.setattr(setup, 'DIRECTORY', tmp_path / 'network')
    monkeypatch.setattr(setup, 'LOCK', tmp_path / 'lock')
    monkeypatch.setattr(setup.os, 'geteuid', lambda: 0)
    calls = []
    monkeypatch.setattr(setup, '_run', calls.append)
    return choice, calls


def params(choice):
    return {'interface': choice['name'], 'fingerprint': choice['fingerprint'],
            'dedicated_network_confirmed': True}


def test_dry_run_does_not_write_or_reconfigure(host):
    choice, calls = host
    value = setup.setup(params(choice), dry_run=True)
    assert value['dry_run'] is True and value['starts_dhcp'] is False
    assert not setup.DIRECTORY.exists()
    assert calls == [['systemctl', 'is-active', '--quiet', 'systemd-networkd']]


@pytest.mark.parametrize('change', [{'dedicated_network_confirmed': False}, {'fingerprint': '0' * 64}, {'interface': 'eth0'}])
def test_rejects_missing_consent_or_changed_inventory(host, change):
    choice, calls = host
    with pytest.raises(ValueError):
        setup.setup(dict(params(choice), **change), dry_run=False)
    assert not setup.DIRECTORY.exists()
    assert not any(c[0] == 'networkctl' for c in calls)


def test_configures_only_selected_mac_and_preserves_management(host, monkeypatch):
    choice, calls = host
    connected = dict(choice, address='10.77.28.1/24')
    monkeypatch.setattr(network, 'interfaces', lambda: [connected] if setup.DIRECTORY.exists() else [])
    value = setup.setup(params(choice), dry_run=False)
    assert value['ok'] and value['dry_run'] is False
    path, = setup.DIRECTORY.iterdir()
    text = path.read_text()
    assert 'MACAddress=52:54:00:27:10:90' in text and 'IPMasquerade=ipv4' in text
    assert 'Gateway=' not in text and 'DHCP=no' in text
    assert calls[-1] == ['networkctl', 'reconfigure', 'eth1']


def test_failed_reconfigure_rolls_back_file_without_touching_uplink(host, monkeypatch):
    choice, calls = host
    def run(argv):
        calls.append(argv)
        if argv == ['networkctl', 'reconfigure', 'eth1'] and len(calls) == 3:
            raise OSError('owned fixture failure')
    monkeypatch.setattr(setup, '_run', run)
    with pytest.raises(OSError, match='owned fixture failure'):
        setup.setup(params(choice), dry_run=False)
    assert list(setup.DIRECTORY.iterdir()) == []
    assert calls[-2:] == [['networkctl', 'reload'], ['networkctl', 'reconfigure', 'eth1']]


def test_existing_host_configuration_is_never_overwritten(host):
    choice, _ = host
    setup.DIRECTORY.mkdir()
    path = setup.DIRECTORY / '05-genestack-provision-525400271090.network'
    path.write_text('existing operator settings')
    with pytest.raises(ValueError, match='already owns'):
        setup.setup(params(choice), dry_run=False)
    assert path.read_text() == 'existing operator settings'


def test_ipv6_link_local_routes_do_not_hide_unused_ethernet(monkeypatch):
    port = dict(PORT, addr_info=[{'family': 'inet6', 'scope': 'link', 'local': 'fe80::1'}])
    inventory(monkeypatch, [port], routes6=[{'dev': 'eth1', 'dst': 'fe80::/64'},
                                            {'dev': 'eth1', 'dst': 'fe80::1', 'type': 'local'},
                                            {'dev': 'eth1', 'dst': 'ff00::/8', 'type': 'multicast'}])
    assert len(network.unused_interfaces()) == 1


def test_only_admin_can_submit_host_network_setup(client, operator_headers, admin_headers):
    from tests.test_envconfig import _create_env

    env = _create_env(client, admin_headers)
    response = client.post(f"/api/v1/environments/{env['id']}/jobs", headers=operator_headers,
                           json={'operation': 'genestack.provisioning_interface',
                                 'params': {'interface': 'eth1', 'fingerprint': '0' * 64,
                                            'dedicated_network_confirmed': True}})
    assert response.status_code == 403
