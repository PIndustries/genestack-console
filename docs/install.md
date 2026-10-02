# Install the Genestack Console

Genestack Console is the program that drives a Genestack install. Read [the README](../README.md) if you have not seen what it does yet.

Install it on a dedicated Linux server. That server holds two other trees: the Genestack checkout at `/opt/genestack`, and the Genestack settings at `/etc/genestack`. This guide calls that computer the deploy host.

Put the deploy host on the same Ethernet network as the bare-metal servers, with no router between them. That is Layer 2. Leave the deploy host outside the cluster. The cluster is the Kubernetes and OpenStack cloud those servers become. The deploy host never joins that cluster. It is not a Kubernetes node and not an OpenStack compute node. Out of band means it reaches the servers on that same Ethernet network and through each server's management port. A management port is the controller inside a server that stays on when the main computer is off. If the cluster stops answering, this machine can still power the servers and run the install.

The console listens on `127.0.0.1:8080` on that machine. You open the UI on the deploy host, or from a laptop with `ssh -L 8080:127.0.0.1:8080 <deploy-host>`.

A laptop is a lab copy of the same program. It is the right place to click through the UI. It is the wrong place to boot a rack of servers, because those servers have to be on a network with the machine that answers DHCP.

One command. A compiled binary.

```bash
curl -fsSL https://get.genestack.dev/console.sh | bash
```

`get.genestack.dev/console.sh` redirects to
`https://github.com/PIndustries/genestack-console/releases/latest/download/console.sh`.
The same redirect is on `genestack.dev`. The first login is a local password.
To connect that console to the account portal at `https://my.genestack.dev`, see [hosted-mode.md](hosted-mode.md). The portal is the account and the Apple apps. It is not this program.

| Host | Console | Local AIO VM (`--dev`) | Production metal |
|---|---|---|---|
| Linux x86_64 | Native ELF + systemd | QEMU/KVM | Yes — this is the fleet hub |
| WSL2 Ubuntu | Same Linux ELF | Only if nested KVM is on | No — laptop lab |
| macOS | Docker Desktop + launchd | QEMU/HVF | No — laptop lab |
| Windows Git Bash | Docker Desktop | No — use WSL2 | No |
| Windows PowerShell | Runs WSL2 installer (`console.ps1`) | Via WSL2 | No |

There is **no Win32 Console**. Windows is WSL2 (preferred) or Docker Desktop.

**Linux / WSL2:** downloads `genestack-console` (Nuitka onefile), writes config, and
starts API + worker under systemd. You do not get a source tree.

**macOS (dev laptop):** same one-liner. There is no macOS ELF — Docker Desktop
(or Colima) runs the published Linux image; launchd starts it at login.
Prefix is `~/genestack-console`. First boot still seeds tenant **demo** /
environment **walkthrough**. For a real local VM as well:

```bash
curl -fsSL https://get.genestack.dev/console.sh | bash -s -- --dev
```

That is `--with-aio-vm` on a Mac: QEMU + Hypervisor.framework, Ubuntu cloud
image (arm64 on Apple Silicon), SSH on `127.0.0.1:2222`. Needs Docker Desktop
running and `brew install qemu`. Not a production rack — a laptop lab.

**Windows:**

```powershell
wsl --install -d Ubuntu
wsl
curl -fsSL https://get.genestack.dev/console.sh | bash
```

Or from PowerShell (jumps into WSL2):

```powershell
irm https://get.genestack.dev/console.ps1 | iex
```

Git Bash + Docker Desktop also runs the hub (seeded walkthrough, no AIO VM).

Listen address is **config**, not a rebuild. First run writes
`server.host` / `server.port` in `/opt/genestack-console/config.yaml`
(default `127.0.0.1:8080`). To listen on every interface:

```yaml
server:
  host: 0.0.0.0
  port: 8090
```

Then `sudo systemctl restart genestack-console`. Same file for dry_run, keys,
PXE pool, WireGuard overlay, and everything else operators change on the box.

## Hub L2 vs agent L2 (both)

The Console on the box does PXE/DHCP when it sits on the same Layer-2 as
the machines. That is not replaced by agents.

A remote site behind a physical firewall cannot see that L2. Install an
agent there (`curl …/agent | bash`). On first adopt the hub:

1. Mints a WireGuard peer on `wg-gsc` (`10.67.67.0/24`, separate from
   any overlay you already run) and pushes it over the WebSocket the agent already
   opened *outbound*.
2. Pushes that agent's `pxe_config` so the agent runs DHCP/HTTP locally
   as the Console's L2 proxy.

Turn the overlay on in config (then restart):

```yaml
wireguard:
  enabled: true
  endpoint: "<hub-ip-or-name>:51820"   # UDP agents can reach through the firewall
```

```bash
ssh -L 8080:127.0.0.1:8080 <console-host>
```

Open `http://127.0.0.1:8080/ui`. Login is `admin` plus the password in
`/opt/genestack-console/ADMIN_CREDENTIALS.txt` (mode 600). Then Guided setup.

## What the operator gets

| Piece | Where it comes from |
|---|---|
| Console | One Linux binary (`GSC_BINARY_URL`) |
| Genestack cloud scripts | Public Apache-2.0 tarball, bind-mounted read-only |

The console binary is compiled. It is not unpacked as Python, a git checkout, or a
docker-save of source layers.

## Docker (optional)

Prefer the binary on the box. If you want a container instead, the same
compiled ELF is published as a `docker save` tarball — still no source:

```bash
curl -fsSL https://genestack.dev/releases/genestack-console-linux-amd64-docker.tar.gz \
  | gunzip | docker load
docker run --rm --name genestack-console \
  -p 127.0.0.1:8080:8080 \
  -v /opt/genestack-console:/opt/genestack-console \
  genestack-console:stable serve --host 0.0.0.0 --port 8080
```

Or let the installer load that image and write compose (no git checkout):

```bash
curl -fsSL https://get.genestack.dev/console.sh | bash -s -- --docker
```

PXE DHCP and boot-file HTTP run **inside the console process** (Python).
There is no PXE sidecar. Bare metal: run the binary on the box (needs the
provisioning NIC and privilege to bind UDP/67). A container can do the same
only with `--network host --cap-add NET_ADMIN`.

`https://get.genestack.dev/console.sh` redirects to the GitHub Release and the script fetches the compiled binary by default.

## Publishing the binary (maintainers)

From a Console checkout, with gcc and Python 3.12:

```bash
./scripts/compile-console.sh
```

That command writes the ELF under `dist/`. A tag `v*` on this repository
runs GitHub Actions and attaches `genestack-console-linux-amd64`,
`version.json`, `console.sh`, and `console.ps1` to the GitHub Release.
The installer script downloads the binary asset.

Developer rebuild from a checkout: `--from-source` (container, not the operator path).
