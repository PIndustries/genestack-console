# Hardware setup and system acceptance

Hardware discovery is the first step of deployment, not proof of a working system.
The normal operator flow must use the native apps through the hosted portal;
backend API tests and direct diagnostic scripts are supporting checks only.

## Implemented in this branch

- Register a machine's BMC credentials, PXE MAC and reserved provisioning IP using
  normal scoped jobs. BMC credentials remain encrypted and secret job parameters.
- Discover CPU threads, RAM, physical drives and NICs without power, boot or disk
  changes. Reports include measurement time; failed refresh clears stale evidence.
- Propose roles using fresh measured capacity rather than machine names, with an
  odd control-plane quorum and initial system reservations. The proposal neither
  saves installation configuration nor authorizes any disk writes.
- Preserve remote deployer paths and export that environment's inventory path.
- Report unavailable health probes explicitly instead of failing with HTTP 500.

## Still required before app-driven deployment is accepted

1. Reconcile physical BMC disks with OS-visible devices, RAID logical volumes and
   stable WWIDs. Exclude attached remote volumes and read-only/virtual media.
   Device ordering, including `/dev/sda`, must not select the install disk alone.
2. Measure connected interfaces and verify site network inputs: PXE isolation,
   routing, gateway, DNS/NTP, external network, VLAN/MTU and address pools.
3. Review/save an installation configuration from those facts. Support advanced
   role overrides while making measured automatic allocation the default.
4. Pin deployment dependencies and prove an empty deployer can prepare its
   runtime, bootstrap services and provisioning environment without manual shell
   repair. An existing configured deployer does not prove this step.
5. Run the whole native Mac/iPhone/iPad journey: create environment, link deployer,
   enter site inputs, register/discover hardware, review plan, start/follow deploy,
   handle an injected failure and resume. Watch/TV must show live progress.
6. Independently verify fresh cluster identity, exact target identities, all nodes
   Ready and OpenStack identity/image/placement/compute/network/block storage.
7. From the apps create the VM network and LAMP VM; verify DHCP, DNS, egress,
   floating IP, HTTP 200, PHP, database roundtrip, persistent storage through a
   reboot and cleanup. Logs from mocked commands cannot satisfy these checks.

Use `scripts/check_redeployment_evidence.py` to check receipt completeness after
real UI drivers and independent probes produce evidence. It does not authenticate
receipts, provision machines or turn simulated results into deployment proof.
Keep private addresses, credentials and site-specific rebuild plans out of this
public repository. Obtain review of the exact target/data-disposition plan before
any destructive rebuild.

## Tested setup increments

A clean published deployer installation and native iPhone account/link/empty-cloud
creation have passed normal authentication and independent isolation checks.
Real deployer preparation passed, including a deliberate tunnel interruption and
recovery of the same job. Published 2026.10.27.1 updated the test deployer without
customer action; native registration and discovery then passed independent
physical and logical disk checks. This is software onboarding and read-only
hardware acceptance, not a completed cluster reinstall.

For a mixed-disk installation, `talos.install_disk_selectors` maps every configured
hostname to its exact OS-reconciled WWID. Wildcards, missing/extra machines,
duplicate identities and a conflicting global `install_disk` are rejected. The
shared template matches no disk; each per-node patch sets its exact selector and
installer image in `UnattendedInstallConfig`. This is separate from obtaining
fresh OS inventory and approving existing-data disposition.

The pinned Talos tool generated and validated control-plane and worker configs
offline. The initial control-plane config failed validation because a legacy
scheduling field conflicted with `KubeNodeConfig`; the replacement patch removes
only the control-plane scheduling taint. Fresh boot/installer and config versions
are aligned. Real hardware boot, permanent VM storage and full app-driven
OpenStack/LAMP acceptance remain required.

## Storage recovery evidence (2026-10-27)

A bounded disposable NFS-backed Cinder worker on the existing DFW cluster passed
real volume provisioning, guest disk writes, first-VM removal and exact-byte
reads from a new VM. A worker restart and an owned NFS share-manager restart each
preserved the data for another fresh VM. The test used Service DNS verified
against Longhorn ownership rather than assuming a service name or storing a
service IP. Its temporary resources were removed; the production Helm release
was unchanged.

The managed export preparation runtime also passed an isolated live test:
retained claim identity and guest-independent file bytes across retries, rejected
changed capacity, and stopped on foreign resources. A transient Kubernetes API
read failure in the first run led to bounded read retries; writes are not blindly
retried. These are component proofs on the existing cluster. They do not certify
fresh physical installation, automatic storage sizing, native deployment approval,
or the complete app-driven LAMP journey.

## Actual RAM-image boot evidence (2026-10-27)

Booting the previous Alpine image on a disposable blank machine exposed a
missing-base-package failure: DHCP and overlay retrieval worked, but the image
entered a recovery shell because `/sbin/init` was absent. This had not been
exercised by running the inspection script inside an installed Ubuntu VM.

