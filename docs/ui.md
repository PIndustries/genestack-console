# The console web page

The page at `http://127.0.0.1:8080/ui` is a client of the API on that same host. It does not talk to Kubernetes, Talos, or a management port by itself. Every click is a call to `/api/v1`, and the console on the deploy host does the work. From a laptop, forward the port with `ssh -L 8080:127.0.0.1:8080`.

The install chapter is [Genestack Console](genestack-guide.md). The API the page calls is [The console HTTP API](http.md). What the console does with that call is [How a job runs](runtime.md).

## The shell

`app/templates/ui.html` is the frame: the sign-in form, the sidebar, and an empty main area. `app/static/js/app.js` reads the hash in the address (`#/fleet`, `#/hardware`) and loads one screen. Each screen is a file in `app/static/js/pages/`. A screen renders into the main area and calls the API. It does not embed another program.

The sign-in card and the sidebar show the Genestack Console mark, centered, with the name under it. The P.Industries wordmark sits centered at the bottom of the sidebar, under the API docs link and Changelog. The sidebar and the cards are glass, so the field shows through. Each card eases on a faint edge, out of step with the next. A reduced-motion setting turns that motion off.

The sidebar is:

| Link | Hash | File | What you do there |
| --- | --- | --- | --- |
| Environments | `#/fleet` | `app/static/js/pages/fleet.js` | Every cloud this console knows. One row each. The row is the last snapshot, not a live probe. |
| Observe | `#/observe` | `app/static/js/pages/observe.js` | Logs and the observe view across the console. |
| Hardware | `#/hardware` | `app/static/js/pages/hardware.js` | Inventory, bare metal, and provider accounts. |
| Activity | `#/activity` | `app/static/js/pages/activity.js` | Jobs, alerts, and the audit log. Three tabs. |
| Catalog | `#/operations` | `app/static/js/pages/operations.js` | Every operation the console can run. The same list as `GET /api/v1/operations`. |
| Admin | `#/admin` | `app/static/js/pages/admin.js` | People, tenants, memberships, and, on `main`, Reach and Database. Hidden unless you are a platform admin. |

A new install opens on a welcome. It says this machine stays outside the cluster, and inventory is one list. Add each machine by hostname and IP. A virtual machine and a physical server are the same row. On Machines, record Talos or Ubuntu, then Deploy. A cluster that is already running is Settings, Access, Adopt Kubespray. Guided setup is `#/setup`, `app/static/js/pages/env_wizard.js`. The Servers step is that list. Import or discover stays behind it and writes the same rows. The Talos image field stays blank, and the page shows the ISO to boot yourself. The summary has Apply on this environment and opens Machines. An empty address, and a company login that lands on `#/fleet`, stay on the fleet. `#/setup` and `#/admin` are left alone.

The API docs link in the sidebar is `/docs` on the console. That is a catalog of the routes, with a curl for each one. The curls point at the console you are signed in to.

Changelog is the button under that link. It lists the public pipeline and the release notes. The bullets in a release are the changes in that build. After this console installs a newer build, the page reloads and shows those notes until they are acknowledged.

## One environment

Open an environment and the sidebar gains a second block: Overview, Image cache, Machines, Kubernetes, OpenStack, Observe, Settings. The screen is `app/static/js/pages/environment_detail.js`. It pulls the other cards in. Those cards are the other files whose names start with `environment_`.

The bar above the tabs is the switch for this environment. Apply on this environment makes jobs from here change the machines. Look around only puts them back to a log. It does not edit `config.yaml` and it does not restart a service. An operator can use it. A viewer sees it and cannot press it.

Overview is the install map. It shows where this cloud is, from an empty checkout to OpenStack answering. The map reads the workflow API. It does not start a job by being opened. Machines are added on Machines, not on this map.

Image cache is its own tab. It lists each pull-through registry and the Helm charts. Cache images and charts queues `registry.mirror`. That copies the images this cluster runs onto this console. An admin starts it. It does not power a machine. OCI charts are stored in that same cache. The other charts stay on their Helm repo. Maintenance on the map still has the same button.

Machines is the list of computers. The list is the first thing on the page. Talos and Ubuntu filter that list. Add a machine opens a dialog. Saving records a hostname and IP. It does not boot the machine. Each row shows whether the address is up, the system is running, the console can connect, and the login was accepted. A Talos machine with no SSH port stays marked Talos. `environment_servers.js` asks `GET /servers/reach`. That check does not install or reboot anything.

| Tab | What it is |
| --- | --- |
| Talos | Talos machines in the list. Versions, Ready, logs, and upgrades sit under the list. Talos is already installed applies a config to every saved address and does not power the machines. After that job succeeds, Deploy from infrastructure starts OpenStack and does not run Talos bootstrap again. `environment_platform.js` and `environment_servers.js`. |
| Ubuntu | Ubuntu machines in the same list. Status is up, running, connect, and authenticated. Already have an OS only records them. Deploy over SSH starts at the hosts stage. |
| Kubernetes | Nodes, namespaces, and pods. `environment_cluster.js`. |
| OpenStack | The cloud services and the instances. `environment_openstack.js` and `environment_cloud.js`. |

