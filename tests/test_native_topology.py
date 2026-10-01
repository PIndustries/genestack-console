"""Native topology scope, provenance and secret boundary regression tests."""

import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db import Base, SessionLocal
from app.models import BaremetalNode, ClusterSnapshot, EnvConfigVersion, Environment
from app.routers.native import NativeTopology, build_topology


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def seed(db, *, demo=False, age=0, probe_ok=True):
    env = Environment(name="native-test", metadata_json={"demo": demo})
    other = Environment(name="other")
    db.add_all([env, other])
    db.flush()
    db.add_all(
        [
            BaremetalNode(
                environment_id=env.id,
                name="worker-a",
                bmc_host="private-bmc",
                bmc_username="private-user",
                bmc_password="SECRET-CREDENTIAL",
            ),
            BaremetalNode(
                environment_id=other.id,
                name="hidden-machine",
                bmc_host="hidden",
                bmc_username="hidden",
                bmc_password="OTHER-SECRET",
            ),
            EnvConfigVersion(
                environment_id=env.id,
                version=1,
                yaml_text="servers:\n  worker-a:\n    ssh_password: SECRET-SSH\n  worker-b: {}\n",
            ),
            ClusterSnapshot(
                environment_id=env.id,
                taken_at=datetime.now(UTC) - timedelta(minutes=age),
                probe_ok=probe_ok,
                error="SECRET-ERROR",
                nodes=[{"name": "worker-a", "ready": True}],
                pods=[
                    {
                        "ns": "openstack",
                        "name": "nova-api",
                        "phase": "Running",
                        "ready": True,
                        "node": "worker-a",
                    }
                ],
                helm=[{"ns": "openstack", "name": "nova", "status": "deployed"}],
            ),
        ]
    )
    db.flush()
    return env


def test_graph_is_observed_scoped_and_secret_free(db):
    graph = build_topology(db, seed(db))
    NativeTopology.model_validate_json(graph.model_dump_json())
    assert len(graph.nodes) == 6
    assert len({node.id for node in graph.nodes}) == len(graph.nodes)
    by_id = {node.id: node for node in graph.nodes}
    assert by_id["machine:worker-a"].status == "unknown"
    assert by_id["k8s:worker-a"].status == "healthy"
    assert by_id["pod:openstack:nova-api"].status == "healthy"
    assert any(edge.kind == "schedules" for edge in graph.edges)
    assert all(edge.source in by_id and edge.target in by_id for edge in graph.edges)
    text = graph.model_dump_json()
    for forbidden in (
        "SECRET",
        "private-bmc",
        "private-user",
        "hidden-machine",
        "ssh_password",
    ):
        assert forbidden not in text
    assert not any(
        n.layer in ("nova", "overlay", "tenants", "edge") for n in graph.nodes
    )


@pytest.mark.parametrize("age,probe_ok", [(6, True), (0, False), (-5, True)])
def test_stale_failed_future_snapshots_never_report_healthy(db, age, probe_ok):
    graph = build_topology(db, seed(db, age=age, probe_ok=probe_ok))
    assert all(n.status == "unknown" for n in graph.nodes)
    assert any("unknown" in warning for warning in graph.warnings)


def test_empty_environment_and_demo_are_explicit(db):
    env = Environment(name="empty")
    db.add(env)
    db.flush()
    graph = build_topology(db, env)
    assert graph.nodes == [] and graph.edges == [] and not graph.is_demo
    assert any("No cluster snapshot" in w for w in graph.warnings)
    demo = build_topology(db, seed(db, demo=True))
    assert demo.is_demo
    assert any("sample" in w for w in demo.warnings)


@pytest.fixture
def route_client():
    # Isolate the new router from unrelated legacy app startup dependencies.
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.routers.native import router

    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        yield client


def test_native_route_enforces_auth_tenant_membership(route_client):
    from app.models import Membership, Tenant, User, UserRole
    from app.services.accounts import create_session

    with SessionLocal() as db:
        tenant = Tenant(name="native-scope-tenant")
        user = User(username="native-viewer", role=UserRole.viewer)
        db.add_all([tenant, user])
        db.flush()
        own = Environment(name="native-own", tenant_id=tenant.id)
        other = Environment(name="native-other")
        db.add_all(
            [
                own,
                other,
                Membership(user_id=user.id, tenant_id=tenant.id, role=UserRole.viewer),
            ]
        )
        db.commit()
        session = create_session(db, user)
        db.commit()
        token = session.token
        own_id, other_id = own.id, other.id
    path = f"/api/v1/environments/{own_id}/native/topology"
    assert route_client.get(path).status_code == 401
    headers = {"Authorization": f"Bearer {token}"}
    response = route_client.get(path, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["environment_id"] == own_id
    assert (
        route_client.get(
            f"/api/v1/environments/{other_id}/native/topology", headers=headers
        ).status_code
        == 403
    )
    assert (
        route_client.get(
            f"/api/v1/environments/{uuid.uuid4()}/native/topology", headers=headers
        ).status_code
        == 404
    )


def test_demo_endpoint_fixture(route_client, admin_headers):
    with SessionLocal() as db:
        env = seed(db, demo=True)
        env.name = "Native walkthrough test fixture"
        db.commit()
        eid = env.id
    response = route_client.get(
        f"/api/v1/environments/{eid}/native/topology", headers=admin_headers
    )
    assert response.status_code == 200
    assert response.json()["is_demo"] is True
    Path("/tmp/genestack-native-topology-demo.json").write_text(
        json.dumps(response.json(), indent=2)
    )


def test_topology_is_registered_in_full_application(client, admin_headers):
    response = client.get(
        f"/api/v1/environments/{uuid.uuid4()}/native/topology", headers=admin_headers
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "Environment not found"


def test_topology_caps_are_explicit_without_dangling_references(db, monkeypatch):
    monkeypatch.setattr("app.routers.native.MAX_NODES", 3)
    monkeypatch.setattr("app.routers.native.MAX_EDGES", 1)
    graph = build_topology(db, seed(db))
    ids = {node.id for node in graph.nodes}
    assert len(graph.nodes) == 3 and len(graph.edges) <= 1
    assert all(node.parent_id is None or node.parent_id in ids for node in graph.nodes)
    assert all(edge.source in ids and edge.target in ids for edge in graph.edges)
    assert any("truncated" in warning for warning in graph.warnings)


def test_legacy_server_keys_use_hostname_without_inventing_duplicate_machine(db):
    env = seed(db)
    db.add(
        EnvConfigVersion(
            environment_id=env.id,
            version=2,
            yaml_text="servers:\n  maas-system-id:\n    hostname: worker-a\n",
        )
    )
    db.flush()
    graph = build_topology(db, env)
    assert [n.id for n in graph.nodes if n.kind == "machine"] == ["machine:worker-a"]
    assert any(
        e.source == "machine:worker-a" and e.target == "k8s:worker-a"
        for e in graph.edges
    )
