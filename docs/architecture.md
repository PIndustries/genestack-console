# Genestack Console Architecture

The [README](../README.md) is the introduction. This page is the map of what runs after you have installed the console.

Genestack is the checkout that installs OpenStack on Kubernetes. The cluster is that cloud. The console is the program that drives the checkout. Both live on one dedicated Linux server, the deploy host. The deploy host sits just outside the cluster and never joins it. It is not a Kubernetes node and not an OpenStack compute node. If the cluster stops answering, you can still power the servers and run the install from this machine.

We recommend the deploy host be L2 with the servers you are installing. L2 means they are on one local network, so the console can give a server an IP address and a boot file itself. When the deploy host cannot be L2 with a site, an agent on a computer that is does that job and connects out. You still run the job from the console. One console often looks after several sites this way.

| Path | What it is |
| --- | --- |
| `/opt/genestack-console` | This program. The UI, the API, a SQLite database, and a worker process. |
| `/opt/genestack` | The Genestack checkout. The worker runs scripts from here. |
| `/etc/genestack` | Inventory and overrides. A job renders the saved config into this tree before the install scripts run. |

An environment is one cloud. Its settings are one versioned document in the console database. A job writes the rendered files to `/etc/genestack` and then runs the Genestack pipeline. Users, the job log, and the management-port passwords stay in that database on the deploy host.

Skyline is the OpenStack dashboard people use once the cloud answers. The console is how the operator builds the cloud.

`https://my.genestack.dev` is an optional account page. The Apple apps sign in there and are forwarded to your console. The environment and the management-port passwords stay on the deploy host. See [hosted-mode.md](hosted-mode.md).

```
your browser
    |
    |  http://127.0.0.1:8080 on the deploy host, or an SSH tunnel
    v
deploy host                    dedicated server, outside the cluster
    /opt/genestack-console     UI, API, database, worker
    /opt/genestack             Genestack scripts
    /etc/genestack             inventory and overrides
    |
    |  L2 recommended: this host hands out addresses and boot files
    |  remote site: an agent there does that, managed from here
    v
physical servers               these machines become the cluster
    management port            console powers the machine (BMC / iLO / iDRAC)
    server port                console answers DHCP and serves the boot file
```

DHCP is how a server asks for an IP address. The boot file is what the server downloads when its network card is told to start from the network. Both are served by the console process (`app/services/pxe_runtime.py`). The files it serves are rendered by `app/services/pxe.py` under the console data directory.

When the deploy host cannot be L2 with the servers, install the agent on a computer that is. DHCP and the boot files for that site run on the agent. The agent connects out to the console, and you still run the job from the console.

## Boot order

Each server has its own next boot. The MAC address is the id of the network port DHCP is watching.

- **disk** is the default, including a MAC the console has never been asked to install. The server boots its local disk.
- **commission** is a small system that runs from memory. It wipes the starts of the fixed disks and posts one report to the console.
- **talos** is served for that MAC only after the report is accepted. Talos is the operating system Kubernetes runs on for this install. The machine config is pushed after the wipe. It is not placed on the kernel command line.

An ISO boot is rejected for this path because it does not wipe the disks.

## Jobs

A job is one operation on one environment. Pushing config, deploying, sending a boot file, and powering a server are all jobs. You start a job from the UI or the API. The worker process runs it, so a long install is not stuck inside the web request. Two jobs that change the same environment do not run at the same time.

Each operation is a Python file under `app/modules/`. The folder is one area. The folder's `__init__.py` lists those files, in order. The runner in `app/services/job_runner.py` sets up the environment and calls the function for that operation. A module you add uses the same shape. Put the folder on `modules.paths` in `config.yaml`. The layout is in [Modules](modules.md). The long deploy job is `app/services/deploy.py`: push the config document, then run the Genestack pipeline.

OVH and the Apple native API are extra ways to reach the same job system. They are described further down. Booting a server uses the bare-metal path above.

## Layers

