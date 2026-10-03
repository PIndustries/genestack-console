"""Pydantic v2 request/response schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.services.env_field_guards import apply_environment_field_guards

RoleName = Literal["viewer", "operator", "admin"]
JobStatusName = Literal["queued", "running", "success", "failed"]

# Sentinel returned in place of stored kubeconfig data; sending it back on
# update means "leave the stored kubeconfig unchanged".
MASKED_KUBECONFIG_DATA = "***"

# Sentinel returned in place of stored SSH private key; sending it back on
# update means "leave the stored key unchanged".
MASKED_SSH_PRIVATE_KEY = "********"


# ---------------------------------------------------------------------------
# Auth / principal
# ---------------------------------------------------------------------------


class Principal(BaseModel):
    """Authenticated console user (API key, service key, or session token)."""

    username: str
    role: RoleName
    auth_method: str = "api_key"
    user_id: Optional[str] = None
    platform_admin: bool = False


# ---------------------------------------------------------------------------
# Tenants / accounts
# ---------------------------------------------------------------------------


class TenantCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    description: Optional[str] = None


class TenantUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=128)
    description: Optional[str] = None


class TenantRead(BaseModel):
    id: str
    name: str
    description: Optional[str] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class TenantMembershipRead(BaseModel):
    """One tenant the current user belongs to (used in login/whoami)."""

    id: str
    name: str
    role: RoleName


class MemberRead(BaseModel):
    user_id: str
    username: str
    role: RoleName


class MemberAdd(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    role: RoleName = "viewer"


class MembershipRef(BaseModel):
    tenant_id: str
    role: RoleName = "viewer"


class UserCreate(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1)
    platform_admin: bool = False
    memberships: list[MembershipRef] = Field(default_factory=list)


class UserRead(BaseModel):
    id: str
    username: str
    platform_admin: bool
    active: bool
    created_at: datetime
    tenants: list[TenantMembershipRead] = Field(default_factory=list)


class UserPasswordSet(BaseModel):
    password: str = Field(..., min_length=1)


class LoginRequest(BaseModel):
    username: str
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str = Field(..., min_length=1)


class LoginUser(BaseModel):
    username: str
    platform_admin: bool
    tenants: list[TenantMembershipRead] = Field(default_factory=list)


class LoginResponse(BaseModel):
    token: str
    expires_at: datetime
    refresh_token: str
    refresh_expires_at: datetime
    user: LoginUser
    # Same session, named the way an OAuth 2 token response names it.
    access_token: str
    token_type: str = "Bearer"
    expires_in: int
    scope: str = "console"


class TicketResponse(BaseModel):
    """Single-use ticket for browser WS/SSE connects (see app.services.tickets)."""

    ticket: str
    expires_in: int


# ---------------------------------------------------------------------------
# Environments
# ---------------------------------------------------------------------------


class EnvironmentBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    region: Optional[str] = None
    tier: Optional[str] = None
    description: Optional[str] = None
    kubeconfig_path: Optional[str] = None
    deployer_ssh_host: Optional[str] = None
    deployer_ssh_user: Optional[str] = None
    genestack_path: Optional[str] = None
    genestack_config_dir: Optional[str] = Field(
        default=None,
        description="The env's /etc/genestack equivalent on the deploy host",
    )
    kubeconfig_data: Optional[str] = Field(
        default=None,
        description="Kubeconfig file contents, encrypted at rest; alternative to kubeconfig_path",
    )
    dry_run: Optional[bool] = Field(
        default=None,
        description="Per-env dry-run override; null inherits global config.yaml dry_run",
    )
    metadata: Optional[dict[str, Any]] = Field(default_factory=dict, alias="metadata_json")

    model_config = ConfigDict(populate_by_name=True)


class EnvironmentCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    region: Optional[str] = None
    tier: Optional[str] = None
    description: Optional[str] = None
    kubeconfig_path: Optional[str] = None
    deployer_ssh_host: Optional[str] = None
    deployer_ssh_user: Optional[str] = None
    genestack_path: Optional[str] = None
    genestack_config_dir: Optional[str] = Field(
        default=None,
        description="The env's /etc/genestack equivalent on the deploy host",
    )
    kubeconfig_data: Optional[str] = Field(
        default=None,
        description="Kubeconfig file contents, encrypted at rest; alternative to kubeconfig_path",
    )
    dry_run: Optional[bool] = Field(
        default=None,
        description="Per-env dry-run override; null inherits global config.yaml dry_run",
    )
    metadata: Optional[dict[str, Any]] = Field(default_factory=dict, alias="metadata_json")
    tenant_id: Optional[str] = Field(
        default=None,
        description="Owning tenant; required for non-platform-admin creators",
    )

    @model_validator(mode="after")
    def _refuse_dangerous_fields(self) -> "EnvironmentCreate":
        cleaned = apply_environment_field_guards(
            deployer_ssh_user=self.deployer_ssh_user,
            deployer_ssh_host=self.deployer_ssh_host,
            kubeconfig_data=self.kubeconfig_data,
            kubeconfig_path=self.kubeconfig_path,
            genestack_path=self.genestack_path,
            genestack_config_dir=self.genestack_config_dir,
        )
        for key, value in cleaned.items():
            if key == "state_repo_path":
                continue
            setattr(self, key, value)
        return self

    model_config = ConfigDict(populate_by_name=True)


class EnvironmentUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=128)
    region: Optional[str] = None
    tier: Optional[str] = None
    description: Optional[str] = None
    kubeconfig_path: Optional[str] = None
    deployer_ssh_host: Optional[str] = None
    deployer_ssh_user: Optional[str] = None
    genestack_path: Optional[str] = None
    genestack_config_dir: Optional[str] = None
    state_repo_path: Optional[str] = Field(default=None, max_length=512)
    state_repo_remote: Optional[str] = Field(default=None, max_length=512)
    kubeconfig_data: Optional[str] = None
    dry_run: Optional[bool] = None
    metadata: Optional[dict[str, Any]] = Field(default=None, alias="metadata_json")

    @model_validator(mode="after")
    def _refuse_dangerous_fields(self) -> "EnvironmentUpdate":
        # Only fields the client sent. Assigning the others marks them set,
        # and a partial PATCH would then store None over the saved paths.
        provided = self.model_fields_set
        cleaned = apply_environment_field_guards(
            deployer_ssh_user=self.deployer_ssh_user,
            deployer_ssh_host=self.deployer_ssh_host,
            kubeconfig_data=self.kubeconfig_data,
            kubeconfig_path=self.kubeconfig_path,
            genestack_path=self.genestack_path,
            genestack_config_dir=self.genestack_config_dir,
            state_repo_path=self.state_repo_path,
        )
        for key, value in cleaned.items():
            if key in provided:
                setattr(self, key, value)
        return self

    model_config = ConfigDict(populate_by_name=True)


def _redact_url_creds(url: str | None) -> str | None:
    if not url:
        return url
    import re

    return re.sub(r"(://[^:/@]+:)[^@/]+(@)", r"\1***\2", url)


class EnvironmentRead(BaseModel):
    id: str
    name: str
    region: Optional[str] = None
    tier: Optional[str] = None
    description: Optional[str] = None
    kubeconfig_path: Optional[str] = None
    deployer_ssh_host: Optional[str] = None
    deployer_ssh_user: Optional[str] = None
    genestack_path: Optional[str] = None
    genestack_config_dir: Optional[str] = None
    state_repo_path: Optional[str] = None
    state_repo_remote: Optional[str] = None
    kubeconfig_data: Optional[str] = None
    dry_run: Optional[bool] = None
    ssh_public_key: Optional[str] = None
    ssh_key_generated: bool = False
    tenant_id: Optional[str] = None
    ovh_account_id: Optional[str] = None
    metadata: Optional[dict[str, Any]] = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)

    @classmethod
    def from_orm_env(cls, env: Any) -> "EnvironmentRead":
        return cls(
            id=env.id,
            name=env.name,
            region=env.region,
            tier=env.tier,
            description=env.description,
            kubeconfig_path=env.kubeconfig_path,
            deployer_ssh_host=env.deployer_ssh_host,
            deployer_ssh_user=env.deployer_ssh_user,
            genestack_path=env.genestack_path,
            genestack_config_dir=env.genestack_config_dir,
            state_repo_path=env.state_repo_path,
            state_repo_remote=_redact_url_creds(env.state_repo_remote),
            # Never leak the stored kubeconfig; masked when set, null when not
            kubeconfig_data=(MASKED_KUBECONFIG_DATA if env.kubeconfig_data else None),
            dry_run=env.dry_run,
            ssh_public_key=env.ssh_public_key,
            ssh_key_generated=bool(env.ssh_private_key_encrypted),
            tenant_id=env.tenant_id,
            ovh_account_id=env.ovh_account_id,
            metadata=env.metadata_json or {},
            created_at=env.created_at,
            updated_at=env.updated_at,
        )


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------


class JobCreate(BaseModel):
    operation: str = Field(..., min_length=1, max_length=128)
    params: dict[str, Any] = Field(default_factory=dict)
    environment_id: Optional[str] = Field(
        default=None,
        description="Target environment (optional for internal ops; also accepted on nested route)",
    )
    run_sync: bool = Field(
        default=False,
        description=(
            "If true, execute immediately in-process; by default jobs are queued "
            "for the worker so long-running deploys do not block the API"
        ),
    )


class JobRetry(BaseModel):
    run_sync: bool = Field(
        default=False,
        description=(
            "If true, execute immediately in-process; by default the retry is "
            "queued for the worker so long-running deploys do not block the API"
        ),
    )


class JobRead(BaseModel):
    id: str
    environment_id: Optional[str] = None
    operation: str
    params: Optional[dict[str, Any]] = None
    status: JobStatusName
    log_text: str = ""
    created_by: Optional[str] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    error: Optional[str] = None
    dry_run: Optional[bool] = None
    cancel_requested: bool = False
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------
# Operations catalog
# ---------------------------------------------------------------------------


class OperationParam(BaseModel):
    default: Any | None = None
    enum: list[Any] | None = None
    name: str
    required: bool = False
    description: str = ""
    type: str = "string"


class OperationSpec(BaseModel):
    id: str
    name: str
    description: str
    required_role: RoleName
    backend: Literal["ansible", "genestack", "internal", "baremetal", "agent"]
    params: list[OperationParam] = Field(default_factory=list)
    secret_params: tuple[str, ...] = Field(
        default_factory=tuple,
        description=(
            "Param names holding secrets (passwords/keys/tokens): scrubbed to '***' "
            "in the persisted job row and audit log; the real value is stored "
            "fernet-encrypted and only restored in memory at execution time"
        ),
    )
    handler: str
    mutating: bool = False
    timeout_seconds: Optional[int] = Field(
        default=None,
        description="Per-op command timeout; None uses the global jobs.timeout_seconds",
    )


# ---------------------------------------------------------------------------
# Agent channel
# ---------------------------------------------------------------------------


class AgentTokenCreate(BaseModel):
    name: str = Field(default="default", min_length=1, max_length=128)


class AgentTokenRead(BaseModel):
    """Returned ONCE at enrollment — the raw token is never stored or shown again."""

    agent_id: str
    environment_id: str
    name: str
    token: str
    hub_url: str
    instructions: str
    docker_run: str
    created_at: datetime
    pxe_config: Optional[dict[str, Any]] = None
    # Set once on enrollment when the WireGuard hub is on. The client
    # config includes the peer private key. Later reads omit it.
    wireguard: Optional[dict[str, Any]] = None


class AgentStatusEntry(BaseModel):
    """One agent of an environment: its credential plus live connection state."""

    agent_id: str
    name: Optional[str] = None
    hostname: Optional[str] = None
    version: Optional[str] = None
    connected: bool = False
    last_seen: Optional[datetime] = None
    pxe_config: Optional[dict[str, Any]] = None


class AgentStatusRead(BaseModel):
    environment_id: str
    enrolled: bool
    connected: bool
    agent_id: Optional[str] = None
    credential_name: Optional[str] = None
    hostname: Optional[str] = None
    version: Optional[str] = None
    last_seen: Optional[datetime] = None
    credential_created_at: Optional[datetime] = None
    # HA view: every agent of the env and how many are connected right now.
    agents: list[AgentStatusEntry] = Field(default_factory=list)
    connected_count: int = 0


# ---------------------------------------------------------------------------
# Discovery inbox
# ---------------------------------------------------------------------------


class DiscoveryClaimRequest(BaseModel):
    mac: str = Field(..., min_length=1, max_length=32)
    name: str = Field(..., min_length=1, max_length=128)
    roles: list[str] = Field(default_factory=list)


class DiscoveryBmcCredsRequest(BaseModel):
    bmc_id: str = Field(..., min_length=1, max_length=36)
    name: str = Field(..., min_length=1, max_length=128)
    username: str = Field(..., min_length=1, max_length=128)
    password: str = Field(..., min_length=1)


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


class AuditLogRead(BaseModel):
    id: int
    timestamp: datetime
    actor: str
    action: str
    resource_type: Optional[str] = None
    resource_id: Optional[str] = None
    environment_id: Optional[str] = None
    details: Optional[dict[str, Any]] = None
    success: bool

    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------
# Health / misc
# ---------------------------------------------------------------------------


class HealthResponse(BaseModel):
    status: str = "ok"
    version: str
    build: str = "dev"
    dry_run: bool
    genestack_root: str
    ansible_root: str
    uptime_seconds: float = 0


class MessageResponse(BaseModel):
    message: str
    detail: Optional[Any] = None


# ---------------------------------------------------------------------------
# Telemetry (snapshots / metrics / alerts)
# ---------------------------------------------------------------------------


ClusterHealthName = Literal["healthy", "degraded", "down", "unknown"]
AlertConditionName = Literal["node_not_ready", "pod_crashloop", "probe_failed", "service_down"]
AlertSeverityName = Literal["info", "warning", "critical"]
AlertEventStatusName = Literal["firing", "resolved"]


class ClusterSnapshotOut(BaseModel):
    id: int
    environment_id: str
    taken_at: datetime
    probe_ok: bool
    error: Optional[str] = None
    nodes: list[dict[str, Any]] = Field(default_factory=list)
    pods: list[dict[str, Any]] = Field(default_factory=list)
    helm: list[dict[str, Any]] = Field(default_factory=list)
    summary: dict[str, Any] = Field(default_factory=dict)
    health: ClusterHealthName = "unknown"

    model_config = ConfigDict(from_attributes=True)


class MetricSampleOut(BaseModel):
    id: int
    environment_id: str
    ts: datetime
    name: str
    labels: Optional[dict[str, Any]] = None
    value: float

    model_config = ConfigDict(from_attributes=True)


class AlertRuleIn(BaseModel):
    """Create/update payload for an alert rule."""

    environment_id: Optional[str] = Field(
        default=None, description="Target environment; null applies to all environments"
    )
    name: str = Field(..., min_length=1, max_length=128)
    condition: AlertConditionName
    threshold: float = 1.0
    severity: AlertSeverityName = "warning"
    enabled: bool = True
    webhook_url: Optional[str] = Field(default=None, max_length=512)
    channel_id: Optional[str] = Field(
        default=None, description="Saved notification channel on this console"
    )


class AlertRuleOut(BaseModel):
    id: int
    environment_id: Optional[str] = None
    name: str
    condition: AlertConditionName
    threshold: float
    severity: AlertSeverityName
    enabled: bool
    webhook_url: Optional[str] = None
    channel_id: Optional[str] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class AlertEventOut(BaseModel):
    id: int
    rule_id: int
    environment_id: str
    status: AlertEventStatusName
    fired_at: datetime
    resolved_at: Optional[datetime] = None
    acknowledged: bool
    details: Optional[dict[str, Any]] = None
    # Denormalized display fields, matching the SSE alert payloads so the UI
    # never has to render placeholders for rule/severity/environment.
    rule_name: Optional[str] = None
    severity: Optional[AlertSeverityName] = None
    environment_name: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------
# Git-backed Apps
# ---------------------------------------------------------------------------

AppTargetName = Literal["kubernetes", "openstack"]
AppBuildName = Literal[
    "auto",
    "manifests",
    "kustomize",
    "helm",
    "dockerfile",
    "heat",
    "terraform",
    "ansible",
]


class AppCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    repo_url: str = Field(min_length=12, max_length=512)
    branch: str = Field(default="main", min_length=1, max_length=128)
    root_path: Optional[str] = Field(default=None, max_length=240)
    target: AppTargetName
    build: AppBuildName = "auto"
    namespace: Optional[str] = Field(default=None, max_length=64)
    stack_name: Optional[str] = Field(default=None, max_length=64)
    deploy_token: Optional[str] = Field(default=None, max_length=256)
    poll_seconds: int = Field(default=0, ge=0, le=86400)


class AppUpdate(BaseModel):
    repo_url: Optional[str] = Field(default=None, min_length=12, max_length=512)
    branch: Optional[str] = Field(default=None, min_length=1, max_length=128)
    root_path: Optional[str] = Field(default=None, max_length=240)
    build: Optional[AppBuildName] = None
    namespace: Optional[str] = Field(default=None, max_length=64)
    stack_name: Optional[str] = Field(default=None, max_length=64)
    deploy_token: Optional[str] = Field(default=None, max_length=256)
    poll_seconds: Optional[int] = Field(default=None, ge=0, le=86400)


class AppRead(BaseModel):
    id: str
    environment_id: str
    name: str
    repo_url: str
    branch: str
    root_path: Optional[str] = None
    target: str
    build: str
    namespace: Optional[str] = None
    stack_name: Optional[str] = None
    webhook_id: str
    webhook_url: str
    has_deploy_token: bool = False
    last_sha: Optional[str] = None
    last_job_id: Optional[str] = None
    last_status: Optional[str] = None
    last_error: Optional[str] = None
    poll_seconds: int = 0
    created_by: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class AppCreated(BaseModel):
    app: AppRead
    webhook_url: str
    webhook_secret: str


class AppDeployBody(BaseModel):
    force: bool = False
