"""Per-MAC PXE boot choice: commission RAM disk, Talos, Ubuntu, or the local disk.

The commission image is a RAM disk. It never mounts a hard drive. When
``gsc_wipe=1`` it clears the front of each fixed disk so the old bootloader
cannot win the next POST, then posts the disk and NIC report to the PXE HTTP
port. Talos is served only after that report is accepted.
"""

from __future__ import annotations

import io
import re
import tarfile
from datetime import datetime, timezone
from typing import Any

NEXT_BOOTS = ("commission", "talos", "ubuntu", "disk")
STOP_AFTER = ("", "commission", "talos")

# Pinned Alpine netboot. The kernel, initramfs, and modloop are fetched once
# into the PXE asset dir. The boot itself does not need the Alpine package repo.
ALPINE_VERSION = "3.20"
ALPINE_NETBOOT = (
    "https://dl-cdn.alpinelinux.org/alpine/v"
    f"{ALPINE_VERSION}/releases/x86_64/netboot"
)
COMMISSION_FILES = ("vmlinuz-lts", "initramfs-lts", "modloop-lts")

_MAC_RE = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")

COMMISSION_SCRIPT = r"""#!/bin/sh
# Genestack commission RAM disk. Does not mount hard drives.
echo "genestack commission start" >/dev/console
report=""
token=""
wipe=0
for arg in $(cat /proc/cmdline); do
  case "$arg" in
    gsc_report=*) report="${arg#gsc_report=}" ;;
    gsc_token=*) token="${arg#gsc_token=}" ;;
    gsc_wipe=1) wipe=1 ;;
  esac
done
esc() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g; s/[[:cntrl:]]//g'; }
read_id() {
  esc "$(cat "$1" 2>/dev/null || printf '%s' unknown)"
}
serial=$(read_id /sys/class/dmi/id/product_serial)
vendor=$(read_id /sys/class/dmi/id/sys_vendor)
product=$(read_id /sys/class/dmi/id/product_name)
nics=""
nfirst=1
for npath in /sys/class/net/*; do
  name=$(basename "$npath")
  [ "$name" = "lo" ] && continue
  mac=$(cat "$npath/address" 2>/dev/null || true)
  [ -n "$mac" ] || continue
  [ "$mac" = "00:00:00:00:00:00" ] && continue
  sep=","
  [ "$nfirst" = 1 ] && sep=""
  nfirst=0
  nics="$nics$sep{\"name\":\"$(esc "$name")\",\"mac\":\"$(esc "$mac")\"}"
done
disks=""
dfirst=1
wiped_all=1
disk_count=0
for dpath in /sys/block/*; do
  name=$(basename "$dpath")
  case "$name" in
    loop*|ram*|fd*|sr*|nbd*|zram*) continue ;;
  esac
  rem=$(cat "$dpath/removable" 2>/dev/null || echo 1)
  [ "$rem" = "0" ] || continue
  size=$(cat "$dpath/size" 2>/dev/null || echo 0)
  [ "$size" -gt 0 ] 2>/dev/null || continue
  model=$(esc "$(cat "$dpath/device/model" 2>/dev/null || true)")
  seriald=$(esc "$(cat "$dpath/device/serial" 2>/dev/null || true)")
  wiped=false
  if [ "$wipe" = 1 ]; then
    if dd if=/dev/zero of="/dev/$name" bs=1048576 count=16 conv=fsync >/dev/null 2>&1; then
      wiped=true
    else
      wiped_all=0
    fi
  fi
  sep=","
  [ "$dfirst" = 1 ] && sep=""
  dfirst=0
  disk_count=$((disk_count + 1))
  disks="$disks$sep{\"name\":\"$(esc "$name")\",\"size_sectors\":$size,\"model\":\"$model\",\"serial\":\"$seriald\",\"wiped\":$wiped}"
done
wipe_json=false
if [ "$wipe" = 1 ] && [ "$wiped_all" = 1 ] && [ "$disk_count" -gt 0 ]; then
  wipe_json=true
fi
printf '{"wipe":%s,"serial":"%s","vendor":"%s","product":"%s","token":"%s","nics":[%s],"disks":[%s]}\n' \
  "$wipe_json" "$serial" "$vendor" "$product" "$(esc "$token")" "$nics" "$disks" \
  > /tmp/gsc-report.json
echo "genestack commission report $report wipe=$wipe_json disks=$disk_count" >/dev/console
if [ -n "$report" ]; then
  wget -q -O /tmp/gsc-out --header="Content-Type: application/json" \
    --post-file=/tmp/gsc-report.json "$report" \
    || echo "genestack commission: report post failed" >/dev/console
fi
sleep 2
poweroff -f
"""


