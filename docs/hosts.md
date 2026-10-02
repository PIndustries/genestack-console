# Hosts

This is the source on main. It is not in the v2026.10.03 binary.

The console stays on a dedicated Linux server outside the cluster. That server is the deploy host. It does not join the cluster.

People deploy Kubernetes in more than one way. Talos stays a choice. The other paths are a plain Ubuntu machine, MicroK8s, and Kubespray.

Autoinstall is Ubuntu's unattended install. `hosts.ubuntu.prepare` writes cloud-init user-data and meta-data for one hostname under the PXE data directory the HTTP server already serves: `data/pxe/ubuntu/<hostname>/`. The same files are copied to `data/pxe/ubuntu/user-data` and `data/pxe/ubuntu/meta-data`, which is the seed the Ubuntu iPXE script reads. The seed creates an identity user named ubuntu, installs the SSH public key you pass, and uses the direct disk layout. That layout uses the whole disk. It does not install OpenStack or Kubernetes. The job does not open an SSH session. It does not start a second DHCP or TFTP server. Place the Ubuntu live-server kernel and initrd at `data/pxe/ubuntu/vmlinuz` and `data/pxe/ubuntu/initrd` on that same tree. Set the machine's next boot to `ubuntu` with `baremetal.node.next_boot` when you want the existing PXE path to serve that script. `boot_now` then uses the same one-shot PXE boot the other profiles use.

L2 means the deploy host and the machines are on one local network, so the console can answer DHCP and serve the boot file. That is a recommendation. If the deploy host cannot be L2 with the machines, the agent at the site serves DHCP and the boot file. You still manage the environment from the console.

iLO, iDRAC, and Redfish stay the path when the server has a management port. Those are the existing bare-metal operations. A desktop or a normal server gets Ubuntu, then one agent command. After Ubuntu is up, run `agent.install`, or the one-liner the console already serves, once on each machine. The agent is how that machine runs work for the console.

MicroK8s is a Kubernetes distribution installed from a snap on Ubuntu. `hosts.microk8s.install` SSHs to the host and runs `sudo snap install microk8s --classic`, then `sudo microk8s status --wait-ready`, only when the job is not a dry run. A dry run returns those commands and does not connect. The job system defaults to a dry run. A lab of Ubuntu machines plus the agent is enough to later add MicroK8s so those machines join.

Kubespray is an Ansible project that builds a Kubernetes cluster. `hosts.kubespray.adopt` records a cluster that already exists. If you pass kubeconfig text and the job is not a dry run, the text is Fernet-encrypted onto the environment. A dry run does not write it. After that, the cluster is managed with the SSH and kubectl path the console already has. The operation does not clone the Kubespray repository and does not run Ansible. The console does not query a Kubernetes API that is down.
