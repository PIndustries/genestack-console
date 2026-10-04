# Genestack Console

[Genestack](https://github.com/rackerlabs/genestack) is the project that installs OpenStack on Kubernetes. The scripts in that checkout build the cloud. The cluster is that cloud: the Kubernetes nodes, and the OpenStack services that run on them.

Genestack Console is a second program. Install it on its own Linux server. Clone Genestack onto that server and install the console beside the checkout. The console is the page you open in a browser to do the install from one place:

- save the settings for one cloud
- power the physical servers on and off
- give a server an IP address and a boot file while you are installing an operating system on it
- run the Genestack scripts and keep the log

That server is the deploy host. The deploy host is the machine that performs the install. It stays just outside the cluster and never joins it. It is not a Kubernetes node and not an OpenStack compute node. You reach the servers from this machine, not through the cluster. If the cluster stops answering, you can still power the servers and run the install from here. The console, the saved settings, the job log, and the passwords you save there stay on that machine.

We recommend the deploy host be L2 with the servers you are installing. L2 means they are on one local network. On that network the console can give a server an IP address and a boot file itself. The section below says how that boot works.

When the deploy host cannot be L2 with a site, install the console agent on a computer that is. The agent gives out the addresses and the boot files at that site, and it connects out to the console. You still run the job from the console. One console often looks after several sites this way, such as more than one datacenter in the same private cloud.

```bash
curl -fsSL https://get.genestack.dev/console.sh | bash
# then open http://127.0.0.1:8080/ui and follow Guided setup
```

`get.genestack.dev/console.sh` redirects to the current GitHub Release asset. The script installs the Linux binary under `/opt/genestack-console` and binds the UI to `127.0.0.1:8080` on the deploy host. From your laptop, `ssh -L 8080:127.0.0.1:8080 <deploy-host>` and open the same URL. Install details are in [docs/install.md](docs/install.md). How a release is cut is in [docs/releasing.md](docs/releasing.md).

Sign in with a user created on that machine. `https://my.genestack.dev` is an account page you can connect later. It lets the Mac, iPhone, iPad, and Apple Watch apps reach this console, and it is where you manage that account. The cloud's settings stay on the deploy host. See [docs/hosted-mode.md](docs/hosted-mode.md).

## The three directories

| Path | What it is |
| --- | --- |
| `/opt/genestack` | The Genestack git checkout. The console runs the install scripts from here. |
| `/etc/genestack` | Inventory and the settings those scripts read. Genestack's `bootstrap.sh` creates this tree. |
| `/opt/genestack-console` | This program. It sits next to the Genestack checkout. It is not a folder inside it. |

An environment is one cloud: a lab, one rack, or one site. You create it in the UI. The settings and the job log for that cloud stay inside it. You point the environment at `/opt/genestack` and `/etc/genestack` on the deploy host.

## How a physical server gets an operating system

This is the path when the console installs the operating system. A management port is how that path powers the machine. It is optional. [Your first cluster](docs/first-cluster.md) covers Talos you installed from an ISO, and Ubuntu that is already on the machines. A hostname and an IP are enough for those two starts.

Where the deploy host is L2 with the servers, the console answers DHCP and serves the boot file on that network. DHCP is how a machine asks for an IP address. The boot file is the small program the network card downloads when the server is told to start from the network instead of from its disk. Both of those services run inside the console process. This is how a server gets Talos. Talos is installed from the network, and the console is the program that does it. You do not set up a separate DHCP server, or another program, to boot the machines.

Each server has two addresses you enter:

- The management port, often called the BMC, iLO, or iDRAC. It is a small controller in the server that stays on when the main computer is off. The console uses it to power the server and to request one network boot.
- The port on the L2 network. The console, or the agent at a remote site, matches the MAC address of that port.

A server you have not selected boots from its own disk. The console leaves it alone.

When you install one server:

1. The console writes a boot file for that MAC and asks the management port for one network boot. The file is a small program that runs in memory, wipes the disks, and sends one report back to the console.
2. After that report, the console writes a Talos boot file for the same MAC and asks for one more network boot. Talos is the operating system Kubernetes runs on for this install.
3. The deploy job then runs the Genestack scripts from `/opt/genestack`: inventory, Kubernetes, then OpenStack.

Where the deploy host cannot be L2 with the servers, the console agent does this job. Install it on a computer that is L2 with those servers. DHCP and the boot files for that site run on the agent. The agent connects out to the console, and you still start the job from the console.

Skyline is the OpenStack dashboard people use after the cloud is up. The console is the program the operator uses to build it.

## Run from source

Use this when you are changing the console. Installing Genestack uses the binary above, not this checkout.

```bash
git clone https://github.com/PIndustries/genestack-console.git
cd genestack-console

# Generate the config with a unique Fernet secret_key and rotated API keys.
# (The shipped config.yaml.example carries REPLACE_ME placeholders; the server
# treats them as unset and starts with the publicly-known dev credentials —
# fine on loopback, a hard startup error on any non-loopback bind.)
python3 -m app.cli make-config > config.yaml
# or, manually: cp config.yaml.example config.yaml and replace every
# REPLACE_ME value with generated secrets before `docker compose up`.

docker compose up -d   # serves http://localhost:8080

# Log in at http://localhost:8080/ui — create the first user in the container:
docker compose exec console python -m app.cli create-user --username admin --password '<pw>' --platform-admin
# API clients authenticate with an API key from config.yaml (X-API-Key header).
```

## Where to read next

- [docs/first-cluster.md](docs/first-cluster.md) — the console is up. Attach a cluster you already have, point at Talos from an ISO, or record Ubuntu that is already installed. A management port is optional.
- [docs/install.md](docs/install.md) — the install on Linux, and the laptop lab on a Mac or Windows.
- [docs/architecture.md](docs/architecture.md) — the processes on the deploy host, the boot sequence, and the job runner.
- [docs/modules.md](docs/modules.md) — where each operation lives, and how to add a module of your own.
- [docs/genestack-guide.md](docs/genestack-guide.md) — the same story, written as a chapter of the Genestack manual.
- [docs/program.md](docs/program.md), [docs/runtime.md](docs/runtime.md), [docs/jobs.md](docs/jobs.md), [docs/http.md](docs/http.md), and [docs/ui.md](docs/ui.md) — the two processes, how a job runs, every operation, the HTTP routes, and the web page.
- [docs/hosted-mode.md](docs/hosted-mode.md) — connecting a console you already run to `https://my.genestack.dev`.

## API

REST API at `/api/v1/*`. Human catalog at `/docs` (same page as https://genestack.dev/docs). Live OpenAPI UI at `/swagger`. Auth via Bearer session or `X-API-Key`.

Key endpoints:
- `POST /api/v1/environments` — Create environment
- `GET /api/v1/environments/{id}/workflow` — Current step state
- `POST /api/v1/environments/{id}/servers/static` — Add server
- `POST /api/v1/environments/{id}/servers/adopt` — Record that hosts already have an OS
- `PUT /api/v1/environments/{id}/config` — Save config
- `POST /api/v1/environments/{id}/jobs` — Run operations (push, deploy, etc.)

## Configuration

All config in `config.yaml`. See `config.yaml.example` for all options.

Key settings:
- `secret_key` — Encryption key for stored secrets (CHANGE FROM DEFAULT)
- `oidc` — Optional sign-in through `https://my.genestack.dev`, or through your own identity provider.
  For the portal, set `issuer_url: https://my.genestack.dev` and `client_id: genestack-console`.
  See [docs/hosted-mode.md](docs/hosted-mode.md). The portal is the account and the Apple connection. The console still runs on your host.
- `hub.advertise_url` — URL that agents can reach (e.g., `https://console.example.com:8080`).
  **Required for installing agents on remote hosts**: the one-liner the console
  shows (`curl <console>/agent | bash -s -- --hub …`) must contain a hub URL
  the *target* host can reach, which the console cannot derive from your own
  browser. With `https`, the agent dials `wss://`.
- `auth.api_keys` — Platform-admin API keys

## Operations

### Backups

The console database (`data/` by default) holds users, sessions, Fernet-encrypted
credentials, and agent token hashes. `scripts/backup-console.sh` takes an online
snapshot (SQLite `.backup` while the console is running, or `pg_dump` when
`database_url` is Postgres) **and copies `config.yaml` alongside it** — a database
restore without the Fernet `secret_key` from that config does not decrypt to the
same secrets. Backups are written mode 0600; treat them like credentials.

```bash
scripts/backup-console.sh /var/backups/genestack --keep 7
# Restore (SQLite): stop the stack, replace data/console.db with the backup
# copy (plus config.yaml if yours was lost), start the stack.
# Restore (Postgres): pg_restore -d console console.dump
```

The compose stack health-checks the API, the worker daemon, and the local-agent
(the agent retries the console-seeded token file for up to 60 s at boot), so
`docker compose ps` is the first place to look when something is wedged.
When more than one environment exists, set `agent.default_environment_id` in
`config.yaml` to pick which one gets the auto "local-agent" credential —
otherwise the console skips seeding rather than guessing a tenant.

## License

Apache-2.0
