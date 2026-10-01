"""Native presentation contract built only from stored, environment-scoped facts."""

from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal
from urllib.parse import quote

import yaml
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.deps import get_db, get_env_scoped, require_viewer
from app.models import BaremetalNode, ClusterSnapshot, EnvConfigVersion, Environment
from app.schemas import Principal
from app.services.demo import is_demo_env

router = APIRouter(prefix="/api/v1", tags=["native"])
MAX_NODES = 2000
MAX_EDGES = 4000


class TopologyNode(BaseModel):
    id: str
    label: str
    kind: str
    layer: Literal["metal", "overlay", "k8s", "pods", "nova", "tenants", "edge"]
    parent_id: str | None = None
    status: str = "unknown"
    detail: str | None = None


class TopologyEdge(BaseModel):
    id: str
    source: str
    target: str
    kind: str


class NativeTopology(BaseModel):
    schema_version: Literal[1] = 1
    environment_id: str
    title: str
    generated_at: datetime
    is_demo: bool
    nodes: list[TopologyNode] = Field(default_factory=list)
    edges: list[TopologyEdge] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


def _id(kind: str, *parts: str) -> str:
    return kind + ":" + ":".join(quote(p, safe="") for p in parts)


def build_topology(db: Session, env: Environment) -> NativeTopology:
    """Allowlist identities and observed states; never serialize credentials/config/errors.

    generated_at is graph assembly time, NOT observation time. Observations older
    than five minutes remain visible but all their health values become unknown.
    No remote subprocesses or provider calls are made during this read.
    """
    now = datetime.now(UTC)
    out = NativeTopology(
        environment_id=env.id,
        title=env.name,
        generated_at=now,
        is_demo=is_demo_env(env),
    )
    if out.is_demo:
        out.warnings.append("Walkthrough sample data; not a live environment.")
    nodes: dict[str, TopologyNode] = {}

    def add(kind, name, layer, *, key=None, parent=None, status="unknown", detail=None):
        nid = key or _id(kind, name)
        nodes[nid] = TopologyNode(
            id=nid,
            label=name,
            kind=kind,
            layer=layer,
            parent_id=parent,
            status=status,
            detail=detail,
        )
        return nid

    def link(source, target, kind):
        if source in nodes and target in nodes:
            out.edges.append(
                TopologyEdge(
                    id=_id(kind, source, target),
                    source=source,
                    target=target,
                    kind=kind,
                )
            )

    machines = {}
    for row in db.scalars(
        select(BaremetalNode)
        .where(BaremetalNode.environment_id == env.id)
        .order_by(BaremetalNode.name)
    ):
        machines[row.name] = add(
            "machine",
            row.name,
            "metal",
            detail="Registered hardware; live health unknown.",
        )
    config = db.scalar(
        select(EnvConfigVersion)
        .where(EnvConfigVersion.environment_id == env.id)
        .order_by(EnvConfigVersion.version.desc())
        .limit(1)
    )
    if config:
        try:
            doc = yaml.safe_load(config.yaml_text)
            servers = doc.get("servers", {}) if isinstance(doc, dict) else {}
            if not isinstance(servers, dict):
                raise TypeError("Invalid server assignments")
            # Legacy MAAS documents use system IDs as keys and carry the
            # hostname in the assignment. Match the config reader's identity.
            names = {
                assignment.get("hostname") or key
                for key, assignment in servers.items()
                if isinstance(key, str)
                and isinstance(assignment, dict)
                and isinstance(assignment.get("hostname") or key, str)
            }
            for name in sorted(names):
                if name not in machines:
                    machines[name] = add(
                        "machine",
                        name,
                        "metal",
                        detail="Configured server; live health unknown.",
                    )
        except (yaml.YAMLError, TypeError):
            out.warnings.append("Stored server configuration could not be read.")
    snap = db.scalar(
        select(ClusterSnapshot)
        .where(ClusterSnapshot.environment_id == env.id)
        .order_by(ClusterSnapshot.taken_at.desc(), ClusterSnapshot.id.desc())
        .limit(1)
    )
    if snap is None:
        out.warnings.append("No cluster snapshot recorded; live topology is unknown.")
    else:
        taken = (
            snap.taken_at.replace(tzinfo=UTC)
            if snap.taken_at.tzinfo is None
            else snap.taken_at
        )
        fresh = snap.probe_ok and timedelta(0) <= now - taken <= timedelta(minutes=5)
        out.warnings.append(f"Cluster observations recorded at {taken.isoformat()}.")
        if not snap.probe_ok:
            out.warnings.append(
                "Latest cluster probe failed; current health is unknown."
            )
        elif not fresh:
            out.warnings.append("Cluster snapshot is stale; current health is unknown.")
        kube = {}
        for row in snap.nodes:
            if not isinstance(row, dict) or not isinstance(row.get("name"), str):
                continue
            name = row["name"]
            state = (
                "healthy"
                if row.get("ready") is True
                else "degraded" if row.get("ready") is False else "unknown"
            )
            kube[name] = add(
                "k8s",
                name,
                "k8s",
                parent=machines.get(name),
                status=state if fresh else "unknown",
            )
            if name in machines:
                link(machines[name], kube[name], "hosts")
        for row in snap.pods:
            if not isinstance(row, dict) or not all(
                isinstance(row.get(k), str) for k in ("ns", "name")
            ):
                continue
            ns, name = row["ns"], row["name"]
            namespace = add("ns", ns, "k8s")
            state = (
                "healthy"
                if row.get("phase") == "Running" and row.get("ready") is True
                else "unknown"
            )
            if row.get("phase") in ("Failed", "Pending"):
                state = "degraded"
            pod = add(
                "pod",
                name,
                "pods",
                key=_id("pod", ns, name),
                parent=namespace,
                status=state if fresh else "unknown",
            )
            link(namespace, pod, "contains")
            if row.get("node") in kube:
                link(kube[row["node"]], pod, "schedules")
        for row in snap.helm:
            if not isinstance(row, dict) or not all(
                isinstance(row.get(k), str) for k in ("ns", "name")
            ):
                continue
            namespace = add("ns", row["ns"], "k8s")
            release = add(
                "helm",
                row["name"],
                "k8s",
                key=_id("helm", row["ns"], row["name"]),
                parent=namespace,
                detail="Observed Helm release; workload health unknown.",
            )
            link(namespace, release, "contains")
    out.warnings.append(
        "Network, Nova, tenant and ingress resources are not present in stored snapshots; empty layers do not prove absence."
    )
    # Bound native scene allocation. Keep deterministic identities and never
    # return dangling graph references when the observed inventory is large.
    out.nodes = sorted(nodes.values(), key=lambda n: n.id)[:MAX_NODES]
    if len(nodes) > MAX_NODES:
        out.warnings.append(
            f"Topology truncated to {MAX_NODES} nodes; use inventory views for all resources."
        )
    kept = {node.id for node in out.nodes}
    for node in out.nodes:
        if node.parent_id not in kept:
            node.parent_id = None
    edges = {
        edge.id: edge
        for edge in out.edges
        if edge.source in kept and edge.target in kept
    }
    out.edges = sorted(edges.values(), key=lambda edge: edge.id)[:MAX_EDGES]
    if len(edges) > MAX_EDGES:
        out.warnings.append(f"Topology truncated to {MAX_EDGES} relationships.")
    return out


@router.get(
    "/environments/{environment_id}/native/topology", response_model=NativeTopology
)
def get_native_topology(
    db: Annotated[Session, Depends(get_db)],
    env: Annotated[Environment, Depends(get_env_scoped("viewer"))],
) -> NativeTopology:
    return build_topology(db, env)


@router.get("/native/capabilities")
def native_capabilities(
    principal: Annotated[Principal, Depends(require_viewer)],
) -> dict:
    return {
        "schema_version": 1,
        "topology_schema_version": 1,
        "stream_protocol": 2,
        "kubernetes_watch": True,
        "pod_log_follow": True,
        "native_console_descriptors": True,
        "stream_replay": False,
        "stream_bootstrap_required": True,
        "config_compare_and_swap": True,
        "invalidation_sources": [
            "snapshots",
            "jobs",
            "alerts",
            "configuration",
            "hardware",
            "metrics",
        ],
    }