The corrected payload uses one SHA256-pinned standard ISO, with its signed
package repository served over the deployer's PXE HTTP service. Its overlay
starts modloop and hardware discovery, and is deterministic across retries.
An actual BIOS DHCP/iPXE test retrieved the profile, kernel, initramfs and local
packages, reported both fixture disks, preserved all disk bytes and powered off.
Two further real network boots verified missing-target rejection (zero writes)
and selected-disk front/end metadata clearing, with the middle canary and the
unselected disk preserved. The full module image loaded before hardware probing.

These are real network-boot and disk-I/O component checks using explicitly owned
64 MiB virtual disks. They do not certify native app/BMC submission, UEFI boot,
Talos/OpenStack bootstrap or the final physical DFW deployment.

## First-use dedicated network test

A fresh secondary Ethernet port had no IPv4 address. The native picker initially
omitted it, leaving a first-time user needing shell configuration. A real iPhone
simulator regression reproduced that failure before implementation.

The native setup now offers unused ports and an explicit dedicated-network
confirmation. Its administrator worker job rechecks the port fingerprint, assigns
an unused private subnet using an exact-MAC systemd-networkd configuration, and
enables outbound masquerading. A successful simulator run configured the actual
owned appliance port and saved the provisioning settings. Independent checks
verified the normal account actor, real successful job, kernel forwarding and
unchanged management address/default route. This is a source-candidate result;
fresh packaged-image replay, node egress and OpenStack/LAMP tests remain required.

The action rejects ports with existing IP networks or non-link-local routes,
master membership and virtual link kinds. It never starts DHCP or reboots a
machine. A failed reconfiguration removes the new file and reconfigures only the
selected port. Existing configured networks remain available through the ordinary
settings-only save path.

## UEFI first-boot regression

The next actual native-triggered RAM boot received a DHCP address but its UEFI
firmware was offered the BIOS `undionly.kpxe` filename; no TFTP service supplied
that initial loader. Earlier tests entered iPXE directly and had not tested this
firmware handoff. Firmware architecture now selects the matching BIOS or x86-64
EFI loader. A separate dnsmasq process serves read-only files from the system
bootloader directory on the selected interface, with DNS and DHCP disabled.
Provisioning readiness includes this helper; missing/bound services fail before
power actions rather than leaving a machine waiting for an unavailable loader.

The repeated iPhone simulator test passed a real UEFI boot, TFTP loader retrieval,
HTTP RAM-image boot and accepted inventory report with both exact disk WWIDs.
The node's provisioning address came from its actual DHCP lease. Independent
byte/hole baselines verified both virtual disks were unchanged. A cancelled run
now revokes only its own unfinished inspection token so it can be retried safely.
The full cloud installer and workload acceptance are still separate steps.

## Reviewed cluster installation and continuation

A saved, OS-reconciled plan can explicitly authorize the first cluster install.
The native confirmation identifies every machine and exact boot/storage WWID.
The worker rechecks BMC identity, validates actual generated Talos configurations
with the installed Talos CLI, and compares the saved configuration version before
recording authorization. Only then can commissioning run. Existing clusters are
rejected by this first-install action.

Data disks use exact-WWID Talos user volumes and stable Longhorn disk paths.
Replication defaults are bounded by the number of measured storage machines.
The native app follows the real installation job and restores it after reopening.
This action stops after the cluster bootstrap; it does not claim that OpenStack
or customer workloads are ready.

The first native-triggered installation accepted commissioning and loaded Talos,
but exposed missing mandatory kernel security arguments and a kernel DHCP wait
before the NIC driver loaded. Correcting those arguments reached maintenance in
about 20 seconds on the owned virtual test machine. The subsequent real install
created the system partitions and the selected data volume, then exposed a
bootstrap timing race during the install reboot. The bootstrap command now waits
through that explicit temporary error and transport interruption, with a bounded
retry deadline and cancellation checks; identity errors remain fatal.

A failed authorized installation can continue through a separate operation.
It validates the same environment, stopped job, machines, addresses, roles and
exact disk selectors, rechecks management identity and compares configuration
versions again. It never invokes commissioning. Actual native continuation
preserved the original commissioning timestamp and report digest. The next
native continuation completed bootstrap and saved the cluster connection. An
independent authenticated Kubernetes read found the fresh expected node and its
measured CPU/memory; Talos reported the system partitions and chosen data volume
ready. The node correctly reports uninitialized CNI until infrastructure setup.
A cold-reopen regression then found that an existing Kubernetes connection hid
unfinished setup; the corrected native navigation retained setup and the completed
job without offering another erase or continuation. OpenStack/LAMP acceptance,
all-node readiness after CNI, and fresh packaged replay remain required.
