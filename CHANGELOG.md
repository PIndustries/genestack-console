# Changelog

Each published build has a section here. The release workflow copies the matching section onto the GitHub Release. The version heading is the `VERSION` string, without the leading `v`.

The oldest GitHub Release is v2026.10.03. The source history starts two version numbers earlier. Those two sections are the commits. They are not downloadable tags.

## 2026.10.07.19

The installer image includes bootc so Image Builder can read its install config.

- The disk image built. The CD image stopped because Image Builder runs bootc inside the installer image, and that program was not there.

## 2026.10.07.18

The installer CD menu gives its numbers as numbers.

- The disk image built. The CD image stopped because image-builder reads the menu default and the timeout as numbers, and the file had them in quotes.

## 2026.10.07.17

The installer CD image includes the tools its live boot needs.

- The disk image built. The CD image stopped because dracut's live module needs dmsetup and parted. Those packages are now in the image.

## 2026.10.07.16

The appliance image includes the GRUB tool bootupd uses to write the boot environment.

- bootc install stopped because bootupd looks for grub2-editenv. Ubuntu names that program grub-editenv. The image links the name bootupd expects.

## 2026.10.07.15

The appliance image includes bootupd so bootc can install the disk.

- bootc install stopped because an ostree image requires bootupd. The image builds bootupd and a GRUB program that can read the boot entries.

## 2026.10.07.14

The appliance image includes podman so bootc can open its container store.

- bootc install stopped because podman was not on PATH. Install and upgrade both use it to create the container store.

## 2026.10.07.13

The appliance disk build no longer tells bootc that SELinux is on. Settings can upgrade or roll back this console.

- bootc install stopped because the image had an SELinux config and no policy. The file-context list Image Builder needs stays. The config file does not.
- On the appliance, SSH does not start. The page listens on port 8080. Settings lists this boot and the previous image. Upgrade and Rollback reboot into the one you pick.

## 2026.10.07.12

The appliance image includes the ostree prepare-root config bootc install reads.

- `/usr/lib/ostree/prepare-root.conf` enables composefs. The initramfs includes ostree-prepare-root.

## 2026.10.07.11

The appliance image includes setfiles so Image Builder can label the disk.

- The labeling step runs inside the Ubuntu image. That image did not contain the program.

## 2026.10.07.10

The appliance image includes the SELinux file-context list Image Builder reads.

- Ubuntu does not ship that file. The disk build stopped while labeling the image.

## 2026.10.07.9

The appliance disk build no longer passes a filesystem blueprint.

- Image Builder rejects `customizations.filesystem` for a bootc qcow2. The published disk is 10 GiB, and the resize command in the appliance docs still grows it.

## 2026.10.07.8

The appliance image enables the Ubuntu 26.04 resolver and cloud-init units.

- systemd-resolved is installed. cloud-init uses cloud-init-main and cloud-init-network.
- The kernel dependency is satisfied by linux-firmware-minimal, so the full firmware set stays off the disk and the ISO.

## 2026.10.07.7

The release includes an installer ISO, and the appliance build can finish.

- The ISO is `genestack-console-appliance-<version>-amd64.iso`. Boot it, name a disk, and it installs the appliance. The qcow2 is still the already-installed disk.
- The appliance is a small Ubuntu 26.04 boot image. The bootc build installs go-md2man, and the image declares ext4 as its root filesystem.

## 2026.10.07.6

The appliance build includes the clang library bootc needs.

- The disk file is `genestack-console-appliance-<version>-amd64.qcow2.xz`.

## 2026.10.07.5

The appliance disk is Ubuntu 26.04 LTS.

- Ubuntu 26.04 has the ostree release bootc 1.16.14 links against. The file is `genestack-console-appliance-<version>-amd64.qcow2.xz`.

## 2026.10.07.4

The Ubuntu appliance disk is attached to this release.

- The disk build compiles bootc with a current Rust toolchain. The file is `genestack-console-appliance-<version>-amd64.qcow2.xz`.

## 2026.10.07.3

A release also ships an Ubuntu appliance disk, and the shell drawer keeps the height you set.

- The same tag includes `genestack-console-appliance-<version>-amd64.qcow2.xz`. Boot that disk for an appliance. The install command stays for a machine you already have.
- The disk is Ubuntu 24.04 LTS. Pass an SSH key when you create the virtual machine. cloud-init writes it for the user `console`.
- Drag the bottom edge of the shell drawer to set the height. This browser remembers that height. Expand fills the screen. The × on a shell tab closes that session.

## 2026.10.07.2

The image cache keeps Genestack images and lets an operator add the rest.

- Genestack images stay on the list and cannot be removed. Other images can be added, edited, and removed so an air-gapped environment can pull them.
- Cache images and charts pulls that list and what the cluster is running.

## 2026.10.07.1

The image cache uses this console's address, and a machine opens in a dialog.

