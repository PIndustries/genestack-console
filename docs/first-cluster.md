# Your first cluster

You installed the console and the sign-in page opened. This page is what to do next. The install itself is [Install the Genestack Console](install.md).

Genestack is the project that installs OpenStack on Kubernetes. The console is the page you use to run that install, and to operate the cloud after it is up. The computer you installed the console on is the deploy host. It stays outside the cluster. It is not a Kubernetes node and not an OpenStack compute node.

A virtual machine is a fine deploy host. VMware, KVM, or any other Ubuntu guest works for the console. The cluster is other machines. The console VM is not the cloud.

## Open the page

On the deploy host the page is `http://127.0.0.1:8080/ui`. From a laptop:

```bash
ssh -L 8080:127.0.0.1:8080 <deploy-host>
```

Sign in as `admin`. The password is in `/opt/genestack-console/ADMIN_CREDENTIALS.txt`. The file mode is `0600`.

The first screen is Guided setup. It creates one environment and points it at two directories on the deploy host.

| Path | What it is |
| --- | --- |
| `/opt/genestack` | The [Genestack](https://github.com/rackerlabs/genestack) checkout. The console runs the install scripts from here. |
| `/etc/genestack` | Inventory and the settings those scripts read. |

An environment is one cloud: a lab, one rack, or one site. Jobs and passwords for that cloud stay inside it.

A fresh install sets `dry_run: true` in `/opt/genestack-console/config.yaml`. A dry run logs what a job would do and does not change servers, and it does not store a kubeconfig. Set `dry_run: false` when you mean the job to apply, then restart both units so the worker reads the file:

```bash
sudo systemctl restart genestack-console genestack-console-worker
```

Leave dry run on while you are only looking around.

## You already have a Genestack cluster

Do not reinstall it. Do not run a wipe, and do not run Deploy, until you mean to change that cloud.

1. On the deploy host, use the Genestack checkout and the `/etc/genestack` inventory that this cluster was built from. Guided setup asks for those two paths. If the checkout is not on this machine yet, clone it to `/opt/genestack`. Do not run `bootstrap.sh` over an inventory you already use.
2. The console has to reach the Kubernetes API of that cluster. A kubeconfig is the file `kubectl` uses. Copy it to the deploy host. Do not paste it into a chat or a ticket.
3. Open the environment, then Settings, then Access, then Hosts. Paste the kubeconfig and choose Adopt Kubespray. That records the cluster. It does not clone Kubespray, it does not run Ansible, and it does not reinstall Kubernetes. Dry run must be off or the job will not store the file.
4. Platform, then Kubernetes, lists the nodes once the console can reach the API. If the nodes are Talos, Platform, then Machines, is where you manage them. Talosconfig is the file for Talos. Download it from Platform after the console has it.

Day to day you stay on this environment. Settings, then Config, is the settings document. Save stores a new version in the console. The menu next to the version opens an older one, read-only. Push writes the current version onto the deploy host. Deploy writes it and then runs the Genestack scripts. Saving by itself does not change the live cloud.

## You want the console to install the cluster

A management port is optional. Vendors call it the BMC, iLO, or iDRAC. When a server has one, the console can power it, ask for one network boot, and open its console. That is deeper control. You can start with a hostname and an IP.

The Genestack checkout at `/opt/genestack` is what the install scripts run from.

### Talos is already installed

Use this when Talos is already running and waiting for a config. A Talos ISO does that. A VMware ESXi guest, a KVM guest, or any machine with no management port starts here. You boot the ISO yourself. The console does not insert the ISO and does not power the machine.

1. Guided setup. Basics names the environment. Connect stays on this console unless a site is somewhere this machine cannot reach. Deployment stays on Talos. The cluster name is a short DNS name. The install disk is the device Talos writes, often `/dev/sda`. Confirm the name on the machine. Leave the image blank unless you have your own.
2. Servers: Static IPs / SSH. Enter each hostname, IP, and roles. One machine needs the control plane role. Every other saved machine is a worker.
3. On the environment, Platform, then Hosts. The path above the table stays Talos. Choosing the path does not install an operating system.
4. Talos is already installed. An admin starts it. The job is `genestack.talos.bootstrap`. The deploy host has to reach each address. Talos in maintenance listens there. Guided setup has to have saved the inventory path, because the job writes under that directory. The job applies a Talos config to every saved address, bootstraps etcd once, and fetches the kubeconfig. The confirm names the whole inventory, not one row.
5. Set `dry_run: false` and restart the two units before you mean that job to apply. A dry run logs the commands and does not send them.

Kubernetes is up after that job succeeds. OpenStack is Deploy, on Settings, then Config. Open Start stage and choose `infrastructure`, so Deploy does not run the Talos bootstrap a second time. A second bootstrap stops when the talos directory under the inventory path, usually `/etc/genestack/talos`, already holds `secrets.yaml` or `talosconfig`. Remove those files only when you mean to create a new cluster identity.

### Ubuntu is already installed

Use this for a group of servers that already have Ubuntu, including guests you installed yourself. Kubespray adopts machines that already have an operating system and SSH.

1. Guided setup. Open Advanced: Kubespray (Ansible). On Servers choose Static IPs / SSH. Enter each hostname, IP, and roles. The deploy host has to reach those machines over SSH.
2. On Hosts, the path above the table is Kubespray. Choosing the path does not reboot anything.
3. Select the rows. Already have an OS. That records them. The row shows Kubespray recorded. It does not reboot them, install anything, or start a playbook. Clear that record removes the mark. Roles stay.
4. Across the group you still want a Kubernetes control plane, etcd, OpenStack control, a worker, and storage. The add-host presets are those roles.
5. Set `dry_run: false` and restart the two units when you want Deploy to run. Deploy uses SSH. It does not network-boot those machines.

A machine may stay a plain Ubuntu server. Install Ubuntu on a Hosts row is a different action. It puts Ubuntu on that one machine and waits until it answers. It does not install Kubernetes or OpenStack. A host that already answers stays on disk. A host that does not answer is network-booted, and that boot uses a management port when the machine has one.

Settings, then Access, then Hosts, then Adopt Kubespray is the record for a cluster that is already up. Paste the kubeconfig there. It does not clone Kubespray and it does not run Ansible. Dry run must be off or the file is not stored.

### The console installs the operating system

Use this when the machines are empty and they have a management port. Talos is the operating system the console installs. It boots a machine straight into Kubernetes. The console answers DHCP and serves the boot file, wipes the disks, boots Talos, then runs the Genestack scripts. DHCP is how a machine asks for an address. The boot file is the small program the network card downloads when the machine starts from the network instead of from its disk.

L2 is recommended. L2 means the deploy host is on the same local network as the machines, so the console can hand out addresses and the boot file itself. When the deploy host cannot be on that network, an agent on a computer that is does that job, and you still start the work from the console.

In Guided setup, Servers can be BMC / Redfish or PXE. On Hosts, record the management port and the MAC address of the port on the install network. A MAC address is the hardware address of that network card.

Settings, then Config. Change what you need. Save. Each save is a version. Set `dry_run: false` and restart the two units only when you are ready for the wipe. Deploy cluster asks you to confirm the wipe. It pushes the settings, network-boots Talos, then runs the Genestack pipeline: Kubernetes, then OpenStack. A server you did not select stays on its own disk. The log is Activity, then Jobs.

Do not put the console's DHCP on a network other machines depend on.

An ISO is rejected on this wipe path because an ISO does not wipe the disks. `baremetal.node.iso_boot` puts an ISO in the management-port virtual CD when the network card cannot PXE. That needs a management port. It is separate from booting a Talos ISO yourself and then choosing Talos is already installed.

## A lab of virtual machines

Use this when you have no physical servers. VMware ESXi is a supported start. Those guests do not have an iLO, and they do not need one.

The console is one Ubuntu virtual machine:

```bash
curl -fsSL https://get.genestack.dev/console.sh | bash
```

The console VM is not the cluster. Add more guests for the cloud.

Boot those guests from a Talos ISO, then follow Talos is already installed. Or install Ubuntu on them and follow Ubuntu is already installed. Both start from a hostname and an IP.

Network boot from the console is optional in a lab, and it is easy to get wrong. The guests and the console have to be on a network where this console is the only DHCP server. Set each guest to boot from the network, and power it yourself from VMware. If the hypervisor is also answering DHCP, the guest will not boot from the console.

## Change the settings later

Settings, then Config, is one YAML document for the environment: the provider, the servers, the network, and which OpenStack services are on. The console checks it when you save.

- Save stores a new version. Older versions stay in the menu. Switch back to current before you edit.
- Render preview shows the files without writing them.
- Push writes the current version into `/etc/genestack`. Deploy does this and then runs the install.
- Deploy (dry-run) rehearses when you want a log without applying. The `dry_run` flag in `config.yaml` does the same for every job.

Back up `/opt/genestack-console/config.yaml` and the console database together. Passwords in the database are encrypted with the key in that file. A copy of the database without the file cannot be decrypted.

`https://my.genestack.dev` is an account page you can connect later. It is how the Apple apps reach this console. It does not run the console, and you do not need it to install a cluster.

## If something fails

Open Activity, then Jobs, and read the log of the job that failed. A second job that changes the same environment is refused while one is still queued or running.

Run from source when you are changing the console and testing that change. A production deploy host uses the binary from the install command. A checkout can run there. The install we support in production is that binary. The checkout steps are [Run from source](../README.md#run-from-source).

| You need | Read |
| --- | --- |
| Install the console on Linux, a Mac, or Windows | [Install](install.md) |
| How a server gets Talos or Ubuntu | [Genestack Console](genestack-guide.md) |
| The Hosts row and the management-port wall | [The console web page](ui.md) |
| Every job, including deploy and the wipe | [Jobs](jobs.md) |
| The screens | [The console web page](ui.md) |
