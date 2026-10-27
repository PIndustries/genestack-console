"""Per-MAC PXE boot choice: commission RAM disk, Talos, Ubuntu, or the local disk.

Talos and Ubuntu are the operating systems this console installs. Commission
is the wipe that Talos requires. Disk leaves the machine on what is already
installed.

The commission image is a RAM disk. It never mounts a hard drive. When
``gsc_wipe=1`` it clears the front of each fixed disk so the old bootloader
cannot win the next POST, then posts the disk and NIC report to the PXE HTTP
port. Talos is served only after that report is accepted.
"""

from __future__ import annotations

import gzip
import io
import re
import tarfile
from datetime import datetime, timezone
from typing import Any

NEXT_BOOTS = ("commission", "talos", "ubuntu", "disk")
STOP_AFTER = ("", "commission", "talos")

# Keep kernel, initramfs, modules and the signed package repository from one ISO.
# The boot node fetches all packages from its deployer; it needs no Internet repo.
ALPINE_VERSION = "3.24.3"
ALPINE_ISO_URL = (
    "https://dl-cdn.alpinelinux.org/alpine/v3.24/releases/x86_64/"
    "alpine-standard-3.24.3-x86_64.iso"
)
ALPINE_ISO_SHA256 = "1629ffe7308d4f3d3eeeed3a97d09ad62293f6d37ef3e483a7306077fdac12be"
COMMISSION_FILES = ("vmlinuz-lts", "initramfs-lts", "modloop-lts")

_MAC_RE = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")

