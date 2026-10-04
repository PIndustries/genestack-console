# Hosts

This is the source on main. It is not in the v2026.10.03 binary.

The console stays on a dedicated Linux server outside the cluster. That server is the deploy host. It does not join the cluster.

Talos is the preferred direct boot. Kubespray adopts machines that already have an operating system. Ubuntu is the other operating system the console installs. A machine can stay a plain Ubuntu server and stay out of the cluster.

A management port is optional. Talos is already installed on the Hosts page runs `genestack.talos.bootstrap`. The machines must already be waiting, which is what a Talos ISO does, including a VMware guest. The job uses every saved address. It does not power them. An admin starts it. Already have an OS records the selected hosts that already run Ubuntu. The record is `adopt: kubespray` on the server. It does not install or reboot. Adopting a cluster that is already up stays Settings, Access, Hosts, Adopt Kubespray.

Talos and Ubuntu are the two operating systems the console installs onto a machine. Commission is the wipe Talos requires before it will boot. Disk leaves the machine on the system that is already installed. Install Ubuntu is on the bare-metal row. It sets that machine's next boot to `ubuntu`, writes only that machine's autoinstall seed, and can power-cycle into it. The seed is `data/pxe/ubuntu/<hostname>/`. The iPXE script for that MAC points at that directory, so the next machine does not replace this one's user-data. The login user is `ubuntu`. The key is the environment SSH public key the console already keeps. The layout uses the whole disk. It does not install OpenStack or Kubernetes, and it does not require the Talos wipe. The node name has to be a hostname, and the node needs a PXE MAC. Place the Ubuntu live-server kernel and initrd at `data/pxe/ubuntu/vmlinuz` and `data/pxe/ubuntu/initrd`. `hosts.ubuntu.prepare` writes the same per-host seed when you pass a hostname and a key by hand. A shared `data/pxe/ubuntu/user-data` remains only for a boot script that has no hostname.

`hosts.ubuntu.bringup` is the job behind Install and Reinstall Ubuntu on the Hosts page. You name the hosts. One that already answers on SSH stays on disk and drops its saved cluster roles. One that does not answer is network-booted into Ubuntu. The job waits until SSH answers, then sets the next boot back to disk. One that is still Talos is left alone unless it is named in `leave_hostnames`. A dry run does not power the machine. This job ships in `v2026.10.04.1`. It is not in the `v2026.10.03` binary.

L2 means the deploy host and the machines are on one local network, so the console can answer DHCP and serve the boot file. That is a recommendation. If the deploy host cannot be L2 with the machines, the agent at the site serves DHCP and the boot file. You still manage the environment from the console.

iLO, iDRAC, and Redfish stay the path when the server has a management port. Those are the existing bare-metal operations. A desktop or a normal server gets Ubuntu, then one agent command. After Ubuntu is up, run `agent.install`, or the one-liner the console already serves, once on each machine. The agent is how that machine runs work for the console.

MicroK8s is a Kubernetes snap you can add on Ubuntu later. It is not an operating system the console boots. `hosts.microk8s.install` SSHs to the host and runs `sudo snap install microk8s --classic`, then `sudo microk8s status --wait-ready`, only when the job is not a dry run. A dry run returns those commands and does not connect. The job system defaults to a dry run. A lab of Ubuntu machines plus the agent is enough to later add MicroK8s so those machines join.

Kubespray is an Ansible project that builds a Kubernetes cluster. `hosts.kubespray.adopt` records a cluster that already exists. If you pass kubeconfig text and the job is not a dry run, the text is Fernet-encrypted onto the environment. A dry run does not write it. After that, the cluster is managed with the SSH and kubectl path the console already has. The operation does not clone the Kubespray repository and does not run Ansible. The console does not query a Kubernetes API that is down.
