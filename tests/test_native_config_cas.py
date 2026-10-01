"""Config compare-and-swap protects concurrent native editor saves."""

import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from sqlalchemy import create_engine, event, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import Base
from app.models import EnvConfigVersion, Environment
from app.services.envconfig import put_version


def test_expected_version_conflict_route(client, admin_headers):
    env = client.post(
        "/api/v1/environments",
        headers=admin_headers,
        json={"name": "cas-" + uuid.uuid4().hex},
    ).json()
    path = f"/api/v1/environments/{env['id']}/config"
    first = client.put(
        path,
        headers=admin_headers,
        json={"yaml_text": "servers: {}\n", "expected_version": 0},
    )
    assert first.status_code == 201, first.text
    assert first.json()["version"] == 1
    stale = client.put(
        path,
        headers=admin_headers,
        json={"yaml_text": "servers: {}\n", "expected_version": 0},
    )
    assert stale.status_code == 409
    negative = client.put(
        path,
        headers=admin_headers,
        json={"yaml_text": "servers: {}\n", "expected_version": -1},
    )
    assert negative.status_code == 422
    second = client.put(
        path,
        headers=admin_headers,
        json={"yaml_text": "servers: {}\n", "expected_version": 1},
    )
    assert second.status_code == 201 and second.json()["version"] == 2
    legacy = client.put(
        path, headers=admin_headers, json={"yaml_text": "servers: {}\n"}
    )
    assert legacy.status_code == 201 and legacy.json()["version"] == 3


def test_simultaneous_expected_version_saves_use_unique_slot(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'race.db'}", connect_args={"timeout": 10}
    )
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        env = Environment(name="race")
        db.add(env)
        db.commit()
        eid = env.id
    ready = Barrier(2, timeout=5)

    def save(name):
        with Session(engine) as db:
            env = db.get(Environment, eid)

            # Both writers finish the latest-version read before either INSERT.
            def before_flush(*args):
                ready.wait()

            event.listen(db, "before_flush", before_flush, once=True)
            try:
                put_version(
                    db, env, f"servers:\n  {name}: {{}}\n", "test", expected_version=0
                )
                db.commit()
                return "saved"
            except IntegrityError:
                db.rollback()
                return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(save, ["writer-a", "writer-b"]))
    assert sorted(results) == ["conflict", "saved"]
    with Session(engine) as db:
        versions = list(db.scalars(select(EnvConfigVersion)))
        assert len(versions) == 1 and versions[0].version == 1
    engine.dispose()
