# The appliance

A release ships three ways to run Genestack Console.

The install command puts the program on a Linux system you already have. Ubuntu is the usual system for that, including a machine you are using for development. That path is [Install the Genestack Console](install.md).

The same release also ships a bootc appliance. The operating system is a small Ubuntu 26.04 LTS boot image: the kernel, systemd, cloud-init, bootc, and the console program. SSH is installed and does not start. It is a boot appliance. It is not Ubuntu Server, and it is not as small as Talos. Talos is one program and a kernel built for that program. This console program is a glibc binary, and bootc needs the ostree in Ubuntu 26.04, so the image stays on that base. The full firmware set is left out. CPU microcode stays, because the kernel package requires it. A network card that needs a firmware file will not come up until that package is added to the image.

Two files carry that appliance:

`genestack-console-appliance-<version>-amd64.qcow2.xz`

`genestack-console-appliance-<version>-amd64.iso`

The qcow2 is an already-installed disk. Attach it to a virtual machine. The ISO is install media. Boot it from virtual media, a USB stick, or a DVD, and it writes the appliance onto a disk you name. `version.json` names the qcow2 in `appliance` and the ISO in `iso`. The `binary` field is still the single Linux program. An install that already uses the program keeps updating that program.

## Install from the ISO

Attach the ISO and boot it. The menu says `Install Genestack Console`. It lists the disks and waits. Type a disk name, for example `nvme0n1`. That disk is wiped. Then paste one SSH public key for the user `console`, or press enter to skip the key. The installed system does not start SSH. The key is saved for later.

Nothing is written until you name a disk. To wipe a known disk with no prompt, edit the kernel line and set `genestack.install=/dev/nvme0n1`. Remove the ISO when the installer says the machine is rebooting.

The installed system is the same appliance as the qcow2. The first boot still grows into the disk and reads cloud-init. The page listens on port 8080.

## What is on the disk

The disk is a bootc image. bootc keeps the operating system as one image, with a data disk beside it.

```
+-------------------------------------------+
| Genestack Console appliance               |
|                                           |
| image, replaced by bootc                  |
|   Ubuntu 26.04 LTS                        |
|   SSH off                                 |
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

`/opt/genestack-console` points at `/var/lib/genestack-console`. `/opt/genestack` points at `/var/lib/genestack`. The two systemd units run `/opt/genestack-console/bin/genestack-console`. The page listens on port 8080.

The first boot writes `config.yaml`, creates the `admin` user, and stores the password and API key in `/opt/genestack-console/ADMIN_CREDENTIALS.txt`. A later boot leaves that file and the program in place.

## Boot it

The qcow2 is a 10 GiB disk. Decompress it and give it room for the Genestack checkout and the image cache. SSH does not start. cloud-init still runs. The first boot grows the root filesystem to the size of the virtual disk and writes the admin password on the machine console.

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
  --network network=default
```

Give the virtual machine 4 vCPU and 8 GiB of memory to start. A deploy host that caches images wants the larger disk.

Open `http://<address>:8080/ui`. The machine console shows the `admin` password. Settings on that page lists this boot and the previous image. Upgrade pulls the image this machine tracks and reboots. Rollback reboots into the previous image.

On OpenStack, import the qcow2 as an image and boot it. A NoCloud seed can still pass a key for the user `console`. SSH stays off until it is enabled on the machine. The user-data file starts with `#cloud-config` and lists the key under the `console` user:

```yaml
#cloud-config
users:
  - name: console
    sudo: ["ALL=(ALL) NOPASSWD:ALL"]
    ssh_authorized_keys:
      - ssh-ed25519 AAAA... comment
```

Open `http://<address>:8080/ui` and sign in as `admin` with the password on the machine console. The same password is in `/opt/genestack-console/ADMIN_CREDENTIALS.txt`.

The appliance is a deploy host. It stays outside the cluster. Put the Genestack checkout at `/opt/genestack` and the settings at `/etc/genestack`, the same places as any other deploy host.

## Updates

On the appliance, open Settings. The version list shows this boot and the previous image. Upgrade runs `bootc upgrade` and reboots. Rollback runs `bootc rollback` and reboots. `config.yaml`, the database, and `/opt/genestack` stay.

A machine that is not an appliance still updates the program file from `version.json`. Settings on that install downloads the version you pick. An appliance does not replace that file on its own. The image is the version.

A newer qcow2 on a later release is a new appliance. Import that file when you are creating another machine. Copying it over a disk that is already running would drop the database.

The release workflow publishes the qcow2. It does not push the bootc image to a public registry. Upgrade pulls the image this machine already tracks. To point it at a registry, build the image with `scripts/build-appliance.sh`, push it, and run `bootc switch`.

## Build the disk yourself

On a Linux x86_64 machine with podman, from a checkout:

```bash
./scripts/compile-console.sh
./scripts/build-appliance.sh
```

The disk is `dist/genestack-console-appliance-<version>-amd64.qcow2.xz`. The installer is `dist/genestack-console-appliance-<version>-amd64.iso`.

The operating system is Ubuntu 26.04 LTS. The build starts from `docker.io/library/ubuntu:26.04` and installs the kernel, OpenSSH, cloud-init, and bootc 1.16.14. Ubuntu 26.04 has the ostree release that bootc links against. The disk boots with GRUB. bootupd installs that bootloader, because bootc's ostree install requires bootupd. bootupd writes the boot environment with `/usr/bin/grub2-editenv`. Ubuntu names that program `grub-editenv`, so the image links the name bootupd expects. The ISO is a second image that boots live and runs `bootc install to-disk` for the appliance image. Its live initramfs needs `dmsetup` and `parted`, because dracut will not build the live module without them. The menu default and the timeout in the ISO config are numbers. Image Builder rejects them when they are quoted. `GSC_BOOTC_BASE` selects the Ubuntu image. `GSC_BOOTC_VERSION` selects the bootc release. `GSC_IMAGE_BUILDER` selects the image-builder container. The default is `ghcr.io/osbuild/image-builder-cli:latest`.

A tag on this repository runs that build after the Linux binary is published and attaches the compressed qcow2 and the ISO to the same GitHub Release. The binary, `version.json`, `console.sh`, and `console.ps1` are published first. A failure in the disk build leaves that release in place. A disk that finished is attached even when the ISO step fails.
