# Changelog

Each published build has a section here. The release workflow copies the matching section onto the GitHub Release. The version heading is the `VERSION` string, without the leading `v`.

The oldest GitHub Release is v2026.10.03. The source history starts two version numbers earlier. Those two sections are the commits. They are not downloadable tags.

## 2026.10.26.1-rc2

First-use and deployment-failure fixes verified during the private-cloud acceptance work.

- Empty environments that have never had a cluster connection require setup instead of raising a critical probe-failed alert. Existing clouds retain failure alerting when their configuration is removed.
- Failed parallel service installation cancels queued work without hanging; the pipeline returns the failed stage and stops before later stages.
- This candidate does not add automatic installation-plan application or perform a greenfield rebuild. Native first-use and workload acceptance continue separately.

## 2026.10.26.1-rc1

Readiness candidate for native cloud operations and measured hardware setup.

- Instance startup scripts/cloud-init are validated and delivered through config drives; older execution paths refuse unsupported delivery.
- Registered machines expose measured CPU, memory, disks and network inventory, with a read-only capacity proposal.
- Remote environment inventory paths, unavailable health responses and provisioning-agent startup retries are corrected.
- This candidate does not automatically apply an installation plan or authorize disk changes. Full greenfield and native workload acceptance remain in progress.

## 2026.10.17.1

The console page arrives as one script.

- Opening the console downloads the screens in one file.
- Opening that same build again uses the copy the browser kept.

## 2026.10.16.5

Control-plane chart hooks land on a node.

- Before each chart, control-plane nodes are labeled openstack-control-plane=enabled and the NoSchedule taint is removed.
- A hook pod that is waiting on that label or taint is placed, and the chart keeps installing.
- A pod that cannot schedule for another reason stops the chart.
- Keystone registration jobs run one after another. Each job needs the one before it.

## 2026.10.16.4

The board and Overview open from the last answer.

- Environments lists each cloud from the saved job. It does not dial the cluster to draw the rows. Health fills in after the rows are on the page.
- Overview and OpenStack show the last answer immediately, then replace it when a new read returns. A section that does not answer keeps the previous one.
- Helm still waits on each chart. That wait is the install, not this page.

## 2026.10.16.3

Opening the console again uses the scripts it already has.

- The scripts and styles for a build stay in the browser.
- The shell still checks for a newer build.

## 2026.10.16.2

The update note lists what changed.

- After an update, the note names the version and the changes in that build.
- The install instructions stay on the release page.

## 2026.10.16.1

Overview asks the cluster once.

- A heavy Overview refresh loads machines, the cloud, VMs, and the Kubernetes lists in one read. Inventory and job status stay on the shorter timer.
- A section that does not answer leaves the others on the page.

## 2026.10.15.3

The console keeps answering while a deploy is writing its log.

- A running job stores the log in pieces. A flush no longer rewrites the whole log, so other pages are not stuck behind that write. Opening a job still loads the full log.
- A deploy downloads Helm repository indexes once. Later charts in that same deploy reuse the index. Helm still waits for each release.
- The required tests check runs the suite in eight parts, so a hosted runner can finish.

## 2026.10.15.2

Saved machines show up before the dial finishes.

- The machine list and the fabric card paint the saved names and addresses, then fill Talos, Kubernetes, and OpenStack when that dial returns. A machine that is still being checked is not marked down.
- Pods, services, gateways, routes, ingresses, address pools, and the VM list are remembered for 15 seconds. One refresh dials at a time.
- A second cloud inventory request waits for the dial already in flight.

## 2026.10.15.1

The page stays responsive when a machine or the cluster is slow.

- The job list sends the start and end of a long log. Opening a job still loads the full log.
- Machine reachability is remembered for 15 seconds, and only one refresh dials the machines. A Talos version check gives up after 3 seconds.
- An idle Overview asks the cluster once a minute. Inventory and job status keep the shorter refresh.

## 2026.10.14.2

Horizon paints inside the console window.

- The dashboard stays visible in the Horizon window. The same page still opens in a new tab.

## 2026.10.14.1

Horizon stays inside the console session.