def normalize_mac(mac: str | None) -> str:
    """Return ``aa:bb:cc:dd:ee:ff`` or an empty string when the MAC is unusable."""
    raw = re.sub(r"[^0-9a-fA-F]", "", str(mac or ""))
    if len(raw) != 12:
        return ""
    pairs = [raw[i : i + 2].lower() for i in range(0, 12, 2)]
    text = ":".join(pairs)
    return text if _MAC_RE.match(text) else ""


def mac_filename(mac: str | None) -> str:
    normal = normalize_mac(mac)
    if not normal:
        return ""
    return normal.replace(":", "-") + ".ipxe"


def render_chain_ipxe(assets_base_url: str) -> str:
    """Env-wide script. Each MAC chains to its own profile, or exits to disk."""
    base = assets_base_url.rstrip("/")
    return f"""#!ipxe
# profile: chain
# Rendered by the Genestack Console. Do not edit.
# Unknown MACs exit so the firmware boots the local disk. Nothing is wiped.
chain {base}/mac/${{net0/mac:hexhyp}}.ipxe || exit
"""


def render_disk_ipxe() -> str:
    return """#!ipxe
# profile: disk
echo Genestack Console: this MAC boots the local disk.
exit
"""


def render_talos_ipxe(assets_base_url: str) -> str:
    """Talos metal maintenance kernel. No machine config on the cmdline."""
    base = assets_base_url.rstrip("/")
    return f"""#!ipxe
# profile: talos
# Talos metal maintenance boot. The console pushes config over port 50000
# only after a commission report has wiped the fixed disks.
kernel {base}/assets/vmlinuz talos.platform=metal ip=dhcp console=tty0 console=ttyS0,115200
initrd {base}/assets/initramfs.xz
boot
"""


def render_commission_ipxe(assets_base_url: str, token: str) -> str:
    """RAM-disk commission. ``gsc_wipe=1`` clears fixed-disk headers."""
    base = assets_base_url.rstrip("/")
    report = f"{base}/commission/{token}"
    return f"""#!ipxe
# profile: commission
kernel {base}/assets/commission/vmlinuz-lts modloop={base}/assets/commission/modloop-lts apkovl={base}/assets/commission.apkovl.tar.gz gsc_wipe=1 gsc_token={token} gsc_report={report} console=tty0 console=ttyS0,115200
initrd {base}/assets/commission/initramfs-lts
boot
"""


def render_ubuntu_ipxe(assets_base_url: str) -> str:
    """Ubuntu autoinstall. user-data is served from this console's PXE tree."""
    base = assets_base_url.rstrip("/")
    seed = f"{base}/ubuntu/"
    return (
        "#!ipxe\n"
        "# profile: ubuntu\n"
        "# Ubuntu autoinstall. Cloud-init reads user-data and meta-data\n"
        "# from the seed URL. No OpenStack and no Kubernetes in this boot.\n"
        "echo Genestack Console: Ubuntu autoinstall\n"
        f"kernel {base}/ubuntu/vmlinuz initrd=initrd ip=dhcp autoinstall "
        f"ds=nocloud-net\\;s={seed} ---\n"
        f"initrd {base}/ubuntu/initrd\n"
        "boot\n"
    )


