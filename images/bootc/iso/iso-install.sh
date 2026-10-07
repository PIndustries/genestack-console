#!/usr/bin/env bash
# Live ISO installer. Names a disk, then bootc writes the appliance onto it.
# genestack.install=ask waits for a name. genestack.install=/dev/nvme0n1
# wipes that disk without a prompt.
set -u

say() {
  printf '%s\n' "$*"
}

fail() {
  say "Install stopped: $*"
  say "A shell is on this console. Remove the ISO before you reboot."
  exec /bin/bash
}

cmdline_value() {
  local arg
  for arg in $(tr ' ' '\n' < /proc/cmdline); do
    case "$arg" in
      genestack.install=*)
        printf '%s\n' "${arg#genestack.install=}"
        return 0
        ;;
    esac
  done
  return 1
}

whole_disk() {
  local dev="$1"
  local kind
  [ -b "$dev" ] || return 1
  kind=$(lsblk -dno TYPE "$dev" 2>/dev/null || true)
  [ "$kind" = "disk" ]
}

live_disk() {
  local dev="$1"
  local src base
  case "$dev" in
    /dev/sr*|/dev/fd*) return 0 ;;
  esac
  src=$(findmnt -n -o SOURCE /run/initramfs/live 2>/dev/null || true)
  [ -n "$src" ] || return 1
  base=$(lsblk -no PKNAME "$src" 2>/dev/null | head -n 1 || true)
  [ -n "$base" ] && [ "/dev/$base" = "$dev" ]
}

target=$(cmdline_value || true)
if [ -z "$target" ]; then
  say "This boot has no genestack.install= argument. Nothing was written."
  exit 0
fi

say "Genestack Console installer"
say "Disks:"
lsblk -dno NAME,SIZE,MODEL,TRAN
say ""

if [ "$target" = "ask" ]; then
  say "Type the disk to wipe and install onto, for example nvme0n1."
  say "Type shell for a shell. Nothing is written until you name a disk."
  read -r choice || fail "no answer on the console"
  if [ "$choice" = "shell" ]; then
    exec /bin/bash
  fi
  target=$choice
fi

case "$target" in
  /dev/*) ;;
  *) target="/dev/$target" ;;
esac

whole_disk "$target" || fail "$target is not a whole disk"
live_disk "$target" && fail "$target is the installer media"

ssh_key=""
if [ "$(cmdline_value)" = "ask" ]; then
  say "Paste one SSH public key for the user console, or press enter to skip."
  read -r ssh_key || ssh_key=""
  if [ -n "$ssh_key" ] && ! printf '%s\n' "$ssh_key" | grep -Eq '^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp256) '; then
    say "That line is not an SSH public key. The install continues without it."
    ssh_key=""
  fi
fi

ref=$(cat /usr/lib/genestack-console/appliance-ref)
if ! podman image exists "$ref"; then
  found=$(podman images --format '{{.Repository}}:{{.Tag}}' | grep 'genestack-console-appliance:' | head -n 1 || true)
  if [ -z "$found" ]; then
    podman images
    fail "the appliance image is not on this ISO"
  fi
  ref=$found
fi

say "Installing onto $target. This wipes that disk."
podman run \
  --rm \
  --privileged \
  --pid=host \
  --ipc=host \
  --pull=never \
  --security-opt label=disable \
  -v /dev:/dev \
  -v /var/lib/containers:/var/lib/containers \
  "$ref" \
  bootc install to-disk --filesystem ext4 --wipe "$target" \
  || fail "bootc install to-disk failed"

if [ -n "$ssh_key" ]; then
  part=$(lsblk -ln -o PATH,TYPE,FSTYPE "$target" | awk '$2=="part" && $3=="ext4" {print $1; exit}')
  mnt=$(mktemp -d)
  if [ -n "$part" ] && mount "$part" "$mnt"; then
    dest=""
    if [ -d "$mnt/var" ]; then
      dest="$mnt/var/lib/cloud/seed/nocloud"
    elif [ -d "$mnt/ostree/deploy/default/var" ]; then
      dest="$mnt/ostree/deploy/default/var/lib/cloud/seed/nocloud"
    fi
    if [ -n "$dest" ]; then
      mkdir -p "$dest"
      printf '%s\n' 'instance-id: genestack-console' 'local-hostname: genestack-console' > "$dest/meta-data"
      printf '%s\n' \
        '#cloud-config' \
        'users:' \
        '  - name: console' \
        '    sudo: ["ALL=(ALL) NOPASSWD:ALL"]' \
        '    ssh_authorized_keys:' \
        "      - ${ssh_key}" \
        > "$dest/user-data"
      say "The SSH key is stored for the user console."
    else
      say "The disk has no /var yet. The SSH key was not written."
    fi
    umount "$mnt"
  else
    say "The SSH key was not written onto the new disk."
  fi
fi

say "Installed. Remove the ISO. The machine reboots into the appliance."
systemctl reboot
