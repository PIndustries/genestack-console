# Install the Genestack Console

Genestack Console is the program that drives a Genestack install. Read [the README](../README.md) if you have not seen what it does yet.

Install it on a dedicated Linux server. That server holds two other trees: the Genestack checkout at `/opt/genestack`, and the Genestack settings at `/etc/genestack`. This guide calls that computer the deploy host.

The deploy host stays outside the cluster. The cluster is the Kubernetes and OpenStack cloud those servers become. The deploy host never joins that cluster. It is not a Kubernetes node and not an OpenStack compute node. If the cluster stops answering, this machine can still power the servers and run the install.

We recommend the deploy host be L2 with the servers you are installing. L2 means they are on one local network. On that network this machine gives a server an IP address and a boot file. When the deploy host cannot be L2 with a site, install the console agent on a computer that is. The agent does that job at the site and connects out to the console. You still run the job from the console. One console often looks after several sites this way.

The console listens on `127.0.0.1:8080` on that machine. You open the UI on the deploy host, or from a laptop with `ssh -L 8080:127.0.0.1:8080 <deploy-host>`.

A laptop is a lab copy of the same program. It is the right place to click through the UI. It is the wrong place to boot a rack of servers. The machine that answers DHCP is the one on their network.

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

## When the deploy host is L2, and when you add an agent

On a network where the deploy host is L2 with the servers, the console
answers DHCP and serves the boot file itself. L2 means the deploy host
and those servers are on one local network. That is the recommendation
when you can do it.

When the deploy host cannot be L2 with a site, install an agent on a
computer that is (`curl …/agent | bash`). The agent gives out addresses
and boot files there, and it connects out to the console. You still
manage the environment from the console. That is the usual layout when
one private cloud has several sites.

The console can also reach a site on a path it runs on the deploy host.
These paths are off until you turn them on. Apply is a button or an API
call. The console does not start them when the process starts. If the
program is not installed, the status says the tool is missing and the
config file is still written. This is on `main`. The `v2026.10.03`
binary does not include it.

WireGuard is a VPN the console manages. The deploy host is the server,
on interface `wg-gsc`, network `10.67.67.0/24`. Pick a network that does
not overlap the hosts. Set `endpoint` to the UDP address a peer can
reach, then apply. The console writes a `wg-quick` file under the data
directory. On adopt, and from the environment's Reach card, the console
mints a peer and shows the client config once. Put that file on the peer
with `wg-quick`. The agent program still dials the console on the
WebSocket. It does not bring the VPN up for you.

Tailscale joins the deploy host to your tailnet. Save an auth key in
Admin, then Reach. It is not shown again. Apply runs `tailscale up`.
For each environment, save the tailnet address or MagicDNS name the
console should use. The console does not invent that address.

Cloudflare Tunnel runs `cloudflared` on the deploy host. Save the tunnel
token in Admin, then Reach. Apply starts `cloudflared tunnel run` and
remembers that process, so a later stop only stops that one. The token
lets this host run the connector. It is not the account site at
`my.genestack.dev`. For an environment, save the hostname that site
published, and a local port if the console should forward to it.

If the Kubernetes API is down, these paths do not make it answer. The
console can still power servers and run an install.

```yaml
wireguard:
  enabled: true
  endpoint: "<hub-ip-or-name>:51820"
tailscale:
  enabled: false
  hostname: genestack-console
cloudflare:
  enabled: false
```

```bash
ssh -L 8080:127.0.0.1:8080 <console-host>
```

Open `http://127.0.0.1:8080/ui`. Login is `admin` plus the password in
`/opt/genestack-console/ADMIN_CREDENTIALS.txt` (mode 600). Then Guided setup.
[Your first cluster](first-cluster.md) is the next page: a cluster you already
have, a new install, or a lab of virtual machines.

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
