"""Walkthrough sample tenant/env — labeled, canned reads, never real BMC/kubectl."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import (
    BaremetalNode,
    ClusterSnapshot,
    Environment,
    Job,
    JobStatus,
    MetricSample,
    Tenant,
)
from app.services.crypto import encrypt_secret

TENANT_NAME = "demo"
ENV_NAME = "walkthrough"
ENV_TIER = "demo"
ENV_DESCRIPTION = "Sample full deployment. Not your metal. Create a real environment when you are ready."
DEMO_JOB_MESSAGE = "walkthrough is a sample. Create your own environment to run jobs."

# TEST-NET-1 (RFC 5737). Fake locally-administered MACs. Not lab addresses.
_NODES: tuple[dict[str, Any], ...] = (
    {
        "name": "control-a",
        "roles": ["control-plane", "control"],
        "bmc": "192.0.2.11",
        "ip": "192.0.2.21",
        "mac": "02:00:00:00:00:11",
        "cpu": "8",
        "mem_gi": 32.0,
    },
    {
        "name": "control-b",
        "roles": ["control-plane", "control"],
        "bmc": "192.0.2.12",
        "ip": "192.0.2.22",
        "mac": "02:00:00:00:00:12",
        "cpu": "8",
        "mem_gi": 32.0,
    },
    {
        "name": "worker-a",
        "roles": ["worker", "compute"],
        "bmc": "192.0.2.13",
        "ip": "192.0.2.23",
        "mac": "02:00:00:00:00:13",
        "cpu": "8",
        "mem_gi": 32.0,
    },
    {
        "name": "worker-b",
        "roles": ["worker", "compute"],
        "bmc": "192.0.2.14",
        "ip": "192.0.2.24",
        "mac": "02:00:00:00:00:14",
        "cpu": "8",
        "mem_gi": 32.0,
    },
)

_KUBE_VER = "v1.32.2"
_TALOS_VER = "v1.10.5"
_HELM = (
    ("keystone", "openstack", "keystone-2025.1.0", "2025.1"),
    ("nova", "openstack", "nova-2025.1.0", "2025.1"),
    ("neutron", "openstack", "neutron-2025.1.0", "2025.1"),
    ("glance", "openstack", "glance-2025.1.0", "2025.1"),
    ("coredns", "kube-system", "coredns-1.11.3", "1.11.3"),
)

_JOBS = (
    ("host.preflight", "connect hardware — walkthrough sample"),
    ("host.basic_ops", "fabric — walkthrough sample"),
    ("genestack.talos.bootstrap", "talos bootstrap — walkthrough sample"),
    ("genestack.deploy", "helm/openstack — walkthrough sample"),
    ("genestack.verify", "verify — walkthrough sample"),
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def is_demo_env(env: Environment | None) -> bool:
    """True when this environment is the labeled walkthrough sample."""
    if env is None:
        return False
    meta = (
        env.metadata_json
        if isinstance(getattr(env, "metadata_json", None), dict)
        else {}
    )
    if meta.get("demo") is True:
        return True
    return (
        str(getattr(env, "name", "") or "") == ENV_NAME
        and str(getattr(env, "tier", "") or "") == ENV_TIER
    )


def seed_demo_if_enabled(db: Session) -> dict[str, Any] | None:
    """No-op unless settings.seed_demo. Idempotent fill when enabled."""
    if not get_settings().seed_demo:
        return None
    return seed_demo(db)


def seed_demo(db: Session) -> dict[str, Any]:
    """Create tenant demo / env walkthrough and sample rows. Idempotent."""
    tenant = db.scalar(select(Tenant).where(Tenant.name == TENANT_NAME))
    if tenant is None:
        tenant = Tenant(
            name=TENANT_NAME,
            description="Walkthrough sample tenant. Not a real customer.",
        )
        db.add(tenant)
        db.flush()

    env = db.scalar(select(Environment).where(Environment.name == ENV_NAME))
    if env is None:
        env = Environment(
            name=ENV_NAME,
            description=ENV_DESCRIPTION,
            region="sample",
            tier=ENV_TIER,
            tenant_id=tenant.id,
            metadata_json={"demo": True},
            dry_run=True,
        )
        db.add(env)
        db.flush()
    else:
        meta = (
            dict(env.metadata_json or {}) if isinstance(env.metadata_json, dict) else {}
        )
        meta["demo"] = True
        env.metadata_json = meta
        env.tier = env.tier or ENV_TIER
        env.description = env.description or ENV_DESCRIPTION
        if not env.tenant_id:
            env.tenant_id = tenant.id

    _seed_nodes(db, env)
    _seed_jobs(db, env)
    _seed_snapshot(db, env)
    _seed_metrics(db, env)
    db.commit()
    return {
        "tenant_id": tenant.id,
        "tenant": TENANT_NAME,
        "environment_id": env.id,
        "environment": ENV_NAME,
        "nodes": len(_NODES),
        "demo": True,
    }


def _seed_nodes(db: Session, env: Environment) -> None:
    existing = {
        n.name
        for n in db.scalars(
            select(BaremetalNode).where(BaremetalNode.environment_id == env.id)
        ).all()
    }
    now = _utcnow()
    password = encrypt_secret("walkthrough") or "walkthrough"
    for spec in _NODES:
        if spec["name"] in existing:
            continue
        db.add(
            BaremetalNode(
                environment_id=env.id,
                name=spec["name"],
                bmc_host=spec["bmc"],
                bmc_username="walkthrough",
                bmc_password=password,
                pxe_mac=spec["mac"],
                expected_ip=spec["ip"],
                state="talos-ready",
                last_seen=now,
            )
        )


def _seed_jobs(db: Session, env: Environment) -> None:
    count = db.scalar(
        select(func.count())
        .select_from(Job)
        .where(Job.environment_id == env.id, Job.created_by == "walkthrough")
    )
    if count:
        return
    now = _utcnow()
    for i, (operation, log_text) in enumerate(_JOBS):
        started = now - timedelta(hours=6 - i)
        finished = started + timedelta(minutes=8)
        db.add(
            Job(
                environment_id=env.id,
                operation=operation,
                params={},
                status=JobStatus.success,
                log_text=log_text,
                created_by="walkthrough",
                started_at=started,
                finished_at=finished,
                created_at=started,
                dry_run=True,
            )
        )


def _seed_snapshot(db: Session, env: Environment) -> None:
    existing = db.scalar(
        select(ClusterSnapshot).where(ClusterSnapshot.environment_id == env.id).limit(1)
    )
    if existing is not None:
        return
    db.add(
        ClusterSnapshot(
            environment_id=env.id,
            probe_ok=True,
            error=None,
            nodes=_snapshot_nodes(),
            pods=_snapshot_pods(),
            helm=_snapshot_helm(),
            summary={
                "nodes_ready": 4,
                "nodes_total": 4,
                "pods_running": len(_snapshot_pods()),
                "pods_pending": 0,
                "pods_failed": 0,
                "crashlooping": [],
            },
            health="healthy",
        )
    )


def _seed_metrics(db: Session, env: Environment) -> None:
    count = db.scalar(
        select(func.count())
        .select_from(MetricSample)
        .where(MetricSample.environment_id == env.id)
    )
    if count:
        return
    now = _utcnow()
    series = (
        ("node.cpu.cores", 6.4),
        ("node.memory.bytes", 48_000_000_000.0),
        ("pod.cpu.cores", 3.1),
        ("cluster.nodes.ready", 4.0),
        ("cloud.servers.active", 2.0),
    )
    for minutes in (90, 60, 30, 5):
        ts = now - timedelta(minutes=minutes)
        for name, value in series:
            db.add(
                MetricSample(
                    environment_id=env.id,
                    ts=ts,
                    name=name,
                    labels={"demo": True},
                    value=value,
                )
            )


def _snapshot_nodes() -> list[dict[str, Any]]:
    return [
        {
            "name": spec["name"],
            "ready": True,
            "roles": list(spec["roles"]),
            "kubelet_version": _KUBE_VER,
        }
        for spec in _NODES
    ]


def _snapshot_pods() -> list[dict[str, Any]]:
    return [
        {
            "ns": p["namespace"],
            "name": p["name"],
            "phase": "Running",
            "ready": True,
            "restarts": 0,
            "node": p["node"],
            "waiting_reason": None,
        }
        for p in _PODS
    ]


def _snapshot_helm() -> list[dict[str, Any]]:
    return [
        {
            "name": name,
            "ns": ns,
            "status": "deployed",
            "chart": chart,
            "version": version,
        }
        for name, ns, chart, version in _HELM
    ]


_PODS: tuple[dict[str, str], ...] = (
    {
        "namespace": "openstack",
        "name": "keystone-api-0",
        "node": "control-a",
        "controller": "StatefulSet/keystone-api",
    },
    {
        "namespace": "openstack",
        "name": "nova-api-0",
        "node": "control-a",
        "controller": "Deployment/nova-api",
    },
    {
        "namespace": "openstack",
        "name": "neutron-server-0",
        "node": "control-b",
        "controller": "Deployment/neutron-server",
    },
    {
        "namespace": "openstack",
        "name": "glance-api-0",
        "node": "control-b",
        "controller": "Deployment/glance-api",
    },
    {
        "namespace": "openstack",
        "name": "cinder-volume-0",
        "node": "worker-a",
        "controller": "Deployment/cinder-volume",
    },
    {
        "namespace": "kube-system",
        "name": "coredns-0",
        "node": "control-a",
        "controller": "Deployment/coredns",
    },
    {
        "namespace": "kube-system",
        "name": "coredns-1",
        "node": "control-b",
        "controller": "Deployment/coredns",
    },
    {
        "namespace": "openstack",
        "name": "horizon-0",
        "node": "worker-b",
        "controller": "Deployment/horizon",
    },
)


def _k8s_node_row(spec: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": spec["name"],
        "roles": list(spec["roles"]),
        "status": "Ready",
        "unschedulable": False,
        "version": _KUBE_VER,
        "cpu_capacity": spec["cpu"],
        "mem_capacity": f"{int(spec['mem_gi'] * 1024)}Mi",
        "mem_gi": spec["mem_gi"],
        "internal_ip": spec["ip"],
        "external_ip": None,
        "hostname": spec["name"],
        "labels": {
            f"node-role.kubernetes.io/{spec['roles'][0]}": "",
            "kubernetes.io/hostname": spec["name"],
        },
        "taints": [],
        "conditions": [
            {
                "type": "Ready",
                "status": "True",
                "reason": "KubeletReady",
                "message": "walkthrough sample",
            }
        ],
        "capacity": {
            "cpu": spec["cpu"],
            "memory": f"{int(spec['mem_gi'])}Gi",
            "pods": "110",
        },
        "allocatable": {
            "cpu": spec["cpu"],
            "memory": f"{int(spec['mem_gi'])}Gi",
            "pods": "110",
        },
        "age": "",
    }


def _pod_row(p: dict[str, str]) -> dict[str, Any]:
    return {
        "namespace": p["namespace"],
        "name": p["name"],
        "node": p["node"],
        "phase": "Running",
        "ready": "1/1",
        "restarts": 0,
        "reason": None,
        "stale": False,
        "age": "",
        "controllers": [p["controller"]],
        "containers": [p["name"].rsplit("-", 1)[0]],
    }


def _filter_ns(rows: list[dict[str, Any]], namespace: str) -> list[dict[str, Any]]:
    ns = str(namespace or "").strip()
    if not ns:
        return rows
    return [r for r in rows if str(r.get("namespace") or "") == ns]


def canned_cluster_overview() -> dict[str, Any]:
    nodes = [_k8s_node_row(spec) for spec in _NODES]
    pods = [_pod_row(p) for p in _PODS]
    releases = [
        {
            "name": name,
            "namespace": ns,
            "status": "deployed",
            "chart": chart,
            "version": version,
            "ready_pods": 1,
            "ready_total": 1,
            "signal": "ok",
        }
        for name, ns, chart, version in _HELM
    ]
    return {
        "reachable": True,
        "nodes": nodes,
        "pods": {"total": len(pods), "running": len(pods), "problems": []},
        "releases": releases,
        "error": None,
        "health": "healthy",
        "health_reason": "walkthrough sample",
        "warnings": [],
        "access": {
            "api_server": None,
            "kubeconfig": False,
            "talosconfig": False,
            "gateway": "192.0.2.10",
            "horizon": "https://horizon.example.test",
        },
        "resources": {"cpu": 32, "memory_gi": 128.0},
        "demo": True,
    }


def canned_openstack_overview() -> dict[str, Any]:
    return {
        "available": True,
        "source": "walkthrough",
        "users": [{"name": "admin"}, {"name": "demo"}],
        "compute_services": [
            {
                "name": "nova-compute",
                "host": spec["name"],
                "zone": "nova",
                "status": "enabled",
                "state": "up",
            }
            for spec in _NODES
            if "compute" in spec["roles"]
        ],
        "network_agents": [
            {
                "type": "ovn-controller",
                "host": spec["name"],
                "alive": ":-)",
                "state": "up",
            }
            for spec in _NODES
        ],
        "images": [{"name": "cirros", "status": "active"}],
        "error": None,
        "demo": True,
    }


def canned_environment_health() -> dict[str, Any]:
    chips = [
        {"id": "metal", "label": "Metal", "state": "ok", "detail": "4 sample nodes"},
        {"id": "k8s", "label": "K8s", "state": "ok", "detail": "4/4 Ready"},
        {
            "id": "identity",
            "label": "Identity",
            "state": "ok",
            "detail": "keystone-api",
        },
        {"id": "compute", "label": "Compute", "state": "ok", "detail": "nova-api"},
        {
            "id": "network",
            "label": "Network",
            "state": "ok",
            "detail": "neutron-server",
        },
        {"id": "volume", "label": "Volume", "state": "ok", "detail": "cinder-volume"},
        {
            "id": "proof",
            "label": "Proof",
            "state": "ok",
            "detail": "sample tempest — walkthrough",
            "ok": 42,
            "fail": 0,
            "error": 0,
            "skipped": 0,
            "job_id": None,
        },
    ]
    services = [
        {"id": sid, "label": label, "state": "ok", "ready": 1, "total": 1}
        for sid, label in (
            ("identity", "Identity"),
            ("compute", "Compute"),
            ("network", "Network"),
            ("volume", "Volume"),
        )
    ]
    return {
        "generated_at": _utcnow().isoformat(),
        "error": None,
        "chips": chips,
        "job": {
            "kind": "none",
            "id": None,
            "operation": "",
            "status": "",
            "stage": "",
            "started_at": None,
            "error": None,
        },
        "services": services,
        "demo": True,
    }


def canned_cluster_logs(
    *, namespace: str, pod: str, container: str | None, tail: int, previous: bool
) -> dict[str, Any]:
    text = (
        f"# walkthrough sample log for {namespace}/{pod}\n"
        "This is a sample. kubectl is not running against real metal.\n"
        f"{pod} 1/1 Running on the walkthrough cluster.\n"
    )
    return {
        "namespace": namespace,
        "pod": pod,
        "container": container,
        "tail": tail,
        "previous": previous,
        "text": text,
        "error": None,
        "demo": True,
    }


def canned_workloads(*, namespace: str = "", pods_only: bool = False) -> dict[str, Any]:
    ns = str(namespace or "").strip()
    pods = _filter_ns([_pod_row(p) for p in _PODS], ns)
    if pods_only:
        return {
            "ok": True,
            "namespace": ns,
            "deployments": [],
            "statefulsets": [],
            "daemonsets": [],
            "pods": pods,
            "error": None,
            "demo": True,
        }
    deployments = [
        {
            "kind": "Deployment",
            "namespace": p["namespace"],
            "name": p["controller"].split("/", 1)[-1],
            "replicas": 1,
            "ready": 1,
            "available": 1,
            "updated": 1,
            "images": [],
            "age": "",
        }
        for p in _PODS
        if p["controller"].startswith("Deployment/")
        and (not ns or p["namespace"] == ns)
    ]
    statefulsets = [
        {
            "kind": "StatefulSet",
            "namespace": p["namespace"],
            "name": p["controller"].split("/", 1)[-1],
            "replicas": 1,
            "ready": 1,
            "available": 1,
            "updated": 1,
            "images": [],
            "age": "",
        }
        for p in _PODS
        if p["controller"].startswith("StatefulSet/")
        and (not ns or p["namespace"] == ns)
    ]
    return {
        "ok": True,
        "namespace": ns,
        "deployments": deployments,
        "statefulsets": statefulsets,
        "daemonsets": [],
        "pods": pods,
        "error": None,
        "demo": True,
    }


def canned_k8s_nodes() -> dict[str, Any]:
    return {
        "ok": True,
        "error": None,
        "nodes": [_k8s_node_row(spec) for spec in _NODES],
        "demo": True,
    }


def canned_services(*, namespace: str = "") -> dict[str, Any]:
    ns = str(namespace or "").strip()
    rows = [
        {
            "namespace": "openstack",
            "name": "public-lb",
            "type": "LoadBalancer",
            "cluster_ip": "10.96.0.20",
            "external_ips": ["192.0.2.10"],
            "ports": ["https:443/TCP"],
            "selector": {"app": "horizon"},
            "load_balancer": ["192.0.2.10"],
            "age": "",
        },
        {
            "namespace": "openstack",
            "name": "keystone-api",
            "type": "ClusterIP",
            "cluster_ip": "10.96.0.21",
            "external_ips": [],
            "ports": ["http:5000/TCP"],
            "selector": {"app": "keystone"},
            "load_balancer": [],
            "age": "",
        },
        {
            "namespace": "kube-system",
            "name": "kube-dns",
            "type": "ClusterIP",
            "cluster_ip": "10.96.0.10",
            "external_ips": [],
            "ports": ["dns:53/UDP", "dns-tcp:53/TCP"],
            "selector": {"k8s-app": "kube-dns"},
            "load_balancer": [],
            "age": "",
        },
    ]
    return {
        "ok": True,
        "error": None,
        "namespace": ns,
        "services": _filter_ns(rows, ns),
        "demo": True,
    }


def canned_ingresses(*, namespace: str = "") -> dict[str, Any]:
    ns = str(namespace or "").strip()
    rows = [
        {
            "namespace": "openstack",
            "name": "horizon",
            "class": "gateway",
            "hosts": ["horizon.example.test"],
            "urls": ["https://horizon.example.test"],
            "backends": [{"namespace": "openstack", "name": "horizon", "port": 80}],
            "address": "192.0.2.10",
            "tls": True,
            "age": "",
        }
    ]
    return {
        "ok": True,
        "error": None,
        "namespace": ns,
        "ingresses": _filter_ns(rows, ns),
        "demo": True,
    }


def canned_gateways(*, namespace: str = "") -> dict[str, Any]:
    ns = str(namespace or "").strip()
    rows = [
        {
            "namespace": "envoy-gateway",
            "name": "flex-gateway",
            "class": "eg",
            "addresses": ["192.0.2.10"],
            "listeners": ["HTTPS/443/https"],
            "address": "192.0.2.10",
            "age": "",
        }
    ]
    return {
        "ok": True,
        "error": None,
        "namespace": ns,
        "gateways": _filter_ns(rows, ns),
        "demo": True,
    }


def canned_httproutes(*, namespace: str = "") -> dict[str, Any]:
    ns = str(namespace or "").strip()
    rows = [
        {
            "namespace": "openstack",
            "name": "custom-horizon-gateway-route",
            "hosts": ["horizon.example.test"],
            "parent_refs": [
                {
                    "namespace": "envoy-gateway",
                    "name": "flex-gateway",
                    "kind": "Gateway",
                }
            ],
            "backends": [
                {
                    "namespace": "openstack",
                    "name": "horizon",
                    "kind": "Service",
                    "port": 80,
                }
            ],
            "urls": ["https://horizon.example.test"],
            "tls": False,
            "age": "",
        }
    ]
    return {
        "ok": True,
        "error": None,
        "namespace": ns,
        "httproutes": _filter_ns(rows, ns),
        "demo": True,
    }


def canned_metallb_pools() -> dict[str, Any]:
    return {
        "ok": True,
        "error": None,
        "pools": [
            {
                "name": "public-pool",
                "addresses": ["192.0.2.10/32"],
                "auto_assign": True,
                "age": "",
            }
        ],
        "demo": True,
    }


def canned_vms() -> dict[str, Any]:
    return {
        "vms": _vm_rows(),
        "source": "walkthrough",
        "error": None,
        "demo": True,
    }


def _vm_rows() -> list[dict[str, Any]]:
    return [
        {
            "id": "00000000-0000-4000-8000-000000000001",
            "name": "demo-web-1",
            "status": "ACTIVE",
            "power_state": "Running",
            "flavor": "m1.small",
            "image": "cirros",
            "addresses": {"private": ["10.0.0.12"]},
            "created": None,
            "host": "worker-a",
            "project_id": "00000000-0000-4000-8000-0000000000aa",
            "project_name": "demo",
        },
        {
            "id": "00000000-0000-4000-8000-000000000002",
            "name": "demo-db-1",
            "status": "ACTIVE",
            "power_state": "Running",
            "flavor": "m1.small",
            "image": "cirros",
            "addresses": {"private": ["10.0.0.13"]},
            "created": None,
            "host": "worker-b",
            "project_id": "00000000-0000-4000-8000-0000000000aa",
            "project_name": "demo",
        },
    ]


def canned_cloud_inventory() -> dict[str, Any]:
    net_id = "00000000-0000-4000-8000-0000000000n1"
    subnet_id = "00000000-0000-4000-8000-0000000000s1"
    out: dict[str, Any] = {
        "available": True,
        "source": "walkthrough",
        "error": None,
        "cached": False,
        "demo": True,
        "servers": _vm_rows(),
        "images": [
            {"id": "img-cirros", "name": "cirros", "status": "active", "size": 0}
        ],
        "flavors": [
            {"id": "m1.small", "name": "m1.small", "vcpus": 1, "ram": 2048, "disk": 20}
        ],
        "volumes": [],
        "networks": [
            {
                "id": net_id,
                "name": "private",
                "subnets": [subnet_id],
                "shared": False,
                "external": False,
                "status": "ACTIVE",
                "project_id": "00000000-0000-4000-8000-0000000000aa",
            }
        ],
        "subnets": [
            {
                "id": subnet_id,
                "name": "private-subnet",
                "network": net_id,
                "cidr": "10.0.0.0/24",
                "project_id": "00000000-0000-4000-8000-0000000000aa",
            }
        ],
        "routers": [],
        "ports": [],
        "floating_ips": [],
        "security_groups": [],
        "keypairs": [],
        "projects": [
            {
                "id": "00000000-0000-4000-8000-0000000000aa",
                "name": "demo",
                "enabled": True,
            }
        ],
        "users": [{"id": "admin", "name": "admin", "enabled": True}],
        "volume_snapshots": [],
        "quotas": {"compute": {}, "network": {}},
        "quotas_error": None,
        "load_balancers": [],
        "load_balancers_error": None,
        "load_balancers_available": False,
        "dns_zones": [],
        "dns_zones_error": None,
        "dns_zones_available": False,
        "secrets": [],
        "secrets_error": None,
        "secrets_available": False,
    }
    for key in (
        "servers",
        "images",
        "flavors",
        "volumes",
        "networks",
        "subnets",
        "routers",
        "ports",
        "floating_ips",
        "security_groups",
        "keypairs",
        "projects",
        "users",
        "volume_snapshots",
    ):
        out[f"{key}_error"] = None
    return out


def canned_observe(env: Environment, *, hours: int = 24) -> dict[str, Any]:
    hours = max(1, min(168, int(hours or 24)))
    now = _utcnow()
    live = {
        "talos": {
            "reachable": 4,
            "machines": 4,
            "versions": [_TALOS_VER],
        },
        "kubernetes": {
            "health": "healthy",
            "nodes": 4,
            "ready": 4,
            "pods": len(_PODS),
        },
        "openstack": {
            "available": True,
            "servers": 2,
            "volumes": 0,
            "networks": 1,
        },
        "jobs": {"running": 0, "failed": 0},
        "alerts": {"firing": 0},
    }
    series: dict[str, list[dict[str, Any]]] = {}
    values = {
        "node.cpu.cores": 6.4,
        "node.memory.bytes": 48_000_000_000.0,
        "pod.cpu.cores": 3.1,
        "cluster.nodes.ready": 4.0,
        "cloud.servers.active": 2.0,
    }
    for name, value in values.items():
        series[name] = [
            {"t": (now - timedelta(minutes=m)).isoformat(), "v": value}
            for m in (90, 60, 30, 5)
        ]
    return {
        "environment_id": env.id,
        "name": env.name,
        "generated_at": now,
        "hours": hours,
        "health": "healthy",
        "metrics_enabled": True,
        "live": live,
        "plane": {
            "talos": {"reachable": 4, "machines": 4},
            "kubernetes": {"ready": 4, "nodes": 4},
            "openstack": {"instances": 2},
            "jobs": {"running": 0},
            "alerts": {"firing": 0},
        },
        "now": {
            "problem_pods": 0,
            "helm_failed": 0,
            "volume_errors": 0,
            "problems": [],
        },
        "series": series,
        "names": list(values.keys()),
        "demo": True,
    }


def canned_observe_logs(
    *, query: str | None, namespace: str | None, pod: str | None, since: str, limit: int
) -> dict[str, Any]:
    lines = [
        "walkthrough sample log — not Loki, not your cluster.",
        "keystone-api 1/1 Running",
        "nova-api 1/1 Running",
    ]
    return {
        "ok": True,
        "error": None,
        "query": query or "",
        "namespace": namespace,
        "pod": pod,
        "since": since,
        "lines": lines[: int(limit or 200)],
        "count": min(len(lines), int(limit or 200)),
        "demo": True,
    }


def canned_platform_overview(env: Environment) -> dict[str, Any]:
    nodes: list[dict[str, Any]] = []
    for spec in _NODES:
        kn = _k8s_node_row(spec)
        nova = None
        if "compute" in spec["roles"]:
            nova = {
                "binary": "nova-compute",
                "host": spec["name"],
                "status": "enabled",
                "state": "up",
            }
        nodes.append(
            {
                "name": spec["name"],
                "public_ip": spec["ip"],
                "private_ip": spec["ip"],
                "roles": list(spec["roles"]),
                "talos": {"reachable": True, "version": _TALOS_VER, "error": None},
                "kubernetes": kn,
                "openstack": nova,
            }
        )
    return {
        "nodes": nodes,
        "error": None,
        "install_image": f"ghcr.io/siderolabs/installer:{_TALOS_VER}",
        "cluster": {
            "name": str(getattr(env, "name", "") or ENV_NAME),
            "machines": 4,
            "control_planes": 2,
            "workers": 2,
            "ready": 4,
            "not_ready": 0,
            "talos_reachable": 4,
            "talos_versions": [_TALOS_VER],
            "kubernetes_versions": [_KUBE_VER],
            "install_image": f"ghcr.io/siderolabs/installer:{_TALOS_VER}",
        },
        "demo": True,
    }


def canned_talos_read(name: str) -> dict[str, Any]:
    spec = next((s for s in _NODES if s["name"] == name), None)
    ip = spec["ip"] if spec else "192.0.2.21"
    text = (
        f"walkthrough sample — node {name}\n"
        "Talos is not connected. This is canned output so the UI can render.\n"
        f"health: ok  version: {_TALOS_VER}\n"
    )
    return {
        "ok": True,
        "error": None,
        "text": text,
        "node": name,
        "ip": ip,
        "members": text,
        "status": "ok",
        "status_error": None,
        "demo": True,
    }


def canned_live_metrics() -> dict[str, Any]:
    nodes = []
    for spec in _NODES:
        nodes.append(
            {
                "name": spec["name"],
                "cpu": {"used": 1.6, "cap": float(spec["cpu"]), "pct": 20.0},
                "mem": {
                    "used": 8_000_000_000.0,
                    "cap": spec["mem_gi"] * 1024**3,
                    "pct": 25.0,
                },
                "disk": {
                    "used": 50_000_000_000.0,
                    "cap": 200_000_000_000.0,
                    "pct": 25.0,
                },
            }
        )
    return {
        "ts": _utcnow().isoformat(),
        "source": "walkthrough",
        "error": None,
        "cluster": {
            "cpu": {"used": 6.4, "cap": 32.0, "pct": 20.0},
            "mem": {"used": 32_000_000_000.0, "cap": 128_000_000_000.0, "pct": 25.0},
            "disk": {"used": 200_000_000_000.0, "cap": 800_000_000_000.0, "pct": 25.0},
            "cores": 32,
        },
        "cpus": [],
        "nodes": nodes,
        "pods": [],
        "demo": True,
    }


def canned_pipeline() -> dict[str, Any]:
    from app.services.service_registry import PIPELINE_STAGES, filter_stage_items

    stages: list[dict[str, Any]] = []
    for spec in PIPELINE_STAGES:
        items = []
        for raw in filter_stage_items(spec, None, None):
            items.append(
                {
                    "name": str(raw.get("name") or ""),
                    "script": str(raw.get("script") or ""),
                    "state": "done",
                }
            )
        stages.append(
            {
                "id": spec["id"],
                "name": spec["name"],
                "description": spec.get("description") or "",
                "state": "done",
                "required": bool(spec.get("required", True)),
                "control": str(spec.get("control") or "helm"),
                "items": items,
            }
        )
    total = sum(len(s.get("items") or []) for s in stages)
    return {
        "stages": stages,
        "next_stage": None,
        "can_continue": False,
        "running": False,
        "failed_at": None,
        "release_count": len(_HELM),
        "node_count": 4,
        "cluster_reachable": True,
        "current": None,
        "remaining": [],
        "done_count": total,
        "total_count": total,
        "elapsed_s": None,
        "timings": {},
        "demo": True,
    }
