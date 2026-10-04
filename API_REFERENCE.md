# Genestack Console API Reference

Base URL: `http://localhost:8000` (or your deployment URL)

All authenticated endpoints require the `Authorization: Bearer <token>` header. Obtain a token via `POST /api/v1/auth/login` or configure a static API key in `config.yaml`.

---

## Health

### GET `/health`

Unauthenticated health check.

**Response:**
```json
{
  "status": "ok",
  "version": "0.1.0",
  "build": "dev",
  "dry_run": false,
  "genestack_root": "/opt/genestack",
  "ansible_root": "/opt/genestack/ansible"
}
```

**Example:**
```bash
curl http://localhost:8000/health
```

---

## OAuth 2

The console is an OAuth 2 authorization server. Discovery is `GET /.well-known/oauth-authorization-server`.

Two logins work at the same time. Each login is its own session. Refresh and revoke touch only the session whose token you sent.

The token endpoint takes `application/x-www-form-urlencoded`. The same field names work as JSON. There is no client secret. The only scope is `console`.

### POST `/api/v1/oauth/token`

**Password grant**

```bash
curl -X POST http://localhost:8000/api/v1/oauth/token \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "grant_type=password&username=admin&password=password&scope=console"
```

**Response:**
```json
{
  "access_token": "<opaque access token>",
  "token_type": "Bearer",
  "expires_in": 43200,
  "refresh_token": "<opaque refresh token>",
  "refresh_expires_in": 604800,
  "scope": "console"
}
```

Send `access_token` as `Authorization: Bearer <access_token>`.

**Refresh grant.** The access token is not required. The refresh token you send stops working. The other login stays.

```bash
curl -X POST http://localhost:8000/api/v1/oauth/token \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "grant_type=refresh_token&refresh_token=<opaque refresh token>"
```

**Authorization code grant.** `code` comes from the authorize redirect. `code_verifier` is the PKCE verifier that produced `code_challenge`. The code works once.

```bash
curl -X POST http://localhost:8000/api/v1/oauth/token \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "grant_type=authorization_code&code=<code>&redirect_uri=http://127.0.0.1:53682/callback&client_id=genestack-console&code_verifier=<verifier>"
```

A bad grant returns `400` with `{"error":"invalid_grant"}`. A client secret returns `401` with `{"error":"invalid_client"}`.

### GET `/api/v1/oauth/authorize`

Browser start for the authorization-code grant.

Query: `response_type=code`, `client_id`, `redirect_uri`, `state`, `code_challenge`, `code_challenge_method=S256`.

`redirect_uri` is this console's `/ui`, or a loopback address (`127.0.0.1`, `localhost`, or `::1`) for a program on the same machine. The person signs in on the page the console returns. The browser is then sent to `redirect_uri?code=...&state=...`.

### POST `/api/v1/oauth/revoke`

Drops that one session. An unknown token still returns `200`.

```bash
curl -X POST http://localhost:8000/api/v1/oauth/revoke \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "token=<access token or refresh token>"
```

### POST `/api/v1/oauth/introspect`

The caller must already be signed in. `{"active": false}` when the token is unknown, expired, or belongs to someone else.

```bash
curl -X POST http://localhost:8000/api/v1/oauth/introspect \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "token=$TOKEN"
```

---

## Reach

How this deploy host reaches an environment. `kind` is `wireguard`, `tailscale`, or `cloudflare`.

A platform admin reads and saves the hub. An operator attaches one environment. Auth keys, tunnel tokens, and WireGuard private keys are not returned. Creating a WireGuard peer returns `client_config` once.

`POST /api/v1/reach/{kind}/apply` writes the config and applies it. A missing `wg-quick`, `tailscale`, or `cloudflared` sets `status` to `tool_missing`. The console does not apply on startup.

This is on `main`. The `v2026.10.03` binary does not include these routes.

```bash
curl -X PUT http://localhost:8000/api/v1/reach/wireguard \
  -H "X-API-Key: $KEY" \
  -H "Content-Type: application/json" \
  -d '{"enabled":true,"endpoint":"203.0.113.10:51820"}'
```

---

## Database

`GET /api/v1/database` returns the engine, `sqlite` or `postgresql`, and the URL with the password replaced by `***`. Platform admin.

`POST /api/v1/database/move` copies every table onto an empty SQLite or Postgres database, writes `database_url`, and leaves the running process on the old database. Restart the console after. The target must have no rows. The password is not returned.

