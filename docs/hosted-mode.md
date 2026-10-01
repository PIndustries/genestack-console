# Hosted Console Mode

The Genestack Console supports two deployment modes: **hosted** and **self-hosted**.

## Hosted Mode (my.genestack.dev Portal)

When running as a hosted client of the portal at `https://my.genestack.dev`:

### Authentication

- **OIDC/SSO Only**: Users sign in through the portal via OIDC. The portal's identity provider handles authentication and provisions users with tenant-scoped claims.
- **No Password Forms**: The hosted Console UI relies entirely on portal-managed identity. Local password login forms are hidden when OIDC is the only configured method.
- **Tenant Claims**: The portal sends `gsc_tenant_id` and `gsc_tenant_role` JWT claims that automatically assign users to tenants with appropriate roles (implemented in PR #9).

### Configuration

Edit `config.yaml`:

```yaml
oidc:
  enabled: true
  issuer_url: "https://my.genestack.dev"
  client_id: "genestack-console"
  client_secret: "<obtain-from-portal-admin>"
  redirect_url: "https://my.genestack.dev/api/v1/auth/oidc/callback"
  default_tenant: ""  # Claims provide tenant; this is fallback only
  default_role: viewer
  label: "Portal SSO"

cors:
  allow_origins:
    - "https://my.genestack.dev"
```

**Important**: For hosted mode, `redirect_url` **must** be `https://my.genestack.dev/api/v1/auth/oidc/callback`. That same-origin portal callback is what enables the `gsc_console` session cookie used by Mac/iOS and `/go/{slug}` (no `X-API-Key`). The console process may stay on loopback; the tunnel agent forwards `/api/v1/*` including the OIDC callback. A console already configured for `https://app.genestack.dev` can still mint `gsc_console` for that old issuer. New configs use `https://my.genestack.dev`.

### Portal Services

The portal (`https://my.genestack.dev`) provides additional services that complement the Console:

- **Tunnel Agent**: Handles `/go/{slug}` shortlinks and routing through firewalls
- **Origin Forwarding**: Cookie mutation and ticket minting for cross-origin requests

Those services are the hosted portal at `https://my.genestack.dev`, not this Console codebase. The Console acts as a client of that portal.

### Network Topology

```
User / Mac app
    ↓  /go/{slug}  (sets gsc_site)
my.genestack.dev (portal)  ──tunnel──►  console on loopback
    ↓  /api/v1/auth/oidc/login?native=1
my.genestack.dev/oauth/authorize  (portal IdP, iss=https://my.genestack.dev)
    ↓  redirect_uri=https://my.genestack.dev/api/v1/auth/oidc/callback
console callback  →  Set-Cookie: gsc_console=…  →  /ui
    ↓  GET /api/v1/auth/whoami  (cookie only, no X-API-Key)
200 identity
```

## Self-Hosted Mode

For on-premise or isolated deployments:

### Authentication

- **Password Login Always Available**: Local username/password accounts work out of the box via `POST /api/v1/auth/login`.
- **Static API Keys**: Platform-admin break-glass credentials defined in `config.yaml` under `auth.api_keys`.
- **Optional OIDC**: You may enable OIDC with your own identity provider (Keycloak, Auth0, Okta, etc.) while keeping password login as a fallback.

### Configuration

Default (password-only):

```yaml
auth:
  api_keys:
    gsc-admin-REPLACE_ME: admin
    gsc-operator-REPLACE_ME: operator
  session_ttl_hours: 12

oidc:
  enabled: false  # Password login is always available
```

With your own OIDC provider:

```yaml
oidc:
  enabled: true
  issuer_url: "https://your-idp.example.com"
  client_id: "your-console-client"
  client_secret: "<your-secret>"
  redirect_url: "http://10.0.1.100:8080/api/v1/auth/oidc/callback"
  default_tenant: "default"
  default_role: viewer
  label: "Corporate SSO"
```

Local password accounts remain available alongside OIDC. The Console never disables password login.

## Configuration Flags

**There are no `password_login` or `service_key_login` flags.** These authentication methods are always available:

- **Password login**: `POST /api/v1/auth/login` always works when a user has a password hash.
- **API keys**: `X-API-Key` header authentication always works per `auth.api_keys` in `config.yaml`.
- **OIDC/SSO**: Optional, enabled via `oidc.enabled: true` and a non-empty `oidc.issuer_url`.

## Testing Authentication Methods

Query available methods (unauthenticated):

```bash
curl http://localhost:8080/api/v1/auth/methods
```

Response:

```json
{
  "local": true,
  "oidc": true,
  "oidc_label": "Portal SSO"
}
```

- `local: true` means password/API-key login is available
- `oidc: true` means OIDC is configured and active

The web UI reads this endpoint at boot and shows/hides the SSO button accordingly.

## Security Notes

### Hosted Mode
- Trust boundary is at the portal. The Console validates JWT signatures and claims but does not manage user identity.
- All session credentials flow through OIDC; no local passwords are created for portal-provisioned users (unless explicitly set by an admin).

### Self-Hosted Mode
- The Console owns the user database and password hashes (bcrypt).
- Static API keys in `config.yaml` grant platform-admin privileges; rotate them and keep the file mode `0600`.
- When enabling your own OIDC, users provisioned via SSO have no password hash and cannot use local login until an admin sets one.

## Migration Path

**Self-hosted → Hosted**:
1. Enable OIDC with portal issuer and credentials
2. Users log in via portal; existing local accounts are matched by username/email
3. Static API keys remain valid for automation/break-glass

**Hosted → Self-hosted**:
1. Set `oidc.enabled: false` or point `issuer_url` at your own IdP
2. Create local password accounts for operators: `docker compose exec console python -m app.cli create-user --username admin --password '<pw>' --platform-admin`
3. Password login works immediately

## Related Documentation

- [Install Guide](install.md) — Binary install, docker compose, systemd setup
- [Architecture](architecture.md) — Console components and agent topology
- [API Reference](../API_REFERENCE.md) — Authentication endpoints and session flow

## Portal Services (External)

Tunnel agent, Origin forwarding, and `/go/{slug}` routing are **not in this repository**. They are part of the hosted portal infrastructure at `https://my.genestack.dev`.

For portal service implementation details, see:
- Hosted portal — `https://my.genestack.dev`