- The dashboard's WEBROOT and static files are rewritten onto that session, so the header, fonts, and templates load with Horizon.

## 2026.10.13.10

An API restart no longer drops a running deploy off the cluster.

- Startup leaves a staged kubeconfig alone while it is younger than the max job timeout.
- An install script whose kubeconfig file is gone stages a new one before it runs.

## 2026.10.13.9

The overview map fills the window.

- The map uses the leftover viewport and pans inside it.
- A running deploy loads its job log. A deploy past infrastructure is no longer drawn as a metal rebuild.
- A Ready Kubernetes node stays Ready while work is running on it.
- The access log redacts a ticket or token in the path and still records the client, method, and status.

## 2026.10.13.8

Open Horizon from inside the console.

- The OpenStack page opens the dashboard here. The console signs in with the cloud admin account. The password stays on the console.
- Kubernetes has the same Open Horizon button. A public horizon address is still listed when the cluster has one.

## 2026.10.13.7

The environment descriptor stays on Settings.

- Expert is part of the Settings tab. Opening it and leaving no longer paints the descriptor, components, and Helm overrides on every other tab.
- Clicking a Talos machine opens its health, logs, and config.

## 2026.10.13.6

The Machines page shows a resumed deploy on the stage that is running.

- A deploy that starts at platform-extras no longer paints infrastructure as running and the earlier stages as not started.
- A storage node recorded as storage-ceph or storage-cinder counts toward storage.

## 2026.10.13.5

A deploy uses mikefarah yq, so Neutron can read the OVN TLS flag.

- Install scripts call `yq eval`. A Python yq earlier on PATH accepts `--version` and then treats the expression as a filename. Neutron stopped with an empty TLS flag. The deploy now runs mikefarah yq.

## 2026.10.13.4

Horizon's startup hook exits before it rewrites Apache.

- The hook's config edit and Apache reload were killing the dashboard. The hook now exits immediately. This image serves the dashboard without that edit.

## 2026.10.13.3

A compiled console can deploy. The admin client pod is created next to its secret.

- The Helm guard no longer asks the compiled binary for Python source, so a deploy gets past the first step.
- The admin client is applied in the openstack namespace, where the Keystone admin secret already is.
- Horizon's startup hook no longer reloads Apache. That reload was stopping the dashboard.

## 2026.10.13.2

The Machines reach column checks a Talos node on its API.

- Talos has no SSH daemon. An open API stays marked Talos.
- A closed API says the API refused. Ubuntu stays on SSH.

## 2026.10.13.1

A Talos deploy stops when a pod cannot schedule, instead of waiting two hours.

- Control planes stay schedulable. Genestack places its control services on those nodes, and the machine config keeps the NoSchedule taint off.
- A cluster that already has a machine config is patched in place before the next chart. That patch does not resend the etcd subnet block.
- Helm stops after a pod has been unschedulable for about a minute. Gateway API CRDs are applied before cert-manager.

## 2026.10.12.4

The Longhorn prepare step runs the installed console binary.

- A restart removes the temporary Python path a compiled console reports as its interpreter.
- The infrastructure script calls that installed binary, which is still there after the restart.

## 2026.10.12.3

An empty provider port stays empty when the infrastructure script starts.

- The script no longer fills that port from bond0.126 on the deploy host or from the compute NIC.
- A saved port replaces an existing bridge annotation. An empty port removes the stale annotation instead of stopping the deploy.

## 2026.10.12.2

A deploy names the missing fact that would stop the infrastructure script, before that script starts.

- An empty gateway domain, a missing container or compute interface, a partial provider VLAN, a missing node role, or a Talos image without iSCSI tools stops a live job before the pipeline. A rehearsal still runs.
- When the inventory already names those roles, the deploy labels the nodes before the script checks them.
- An empty ACME email skips the certificate prompt. A saved name such as cluster.local is reported as a certificate that will stay pending.
- A provider port that is empty, or an ethN name on Talos, is reported as a bridge with no physical port. Deploy still runs.
- Install from the management port does not wipe disks when one of those facts would stop the deploy that follows.

## 2026.10.12.1

