"""Only the actual writable appliance state partition is eligible to grow."""

import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "grow_state", Path(__file__).parents[1] / "images/bootc/grow-state.py"
)
grow = importlib.util.module_from_spec(spec)
spec.loader.exec_module(grow)


def records(source="/dev/vda4", parent="/dev/vda"):
    return (
        {"source": source, "target": "/sysroot", "fstype": "ext4", "options": "ro,relatime"},
        {
            "source": source + "[/ostree/deploy/default/var]",
            "target": "/var",
            "fstype": "ext4",
            "options": "rw,relatime",
        },
        {"name": source, "type": "part", "pkname": parent},
    )


@pytest.mark.parametrize(
    "source,parent,number",
    [
        ("/dev/vda4", "/dev/vda", 4),
        ("/dev/nvme0n1p4", "/dev/nvme0n1", 4),
        ("/dev/sda3", "/dev/sda", 3),
    ],
)
def test_target_comes_from_the_mounted_state_not_a_guessed_disk(source, parent, number):
    mount, state, block = records(source, parent)
    assert grow.growth_target(mount, state, block, number) == (source, parent, number)


@pytest.mark.parametrize(
    "change",
    [
        lambda m, b: m.update(source="composefs", target="/"),
        lambda m, b: m.update(target="/var"),
        lambda m, b: m.update(fstype="overlay"),
        lambda m, b: b.update(name="/dev/sdb4"),
        lambda m, b: b.update(type="disk"),
        lambda m, b: b.update(pkname="/dev/vda4"),
        lambda m, b: b.update(pkname="/tmp/disk"),
    ],
)
def test_unsupported_or_unrelated_devices_are_rejected(change):
    mount, state, block = records()
    change(mount, block)
    with pytest.raises(RuntimeError):
        grow.growth_target(mount, state, block, 4)


@pytest.mark.parametrize("partition", [0, 129, True, "4"])
def test_partition_number_must_be_kernel_metadata(partition):
    with pytest.raises(RuntimeError):
        grow.growth_target(*records(), partition)


@pytest.mark.parametrize(
    "change",
    [
        lambda s: s.update(source="/dev/sdb4"),
        lambda s: s.update(options="ro,relatime"),
        lambda s: s.update(target="/tmp"),
        lambda s: s.update(fstype="overlay"),
    ],
)
def test_writable_state_must_be_a_bind_of_the_same_filesystem(change):
    mount, state, block = records()
    change(state)
    with pytest.raises(RuntimeError):
        grow.growth_target(mount, state, block, 4)