def render_profile_ipxe(
    next_boot: str, assets_base_url: str, token: str | None
) -> str:
    choice = str(next_boot or "disk").strip().lower()
    if choice == "commission":
        return render_commission_ipxe(assets_base_url, token or "")
    if choice == "talos":
        return render_talos_ipxe(assets_base_url)
    if choice == "ubuntu":
        return render_ubuntu_ipxe(assets_base_url)
    return render_disk_ipxe()


def profile_from_script(text: str) -> str:
    for line in text.splitlines():
        if line.startswith("# profile:"):
            return line.split(":", 1)[1].strip() or "unknown"
    return "unknown"


def build_apkovl() -> bytes:
    """Alpine apkovl that runs the commission script from local.d."""
    payload = COMMISSION_SCRIPT.encode()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo("etc/local.d/commission.start")
        info.size = len(payload)
        info.mode = 0o755
        info.mtime = 0
        tar.addfile(info, io.BytesIO(payload))
        link = tarfile.TarInfo("etc/runlevels/default/local")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/init.d/local"
        link.mtime = 0
        tar.addfile(link)
    return buf.getvalue()


def _macs_in_report(body: dict[str, Any]) -> set[str]:
    found: set[str] = set()
    nics = body.get("nics")
    if not isinstance(nics, list):
        return found
    for nic in nics:
        if isinstance(nic, dict):
            normal = normalize_mac(str(nic.get("mac") or ""))
            if normal:
                found.add(normal)
    return found


def validate_report(body: Any, pxe_mac: str | None) -> tuple[bool, str]:
    """Accept a commission report only when every listed fixed disk was wiped."""
    if not isinstance(body, dict):
        return False, "report must be an object"
    if body.get("wipe") is not True:
        return False, "report did not wipe fixed disks"
    disks = body.get("disks")
    if not isinstance(disks, list) or not disks:
        return False, "report has no fixed disks"
    for disk in disks:
        if not isinstance(disk, dict) or not str(disk.get("name") or "").strip():
            return False, "disk entry missing name"
        if disk.get("wiped") is not True:
            return False, "a fixed disk was not wiped"
    expected = normalize_mac(pxe_mac)
    if expected:
        seen = _macs_in_report(body)
        if expected not in seen:
            return False, "report NICs do not include the node PXE MAC"
    return True, ""


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def served_after_wipe(
    talos_served_at: datetime | None, wiped_at: datetime | None
) -> bool:
    """True when Talos was served at or after the accepted wipe."""
    served = _as_utc(talos_served_at)
    wiped = _as_utc(wiped_at)
    return served is not None and wiped is not None and served >= wiped


def classify_boot(
    api_up: bool,
    k8s_ready: bool | None,
    *,
    wiped_at: datetime | None,
    talos_served_at: datetime | None,
    require_fresh: bool,
    was_ready: bool = False,
    saw_down: bool = True,
) -> str:
    """Distinguish a fresh Talos maintenance boot from the old install.

    Without ``require_fresh`` the historical three-way result is kept
    (``down``, ``old-os``, ``maintenance``) so existing callers stay stable.
    """
    if not api_up:
        return "down"
    if k8s_ready is True:
        return "old-os"
    if was_ready and not saw_down:
        return "old-os"
    if not require_fresh:
        return "maintenance"
    if served_after_wipe(talos_served_at, wiped_at):
        return "fresh-maintenance"
    return "old-maintenance"


def metal_ready(boot_stage: str | None, probe: str, stop_after: str | None) -> bool:
    """Whether the metal wait can advance for this stop point."""
    stop = str(stop_after or "").strip().lower()
    stage = str(boot_stage or "")
    if stop == "commission":
        return stage in (
            "commissioned",
            "talos",
            "fresh-maintenance",
            "installed",
        )
    return probe == "fresh-maintenance"


def commission_summary(report: Any) -> dict[str, Any]:
    body = report if isinstance(report, dict) else {}
    disks = body.get("disks") if isinstance(body.get("disks"), list) else []
    return {
        "wiped": body.get("wipe") is True,
        "disk_count": len(disks),
        "serial": str(body.get("serial") or ""),
        "product": str(body.get("product") or ""),
    }