This is on `main`. The `v2026.10.03` binary does not include these routes.

```bash
curl -X POST http://localhost:8000/api/v1/database/move \
  -H "X-API-Key: $KEY" \
  -H "Content-Type: application/json" \
  -d '{"target_url":"sqlite:///./data/console-next.db"}'
```

---

## Traces

`GET /api/v1/traces` returns recent spans, newest last. `POST /api/v1/traces` appends one span. Admin only. The buffer is in memory. A restart clears it. The payload has no tokens.

This is on `main`. The `v2026.10.03` binary does not include these routes.

```bash
curl http://localhost:8000/api/v1/traces \
  -H "X-API-Key: $KEY"
```

---

## Auth

`POST /api/v1/auth/login` and `POST /api/v1/auth/refresh` issue the same session for a client that already calls them. Prefer the OAuth 2 token endpoint above.

### POST `/api/v1/auth/login`

Exchange username/password for a session token.

**Request body:**
```json
{
  "username": "admin",
  "password": "password"
}
```

The bearer and the refresh token are opaque strings. A second login returns a second pair. Send the bearer as `Authorization: Bearer <token>`.

**Response:**
```json
{
  "token": "<opaque session token>",
  "access_token": "<opaque session token>",
  "token_type": "Bearer",
  "expires_in": 43200,
  "expires_at": "2025-01-15T12:00:00Z",
  "refresh_token": "<opaque refresh token>",
  "refresh_expires_at": "2025-01-22T00:00:00Z",
  "scope": "console",
  "user": {
    "username": "admin",
    "platform_admin": true,
    "tenants": []
  }
}
```

**Example:**
```bash
curl -X POST http://localhost:8000/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username": "admin", "password": "password"}'
```

---

### POST `/api/v1/auth/refresh`

Exchange one login's refresh token for a new bearer and a new refresh token. The bearer is not required. The refresh token you send stops working. Another login for the same person is left alone. An unknown, expired, or reused refresh token is `401`.

**Request body:**
```json
{
  "refresh_token": "<opaque refresh token>"
}
```

**Response:** the same shape as login.

**Example:**
```bash
curl -X POST http://localhost:8000/api/v1/auth/refresh \
  -H "Content-Type: application/json" \
  -d '{"refresh_token": "<opaque refresh token>"}'
```

---

### POST `/api/v1/auth/logout`

Invalidate the caller's session token. No-op for static API keys.

**Headers:** `Authorization: Bearer <token>`

**Response:**
```json
{
  "message": "logged out"
}
```

**Example:**
```bash
curl -X POST http://localhost:8000/api/v1/auth/logout \
  -H "Authorization: Bearer $TOKEN"
```

---

### POST `/api/v1/auth/ticket`

Exchange the caller's credential for a single-use WS/SSE ticket. Used by browser realtime endpoints (`/stream`, `/terminal`) that cannot set auth headers.

**Headers:** `Authorization: Bearer <token>`

**Response:**
```json
{
  "ticket": "abc123...",
  "expires_in": 60
}
```

**Example:**
```bash
curl -X POST http://localhost:8000/api/v1/auth/ticket \
  -H "Authorization: Bearer $TOKEN"
```

---

### GET `/api/v1/auth/whoami`

Return identity, role, and tenants for the current credential.

**Headers:** `Authorization: Bearer <token>`

**Response:**
```json
{
  "key_name": "admin",
  "role": "admin",
  "auth_method": "session_token",
  "user_id": "550e8400-e29b-41d4-a716-446655440000",
  "platform_admin": true,
  "tenants": []
}
```

**Example:**
```bash
curl http://localhost:8000/api/v1/auth/whoami \
  -H "Authorization: Bearer $TOKEN"
```

---

### GET `/api/v1/auth/methods`

Advertise available login methods (unauthenticated).

**Response:**
```json
{
  "local": true,
  "oidc": false,
  "oidc_label": "SSO"
}
```

**Example:**
```bash
curl http://localhost:8000/api/v1/auth/methods
```

---

### GET `/api/v1/auth/oidc/login`

Redirect to the OIDC provider's authorize URL. Requires OIDC to be enabled in `config.yaml`.

**Response:** HTTP 302 redirect to the provider.

**Example:**
```bash
curl -I http://localhost:8000/api/v1/auth/oidc/login
```

---