| Layer | Responsibility |
|-------|----------------|
| **Console API** | Auth (API keys + user sessions), RBAC, tenants/memberships, env registry, job queue/runner, audit trail, operation catalog, env config documents |
| **Aggregation services** | Read-only rollups over env state: descriptor (`app/services/descriptor.py`), guided workflow (`app/services/workflow.py` — six lifecycle steps), fleet board (`app/services/fleet.py` — compact per-env step states, no synchronous probes) |
| **Telemetry** | Worker-side collector writing per-env cluster snapshots, in-process event bus + SSE stream, alert rules/events, optional metric samples — see [Telemetry & real-time subsystem](#telemetry--real-time-subsystem) |
| **Agent channel** | A machine on a remote site opens a WebSocket out to the console (`app/services/agents.py`, `agent/`): HMAC enrollment handshake, heartbeats, allowlisted commands — see [Agent channel](#agent-channel-hub-and-spoke) |
| **Bare metal** | Per-MAC boot order (disk, commission, talos), Redfish client for the management port (`app/services/redfish.py`), node registry (`app/services/baremetal.py`), DHCP and boot files in-process (`app/services/pxe_runtime.py`, files rendered by `app/services/pxe.py`) |
| **Ansible** | Run allow-listed playbooks (`host_preflight.yml`, `basic_ops.yml`, …) |
| **Genestack bridge** | Read `openstack-components.yaml`, list `bin/` scripts, enable services via allowlist, run pipeline stages; inventory + curated run of repo utility scripts (`app/services/repo_scripts.py`) |

## Multi-environment model

An **environment** is a named ops context (lab, staging, production-rack-a).
Jobs always run in the scope of an environment so credentials and inventory
stay isolated.

```
Environment
  ├── id, name, description, labels
  ├── tenant_id              (owning tenant; existing envs backfilled to "default")
  ├── inventory and deploy-host overrides
  ├── genestack_config_dir   (per-env /etc/genestack path)
  ├── kubeconfig_data        (kubeconfig blob, encrypted at rest)
  ├── dry_run                (per-env override; null = inherit global)
  └── Jobs[]  (operation + params + status + result)
```

Typical flow:

1. Admin creates an environment (`POST /api/v1/environments`).
2. Operator submits a job against that environment (`POST /api/v1/jobs`).
3. Runner executes the operation (sync in-process or worker); result is stored.
4. Audit log records actor, operation, env, outcome.

## Multi-tenancy and auth

Two credential kinds coexist:

- **Static API keys** (`auth.api_keys` in config.yaml, `X-API-Key` header) —
  platform-admin break-glass credentials that bypass tenant scoping.
- **User accounts + sessions** — `POST /api/v1/auth/login` exchanges
  username/password for a `SessionToken` (Bearer); passwords are hashed with
  PBKDF2-SHA256 (600k iterations, stdlib only); tokens expire after
  `auth.session_ttl_hours` (default 12). `POST …/logout` invalidates;
  `GET …/whoami` reports identity, `platform_admin`, and tenant memberships.

```
Tenant ──< Membership (role: viewer|operator|admin) >── User
  │
  └── Environment[]   (every env belongs to exactly one tenant)
```

All env-scoped routes resolve the environment through
`get_env_scoped(minimum_role)`: environment lists are filtered by membership
and cross-tenant access returns 403. Tenant CRUD and membership management
live under `/api/v1/tenants*`; user CRUD under `/api/v1/users` is platform
admin only. Bootstrap path: `python -m app.cli create-tenant`,
`create-user [--platform-admin]`, `add-member`.

## Per-environment execution

Each job resolves an **EnvContext** from its environment before running any
subprocess. The context controls which config tree, credentials, and safety
mode the job sees:

- **Subprocess env vars** — every job gets `GENESTACK_BASE_DIR` (resolved
  Genestack root). When `genestack_config_dir` is set, jobs additionally get
  `GENESTACK_CONFIG` and `GENESTACK_OVERRIDES_DIR` pointing at it, plus
  `ANSIBLE_INVENTORY` when the dir contains an `inventory/` subdirectory.
  `KUBECONFIG` points at the env's kubeconfig (staged from the encrypted
  `kubeconfig_data` blob to a 0600 file for the job's duration, else
  `kubeconfig_path`, else kubectl's default).
- **Per-env dry_run** — `env.dry_run` overrides the global `dry_run` from
  config.yaml when set; `null` inherits.
- **Components scoping** — `PUT /api/v1/genestack/components` writes
  `<genestack_config_dir>/openstack-components.yaml` when set, else the
  repo-root global copy; responses and audit entries carry
  `scope: environment|global`.
- **Mutating-job lock** — mutating operations (everything with
  `mutating: true` in the catalog: deploy, config push, host prepare, verify,
  the day-2 ops, MAAS writes, service enable, pipeline/ansible runs, …) are
  serialized per environment: a second submission while one is queued/running
  fails with HTTP 409 and the conflicting job id; the worker additionally
  skips a queued mutating job whose env already has a *running* one and
  retries it on a later pass.
- **Secret encryption** — `kubeconfig_data` and the MAAS api key are stored
  Fernet-encrypted (`fernet:` prefix, key derived from `secret_key`);
  legacy plaintext values still decrypt. Rotating `secret_key` requires
  re-encrypting stored secrets; startup warns while it is the default.
- **SSH executor** — when the env sets `deployer_ssh_host` (plus optional
  `deployer_ssh_user`), Genestack bridge commands (install scripts,
  playbooks, pipeline stages) run on that deploy host via
  `ssh -o BatchMode=yes -o ConnectTimeout=10 [user@]host '<remote_env> cd <cwd> && <cmd>'`.
  Only the Genestack-scoped variables (`remote_env()`) cross the wire —
  staged kubeconfig blobs stay local (host-path kubeconfigs only) — and
  kubectl/helm cluster probes always run locally. Dry-run logs show the
  wrapped command.
- **Descriptor** — `GET /api/v1/environments/{id}/descriptor` (viewer+;
  `?format=yaml` for YAML) is a read-only snapshot of one environment:
  identity, provider, inventory topology (groups/hosts/group_vars, with a
  `drift` flag when the console-rendered inventory disagrees with the on-disk
  inventory), components + chart versions (scope `environment|global`), helm
  overrides, kustomize overlays, gateway files, and live cluster/services
  reachability.

## Environment config document

Each environment keeps **one flat, versioned YAML document** in the console
(`EnvConfigVersion` rows; the doc is the source of truth). Top-level sections: `provider`, `deploy`, `maas`, `servers`,
`network`, `components`, `chart_versions`, `helm_overrides`,
`kustomize_patches`, `group_vars`, `secrets`, `storage`, `talos`, `pxe`
(unknown keys warn, not reject).

| Endpoint | Role | Purpose |
|----------|------|---------|
| `GET/PUT /api/v1/environments/{id}/config` | viewer / operator | Read current doc; store a new version |
| `GET …/config/versions[/{n}]` | viewer | Version history / fetch one version |
| `GET …/config/render` | viewer | Preview rendered config-dir-relative files |
| `GET …/servers` | viewer | MAAS machines merged with doc role assignments |
| `POST …/servers/assign` | operator | Upsert a role assignment (stored as a new version) |

Rendering maps only the sections present in the doc onto the env's
`/etc/genestack` tree — absent sections never wipe existing files. Console
output uses `console-rendered.yaml` filenames to stay separate from
hand-maintained files:

| Doc section | Rendered file(s) |
|-------------|------------------|
| `provider` | `provider` |
| `servers` | `inventory/inventory.yaml` (roles → groups: control→controllers, compute→compute, network→network, storage→storage; sub-roles `storage-ceph`→`ceph_storage_nodes` and `storage-cinder`→`cinder_storage_nodes`, both also joining `kube_node`) |
| `components` | `openstack-components.yaml` |
| `chart_versions` | `helm-chart-versions.yaml` |
| `helm_overrides` | `helm-configs/<svc>/console-rendered.yaml` (`global` key → `helm-configs/global_overrides/`) |
| `kustomize_patches` | `kustomize/<svc>/overlay/patches.yaml` |
| `network` | `gateway-api/console-rendered.yaml` |
| `group_vars` | `inventory/group_vars/<group>/console-rendered.yml` (`.yml`, matching genestack's own `ansible/inventory/genestack/group_vars` tree) |
| `secrets` | `kubesecrets.yaml` (multi-doc `v1/Secret` manifests in `bin/create-secrets.sh` shape) |
| `storage` | `cinder_backend_name` / `cinder_worker_name` merge into the `cinder_storage_nodes` group vars; `ceph:` keys are recorded for future rook handling (no files today) |
| `talos` | No rendered files — `cluster_name` / `install_disk` / `image_url` are consumed by the provider=talos bootstrap flow (and as the default image URL for MAAS/PXE asset fetches) |
| `pxe` | Not pushed to the config dir — rendered by `app/services/pxe.py` into `<data_dir>/pxe/` (`dnsmasq.conf`, `boot.ipxe`, talos assets) and served in-process |

Two further sections carry extra semantics:

- **`network`** also maps onto the env vars genestack's setup scripts read:
  `gateway_domain`→`GATEWAY_DOMAIN`, `acme_email`→`ACME_EMAIL`,
  `hyperconverged`→`HYPERCONVERGED`, `container_interface`→`CONTAINER_INTERFACE`,
  `compute_interface`→`COMPUTE_INTERFACE`, and an `ovn:` sub-mapping whose
  snake keys upper-case to `OVN_<KEY>` (list values comma-joined, e.g.
  `OVN_VLANS`). Job runners merge these into the subprocess environment.
- **`secrets`** values are **encrypted at rest** (Fernet, `fernet:` prefix)
  and **masked as `***` in every API response** — the document, render
  previews, and rendered `kubesecrets.yaml` alike; writing `***` back keeps
  the stored value. Push is the exception to the plain-overwrite rule:
  `merge_kubesecrets()` merges the rendered manifests into an existing
  `kubesecrets.yaml` **by Secret name** (create-secrets.sh refuses to
  regenerate that file to avoid mass rotation), and ssh push logs redact the
  base64 payloads (`_redacting_log`).

The `genestack.config.push` operation (operator, mutating, per-env locked)
renders the current doc and writes the files to the env's config dir — one
base64 write command per file over the ssh executor when a deploy host is
set, local writes otherwise — backing up pre-existing files to
`.console-backup/<utc-ts>/` first. Push also syncs the doc's `deploy`/`maas`
fields onto the Environment row (maas api key stored encrypted). Dry-run logs
the file plan without writing.

## Deploy orchestration (`genestack.deploy`)

`app/services/deploy.py` implements the one-click deploy operation (admin,
mutating, per-env locked, 6h timeout — `timeout_seconds: 21600`, clamped at
the `MAX_JOB_TIMEOUT_SECONDS` ceiling). One job takes an environment from
config document to deployed cloud:

1. **Phase 1 — push** (skipped with `skip_push=true`): render the current
   config document and write the files to the env's config dir via the same
   `envconfig.push_rendered` path as `genestack.config.push` (base64 over ssh
   when a deploy host is set, local writes otherwise). Fails fast when no
   config document or no `genestack_config_dir` exists.
2. **Phase 2 — pipeline**: run every stage of `PIPELINE_STAGES`
   (`app/services/service_registry.py`) in order — hosts, infrastructure,
   operators, core, compute-network, platform-extras, observability, testing —
   each item as `bash <script>` through the Genestack bridge. The pipeline
   **stops at the first non-zero returncode** and reports
   `failed_at: <stage>/<item>` plus `stages_completed/stages_total`.
3. **Phase 3 — credentials** (non-dry-run only): run
   `bin/setup-openstack-rc.sh` so the deploy finishes with genestack's
   standard admin credentials (`~/.config/openstack/clouds.yaml`) on the
   deploy host. Non-fatal — a failure logs a warning and sets
   `credentials_warning` on the result instead of failing the deploy.

The `dry_run` param forces a rehearsal regardless of the env setting. Stage
counts land in the deploy audit entry (`env.deploy`); the workflow's deploy
step and the UI progress strip read them from there.

## Deploy-host preparation (`genestack.host_prepare`)

`app/services/host_prepare.py` is "getting started" as a button (admin,
mutating): what `docs/genestack-getting-started.md` tells operators to do by
hand, run over the ssh executor. Ordered steps, stop-on-first-failure:

1. **Preflight** — `git` must exist (fatal); missing `ansible-playbook` only
   warns (bootstrap.sh installs ansible into `~/.venvs/genestack`).
2. **Repo** — `git clone --recurse-submodules <repo_url> <genestack_path>`
   when absent; an existing checkout gets `git fetch` only (never a pull —
   the branch is left as-is); `checkout <repo_ref>` runs only on a fresh
   clone.
3. **Bootstrap** — `sudo -n -E bash bootstrap.sh` with `GENESTACK_CONFIG`
   pointing at the env's config dir (`-n` because ssh runs BatchMode).
4. **Verify** — the skeleton exists: provider file, inventory dir, and
   helm-configs dir under the config dir.

Params `repo_url` / `repo_ref` / `genestack_path` / `config_dir` default from
the env (metadata `repo_url`, `genestack_path`, `genestack_config_dir`) then
to rackerlabs/genestack@main, `/opt/genestack`, `/etc/genestack`. The
workflow's connect step reports the prepared state from the latest
host_prepare job, and the UI offers a **Prepare host** button there.

## Verify and day-2 operations

Post-deploy operations, all per-env locked mutating jobs through the same
bridge/ssh executor:

| Operation | Role | Timeout | What it runs |
|-----------|------|---------|--------------|
| `genestack.verify` | operator | 1h | `bash scripts/tests/run-all-tests.sh <level>` — genestack's own "did it work" suite; `level` ∈ `quick` / `standard` (default) / `full` (`full` provisions real test resources, then cleans up) |
| `genestack.k8s_upgrade` | admin | 6h | `ansible-playbook upgrade-cluster.yml --become -i <config_dir>/inventory` from `submodules/kubespray`; one major version per run, target from inventory `group_vars` `k8s_cluster.kube_version` (the `kube_version` param is advisory only — v1 writes no files) |
| `genestack.backup_mariadb` | operator | 1h | `bash scripts/backup-mariadb.sh` — dumps every openstack-namespace database (minus `performance_schema`/`information_schema`) to `$HOME/backup/mariadb/<timestamp>/` on the host it runs on |
| `genestack.tempest` | operator | 2h | genestack's official tempest conformance suite: `action=install` deploys the openstack-helm tempest chart via `bin/install-tempest.sh`, `action=run` executes `helm -n openstack test tempest`, `install-run` (default) does both (`suite` is a passthrough hint, not wired in v1) |
| `genestack.repo_script.run` | operator | 1h | Run a curated genestack utility as `bash scripts/<name> [args]` — the script must exist under `scripts/` and be in `SAFE_REPO_SCRIPTS` (`app/services/repo_scripts.py`, the documented extension point). Companion `genestack.repo_scripts.list` (viewer) inventories `scripts/*.sh`, `maintenances/*.txt`, and `ops-tools/**` |

The operate step of the workflow surfaces the latest `genestack.verify` job
(`verify: {job_id, status, level, finished_at}` in details) and the UI puts
verify-level buttons with a live result pill plus the day-2 buttons there.

## MAAS provisioning (write actions)

MAAS is optional. The console boots a server itself with DHCP and a boot file. That path is the next section. This section is only for a site that already runs a MAAS server. The operations live in `app/modules/maas/`.

Beyond inventory reads, the MAAS adapter (`app/services/maas.py`) supports
`commission` / `deploy` / `release` (operator, mutating, per-op timeouts
10–30 min) and a read-only `power_status` (viewer). The mock client applies
deterministic status transitions for each write op, so the full flow is
testable without a MAAS.

`maas.machine.deploy` is deploy → inventory in one action: it renders
cloud-init user-data (`app/services/maas_userdata.py` — hostname, SSH
authorized keys, and `GENESTACK_ENV` / `GENESTACK_ROLE` markers written to
`/etc/genestack/env`, which `ansible/playbooks/provision_bridge.yml` reads
when bridging a freshly deployed node), then **upserts the machine into the
env config doc's `servers` section** (source `maas`, as a new config
version). The Servers card drives
all three actions with per-MAAS-status buttons and polls each job to a
result line.

**Talos on MAAS.** `maas.talos.image_upload` (operator, 30 min) downloads a
Talos Image Factory image over HTTPS (bounded size, sha256 logged) and
uploads it as a custom MAAS boot-resource; the URL defaults to the doc's
`talos.image_url`. `maas.machine.deploy image=<name>` then deploys onto that
image and skips cloud-init entirely.

## Zero-touch bare-metal provisioning

For racks without MAAS the console provisions nodes itself. Components:

- **`app/services/redfish.py`** — a generic Redfish BMC client:
  `power_state`, `set_pxe_boot` (one-shot, `BootSourceOverrideTarget=Pxe` /
  `Once`), `power` (On / ForceOff / ForceRestart via
  `Systems/1/Actions/ComputerSystem.Reset`), and `system_macs`. HTTP Basic
  auth, `verify=False` (BMCs present self-signed certs — deliberate),
  10 s timeouts.
- **`BaremetalNode`** (`app/models.py`) — per-env node registry (unique per
  env+name): BMC address + credentials (`bmc_password` Fernet-encrypted at
  rest like the other stored secrets), `pxe_mac`, `expected_ip` (reserved
  from the PXE pool at provision time), and a state:
  `registered` → `booting` → `talos-ready` (or `failed`).
- **`app/services/pxe.py` + `app/services/pxe_runtime.py`** — DHCP and
  boot-file HTTP run in the console process. The host must sit on the
  provisioning L2, because DHCP is a broadcast. The console renders
  `<data_dir>/pxe/`: `dnsmasq.conf` (authoritative DHCP + iPXE chainload,
  static `dhcp-host` reservations for `source: baremetal` servers),
  `boot.ipxe`, and `assets/` (kernel and initramfs). Per-MAC scripts live
  under `mac/`. Config comes from the env doc's `pxe:` section or from the
  agent's `pxe_config` (`interface`, `range_start`, and `range_end` required).
  A write reloads the in-process runtime. There is no PXE sidecar.
- **Catalog ops** — `baremetal.node.register` (operator; Redfish MAC
  autofill when `pxe_mac` is omitted — probe skipped on dry-run, failure
  registers without a MAC), `baremetal.nodes.list` (viewer),
  `baremetal.node.power` (operator, 2 min), `baremetal.node.pxe_boot`
  (operator, 5 min — one-shot PXE + ForceRestart, state → `booting`), and
  `baremetal.node.provision` (admin, 30 min): the full zero-touch chain in
  one job — prepare PXE config and assets → PXE boot → poll the Talos
  maintenance API at `https://<expected_ip>:50000` (insecure) → state
  `talos-ready` → **upsert the node into the env doc's `servers` section**
  (source `baremetal`, optional `roles`).
- **REST** — `GET /api/v1/environments/{id}/baremetal` (viewer) feeds the
  UI's Bare metal card; all mutations go through the job queue.

The handoff target is the talos bootstrap below: a `talos-ready` node is
ordinary inventory from that point on.

## Talos provider (`provider=talos`)

Environments whose config doc sets `provider: talos` bring Kubernetes up
with talosctl instead of kubespray:

- **`genestack.talos.bootstrap`** (admin, 2h, per
  `docs/k8s-talos.md`): `talosctl gen config` → `apply-config` to each
  control-plane and worker node → set endpoints → bootstrap etcd (once) →
  fetch the kubeconfig — all from `<config_dir>/talos` on the deploy host.
  Control-plane nodes are doc servers with role `k8s_control_plane`; every
  other server is a worker. Cluster name and install disk come from the
  doc's `talos:` section (defaults: env name, `/dev/sda`). Stops at the
  first failing phase. (Operational note from the catalog: pin kube-ovn to
  v1.14.10 and boot nodes from a factory image with iscsi-tools +
  util-linux-tools extensions.)
- **Deploy branching** — in `genestack.deploy`, the `hosts` pipeline stage
  runs the talos bootstrap flow (`app/services/talos.py`) instead of
  `bin/setup-hosts.sh`; the kubespray `cluster.yml` portion of that script
  is skipped entirely. The remaining stages are unchanged.

## Agent channel (hub-and-spoke)

Environments behind firewalls/NAT can't be reached inbound, so the channel
is inverted: an agent inside the environment dials **out** to the console
hub over a persistent WebSocket. All four phases are landed: enrollment +
proof commands, execution through the agent (push/deploy), the
hardware-discovery inbox, and the BMC sweep.

**Components** — `AgentCredential` (`app/models.py`), the registry and
protocol helpers (`app/services/agents.py`), the DB-backed relay
(`app/services/agent_relay.py` + the `agent_commands` table), executor
selection (`app/services/executors.py`), the REST/WS endpoints
(`app/routers/agents.py`), and the standalone agent (`agent/`: `main.py` —
pure-stdlib + `websockets`, non-root `Containerfile`, `install.sh`;
configured via `GSC_HUB_URL` / `GSC_AGENT_TOKEN` / `GSC_AGENT_NAME`, plus
`GSC_ALLOWED_ROOT` and `GSC_PXE_LEASES` for the phase 2/3 features).

**Token model** — `POST /api/v1/environments/{id}/agent/token` (admin)
generates a `gsca_…` token, stores **only its sha256**, and returns the raw
value **once** together with a `docker run` one-liner and the curl-pipe
installer command (the console self-hosts `agent/install.sh` at
`GET /agent`). Exactly one credential per environment: creating a new token
deletes the old row (revocation by replacement). Creation is audited
(`env.agent_token.create`). `agent.install` (admin) push-installs the agent
onto a host over ssh — the curl-pipe installer on the target, or the
packaged script streamed over ssh stdin when the target can't reach the
hub; requires `hub.advertise_url`; the raw token is masked (`gsca_***`) in
the job log.

**Protocol** — the agent connects to `WS /api/v1/agents/connect?token=<raw>`;
the hub verifies `sha256(token)` against the stored credential and answers
`challenge{nonce}`; the agent proves possession with
`proof{hmac: HMAC-SHA256(token, nonce)}` (10 s handshake timeout, auth
failures close the socket). After `welcome`, the agent sends `hello`
(hostname, version, caps), heartbeats every **15 s**, and executes command
frames, streaming output back as `log` frames and finishing with
`result{rc, stdout, stderr}`; `bye` on shutdown. **45 s** of frame silence
marks the agent offline. Live metadata (`last_seen`/`hostname`/`version`)
is mirrored onto the credential row as frames arrive;
`GET /api/v1/environments/{id}/agent/status` (viewer) combines enrollment
(DB) + live connection (registry). Extended frames (locked with the agent):

| Direction | Frame | Purpose |
|-----------|-------|---------|
| hub → agent | `command{id, cmd[], cwd?, env?, timeout}` | Run a command |
| hub → agent | `file_write{id, path, b64, mode, backup_dir}` | Write a file (backup first) |
| hub → agent | `command{scan_bmc: {subnet}}` | Sweep a subnet CIDR for Redfish BMCs |
| agent → hub | `log{id, line}` / `result{id, rc, …}` | Streamed output / terminal result |
| agent → hub | `pxe_request` / `bmc_found` | Discovery-inbox events (PXE sightings from the `GSC_PXE_LEASES` watcher; BMC finds from sweeps) |

**Cross-process execution (the relay)** — live websockets stay in the API
process (`AgentRegistry` is in-memory, per-process), but job handlers run in
the worker daemon. Like the SSE relay, SQLite is the shared medium: callers
insert `agent_commands` rows with `agent_exec()` (sync, any process —
kinds `run_command` / `file_write`); a relay task started from the API
lifespan (`agent_relay_enabled`) polls for `pending` rows, claims them,
dispatches the frame to the env's connected agent, streams agent `log`
frames into the row's append-only `log_text`, and records the result.
Lifecycle: `pending → dispatched → done | failed | timeout`; an env with no
connected agent fails immediately. Two SQLite details make this safe:
`PRAGMA busy_timeout=5000` on every connection (`app/db.py`), and
`JobRunner.append_log` **commits per line** — a flush-only session would
hold the write lock for the whole dispatch and deadlock the relay's writes
from the same thread.

**Executor selection** — `pick_executor(env, ctx)`
(`app/services/executors.py`) chooses where an env's commands run:
**agent → ssh deploy host → local**. The agent check is DB-only (credential
with a recent frame), so it works from the worker process. Job handlers
(pipeline stages, enable_service, deploy) and config push all route through
it.

**How push works via agent** — `genestack.config.push` / `genestack.deploy`
phase 1 with an agent-connected env:

1. The worker-side handler renders the doc into files as usual, then calls
   `_push_file_agent` per file instead of the local/ssh writer.
2. Each file becomes a `file_write` payload `{path, b64, mode: 0644,
   backup_dir}` inserted as a pending `agent_commands` row; `agent_exec`
   blocks on the row's terminal status.
3. The API-process relay claims the row and forwards the frame over the
   env's websocket.
4. The agent resolves `path` against its allowed roots (`GSC_ALLOWED_ROOT`,
   default `/etc/genestack` — writes elsewhere are refused), backs up a
   pre-existing target into `backup_dir` (the same
   `<config_dir>/.console-backup/<ts>/` layout as local/ssh pushes), writes
   the file, chmods 0644, and replies `result{rc}`.
5. The relay stores the result; the worker's blocked `agent_exec` returns it;
   a non-zero rc fails the push. `genestack.deploy` phase 2 then runs the
   whole pipeline the same way (`run_command` rows) when the executor is
   `agent`.

**Commands** — `agent.command` (admin, mutating, 10 min) runs an allowlisted
proof command (`uptime`, `hostname`, `ip addr`, `talosctl version`,
`kubectl get nodes`, `ls /etc/genestack` — `AGENT_COMMAND_ALLOWLIST`) on the
env's connected agent; log frames stream into the job log and the agent's
return code decides the job result (audited as `env.agent.command`).
`agent.status` (viewer) is the read-only companion.

**Threat notes** — hash-only storage means a DB leak doesn't leak usable
tokens; the HMAC challenge/proof means the raw token crosses the wire only
once (the connect query string — use `wss://`/TLS in real deployments, as
with any bearer-in-URL); credentials are scoped to one environment and
created under tenant-scoped admin; `agent.command` is a fixed allowlist —
no general shell; `file_write` is confined agent-side to the allowed roots.

## Hardware discovery inbox

Agents report what they see on the provisioning network; the hub stores it
as a per-env inbox (`app/services/discovery.py`):

- **`DiscoveredNode`** — PXE/DHCP sightings. The agent watches a dnsmasq
  leases file (`GSC_PXE_LEASES`) and reports new/renewed leases as
  `pxe_request` events; the hub ingests them in
  `agents.handle_agent_event`. States: `discovered` → `claimed`.
- **`DiscoveredBmc`** — Redfish finds. `baremetal.bmc_scan` (operator,
  15 min) routes a `scan_bmc{subnet}` frame to the env's connected agent,
  which sweeps the CIDR for Redfish endpoints and reports each as a
  `bmc_found` event. States: `new` → `registered`.

Endpoints (`app/routers/discovery.py`, all env-scoped):

| Endpoint | Role | Purpose |
|----------|------|---------|
| `GET /api/v1/environments/{id}/discovery` | viewer | Both inbox tables |
| `POST …/discovery/claim` | operator | Claim a sighted host into the env doc's `servers` section (source `baremetal` — the same hand-off as `baremetal.node.provision`) |
| `POST …/discovery/bmc-creds` | operator | Attach credentials to a found BMC — creates/updates the linked `BaremetalNode` (password Fernet-encrypted) |

The UI surfaces the inbox as the Hardware discovery card (inventory step of
the env spine; "Discovery" tab on the Hardware page).

## Deploy-host terminal

`WS /api/v1/terminal` (`app/routers/terminal.py`) gives operators an
interactive ssh shell on the env's deploy host without leaving the console UI.
The server spawns a pty running `ssh -o BatchMode=yes -o ConnectTimeout=10
<deployer_ssh_user=root>@<deployer_ssh_host>` and bridges frames:
`input`/`resize` client → server (pty stdin / TIOCSWINSZ), `output`/`exit`
server → client.

Guardrails: **operator role minimum** (auth via `?token=` or standard
headers — browsers can't set WebSocket headers; tenant-checked); **one
session per (user, environment)** — a second connect replaces the first
(close code 4000); **15-minute idle timeout**; the pty is killed when the
socket closes; **no free-form command** — the v1 target is always the env's
deploy host (no deployer configured → close 4400). Opens and closes are
audited on their own DB session (the WS outlives any request session).
`terminal.command_override` in config.yaml replaces the ssh argv wholesale —
a test hook (CI points it at `/bin/cat`), not a feature.

The frontend (`pages/environment_terminal.js`, mounted in the workflow's
connect step) lazy-loads the **vendored** xterm.js assets from
`app/static/vendor/xterm/` (no CDN) with the fit addon, clipboard shortcuts,
fullscreen, and complete key handling; role-gated to operator+.

## Guided workflow and fleet board

Two read-only aggregation views sit on top of env state; both are built to
**never raise** (one broken env/step degrades to `pending` with an `error`
note, never a 500).

- **Workflow** — `GET /api/v1/environments/{id}/workflow` (viewer+;
  `app/services/workflow.py`) returns six lifecycle steps in fixed order —
  connect, inventory, config, push, deploy, operate — each with a
  traffic-light state (`done` / `attention` / `pending`), a one-line summary,
  and a details mapping. Step sources: connect = env fields (config dir, ssh
  deploy host, kubeconfig source) plus the prepared state from the latest
  `genestack.host_prepare` job; inventory = config-doc servers and
  required roles (`k8s_control_plane`, `etcd`, `control`); config = latest
  `EnvConfigVersion`; push/deploy = latest push/deploy job + audit details;
  operate = live kubectl/helm probes, the descriptor's inventory drift
  check, and the latest `genestack.verify` job (`verify` in details). The
  service's `include_operate_probe=False` flag skips the live
  cluster probes (operate reports "not checked") for callers that fan out.
- **Fleet board** — `GET /api/v1/fleet` (viewer+; `app/services/fleet.py`)
  returns the caller's tenants (all tenants for platform admins) plus every
  visible environment reduced to a compact row: per-step states,
  `current_step` (first step not `done`, in lifecycle order), and a compact
  deploy view (`job_id`, `status`, stage counts). It builds each env's
  workflow with `include_operate_probe=False`, so a fleet of N environments
  never costs N kubectl calls — the board **never probes clusters
  synchronously**. Live cluster health on the board instead comes from
  `cluster_snapshots` rows written by the collector (see below), surfaced via
  `GET /api/v1/fleet/live` and the SSE `fleet` topic; the request path itself
  only reads the database.

## Telemetry & real-time subsystem

The console keeps a live picture of every environment without putting
cluster calls in the HTTP request path: a collector in the worker daemon
writes snapshots to the database, an in-process event bus fans changes out
over Server-Sent Events, and an alerting layer evaluates rules against each
snapshot.

### Collector and cluster snapshots

The worker daemon (`python -m app.worker.runner --daemon`) runs a collector
alongside the job loop (`--no-collector` disables it; `collector.enabled:
false` in config.yaml does the same). Each environment is
probed on its own schedule — every `collector.interval_seconds` (default 60,
clamped ≥5) — from a bounded thread pool of 4 so one hung cluster cannot
starve the others. A probe stages the env's kubeconfig the same way jobs do
and runs three reads with a `collector.probe_timeout_seconds` budget
(default 15):

- `kubectl get nodes -o json`
- `kubectl get pods -A -o json`
- `helm list -A -o json`

Each probe stores one **`cluster_snapshots`** row: the raw nodes/pods/helm
JSON, a computed summary (counts, crashlooping pods, release states), a
derived `health`, and `probe_ok` + an error string when the probe failed.
Health rules, in order:

| Health | Condition |
|--------|-----------|
| `down` | Probe failed (unreachable API, bad kubeconfig, timeout) |
| `degraded` | Any node `NotReady`, any pod `CrashLoopBackOff` or with ≥5 restarts, or any failed pod |
| `healthy` | Otherwise |
| `unknown` | No usable signal yet (e.g. before the first probe) |

Snapshots older than `collector.retention_hours` (default 168 — 7 days) are
pruned, except the newest snapshot per environment, which is never deleted.

### State API

- `GET /api/v1/environments/{id}/state` — latest snapshot for one env
  (404 until the first probe lands).
- `GET /api/v1/environments/{id}/state/history?limit=&hours=` — recent
  snapshots for trend views.
- `GET /api/v1/fleet/live` — latest-snapshot rollup for every
  tenant-visible environment (what the fleet board's health pills read).

All three are pure database reads — no synchronous cluster access.

### Event bus and SSE stream

`app/services/events.py` is a small in-process pub/sub bus. Producers (the
collector, the job runner, the alerting engine) publish to topics; the SSE
endpoint multiplexes them to browsers. Topics:

| Topic | Carries |
|-------|---------|
| `fleet` | Fleet-level changes (snapshot health transitions) |
| `env:{id}` | Per-environment snapshots and alert events |
| `jobs` | Job status transitions (queued → running → success/failed) |
| `alerts` | Alert firing/resolved events |
| `metrics` | Metric sample batches (when metrics are enabled) |

`GET /api/v1/stream?topics=fleet,alerts,jobs&token=` upgrades to SSE.
Messages are `data: {"topic": "...", "payload": {...}}` frames with a 15 s
heartbeat comment so proxies don't reap idle connections. Because
`EventSource` cannot set headers, the stream authenticates with a `?token=`
query param (session token or API key) instead of `Authorization`/`X-API-Key`.
`env:{id}` topics are gated by tenant membership **at connect time**, and
`stream.max_subscribers` (default 100) caps concurrent subscribers — new
connections past the cap are refused. Known follow-up: `fleet`-topic
payloads are not yet filtered per tenant beyond connect-time gating.

The bus is strictly in-process, but the publishers above run in the worker
daemon process — their events never reach the uvicorn process where SSE
subscribers live. `app/services/relay.py` closes that gap: a task started in
the API lifespan polls the shared SQLite database every
`stream.relay_interval_seconds` (default 3 s, `stream.relay_enabled` to
disable) and re-publishes new snapshots, alert transitions, and job status
changes onto the local bus with the exact payload shapes the worker emits.
Boot high-water marks (current table maxima, captured without publishing)
keep an API restart from storming subscribers with historical rows.

### Alerting

Two tables drive alerting:

- **`alert_rules`** — global or per-env (`environment_id` null = global).
  A rule has a `condition` (`node_not_ready` | `pod_crashloop` |
  `probe_failed` | `service_down`), a `severity` (`info` | `warning` |
  `critical`), and an optional `webhook_url`. Four default **global** rules
  are seeded automatically: node-not-ready (critical), pod-crashloop
  (warning), probe-failed (critical), and core-service-down (critical) — the
  last fires when a helm release for one of the core services (mariadb,
  rabbitmq, keystone, memcached, glance, nova, neutron, horizon) has a status
  other than `deployed`.
- **`alert_events`** — a firing/resolved state machine per (rule, env).
  Rules are evaluated against each new snapshot: while an event is firing,
  repeat matches are deduplicated onto the open event; when the condition
  clears, the event resolves. Events can be acknowledged via the API.
  Webhooks are POSTed **on fire only**, not on resolve.

Endpoints: `GET/POST /api/v1/alerts/rules`, `PATCH/DELETE
/api/v1/alerts/rules/{id}`, `GET /api/v1/alerts/events`, `POST
/api/v1/alerts/events/{id}/ack`, `GET /api/v1/alerts/summary`.

### Metrics (optional)

With `metrics.enabled: true` the collector parses `kubectl top nodes` /
`kubectl top pods` into **`metric_samples`** rows — CPU cores and memory
bytes, labeled by node or namespace/pod — and also writes gauge samples
from live APIs (`cluster.nodes.ready`, `talos.nodes.reachable`,
`cloud.servers.active`, `jobs.running`, `alerts.firing`, …). Samples are
retained for `metrics.retention_hours` (default 72). Collection degrades
gracefully: when metrics-server is absent the `top` calls are skipped and
gauges still record (snapshot health is unaffected). The Observe plane
exposes this in-console: `GET /environments/{id}/observe` and
`GET /fleet/observe` (viewer) return live tiles plus downsampled series.
Empty series is 200. Example config enables collection.

### How the UI consumes it

Every live surface prefers SSE and falls back to polling when the stream is
unavailable:

- **Fleet board** — health pills per environment update from the `fleet`
  topic (polling `/api/v1/fleet/live` as fallback).
- **Environment detail** — the operate step's cluster card renders nodes,
  pods, and helm releases from the snapshots, refreshed over the `env:{id}`
  topic.
- **Alerts** (Activity page tab) — rules CRUD, fire/ack on events, and a nav
  badge with the firing count, fed by the `alerts` topic and
  `GET /api/v1/alerts/summary`.
- **Observe** — environment tab and fleet page (`#/observe`) from
  `GET …/observe` and `GET /fleet/observe`: live Talos / Kubernetes /
  OpenStack / jobs / alerts tiles plus SVG series. No Grafana iframe.
- **Jobs list** — live status updates from the `jobs` topic.

## Web UI map

`GET /ui` serves `app/templates/ui.html` — a hash-routed single-page UI
of plain ES modules under `app/static/js/` (no build step).

| Module | Renders |
|--------|---------|
| `app.js` | Shell: login (session token, service-key toggle), whoami, hash router, topbar, first-login welcome overlay; nav groups **Environments / Hardware / Activity / More ▾** |
| `store.js`, `api.js`, `stream.js` | Shared client state (envs, role gates), fetch wrapper (+ shared pills, e.g. `dryRunPill`), ref-counted SSE EventSource |
| `pages/tenant.js` | Tenant switcher in the nav; tenant filter for the fleet board |
| `pages/fleet.js` | **Environments nav, landing after login** (`#/fleet`): fleet board from `GET /api/v1/fleet` — step states, current step, next action per env; live health pills over SSE; agent connectivity dot |
| `pages/observe.js`, `environment_observe.js` | Fleet Observe (`#/observe`) and environment Observe tab: live tiles + series from `GET /fleet/observe` and `GET …/observe` |
| `pages/environments.js`, `env_wizard.js` | Env registry; `#/setup` guided-setup wizard (4 steps, machine-source radio MAAS/static/bare-metal, sessionStorage persistence) |
| `pages/hardware.js` | `#/hardware`: one env selector + lazy tabs — **Discovery** (`environment_discovery.js`), **Bare metal** (`environment_baremetal.js`), **MAAS machines** (`machines.js`) |
| `pages/activity.js` | `#/activity`: Jobs / Alerts / Audit tabs (one mounted at a time — `jobs.js`, `alerts.js`, `audit.js`); firing-alert badge in the nav |
| `pages/hosts.js` | `#/hosts` (under More): QEMU host VMs with operator-gated power actions |
| `pages/environment_detail.js` | Environment page: the workflow spine **is** the page — one step expanded at a time, cards rendered once into the hidden `#wf-mod-park` and moved into the expanded step's mount slot. At the bottom, ONE "Expert" `<details>` block: raw descriptor, `environment_components.js`, helm/kustomize/gateway file views |
| `pages/environment_workflow.js` | Workflow spine from `GET …/workflow` — six steps with what/why explainers and per-step how-tos; Prepare host, agent status pill + create-token modal (connect); verify buttons + result pill, day-2 buttons (operate) |
| `pages/environment_terminal.js` | Deploy-host terminal card (connect step): lazy-loads vendored xterm.js from `/static/vendor/xterm/`, bridges `WS /api/v1/terminal` frames; clipboard/fullscreen/key handling; operator-gated |
| `pages/environment_discovery.js` | Hardware discovery card (inventory step): PXE/DHCP sightings with per-row Claim…, BMC finds with per-row Credentials…; reads `GET …/discovery` |
| `pages/environment_config.js` | Config editor (config step): versioned YAML doc, history, render preview, push + Deploy buttons; secret values stay masked |
| `pages/environment_servers.js`, `environment_baremetal.js` | Inventory-step cards: MAAS/static servers with role assignment; console-managed bare-metal registry (register, power/PXE-boot/Provision) |
| `pages/environment_progress.js` | Deploy-step progress strip: 8 pipeline stages parsed from the deploy job's log, polled every 5 s while queued/running |
| `pages/environment_cluster.js`, `environment_openstack.js` | Operate-step cards: live cluster detail, OpenStack control-plane state |
| `pages/services.js`, `pipeline.js`, `operations.js` (More), `machines.js`, `jobs.js`, `alerts.js`, `audit.js`, `dashboard.js` | Component catalog / pipeline view / operation catalog; tab-mounted modules (MAAS machines, jobs, alerts, audit); dashboard partials reused by the fleet page |
| `pages/environment_cloudmap.js`, `environment_live.js`, `environment_instances.js` | **Unmounted since the task-flow restructure** (superseded by the cluster/openstack cards) — dead code pending cleanup |

## Job lifecycle

```
          submit (queued by default; run_sync=true runs inline)
            │
            ▼
        ┌───────┐   atomic claim: UPDATE … WHERE status='queued'
        │queued │   (rowcount == 1 — one worker wins)
        └───┬───┘
            ▼
        ┌───────┐
        │running│
        └───┬───┘
            │
     ┌──────┴──────┐
     ▼             ▼
┌─────────┐  ┌─────────┐
│ success │  │ failed  │
└─────────┘  └─────────┘
```

Canonical terminal states in the ORM: `success` and `failed`.
Clients may also treat aliases (`succeeded`, `completed`) as success.

Async execution:

- **Queued by default** — `execute_operation(run_sync=False)` just inserts a
  queued row; explicit `run_sync=true` still executes inline for quick reads.
- **Worker** — `python -m app.worker.runner` claims queued jobs oldest-first
  and runs them via `JobRunner`. Modes: `--once` (default single batch),
  `--loop` (until the queue drains), `--daemon --interval N` (poll forever,
  default 5 s), `--job-id <uuid>` (single job; finished jobs are re-queued).
  One bad job never kills the loop. In daemon mode the worker also hosts the
  telemetry collector (per-env cluster snapshots; `--no-collector` disables
  it — see Telemetry above), and job status transitions are published to the
  event bus.
- **Per-op timeouts** — each job runs with its operation's
  `timeout_seconds` from the catalog (deploy 6h, pipeline 4h, service
  enable/host setup/playbook 1h), falling back to
  `settings.job_timeout_seconds` (default 600 s) and clamped at the 6h
  `MAX_JOB_TIMEOUT_SECONDS` ceiling.
- **Stale-job recovery** — on API and worker startup,
  `recover_stale_jobs()` fails *running* jobs whose `started_at` is past
  their effective timeout (same source as live deadlines:
  `effective_timeout_seconds`). Queued jobs are never expired on wait age.
  The job worker additionally calls `recover_abandoned_running_jobs()` once
  at process start (not from the API lifespan) to fail in-flight work a new
  worker cannot inherit.
- **Claim-time env lock** — a queued mutating job whose environment already
  has a running mutating job is skipped that pass (left queued) and retried
  later; submission-time rejection (HTTP 409) lives in `execute_operation`.
- **SQLite concurrency** — every connection sets `PRAGMA busy_timeout=5000`
  (`app/db.py`), and `JobRunner.append_log` **commits per line**: a
  flush-only session would hold the write lock for a whole dispatch and
  deadlock the agent relay, which writes `agent_commands` rows from the same
  thread on a second connection while the job is still running.

### Operation categories (catalog)

| Prefix | Examples | Typical min role |
|--------|----------|------------------|
| `maas.*` | `maas.machines.list` | viewer (read) / operator (write) |
| `baremetal.*` | `baremetal.nodes.list`, `baremetal.node.provision` | viewer (read) / operator / admin (provision) |
| `agent.*` | `agent.status`, `agent.command` | viewer (status) / admin (command) |
| `host.*` / `ansible.*` | `host.preflight` | operator |
| `genestack.*` | `genestack.scripts.list`, `genestack.service.enable`, `genestack.components.desired` | viewer / operator |

Service enablement is **allow-listed** (e.g. `placement` ok; `rm` rejected).

## Safety model

| Control | Default | Notes |
|---------|---------|-------|
| `dry_run` in config.yaml | `true` | Destructive steps are logged, not executed; per-env `dry_run` field overrides |
| empty `maas.url` | not configured | No fake machines. Set `maas.mock: true` only for a dev stand-in. Booting a server does not use MAAS. |
| API keys + roles | config.yaml | `X-API-Key` header; viewer < operator < admin; static keys = platform-admin break-glass |
| Tenant isolation | enforced | Env lists filtered by membership; cross-tenant access → 403 |
| User sessions | 12 h TTL | PBKDF2-SHA256 (600k) passwords; `auth.session_ttl_hours`; logout invalidates |
| Service allowlist | enforced | Blocks shell injection / unknown services |
| Per-env mutating-job lock | enforced | Second mutating job for same env → HTTP 409 |
| Per-op job timeouts | 600 s default, 6 h ceiling | Long ops override in the catalog (deploy 6 h, pipeline 4 h); running jobs past deadline recovered to `failed` on startup; worker also abandons all running once at start |
| Secret encryption at rest | on | Fernet (`fernet:` prefix) keyed off `secret_key` — `kubeconfig_data`, MAAS api key, config-doc `secrets`, bare-metal BMC passwords; masked as `***` on reads; rotation requires re-encrypting |
| Redfish BMC access | per-node creds | Basic auth, `verify=False` (self-signed BMC certs), 10 s timeouts; creds stored encrypted, never returned by the API |
| PXE/DHCP | in-process | Console-owned DHCP and boot HTTP on the provisioning L2; files under `data_dir/pxe/`; a remote site uses the dial-out agent |
| Agent channel | hash-only tokens | Raw `gsca_` token shown once, sha256 stored; HMAC-SHA256 challenge/proof (raw token on the wire once — use `wss://`); one credential per env, replace-on-create; commands fixed-allowlist only; file_write confined agent-side to `GSC_ALLOWED_ROOT` |
| Deploy-host terminal | operator+ | One session per (user, env), 15-min idle timeout, pty killed on close, no free-form command (always the env's deploy host), open/close audited |
| SSE stream | capped | `?token=` auth (session token or API key), env topics gated by tenant membership at connect, `stream.max_subscribers` (default 100) caps concurrent subscribers |
| Audit log | on | Who ran what, when, against which env |

## Data

- Default DB: SQLite under `data_dir` from config.yaml (`./data/console.db`).
- Tests use a temp config + SQLite and never touch production data.

## Related paths

| Path | Role |
|------|------|
| `config.yaml` | Single configuration file |
| `app/` | FastAPI application |
| `app/static/js/` | Web UI (plain ES modules, no build step) |
| `ansible/playbooks/` | Console-owned playbooks |
| `pxe/` | Rendered boot files (iPXE, assets). DHCP and HTTP are in-process, not this directory as a container |
| `agent/` | Standalone in-environment agent (`main.py`, `Containerfile`, `install.sh`) |
| `app/static/vendor/xterm/` | Vendored xterm.js + fit addon for the deploy-host terminal (no CDN) |
| `genestack.root` | Genestack checkout (`bin/`, `openstack-components.yaml`) |
| `scripts/systemd/` | systemd units for the API and worker daemon |
| `tests/` | Contract + unit tests |
