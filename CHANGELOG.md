# Changelog

Each published build has a section here. The release workflow copies the matching section onto the GitHub Release. The version heading is the `VERSION` string, without the leading `v`.

The oldest GitHub Release is v2026.10.03. The source history starts two version numbers earlier. Those two sections are the commits. They are not downloadable tags.

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
