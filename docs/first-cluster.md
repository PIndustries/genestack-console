# Your first cluster

You installed the console and the sign-in page opened. This page is what to do next. The install itself is [Install the Genestack Console](install.md).

Genestack is the project that installs OpenStack on Kubernetes. The console is the page you use to run that install, and to operate the cloud after it is up. The computer you installed the console on is the deploy host. It stays outside the cluster. It is not a Kubernetes node and not an OpenStack compute node.

A virtual machine is a fine deploy host. Any Ubuntu machine works for the console. The cluster is other machines. The console machine is not the cloud.

## Pick the machines you have

Four starts. Each one ends in this console. The computer that runs the console stays outside the cluster.

| You have | Start |
| --- | --- |
| A Mac | [Mac, one local virtual machine](#mac-one-local-virtual-machine) |
| One machine that already has Ubuntu | [One Ubuntu machine](#one-ubuntu-machine) |
| One dedicated server | [One dedicated server](#one-dedicated-server) |
| Three machines | [Three machines in one environment](#three-machines-in-one-environment) |

An environment is one cloud. A second cloud is a second environment. One environment can be a single machine. Another environment can be three machines. They do not share servers. Apply on this environment, and Deploy, belong to that environment.

## Open the page

On the deploy host the page is `http://127.0.0.1:8080/ui`. From a laptop:

```bash
ssh -L 8080:127.0.0.1:8080 <deploy-host>
```

Sign in as `admin`. On Linux the password is in `/opt/genestack-console/ADMIN_CREDENTIALS.txt`. On a Mac it is in `~/genestack-console/ADMIN_CREDENTIALS.txt`. The file mode is `0600`. A Mac lab opens `http://127.0.0.1:8080/ui` on that Mac.

The first screen is this console. It says the deploy host stays outside the cluster, the other machines are added by hostname and IP, and Deploy is how OpenStack gets installed. Guided setup creates one environment and points it at two directories on the deploy host.

| Path | What it is |
| --- | --- |
| `/opt/genestack` | The [Genestack](https://github.com/rackerlabs/genestack) checkout. The console runs the install scripts from here. |
| `/etc/genestack` | Inventory and the settings those scripts read. |

An environment is one cloud: a lab, one rack, or one site. Jobs and passwords for that cloud stay inside it.

A fresh install leaves the console in a dry run. A dry run logs what a job would do and does not change servers, and it does not store a kubeconfig. The bar on the environment says so. **Apply on this environment** turns that one environment live. Jobs from then on change the machines. **Look around only** puts it back to a log. That switch is in the console. It does not edit a file and it does not restart a service. Leave the environment logging while you are only looking around.

## Mac, one local virtual machine

Use this to learn the console on a Mac. It is a laptop lab. Docker Desktop is running. QEMU is installed (`brew install qemu`). Apple Silicon and Intel both work. The installer picks the Ubuntu cloud image for that Mac.

```bash
curl -fsSL https://get.genestack.dev/console.sh | bash -s -- --dev
```

The console runs in Docker and listens on `http://127.0.0.1:8080/ui`. The files are under `~/genestack-console`. The same command creates one Ubuntu virtual machine on the Mac and registers an environment named `genestack-aio`. The installer's default for that machine is 4 vCPU, 8 GiB of memory, and a 60 GB disk. A later run of the same command keeps that disk, the settings, and the database.

The console stays on the Mac. The Ubuntu machine is the one machine the cloud can use. From the Mac:

```bash
ssh -i ~/genestack-console/ssh/genestack-aio_key -p 2222 ubuntu@127.0.0.1
```

Open the environment `genestack-aio`. The bar says this environment only logs. **Apply on this environment** when you mean the jobs to run. `#/hosts` is Host VMs. That page lists this virtual machine and can start it, stop it, and show its serial log. Machines, inside the environment, is the cluster inventory. Talos and Ubuntu are the tabs. They are different screens.

On the virtual machine, `sudo /usr/local/sbin/genestack-aio-setup.sh` clones Genestack and runs `bootstrap.sh`. That prepares the checkout. It does not install OpenStack. This lab machine is reached through an SSH port on the Mac, so the inventory cannot record it as a normal address. A cloud you will keep is one Ubuntu machine, below, or the dedicated server. Those machines have addresses of their own, and Deploy runs from this console.

## One Ubuntu machine

Use this when the cloud is one computer that already has Ubuntu, and the console is a different computer. The cloud machine can be a physical server or a virtual machine. The hypervisor is the one you already run. The console does not create that machine, and it does not install the hypervisor.

The console computer is Ubuntu as well. A virtual machine is fine. Install the console there:

```bash
curl -fsSL https://get.genestack.dev/console.sh | bash
```

Run that command again on a machine that already has the console. It follows the latest release and leaves the settings and the database in place. This computer is not a Kubernetes node.

The cloud machine needs a hostname and an IP address the console can reach. A starting size for a virtual machine is 4 vCPU, 16 GiB of memory, and an 80 GB disk. Deploy is where a small machine runs out of room. The console does not refuse a small machine.

In the console, Guided setup:

1. Basics names the environment. Connect stays on this console.
2. Deployment: open Advanced and choose Kubespray. Ubuntu is already installed, so this path uses SSH.
3. Servers: Static IPs / SSH. Add the cloud machine and set its roles. One machine that runs every role is an example on that step. Add more hosts when you have them. After the environment exists, Machines, the same roles are the preset **All-in-One (single node)**.
4. **Apply on this environment.**
5. Machines, then Ubuntu. Select the row. **Already have an OS.** That records the machine. It does not reboot it.
6. Settings, then Config, then Deploy. Deploy installs Kubernetes and OpenStack over SSH.

## One dedicated server

Two uses. Pick the one that matches the computer.

The server is the console, and it can run a virtual machine. Linux with KVM, which is `/dev/kvm`, uses the same lab as the Mac:

```bash
curl -fsSL https://get.genestack.dev/console.sh | bash -s -- --with-aio-vm
```

The console is the published binary on that server. The installer creates one local Ubuntu virtual machine and an environment named `genestack-aio`. The default size is 4 vCPU, 8 GiB of memory, and a 60 GB disk. SSH is `ssh -i /opt/genestack-console/ssh/genestack-aio_key -p 2222 ubuntu@127.0.0.1`. Open that environment, apply it when you mean the jobs to run, and open `#/hosts` for the virtual machine. On the virtual machine, `sudo /usr/local/sbin/genestack-aio-setup.sh` clones Genestack and runs `bootstrap.sh`. A computer that cannot run a local virtual machine uses the one Ubuntu machine above: the console on one computer, the cloud on the other.

The server is the one cloud machine. Install Ubuntu on it. Install the console on a different computer: the Mac command above, or any other Ubuntu computer with the plain install command. In that console, add this server on Servers and set its roles, or use the one-machine example. Add more hosts when you have them. After the environment exists, Machines, the same roles are the preset **All-in-One (single node)**. Then **Already have an OS**, **Apply on this environment**, and Deploy. The console stays on the other computer.

## Three machines in one environment

Use this when you want a control plane and two workers. The three machines can be physical servers or virtual machines. The console is one more computer. It is not one of the three.

A starting lab is 4 vCPU, 16 GiB of memory, and an 80 GB disk on each of the three. Talos can boot on less. Deploy is where a small machine runs out of room. The console does not refuse a small machine.

Talos is the operating system this console prefers. Boot the three machines from this ISO. It is the same image the console applies. Leave the image field in Guided setup blank. Guided setup shows this same address.

```text
https://factory.talos.dev/image/613e1592b2da41ae5e265e8789429f22e121aab91cb4deb6bc3c0b6262961245/v1.13.9/metal-amd64.iso
```

A SCSI disk is often `/dev/sda`. Confirm the name on the machine. The one step outside the console is booting that ISO yourself. The console does not insert a disc and does not power the machine. Then follow Talos is already installed, below. Add each host and set its roles. Three machines with one control plane and two workers is an example on that step. Add or remove hosts for the machines you have. After the environment exists, Machines, the same roles are the preset **Control Plane** on one machine and **Worker (compute + storage)** on the other two.

Ubuntu that is already installed follows Ubuntu is already installed, below. Add each host and set its roles. Three machines with one control plane and two workers is an example. After the environment exists, Machines, the same roles are the preset **Control Plane** on one machine and **Worker (compute + storage)** on the other two.

The console computer is Ubuntu and the plain install command:

```bash
curl -fsSL https://get.genestack.dev/console.sh | bash
```

Network boot from the console is optional, and it is easy to get wrong. The machines and the console have to be on a network where this console is the only DHCP server. Set each machine to boot from the network, and power it yourself. If something else is also answering DHCP, the machine will not boot from the console.

## You already have a Genestack cluster

Do not reinstall it. Do not run a wipe, and do not run Deploy, until you mean to change that cloud.

1. On the deploy host, use the Genestack checkout and the `/etc/genestack` inventory that this cluster was built from. Guided setup asks for those two paths. If the checkout is not on this machine yet, clone it to `/opt/genestack`. Do not run `bootstrap.sh` over an inventory you already use.
2. The console has to reach the Kubernetes API of that cluster. A kubeconfig is the file `kubectl` uses. Copy it to the deploy host. Do not paste it into a chat or a ticket.
3. Open the environment, then Settings, then Access, then Hosts. Paste the kubeconfig and choose Adopt Kubespray. That records the cluster. It does not clone Kubespray, it does not run Ansible, and it does not reinstall Kubernetes. Dry run must be off or the job will not store the file.
4. Kubernetes lists the nodes once the console can reach the API. If the nodes are Talos, Machines, then Talos, is where you manage them. Talosconfig is the file for Talos. Download it from Machines after the console has it.

Day to day you stay on this environment. Settings, then Config, is the settings document. Save stores a new version in the console. The menu next to the version opens an older one, read-only. Push writes the current version onto the deploy host. Deploy writes it and then runs the Genestack scripts. Saving by itself does not change the live cloud.

## You want the console to install the cluster

A management port is optional. Vendors call it the BMC, iLO, or iDRAC. When a server has one, the console can power it, ask for one network boot, and open its console. That is deeper control. You can start with a hostname and an IP.

The Genestack checkout at `/opt/genestack` is what the install scripts run from.

### Talos is already installed

Use this when Talos is already running and waiting for a config. A Talos ISO does that. A virtual machine, or any machine with no management port, starts here. You boot the ISO yourself. The console does not insert the ISO and does not power the machine.

1. Guided setup. Basics names the environment. Connect stays on this console unless a site is somewhere this machine cannot reach. Deployment stays on Talos. The cluster name is a short DNS name. The install disk is the device Talos writes, often `/dev/sda`. Confirm the name on the machine. Leave the image blank unless you have your own.
2. Servers: Static IPs / SSH. Add each host, its hostname, its IP, and its roles. One machine that runs every role, and three machines with one control plane and two workers, are examples. Add the hosts you have.
3. On the environment, open Machines and stay on Talos. The tab does not install an operating system.
4. Talos is already installed. An admin starts it. The job is `genestack.talos.bootstrap`. The deploy host has to reach each address. Talos in maintenance listens there. Guided setup has to have saved the inventory path, because the job writes under that directory. The job applies a Talos config to every saved address, bootstraps etcd once, and fetches the kubeconfig. The confirm names the whole inventory, not one row.
5. Apply on this environment before you mean that job to run. While the environment only logs, the job records the commands and does not send them.

Kubernetes is up after that job succeeds. On Machines, Talos, the diagram marks infrastructure, and the button is Deploy from infrastructure. That Deploy starts there, so it does not run the Talos bootstrap a second time. A second bootstrap stops when the talos directory under the inventory path, usually `/etc/genestack/talos`, already holds `secrets.yaml` or `talosconfig`. Remove those files only when you mean to create a new cluster identity.

### Ubuntu is already installed

Use this for a group of servers that already have Ubuntu, including guests you installed yourself. Kubespray adopts machines that already have an operating system and SSH.

1. Guided setup. Open Advanced: Kubespray (Ansible). On Servers choose Static IPs / SSH. Add each host, its hostname, its IP, and its roles. The deploy host has to reach those machines over SSH.
2. Open Machines and choose Ubuntu. The tab does not reboot anything.
3. Select the rows. Already have an OS. That records them. The row shows Kubespray recorded. It does not reboot them, install anything, or start a playbook. Clear that record removes the mark. Roles stay.
4. Across the group you still want a Kubernetes control plane, etcd, OpenStack control, a worker, and storage. After the environment exists, the Machines presets are those same roles.
5. Apply on this environment when you want Deploy to run. Deploy uses SSH. It does not network-boot those machines. While the environment only logs, Deploy records the work and does not change the guests.

A machine may stay a plain Ubuntu server. Install Ubuntu on a Machines row is a different action. It puts Ubuntu on that one machine and waits until it answers. It does not install Kubernetes or OpenStack. A host that already answers stays on disk. A host that does not answer is network-booted, and that boot uses a management port when the machine has one.

Settings, then Access, then Hosts, then Adopt Kubespray is the record for a cluster that is already up. Paste the kubeconfig there. It does not clone Kubespray and it does not run Ansible. Dry run must be off or the file is not stored.

### The console installs the operating system

Use this when the machines are empty and they have a management port. Talos is the operating system the console installs. It boots a machine straight into Kubernetes. The console answers DHCP and serves the boot file, wipes the disks, boots Talos, then runs the Genestack scripts. DHCP is how a machine asks for an address. The boot file is the small program the network card downloads when the machine starts from the network instead of from its disk.

L2 is recommended. L2 means the deploy host is on the same local network as the machines, so the console can hand out addresses and the boot file itself. When the deploy host cannot be on that network, an agent on a computer that is does that job, and you still start the work from the console.

In Guided setup, Servers can be BMC / Redfish or PXE. On Machines, record the management port and the MAC address of the port on the install network. A MAC address is the hardware address of that network card.

Settings, then Config. Change what you need. Save. Each save is a version. Apply on this environment only when you are ready for the wipe. Deploy cluster asks you to confirm the wipe. It pushes the settings, network-boots Talos, then runs the Genestack pipeline: Kubernetes, then OpenStack. A server you did not select stays on its own disk. The log is Activity, then Jobs.

Do not put the console's DHCP on a network other machines depend on.

An ISO is rejected on this wipe path because an ISO does not wipe the disks. `baremetal.node.iso_boot` puts an ISO in the management-port virtual CD when the network card cannot PXE. That needs a management port. It is separate from booting a Talos ISO yourself and then choosing Talos is already installed.

## Change the settings later

Settings, then Config, is one YAML document for the environment: the provider, the servers, the network, and which OpenStack services are on. The console checks it when you save.

- Save stores a new version. Older versions stay in the menu. Switch back to current before you edit.
- Render preview shows the files without writing them.
- Push writes the current version into `/etc/genestack`. Deploy does this and then runs the install.
- Deploy (dry-run) rehearses one Deploy. The bar on the environment is how that environment logs or applies. The console-wide default stays `dry_run` in `/opt/genestack-console/config.yaml`. You do not edit that file to install a cloud.

Back up `/opt/genestack-console/config.yaml` and the console database together. Passwords in the database are encrypted with the key in that file. A copy of the database without the file cannot be decrypted.

`https://my.genestack.dev` is an account page you can connect later. It is how the Apple apps reach this console. It does not run the console, and you do not need it to install a cluster.

## If something fails

Open Activity, then Jobs, and read the log of the job that failed. A second job that changes the same environment is refused while one is still queued or running.

Run from source when you are changing the console and testing that change. A production deploy host uses the binary from the install command. A checkout can run there. The install we support in production is that binary. The checkout steps are [Run from source](../README.md#run-from-source).

| You need | Read |
| --- | --- |
| Install the console on Linux, a Mac, or Windows | [Install](install.md) |
| How a server gets Talos or Ubuntu | [Genestack Console](genestack-guide.md) |
| The Machines row and the management-port wall | [The console web page](ui.md) |
| Every job, including deploy and the wipe | [Jobs](jobs.md) |
| The screens | [The console web page](ui.md) |
