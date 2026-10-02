"""SQLAlchemy 2.0 ORM models."""

from __future__ import annotations

import enum
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.db import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return str(uuid4())


class JobStatus(str, enum.Enum):
    queued = "queued"
    running = "running"
    success = "success"
    failed = "failed"


class UserRole(str, enum.Enum):
    viewer = "viewer"
    operator = "operator"
    admin = "admin"


class Tenant(Base):
    """A tenancy boundary: owns environments, has users via memberships."""

    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    name: Mapped[str] = mapped_column(
        String(128), unique=True, nullable=False, index=True
    )
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )


class Environment(Base):
    """A Genestack deployment / fleet target."""

    __tablename__ = "environments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    name: Mapped[str] = mapped_column(
        String(128), unique=True, nullable=False, index=True
    )
    region: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    tier: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True
    )  # e.g. prod/lab/dev
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    maas_url: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    # Encrypted at rest (fernet: prefix); legacy plaintext values still decrypt
    maas_api_key_encrypted: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # OVH per-environment consumer key, encrypted at rest (fernet: prefix).
    # This authorises THIS environment's operator to list their OVH dedicated
    # servers. The app credentials it was created under live on the OvhAccount
    # row referenced by ovh_account_id (one OVH account per console admin).
    ovh_consumer_key_encrypted: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True
    )
    ovh_account_id: Mapped[Optional[str]] = mapped_column(
        String(36), nullable=True, index=True
    )

    kubeconfig_path: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    deployer_ssh_host: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    deployer_ssh_user: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    genestack_path: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    # The env's /etc/genestack equivalent on the deploy host
    genestack_config_dir: Mapped[Optional[str]] = mapped_column(
        String(512), nullable=True
    )
    # Git-backed state export: local checkout of the genestack repo where
    # rendered state is written under state/<env-name>/ and committed.
    state_repo_path: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    # Remote URL to push state commits to (None = commit only, no push).
    state_repo_remote: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    # Kubeconfig file contents, encrypted at rest (fernet:); alternative to kubeconfig_path
    kubeconfig_data: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Per-environment SSH key pair for host access. Private key is fernet-encrypted at rest.
    # Generated automatically on environment creation; user may regenerate.
    ssh_private_key_encrypted: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True
    )
    ssh_public_key: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # None = inherit global settings.dry_run
    dry_run: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)

    metadata_json: Mapped[Optional[dict[str, Any]]] = mapped_column(
        "metadata", JSON, nullable=True, default=dict
    )

    # Owning tenant; NULL only for rows predating the backfill in init_db
    tenant_id: Mapped[Optional[str]] = mapped_column(
        String(36),
        ForeignKey("tenants.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    jobs: Mapped[list["Job"]] = relationship(
        back_populates="environment", cascade="all, delete-orphan"
    )


class OvhAccount(Base):
    """A stored OVHcloud application credential set (one per OVH account).

    Platform-admin managed. The app key identifies the console to OVH; the
    app secret is fernet-encrypted at rest. The (read-only) consumer key is
    stored here too — one per OVH account, created in the admin UI — and
    environments bind to an account via ``Environment.ovh_account_id`` to
    reuse it for server listing.
    """

    __tablename__ = "ovh_accounts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    name: Mapped[str] = mapped_column(
        String(128), unique=True, nullable=False, index=True
    )
    endpoint: Mapped[str] = mapped_column(String(512), nullable=False)
    app_key: Mapped[str] = mapped_column(String(256), nullable=False)
    app_secret_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    # Consumer key (fernet-encrypted), approved via its validation URL.
    # Connect requests list + BYOI reinstall + IPMI; environments bind to
    # the account and no longer hold their own key.
    consumer_key_encrypted: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )


class HardwareAccount(Base):
    """Terraform bare-metal credentials for a cloud (Rackspace, AWS, Azure, GCP).

    OVH stays on ``ovh_accounts`` (provider API, not Terraform). Secrets are
    a fernet-encrypted JSON object; APIs never return the plaintext.

    Tenant-scoped: accounts without a tenant_id are platform-admin only (legacy
    migration path); new accounts always require a tenant.
    """

    __tablename__ = "hardware_accounts"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "kind", "name", name="uq_hardware_accounts_tenant_kind_name"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    region: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    credentials_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    tenant_id: Mapped[Optional[str]] = mapped_column(
        String(36),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )


class TerraformState(Base):
    """Last-known terraform.tfstate for an environment + Hardware account.

    Terraform still runs against the on-disk work dir. After each run the
    file is copied here (fernet-encrypted) so a database restore can write
    it back. Disk wins when both exist.
    """

    __tablename__ = "terraform_states"
    __table_args__ = (
        UniqueConstraint(
            "environment_id", "account_id", name="uq_terraform_states_env_account"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    environment_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("environments.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    account_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("hardware_accounts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    state_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    serial: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    lineage: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )


class Job(Base):
    """An asynchronous (or sync local) operation execution record."""

    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    environment_id: Mapped[Optional[str]] = mapped_column(
        String(36),
        ForeignKey("environments.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    operation: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    # params holds the scrubbed copy: catalog-marked secret params are "***".
    params: Mapped[Optional[dict[str, Any]]] = mapped_column(
        JSON, nullable=True, default=dict
    )
    # Real values of the scrubbed secret params, fernet-encrypted; merged back
    # into the in-memory params at execution time (never serialized to APIs).
    secret_params: Mapped[Optional[dict[str, str]]] = mapped_column(
        JSON, nullable=True, default=None
    )
    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus), default=JobStatus.queued, nullable=False, index=True
    )
    log_text: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_by: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # True when the job only rehearsed (dry-run); None = not dry-run-relevant
    dry_run: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    # Best-effort cancellation: set via the cancel API for a running job; the
    # worker polls it between commands and stops the job.
    cancel_requested: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    environment: Mapped[Optional["Environment"]] = relationship(back_populates="jobs")


class EnvMutex(Base):
    """Per-environment mutual-exclusion row for mutating job execution.

    One row while a mutating job for the environment is in flight
    (claimed -> running). Acquired atomically via a PRIMARY KEY insert so
    two racing submitters/claimants cannot both enter the critical section —
    the second INSERT fails with an IntegrityError instead of relying on a
    plain SELECT-then-INSERT (TOCTOU). Released when the job reaches a
    terminal status; stale rows are swept at startup recovery.
    """

    __tablename__ = "env_mutexes"

    environment_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("environments.id", ondelete="CASCADE"), primary_key=True
    )
    job_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False
    )
    acquired_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )


class AuditLog(Base):
    """Immutable audit trail for console actions."""

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False, index=True
    )
    actor: Mapped[str] = mapped_column(String(128), nullable=False)
    action: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    resource_type: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    resource_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    environment_id: Mapped[Optional[str]] = mapped_column(
        String(36), nullable=True, index=True
    )
    details: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    success: Mapped[bool] = mapped_column(default=True, nullable=False)


class User(Base):
    """Local user account; authenticates with a password, gets session tokens."""

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    username: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False, index=True
    )
    role: Mapped[UserRole] = mapped_column(
        Enum(UserRole), default=UserRole.viewer, nullable=False
    )
    password_hash: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Platform admins bypass tenancy (break-glass / bootstrap)
    platform_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    api_key_hint: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    active: Mapped[bool] = mapped_column(default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )


class Membership(Base):
    """A user's role within one tenant."""

    __tablename__ = "memberships"
    __table_args__ = (
        UniqueConstraint("user_id", "tenant_id", name="uq_membership_user_tenant"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    tenant_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role: Mapped[UserRole] = mapped_column(
        Enum(UserRole), default=UserRole.viewer, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )


class SessionToken(Base):
    """Opaque bearer token issued at login; no refresh tokens."""

    __tablename__ = "session_tokens"

    token: Mapped[str] = mapped_column(String(128), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )


class EnvConfigVersion(Base):
    """Versioned flat YAML config document per environment (used by config push)."""

    __tablename__ = "env_config_versions"
    __table_args__ = (
        UniqueConstraint("environment_id", "version", name="uq_env_config_version"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    environment_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("environments.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    yaml_text: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )


class ClusterSnapshot(Base):
    """Point-in-time probe of one environment's cluster state."""

    __tablename__ = "cluster_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    environment_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("environments.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    taken_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False, index=True
    )
    probe_ok: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # [{name, ready, roles, kubelet_version}]
    nodes: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, default=list, nullable=False
    )
    # [{ns, name, phase, ready, restarts, node, waiting_reason}]
    pods: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, default=list, nullable=False
    )
    # [{name, ns, status, chart, version}]
    helm: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, default=list, nullable=False
    )
    # {nodes_ready, nodes_total, pods_running, pods_pending, pods_failed, crashlooping}
    summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    # One of: healthy, degraded, down, unknown
    health: Mapped[str] = mapped_column(String(16), default="unknown", nullable=False)


class ConfigDrift(Base):
    """Latest drift-check result for one rendered config artifact of an environment.

    One row per (environment_id, artifact), updated in place by the collector
    on every probe — it always reflects the most recent check, never history.
    """

    __tablename__ = "config_drift"
    __table_args__ = (
        UniqueConstraint(
            "environment_id", "artifact", name="uq_config_drift_env_artifact"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    environment_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("environments.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    checked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    # Config-dir-relative rendered path, e.g. "openstack-components.yaml"
    artifact: Mapped[str] = mapped_column(String(256), nullable=False)
    # One of: match, drift, missing, unknown
    status: Mapped[str] = mapped_column(String(16), default="unknown", nullable=False)
    expected_sha256: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    actual_sha256: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class MetricSample(Base):
    """A single named metric observation for an environment."""

    __tablename__ = "metric_samples"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    environment_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("environments.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    labels: Mapped[Optional[dict[str, Any]]] = mapped_column(
        JSON, nullable=True, default=dict
    )
    value: Mapped[float] = mapped_column(Float, nullable=False)


class AlertRule(Base):
    """Condition evaluated against snapshots/metrics; fires alert events."""

    __tablename__ = "alert_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # NULL = rule applies to all environments
    environment_id: Mapped[Optional[str]] = mapped_column(
        String(36),
        ForeignKey("environments.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    # One of: node_not_ready, pod_crashloop, probe_failed, service_down
    condition: Mapped[str] = mapped_column(String(32), nullable=False)
    threshold: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    # One of: info, warning, critical
    severity: Mapped[str] = mapped_column(String(16), default="warning", nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    webhook_url: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )


class HostVM(Base):
    """A QEMU virtual machine on the console host.

    These are the raw qemu-system processes that run genestack lab/dev/AIO
    nodes (no libvirt, no management scripts) — discovered from pidfiles
    under the configured hypervisor roots plus a /proc scan. Rows are never
    deleted by discovery: a VM that disappears simply reports running=False.
    """

    __tablename__ = "host_vms"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    name: Mapped[str] = mapped_column(
        String(128), unique=True, nullable=False, index=True
    )
    workdir: Mapped[str] = mapped_column(String(512), nullable=False)
    pidfile_path: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    serial_log_path: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    # Full argv captured at discovery — the command used to (re)start the VM.
    cmdline: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    # One of: discovered, manual
    source: Mapped[str] = mapped_column(
        String(16), default="discovered", nullable=False
    )
    vnc_host: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    vnc_port: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    novnc_port: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )


class BaremetalNode(Base):
    """A bare-metal server the console provisions directly (no MAAS).

    BMC credentials drive Redfish power/boot control. PXE is served in-process
    from data_dir/pxe (disk, then commission, then one-shot Talos). The node
    then lands in the env config doc servers section (source "baremetal") for
    the talos bootstrap flow. ``bmc_password`` is fernet-encrypted at rest like
    the other stored
    secrets. ``expected_ip`` is the IP reserved for the node from the PXE
    pool at provision time.
    """

    __tablename__ = "baremetal_nodes"
    __table_args__ = (
        UniqueConstraint("environment_id", "name", name="uq_baremetal_node_env_name"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    environment_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("environments.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    bmc_host: Mapped[str] = mapped_column(String(256), nullable=False)
    bmc_username: Mapped[str] = mapped_column(String(128), nullable=False)
    # Encrypted at rest (fernet: prefix)
    bmc_password: Mapped[str] = mapped_column(Text, nullable=False)
    pxe_mac: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    expected_ip: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    # One of: registered, booting, talos-ready, failed
    state: Mapped[str] = mapped_column(String(32), default="registered", nullable=False)
    # Next PXE profile for this MAC: commission, talos, or disk.
    # disk is the safe default — a stray PXE boot does not wipe anything.
    next_boot: Mapped[str] = mapped_column(String(16), default="disk", nullable=False)
    # new, commissioning, commissioned, talos, fresh-maintenance, installed, failed
    boot_stage: Mapped[str] = mapped_column(String(32), default="new", nullable=False)
    commission_token: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    commission_report: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    boot_log: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    wiped_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    talos_served_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_seen: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )


class DiscoveredNode(Base):
    """A PXE/DHCP sighting reported by the environment's agent (discovery inbox).

    Upserted by MAC on every ``pxe_request`` event: a re-sighting only bumps
    ``last_seen`` (and refreshes ip/hostname). ``state`` moves from
    ``discovered`` to ``claimed`` when an operator assigns the node a name and
    roles via the discovery claim endpoint.
    """

    __tablename__ = "discovered_nodes"
    __table_args__ = (
        UniqueConstraint("environment_id", "mac", name="uq_discovered_node_env_mac"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    environment_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("environments.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    mac: Mapped[str] = mapped_column(String(32), nullable=False)
    ip: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    hostname: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    # One of: discovered, claimed
    state: Mapped[str] = mapped_column(String(16), default="discovered", nullable=False)
    first_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    last_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )


class DiscoveredBmc(Base):
    """A Redfish BMC find reported by the environment's agent (discovery inbox).

    Upserted by IP on every ``bmc_found`` event: a re-sighting only bumps
    ``last_seen`` (and refreshes vendor/model). ``state`` moves from ``new``
    to ``registered`` when an operator attaches credentials via the discovery
    bmc-creds endpoint (which creates/updates the linked BaremetalNode).
    """

    __tablename__ = "discovered_bmcs"
    __table_args__ = (
        UniqueConstraint("environment_id", "ip", name="uq_discovered_bmc_env_ip"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    environment_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("environments.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    ip: Mapped[str] = mapped_column(String(64), nullable=False)
    vendor: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    model: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    # One of: new, registered
    state: Mapped[str] = mapped_column(String(16), default="new", nullable=False)
    first_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    last_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )


class AgentCredential(Base):
    """Enrollment credential for an environment's console agent.

    Only the sha256 of the raw token is stored; the raw token (``gsca_...``)
    is shown once at creation and used by the agent for the WebSocket
    challenge/proof handshake. ``last_seen``/``hostname``/``version`` are
    refreshed by the hub as frames arrive; live connection state (websocket,
    pending commands) is in-memory only — see app.services.agents.AgentRegistry.

    An environment may hold several credentials (HA agents): uniqueness is
    (environment_id, name) — creating with an existing name replaces that
    credential only.
    """

    __tablename__ = "agent_credentials"
    __table_args__ = (
        UniqueConstraint("environment_id", "name", name="uq_agent_credential_env_name"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    environment_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("environments.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(128), default="default", nullable=False)
    # sha256 hexdigest of the raw token — the raw token is never stored
    token_hash: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False, index=True
    )
    last_seen: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    version: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    hostname: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    # JSON: {interface, range_start, range_end, gateway, dns, next_server,
    #        http_port, image_url} — the PXE network this agent serves.
    pxe_config: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)


class AgentCommand(Base):
    """A durable command/file-write request routed to an env's console agent.

    Cross-process agent execution: live agent WebSockets live in the API
    process, but the callers that need agent execution (job handlers) run in
    the worker daemon. The caller inserts a ``pending`` row (see
    app/services/agent_relay.agent_exec); the API-process relay claims it,
    dispatches the matching frame to the connected agent, streams agent log
    frames into ``log_text`` (append-only), and stores the result payload.

    payload shapes: run_command ``{cmd, cwd, env, timeout}``; scan_bmc
    ``{subnet, timeout}``; file_write ``{path, b64, mode, backup_dir}``.
    result shapes: ``{rc, stdout, stderr}`` for run_command, ``{rc, found,
    error?}`` for scan_bmc, ``{rc, error?}`` for file_write, ``{error}`` on
    dispatch failure/timeout.
    """

    __tablename__ = "agent_commands"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    environment_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("environments.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # One of: run_command, scan_bmc, file_write
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    # One of: pending, dispatched, done, failed, timeout
    status: Mapped[str] = mapped_column(
        String(16), default="pending", nullable=False, index=True
    )
    log_text: Mapped[str] = mapped_column(Text, default="", nullable=False)
    result: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    finished_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class AlertEvent(Base):
    """One firing/resolved episode of an alert rule against an environment."""

    __tablename__ = "alert_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    rule_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("alert_rules.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    environment_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("environments.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # One of: firing, resolved
    status: Mapped[str] = mapped_column(String(16), default="firing", nullable=False)
    fired_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    resolved_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    acknowledged: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    details: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)


class App(Base):
    """Git-backed application or OpenStack stack attached to an environment."""

    __tablename__ = "apps"
    __table_args__ = (
        UniqueConstraint("environment_id", "name", name="uq_apps_env_name"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    environment_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("environments.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    repo_url: Mapped[str] = mapped_column(String(512), nullable=False)
    branch: Mapped[str] = mapped_column(String(128), default="main", nullable=False)
    root_path: Mapped[Optional[str]] = mapped_column(String(240), nullable=True)
    target: Mapped[str] = mapped_column(
        String(16), nullable=False
    )  # kubernetes | openstack
    build: Mapped[str] = mapped_column(String(16), default="auto", nullable=False)
    namespace: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    stack_name: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    webhook_id: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False, index=True
    )
    webhook_secret_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    deploy_token_encrypted: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    last_sha: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    last_job_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    last_status: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    poll_seconds: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_by: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )
