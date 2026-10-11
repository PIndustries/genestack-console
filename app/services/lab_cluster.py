"""Plan for a lab cluster on the console host.

What: names, roles, sizes, qemu arguments, and the cloud-init seed for a
3 to 5 node lab. No process is started here and no file is written.
Where: app/services/lab_cluster.py. The hostvm.lab job calls this, then
app/services/lab_launch.py does the work.
Why: the same plan is what the job logs, what the tests lock, and what a
later run skips when that guest is already up.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

LAB_CPUS = 4
LAB_MEM_MB = 16384
LAB_DISK = "80G"
LAB_SSH_BASE = 2222
LAB_HEADROOM_MB = 8192
LAB_MIN_FREE_DISK = 25 * 1024**3
LAB_MIN_COUNT = 3
LAB_MAX_COUNT = 5
DEFAULT_COUNT = 3
DEFAULT_NAME = "genestack-lab"
IMAGE_NAME = "noble-server-cloudimg-amd64.img"
IMAGE_URL = (
    "https://cloud-images.ubuntu.com/noble/current/"
    "noble-server-cloudimg-amd64.img"
)
QCOW_MAGIC = b"QFI\xfb"

# Index is the guest number. Four adds a worker. Five adds a second compute.
_ROLES: dict[int, tuple[str, ...]] = {
    1: ("k8s_control_plane", "etcd", "control"),
    2: ("compute", "network"),
    3: ("storage",),
    4: ("worker",),
    5: ("compute",),
}

_SECTOR = 2048


@dataclass(frozen=True)
class LabNode:
    name: str
    roles: tuple[str, ...]
    ssh_port: int
    cpus: int = LAB_CPUS
    mem_mb: int = LAB_MEM_MB
    disk: str = LAB_DISK


def parse_count(value: Any) -> int:
    """3, 4, or 5. An empty value is the default of 3."""
    if value is None or value == "":
        return DEFAULT_COUNT
    try:
        count = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("count must be 3, 4, or 5") from exc
    if count < LAB_MIN_COUNT or count > LAB_MAX_COUNT:
        raise ValueError("count must be 3, 4, or 5")
    return count


def confirmed(value: Any) -> bool:
    """True only when the caller named the start."""
    if value is True or value == 1:
        return True
    return isinstance(value, str) and value.strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def parse_name(value: Any) -> str:
    """Environment name. The guest names stay lab-1 and so on."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return DEFAULT_NAME
    name = str(value).strip()
    if not name or len(name) > 63:
        raise ValueError("name must be a short environment name")
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.")
    if name[0] in "-_." or any(ch not in allowed for ch in name):
        raise ValueError("name must be a short environment name")
    return name


def nodes_for(count: int) -> list[LabNode]:
    """lab-1 through lab-N. Ports start at 2222."""
    count = parse_count(count)
    nodes: list[LabNode] = []
    for index in range(1, count + 1):
        nodes.append(
            LabNode(
                name=f"lab-{index}",
                roles=_ROLES[index],
                ssh_port=LAB_SSH_BASE + index - 1,
            )
        )
    return nodes


def room_error(count: int, mem_available_mb: int, disk_free_bytes: int) -> str | None:
    """Refuse when the host cannot hold the thin guests. None means it can."""
    need_mb = count * LAB_MEM_MB + LAB_HEADROOM_MB
    if mem_available_mb < need_mb:
        return (
            f"This host has {mem_available_mb} MiB available. "
            f"The lab needs {need_mb} MiB. The guests were not started."
        )
    if disk_free_bytes < LAB_MIN_FREE_DISK:
        free_gib = disk_free_bytes // (1024**3)
        need_gib = LAB_MIN_FREE_DISK // (1024**3)
        return (
            f"This host has {free_gib} GiB free on the lab disk. "
            f"The lab needs {need_gib} GiB free. The guests were not started."
        )
    return None


def refusal(
    count: int,
    *,
    mem_available_mb: int,
    disk_free_bytes: int,
    kvm: bool,
    qemu: bool,
    qemu_img: bool,
) -> str | None:
    """Why this host cannot start the lab. None means it can."""
    if not kvm:
        return "This host has no /dev/kvm. The lab was not started."
    if not qemu:
        return "qemu-system-x86_64 is not installed. The lab was not started."
    if not qemu_img:
        return "qemu-img is not installed. The lab was not started."
    return room_error(count, mem_available_mb, disk_free_bytes)


def user_data(node: LabNode, pubkey: str) -> str:
    """Cloud-init user-data. The public key is one line. No private key."""
    key = " ".join((pubkey or "").split())
    roles = ", ".join(node.roles)
    return (
        "#cloud-config\n"
        f"hostname: {node.name}\n"
        f"fqdn: {node.name}.genestack.local\n"
        "manage_etc_hosts: true\n"
        "users:\n"
        "  - name: ubuntu\n"
        "    sudo: ALL=(ALL) NOPASSWD:ALL\n"
        "    shell: /bin/bash\n"
        "    lock_passwd: true\n"
        "    ssh_authorized_keys:\n"
        f"      - {key}\n"
        "package_update: false\n"
        "runcmd:\n"
        "  - systemctl disable --now unattended-upgrades || true\n"
        f"# roles: {roles}\n"
    )


def meta_data(node: LabNode) -> str:
    return f"instance-id: iid-{node.name}-01\nlocal-hostname: {node.name}\n"


