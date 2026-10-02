# Genestack Console

[Genestack](https://github.com/rackerlabs/genestack) is the project that installs OpenStack on Kubernetes. The scripts in that checkout build the cloud. The cluster is that cloud: the Kubernetes nodes, and the OpenStack services that run on them.

Genestack Console is a second program. The recommended place for it is a dedicated Linux server, on the same Ethernet network as the bare-metal machines, with no router between them. That is Layer 2. Clone Genestack onto that server and install the console beside the checkout. The console is the page you open in a browser to do the install from one place:

- save the settings for one cloud
- power the physical servers on and off
- give a server an IP address and a boot file while you are installing an operating system on it
- run the Genestack scripts and keep the log

That server is the deploy host. The name means the machine that performs the deploy. It sits just outside the cluster and never joins it. It is not a Kubernetes node and not an OpenStack compute node. Out of band means it reaches the servers on that same Ethernet network and through each server's management port, not through the cluster. A management port is the controller in the server that stays on when the main computer is off. If the cluster stops answering, this machine can still power the servers, hand out a boot file, and run the install scripts. The console, the saved settings, the job log, and the passwords for the server management ports all stay on it.

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

The deploy host and the servers are on the same Ethernet network, with no router between them. On that network the console answers DHCP. DHCP is the service that hands a machine an IP address when it asks. The console also serves a boot file. A server that is told to boot from the network downloads that file from the console and runs it. Both of those services run inside the console process. You do not set up a separate DHCP appliance for this.

Each server has two addresses you enter:

- The management port, often called the BMC, iLO, or iDRAC. It is a small controller in the server that stays on when the main computer is off. The console uses it to power the server and to request one network boot.
- The port on the same network as the deploy host. DHCP matches the MAC address of that port.

A server you have not selected boots from its own disk. The console leaves it alone.

When you install one server:

1. The console writes a boot file for that MAC and asks the management port for one network boot. The file is a small program that runs in memory, wipes the disks, and sends one report back to the console.
2. After that report, the console writes a Talos boot file for the same MAC and asks for one more network boot. Talos is the operating system Kubernetes runs on for this install.
3. The deploy job then runs the Genestack scripts from `/opt/genestack`: inventory, Kubernetes, then OpenStack.

A site on the far side of a firewall cannot hear the deploy host's DHCP. Put the console agent on a computer that is on that site's network. The agent opens a connection out to the console. DHCP and the boot files for those servers run on the agent.

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

- [docs/install.md](docs/install.md) — the install on Linux, and the laptop lab on a Mac or Windows.
- [docs/architecture.md](docs/architecture.md) — the processes on the deploy host, the boot sequence, and the job runner.
- [docs/modules.md](docs/modules.md) — where each operation lives, and how to add a module of your own.
- [docs/genestack-guide.md](docs/genestack-guide.md) — the same story, written as a chapter of the Genestack manual.
- [docs/hosted-mode.md](docs/hosted-mode.md) — connecting a console you already run to `https://my.genestack.dev`.

## API

REST API at `/api/v1/*`. Human catalog at `/docs` (same page as https://genestack.dev/docs). Live OpenAPI UI at `/swagger`. Auth via Bearer session or `X-API-Key`.

Key endpoints:
- `POST /api/v1/environments` — Create environment
- `GET /api/v1/environments/{id}/workflow` — Current step state
- `POST /api/v1/environments/{id}/servers/static` — Add server
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