- Configure sets the address machines pull from and which registries are mirrored. An empty address follows the PXE next-server, then this console's own address. Start caches brings the proxies up. Cache images and charts also pulls what the cluster is running.
- Overview counts the machines, how many are Ready, and how many have a role and are not joined. A metal-rebuild line shows only while that job is queued or running.
- Machines opens on All. The host name opens that machine. OS is Talos or Ubuntu. Cluster is In cluster, Not joined, or No role.
- Look around means jobs only write a log. Apply means jobs change the machines.
- Backtick lists the machines. Shell is SSH. Console is the management port. An Ubuntu machine installed from here accepts the ubuntu login.
- Kubernetes groups workloads by namespace. Logs and Describe open in front of the page.

## 2026.10.06.5

The sign-in page has a moving field.

- The login background is a green meadow with slow light behind the card. The rest of the console stays quiet. Reduced motion turns the extra motion off.

## 2026.10.06.4

The deploy host signs in with a local account.

- Sign in with Genestack stays on my.genestack.dev. A deploy console does not show that button. my.genestack.dev does not open a session on that host.
- A site that points the console at its own identity provider still shows that provider.

## 2026.10.06.3

The remote console shows the machine, and Machines lists the computers first.

- The iLO console picture connects. The socket script parses, and the KVM stays on this console.
- Machines shows the list first. Talos and Ubuntu filter it. Add a machine is a dialog. Each row shows up, running, connect, and authenticated. A Talos machine with no SSH stays marked Talos.
- A tab shows a spinner until its first load finishes.
- Image cache is its own tab. Overview stays the install map. Maintenance on the map still caches images and charts.
- An update replaces the binary and restarts after the request returns. The open page waits and reloads. A published binary installs the next release on its own.

## 2026.10.06.2

Live logs follow the newest line.

- Job, deploy, pod, node, and serial logs open on the newest line and stay there while output arrives. Scrolling up holds the view. Latest jumps back.
- The scrollbar on those boxes stays visible.

## 2026.10.06.1

Inventory is one list of hostname and IP.

- Guided setup adds each machine by hostname and IP. A virtual machine and a physical server are the same row. On Machines, record Talos or Ubuntu, then Deploy.
- A provider account or a network sighting stays under Import or discover and writes that same list.
- A cluster that is already running is Settings, Access, Adopt Kubespray. Paste the kubeconfig. Dry run must be off or the file is not stored.
- The summary opens Machines.

## 2026.10.05.6

The console follows the latest build, and the page uses the whole window.

- The running console checks the published release. When a newer build exists and no job is queued or running, it installs that binary, restarts, and reloads the page. The release notes stay up until they are acknowledged. Changelog, at the bottom of the sidebar, opens the same notes. `update.watch` defaults to on when that key is omitted. `update.auto` is only the installer's daily timer. The example `update.url` is the GitHub latest `version.json`.
- A deleted environment leaves the sidebar and every environment list together.
- The install places `talosctl` v1.14.2 on the deploy host. A reboot request applies with mode `auto`, then reboots the node. The client does not receive `--mode reboot`. The metal image stays v1.13.9.
- The page fills the window, including a wide display, and stacks on a narrow one. Toasts sit on the screen. A structured API error shows its message. When live metrics are not published, the panel says so once.

## 2026.10.05.5

Machines is one page, and the image cache is on Overview.

- Talos and Ubuntu are tabs on Machines. Add a machine there. Overview is the install map. Kubernetes and OpenStack are their own pages.
- Image cache sits above the map. It lists each pull-through registry and the Helm charts. Cache images and charts copies the images this cluster runs onto this console. An admin starts it. It does not power a machine. OCI charts are stored in that cache. The other charts stay on their Helm repo.

## 2026.10.05.4

The environment overview fits the window, and Delete is on the environment list.

- An idle deploy-host terminal stays one line. Open terminal still opens a shell, and expand still fills the screen.
- Repair, restack, image cache, Tempest, and Greenfield metal wipe are under Maintenance. The metal wipe still asks before it runs.
- Delete is on each environment card and on the environment page. It asks before it removes the environment. An admin is required.

## 2026.10.05.3

Guided setup lets you add the hosts you have. One machine and three machines are examples.

- Add server is the action on Static IPs / SSH. Each row keeps its own roles. A further host starts as a worker when a control plane is already listed.
- One machine and Three machines fill an example. One machine runs every role. Three machines are one control plane and two workers. Add or remove hosts and change the roles after that.

## 2026.10.05.2

The dry-run pill follows the environment you have open, and Guided setup can shape the lab.

- On an environment, the top bar says dry-run ON or dry-run OFF for that environment. Apply on this environment updates the pill. Fleet cards say dry-run or applies, including when the environment inherits the console default.
- Guided setup, Servers, Static IPs / SSH, has One machine and Three machines. One machine runs every role. Three machines are one control plane and two workers. A new static panel starts as one machine.

## 2026.10.05.1

Guided setup creates the environment when the saved id is gone.

