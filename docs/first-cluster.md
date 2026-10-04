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

Talos is the operating system to use when the console is installing machines itself. Talos boots a machine straight into Kubernetes. The console answers DHCP and serves the boot file, wipes the disks, boots Talos, then runs the Genestack scripts. DHCP is how a machine asks for an address. The boot file is the small program the network card downloads when the machine starts from the network instead of from its disk.

That path needs three things.

- The deploy host is on the same local network as the machines. That is L2. L2 is recommended. It is how the console hands out addresses and the boot file itself. When the deploy host cannot be on that network, an agent on a computer that is does that job, and you still start the work from the console.
- A management port on each server. Vendors call it the BMC, iLO, or iDRAC. The console uses it to power the server and to ask for one network boot.
- The Genestack checkout at `/opt/genestack`.

In Guided setup:

1. Basics names the environment.
2. Connect stays on this console unless a site is somewhere this machine cannot reach.
3. Deployment stays on Talos. The cluster name is a short DNS name. The install disk is the device Talos writes, often `/dev/sda`. Confirm the name on the machine. Leave the image blank unless you have your own.
4. Servers is where the machines come from. BMC / Redfish is a management port you type in. PXE is a machine the console already saw asking for an address. Static IPs / SSH is a machine that already has an address you can reach.

Then, on the environment:

1. Platform, then Hosts, is the server list. Add each machine. Record the management port and the MAC address of the port on the install network. A MAC address is the hardware address of that network card.
2. The path above the table stays Talos. Talos is the preferred direct boot. Choosing the path does not install an operating system by itself.
3. Settings, then Config. Change what you need. Save. Each save is a version.
4. Set `dry_run: false` and restart the two units only when you are ready for the wipe.
5. Deploy cluster asks you to confirm the wipe. It pushes the settings, network-boots Talos, then runs the Genestack pipeline: Kubernetes, then OpenStack. A server you did not select stays on its own disk. The log is Activity, then Jobs.

Ubuntu is the other operating system the console installs. On a Hosts row, Install Ubuntu puts Ubuntu on that one machine and waits until it answers. It does not install Kubernetes or OpenStack. A machine may stay a plain Ubuntu server.

Kubespray is the other way onto Kubernetes. It adopts machines that already have an operating system. Guided setup hides it under Advanced: Kubespray (Ansible). Those machines need SSH from the deploy host. The Genestack scripts then install Kubernetes and OpenStack. This does not wipe disks the way the Talos path does. It does change the machines you named.

## A lab of virtual machines

Use this when you have no physical servers. VMware is fine.

The console is one Ubuntu virtual machine and the install command:

```bash
curl -fsSL https://get.genestack.dev/console.sh | bash
```

Give it more virtual machines if you want a cloud to install. A normal guest has no management port, so the console cannot power it.

The straightforward lab is Ubuntu guests you install yourself. Put them on a network the console can SSH to. In Guided setup choose Kubespray, and on Servers choose Static IPs / SSH. Enter each guest. Save the config. Turn dry run off when you want Deploy to run. Deploy uses SSH. It does not PXE those guests.

Talos from the network is the other lab, and it is easier to get wrong. The guests and the console have to be on a network where this console is the only DHCP server. Set each guest to boot from the network, and power it yourself from VMware. If the hypervisor is also answering DHCP, the guest will not boot from the console. Do not put the console's DHCP on a network other machines depend on.

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

Run from source only when you are changing the console. Installing a cloud uses the binary from the install command. That checkout is [Run from source](../README.md#run-from-source).

| You need | Read |
| --- | --- |
| Install the console on Linux, a Mac, or Windows | [Install](install.md) |
| How a server gets Talos or Ubuntu | [Genestack Console](genestack-guide.md) |
| The Hosts row and the management-port wall | [The console web page](ui.md) |
| Every job, including deploy and the wipe | [Jobs](jobs.md) |
| The screens | [The console web page](ui.md) |