### GET `/api/v1/auth/oidc/callback`

Complete OIDC flow and redirect the browser to `/ui#token=...&refresh=...`. The fragment is not sent back to the server.

**Query params:** `code`, `state`

**Response:** HTTP 302 redirect to `/ui#token=...&refresh=...`

---

## Tenants

### GET `/api/v1/tenants`

List tenants. Platform admins see all; others see only their own.

**Headers:** `Authorization: Bearer <token>`

**Response:** `list[TenantRead]`
```json
[
  {
    "id": "tenant-1",
    "name": "production",
    "description": "Production cluster",
    "created_at": "2025-01-01T00:00:00Z"
  }
]
```

**Example:**
```bash
curl http://localhost:8000/api/v1/tenants \
  -H "Authorization: Bearer $TOKEN"
```

---

### POST `/api/v1/tenants`

Create a new tenant. Requires platform admin.

**Headers:** `Authorization: Bearer <token>`

**Request body:**
```json
{
  "name": "staging",
  "description": "Staging environment"
}
```

**Response:** `TenantRead` (201 Created)

**Example:**
```bash
curl -X POST http://localhost:8000/api/v1/tenants \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name": "staging", "description": "Staging environment"}'
```

---

### GET `/api/v1/tenants/{tenant_id}`

Get a single tenant.

**Headers:** `Authorization: Bearer <token>`

**Response:** `TenantRead`

---

### PATCH `/api/v1/tenants/{tenant_id}`

Update a tenant. Requires tenant admin.

**Headers:** `Authorization: Bearer <token>`

**Request body:**
```json
{
  "description": "Updated description"
}
```

**Response:** `TenantRead`

---

### DELETE `/api/v1/tenants/{tenant_id}`

Delete a tenant. Requires platform admin.

**Response:** 204 No Content

---

## Memberships

### GET `/api/v1/tenants/{tenant_id}/members`

List members of a tenant. Requires tenant admin.

**Response:** `list[MemberRead]`
```json
[
  {
    "user_id": "user-1",
    "username": "alice",
    "role": "operator"
  }
]
```

---

### POST `/api/v1/tenants/{tenant_id}/members`

Add a member to a tenant. Requires tenant admin.

**Request body:**
```json
{
  "username": "bob",
  "role": "viewer"
}
```

**Response:** `MemberRead` (201 Created)

---

### DELETE `/api/v1/tenants/{tenant_id}/members/{user_id}`

Remove a member from a tenant. Requires tenant admin.

**Response:** 204 No Content

---

## Users

### GET `/api/v1/users`

List all users. Requires platform admin.

**Response:** `list[UserRead]`
```json
[
  {
    "id": "user-1",
    "username": "alice",
    "platform_admin": false,
    "active": true,
    "created_at": "2025-01-01T00:00:00Z",
    "tenants": []
  }
]
```

---

### POST `/api/v1/users`

Create a user. Requires platform admin.

**Request body:**
```json
{
  "username": "bob",
  "password": "secretpassword",
  "platform_admin": false,
  "memberships": [
    {
      "tenant_id": "tenant-1",
      "role": "operator"
    }
  ]
}
```

**Response:** `UserRead` (201 Created)

---

### POST `/api/v1/users/{username}/password`

Reset a user's password. Platform admins can reset anyone; users can reset their own.

**Request body:**
```json
{
  "password": "newpassword"
}
```

**Response:** 204 No Content

---

### DELETE `/api/v1/users/{username}`

Delete a user. Requires platform admin. Cannot delete your own account.

**Response:** 204 No Content

---

## Environments

### GET `/api/v1/environments`

List environments. Platform admins see all; others see only environments in their tenants.

**Headers:** `Authorization: Bearer <token>`

**Response:** `list[EnvironmentRead]`

**Example:**
```bash
curl http://localhost:8000/api/v1/environments \
  -H "Authorization: Bearer $TOKEN"
```

---

### POST `/api/v1/environments`

Create a new environment. Requires operator. Non-platform-admin creators must provide `tenant_id`.

**Headers:** `Authorization: Bearer <token>`

**Request body:**
```json
{
  "name": "prod-cluster",
  "region": "us-east-1",
  "tier": "production",
  "description": "Main production cluster",
  "tenant_id": "tenant-1"
}
```

**Response:** `EnvironmentRead` (201 Created)

