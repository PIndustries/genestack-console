# Optional lab: console + all-in-one genestack dev VM

The **default community path** is the fleet hub only — see [install.md](install.md):

```bash
curl -fsSL https://genestack.dev/console.sh | bash
```

Then open Guided setup and deploy OVH Rise with Talos. `--with-aio-vm` stays a
local **dev/test** extra (not a production path — production deployments are
multi-node from real inventory).

## Quickstart (AIO lab)

With the AIO dev VM:

**Linux** (needs KVM — bare metal or a VM with nested virtualization):

```bash
curl -fsSL https://genestack.dev/console.sh | bash -s -- --with-aio-vm
```

**macOS** (Docker Desktop for the Console, QEMU/HVF for the VM — Apple Silicon
or Intel):

```bash
curl -fsSL https://genestack.dev/console.sh | bash -s -- --dev
```

`--dev` is the same flag as `--with-aio-vm`. Installs to `~/genestack-console`,
starts the hub in Docker, then launches an Ubuntu cloud VM (arm64 image on
Apple Silicon). SSH: `ssh -i ~/genestack-console/ssh/genestack-aio_key -p 2222 ubuntu@127.0.0.1`.
Needs `brew install qemu` if QEMU is missing.

Prefer to read the script before running it (recommended), or don't have a
stable URL yet? The git-clone alternative does exactly the same thing:

```bash
git clone --depth 1 https://github.com/PIndustries/genestack-console.git
sudo genestack-console/scripts/genestack-console.sh --with-aio-vm
```

When the script detects it is running from inside a repo checkout (a
`Containerfile` next to `scripts/`), it installs from that local source
instead of cloning.

## What the installer does, phase by phase

`scripts/genestack-console.sh` is idempotent — re-running it is always safe and never
regenerates secrets or recreates things that already exist.

1. **Preflight** — Linux: Ubuntu 22.04/24.04 (warns and continues
   elsewhere), x86_64, and — only with `--with-aio-vm` — that `/dev/kvm`
   exists and is accessible. macOS: Docker Desktop + HVF (`kern.hv_support`).
2. **Dependencies** — installs via apt only what is missing: `curl`, `jq`,
   `git`, `python3`, and a container engine (`docker.io` preferred; an existing podman
   install is used instead). With `--with-aio-vm` also `qemu-kvm`,
   `qemu-system-x86`, and `cloud-image-utils`. Uses `sudo` when not root;
   asks first when interactive.
3. **Source** — clones the repo (`--depth 1`, `GSC_REPO_URL` / `GSC_REPO_REF`)
   and copies the console tree to `<prefix>/src`, or reuses the local checkout
   when run from one. Also populates `/opt/genestack` (or `$GENESTACK_ROOT`)
   so the compose bind-mount at `/genestack` is not empty.
4. **Configuration** — writes `<prefix>/config.yaml` on first run only:
   freshly rotated API keys and Fernet `secret_key` (never the publicly known
   `dev-*` keys), `auth.dev_auto_login: false`, CORS restricted to
   `http://127.0.0.1:<port>` / `http://localhost:<port>`, and
   `hypervisor.roots` pointing at `<prefix>/vms`. Also generates
   `<prefix>/docker-compose.yml` (api + worker containers, loopback-only port
   mapping, `pid: host` so the hypervisor tier can see host QEMU VMs).
5. **Boot persistence** — generates a systemd unit under `<prefix>/systemd/`
   (based on `scripts/systemd/`, adapted for the container stack) and
   installs it as a system unit when root/sudo is available, otherwise as a
   `systemd --user` unit. The compose `restart: unless-stopped` policy is the
   fallback either way.
6. **Build & start** — `docker compose up -d --build` (or the podman
   equivalent: one image, two containers — api and job worker).
7. **Bootstrap** — waits for `GET /health`, creates the `admin` platform-admin
   user with a strong random password, prints it **once** in a boxed banner,
   and stores it mode-600 in `<prefix>/ADMIN_CREDENTIALS.txt`.
8. **AIO VM** (only with `--with-aio-vm`) — see below.

## Flags

| Flag | Default | Meaning |
|------|---------|---------|
| `--prefix DIR` | `/opt/genestack-console` | Install root |
| `--port N` | `8080` | Host port for the UI/API (loopback only) |
| `--with-aio-vm` | off | Also create + register the AIO dev VM |
| `--non-interactive` | off | Never prompt (for `curl \| bash` and CI) |
| `--uninstall` | — | Stop everything and remove the prefix |
| `--help` | — | Usage, including all `GSC_*` env knobs |

Useful environment knobs: `GSC_REPO_URL` (source repo), `GSC_AIO_CPUS` /
`GSC_AIO_MEM_MB` / `GSC_AIO_DISK` / `GSC_AIO_SSH_PORT` (VM shape),
`GSC_AIO_IMAGE_URL` / `GSC_AIO_IMAGE_PATH` (cloud image to download, or a
local base image for air-gapped installs). `GSC_SKIP_APT=1` and
`GSC_SKIP_ENGINE=1` are CI escape hatches used by
`scripts/test-install.sh`.

Set `GSC_WITH_PXE=1` at install time to add the optional PXE/DHCP sidecar
service (`pxe/Dockerfile`: dnsmasq + a static boot-asset HTTP server) to the
compose stack, for console-managed bare-metal Talos provisioning. The sidecar
runs with `network_mode: host` — DHCP is L2 broadcast, so the console host
must sit on the provisioning network — and serves what the console renders
into `<prefix>/pxe` (`dnsmasq.conf`, `boot.ipxe`, `assets/`). Default: off.

## Where things land

```
/opt/genestack-console/
├── src/                  # console source (build context)
├── config.yaml           # generated once; edit freely, never overwritten
├── docker-compose.yml    # regenerated each run
├── data/                 # sqlite DB + logs (container uid 1000)
├── pxe/                  # PXE sidecar inputs (dnsmasq.conf, boot.ipxe, assets/)
├── systemd/              # generated unit(s)
├── ADMIN_CREDENTIALS.txt # mode 600 — admin password + admin API key
├── images/               # cached cloud base image (with --with-aio-vm)
├── ssh/                  # generated VM ssh keypair
└── vms/                  # hypervisor root (console discovers VMs here)
    ├── genestack-aio.pid # pidfile at the TOP of the root (discovery scans root/*.pid)
    └── genestack-aio/
        ├── genestack-aio.qcow2   # overlay backed by the base image
        ├── seed.iso              # cloud-localds cloud-init seed
        ├── user-data, meta-data
        └── serial.log            # VM serial console (viewable in the UI)
```

## Logging in

Open `http://127.0.0.1:8080/ui` and sign in as `admin` with the password from
the boxed banner (also in `<prefix>/ADMIN_CREDENTIALS.txt`, mode 600). The
same file holds the rotated admin API key for `curl -H 'X-API-Key: …'` calls.

The console binds loopback only, by design — reach it from elsewhere via an
ssh tunnel (`ssh -L 8080:127.0.0.1:8080 host`), a VPN, or a reverse proxy;
do not flip `server.host` to `0.0.0.0` on a reachable interface.

## The AIO VM in the console

With `--with-aio-vm` the installer downloads an Ubuntu noble cloud image
(cached under `<prefix>/images/`), creates a qcow2 overlay and cloud-init
seed (hostname `genestack-aio`, your generated ssh key), and launches it with
`qemu-system-x86_64 -daemonize -pidfile … -serial file:…` under
`<prefix>/vms/` — the same layout the console's hypervisor tier discovers.
Because `<prefix>/vms` is the configured `hypervisor.roots`:

- the VM shows up on the console's **Hosts page** (`#/hosts`) with live
  status, CPU/RSS, start/stop/restart actions, and its serial log;
- the installer also **registers a `genestack-aio` environment** via the API
  (tier `dev`, `genestack_path: /opt/genestack`, ssh coordinates in metadata),
  so it appears on the Fleet and Environments pages.

Reach the VM directly with:

```bash
ssh -i /opt/genestack-console/ssh/genestack-aio_key -p 2222 ubuntu@127.0.0.1
```

Inside the VM, `sudo /usr/local/sbin/genestack-aio-setup.sh` runs genestack's
real getting-started path (clone with submodules, `bootstrap.sh`, kubespray
provider, inventory in `/etc/genestack`) — or drive the same steps from the
console's workflow buttons (`genestack.host_prepare`, `genestack.deploy`)
against the registered environment.

## Uninstall

```bash
sudo /opt/genestack-console/src/scripts/genestack-console.sh --uninstall
# or from a checkout:
sudo genestack-console/scripts/genestack-console.sh --prefix /opt/genestack-console --uninstall
```

Stops the AIO VM, brings the compose stack down, disables and removes the
systemd unit, and deletes the prefix (including the DB, cached images, and
credentials). Interactive runs ask for confirmation first.

## Hosting `genestack-console.sh` at a stable URL

The release workflow copies `scripts/genestack-console.sh` to the GitHub
Release as `console.sh`. The live one-liner follows that asset:

```shell
curl -fsSL https://get.genestack.dev/console.sh | bash
```

`get.genestack.dev` and `genestack.dev` redirect to
`https://github.com/PIndustries/genestack-console/releases/latest/download/console.sh`.
The script in the repository is the file those URLs serve.

## Verifying the installer

`scripts/test-install.sh` runs the installer against a scratch prefix
(`/tmp/gsc-test`, port `18099`) with `GSC_SKIP_APT=1 GSC_SKIP_ENGINE=1` and
asserts: idempotent re-run, generated config (`dev_auto_login: false`,
rotated keys, pinned CORS), compose/systemd artifacts, and uninstall.
Checks that need a container engine, a KVM-less host, or a real VM degrade
to explicit SKIP lines. Set `GSC_TEST_WITH_AIO=1` (plus `GSC_AIO_IMAGE_PATH`
to skip the download) to also launch and tear down a real AIO VM.