COMMISSION_SCRIPT = r"""#!/bin/sh
# Genestack commission RAM disk. Never mounts disks or infers a wipe target.
echo "genestack commission start" >/dev/console
report=""
token=""
allowed=""
wipe=0
for arg in $(cat /proc/cmdline); do
  case "$arg" in
    gsc_report=*) report="${arg#gsc_report=}" ;;
    gsc_token=*) token="${arg#gsc_token=}" ;;
    gsc_allow_wwids=*) allowed="${arg#gsc_allow_wwids=}" ;;
    gsc_wipe=1) wipe=1 ;;
  esac
done
esc() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g; s/[[:cntrl:]]//g'; }
read_id() { esc "$(cat "$1" 2>/dev/null || printf '%s' unknown)"; }
wwid_for() {
  value=$(cat "$1/wwid" 2>/dev/null || cat "$1/device/wwid" 2>/dev/null || true)
  case "$value" in ''|*[!A-Za-z0-9._:+-]*) value="" ;; esac
  printf '%s' "$value"
}
remote_disk() {
  link=$(readlink -f "$1/device" 2>/dev/null || true)
  case "$link" in */session[0-9]*/*) return 0 ;; esac
  transport=$(cat "$1/device/transport" 2>/dev/null || true)
  case "$transport" in iscsi|tcp|rdma|fc) return 0 ;; esac
  return 1
}
mounted_disk() { grep -Eq "^/dev/$1(p?[0-9]+)? " /proc/mounts; }
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
  carrier=$(cat "$npath/carrier" 2>/dev/null || true)
  link_up=null
  [ "$carrier" = 1 ] && link_up=true
  [ "$carrier" = 0 ] && link_up=false
  sep=","
  [ "$nfirst" = 1 ] && sep=""
  nfirst=0
  nics="$nics$sep{\"name\":\"$(esc "$name")\",\"mac\":\"$(esc "$mac")\",\"link_up\":$link_up}"
done
inventory=/tmp/gsc-disk-inventory
selected=/tmp/gsc-selected-disks
: > "$inventory"
: > "$selected"
# Inventory every candidate first. A missing or ambiguous target forbids ALL writes.
for dpath in /sys/block/*; do
  name=$(basename "$dpath")
  case "$name" in loop*|ram*|fd*|sr*|nbd*|zram*|dm-*) continue ;; esac
  rem=$(cat "$dpath/removable" 2>/dev/null || echo 1)
  [ "$rem" = 0 ] || continue
  size=$(cat "$dpath/size" 2>/dev/null || echo 0)
  [ "$size" -gt 0 ] 2>/dev/null || continue
  wwid=$(wwid_for "$dpath")
  ro=$(cat "$dpath/ro" 2>/dev/null || echo 1)
  remote=0
  remote_disk "$dpath" && remote=1
  mounted=0
  mounted_disk "$name" && mounted=1
  printf '%s|%s|%s|%s|%s|%s\n' "$name" "$wwid" "$size" "$remote" "$ro" "$mounted" >> "$inventory"
done
plan_valid=1
case "$allowed" in ''|,*|*,|*,,*|*[!A-Za-z0-9,._:+-]*) plan_valid=0 ;; esac
seen=","
selected_count=0
for identity in $(printf '%s' "$allowed" | tr ',' ' '); do
  case "$seen" in *",$identity,"*) plan_valid=0 ;; esac
  seen="$seen$identity,"
  if ! awk -F'|' -v id="$identity" '
    $2==id {count++; row=$1 "|" $2; valid=($3>=65536 && $4==0 && $5==0 && $6==0)}
    END {if(count==1 && valid) print row; else exit 1}
  ' "$inventory" >> "$selected"; then plan_valid=0; fi
  selected_count=$((selected_count + 1))
done
disks=""
dfirst=1
wiped_all=1
disk_count=0
wiped_count=0
while IFS='|' read -r name wwid size remote ro mounted; do
  dpath="/sys/block/$name"
  model=$(esc "$(cat "$dpath/device/model" 2>/dev/null || true)")
  seriald=$(esc "$(cat "$dpath/device/serial" 2>/dev/null || true)")
  chosen=false
  grep -Fqx "$name|$wwid" "$selected" && chosen=true
  wiped=false
  if [ "$wipe" = 1 ] && [ "$plan_valid" = 1 ] && [ "$chosen" = true ]; then
    # Recheck identity and access immediately before writing selected metadata.
    current=$(wwid_for "$dpath")
    current_ro=$(cat "$dpath/ro" 2>/dev/null || echo 1)
    if [ "$current" = "$wwid" ] && [ "$current_ro" = 0 ] && ! remote_disk "$dpath" && ! mounted_disk "$name" &&
       dd if=/dev/zero of="/dev/$name" bs=1048576 count=16 conv=fsync >/dev/null 2>&1 &&
       dd if=/dev/zero of="/dev/$name" bs=512 seek="$((size - 32768))" count=32768 conv=fsync >/dev/null 2>&1; then
      wiped=true
      wiped_count=$((wiped_count + 1))
    else
      wiped_all=0
    fi
  fi
  sep=","
  [ "$dfirst" = 1 ] && sep=""
  dfirst=0
  disk_count=$((disk_count + 1))
  remote_json=false
  readonly_json=false
  [ "$remote" = 1 ] && remote_json=true
  [ "$ro" != 0 ] && readonly_json=true
  disks="$disks$sep{\"name\":\"$(esc "$name")\",\"wwid\":\"$(esc "$wwid")\",\"size_sectors\":$size,\"model\":\"$model\",\"serial\":\"$seriald\",\"remote\":$remote_json,\"readonly\":$readonly_json,\"selected\":$chosen,\"wiped\":$wiped}"
done < "$inventory"
wipe_json=false
if [ "$wipe" = 1 ] && [ "$plan_valid" = 1 ] && [ "$wiped_all" = 1 ] && [ "$selected_count" -gt 0 ] && [ "$wiped_count" = "$selected_count" ]; then
  wipe_json=true
fi
printf '{"wipe":%s,"serial":"%s","vendor":"%s","product":"%s","token":"%s","nics":[%s],"disks":[%s]}\n' \
  "$wipe_json" "$serial" "$vendor" "$product" "$(esc "$token")" "$nics" "$disks" > /tmp/gsc-report.json
echo "genestack commission report wipe=$wipe_json selected=$selected_count disks=$disk_count" >/dev/console
if [ -n "$report" ]; then
  wget -q -O /tmp/gsc-out --header="Content-Type: application/json" --post-file=/tmp/gsc-report.json "$report" \
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


def validate_wipe_wwids(value: Any) -> list[str]:
    if not isinstance(value, list) or not 1 <= len(value) <= 64:
        raise ValueError("A nonempty exact disk WWID allowlist is required before commissioning")
    if any(not isinstance(v, str) or not re.fullmatch(r"(?:(?:naa|eui)\.|0x)?[0-9a-fA-F]{16,64}", v) for v in value):
        raise ValueError("The commissioning disk allowlist requires exact WWIDs without wildcards")
    if len(",".join(value)) > 1024:
        raise ValueError("The commissioning disk allowlist exceeds the boot command-line budget")
    canonical = [re.sub(r"^(?:naa\.|eui\.|0x)", "", v.lower()) for v in value]
    if len(canonical) != len(set(canonical)):
        raise ValueError("The commissioning disk allowlist contains duplicate identities")
    return list(value)


def render_commission_ipxe(assets_base_url: str, token: str, wipe_wwids: Any = None) -> str:
    """An unbound or legacy profile must never boot the wipe image."""
    try:
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", str(token or "")):
            return render_disk_ipxe()
        allowed = ",".join(validate_wipe_wwids(wipe_wwids))
    except ValueError:
        return render_disk_ipxe()
    base = assets_base_url.rstrip("/")
    report = f"{base}/commission/{token}"
    return f"""#!ipxe
# profile: commission
kernel {base}/assets/commission/{ALPINE_VERSION}/vmlinuz-lts ip=dhcp alpine_repo={base}/assets/commission/{ALPINE_VERSION}/apks modloop={base}/assets/commission/{ALPINE_VERSION}/modloop-lts apkovl={base}/assets/commission.apkovl.tar.gz gsc_wipe=1 gsc_allow_wwids={allowed} gsc_token={token} gsc_report={report} console=tty0 console=ttyS0,115200
initrd {base}/assets/commission/{ALPINE_VERSION}/initramfs-lts
boot
"""


def render_inspection_ipxe(assets_base_url: str, token: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", str(token or "")):
        return render_disk_ipxe()
    base = assets_base_url.rstrip("/")
    return f"""#!ipxe
# profile: inspect
kernel {base}/assets/commission/{ALPINE_VERSION}/vmlinuz-lts ip=dhcp alpine_repo={base}/assets/commission/{ALPINE_VERSION}/apks modloop={base}/assets/commission/{ALPINE_VERSION}/modloop-lts apkovl={base}/assets/commission.apkovl.tar.gz gsc_wipe=0 gsc_token={token} gsc_report={base}/commission/{token} console=tty0 console=ttyS0,115200
initrd {base}/assets/commission/{ALPINE_VERSION}/initramfs-lts
boot
"""


def render_ubuntu_ipxe(assets_base_url: str, hostname: str | None = None) -> str:
    """Ubuntu autoinstall. A hostname uses that machine's own seed directory."""
    base = assets_base_url.rstrip("/")
    host = str(hostname or "").strip().lower()
    if host and re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", host):
        seed = f"{base}/ubuntu/{host}/"
    else:
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
    next_boot: str,
    assets_base_url: str,
    token: str | None,
    hostname: str | None = None,
    wipe_wwids: Any = None,
    node_id: str | None = None,
) -> str:
    choice = str(next_boot or "disk").strip().lower()
    if choice == "commission":
        script = render_commission_ipxe(assets_base_url, token or "", wipe_wwids)
    elif choice == "inspect":
        script = render_inspection_ipxe(assets_base_url, token or "")
    elif choice == "talos":
        script = render_talos_ipxe(assets_base_url)
    elif choice == "ubuntu":
        script = render_ubuntu_ipxe(assets_base_url, hostname)
    else:
        script = render_disk_ipxe()
    if node_id and re.fullmatch(r"[A-Za-z0-9-]{1,64}", node_id):
        script = script.replace("#!ipxe\n", f"#!ipxe\n# node-id: {node_id}\n", 1)
    return script


def profile_from_script(text: str) -> str:
    for line in text.splitlines():
        if line.startswith("# profile:"):
            return line.split(":", 1)[1].strip() or "unknown"
    return "unknown"


def build_apkovl() -> bytes:
    """Alpine apkovl that runs the commission script from local.d."""
    payload = COMMISSION_SCRIPT.encode()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo("etc/local.d/commission.start")
        info.size = len(payload)
        info.mode = 0o755
        info.mtime = 0
        tar.addfile(info, io.BytesIO(payload))
        # An apkovl means Alpine does not create its normal sysinit links.
        # Load the ISO's full module set before PCI hardware discovery/local.
        services = [("sysinit", name) for name in ("devfs", "dmesg", "mdev", "hwdrivers", "modloop")]
        services += [("default", "local")]
        for runlevel, name in services:
            link = tarfile.TarInfo(f"etc/runlevels/{runlevel}/{name}")
            link.type = tarfile.SYMTYPE
            link.linkname = f"/etc/init.d/{name}"
            link.mtime = 0
            tar.addfile(link)
    compressed = io.BytesIO()
    with gzip.GzipFile(fileobj=compressed, mode="wb", mtime=0, compresslevel=9) as stream:
        stream.write(buf.getvalue())
    return compressed.getvalue()


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


def validate_scoped_report(body: Any, pxe_mac: str | None, wipe_wwids: Any) -> tuple[bool, str]:
    try:
        expected = set(validate_wipe_wwids(wipe_wwids))
    except ValueError as exc:
        return False, str(exc)
    if not isinstance(body, dict) or body.get("wipe") is not True:
        return False, "report did not wipe the selected disk allowlist"
    disks = body.get("disks")
    if not isinstance(disks, list) or not disks:
        return False, "report has no disks"
    found, names = set(), set()
    for disk in disks:
        if not isinstance(disk, dict) or not isinstance(disk.get("name"), str) or not disk["name"] or disk["name"] in names:
            return False, "invalid or duplicate disk entry"
        names.add(disk["name"])
        identity = disk.get("wwid")
        if not isinstance(identity, str):
            identity = ""
        if identity in expected:
            if identity in found:
                return False, "disk allowlist identity is ambiguous"
            if disk.get("wiped") is not True or disk.get("remote") or disk.get("readonly"):
                return False, "a selected local writable disk was not wiped"
            found.add(identity)
        elif disk.get("wiped") is True:
            return False, "a disk outside the allowlist was wiped"
    if found != expected:
        return False, "report does not cover the complete disk allowlist"
    expected_mac = normalize_mac(pxe_mac)
    if not expected_mac or expected_mac not in _macs_in_report(body):
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