**Example:**
```bash
curl -X POST http://localhost:8000/api/v1/environments \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name": "prod-cluster", "region": "us-east-1", "tier": "production"}'
```

---

### GET `/api/v1/environments/{environment_id}`

Get a single environment.

**Response:** `EnvironmentRead`

---

### GET `/api/v1/environments/{environment_id}/inventory`

Get the Ansible inventory for an environment.

**Response:**
```json
{
  "all": {
    "children": ["controllers", "workers", "nfs_servers"]
  }
}
```

---

### PATCH `/api/v1/environments/{environment_id}`

Update an environment. Requires operator. Secret fields are encrypted at rest.

**Request body:**
```json
{
  "description": "Updated description",
  "deployer_ssh_host": "deployer.example.com",
  "deployer_ssh_user": "deployer"
}
```

**Response:** `EnvironmentRead`

**Example:**
```bash
curl -X PATCH http://localhost:8000/api/v1/environments/env-1 \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"deployer_ssh_host": "deployer.example.com"}'
```

---

### DELETE `/api/v1/environments/{environment_id}`

Delete an environment and all its jobs. Requires admin.

**Response:** 204 No Content

---

### GET `/api/v1/environments/{environment_id}/ssh-key/public`

Get the environment's SSH public key and fingerprint.

**Response:**
```json
{
  "public_key": "ssh-ed25519 AAAA...",
  "has_key": true,
  "fingerprint": "SHA256:abc123..."
}
```

---

### POST `/api/v1/environments/{environment_id}/ssh-key/regenerate`

Generate a new SSH key pair for the environment. Requires operator.

**Response:**
```json
{
  "public_key": "ssh-ed25519 AAAA...",
  "fingerprint": "SHA256:abc123...",
  "message": "SSH key pair regenerated"
}
```

---

### GET `/api/v1/environments/{environment_id}/ssh-key/private`

Download the decrypted private key. Requires operator.

**Response:**
```json
{
  "private_key": "-----BEGIN OPENSSH PRIVATE KEY-----...",
  "public_key": "ssh-ed25519 AAAA..."
}
```

---

## Environment Config

All config endpoints are scoped to an environment.

### GET `/api/v1/environments/{environment_id}/config`

Get the current config document (secrets masked).

**Response:**
```json
{
  "version": 3,
  "yaml": "controllers:\n  - controller1\n...",
  "created_by": "admin",
  "created_at": "2025-01-15T10:00:00Z"
}
```

**Example:**
```bash
curl http://localhost:8000/api/v1/environments/env-1/config \
  -H "Authorization: Bearer $TOKEN"
```

---

### PUT `/api/v1/environments/{environment_id}/config`

Set or update the config document. Creates a new version. Requires operator.

**Request body:**
```json
{
  "yaml_text": "controllers:\n  - controller1\n  - controller2\nworkers:\n  - worker1\n  - worker2\n"
}
```

**Response:**
```json
{
  "version": 4,
  "yaml": "...",
  "created_by": "admin",
  "created_at": "2025-01-15T10:05:00Z",
  "warnings": []
}
```

**Example:**
```bash
curl -X PUT http://localhost:8000/api/v1/environments/env-1/config \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"yaml_text": "controllers:\n  - controller1\n"}'
```

---

### GET `/api/v1/environments/{environment_id}/config/versions`

List all config versions (history).

**Response:**
```json
[
  {
    "version": 3,
    "created_by": "admin",
    "created_at": "2025-01-15T10:00:00Z"
  },
  {
    "version": 2,
    "created_by": "admin",
    "created_at": "2025-01-14T08:00:00Z"
  }
]
```

---

### GET `/api/v1/environments/{environment_id}/config/versions/{version}`

Get a specific config version.

**Response:**
```json
{
  "version": 2,
  "yaml": "...",
  "created_by": "admin",
  "created_at": "2025-01-14T08:00:00Z"
}
```

---

### GET `/api/v1/environments/{environment_id}/config/render`

Preview rendered config-dir-relative files for the current document (secrets masked).

**Response:**
```json
{
  "version": 3,
  "files": {
    "inventory.yaml": "...",
    "group_vars/all.yml": "..."
  }
}
```

---

## Servers

Server management for an environment. These routes are for hosts you already have an address for. A hostname and an IP are enough. A management port is optional. Talos that is already installed, such as from an ISO, is the job `genestack.talos.bootstrap`. Recording an operating system that is already there is `POST /servers/adopt`. Installing Talos from the network is the bare-metal path: the console answers DHCP and serves the boot file.

