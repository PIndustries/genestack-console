# The appliance

A release ships three ways to run Genestack Console.

The install command puts the program on a Linux system you already have. Ubuntu is the usual system for that, including a machine you are using for development. That path is [Install the Genestack Console](install.md).

The same release also ships a bootc appliance. The operating system is a small Ubuntu 26.04 LTS boot image: the kernel, systemd, sshd, cloud-init, bootc, and the console program. It is a boot appliance. It is not Ubuntu Server, and it is not as small as Talos. Talos is one program and a kernel built for that program. This console program is a glibc binary, and bootc needs the ostree in Ubuntu 26.04, so the image stays on that base. The full firmware set is left out. CPU microcode stays, because the kernel package requires it. A network card that needs a firmware file will not come up until that package is added to the image.

Two files carry that appliance:

`genestack-console-appliance-<version>-amd64.qcow2.xz`

`genestack-console-appliance-<version>-amd64.iso`

The qcow2 is an already-installed disk. Attach it to a virtual machine. The ISO is install media. Boot it from virtual media, a USB stick, or a DVD, and it writes the appliance onto a disk you name. `version.json` names the qcow2 in `appliance` and the ISO in `iso`. The `binary` field is still the single Linux program. An install that already uses the program keeps updating that program.

## Install from the ISO

Attach the ISO and boot it. The menu says `Install Genestack Console`. It lists the disks and waits. Type a disk name, for example `nvme0n1`. That disk is wiped. Then paste one SSH public key for the user `console`, or press enter to skip the key.

Nothing is written until you name a disk. To wipe a known disk with no prompt, edit the kernel line and set `genestack.install=/dev/nvme0n1`. Remove the ISO when the installer says the machine is rebooting.

The installed system is the same appliance as the qcow2. The first boot still grows into the disk and reads cloud-init. A key you pasted is written for `console`. A boot with no key and no cloud-init metadata has no SSH login. The page listens on `127.0.0.1:8080`.

## What is on the disk

The disk is a bootc image. bootc keeps the operating system as one image, with a data disk beside it.

```
+-------------------------------------------+
| Genestack Console appliance               |
|                                           |
| image, replaced by bootc                  |
|   Ubuntu 26.04 LTS                        |
|   sshd                                    |
|   a copy of the console program           |
|                                           |
| data disk, kept across boots              |
|   /var/lib/genestack-console/config.yaml  |
|   /var/lib/genestack-console/data/        |
|   /var/lib/genestack-console/bin/         |
|   /var/lib/genestack     (/opt/genestack) |
|   /etc/genestack                          |
+-------------------------------------------+
```

`/opt/genestack-console` points at `/var/lib/genestack-console`. `/opt/genestack` points at `/var/lib/genestack`. The two systemd units run `/opt/genestack-console/bin/genestack-console`. The page listens on `127.0.0.1:8080`.

The first boot writes `config.yaml`, creates the `admin` user, and stores the password and API key in `/opt/genestack-console/ADMIN_CREDENTIALS.txt`. A later boot leaves that file and the program in place.

## Boot it

Decompress the disk and give it room for the Genestack checkout and the image cache. The disk has sshd and cloud-init. It has no password login. Pass your SSH key when you create the virtual machine. cloud-init writes that key for the user `console` on first boot. The same first boot grows the root filesystem to the size of the virtual disk.

```bash
xz -d genestack-console-appliance-<version>-amd64.qcow2.xz
qemu-img resize genestack-console-appliance-<version>-amd64.qcow2 80G
virt-install \
  --name genestack-console \
  --memory 8192 \
  --vcpus 4 \
  --import \
  --disk genestack-console-appliance-<version>-amd64.qcow2 \
  --os-variant ubuntu26.04 \
  --network network=default \
  --cloud-init ssh-key=$HOME/.ssh/id_ed25519.pub
```

Give the virtual machine 4 vCPU and 8 GiB of memory to start. A deploy host that caches images wants the larger disk.

On OpenStack, import the qcow2 as an image and boot it with a key pair. cloud-init reads that key pair. A NoCloud seed works the same way. The user-data file starts with `#cloud-config` and lists the key under the `console` user:

```yaml
#cloud-config
users:
  - name: console
    sudo: ["ALL=(ALL) NOPASSWD:ALL"]
    ssh_authorized_keys:
      - ssh-ed25519 AAAA... comment
```

Sign in as `console` with that key. Read `/opt/genestack-console/ADMIN_CREDENTIALS.txt`. From your laptop:

```bash
ssh -L 8080:127.0.0.1:8080 console@<appliance>
```

Open `http://127.0.0.1:8080/ui` and sign in as `admin` with the password from that file.

The appliance is a deploy host. It stays outside the cluster. Put the Genestack checkout at `/opt/genestack` and the settings at `/etc/genestack`, the same places as any other deploy host.

## Updates

The console program on a running appliance updates the same way it does on Ubuntu. The process reads `version.json`. When the release is newer and no job is queued or running, it replaces `/opt/genestack-console/bin/genestack-console` and restarts the two units. `config.yaml`, the database, and `/opt/genestack` stay.

A newer qcow2 on a later release is a new appliance. Import that file when you are creating another machine. Copying it over a disk that is already running would drop the database.

bootc is how the operating system image on that machine moves. `bootc status` shows the image this disk was built from. `bootc upgrade` pulls a newer image from a registry and stages it. A reboot starts that image. `bootc rollback` returns to the previous image. The data disk stays either way. The release workflow publishes the qcow2. It does not push the bootc image to a public registry. To move the operating system with `bootc upgrade`, build the image with `scripts/build-appliance.sh`, push it to a registry you control, and point this machine at that reference with `bootc switch`.

## Build the disk yourself

On a Linux x86_64 machine with podman, from a checkout:

```bash
./scripts/compile-console.sh
./scripts/build-appliance.sh
```

The disk is `dist/genestack-console-appliance-<version>-amd64.qcow2.xz`. The installer is `dist/genestack-console-appliance-<version>-amd64.iso`.

The operating system is Ubuntu 26.04 LTS. The build starts from `docker.io/library/ubuntu:26.04` and installs the kernel, OpenSSH, cloud-init, and bootc 1.16.14. Ubuntu 26.04 has the ostree release that bootc links against. The disk boots with systemd-boot. The ISO is a second image that boots live and runs `bootc install to-disk` for the appliance image. `GSC_BOOTC_BASE` selects the Ubuntu image. `GSC_BOOTC_VERSION` selects the bootc release. `GSC_IMAGE_BUILDER` selects the image-builder container. The default is `ghcr.io/osbuild/image-builder-cli:latest`.

A tag on this repository runs that build after the Linux binary is published and attaches the compressed qcow2 and the ISO to the same GitHub Release. The binary, `version.json`, `console.sh`, and `console.ps1` are published first. A failure in the disk build leaves that release in place. A disk that finished is attached even when the ISO step fails.