A Talos deploy installs the iSCSI tools Longhorn checks for, and kube-ovn follows the NIC that owns the node address.

- Writing the factory image into the machine config does not install those tools. The deploy upgrades a control plane or storage node that is missing ext-iscsid, one node at a time, and skips a node that already has it.
- An ethN provider port is not plugged into br-ex. A bond or PCI name is still pinned.
- A tunnel-interface annotation is removed only after kube-ovn's iface is empty, and the CNI then restarts.

## 2026.10.11.10

Kube-OVN no longer requires the NIC to be named eth0.

- A Talos factory image renames eth0. Pinning that name makes the CNI exit on the upgraded node.
- An ethN container interface is left unset, so each node uses the NIC that owns its address. A bond or PCI name is still pinned.
- A Talos image upgrade waits up to five minutes for the installer to accept the request.

## 2026.10.11.9

Envoy listeners are added one at a time.

- A local infrastructure deploy uses the envoy path that replaces the chart's port-80 listener instead of adding a second one with the same hostname.
- The gateway no longer rejects the listener list for a duplicate port, protocol, and hostname.

## 2026.10.11.8

An infrastructure deploy's kustomize overlay no longer requires a patch file that was never written.

- A service overlay without patches matches bootstrap.sh.
- An overlay that already points at a missing patches.yaml drops that entry before helm runs.

## 2026.10.11.7

A deploy keeps its kubeconfig, and infrastructure reads this environment's config directory.

- Each job stages its own kubeconfig. A cluster check no longer removes the file a deploy is using.
- A local live infrastructure run follows this environment's config directory. Chart versions, manifests, and kustomize bases are placed there when bootstrap did not run.
- Kube-OVN uses the container interface saved on the environment.

## 2026.10.11.6

A Talos maintenance apply rewrites the machine config first.

- Nodes that are still waiting for a config get a fresh machine file before apply. A file left by a failed run is not reused.
- A node that already has this environment's config keeps that identity.

## 2026.10.11.5

A Talos retry rewrites the machine config from the saved identity.

- When secrets already exist, bootstrap regenerates the machine config from those secrets. A failed apply does not reuse the old file.
- The cluster identity stays. The retry does not mint a new one.

## 2026.10.11.4

Talos writes the management gateway on the NIC it pins.

- network.gateway and network.cidr are the untagged management network. A saved private MAC gets that address and the default route.
- A vRack VLAN does not take that default route.

## 2026.10.11.3

Talos bootstrap uses VLAN 100 only on an OVH vRack, and writes the Talos 1.14 documents.

- VLAN 100 and 10.10.0.0/24 apply when a vRack is selected. A saved copy of those values does not tag a flat network. Guided setup no longer writes them onto every environment.
- A vRack still needs a private MAC or an interface name on each machine. The console does not pick one of several NICs.
- Hostname, kubelet node IP, and kube-proxy node ports are written as Talos 1.14 documents.

## 2026.10.11.2

Hosts can start a lab on the console computer.

- Create lab starts three Ubuntu guests. Four adds a worker. Five adds a second compute. Each guest is 4 vCPU, 16 GiB, and a thin 80 GB disk. The three are control, compute and network, and storage. The console host stays outside the cluster. This is not a production workload.
- The environment stays rehearsed until you Apply. SSH is ubuntu@127.0.0.1 from port 2222. Confirm starts the guests. Without confirm, the job writes the plan. A guest that is already up is left alone.
- If qemu is not installed, the job installs qemu-system-x86 and qemu-utils.

## 2026.10.11.1

A release now has to pass the build matrix before it compiles, and a deploy says where the time went.

- The nine intakes are outside ISO, this console's PXE, virtual CD, PXE with no management port, user-installed Ubuntu, existing Talos, existing Kubernetes, Talos then Ubuntu then Kubespray, and Look around. The report marks the hardware as not run.
- Virtual CD insert posts the generic Redfish InsertMedia action. iLO and iDRAC use that same path. An outside ISO URL is the image that gets mounted. The test does not call a BMC.
- Deploy over SSH passes parallelism 2. Keystone finishes first. Placement and Glance then run together. The other stages stay one at a time. The log ends with a scoreboard of seconds, waited items, and items that ran beside another.