### GET `/api/v1/environments/{environment_id}/servers`

List the servers saved for this environment.

**Response:**
```json
{
  "count": 3,
  "ovh_bound": false,
  "servers": [
    {
      "system_id": null,
      "hostname": "controller1",
      "ip": "10.0.0.1",
      "roles": ["controller"],
      "assigned": true,
      "source": "static",
      "adopt": ""
    }
  ]
}
```

---

### POST `/api/v1/environments/{environment_id}/servers/static`

Add or update a host by address. Creates a new config version. Requires operator.

**Request body:**
```json
{
  "hostname": "worker1",
  "ip": "10.0.0.10",
  "ssh_user": "ubuntu",
  "ssh_auth_method": "publickey",
  "roles": ["worker"]
}
```

**Response:**
```json
{
  "version": 5,
  "warnings": [],
  "server": {
    "system_id": null,
    "hostname": "worker1",
    "ip": "10.0.0.10",
    "ssh_user": "ubuntu",
    "ssh_auth_method": "publickey",
    "roles": ["worker"],
    "source": "static",
    "assigned": true
  }
}
```

**Example:**
```bash
curl -X POST http://localhost:8000/api/v1/environments/env-1/servers/static \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"hostname": "worker1", "ip": "10.0.0.10", "roles": ["worker"]}'
```

---

### POST `/api/v1/environments/{environment_id}/servers/adopt`

Record that the named hosts already have an operating system, or clear that record. Creates a new config version when the mark changes. Requires operator. This does not reboot, install, or start a playbook.

`adopt` is `kubespray` or an empty string. Empty removes the mark. A later `POST /servers/static` for the same hostname keeps it.

**Request body:**
```json
{
  "hostnames": ["worker1", "worker2"],
  "adopt": "kubespray"
}
```

**Response:**
```json
{
  "version": 6,
  "warnings": [],
  "adopt": "kubespray"
}
```

---

### POST `/api/v1/environments/{environment_id}/servers/remove`

Remove a server from the config. Creates a new config version. Requires operator.

**Request body:**
```json
{
  "hostname": "worker1"
}
```

**Response:**
```json
{
  "version": 6,
  "warnings": [],
  "removed": "worker1"
}
```

---

## Notifications

Credentials for alerts from this console. An admin saves them. They stay on this machine. A viewer can list the channel name and kind. Webhook URLs, Resend API keys, and Twilio auth tokens are not returned after save.

Kinds are `slack`, `discord`, and `teams` (a webhook URL), `resend` (`api_key`, `from_email`, `to`), and `twilio` (`account_sid`, `auth_token`, `from_number`, `to_number`).

`GET /api/v1/notify/channels` lists them. `POST /api/v1/notify/channels` creates one (admin). `PATCH /api/v1/notify/channels/{id}` changes the name, the enabled flag, or replaces `config` (admin). `DELETE /api/v1/notify/channels/{id}` removes it and clears `channel_id` on rules that used it (admin).

An alert rule accepts `channel_id`. An unknown id is rejected. An empty string clears it. The rule's own webhook URL still fires.

---

## Operations

### GET `/api/v1/operations`

List operations the caller is allowed to see based on their role.

**Response:** `list[OperationSpec]`
```json
[
  {
    "id": "deploy",
    "name": "Deploy Environment",
    "description": "Run full environment deployment",
    "required_role": "operator",
    "backend": "ansible",
    "params": [],
    "mutating": true,
    "handler": "app.operations:deploy"
  }
]
```

**Example:**
```bash
curl http://localhost:8000/api/v1/operations \
  -H "Authorization: Bearer $TOKEN"
```

---

## Jobs

### GET `/api/v1/jobs`

List jobs. Supports filtering by `environment_id` and `status`.

**Query params:**
- `environment_id` — filter by environment
- `status` — filter by status (`queued`, `running`, `success`, `failed`)
- `limit` — max results (default 50, max 500)

**Response:** `list[JobRead]`

**Example:**
```bash
curl "http://localhost:8000/api/v1/jobs?environment_id=env-1&limit=20" \
  -H "Authorization: Bearer $TOKEN"
```

---

### GET `/api/v1/jobs/{job_id}`

Get a single job with full details and log output.