def linux_argv(
    node: LabNode,
    *,
    disk: Path,
    seed: Path,
    serial: Path,
    pidfile: Path,
) -> list[str]:
    """Linux KVM command. The pidfile sits at the top of the hypervisor root."""
    return [
        "qemu-system-x86_64",
        "-name",
        f"guest={node.name},debug-threads=on",
        "-machine",
        "pc,accel=kvm",
        "-cpu",
        "host",
        "-smp",
        str(node.cpus),
        "-m",
        str(node.mem_mb),
        "-drive",
        f"file={disk},if=virtio,format=qcow2",
        "-drive",
        f"file={seed},if=ide,format=raw,media=cdrom,readonly=on",
        "-netdev",
        f"user,id=net0,hostfwd=tcp:127.0.0.1:{node.ssh_port}-:22",
        "-device",
        "virtio-net-pci,netdev=net0",
        "-display",
        "none",
        "-serial",
        f"file:{serial}",
        "-pidfile",
        str(pidfile),
        "-daemonize",
    ]


def qemu_img_argv(base: Path, disk: Path) -> list[str]:
    """Thin qcow2 overlay. No preallocation."""
    return [
        "qemu-img",
        "create",
        "-f",
        "qcow2",
        "-b",
        str(base),
        "-F",
        "qcow2",
        str(disk),
        LAB_DISK,
    ]


def guest_paths(root: Path, node: LabNode) -> dict[str, Path]:
    """Disk and seed live in the guest directory. The pidfile stays on the root."""
    guest = root / node.name
    return {
        "guest": guest,
        "disk": guest / f"{node.name}.qcow2",
        "seed": guest / "seed.iso",
        "serial": guest / "serial.log",
        "pidfile": root / f"{node.name}.pid",
        "user_data": guest / "user-data",
        "meta_data": guest / "meta-data",
    }


def image_path(root: Path) -> Path:
    """Cache the cloud image beside the hypervisor root."""
    return root.parent / "images" / IMAGE_NAME


def _both16(value: int) -> bytes:
    return value.to_bytes(2, "little") + value.to_bytes(2, "big")


def _both32(value: int) -> bytes:
    return value.to_bytes(4, "little") + value.to_bytes(4, "big")


def _dir_record(extent: int, size: int, name: bytes, flags: int) -> bytes:
    record = bytearray(33)
    record[2:10] = _both32(extent)
    record[10:18] = _both32(size)
    # 2026-10-11 00:00:00 UTC. Year is years since 1900.
    record[18:25] = bytes((126, 10, 11, 0, 0, 0, 0))
    record[25] = flags
    record[28:32] = _both16(1)
    record[32] = len(name)
    record += name
    if len(record) % 2:
        record += b"\x00"
    record[0] = len(record)
    return bytes(record)


def _path_record(extent: int, *, big: bool) -> bytes:
    order = "big" if big else "little"
    record = bytearray()
    record.append(1)
    record.append(0)
    record += extent.to_bytes(4, order)
    record += (1).to_bytes(2, order)
    record += b"\x00"
    if len(record) % 2:
        record += b"\x00"
    return bytes(record)


def _sectors(size: int) -> int:
    if size <= 0:
        return 0
    return (size + _SECTOR - 1) // _SECTOR


def cidata_iso(user_data_bytes: bytes, meta_data_bytes: bytes) -> bytes:
    """ISO 9660 image whose label is cidata and whose files are the seed.

    Linux shows USER-DATA;1 as user-data. Cloud-init reads that label.
    """
    user_lba = 21
    user_sectors = _sectors(len(user_data_bytes))
    meta_lba = user_lba + user_sectors
    meta_sectors = _sectors(len(meta_data_bytes))
    total = meta_lba + meta_sectors
    root_lba = 20
    root = (
        _dir_record(root_lba, _SECTOR, b"\x00", 2)
        + _dir_record(root_lba, _SECTOR, b"\x01", 2)
        + _dir_record(user_lba, len(user_data_bytes), b"USER-DATA;1", 0)
        + _dir_record(meta_lba, len(meta_data_bytes), b"META-DATA;1", 0)
    )
    if len(root) > _SECTOR:
        raise RuntimeError("cloud-init seed directory does not fit one sector")
    l_path = _path_record(root_lba, big=False)
    m_path = _path_record(root_lba, big=True)
    pvd = bytearray(_SECTOR)
    pvd[0] = 1
    pvd[1:6] = b"CD001"
    pvd[6] = 1
    pvd[8:40] = b" " * 32
    pvd[40:72] = b"cidata" + b" " * (32 - len(b"cidata"))
    pvd[80:88] = _both32(total)
    pvd[120:124] = _both16(1)
    pvd[124:128] = _both16(1)
    pvd[128:132] = _both16(_SECTOR)
    pvd[132:136] = len(l_path).to_bytes(4, "little")
    pvd[136:140] = len(l_path).to_bytes(4, "big")
    pvd[140:144] = (18).to_bytes(4, "little")
    pvd[148:152] = (19).to_bytes(4, "big")
    root_record = _dir_record(root_lba, _SECTOR, b"\x00", 2)
    pvd[156:156 + len(root_record)] = root_record
    pvd[881] = 1
    terminator = bytearray(_SECTOR)
    terminator[0] = 255
    terminator[1:6] = b"CD001"
    terminator[6] = 1
    blob = bytearray(total * _SECTOR)
    blob[16 * _SECTOR : 17 * _SECTOR] = pvd
    blob[17 * _SECTOR : 18 * _SECTOR] = terminator
    blob[18 * _SECTOR : 18 * _SECTOR + len(l_path)] = l_path
    blob[19 * _SECTOR : 19 * _SECTOR + len(m_path)] = m_path
    blob[20 * _SECTOR : 20 * _SECTOR + len(root)] = root
    blob[user_lba * _SECTOR : user_lba * _SECTOR + len(user_data_bytes)] = user_data_bytes
    blob[meta_lba * _SECTOR : meta_lba * _SECTOR + len(meta_data_bytes)] = meta_data_bytes
    return bytes(blob)
