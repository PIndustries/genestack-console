"""Owned, read-only bootloader TFTP helper; cloud profiles stay on HTTP."""
from __future__ import annotations

import grp
import os
import pwd
import re
import shutil
import subprocess
import time
from pathlib import Path


def bootloader_directory() -> Path | None:
    return next((p for p in [Path('/usr/lib/ipxe'), Path('/usr/share/ipxe')]
                 if all((p / name).is_file() for name in ['ipxe.efi', 'undionly.kpxe'])), None)


class BootloaderTftpServer:
    def __init__(self, interface: str, log_path: Path):
        self.interface = interface
        self.log_path = log_path
        self.process: subprocess.Popen | None = None
        self.error: str | None = None

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def start(self) -> None:
        if self.running:
            return
        self.error = None
        helper = shutil.which('dnsmasq')
        root = bootloader_directory()
        if not helper or root is None:
            self.error = 'Prepare the deployer with dnsmasq and the BIOS/UEFI iPXE bootloaders before booting machines'
            return
        if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,14}', self.interface):
            self.error = 'Select a dedicated provisioning interface for bootloader TFTP'
            return
        argv = [helper, '--keep-in-foreground', '--conf-file=/dev/null', '--port=0',
                '--no-hosts', '--no-resolv', '--pid-file=', '--log-facility=-',
                '--bind-interfaces', '--interface=' + self.interface, '--except-interface=lo',
                '--enable-tftp=' + self.interface, '--tftp-root=' + str(root), '--tftp-max=16']
        if os.geteuid() == 0:
            user = pwd.getpwnam('nobody')
            argv += ['--user=' + user.pw_name, '--group=' + grp.getgrgid(user.pw_gid).gr_name]
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'ab') as output:
                os.fchmod(output.fileno(), 0o600)
                self.process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=output,
                                                stderr=output, start_new_session=True)
            time.sleep(0.2)
            if not self.running:
                self.error = 'Bootloader TFTP could not start on the selected interface. Check the deployer’s TFTP log and port 69'
        except OSError:
            self.error = 'Bootloader TFTP could not launch on the deployer'

    def stop(self) -> None:
        if self.process is not None:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=5)
            self.process = None
