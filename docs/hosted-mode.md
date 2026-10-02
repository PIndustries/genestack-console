# Connect a console to my.genestack.dev

The console runs on a dedicated Linux server you operate. Put that server on the same Ethernet as the bare metal, with no router between them, and leave it outside the cluster. It never joins the cluster. `https://my.genestack.dev` is the account portal in front of that console. It is how the Apple apps reach the console, and it is where the account holder manages the account and asks us for support.

The portal does not run a copy of this deployer. An environment, a job, a BMC secret, and the cluster stay on your console. On my.genestack.dev you manage the account and the link to a console you already installed.

A console can accept sign-in from that portal, or it can stand alone with local accounts.

## Signed in through my.genestack.dev

Use this when a Mac, iPhone, iPad, or Apple Watch should open a console you run:

### Authentication

- **OIDC/SSO Only**: Users sign in through the portal via OIDC. The portal's identity provider handles authentication and provisions users with tenant-scoped claims.
- **No Password Forms**: With portal sign-in, the console UI uses the portal account. Local password forms are hidden when OIDC is the only configured method.
- **Tenant Claims**: The portal sends `gsc_tenant_id` and `gsc_tenant_role` JWT claims that assign users to tenants with the matching role.

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

**Important**: When the console accepts portal sign-in, `redirect_url` must be `https://my.genestack.dev/api/v1/auth/oidc/callback`. That same-origin portal callback sets the `gsc_console` session cookie used by the Apple apps and `/go/{slug}`. The apps do not send `X-API-Key`. The console process may stay on loopback. The tunnel agent on your host forwards `/api/v1/*`, including the OIDC callback. A console already configured for `https://app.genestack.dev` can still mint `gsc_console` for that old issuer. New configs use `https://my.genestack.dev`.

### What the portal does

`https://my.genestack.dev` is outside this repository. It provides:

- **Account**: The account holder manages the account here.
- **Apple apps**: Mac, iPhone, iPad, and Apple Watch sign in here and are linked to a console you run.
- **Support**: The account holder can ask us for support from this account.
- **Tunnel**: `/go/{slug}` selects the console, and the tunnel on your host carries the session through your firewall.

The portal forwards the session. It does not store the environment, the job log, or the BMC secrets.

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

## Sign in on the console

For a console that operators open directly, with no portal in the path:

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

### Connected to my.genestack.dev
- The trust boundary for identity is the portal. The console checks the token and the claims. The portal owns the account.
- The console owns the environment. Portal-provisioned users have no local password unless an admin sets one.

### Direct console sign-in
- The Console owns the user database and password hashes (bcrypt).
- Static API keys in `config.yaml` grant platform-admin privileges; rotate them and keep the file mode `0600`.
- When enabling your own OIDC, users provisioned via SSO have no password hash and cannot use local login until an admin sets one.

## Connect or disconnect the portal

**Connect an existing console to my.genestack.dev**:
1. Enable OIDC with the portal issuer and credentials above.
2. People sign in through the portal. Existing local accounts match on username or email.
3. Static API keys remain valid for automation and break-glass access.

**Use the console on its own**:
1. Set `oidc.enabled: false`, or point `issuer_url` at your own identity provider.
2. Create local password accounts for operators: `docker compose exec console python -m app.cli create-user --username admin --password '<pw>' --platform-admin`
3. Password login works immediately.

## Related Documentation

- [Install Guide](install.md) — Binary install, docker compose, systemd setup
- [Architecture](architecture.md) — Console components and agent topology
- [API Reference](../API_REFERENCE.md) — Authentication endpoints and session flow

## Portal services

The account portal, Apple sign-in, support, and `/go/{slug}` routing are outside this repository. They are `https://my.genestack.dev`. This repository is the console you run.
