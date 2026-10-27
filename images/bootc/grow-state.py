#!/usr/bin/python3
"""Expand the appliance's mounted ext4 state partition, never composefs /."""

from __future__ import annotations

import json
import pathlib
import re
import stat
import subprocess

_DEVICE = re.compile(r"/dev/[A-Za-z0-9_.+-]+")


def growth_target(mount: dict, state: dict, block: dict, partition: int) -> tuple[str, str, int]:
    source = str(mount.get("source") or "").split("[", 1)[0]
    parent = str(block.get("pkname") or "")
    if (
        mount.get("target") != "/sysroot"
        or mount.get("fstype") != "ext4"
        or state.get("target") != "/var"
        or state.get("fstype") != "ext4"
        or str(state.get("source") or "").split("[", 1)[0] != source
        or "rw" not in str(state.get("options") or "").split(",")
        or not _DEVICE.fullmatch(source)
        or not _DEVICE.fullmatch(parent)
        or source == parent
        or block.get("name") != source
        or block.get("type") != "part"
        or isinstance(partition, bool)
        or not isinstance(partition, int)
        or not 1 <= partition <= 128
    ):
        raise RuntimeError("The appliance state mount is not a supported writable ext4 partition")
    return source, parent, partition


def main() -> None:
    if not pathlib.Path("/run/ostree-booted").exists():
        raise RuntimeError("State growth requires a booted OSTree appliance")
    mounts = json.loads(
        subprocess.check_output(
            [
                "findmnt",
                "--json",
                "--mountpoint",
                "/sysroot",
                "--output",
                "SOURCE,FSTYPE,TARGET,OPTIONS",
            ],
            text=True,
        )
    )["filesystems"]
    if len(mounts) != 1:
        raise RuntimeError("Expected one appliance state mount")
    states = json.loads(
        subprocess.check_output(
            [
                "findmnt",
                "--json",
                "--mountpoint",
                "/var",
                "--output",
                "SOURCE,FSTYPE,TARGET,OPTIONS",
            ],
            text=True,
        )
    )["filesystems"]
    if len(states) != 1:
        raise RuntimeError("Expected one writable appliance state mount")
    source = str(mounts[0].get("source") or "").split("[", 1)[0]
    if not _DEVICE.fullmatch(source) or not stat.S_ISBLK(pathlib.Path(source).stat().st_mode):
        raise RuntimeError("The state mount source is not a local block device")
    blocks = json.loads(
        subprocess.check_output(
            [
                "lsblk",
                "--json",
                "--paths",
                "--output",
                "NAME,TYPE,PKNAME",
                source,
            ],
            text=True,
        )
    )["blockdevices"]
    if len(blocks) != 1:
        raise RuntimeError("Expected one appliance state partition")
    partition = int(
        (pathlib.Path("/sys/class/block") / pathlib.Path(source).name / "partition").read_text()
    )
    source, parent, partition = growth_target(mounts[0], states[0], blocks[0], partition)
    if not stat.S_ISBLK(pathlib.Path(parent).stat().st_mode):
        raise RuntimeError("The state partition parent is not a block device")
    result = subprocess.run(
        ["growpart", parent, str(partition)], check=False, capture_output=True, text=True
    )
    if result.returncode != 0 and not (
        result.returncode == 1 and result.stdout.startswith("NOCHANGE:")
    ):
        raise RuntimeError("Could not expand the appliance state partition")
    # /sysroot is a read-only mount on bootc. /var is the writable bind of
    # the same filesystem; grow through its mountpoint without remounting /.
    result = subprocess.run(
        ["/usr/lib/systemd/systemd-growfs", "/var"], check=False, capture_output=True, text=True
    )
    if result.returncode:
        raise RuntimeError("Could not expand the appliance state filesystem")
    print("Genestack appliance state filesystem is ready", flush=True)


if __name__ == "__main__":
    main()
