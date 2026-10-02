# Genestack Console

The program you run on the deploy host, next to a [Genestack](https://github.com/rackerlabs/genestack) checkout. It runs the bootstrap, Ansible, and Talos steps. The environment, the job log, and the BMC secrets stay on that host.

```bash
curl -fsSL https://get.genestack.dev/console.sh | bash
# then open http://127.0.0.1:8080/ui and follow Guided setup
```

`get.genestack.dev/console.sh` redirects to the current GitHub Release asset. The script installs the Linux binary under `/opt/genestack-console` and binds the UI to `127.0.0.1:8080`. Install details are in [docs/install.md](docs/install.md). How a release is cut is in [docs/releasing.md](docs/releasing.md).

Sign in with a local account. To let the Apple apps and the account page reach this console, connect it to `https://my.genestack.dev`. That portal does not run a copy of the console. See [docs/hosted-mode.md](docs/hosted-mode.md).

## What it does

- **Environments** — One cloud per environment: a lab, a rack, or a region. Jobs and credentials stay inside it.
- **Deploy jobs** — Push the saved config to the deploy host, then run the Genestack install scripts. The job log stays on the environment.
- **Agents** — A remote site dials out to the console. The agent does not accept inbound connections from the console.
- **Bare metal** — Each MAC has a next boot. Disk is the default. A selected machine is commissioned, then PXE'd once into Talos. DHCP and the boot files run in the console process when it is on that L2.
- **Accounts** — Local passwords, an API key, your own identity provider, or sign-in through `my.genestack.dev`.

## Quick start

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

## Architecture

- **Console** on the deploy host — FastAPI, the UI, and SQLite. The longer map is [docs/architecture.md](docs/architecture.md).
- **Worker** — Runs the jobs: config push, deploy, bare-metal boot.
- **Agent** — Dial-out WebSocket from a remote site. Serves PXE there when the console is not on that L2.
- **PXE** — In-process DHCP and boot-file HTTP. There is no PXE sidecar.

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