## 2026.10.10.3

The machines you added can be drawn as a rack, and the next action says how to install.

- With nothing typed, every machine is 1U on Rack 1, stacked from U1. A machine past U48 stays beside that rack. Flat is the elevation. Space is the same cabinet. Cloud puts that rack beside Metal, Kubernetes, and Instances.
- A solid line is a fact this console already has. A dashed line is a connection you typed. The console does not invent a switch or a PDU cable. Add a switch, a PDU, or a layer when you want one, with a height in U.
- Boot the Talos ISO yourself. That button does not boot it. Ubuntu is recorded on each machine, then Deploy over SSH. A machine that is not in the cloud yet still needs an operating system, then the compute stage.
- Install from the management port is offered only before the cloud is up. Confirming names the hosts, replaces the running install, and wipes the disks.

## 2026.10.10.2

Overview and Machines open on the same next action. The sentence changes as the cloud grows.

- No machines yet: add a hostname and an address. Adding a machine does not boot it and does not install OpenStack.
- Talos that is already running is one step. A drifted machine still waits until you name it.
- OpenStack continues from the stage that is left: infrastructure, operators, kube-ovn, core, or compute-network. A disk wipe stays a separate action.
- After those stages succeed, the next action is add capacity, change a service, or check the cloud.
- While the environment only logs, the strip says the run writes a log and does not change the machines. Guided setup opens the environment on this strip.

## 2026.10.10.1

A drifted Talos machine waits until you name it. Deploy from infrastructure stays closed until this console can read a kubeconfig and a control plane is Ready.

- Adding a machine that takes too long checks whether it was saved. Reach ends as Up, Talos, or Down. One slow health check says the console is slow.
- The saved Talos identity is one status. Adopt pastes the running cluster's talosconfig into the vault. Replace names one machine. Confirming reboots that machine into the installer and removes the running install.
- OpenStack stages are on the environment: infrastructure, operators, kube-ovn, core, and compute-network. Continue from here starts at that stage.
- Before bootstrap, the page names the Talos client pin, the boot image, and whether the image includes iscsi-tools and util-linux-tools.
- A management port can reboot the named machine after that confirm. It does not reboot every drifted machine.

## 2026.10.09.2

An admin can view, edit, and delete a record in an environment's vault.

- Open a record to see its value. Close clears that value from the page. The list still shows names only.
- Edit replaces ssh, a machine's management password, kubeconfig, talosconfig, or a note. A new note can be added.
- Delete removes that record. Deleting ssh clears the key pair. The SSH Keys card can generate a new one. Deleting a management password leaves the machine. Deleting kubeconfig or talosconfig leaves the file on this console, and the next download can file it again.

## 2026.10.09.1

Each environment has a vault for its client certificates, and a drifted Talos machine can be returned to the installer.

- Open an environment and choose Vault. The list is the names stored for that environment. Values stay in the console.
- Kubeconfig and Talosconfig download the copy in that vault. The first download saves the copy this console already has. Regenerate asks the cluster for a new client certificate, valid for one year, and replaces the vault copy.
- A Talos machine whose certificate does not match the saved config is rebooted into the installer when this console has its management port. Otherwise the job names the one step left and waits for you to confirm that step.
- SSH user, password, and key are shown only for Ubuntu. Worker and network stay checkboxes on Custom.
- The disk image is attached to the release as soon as it is built. The CD image follows.

## 2026.10.08.1

A machine's roles are separate functions.

- A worker is a Kubernetes node for workloads such as monitoring and gateways. Compute, network, and storage are their own roles. One machine can still have more than one role.

## 2026.10.07.21

The installer CD image can format its EFI boot image.

- The disk image built. The CD image stopped because Image Builder runs mkfs.fat to create the EFI boot image, and that program was not in the installer image. The image also links the CD builder name Image Builder runs.

## 2026.10.07.20

The installer CD image includes skopeo.

- The disk image built. The CD image stopped because Image Builder runs skopeo to put the appliance image on the CD, and that program was not in the installer image.

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
