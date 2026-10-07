"""First boot of the appliance disk seeds state and then leaves it alone."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PREPARE = ROOT / "images" / "bootc" / "prepare.sh"
IMAGE = ROOT / "images" / "bootc"


def _keeps_firmware_minimal(text: str) -> None:
    assert "linux-firmware-minimal" in text
    requested = [
        line.strip().rstrip("\\").strip()
        for line in text.splitlines()
    ]
    assert "linux-firmware" not in requested


def test_appliance_image_takes_an_ssh_key_from_cloud_init():
    container = (IMAGE / "Containerfile").read_text(encoding="utf-8")
    assert "cloud-init" in container
    assert "10-genestack-cloud.cfg" in container
    assert "docker.io/library/ubuntu:26.04" in container
    assert "apt-get" in container
    assert "v1.16.14" in container
    assert "default-toolchain stable" in container
    assert "libclang-dev" in container
    assert "linux-image-generic" in container
    assert "go-md2man" in container
    assert "00-genestack.toml" in container
    assert 'type = "ext4"' in container
    assert "setup-root-conf.toml" in container
    assert "/usr/lib/ostree/prepare-root.conf" in container
    assert "[composefs]" in container
    assert "enabled = true" in container
    assert "ostree-boot" in container
    assert "podman" in container
    assert "v0.3.2" in container
    assert "make install-all" in container
    assert "grub-efi-amd64-bin" in container
    assert "grub-efi-amd64-signed" in container
    assert "shim-signed" in container
    assert "blsuki" in container
    assert "shimx64.efi" in container
    assert "bootupctl backend generate-update-metadata" in container
    assert "test ! -e /usr/sbin/grub2-install" in container
    assert "rm -f /usr/sbin/grub2-install" in container
    assert 'add_dracutmodules+=" ostree bootc "' in container
    assert "btrfs-progs" not in container
    assert "xfsprogs" not in container
    _keeps_firmware_minimal(container)
    assert "systemd-resolved" in container
    assert "cloud-init-main.service" in container
    assert "cloud-init-network.service" in container
    assert "cloud-init.service" not in container
    assert "/etc/selinux/targeted/contexts/files/file_contexts" in container
    assert "rm -f /etc/selinux/config" in container
    assert "SELINUX=disabled" not in container
    assert "policycoreutils" in container
    assert "/usr/sbin/setfiles" in container
    assert "systemctl disable ssh.service" in container
    assert "systemctl enable ssh.service" not in container
    assert "genestack ALL=(root) NOPASSWD: /usr/bin/bootc" in container
    assert "centos" not in container.lower()
    assert "dnf" not in container
    assert "anaconda" not in container.lower()
    cfg = (IMAGE / "10-genestack-cloud.cfg").read_text(encoding="utf-8")
    assert "name: console" in cfg
    assert "groups: [sudo]" in cfg
    assert "NoCloud" in cfg
    assert "OpenStack" in cfg
    script = (ROOT / "scripts" / "build-appliance.sh").read_text(encoding="utf-8")
    assert "10-genestack-cloud.cfg" in script
    assert "docker.io/library/ubuntu:26.04" in script
    assert "bootc-generic-iso" in script
    assert "blueprint" not in script
    assert "centos" not in script.lower()
    installer = (IMAGE / "iso" / "Containerfile").read_text(encoding="utf-8")
    assert "dracut-live" in installer
    assert "grub-efi" in installer
    assert "anaconda" not in installer.lower()
    _keeps_firmware_minimal(installer)
    assert "/etc/selinux/targeted/contexts/files/file_contexts" in installer
    assert "rm -f /etc/selinux/config" in installer
    assert "SELINUX=disabled" not in installer
    assert "policycoreutils" in installer
    assert "/usr/sbin/setfiles" in installer
    service = (IMAGE / "genestack-console.service").read_text(encoding="utf-8")
    assert "--host 0.0.0.0" in service
    prepare = (IMAGE / "prepare.sh").read_text(encoding="utf-8")
    assert "host: 0.0.0.0" in prepare
    assert "watch: false" in prepare
    assert "SSH is off." in prepare
    iso_install = (IMAGE / "iso" / "iso-install.sh").read_text(encoding="utf-8")
    assert "bootc install to-disk" in iso_install
    assert "--wipe" in iso_install
    assert "genestack.install" in iso_install
    docs = (ROOT / "docs" / "appliance.md").read_text(encoding="utf-8")
    assert "Ubuntu 26.04" in docs
    assert "ubuntu26.04" in docs
    assert "genestack-console-appliance-<version>-amd64.iso" in docs
    assert "not as small as Talos" in docs
    assert "bootupd" in docs
    assert "centos" not in docs.lower()


def test_prepare_writes_config_once_and_keeps_the_binary(tmp_path: Path):
    image_bin = tmp_path / "image-bin"
    image_bin.write_text("#!/bin/sh\necho appliance\n", encoding="utf-8")
    image_bin.chmod(0o755)
    prefix = tmp_path / "state"
    genestack = tmp_path / "genestack"
    env = os.environ.copy()
    env.update(
        {
            "GSC_APPLIANCE_PREFIX": str(prefix),
            "GSC_APPLIANCE_IMAGE_BIN": str(image_bin),
            "GSC_APPLIANCE_GENESTACK": str(genestack),
        }
    )

    subprocess.run(["bash", str(PREPARE)], check=True, env=env)
    config = (prefix / "config.yaml").read_text(encoding="utf-8")
    assert "secret_key:" in config
    assert "host: 0.0.0.0" in config
    assert "REPLACE_ME" not in config
    assert f"data_dir: {prefix}/data" in config
    binary = prefix / "bin" / "genestack-console"
    assert binary.read_text(encoding="utf-8").startswith("#!/bin/sh")
    assert stat.S_IMODE((prefix / "config.yaml").stat().st_mode) == 0o600

    image_bin.write_text("#!/bin/sh\necho replaced\n", encoding="utf-8")
    subprocess.run(["bash", str(PREPARE)], check=True, env=env)
    assert binary.read_text(encoding="utf-8").startswith("#!/bin/sh\necho appliance")
    assert (prefix / "config.yaml").read_text(encoding="utf-8") == config
