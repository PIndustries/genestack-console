"""Firmware boot must select a real loader before the iPXE HTTP chain exists."""
import struct

import pytest

from app.services import pxe_runtime as pxe


def packet(architecture=None, ipxe=False):
    options = {pxe.OPT_MSGTYPE: bytes([pxe.DHCPDISCOVER])}
    if architecture is not None:
        options[93] = architecture
    if ipxe:
        options[pxe.OPT_USER_CLASS] = b'iPXE'
    return pxe.DhcpPacket(op=1, htype=1, hlen=6, hops=0, xid=42, secs=0, flags=0x8000,
                          ciaddr='0.0.0.0', yiaddr='0.0.0.0', siaddr='0.0.0.0', giaddr='0.0.0.0',
                          chaddr=bytes.fromhex('525400271098'), sname=b'', file=b'', options=options)


@pytest.fixture
def server(tmp_path):
    return pxe.DhcpServer(pxe.PxeNetConfig(interface='eth1', range_start='10.77.28.128',
                                          range_end='10.77.28.254', next_server='10.77.28.1', root=tmp_path))


@pytest.mark.parametrize('architecture', [7, 9])
def test_uefi_firmware_receives_efi_loader_not_bios(server, architecture):
    reply = server.handle(packet(struct.pack('!H', architecture)).to_bytes(), ('0.0.0.0', 68))
    parsed = pxe.parse_dhcp(reply)
    assert parsed.options[pxe.OPT_BOOTFILE] == b'ipxe.efi'
    assert parsed.file.rstrip(b'\0') == b'ipxe.efi'


def test_legacy_firmware_keeps_bios_loader(server):
    reply = pxe.parse_dhcp(server.handle(packet().to_bytes(), ('0.0.0.0', 68)))
    assert reply.options[pxe.OPT_BOOTFILE] == b'undionly.kpxe'


def test_ipxe_keeps_http_chain_without_bootloader_loop(server):
    reply = pxe.parse_dhcp(server.handle(packet(struct.pack('!H', 9), True).to_bytes(), ('0.0.0.0', 68)))
    assert reply.options[pxe.OPT_BOOTFILE] == b'http://10.77.28.1:8088/boot.ipxe'


@pytest.mark.parametrize('architecture', [b'\0', struct.pack('!H', 11)])
def test_malformed_or_unsupported_architecture_never_receives_bios_loader(server, architecture):
    assert server.handle(packet(architecture).to_bytes(), ('0.0.0.0', 68)) is None


def test_tftp_serves_only_system_loaders_on_selected_port(tmp_path, monkeypatch):
    from app.services import pxe_tftp

    loaders = tmp_path / 'system-ipxe'
    loaders.mkdir()
    monkeypatch.setattr(pxe_tftp, 'bootloader_directory', lambda: loaders)
    monkeypatch.setattr(pxe_tftp.shutil, 'which', lambda _: '/usr/sbin/dnsmasq')
    monkeypatch.setattr(pxe_tftp.os, 'geteuid', lambda: 1000)
    monkeypatch.setattr(pxe_tftp.time, 'sleep', lambda _: None)
    calls = []
    class Process:
        finished = False
        def poll(self): return 0 if self.finished else None
        def terminate(self): self.finished = True
        def wait(self, timeout): return 0
    def start(argv, **kwargs):
        calls.append(argv)
        return Process()
    monkeypatch.setattr(pxe_tftp.subprocess, 'Popen', start)
    server = pxe_tftp.BootloaderTftpServer('eth1', tmp_path / 'private-state' / 'tftp.log')
    server.start(); server.start()
    assert server.running and len(calls) == 1
    argv = calls[0]
    assert '--enable-tftp=eth1' in argv and '--bind-interfaces' in argv
    assert '--tftp-root=' + str(loaders) in argv
    assert '--conf-file=/dev/null' in argv and '--port=0' in argv
    assert not any(arg.startswith('--dhcp-range') for arg in argv)
    assert not any(arg.startswith('--tftp-root=') and 'private-state' in arg for arg in argv)
    server.stop()
    assert not server.running


def test_tftp_failure_prevents_advertising_missing_loaders(tmp_path, monkeypatch):
    monkeypatch.setattr(pxe.BootloaderTftpServer, 'start', lambda self: None)
    monkeypatch.setattr(pxe.PxeHttpServer, 'start', lambda self: pytest.fail('No boot HTTP before loader readiness'))
    monkeypatch.setattr(pxe.DhcpServer, 'start', lambda self: pytest.fail('No DHCP before loader readiness'))
    runtime = pxe.PxeRuntime(pxe.PxeNetConfig(interface='eth1', range_start='10.77.28.128',
                                            range_end='10.77.28.254', root=tmp_path))
    status = runtime.start()
    assert not status['http_running'] and not status['dhcp_running'] and not status['tftp_running']
    runtime.stop()