- Create environment was retrying an id left in the browser from an earlier attempt. If that environment had been deleted, the page stopped on "Environment not found" and did not create the new one.
- A missing id, or a name you changed on Basics, now creates the environment you just named.

## 2026.10.04.8

Hosts shows the next step after Talos is already installed.

- The diagram lists that Talos job, then infrastructure, operators, cni, OpenStack core, and compute and network. The same deploy continues through platform extras and observability. Tempest stays its own step.
- Deploy from infrastructure queues `genestack.deploy` with the start stage set to `infrastructure`. Kubernetes stays as it is. Talos bootstrap does not run a second time.
- The button stays closed until that bootstrap job has succeeded, and it asks before it queues the deploy.
- Ubuntu that is already installed does not use this button. That deploy still starts at the hosts stage, over SSH.

## 2026.10.04.7

The getting-started page and the on-screen hints describe the machines you have.

- The four starts are a Mac with one local VM, one Ubuntu machine, one dedicated server, or three machines in one environment.
- You add a hostname and an IP, boot a Talos ISO yourself, or use Ubuntu that is already installed. A virtual machine and a physical server start the same way.
- v2026.10.04.6 still names a hypervisor in a few screens. This build does not.

## 2026.10.04.6

A new environment starts from a hostname and an IP.

- Guided setup shows the Talos ISO and leaves the image blank, so the console uses its factory default.
- Apply on this environment turns that one environment live. Look around only turns it back to logging. The switch does not edit `config.yaml` and does not restart a service.
- The install docs say the published binary is the production install. A source checkout is for changing the console.

## 2026.10.04.5

Running the install command again follows the latest release.

- The same `curl` replaces the Linux binary when a newer release exists. It leaves `config.yaml`, the database, and `/opt/genestack` in place.
- `--version` installs one published tag, including an older build. A later run with no version returns to latest.

## 2026.10.04.4

`genestack-console update` and `console.sh update` replace the Linux binary from the latest release and restart the console and the worker. Config, the database, and `/opt/genestack` stay in place.

## 2026.10.04.3

A management port is optional.

- Talos is already installed applies a config to every saved address and does not power the machines. You boot the Talos ISO yourself.
- Already have an OS records Ubuntu that is already there. The record does not install or reboot.
- A management port remains the way to power a machine, ask for one network boot, and open its console.

## 2026.10.04.2

The page loads again. v2026.10.04.1 failed to open because the overview map imported a console function the bare-metal page never exported. That function now lives with the map.

`docs/first-cluster.md` is the page after sign-in: a cluster you already have, a cluster the console installs, or a small lab.

## 2026.10.04.1

Do not install this build. The page does not load. Use v2026.10.04.2 or later. The work below is in that later build.

- A Hosts row can install or reinstall Ubuntu or Talos. Install Ubuntu on a bare-metal row is one machine and the whole disk. It does not install Kubernetes or OpenStack. The management-port wall can show more than one console. Opening a console does not power the machine.
- The sidebar and the cards use the glass theme.
- `console.sh` and `console.ps1` are on the release. `get.genestack.dev` redirects to them.
- The manual says the console runs on its own server outside the cluster. L2 with the machines is recommended, so the console can answer DHCP and serve the boot file. When the console cannot sit on that network, an agent at the site does.
- The console boots Talos itself.
- Each operation lives in its own file. Notification channels replace the old machine client.
- Sign-in can use OAuth. A refresh token does not end the other login.
- The deploy host can reach a site over WireGuard, Tailscale, or a Cloudflare Tunnel.
- The console database can be copied between SQLite and Postgres. A Python SDK and traces ship with this build.
- A job removes secret files it created when the job ends. It does not keep a kubeconfig it did not create.

## 2026.10.03

First published build. The install line points at genestack.dev. The Linux binary is the release asset, and `version.json` names that file.

This tag includes the two unpublished version numbers below. my.genestack.dev is an account page, not a second console.

## 2026.10.02

Not a GitHub Release. The commits are inside v2026.10.03.

- Bare metal boots in two layers. Commission runs from a RAM disk, the console accepts one wipe report, then it network-boots Talos once for that machine. The next boot stays disk unless you choose otherwise. An ISO boot is rejected on this path because an ISO does not wipe the disks.
- The docs call my.genestack.dev an account link. The Apache-2.0 license text is in the tree.
- The deploy host, the boot order, and that account page are written on the Genestack page.
- The get.genestack.dev website files were removed from this repository.

## 2026.10.01

Not a GitHub Release. This is the first public commit, and it is inside v2026.10.03.

- One web page runs more than one environment. A worker runs the long jobs. Agents dial out to the console and run the work at a site.
- Each environment has its own SSH key. Settings are a versioned document. Push writes them to the deploy host. Deploy runs from there and the log streams back.
- Bare metal is PXE, DHCP, and a Talos install, served from the agent sidecar. Machines on the local network can be discovered, and a management port can be registered.
- The API is `/api/v1`. The database starts as SQLite. Sign-in can be a local account, an API key, or OIDC.