**Response:** `JobRead`
```json
{
  "id": "job-1",
  "environment_id": "env-1",
  "operation": "deploy",
  "params": {},
  "status": "success",
  "log_text": "[2025-01-15] Starting deploy...\n[2025-01-15] Deploy complete.\n",
  "created_by": "admin",
  "started_at": "2025-01-15T10:00:00Z",
  "finished_at": "2025-01-15T10:05:00Z",
  "error": null,
  "cancel_requested": false,
  "created_at": "2025-01-15T10:00:00Z"
}
```

---

### POST `/api/v1/environments/{environment_id}/jobs`

Create a job for a specific environment. Requires operator.

**Request body:**
```json
{
  "operation": "deploy",
  "params": {
    "force": false
  },
  "run_sync": false
}
```

**Response:** `JobRead` (201 Created)

**Example:**
```bash
curl -X POST http://localhost:8000/api/v1/environments/env-1/jobs \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"operation": "deploy", "params": {}}'
```

---

### POST `/api/v1/jobs`

Create a job not bound to an environment (e.g. `internal.health`). Requires operator.

**Request body:**
```json
{
  "operation": "internal.health",
  "environment_id": "env-1",
  "params": {},
  "run_sync": true
}
```

**Response:** `JobRead` (201 Created)

---

### POST `/api/v1/jobs/{job_id}/retry`

Create a new job with the same operation/params as an existing one. The source job is left untouched.

**Request body:** (optional)
```json
{
  "run_sync": true
}
```

**Response:** `JobRead` (201 Created)

**Example:**
```bash
curl -X POST http://localhost:8000/api/v1/jobs/job-1/retry \
  -H "Authorization: Bearer $TOKEN"
```

---

### POST `/api/v1/jobs/{job_id}/cancel`

Cancel a queued or running job. Queued jobs are failed immediately; running jobs stop at the next command boundary.

**Response:** `JobRead`

**Example:**
```bash
curl -X POST http://localhost:8000/api/v1/jobs/job-1/cancel \
  -H "Authorization: Bearer $TOKEN"
```

---

## Agents

### GET `/agent`

Serve the agent install script. Unauthenticated, returns `agent/install.sh` as plain text.

**Example:**
```bash
curl http://localhost:8000/agent
```

---

### POST `/api/v1/environments/{environment_id}/agent/token`

Enroll an agent: issue a one-time enrollment token. Raw token shown once, hash stored. Requires admin on the environment.

**Request body:**
```json
{
  "name": "agent-1"
}
```

**Response:**
```json
{
  "agent_id": "cred-1",
  "environment_id": "env-1",
  "name": "agent-1",
  "token": "raw-secret-token-once",
  "hub_url": "ws://localhost:8000/api/v1/agents/connect",
  "instructions": "curl -fsSL http://localhost:8000/agent | bash -s -- --hub ws://localhost:8000/api/v1/agents/connect --token raw-secret-token-once",
  "docker_run": "docker run -d --name gsc-agent ...",
  "created_at": "2025-01-15T10:00:00Z"
}
```

**Example:**
```bash
curl -X POST http://localhost:8000/api/v1/environments/env-1/agent/token \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name": "agent-1"}'
```

---

### GET `/api/v1/environments/{environment_id}/agent/status`

Get enrollment and connection status for agents in the environment.

**Response:**
```json
{
  "environment_id": "env-1",
  "enrolled": true,
  "connected": true,
  "agent_id": "cred-1",
  "credential_name": "agent-1",
  "hostname": "worker1",
  "version": "0.1.0",
  "last_seen": "2025-01-15T10:05:00Z",
  "agents": [
    {
      "agent_id": "cred-1",
      "name": "agent-1",
      "hostname": "worker1",
      "connected": true,
      "last_seen": "2025-01-15T10:05:00Z"
    }
  ],
  "connected_count": 1
}
```

---

### GET `/api/v1/environments/{environment_id}/agents`

List all enrolled agents with their PXE config.

**Response:**
```json
[
  {
    "agent_id": "cred-1",
    "name": "agent-1",
    "hostname": "worker1",
    "version": "0.1.0",
    "connected": true,
    "last_seen": "2025-01-15T10:05:00Z",
    "created_at": "2025-01-15T10:00:00Z",
    "pxe_config": null
  }
]
```

---

### PATCH `/api/v1/environments/{environment_id}/agent/pxe-config`

Set or clear PXE config on an agent credential.

