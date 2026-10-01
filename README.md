# Genestack Console

```bash
curl -fsSL https://get.genestack.dev/console.sh | bash
# then open http://127.0.0.1:8080/ui and follow Guided setup
```

`get.genestack.dev` is the live installer. Checkout install: [docs/install.md](docs/install.md).
Compiled Linux binaries are GitHub Release assets: [docs/releasing.md](docs/releasing.md).

Operator fleet control plane for managing [Genestack](https://github.com/rackerlabs/genestack) environments.

## Deployment Modes

The Console supports two deployment modes:

- **Hosted** — As a client of the portal at `https://my.genestack.dev`. Users authenticate via portal OIDC, and tenant provisioning is managed by the portal. See [docs/hosted-mode.md](docs/hosted-mode.md).
- **Self-hosted** — On-premise or isolated deployment. Local password accounts and optional OIDC with your own identity provider. See [docs/install.md](docs/install.md).

## What it does

- **Multi-environment management** — Create, configure, and deploy Genestack clouds from a single web UI
- **Agent-based operations** — Install lightweight agents on hosts; all operations run through them
- **SSH key management** — Auto-generated per-environment Ed25519 key pairs
- **Config push & deploy** — Versioned config documents pushed to deploy hosts, deployed with real-time progress
- **Bare metal provisioning** — PXE boot, DHCP, and Talos installation via agent sidecars
- **Hardware discovery** — Discover machines on your L2 network, register BMCs, provision bare metal

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

- **Console** (FastAPI + SQLite) — REST API, web UI, SSE event stream
- **Worker** (daemon process) — Long-running jobs: config push, deploy, agent install
- **Agent** (Docker container on target hosts) — WebSocket dial-out to console, receives commands
- **PXE sidecar** (optional) — dnsmasq + iPXE for bare metal provisioning

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
- `oidc` — Optional OIDC/SSO login. For hosted mode (portal at `https://my.genestack.dev`),
  enable with `issuer_url: https://my.genestack.dev` and `client_id: genestack-console`.
  See [docs/hosted-mode.md](docs/hosted-mode.md) for details.
- `hub.advertise_url` — URL that agents can reach (e.g., `https://console.example.com:8080`).
  **Required for installing agents on remote hosts**: the one-liner the portal
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