On a row, Ubuntu and Talos are the two choices. The outlined choice is the operating system already recorded. The button says Reinstall when you keep that system, and Install when you pick the other one. Ubuntu queues `hosts.ubuntu.bringup`. Talos on a row that has a management port queues `baremetal.node.next_boot` with the next boot set to Talos and boot now. Talos on a row with no management port, and the Talos is already installed button, queue `genestack.talos.bootstrap` for every saved host. That job does not power the machines. An admin starts it. Already have an OS records the selected hosts that already have Ubuntu. It does not install. A management port is optional. Open wall lists the management ports. Select a server and its console appears. Select it again and that console goes away. Show all live opens the rest. Clear and Close empty the stage. Opening a console does not power or boot the machine. The row and the wall ship in `v2026.10.04.1`. The `v2026.10.03` binary does not have this row or this wall.

Settings is four more tabs.

| Tab | What it is |
| --- | --- |
| Config | The settings document. Saving a version does not edit `/etc/genestack`. A push job does that. `environment_config.js`. |
| Apps | An application to deploy onto a cloud that is already up. `environment_apps.js`. |
| Access | SSH keys, agents, Reach, Hosts, and the bare-metal cards. `environment_sshkeys.js`, `environment_agents.js`, `environment_reach.js`, `environment_hosts.js`, `environment_baremetal.js`, `environment_pxe.js`. |
| Expert | The fields that do not have their own card. |

The bare-metal cards are also on the Hardware page. Hardware has three tabs: Inventory, Bare metal, and Providers. Inventory is the servers you have typed in. Bare metal is the boot service and the management ports. The next boot on a bare-metal row is Disk, Commission, Serve Talos, or Install Ubuntu. Providers is a saved login for OVH, Rackspace, AWS, Azure, or GCP. That login is stored in the console database on the deploy host.

Activity has three tabs.

| Tab | File | What it shows |
| --- | --- | --- |
| Jobs | `jobs.js` | Queued, running, and finished jobs, and the log of the one you open. |
| Alerts | `alerts.js` | Rules, firing events, and, on `main`, the notification channels. |
| Audit | `audit.js` | Who changed what. |

A notification channel is a Slack, Discord, or Teams webhook, or a Resend or Twilio credential. An admin saves it on the Alerts tab. The secret is encrypted and is not shown again. A rule can send through that channel and through its own webhook URL. This is on `main`. The `v2026.10.03` binary does not have that card.

On `main`, Admin has a Reach card for the deploy host, and Settings → Access has a Reach card for one environment. A WireGuard client config is shown once. A Tailscale auth key and a Cloudflare tunnel token are not shown again. The `v2026.10.03` binary does not have those cards.

On `main`, Admin has a Database card that shows SQLite or Postgres, the URL with the password hidden, and Move. Move copies every table onto an empty target, writes `database_url`, and tells you to restart. The `v2026.10.03` binary does not have that card.

Traces are API-only. An admin reads and posts spans at `/api/v1/traces`. There is no traces screen. This is on `main`. It is not in the `v2026.10.03` binary.

On `main`, Settings → Access has the cluster-already-running card. Adopt Kubespray is the first form. Paste the kubeconfig there. That records a cluster that is already up. It does not add servers. Servers are added on Machines. Ubuntu autoinstall and MicroK8s stay behind that form. Talos stays on Machines. Install Ubuntu on the bare-metal row is the boot that puts Ubuntu on the machine. The `v2026.10.03` binary does not have this card. Install Ubuntu is on `main` and is not in that binary either.

## What the other screen files are

| File | What it draws |
| --- | --- |
| `environment_workflow.js` | The six guided steps. |
| `environment_progress.js` | The log while a deploy is running. |
| `environment_deploy_map.js` | The picture of those steps on Overview. |
| `environment_live_state.js` | The latest collector snapshot. |
| `environment_observe.js` | Logs and metrics for this environment. |
| `environment_components.js` | The OpenStack services the settings ask for, against what is installed. |
| `environment_discovery.js` | Servers and management ports a scan found, before you accept them. |
| `environment_terminal.js` | The shell card. |
| `environment_honeycomb.js` and `environment_space3d.js` | Two drawings of the same cloud. They read the snapshot. They do not change it. |
| `hosts.js` | A host list used from the fleet. |
| `tenant.js` | The tenant switcher in the top bar. |

The drawings are a view of data the API already returned. Orbiting one does not power a server.

## What you still do in the Genestack checkout

The page does not replace the files in `/opt/genestack` or `/etc/genestack`. You still edit inventory the way the Genestack manual describes. The console stores a copy of the settings, and a job renders that copy onto the deploy host before it runs `bin/`. Skyline, once the cloud is up, is the page for people using the cloud. This page is for the people who build it.