**Request body:**
```json
{
  "agent_id": "cred-1",
  "pxe_config": {
    "server": "10.0.0.1",
    "filename": "ipxe.bin"
  }
}
```

Send `"pxe_config": null` to clear.

**Response:**
```json
{
  "agent_id": "cred-1",
  "name": "agent-1",
  "pxe_config": {
    "server": "10.0.0.1",
    "filename": "ipxe.bin"
  }
}
```

---

### WebSocket `/api/v1/agents/connect?token=<token>`

Agent WebSocket endpoint. Agents authenticate via challenge/proof handshake using their enrollment token.

**Protocol:**
1. Server sends: `{"type": "challenge", "nonce": "..."}`
2. Agent sends: `{"type": "proof", "hmac": "<HMAC-SHA256(raw_token, nonce)>"}`
3. On success: `{"type": "welcome", "agent_id": "..."}`
4. Agent sends: `{"type": "hello", "hostname": "...", "version": "...", "caps": [...]}`
5. Agent sends: `{"type": "heartbeat"}` periodically

---

## Fleet

### GET `/api/v1/fleet`

Fleet status board: tenants and visible environments with step states.

**Response:** Full fleet state object with per-environment status.

**Example:**
```bash
curl http://localhost:8000/api/v1/fleet \
  -H "Authorization: Bearer $TOKEN"
```

---

### GET `/api/v1/fleet/live`

One row per visible environment with its latest snapshot's health summary.

**Response:**
```json
[
  {
    "environment_id": "env-1",
    "name": "prod-cluster",
    "region": "us-east-1",
    "tier": "production",
    "health": "healthy",
    "probe_ok": true,
    "error": null,
    "taken_at": "2025-01-15T10:05:00Z",
    "summary": {},
    "drifted": false
  }
]
```

---

## State

### GET `/api/v1/environments/{environment_id}/state`

Latest cluster snapshot. Returns 404 until the first probe lands.

**Response:** `ClusterSnapshotOut`
```json
{
  "id": 1,
  "environment_id": "env-1",
  "taken_at": "2025-01-15T10:05:00Z",
  "probe_ok": true,
  "error": null,
  "nodes": [],
  "pods": [],
  "helm": [],
  "summary": {},
  "health": "healthy"
}
```

---

### GET `/api/v1/environments/{environment_id}/state/history`

Snapshot history from the last N hours.

**Query params:**
- `limit` — max results (default 50, max 500)
- `hours` — time window in hours (default 24)

**Response:** `list[ClusterSnapshotOut]`

---

### GET `/api/v1/environments/{environment_id}/drift`

Per-artifact config drift check.

**Response:**
```json
{
  "environment_id": "env-1",
  "drifted": false,
  "checked_at": "2025-01-15T10:05:00Z",
  "artifacts": [
    {
      "artifact": "inventory.yaml",
      "status": "ok",
      "expected_sha256": "abc123...",
      "actual_sha256": "abc123..."
    }
  ]
}
```

---

## Genestack

### GET `/api/v1/genestack/scripts`

List available install scripts from the Genestack root.

**Response:**
```json
{
  "genestack_root": "/opt/genestack",
  "count": 3,
  "scripts": [...]
}
```

---

### GET `/api/v1/genestack/components`

List desired OpenStack components from `openstack-components.yaml`.

**Response:** Component configuration object.

---

### GET `/api/v1/genestack/operations`

Alias endpoint pointing to `GET /api/v1/operations`.

**Response:**
```json
{
  "message": "Use GET /api/v1/operations for the full catalog",
  "count": 25
}
```

---

## Misc Endpoints

### GET `/`

Redirects to `/ui` (307).

---

### GET `/api`

Service info endpoint.

**Response:**
```json
{
  "service": "genestack-console",
  "version": "0.1.0",
  "ui": "/ui",
  "docs": "/docs",
  "health": "/health",
  "dry_run": false
}
```

---

## Authentication Summary

| Method | Header / Param | Use case |
|--------|---------------|----------|
| Session token | `Authorization: Bearer <token>` | Browser UI, CLI scripts |
| API key | `Authorization: Bearer <key>` | Service-to-service, CI/CD |
| Ticket | `?ticket=<ticket>` | Browser WebSocket/SSE |

Tokens are obtained via `POST /api/v1/auth/login` or configured as static keys in `config.yaml`. Tickets are short-lived credentials obtained via `POST /api/v1/auth/ticket`.
