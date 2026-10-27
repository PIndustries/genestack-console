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
